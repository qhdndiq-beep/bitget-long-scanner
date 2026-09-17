
#!/usr/bin/env python3
"""
Bitget 4H -> 15M PRE-LONG / PRE-SHORT WATCH v1
Independent observation scanner.

Core sequence:
4H Sweep -> 4H CISD -> 4H Key Level
-> 15M opposite FVG -> IFVG conversion
-> IFVG retest/HOLD -> compression
-> breakout NOT YET confirmed.

This file does not modify the existing confirmed scanner and places no orders.
The workflow stores the report as an artifact for research.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

H4_LIMIT = 120
M15_LIMIT = 160
MAX_WORKERS = 8
TIMEOUT = 12
RETRIES = 3

LIQUIDITY_LOOKBACK = 8
MAX_SWEEP_BARS = 8
MAX_SWEEP_TO_CISD = 8
MAX_CISD_RECENCY = 8
MIN_CISD_BODY = 0.30

KEY_LOOKBACK = 80
KEY_ATR_PERIOD = 14
KEY_MAX_DISTANCE_ATR = 0.75
MIN_DEPARTURE_BODY = 0.45

ATR_PERIOD = 14
RVOL_PERIOD = 20
FVG_MIN_ATR = 0.08
IFVG_MAX_AGE = 48
PATTERN_LOOKBACK = 80

COMPRESSION_LOOKBACK = 8
COMPRESSION_MAX_ATR = 2.20

PREMOVE_FAST = 4
PREMOVE_SLOW = 8
PREMOVE_MAX = 0.035

BOX_LOOKBACK = 32
MAX_BOX_DISTANCE_ATR = 0.35

MIN_PRE_SCORE = 60.0
MAX_RESULTS = 10
REPORT_FILE = "pre_long_candidates.json"


class Candle:
    def __init__(self, row):
        self.ts = int(row[0])
        self.o = float(row[1])
        self.h = float(row[2])
        self.l = float(row[3])
        self.c = float(row[4])
        self.v = float(row[5])


def get_json(path, params):
    url = f"{BASE_URL}{path}?{urlencode(params)}"
    last = None
    for attempt in range(RETRIES):
        try:
            req = Request(url, headers={"User-Agent": "bitget-pre-long-v1/1.0"})
            with urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode())
            if data.get("code") != "00000":
                raise RuntimeError(f"{data.get('code')} {data.get('msg')}")
            return data
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as e:
            last = e
            time.sleep(0.7 * (attempt + 1))
    raise RuntimeError(f"API failed: {path}: {last}")


def get_symbols():
    data = get_json("/api/v3/market/instruments", {"category": PRODUCT_TYPE})
    out = []
    for x in data.get("data", []):
        s = str(x.get("symbol", "")).strip()
        if (
            s.endswith("USDT")
            and str(x.get("quoteCoin", "")).upper() == "USDT"
            and str(x.get("symbolType", "")).lower() == "crypto"
            and str(x.get("type", "")).lower() == "perpetual"
            and str(x.get("status", "")).lower() == "online"
            and str(x.get("isRwa", "YES")).upper() == "NO"
        ):
            out.append(s)
    return sorted(set(out))


def get_candles(symbol, granularity, limit):
    data = get_json(
        "/api/v2/mix/market/history-candles",
        {"symbol": symbol, "productType": PRODUCT_TYPE,
         "granularity": granularity, "limit": limit},
    )
    rows = [Candle(r) for r in data.get("data", []) if len(r) >= 6]
    rows.sort(key=lambda x: x.ts)
    now = int(time.time() * 1000)
    interval = 4 * 60 * 60 * 1000 if granularity == "4H" else 15 * 60 * 1000
    return [c for c in rows if c.ts + interval <= now]


def bullish(c): return c.c > c.o
def bearish(c): return c.c < c.o
def body_ratio(c): return abs(c.c - c.o) / max(c.h - c.l, 1e-12)


def tr(candles, i):
    if i == 0:
        return candles[i].h - candles[i].l
    c, p = candles[i], candles[i - 1]
    return max(c.h - c.l, abs(c.h - p.c), abs(c.l - p.c))


def atr_at(candles, i, period=ATR_PERIOD):
    if i < period:
        return None
    vals = [tr(candles, j) for j in range(max(1, i - period + 1), i + 1)]
    return sum(vals) / len(vals) if vals else None


def latest_atr(candles, period=ATR_PERIOD):
    return atr_at(candles, len(candles) - 1, period)


def latest_sweep(candles, direction):
    if len(candles) < LIQUIDITY_LOOKBACK + 2:
        return None
    end = len(candles) - 1
    start = max(LIQUIDITY_LOOKBACK, end - MAX_SWEEP_BARS + 1)
    for i in range(end, start - 1, -1):
        prev = candles[i - LIQUIDITY_LOOKBACK:i]
        cur = candles[i]
        if direction == "LONG":
            level = min(x.l for x in prev)
            if cur.l < level and cur.c > level:
                return {"index": i, "ts": cur.ts, "level": level}
        else:
            level = max(x.h for x in prev)
            if cur.h > level and cur.c < level:
                return {"index": i, "ts": cur.ts, "level": level}
    return None


def cisd_after_sweep(candles, direction, sweep):
    a = sweep["index"] + 1
    b = min(len(candles) - 1, sweep["index"] + MAX_SWEEP_TO_CISD)
    for i in range(a, b + 1):
        cur, ref = candles[i], candles[i - 1]
        if direction == "LONG" and bullish(cur) and cur.c > ref.h and body_ratio(cur) >= MIN_CISD_BODY:
            return {"index": i, "ts": cur.ts, "open": cur.o, "close": cur.c, "body": body_ratio(cur)}
        if direction == "SHORT" and bearish(cur) and cur.c < ref.l and body_ratio(cur) >= MIN_CISD_BODY:
            return {"index": i, "ts": cur.ts, "open": cur.o, "close": cur.c, "body": body_ratio(cur)}
    return None


def key_levels(candles, direction):
    if len(candles) < 30:
        return []
    end = len(candles) - 1
    start = max(2, end - KEY_LOOKBACK + 1)
    levels = []
    for i in range(start, end + 1):
        a = atr_at(candles, i, KEY_ATR_PERIOD)
        cur = candles[i]
        if not a or body_ratio(cur) < MIN_DEPARTURE_BODY:
            continue
        prev = candles[i - 1]
        if direction == "LONG" and bullish(cur):
            low, high = prev.l, max(prev.o, prev.c)
            typ, displacement = ("Demand" if bearish(prev) else "Bullish OB"), cur.c - cur.o
        elif direction == "SHORT" and bearish(cur):
            low, high = min(prev.o, prev.c), prev.h
            typ, displacement = ("Supply" if bullish(prev) else "Bearish OB"), cur.o - cur.c
        else:
            continue
        if high - low < a * 0.10:
            high = low + a * 0.10
        strength = 6.0 if body_ratio(cur) < 0.55 else 8.0 if body_ratio(cur) < 0.70 else 10.0
        strength += 8.0 if displacement >= a * 0.75 else 6.0 if displacement >= a * 0.50 else 4.0 if displacement >= a * 0.30 else 0.0
        levels.append({"type": typ, "low": low, "high": high, "ts": prev.ts, "strength": min(strength, 18.0)})
    levels.sort(key=lambda x: (x["strength"], x["ts"]), reverse=True)
    return levels[:12]


def nearest_key_level(c4, c15, direction):
    a4 = latest_atr(c4, KEY_ATR_PERIOD)
    if not a4:
        return None
    price = c15[-1].c
    best = None
    for x in key_levels(c4, direction):
        if price < x["low"]:
            dist = x["low"] - price
        elif price > x["high"]:
            dist = price - x["high"]
        else:
            dist = 0.0
        d = dist / a4
        if d > KEY_MAX_DISTANCE_ATR:
            continue
        score = min(x["strength"] * 15.0 / 18.0, 15.0)
        score += 10.0 if dist == 0 else 9.0 if d <= 0.15 else 7.0 if d <= 0.30 else 5.0 if d <= 0.50 else 3.0
        cand = {**x, "distance_atr": d, "score": min(score, 25.0)}
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


def fvg_zone(candles, i, direction):
    if i < 2:
        return None
    a = atr_at(candles, i)
    if not a:
        return None
    x, z = candles[i - 2], candles[i]
    if direction == "LONG":
        if x.h >= z.l:
            return None
        low, high = x.h, z.l
    else:
        if x.l <= z.h:
            return None
        low, high = z.h, x.l
    if high - low < a * FVG_MIN_ATR:
        return None
    return {"low": low, "high": high, "index": i, "ts": z.ts}


def ifvg_retest(candles, direction):
    end = len(candles) - 1
    start = max(2, end - PATTERN_LOOKBACK + 1)
    opposite = "SHORT" if direction == "LONG" else "LONG"

    for i in range(end - 2, start - 1, -1):
        zone = fvg_zone(candles, i, opposite)
        if not zone or end - i > IFVG_MAX_AGE:
            continue

        converted = None
        for j in range(i + 1, end + 1):
            if direction == "LONG" and candles[j].c > zone["high"]:
                converted = j
                break
            if direction == "SHORT" and candles[j].c < zone["low"]:
                converted = j
                break
        if converted is None or converted >= end:
            continue

        for j in range(max(converted + 1, end - 5), end + 1):
            c = candles[j]
            if c.h < zone["low"] or c.l > zone["high"]:
                continue
            if direction == "LONG" and c.c >= zone["high"]:
                return {"zone": zone, "converted": converted, "retest": j}
            if direction == "SHORT" and c.c <= zone["low"]:
                return {"zone": zone, "converted": converted, "retest": j}
    return None


def compression(candles):
    end = len(candles) - 1
    stop = end
    start = stop - COMPRESSION_LOOKBACK
    if start < 0:
        return None
    recent = candles[start:stop]
    a = latest_atr(candles)
    if not a:
        return None
    ratio = (max(x.h for x in recent) - min(x.l for x in recent)) / a
    return {"ratio": ratio, "ok": ratio <= COMPRESSION_MAX_ATR}


def pre_move(candles, direction, n):
    end = len(candles) - 1
    if end < n:
        return None
    old, now = candles[end - n].c, candles[end].c
    if old <= 0:
        return None
    return ((now - old) / old) if direction == "LONG" else ((old - now) / old)


def box_state(candles, direction):
    end = len(candles) - 1
    if end < BOX_LOOKBACK + 1:
        return None
    prev = candles[end - BOX_LOOKBACK:end]
    high, low = max(x.h for x in prev), min(x.l for x in prev)
    a = latest_atr(candles)
    if not a or high <= low:
        return None
    price = candles[end].c
    distance = high - price if direction == "LONG" else price - low
    if distance < 0:
        return {"broken": True, "distance_atr": abs(distance) / a}
    return {"broken": False, "distance_atr": distance / a, "high": high, "low": low}


def rvol(candles):
    end = len(candles) - 1
    if end < RVOL_PERIOD + 1:
        return None
    avg = sum(candles[i].v for i in range(end - RVOL_PERIOD, end)) / RVOL_PERIOD
    return candles[end].v / avg if avg > 0 else None


def score(key, comp, box, fast, slow, volume):
    s = key["score"]
    s += 30.0  # IFVG conversion + retest/HOLD
    r = comp["ratio"]
    s += 20.0 if r <= 1.0 else 17.0 if r <= 1.5 else 13.0
    d = box["distance_atr"]
    s += 15.0 if d <= 0.10 else 12.0 if d <= 0.20 else 8.0
    if fast is not None and slow is not None:
        s += 10.0 if fast <= 0.012 and slow <= 0.020 else 7.0 if fast <= 0.022 and slow <= 0.035 else 4.0
    if volume is not None:
        s += 5.0 if volume >= 2.0 else 4.0 if volume >= 1.5 else 3.0 if volume >= 1.2 else 2.0 if volume >= 1.0 else 0.0
    return round(s, 1)


def evaluate(symbol):
    try:
        c4 = get_candles(symbol, "4H", H4_LIMIT)
        c15 = get_candles(symbol, "15m", M15_LIMIT)
        if len(c4) < 60 or len(c15) < 100:
            return []

        out = []
        for direction in ("LONG", "SHORT"):
            sweep = latest_sweep(c4, direction)
            if not sweep:
                continue
            cisd = cisd_after_sweep(c4, direction, sweep)
            if not cisd:
                continue
            recency = len(c4) - 1 - cisd["index"]
            if recency > MAX_CISD_RECENCY:
                continue

            key = nearest_key_level(c4, c15, direction)
            box = box_state(c15, direction)
            pattern = ifvg_retest(c15, direction)
            comp = compression(c15)
            fast, slow = pre_move(c15, direction, PREMOVE_FAST), pre_move(c15, direction, PREMOVE_SLOW)
            vol = rvol(c15)

            if not key or not box or box["broken"] or box["distance_atr"] > MAX_BOX_DISTANCE_ATR:
                continue
            if not pattern or not comp or not comp["ok"]:
                continue
            if fast is None or slow is None or fast > PREMOVE_MAX or slow > PREMOVE_MAX:
                continue

            sc = score(key, comp, box, fast, slow, vol)
            if sc < MIN_PRE_SCORE:
                continue

            cur = c15[-1]
            out.append({
                "symbol": symbol, "direction": direction, "score": sc,
                "signal_ts": cur.ts, "price": cur.c,
                "key_level_type": key["type"], "key_level_low": key["low"],
                "key_level_high": key["high"], "key_level_score": key["score"],
                "key_level_distance_atr": round(key["distance_atr"], 3),
                "ifvg_low": pattern["zone"]["low"], "ifvg_high": pattern["zone"]["high"],
                "ifvg_age_bars": len(c15) - 1 - pattern["zone"]["index"],
                "compression_range_atr": round(comp["ratio"], 3),
                "box_distance_atr": round(box["distance_atr"], 3),
                "pre_move_fast": round(fast, 4), "pre_move_slow": round(slow, 4),
                "rvol": round(vol, 2) if vol is not None else None,
                "four_hour_recency": recency,
                "stage": "IFVG RETEST + COMPRESSION / PRE-BREAKOUT",
            })
        return out
    except Exception as e:
        print(f"{symbol}: {e}")
        return []


def fmt_price(x):
    return f"{x:,.2f}" if x >= 1000 else f"{x:,.4f}" if x >= 1 else f"{x:.8f}"


def kst(ts):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%H:%M KST")


def send_telegram(message):
    token, chat_id = os.getenv("TELEGRAM_SIGNAL_BOT_TOKEN"), os.getenv("TELEGRAM_SIGNAL_CHAT_ID")
    if not token or not chat_id:
        print(message)
        return
    req = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urlencode({"chat_id": chat_id, "text": message}).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(req, timeout=TIMEOUT):
        pass


def message(x):
    icon = "🟡" if x["direction"] == "LONG" else "🟠"
    rvol_text = f"{x['rvol']:.2f}x" if x["rvol"] is not None else "N/A"
    return (
        f"{icon} PRE-{x['direction']} WATCH\n\n{x['symbol']}\n\n"
        f"⭐ PRE SCORE {x['score']:.1f}/100\n\n"
        f"4H Sweep → CISD → Recency ✅\n4H Key Level ✅\n"
        f"15M IFVG Retest/HOLD ✅\n15M Compression ✅\n"
        f"15M Breakout ⏳ NOT YET\n\n"
        f"📍 KEY LEVEL\n{x['key_level_type']}\n"
        f"{fmt_price(x['key_level_low'])} ~ {fmt_price(x['key_level_high'])}\n"
        f"거리: {x['key_level_distance_atr']:.2f} ATR\n\n"
        f"📦 IFVG\n{fmt_price(x['ifvg_low'])} ~ {fmt_price(x['ifvg_high'])}\n"
        f"Age: {x['ifvg_age_bars']} bars\n\n"
        f"Compression: {x['compression_range_atr']:.2f} ATR\n"
        f"Box까지: {x['box_distance_atr']:.2f} ATR\n"
        f"PreMove: {x['pre_move_fast']*100:.1f}% / {x['pre_move_slow']*100:.1f}%\n"
        f"RVOL: {rvol_text}\n"
        f"현재가: {fmt_price(x['price'])}\n시각: {kst(x['signal_ts'])}\n\n"
        f"관찰용 V1 · 자동주문 없음"
    )


def main():
    symbols = get_symbols()
    print(f"Symbols: {len(symbols)}")
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(evaluate, s) for s in symbols]
        for f in as_completed(futures):
            results.extend(f.result())

    results.sort(key=lambda x: x["score"], reverse=True)
    results = results[:MAX_RESULTS]

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": datetime.now(timezone.utc).isoformat(),
             "mode": "OBSERVATION", "candidates": results},
            f, ensure_ascii=False, indent=2,
        )

    print(f"PRE candidates: {len(results)}")
    for x in results:
        try:
            send_telegram(message(x))
        except Exception as e:
            print(f"Telegram failed for {x['symbol']}: {e}")


if __name__ == "__main__":
    main()
