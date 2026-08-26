#!/usr/bin/env python3

"""
Bitget 4H -> 15M Ranked Signal Scanner

FLOW
----
4H Liquidity Sweep
        ↓
4H CISD
        ↓
4H Recency
        ↓
15M CURRENT CLOSED candle structure break
        ↓
Box / Fake-breakout filter
        ↓
Signal quality score
        ↓
Minimum score filter
        ↓
TOP 10
        ↓
Telegram

IMPORTANT
---------
- Existing v2.6 scanner is NOT modified.
- Only the latest CLOSED 15M candle can generate a signal.
- Historical 15M structure breaks are NOT replayed.
- Absolute volume is NOT used for ranking.
- Relative Volume (RVOL) is used instead.
- Maximum 10 signals are sent.
- Signals below minimum score are rejected.
- No trading orders are placed.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
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


# ============================================================
# 4H CONDITIONS
# ============================================================

MAX_4H_EVENT_BARS = 8
MAX_SWEEP_TO_CISD_BARS = 8
MAX_4H_RECENCY_BARS = 8

LIQUIDITY_LOOKBACK = 8

MIN_CISD_BODY_RATIO = 0.30


# ============================================================
# 15M STRUCTURE
# ============================================================

SWING_LEFT = 2
SWING_RIGHT = 2

MIN_STRUCTURE_DISTANCE = 3

MIN_STRUCTURE_BODY_RATIO = 0.25


# ============================================================
# BOX / FAKE BREAKOUT FILTER
# ============================================================

BOX_LOOKBACK = 32

MIN_BOX_BREAK_ATR = 0.10

MAX_BOX_RANGE_ATR = 5.5

ATR_PERIOD = 14


# ============================================================
# SIGNAL QUALITY SCORE
# ============================================================

MIN_SIGNAL_SCORE = 65
MAX_TELEGRAM_SIGNALS = 10

RVOL_PERIOD = 20


# ============================================================
# STATE
# ============================================================

STATE_FILE = "signal_state.json"


# ============================================================
# DATA
# ============================================================

class Candle:
    def __init__(
        self,
        ts: int,
        o: float,
        h: float,
        l: float,
        c: float,
        v: float,
    ):
        self.ts = ts
        self.o = o
        self.h = h
        self.l = l
        self.c = c
        self.v = v


# ============================================================
# HTTP
# ============================================================

def get_json(
    path: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:

    query = urlencode(params)
    url = f"{BASE_URL}{path}?{query}"

    last_err = None

    for attempt in range(REQUEST_RETRIES):

        try:

            req = Request(
                url,
                headers={
                    "User-Agent":
                        "bitget-4h15m-ranked-scanner/1.0",
                    "Accept":
                        "application/json",
                },
                method="GET",
            )

            with urlopen(
                req,
                timeout=REQUEST_TIMEOUT,
            ) as resp:

                data = json.loads(
                    resp.read().decode("utf-8")
                )

            if data.get("code") != "00000":

                raise RuntimeError(
                    "Bitget API error: "
                    f"{data.get('code')} "
                    f"{data.get('msg')}"
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

            time.sleep(
                0.7 * (attempt + 1)
            )

    raise RuntimeError(
        f"Request failed: "
        f"{path} {params} :: {last_err}"
    )


# ============================================================
# SYMBOLS
# ============================================================

def get_symbols() -> List[str]:

    data = get_json(
        "/api/v3/market/instruments",
        {
            "category": PRODUCT_TYPE
        },
    )

    symbols = []

    for item in data.get("data", []):

        symbol = str(
            item.get("symbol", "")
        ).strip()

        quote_coin = str(
            item.get("quoteCoin", "")
        ).upper().strip()

        symbol_type = str(
            item.get("symbolType", "")
        ).lower().strip()

        contract_type = str(
            item.get("type", "")
        ).lower().strip()

        status = str(
            item.get("status", "")
        ).lower().strip()

        is_rwa = str(
            item.get("isRwa", "YES")
        ).upper().strip()

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

def get_candles(
    symbol: str,
    granularity: str,
    limit: int,
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

    candles.sort(
        key=lambda x: x.ts
    )

    # --------------------------------------------------------
    # REMOVE CURRENTLY FORMING CANDLE
    # --------------------------------------------------------

    now_ms = int(
        time.time() * 1000
    )

    interval_ms = {
        "4H":
            4 * 60 * 60 * 1000,
        "15m":
            15 * 60 * 1000,
    }[granularity]

    return [
        candle
        for candle in candles
        if candle.ts + interval_ms <= now_ms
    ]


# ============================================================
# BASIC HELPERS
# ============================================================

def bullish(
    candle: Candle,
) -> bool:

    return candle.c > candle.o


def bearish(
    candle: Candle,
) -> bool:

    return candle.c < candle.o


def body_ratio(
    candle: Candle,
) -> float:

    candle_range = max(
        candle.h - candle.l,
        1e-12,
    )

    return abs(
        candle.c - candle.o
    ) / candle_range


def true_range(
    candles: List[Candle],
    index: int,
) -> float:

    if index <= 0:

        return (
            candles[index].h
            - candles[index].l
        )

    current = candles[index]
    previous = candles[index - 1]

    return max(
        current.h - current.l,
        abs(
            current.h - previous.c
        ),
        abs(
            current.l - previous.c
        ),
    )


def atr(
    candles: List[Candle],
    period: int = ATR_PERIOD,
) -> Optional[float]:

    if len(candles) < period + 1:
        return None

    end = len(candles) - 1

    start = max(
        1,
        end - period + 1,
    )

    values = [
        true_range(
            candles,
            i,
        )
        for i in range(
            start,
            end + 1,
        )
    ]

    if not values:
        return None

    return sum(values) / len(values)


# ============================================================
# 4H LIQUIDITY SWEEP
# ============================================================

def find_latest_sweep(
    candles: List[Candle],
    direction: str,
) -> Optional[Dict[str, Any]]:

    if len(candles) < (
        LIQUIDITY_LOOKBACK + 2
    ):
        return None

    end = len(candles) - 1

    start = max(
        LIQUIDITY_LOOKBACK,
        end - MAX_4H_EVENT_BARS + 1,
    )

    for i in range(
        end,
        start - 1,
        -1,
    ):

        current = candles[i]

        previous = candles[
            i - LIQUIDITY_LOOKBACK:i
        ]

        if direction == "LONG":

            prior_low = min(
                candle.l
                for candle in previous
            )

            if (
                current.l < prior_low
                and current.c > prior_low
            ):

                return {
                    "index": i,
                    "ts": current.ts,
                    "level": prior_low,
                    "open": current.o,
                }

        else:

            prior_high = max(
                candle.h
                for candle in previous
            )

            if (
                current.h > prior_high
                and current.c < prior_high
            ):

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

    end = min(
        len(candles) - 1,
        sweep_index + MAX_SWEEP_TO_CISD_BARS,
    )

    if start > end:
        return None

    for i in range(
        start,
        end + 1,
    ):

        current = candles[i]
        reference = candles[i - 1]

        if direction == "LONG":

            if not bullish(current):
                continue

            if current.c <= reference.h:
                continue

            if (
                body_ratio(current)
                < MIN_CISD_BODY_RATIO
            ):
                continue

            return {
                "index": i,
                "ts": current.ts,
                "level": reference.h,
                "open": current.o,
                "close": current.c,
                "body_ratio":
                    body_ratio(current),
            }

        else:

            if not bearish(current):
                continue

            if current.c >= reference.l:
                continue

            if (
                body_ratio(current)
                < MIN_CISD_BODY_RATIO
            ):
                continue

            return {
                "index": i,
                "ts": current.ts,
                "level": reference.l,
                "open": current.o,
                "close": current.c,
                "body_ratio":
                    body_ratio(current),
            }

    return None


# ============================================================
# 15M SWINGS
# ============================================================

def is_swing_high(
    candles: List[Candle],
    idx: int,
) -> bool:

    if (
        idx < SWING_LEFT
        or idx + SWING_RIGHT
        >= len(candles)
    ):
        return False

    current = candles[idx]

    left = [
        candles[j].h
        for j in range(
            idx - SWING_LEFT,
            idx,
        )
    ]

    right = [
        candles[j].h
        for j in range(
            idx + 1,
            idx + SWING_RIGHT + 1,
        )
    ]

    return (
        current.h >= max(left)
        and current.h >= max(right)
    )


def is_swing_low(
    candles: List[Candle],
    idx: int,
) -> bool:

    if (
        idx < SWING_LEFT
        or idx + SWING_RIGHT
        >= len(candles)
    ):
        return False

    current = candles[idx]

    left = [
        candles[j].l
        for j in range(
            idx - SWING_LEFT,
            idx,
        )
    ]

    right = [
        candles[j].l
        for j in range(
            idx + 1,
            idx + SWING_RIGHT + 1,
        )
    ]

    return (
        current.l <= min(left)
        and current.l <= min(right)
    )


# ============================================================
# CURRENT CLOSED CANDLE STRUCTURE BREAK
# ============================================================

def check_current_structure_break(
    candles: List[Candle],
    direction: str,
    start_index: int,
) -> Optional[Dict[str, Any]]:

    if len(candles) < (
        SWING_LEFT
        + SWING_RIGHT
        + 5
    ):
        return None

    current_index = len(candles) - 1

    current = candles[
        current_index
    ]

    if current_index <= start_index:
        return None

    latest_possible_swing = (
        current_index
        - SWING_RIGHT
        - 1
    )

    latest_possible_swing = min(
        latest_possible_swing,
        current_index
        - MIN_STRUCTURE_DISTANCE,
    )

    if latest_possible_swing < start_index:
        return None

    if direction == "LONG":

        if not bullish(current):
            return None

        if (
            body_ratio(current)
            < MIN_STRUCTURE_BODY_RATIO
        ):
            return None

        for swing_index in range(
            latest_possible_swing,
            start_index - 1,
            -1,
        ):

            if not is_swing_high(
                candles,
                swing_index,
            ):
                continue

            level = candles[
                swing_index
            ].h

            if current.c <= level:
                continue

            return {
                "index": current_index,
                "ts": current.ts,
                "level": level,
                "swing_ts":
                    candles[
                        swing_index
                    ].ts,
            }

    else:

        if not bearish(current):
            return None

        if (
            body_ratio(current)
            < MIN_STRUCTURE_BODY_RATIO
        ):
            return None

        for swing_index in range(
            latest_possible_swing,
            start_index - 1,
            -1,
        ):

            if not is_swing_low(
                candles,
                swing_index,
            ):
                continue

            level = candles[
                swing_index
            ].l

            if current.c >= level:
                continue

            return {
                "index": current_index,
                "ts": current.ts,
                "level": level,
                "swing_ts":
                    candles[
                        swing_index
                    ].ts,
            }

    return None


# ============================================================
# BOX FILTER
# ============================================================

def check_box_breakout(
    candles: List[Candle],
    direction: str,
) -> Optional[Dict[str, Any]]:

    current_index = len(candles) - 1

    if current_index < BOX_LOOKBACK + 5:
        return None

    current = candles[
        current_index
    ]

    previous_start = (
        current_index
        - BOX_LOOKBACK
    )

    previous = candles[
        previous_start:current_index
    ]

    if len(previous) < BOX_LOOKBACK:
        return None

    current_atr = atr(
        candles,
        ATR_PERIOD,
    )

    if current_atr is None:
        return None

    box_high = max(
        candle.h
        for candle in previous
    )

    box_low = min(
        candle.l
        for candle in previous
    )

    box_range = (
        box_high - box_low
    )

    if box_range <= 0:
        return None

    range_atr = (
        box_range / current_atr
    )

    if direction == "LONG":

        break_distance = (
            current.c - box_high
        )

        if break_distance <= 0:
            return None

        if (
            break_distance
            < current_atr
            * MIN_BOX_BREAK_ATR
        ):
            return None

        return {
            "box_high": box_high,
            "box_low": box_low,
            "box_range": box_range,
            "range_atr": range_atr,
            "break_distance":
                break_distance,
            "atr": current_atr,
        }

    else:

        break_distance = (
            box_low - current.c
        )

        if break_distance <= 0:
            return None

        if (
            break_distance
            < current_atr
            * MIN_BOX_BREAK_ATR
        ):
            return None

        return {
            "box_high": box_high,
            "box_low": box_low,
            "box_range": box_range,
            "range_atr": range_atr,
            "break_distance":
                break_distance,
            "atr": current_atr,
        }


# ============================================================
# RVOL
# ============================================================

def relative_volume(
    candles: List[Candle],
) -> Optional[float]:

    current_index = len(candles) - 1

    if current_index < RVOL_PERIOD + 1:
        return None

    current_volume = candles[
        current_index
    ].v

    start = (
        current_index
        - RVOL_PERIOD
    )

    previous_volumes = [
        candles[i].v
        for i in range(
            start,
            current_index,
        )
    ]

    if not previous_volumes:
        return None

    average_volume = (
        sum(previous_volumes)
        / len(previous_volumes)
    )

    if average_volume <= 0:
        return None

    return (
        current_volume
        / average_volume
    )


# ============================================================
# SCORE HELPERS
# ============================================================

def score_breakout_distance(
    break_distance: float,
    current_atr: float,
) -> float:

    if current_atr <= 0:
        return 0.0

    ratio = (
        break_distance
        / current_atr
    )

    if ratio >= 0.75:
        return 20.0

    if ratio >= 0.50:
        return 16.0

    if ratio >= 0.30:
        return 12.0

    if ratio >= 0.15:
        return 8.0

    return 4.0


def score_candle_quality(
    candle: Candle,
) -> float:

    ratio = body_ratio(candle)

    if candle.h <= candle.l:
        return 0.0

    if candle.c >= candle.o:

        close_position = (
            candle.c - candle.l
        ) / (
            candle.h - candle.l
        )

    else:

        close_position = (
            candle.h - candle.c
        ) / (
            candle.h - candle.l
        )

    score = 0.0

    if ratio >= 0.70:
        score += 10
    elif ratio >= 0.55:
        score += 8
    elif ratio >= 0.40:
        score += 6
    elif ratio >= 0.30:
        score += 4
    else:
        score += 2

    if close_position >= 0.80:
        score += 5
    elif close_position >= 0.65:
        score += 4
    elif close_position >= 0.50:
        score += 2

    return min(
        score,
        15.0,
    )


def score_rvol(
    rvol: Optional[float],
) -> float:

    if rvol is None:
        return 0.0

    if rvol >= 2.0:
        return 10.0

    if rvol >= 1.5:
        return 8.0

    if rvol >= 1.2:
        return 6.0

    if rvol >= 1.0:
        return 4.0

    if rvol >= 0.8:
        return 2.0

    return 0.0


def score_4h_quality(
    cisd: Dict[str, Any],
    recency: int,
) -> float:

    score = 0.0

    cisd_body = cisd.get(
        "body_ratio",
        0.0,
    )

    if cisd_body >= 0.70:
        score += 10
    elif cisd_body >= 0.55:
        score += 8
    elif cisd_body >= 0.40:
        score += 6
    elif cisd_body >= 0.30:
        score += 4

    if recency <= 2:
        score += 5
    elif recency <= 4:
        score += 4
    elif recency <= 6:
        score += 2

    return min(
        score,
        15.0,
    )


# ============================================================
# SIGNAL QUALITY SCORE
# ============================================================

def calculate_signal_score(
    c15: List[Candle],
    cisd: Dict[str, Any],
    recency: int,
    box: Dict[str, Any],
) -> Dict[str, Any]:

    current = c15[
        -1
    ]

    breakout_score = score_breakout_distance(
        box["break_distance"],
        box["atr"],
    )

    candle_score = score_candle_quality(
        current
    )

    rvol = relative_volume(
        c15
    )

    rvol_score = score_rvol(
        rvol
    )

    structure_score = 15.0

    four_hour_score = score_4h_quality(
        cisd,
        recency,
    )

    total = (
        breakout_score
        + candle_score
        + rvol_score
        + structure_score
        + four_hour_score
    )

    return {
        "score":
            round(total, 1),

        "breakout_score":
            round(breakout_score, 1),

        "candle_score":
            round(candle_score, 1),

        "rvol_score":
            round(rvol_score, 1),

        "structure_score":
            round(structure_score, 1),

        "four_hour_score":
            round(four_hour_score, 1),

        "rvol":
            round(rvol, 2)
            if rvol is not None
            else None,

        "atr":
            box["atr"],

        "break_distance":
            box["break_distance"],
    }


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_symbol(
    symbol: str,
) -> List[Dict[str, Any]]:

    signals: List[
        Dict[str, Any]
    ] = []

    try:

        c4 = get_candles(
            symbol,
            "4H",
            HISTORY_LIMIT_4H,
        )

    except Exception:

        return signals

    if len(c4) < 40:
        return signals

    for direction in (
        "LONG",
        "SHORT",
    ):

        sweep = find_latest_sweep(
            c4,
            direction,
        )

        if not sweep:
            continue

        cisd = find_cisd_after_sweep(
            c4,
            direction,
            sweep,
        )

        if not cisd:
            continue

        recency = (
            len(c4)
            - 1
            - cisd["index"]
        )

        if (
            recency
            > MAX_4H_RECENCY_BARS
        ):
            continue

        try:

            c15 = get_candles(
                symbol,
                "15m",
                HISTORY_LIMIT_15M,
            )

        except Exception:

            continue

        if len(c15) < 100:
            continue

        start15 = next(
            (
                i
                for i, candle
                in enumerate(c15)
                if candle.ts > cisd["ts"]
            ),
            len(c15),
        )

        if start15 >= len(c15):
            continue

        structure = (
            check_current_structure_break(
                c15,
                direction,
                start15,
            )
        )

        if not structure:
            continue

        box = check_box_breakout(
            c15,
            direction,
        )

        if not box:
            continue

        signal_candle = c15[
            -1
        ]

        interval_ms = (
            15
            * 60
            * 1000
        )

        now_ms = int(
            time.time() * 1000
        )

        if (
            signal_candle.ts
            + interval_ms
            > now_ms
        ):
            continue

        score = calculate_signal_score(
            c15,
            cisd,
            recency,
            box,
        )

        if (
            score["score"]
            < MIN_SIGNAL_SCORE
        ):
            continue

        signals.append(
            {
                "symbol":
                    symbol,

                "direction":
                    direction,

                "cisd_open":
                    cisd["open"],

                "cisd_close":
                    cisd["close"],

                "cisd_body_ratio":
                    cisd["body_ratio"],

                "signal_open":
                    signal_candle.o,

                "signal_close":
                    signal_candle.c,

                "signal_ts":
                    signal_candle.ts,

                "structure_level":
                    structure["level"],

                "swing_ts":
                    structure["swing_ts"],

                "box_high":
                    box["box_high"],

                "box_low":
                    box["box_low"],

                "box_range":
                    box["box_range"],

                "break_distance":
                    box["break_distance"],

                "atr":
                    box["atr"],

                "score":
                    score["score"],

                "breakout_score":
                    score[
                        "breakout_score"
                    ],

                "candle_score":
                    score[
                        "candle_score"
                    ],

                "rvol_score":
                    score[
                        "rvol_score"
                    ],

                "structure_score":
                    score[
                        "structure_score"
                    ],

                "four_hour_score":
                    score[
                        "four_hour_score"
                    ],

                "rvol":
                    score["rvol"],
            }
        )

    return signals


# ============================================================
# TELEGRAM
# ============================================================

def fmt_price(
    value: float,
) -> str:

    if value >= 1000:
        return f"{value:,.2f}"

    if value >= 1:
        return f"{value:,.4f}"

    return f"{value:.8f}"


# ------------------------------------------------------------
# 15M CANDLE TIME
#
# Bitget candle timestamp = candle OPEN time.
# Therefore:
# 23:00 timestamp = 23:00 ~ 23:15 candle
# Actual close time = 23:15
# ------------------------------------------------------------

def kst_time(
    ts: int,
) -> str:

    dt = datetime.fromtimestamp(
        ts / 1000,
        tz=timezone.utc,
    )

    dt = dt.astimezone(
        timezone(
            timedelta(hours=9)
        )
    )

    return dt.strftime(
        "%H:%M KST"
    )


def kst_15m_close_time(
    ts: int,
) -> str:

    close_ts = (
        ts
        + 15 * 60 * 1000
    )

    return kst_time(
        close_ts
    )


def make_message(
    signal: Dict[str, Any],
) -> str:

    icon = (
        "🟢"
        if signal["direction"]
        == "LONG"
        else "🔴"
    )

    rvol_text = (
        f"{signal['rvol']:.2f}x"
        if signal["rvol"]
        is not None
        else "N/A"
    )

    signal_open_time = kst_time(
        signal["signal_ts"]
    )

    signal_close_time = kst_15m_close_time(
        signal["signal_ts"]
    )

    return (
        f"{icon} "
        f"{signal['direction']} SIGNAL\n\n"

        f"{signal['symbol']}\n\n"

        f"⭐ SCORE "
        f"{signal['score']:.1f}/100\n\n"

        f"4H Sweep → CISD → Recency ✅\n"
        f"15M Structure Break ✅\n"
        f"15M Closed Candle ✅\n"
        f"Box Breakout ✅\n\n"

        f"CISD 시가: "
        f"{fmt_price(signal['cisd_open'])}\n"

        f"신호봉 종가: "
        f"{fmt_price(signal['signal_close'])}\n\n"

        f"돌파강도: "
        f"{signal['breakout_score']:.1f}/20\n"

        f"캔들품질: "
        f"{signal['candle_score']:.1f}/15\n"

        f"RVOL: "
        f"{rvol_text} "
        f"({signal['rvol_score']:.1f}/10)\n"

        f"4H 품질: "
        f"{signal['four_hour_score']:.1f}/15\n\n"

        f"신호봉: "
        f"{signal_open_time} ~ "
        f"{signal_close_time}\n"

        f"신호봉 마감: "
        f"{signal_close_time}"
    )


def send_telegram(
    message: str,
) -> None:

    token = os.getenv(
        "TELEGRAM_SIGNAL_BOT_TOKEN",
        "",
    ).strip()

    chat_id = os.getenv(
        "TELEGRAM_SIGNAL_CHAT_ID",
        "",
    ).strip()

    if not token or not chat_id:

        print(
            "[INFO] "
            "Telegram secrets are not set."
        )

        return

    url = (
        "https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    payload = urlencode(
        {
            "chat_id":
                chat_id,

            "text":
                message,

            "disable_web_page_preview":
                "true",
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
        timeout=REQUEST_TIMEOUT,
    ) as resp:

        resp.read()


# ============================================================
# STATE
# ============================================================

def load_state() -> Dict[str, Any]:

    if not os.path.exists(
        STATE_FILE
    ):

        return {
            "signals": []
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        if (
            isinstance(data, dict)
            and isinstance(
                data.get("signals"),
                list,
            )
        ):

            return data

    except Exception:

        pass

    return {
        "signals": []
    }


def save_state(
    state: Dict[str, Any],
) -> None:

    state["signals"] = (
        state.get(
            "signals",
            []
        )[-500:]
    )

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )


def signal_key(
    signal: Dict[str, Any],
) -> str:

    return (
        f"{signal['symbol']}:"
        f"{signal['direction']}:"
        f"{signal['signal_ts']}"
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    print(
        "\n============================================"
    )

    print(
        " Bitget 4H -> 15M Ranked Signal Scanner"
    )

    print(
        "============================================"
    )

    print(
        "4H SWEEP -> CISD -> RECENCY"
    )

    print(
        "15M CURRENT CLOSED STRUCTURE BREAK"
    )

    print(
        "BOX FILTER + QUALITY SCORE"
    )

    print(
        f"MIN SCORE: {MIN_SIGNAL_SCORE}"
    )

    print(
        f"MAX SIGNALS: {MAX_TELEGRAM_SIGNALS}"
    )

    print(
        "LONG + SHORT"
    )

    print(
        "NO ORDERS"
    )

    try:

        symbols = get_symbols()

    except Exception as exc:

        print(
            "[FATAL] "
            f"symbol loading failed: {exc}"
        )

        return

    print(
        "[INFO] selected symbols: "
        f"{len(symbols)}"
    )

    all_signals: List[
        Dict[str, Any]
    ] = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                symbol,
            ): symbol
            for symbol in symbols
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
                    "[ERROR] "
                    f"{futures[future]}: "
                    f"{exc}"
                )

            if done % 50 == 0:

                print(
                    "[INFO] progress "
                    f"{done}/{len(symbols)}"
                )

    unique = {}

    for signal in all_signals:

        unique[
            signal_key(signal)
        ] = signal

    all_signals = list(
        unique.values()
    )

    all_signals.sort(
        key=lambda x: (
            x["score"],
            x["signal_ts"],
        ),
        reverse=True,
    )

    print(
        "[INFO] signals after "
        "box + score filter: "
        f"{len(all_signals)}"
    )

    ranked_signals = (
        all_signals[
            :MAX_TELEGRAM_SIGNALS
        ]
    )

    print(
        "[INFO] ranked TOP signals: "
        f"{len(ranked_signals)}"
    )

    state = load_state()

    sent_keys = set(
        state.get(
            "signals",
            [],
        )
    )

    new_signals = [
        signal
        for signal in ranked_signals
        if signal_key(signal)
        not in sent_keys
    ]

    print(
        "[INFO] new Telegram signals: "
        f"{len(new_signals)}"
    )

    for rank, signal in enumerate(
        new_signals,
        start=1,
    ):

        print(
            f"\n========== TOP {rank} =========="
        )

        message = make_message(
            signal
        )

        print(
            message
        )

        try:

            send_telegram(
                message
            )

            state.setdefault(
                "signals",
                []
            ).append(
                signal_key(signal)
            )

            save_state(
                state
            )

            print(
                "[INFO] Telegram sent: "
                f"{signal_key(signal)}"
            )

        except Exception as exc:

            print(
                "[WARN] "
                f"Telegram failed: {exc}"
            )

    save_state(
        state
    )

    print(
        "\n[INFO] scan finished."
    )


if __name__ == "__main__":
    main()
