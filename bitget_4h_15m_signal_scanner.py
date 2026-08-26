#!/usr/bin/env python3

"""
Bitget 4H -> 15M Signal Scanner

PURPOSE
-------
Production-style signal scanner based on the validated v2.6 flow:

4H Liquidity Sweep
        ↓
4H CISD
        ↓
4H Recency
        ↓
15M Structure Break
        ↓
15M CLOSED candle confirmation
        ↓
Telegram signal

IMPORTANT
---------
- Existing v2.6 scanner is NOT modified by this file.
- Only CLOSED candles are used.
- No trading orders are placed.
- Telegram receives ONLY confirmed signals.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

HISTORY_LIMIT_4H = 200
HISTORY_LIMIT_15M = 200

MAX_WORKERS = 8
REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3

# 4H conditions: keep v2.6 values
MAX_4H_EVENT_BARS = 8
MAX_SWEEP_TO_CISD_BARS = 8
MAX_4H_RECENCY_BARS = 8
LIQUIDITY_LOOKBACK = 8
MIN_CISD_BODY_RATIO = 0.30

# 15M structure: keep v2.6 values
SWING_LEFT = 2
SWING_RIGHT = 2
MAX_15M_STRUCTURE_BARS = 96
MIN_STRUCTURE_DISTANCE = 3
MIN_STRUCTURE_BODY_RATIO = 0.25

# Signal de-duplication
STATE_FILE = "signal_state.json"


# ============================================================
# DATA
# ============================================================

class Candle:
    def __init__(self, ts: int, o: float, h: float, l: float, c: float, v: float):
        self.ts = ts
        self.o = o
        self.h = h
        self.l = l
        self.c = c
        self.v = v


# ============================================================
# HTTP
# ============================================================

def get_json(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    query = urlencode(params)
    url = f"{BASE_URL}{path}?{query}"
    last_err = None

    for attempt in range(REQUEST_RETRIES):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "bitget-4h15m-signal-scanner/1.0",
                    "Accept": "application/json",
                },
                method="GET",
            )

            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data.get("code") != "00000":
                raise RuntimeError(
                    f"Bitget API error: {data.get('code')} {data.get('msg')}"
                )

            return data

        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_err = exc
            time.sleep(0.7 * (attempt + 1))

    raise RuntimeError(f"Request failed: {path} {params} :: {last_err}")


# ============================================================
# SYMBOLS
# ============================================================

def get_symbols() -> List[str]:
    data = get_json(
        "/api/v3/market/instruments",
        {"category": PRODUCT_TYPE},
    )

    symbols = []

    for item in data.get("data", []):
        symbol = str(item.get("symbol", "")).strip()
        quote_coin = str(item.get("quoteCoin", "")).upper().strip()
        symbol_type = str(item.get("symbolType", "")).lower().strip()
        contract_type = str(item.get("type", "")).lower().strip()
        status = str(item.get("status", "")).lower().strip()
        is_rwa = str(item.get("isRwa", "YES")).upper().strip()

        if (
            symbol.endswith("USDT")
            and quote_coin == "USDT"
            and symbol_type == "crypto"
            and contract_type == "perpetual"
            and status == "online"
            and is_rwa == "NO"
        ):
            symbols.append(symbol)

    return sorted(set(symbols))


# ============================================================
# CANDLES
# ============================================================

def get_candles(symbol: str, granularity: str, limit: int) -> List[Candle]:
    data = get_json(
        "/api/v2/mix/market/history-candles",
        {
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "granularity": granularity,
            "limit": limit,
        },
    )

    candles = []

    for row in data.get("data", []):
        if len(row) < 6:
            continue

        candles.append(
            Candle(
                ts=int(row[0]),
                o=float(row[1]),
                h=float(row[2]),
                l=float(row[3]),
                c=float(row[4]),
                v=float(row[5]),
            )
        )

    candles.sort(key=lambda x: x.ts)

    # IMPORTANT:
    # Remove the currently forming candle.
    now_ms = int(time.time() * 1000)

    interval_ms = {
        "4H": 4 * 60 * 60 * 1000,
        "15m": 15 * 60 * 1000,
    }[granularity]

    return [
        c for c in candles
        if c.ts + interval_ms <= now_ms
    ]


# ============================================================
# HELPERS
# ============================================================

def bullish(c: Candle) -> bool:
    return c.c > c.o


def bearish(c: Candle) -> bool:
    return c.c < c.o


def body_ratio(c: Candle) -> float:
    candle_range = max(c.h - c.l, 1e-12)
    return abs(c.c - c.o) / candle_range


# ============================================================
# 4H LIQUIDITY SWEEP
# ============================================================

def find_latest_sweep(candles: List[Candle], direction: str) -> Optional[Dict[str, Any]]:
    if len(candles) < LIQUIDITY_LOOKBACK + 2:
        return None

    end = len(candles) - 1
    start = max(LIQUIDITY_LOOKBACK, end - MAX_4H_EVENT_BARS + 1)

    for i in range(end, start - 1, -1):
        current = candles[i]
        previous = candles[i - LIQUIDITY_LOOKBACK:i]

        if direction == "LONG":
            prior_low = min(c.l for c in previous)

            if current.l < prior_low and current.c > prior_low:
                return {
                    "index": i,
                    "ts": current.ts,
                    "level": prior_low,
                    "open": current.o,
                }

        else:
            prior_high = max(c.h for c in previous)

            if current.h > prior_high and current.c < prior_high:
                return {
                    "index": i,
                    "ts": current.ts,
                    "level": prior_high,
                    "open": current.o,
                }

    return None


# ============================================================
# 4H CISD
# ============================================================

def find_cisd_after_sweep(
    candles: List[Candle],
    direction: str,
    sweep: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

    sweep_index = sweep["index"]
    start = sweep_index + 1
    end = min(len(candles) - 1, sweep_index + MAX_SWEEP_TO_CISD_BARS)

    if start > end:
        return None

    for i in range(start, end + 1):
        cur = candles[i]
        ref = candles[i - 1]

        if direction == "LONG":
            if not bullish(cur):
                continue
            if cur.c <= ref.h:
                continue
            if body_ratio(cur) < MIN_CISD_BODY_RATIO:
                continue

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.h,
                "open": cur.o,
                "close": cur.c,
            }

        else:
            if not bearish(cur):
                continue
            if cur.c >= ref.l:
                continue
            if body_ratio(cur) < MIN_CISD_BODY_RATIO:
                continue

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.l,
                "open": cur.o,
                "close": cur.c,
            }

    return None


# ============================================================
# 15M SWINGS
# ============================================================

def is_swing_high(candles: List[Candle], idx: int) -> bool:
    if idx < SWING_LEFT or idx + SWING_RIGHT >= len(candles):
        return False

    current = candles[idx]

    left = [candles[j].h for j in range(idx - SWING_LEFT, idx)]
    right = [candles[j].h for j in range(idx + 1, idx + SWING_RIGHT + 1)]

    return current.h >= max(left) and current.h >= max(right)


def is_swing_low(candles: List[Candle], idx: int) -> bool:
    if idx < SWING_LEFT or idx + SWING_RIGHT >= len(candles):
        return False

    current = candles[idx]

    left = [candles[j].l for j in range(idx - SWING_LEFT, idx)]
    right = [candles[j].l for j in range(idx + 1, idx + SWING_RIGHT + 1)]

    return current.l <= min(left) and current.l <= min(right)


# ============================================================
# 15M STRUCTURE BREAK
# ============================================================

def find_structure_break(
    candles: List[Candle],
    direction: str,
    start_index: int,
) -> Optional[Dict[str, Any]]:

    search_end = min(
        len(candles) - 1,
        start_index + MAX_15M_STRUCTURE_BARS,
    )

    if start_index >= search_end:
        return None

    if direction == "LONG":
        for break_index in range(start_index, search_end + 1):
            cur = candles[break_index]

            if not bullish(cur):
                continue

            if body_ratio(cur) < MIN_STRUCTURE_BODY_RATIO:
                continue

            swing_end = break_index - SWING_RIGHT - 1

            if swing_end < start_index:
                continue

            for swing_index in range(swing_end, start_index - 1, -1):
                if not is_swing_high(candles, swing_index):
                    continue

                if break_index - swing_index < MIN_STRUCTURE_DISTANCE:
                    continue

                level = candles[swing_index].h

                if cur.c <= level:
                    continue

                return {
                    "index": break_index,
                    "ts": cur.ts,
                    "level": level,
                    "swing_ts": candles[swing_index].ts,
                }

    else:
        for break_index in range(start_index, search_end + 1):
            cur = candles[break_index]

            if not bearish(cur):
                continue

            if body_ratio(cur) < MIN_STRUCTURE_BODY_RATIO:
                continue

            swing_end = break_index - SWING_RIGHT - 1

            if swing_end < start_index:
                continue

            for swing_index in range(swing_end, start_index - 1, -1):
                if not is_swing_low(candles, swing_index):
                    continue

                if break_index - swing_index < MIN_STRUCTURE_DISTANCE:
                    continue

                level = candles[swing_index].l

                if cur.c >= level:
                    continue

                return {
                    "index": break_index,
                    "ts": cur.ts,
                    "level": level,
                    "swing_ts": candles[swing_index].ts,
                }

    return None


# ============================================================
# SIGNAL
# ============================================================

def analyze_symbol(symbol: str) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []

    try:
        c4 = get_candles(symbol, "4H", HISTORY_LIMIT_4H)
    except Exception:
        return signals

    if len(c4) < 40:
        return signals

    for direction in ("LONG", "SHORT"):
        sweep = find_latest_sweep(c4, direction)

        if not sweep:
            continue

        cisd = find_cisd_after_sweep(c4, direction, sweep)

        if not cisd:
            continue

        recency = len(c4) - 1 - cisd["index"]

        if recency > MAX_4H_RECENCY_BARS:
            continue

        try:
            c15 = get_candles(symbol, "15m", HISTORY_LIMIT_15M)
        except Exception:
            continue

        if len(c15) < 100:
            continue

        # Start only AFTER the 4H CISD candle.
        start15 = next(
            (i for i, candle in enumerate(c15) if candle.ts > cisd["ts"]),
            len(c15),
        )

        if start15 >= len(c15) - 10:
            continue

        structure = find_structure_break(c15, direction, start15)

        if not structure:
            continue

        signal_candle = c15[structure["index"]]

        # Defensive check: signal candle must be fully closed.
        interval_ms = 15 * 60 * 1000
        now_ms = int(time.time() * 1000)

        if signal_candle.ts + interval_ms > now_ms:
            continue

        signals.append(
            {
                "symbol": symbol,
                "direction": direction,
                "cisd_open": cisd["open"],
                "signal_open": signal_candle.o,
                "signal_close": signal_candle.c,
                "signal_ts": signal_candle.ts,
                "structure_level": structure["level"],
            }
        )

    return signals


# ============================================================
# TELEGRAM
# ============================================================

def fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:.8f}"


def kst_time(ts: int) -> str:
    from datetime import timedelta

    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    dt = dt.astimezone(timezone(timedelta(hours=9)))

    return dt.strftime("%H:%M KST")


def make_message(signal: Dict[str, Any]) -> str:
    icon = "🟢" if signal["direction"] == "LONG" else "🔴"

    return (
        f"{icon} {signal['direction']} SIGNAL\n\n"
        f"{signal['symbol']}\n\n"
        f"15M\n"
        f"CISD Break ✅\n\n"
        f"CISD 시가: {fmt_price(signal['cisd_open'])}\n"
        f"봉마감 종가: {fmt_price(signal['signal_close'])}\n\n"
        f"신호봉 마감\n"
        f"{kst_time(signal['signal_ts'])}"
    )


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_SIGNAL_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_SIGNAL_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("[INFO] New Telegram secrets are not set.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode()

    req = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        resp.read()


# ============================================================
# STATE / DUPLICATION
# ============================================================

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"signals": []}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and isinstance(data.get("signals"), list):
            return data

    except Exception:
        pass

    return {"signals": []}


def save_state(state: Dict[str, Any]) -> None:
    # Keep the state small.
    state["signals"] = state.get("signals", [])[-500:]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def signal_key(signal: Dict[str, Any]) -> str:
    return (
        f"{signal['symbol']}:"
        f"{signal['direction']}:"
        f"{signal['signal_ts']}"
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    print("\n============================================")
    print(" Bitget 4H -> 15M Signal Scanner")
    print("============================================")
    print("CLOSED 15M CANDLE ONLY")
    print("LONG + SHORT")
    print("NO ORDERS")

    try:
        symbols = get_symbols()
    except Exception as exc:
        print(f"[FATAL] symbol loading failed: {exc}")
        return

    print(f"[INFO] selected symbols: {len(symbols)}")

    all_signals: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(analyze_symbol, symbol): symbol
            for symbol in symbols
        }

        done = 0

        for future in as_completed(futures):
            done += 1

            try:
                all_signals.extend(future.result())
            except Exception as exc:
                print(f"[ERROR] {futures[future]}: {exc}")

            if done % 50 == 0:
                print(f"[INFO] progress {done}/{len(symbols)}")

    # De-duplicate inside the same run.
    unique = {}
    for signal in all_signals:
        unique[signal_key(signal)] = signal

    all_signals = list(unique.values())
    all_signals.sort(key=lambda x: (x["signal_ts"], x["symbol"], x["direction"]))

    state = load_state()
    sent_keys = set(state.get("signals", []))

    new_signals = [
        signal
        for signal in all_signals
        if signal_key(signal) not in sent_keys
    ]

    print(f"[INFO] confirmed signals: {len(all_signals)}")
    print(f"[INFO] new signals: {len(new_signals)}")

    for signal in new_signals:
        message = make_message(signal)

        print("\n" + message + "\n")

        try:
            send_telegram(message)
            state.setdefault("signals", []).append(signal_key(signal))
            save_state(state)
            print(f"[INFO] Telegram sent: {signal_key(signal)}")
        except Exception as exc:
            print(f"[WARN] Telegram failed: {exc}")

    # Save state even when there are no new signals.
    save_state(state)

    print("\n[INFO] scan finished.")


if __name__ == "__main__":
    main()
