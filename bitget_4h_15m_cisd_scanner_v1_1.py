#!/usr/bin/env python3
"""
Bitget 4H -> 15M CISD Scanner v1.2
- Scans Bitget CRYPTO USDT-M perpetual futures only.
- Excludes stocks, ETFs, commodities, metals and RWA products.
- 4H: custom CISD-style body-close structure detection.
- 15M: after the 4H CISD, find a 15M CISD body-close confirmation.
- FVG / OB are informational only in v1.2; they are NOT required for a signal.
- LONG and SHORT are both scanned.
- SHORT can also be treated as a LONG exit warning by the user.
- No trading orders are placed.

Important:
This is a heuristic scanner, not an exact reproduction of TradingView's LuxAlgo CISD.
It is intentionally designed to find "look at this chart" candidates, not auto-trade.

v1.2 change:
- Symbol universe is now filtered using Bitget's Instruments API.
- Only:
    symbolType == "crypto"
    type == "perpetual"
    quoteCoin == "USDT"
    status == "online"
    isRwa == "NO"
  are allowed.
- This prevents TradFi US-Stock / ETF / commodity / metal products
  from entering the scanner.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# Bitget API
# ============================================================

BASE_URL = "https://api.bitget.com"

PRODUCT_TYPE = "USDT-FUTURES"

# ---- Scan controls ----
HISTORY_LIMIT_4H = 200
HISTORY_LIMIT_15M = 200

MAX_WORKERS = 8
REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3

# A candidate must have a recent 4H CISD.
MAX_4H_CISD_BARS = 12  # up to ~48 hours

# 15M setup should occur after the 4H event,
# within this many 15M candles.
MAX_15M_SETUP_BARS = 96  # ~24 hours

# FVG/OB zone tolerance.
ZONE_TOLERANCE_PCT = 0.0015  # 0.15%


# ============================================================
# Data structures
# ============================================================

@dataclass
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class Zone:
    kind: str
    direction: str
    low: float
    high: float
    ts: int
    age_bars: int


@dataclass
class Signal:
    symbol: str
    direction: str
    score: int
    status: str
    price: float

    cisd_ts: int
    cisd_level: float
    cisd_body_close: bool
    cisd_sweep: bool

    fvg_4h: bool
    ob_4h: bool
    fvg_15m: bool
    ob_15m: bool
    pullback_15m: bool
    liquidity_sweep_15m: bool
    structure_break_15m: bool
    confirmation_body_close: bool

    entry_low: Optional[float]
    entry_high: Optional[float]

    notes: List[str]


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
                    "User-Agent": "bitget-4h15m-structure-scanner/1.2",
                    "Accept": "application/json",
                },
                method="GET",
            )

            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                data = json.loads(body)

            if data.get("code") != "00000":
                raise RuntimeError(
                    f"Bitget API error: "
                    f"{data.get('code')} {data.get('msg')}"
                )

            return data

        except (
            HTTPError,
            URLError,
            TimeoutError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:

            last_err = exc
            time.sleep(0.7 * (attempt + 1))

    raise RuntimeError(
        f"Request failed: {url} :: {last_err}"
    )


# ============================================================
# Bitget market data
# ============================================================

def get_symbols() -> List[str]:
    """
    Return ONLY crypto USDT perpetual futures.

    Bitget's current Instruments API exposes:
      - category
      - quoteCoin
      - isRwa
      - type
      - status
      - symbolType

    We deliberately require:

      category   = USDT-FUTURES
      quoteCoin  = USDT
      symbolType = crypto
      type       = perpetual
      status     = online
      isRwa      = NO

    This prevents TradFi stocks / ETFs / metals / commodities /
    RWA products from entering our scanner.
    """

    data = get_json(
        "/api/v3/market/instruments",
        {
            "category": PRODUCT_TYPE,
        },
    )

    symbols: List[str] = []

    total = 0
    excluded = 0

    for item in data.get("data", []):
        total += 1

        symbol = str(item.get("symbol", "")).strip()
        quote_coin = str(item.get("quoteCoin", "")).upper().strip()
        symbol_type = str(item.get("symbolType", "")).lower().strip()
        contract_type = str(item.get("type", "")).lower().strip()
        status = str(item.get("status", "")).lower().strip()
        is_rwa = str(item.get("isRwa", "YES")).upper().strip()

        # ----------------------------------------------------
        # ONLY CRYPTO PERPETUAL USDT FUTURES
        # ----------------------------------------------------
        if (
            symbol.endswith("USDT")
            and quote_coin == "USDT"
            and symbol_type == "crypto"
            and contract_type == "perpetual"
            and status == "online"
            and is_rwa == "NO"
        ):
            symbols.append(symbol)
        else:
            excluded += 1

    symbols = sorted(set(symbols))

    print(
        f"[INFO] Bitget instruments: {total} total | "
        f"{len(symbols)} crypto perpetuals selected | "
        f"{excluded} excluded"
    )

    return symbols


def get_candles(
    symbol: str,
    granularity: str,
    limit: int
) -> List[Candle]:

    data = get_json(
        "/api/v2/mix/market/history-candles",
        {
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "granularity": granularity,
            "limit": limit,
        },
    )

    candles: List[Candle] = []

    for row in data.get("data", []):

        # [timestamp, open, high, low, close, volume, turnover]
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

    # Never analyze an incomplete current candle.
    now_ms = int(time.time() * 1000)

    interval_ms = {
        "4H": 4 * 60 * 60 * 1000,
        "15m": 15 * 60 * 1000,
    }[granularity]

    candles = [
        x for x in candles
        if x.ts + interval_ms <= now_ms
    ]

    return candles


# ============================================================
# Helpers
# ============================================================

def overlaps(
    price_low: float,
    price_high: float,
    zone: Zone
) -> bool:

    tol = max(
        price_high - price_low,
        zone.high - zone.low
    ) * 0.10

    tol = max(
        tol,
        ((price_low + price_high) / 2)
        * ZONE_TOLERANCE_PCT
    )

    return (
        price_high >= zone.low - tol
        and price_low <= zone.high + tol
    )


def bullish(c: Candle) -> bool:
    return c.c > c.o


def bearish(c: Candle) -> bool:
    return c.c < c.o


def local_low(
    candles: List[Candle],
    idx: int,
    radius: int = 2
) -> bool:

    lo = max(0, idx - radius)
    hi = min(len(candles), idx + radius + 1)

    return candles[idx].l <= min(
        x.l for x in candles[lo:hi]
    )


def local_high(
    candles: List[Candle],
    idx: int,
    radius: int = 2
) -> bool:

    lo = max(0, idx - radius)
    hi = min(len(candles), idx + radius + 1)

    return candles[idx].h >= max(
        x.h for x in candles[lo:hi]
    )


# ============================================================
# CISD heuristic
# ============================================================

def find_latest_cisd(
    candles: List[Candle],
    direction: str,
    max_bars: int,
) -> Optional[Dict[str, Any]]:

    """
    Custom CISD-style definition based on the user's chart examples.

    LONG:
      - reference candle is bearish
      - bullish candle closes ABOVE reference candle HIGH
      - wick-only break is rejected

    SHORT:
      - reference candle is bullish
      - bearish candle closes BELOW reference candle LOW
    """

    if len(candles) < 10:
        return None

    end = len(candles) - 1
    start = max(
        3,
        end - max_bars + 1
    )

    for i in range(end, start - 1, -1):

        cur = candles[i]
        ref = candles[i - 1]

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------
        if direction == "LONG":

            if not (
                bullish(cur)
                and bearish(ref)
            ):
                continue

            # Body/CLOSE must beat the entire reference wick.
            if cur.c <= ref.h:
                continue

            had_local_low = any(
                local_low(candles, j, 2)
                for j in range(
                    max(2, i - 8),
                    max(2, i - 2)
                )
            )

            sweep = False

            for j in range(
                max(2, i - 8),
                max(2, i - 1)
            ):

                prev_window = candles[
                    max(0, j - 8):j
                ]

                if prev_window:

                    prev_low = min(
                        x.l for x in prev_window
                    )

                    if (
                        candles[j].l < prev_low
                        and candles[j].c > prev_low
                    ):
                        sweep = True
                        break

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.h,
                "sweep": sweep,
                "context_low": had_local_low,
                "reference_ts": ref.ts,
            }

        # ----------------------------------------------------
        # SHORT
        # ----------------------------------------------------
        else:

            if not (
                bearish(cur)
                and bullish(ref)
            ):
                continue

            if cur.c >= ref.l:
                continue

            had_local_high = any(
                local_high(candles, j, 2)
                for j in range(
                    max(2, i - 8),
                    max(2, i - 2)
                )
            )

            sweep = False

            for j in range(
                max(2, i - 8),
                max(2, i - 1)
            ):

                prev_window = candles[
                    max(0, j - 8):j
                ]

                if prev_window:

                    prev_high = max(
                        x.h for x in prev_window
                    )

                    if (
                        candles[j].h > prev_high
                        and candles[j].c < prev_high
                    ):
                        sweep = True
                        break

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.l,
                "sweep": sweep,
                "context_high": had_local_high,
                "reference_ts": ref.ts,
            }

    return None


# ============================================================
# FVG
# ============================================================

def find_fvgs(
    candles: List[Candle],
    direction: str,
    max_age: int = 60
) -> List[Zone]:

    zones: List[Zone] = []

    start = max(
        2,
        len(candles) - max_age
    )

    for i in range(
        start,
        len(candles)
    ):

        a, _, c = (
            candles[i - 2],
            candles[i - 1],
            candles[i]
        )

        # Bullish FVG
        if direction == "LONG":

            if a.h < c.l:

                zones.append(
                    Zone(
                        kind="FVG",
                        direction=direction,
                        low=a.h,
                        high=c.l,
                        ts=c.ts,
                        age_bars=(
                            len(candles) - 1 - i
                        ),
                    )
                )

        # Bearish FVG
        else:

            if a.l > c.h:

                zones.append(
                    Zone(
                        kind="FVG",
                        direction=direction,
                        low=c.h,
                        high=a.l,
                        ts=c.ts,
                        age_bars=(
                            len(candles) - 1 - i
                        ),
                    )
                )

    return zones


# ============================================================
# OB
# ============================================================

def find_obs(
    candles: List[Candle],
    direction: str,
    max_age: int = 80
) -> List[Zone]:

    """
    Practical first-pass OB.

    LONG:
      last bearish candle before bullish displacement.

    SHORT:
      last bullish candle before bearish displacement.
    """

    zones: List[Zone] = []

    start = max(
        2,
        len(candles) - max_age
    )

    for i in range(
        start,
        len(candles) - 1
    ):

        cur = candles[i]
        nxt = candles[i + 1]

        # LONG OB
        if (
            direction == "LONG"
            and bearish(cur)
        ):

            if (
                bullish(nxt)
                and nxt.c > cur.h
            ):

                zones.append(
                    Zone(
                        kind="OB",
                        direction=direction,
                        low=cur.l,
                        high=cur.o,
                        ts=cur.ts,
                        age_bars=(
                            len(candles) - 1 - i
                        ),
                    )
                )

        # SHORT OB
        elif (
            direction == "SHORT"
            and bullish(cur)
        ):

            if (
                bearish(nxt)
                and nxt.c < cur.l
            ):

                zones.append(
                    Zone(
                        kind="OB",
                        direction=direction,
                        low=cur.o,
                        high=cur.h,
                        ts=cur.ts,
                        age_bars=(
                            len(candles) - 1 - i
                        ),
                    )
                )

    return zones


# ============================================================
# 15M CISD confirmation
# ============================================================

def find_latest_15m_cisd(
    candles: List[Candle],
    direction: str,
    start_index: int,
    max_bars: int = MAX_15M_SETUP_BARS,
) -> Optional[Dict[str, Any]]:

    """
    After the 4H CISD, find a 15M CISD
    in the SAME direction.

    LONG:
      reference = bearish
      current = bullish
      current CLOSE > reference HIGH

    SHORT:
      reference = bullish
      current = bearish
      current CLOSE < reference LOW

    Wick-only break is rejected.
    """

    start_index = max(
        1,
        start_index
    )

    end_index = min(
        len(candles) - 1,
        start_index + max_bars - 1
    )

    if start_index >= end_index:
        return None

    for i in range(
        end_index,
        start_index - 1,
        -1
    ):

        cur = candles[i]
        ref = candles[i - 1]

        # LONG
        if direction == "LONG":

            if (
                bullish(cur)
                and bearish(ref)
                and cur.c > ref.h
            ):

                return {
                    "index": i,
                    "ts": cur.ts,
                    "level": ref.h,
                    "reference_ts": ref.ts,
                    "body_close": True,
                    "sweep": False,
                }

        # SHORT
        else:

            if (
                bearish(cur)
                and bullish(ref)
                and cur.c < ref.l
            ):

                return {
                    "index": i,
                    "ts": cur.ts,
                    "level": ref.l,
                    "reference_ts": ref.ts,
                    "body_close": True,
                    "sweep": False,
                }

    return None


# ============================================================
# Symbol analysis
# ============================================================

def analyze_symbol(
    symbol: str
) -> List[Signal]:

    try:

        c4 = get_candles(
            symbol,
            "4H",
            HISTORY_LIMIT_4H
        )

        c15 = get_candles(
            symbol,
            "15m",
            HISTORY_LIMIT_15M
        )

        if (
            len(c4) < 30
            or len(c15) < 60
        ):
            return []

        signals: List[Signal] = []

        for direction in (
            "LONG",
            "SHORT"
        ):

            # ------------------------------------------------
            # STEP 1
            # 4H CISD
            # ------------------------------------------------

            cisd4 = find_latest_cisd(
                c4,
                direction,
                MAX_4H_CISD_BARS
            )

            if not cisd4:
                continue

            # ------------------------------------------------
            # STEP 2
            # Move to 15M
            # ------------------------------------------------

            start15 = next(
                (
                    i
                    for i, c in enumerate(c15)
                    if c.ts > cisd4["ts"]
                ),
                len(c15)
            )

            cisd15 = find_latest_15m_cisd(
                c15,
                direction,
                start15,
                MAX_15M_SETUP_BARS
            )

            # ------------------------------------------------
            # Informational FVG / OB
            # ------------------------------------------------

            c4_after = c4[
                cisd4["index"] :
            ]

            fvg4 = find_fvgs(
                c4_after,
                direction,
                max_age=40
            )

            ob4 = find_obs(
                c4_after,
                direction,
                max_age=50
            )

            fvg15 = False
            ob15 = False

            entry_low = None
            entry_high = None

            if cisd15:

                c15_after = c15[
                    cisd15["index"] :
                ]

                fvgs15 = find_fvgs(
                    c15_after,
                    direction,
                    max_age=40
                )

                obs15 = find_obs(
                    c15_after,
                    direction,
                    max_age=50
                )

                last = c15[-1]

                hits: List[Zone] = []

                for z in fvgs15:

                    if overlaps(
                        last.l,
                        last.h,
                        z
                    ):

                        fvg15 = True
                        hits.append(z)

                for z in obs15:

                    if overlaps(
                        last.l,
                        last.h,
                        z
                    ):

                        ob15 = True
                        hits.append(z)

                if hits:

                    z = sorted(
                        hits,
                        key=lambda z: abs(
                            last.c
                            - ((z.low + z.high) / 2)
                        )
                    )[0]

                    entry_low = z.low
                    entry_high = z.high

            # ------------------------------------------------
            # CORE SIGNAL
            # ------------------------------------------------

            if cisd15:

                status = "ENTRY_CANDIDATE"

                score = 80

                notes = [
                    "4H CISD body-close confirmed",
                    "15M CISD body-close confirmed",
                ]

                if cisd4["sweep"]:

                    score += 5
                    notes.append(
                        "4H liquidity sweep"
                    )

                if fvg4:

                    score += 3
                    notes.append(
                        "4H FVG exists"
                    )

                if ob4:

                    score += 3
                    notes.append(
                        "4H OB exists"
                    )

                if fvg15:

                    score += 4
                    notes.append(
                        "15M FVG near current price"
                    )

                if ob15:

                    score += 4
                    notes.append(
                        "15M OB near current price"
                    )

                signals.append(
                    Signal(
                        symbol=symbol,
                        direction=direction,
                        score=min(score, 100),
                        status=status,
                        price=c15[-1].c,

                        cisd_ts=cisd4["ts"],
                        cisd_level=cisd4["level"],
                        cisd_body_close=True,
                        cisd_sweep=cisd4["sweep"],

                        fvg_4h=bool(fvg4),
                        ob_4h=bool(ob4),

                        fvg_15m=fvg15,
                        ob_15m=ob15,

                        pullback_15m=True,
                        liquidity_sweep_15m=False,
                        structure_break_15m=True,
                        confirmation_body_close=True,

                        entry_low=entry_low,
                        entry_high=entry_high,

                        notes=notes,
                    )
                )

        return signals

    except Exception as exc:

        print(
            f"[WARN] {symbol}: {exc}",
            file=sys.stderr
        )

        return []


# ============================================================
# Formatting
# ============================================================

def fmt_price(
    x: Optional[float]
) -> str:

    if x is None:
        return "-"

    if x >= 1000:
        return f"{x:,.2f}"

    if x >= 1:
        return f"{x:,.4f}"

    return f"{x:.8f}"


def short_ts(ms: int) -> str:

    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).strftime(
        "%m-%d %H:%M UTC"
    )


def format_signal(
    s: Signal
) -> str:

    icon = (
        "🟢"
        if s.direction == "LONG"
        else "🔴"
    )

    title = (
        "LONG 후보"
        if s.direction == "LONG"
        else "SHORT 후보 / LONG 청산경고"
    )

    zone = "-"

    if (
        s.entry_low is not None
        and s.entry_high is not None
    ):

        zone = (
            f"{fmt_price(s.entry_low)}"
            f" ~ "
            f"{fmt_price(s.entry_high)}"
        )

    return (
        f"{icon} {s.symbol} | {title}\n"

        f"점수 {s.score} | {s.status}\n"

        f"현재가 {fmt_price(s.price)}\n"

        f"4H CISD "
        f"{short_ts(s.cisd_ts)} "
        f"| 레벨 {fmt_price(s.cisd_level)}\n"

        f"4H: body✓ "
        f"sweep "
        f"{'✓' if s.cisd_sweep else '-'} "
        f"FVG "
        f"{'✓' if s.fvg_4h else '-'} "
        f"OB "
        f"{'✓' if s.ob_4h else '-'}\n"

        f"15M: pullback "
        f"{'✓' if s.pullback_15m else '-'} "
        f"FVG "
        f"{'✓' if s.fvg_15m else '-'} "
        f"OB "
        f"{'✓' if s.ob_15m else '-'} "
        f"sweep "
        f"{'✓' if s.liquidity_sweep_15m else '-'}\n"

        f"15M structure "
        f"{'✓' if s.structure_break_15m else '-'} "
        f"body "
        f"{'✓' if s.confirmation_body_close else '-'}\n"

        f"zone: {zone}"
    )


# ============================================================
# Telegram
# ============================================================

def send_telegram(
    text: str
) -> None:

    token = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        ""
    ).strip()

    chat_id = os.getenv(
        "TELEGRAM_CHAT_ID",
        ""
    ).strip()

    if not token or not chat_id:

        print(
            "[INFO] Telegram secrets are not set; "
            "console-only mode."
        )

        return

    url = (
        "https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode()

    req = Request(
        url,
        data=payload,
        headers={
            "Content-Type":
            "application/x-www-form-urlencoded"
        },
        method="POST",
    )

    with urlopen(
        req,
        timeout=REQUEST_TIMEOUT
    ) as resp:

        resp.read()


# ============================================================
# Report
# ============================================================

def build_report(
    signals: List[Signal],
    symbol_count: int
) -> str:

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    signals = sorted(
        signals,
        key=lambda x: (
            x.status != "ENTRY_CANDIDATE",
            -x.score
        )
    )

    if not signals:

        return (
            f"🔎 4H→15M Structure Scanner\n"
            f"{now}\n\n"
            f"스캔 {symbol_count}개\n"
            f"유효 후보 없음\n\n"
            f"기준: 4H CISD + 15M CISD "
            f"모두 몸통 마감 필수"
        )

    blocks = [
        f"🔎 4H→15M Structure Scanner\n"
        f"{now}\n"
        f"스캔 {symbol_count}개\n"
        f"후보 {len(signals)}개"
    ]

    for s in signals[:12]:

        blocks.append(
            format_signal(s)
        )

    return "\n\n".join(blocks)


# ============================================================
# Main
# ============================================================

def main() -> None:

    started = time.time()

    print(
        "=== Bitget 4H -> 15M "
        "CISD Scanner v1.2 ==="
    )

    print(
        "CRYPTO ONLY | "
        "USDT PERPETUAL ONLY | "
        "LONG + SHORT | "
        "4H CISD -> 15M CISD | "
        "body-close only | "
        "no orders"
    )

    # --------------------------------------------------------
    # Get ONLY crypto perpetual futures
    # --------------------------------------------------------

    symbols = get_symbols()

    print(
        f"[INFO] selected crypto symbols: "
        f"{len(symbols)}"
    )

    # --------------------------------------------------------
    # Scan
    # --------------------------------------------------------

    all_signals: List[Signal] = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                s
            ): s
            for s in symbols
        }

        done = 0

        for future in as_completed(
            futures
        ):

            done += 1

            try:

                result = future.result()

                all_signals.extend(
                    result
                )

            except Exception as exc:

                print(
                    f"[WARN] worker error: "
                    f"{exc}",
                    file=sys.stderr
                )

            if done % 50 == 0:

                print(
                    f"[INFO] progress "
                    f"{done}/{len(symbols)}"
                )

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    all_signals.sort(
        key=lambda x: (
            -x.score,
            x.symbol,
            x.direction
        )
    )

    # --------------------------------------------------------
    # Build report
    # --------------------------------------------------------

    report = build_report(
        all_signals,
        len(symbols)
    )

    print(
        "\n" + report
    )

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    try:

        send_telegram(
            report
        )

    except Exception as exc:

        print(
            f"[WARN] Telegram failed: "
            f"{exc}",
            file=sys.stderr
        )

    # --------------------------------------------------------
    # Save JSON
    # --------------------------------------------------------

    with open(
        "structure_scan_results.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            [
                asdict(s)
                for s in all_signals
            ],
            f,
            ensure_ascii=False,
            indent=2,
        )

    elapsed = (
        time.time() - started
    )

    print(
        f"\n[INFO] elapsed: "
        f"{elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
