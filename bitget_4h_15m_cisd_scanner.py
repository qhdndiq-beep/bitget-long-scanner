#!/usr/bin/env python3

"""
Bitget 4H -> 15M Structure Scanner v2.6

FLOW
----
4H Liquidity Sweep
        ↓
4H CISD
        ↓
4H Recency
        ↓
15M Swing Structure Break
        ↓
TIMING DIAGNOSTIC
        ↓
1H ~ 6H TIMING CANDIDATE
        ↓
POST-STRUCTURE PERFORMANCE DIAGNOSTIC

v2.6 PURPOSE
-------------
v2.5에서 확인한 CISD -> Structure timing을 더 세분화하고,
Structure 발생 이후의 고정 시간대 가격 변화와
6H 기준 MFE / MAE를 기록한다.

IMPORTANT
---------
4H / 15M Structure 조건은 v2.5와 동일하다.
조건 완화 없음.
기존 24H Structure SEARCH WINDOW 유지.

추가:
- 1-2H / 2-4H / 4-6H timing bucket
- Structure 이후 +1H / +2H / +4H / +6H / +12H / +24H
  방향 기준 수익률 진단
- 6H MFE / MAE
- JSON에 post-performance 저장
- Telegram 4096 character 자동 분할

No trading orders are placed.
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
# 4H EVENT
# ============================================================

MAX_4H_EVENT_BARS = 8
MAX_SWEEP_TO_CISD_BARS = 8
MAX_4H_RECENCY_BARS = 8


# ============================================================
# 4H LIQUIDITY
# ============================================================

LIQUIDITY_LOOKBACK = 8


# ============================================================
# 4H CISD
# ============================================================

MIN_CISD_BODY_RATIO = 0.30


# ============================================================
# 15M STRUCTURE
# ============================================================

SWING_LEFT = 2
SWING_RIGHT = 2

# v2.5 조건 그대로 유지
MAX_15M_STRUCTURE_BARS = 96

MIN_STRUCTURE_DISTANCE = 3

MIN_STRUCTURE_BODY_RATIO = 0.25


# ============================================================
# v2.6 TIMING FILTER
# ============================================================

STRUCTURE_INTERVAL_MINUTES = 15

STRUCTURE_WINDOW_HOURS = (
    MAX_15M_STRUCTURE_BARS
    * STRUCTURE_INTERVAL_MINUTES
    / 60
)

TIMING_MINUTES = 60
TIMING_MAX_MINUTES = 360


# ============================================================
# v2.6 POST-STRUCTURE WINDOWS
# ============================================================

POST_PERFORMANCE_HOURS = (
    1,
    2,
    4,
    6,
    12,
    24,
)

MFE_MAE_HOURS = 6


# ============================================================
# TIMING BUCKETS
# ============================================================

STRUCTURE_TIME_BUCKETS = [
    ("0-1H", 0, 60),
    ("1-2H", 60, 120),
    ("2-4H", 120, 240),
    ("4-6H", 240, 360),
    ("6-12H", 360, 720),
    ("12-24H", 720, 1440),
    ("24H+", 1440, None),
]


# ============================================================
# DATA
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
class StructureCandidate:
    symbol: str
    direction: str

    sweep_ts: int
    sweep_level: float

    cisd_ts: int
    cisd_level: float

    structure_ts: int
    structure_level: float

    swing_ts: int

    current_price: float

    cisd_to_structure_minutes: float
    cisd_to_structure_hours: float

    # v2.6 timing
    timing_candidate: bool
    timing_bucket: str

    # v2.6 post-structure performance
    post_1h_pct: Optional[float]
    post_2h_pct: Optional[float]
    post_4h_pct: Optional[float]
    post_6h_pct: Optional[float]
    post_12h_pct: Optional[float]
    post_24h_pct: Optional[float]

    # v2.6 6H excursion
    mfe_6h_pct: Optional[float]
    mae_6h_pct: Optional[float]


# ============================================================
# HTTP
# ============================================================

def get_json(
    path: str,
    params: Dict[str, Any]
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
                        "bitget-4h15m-structure-scanner/2.6",
                    "Accept":
                        "application/json",
                },
                method="GET",
            )

            with urlopen(
                req,
                timeout=REQUEST_TIMEOUT
            ) as resp:

                body = resp.read().decode("utf-8")
                data = json.loads(body)

            if data.get("code") != "00000":

                raise RuntimeError(
                    f"Bitget API error: "
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
        f"{path} "
        f"{params} :: {last_err}"
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

    total = 0
    excluded = 0

    for item in data.get("data", []):

        total += 1

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

        else:

            excluded += 1

    symbols = sorted(set(symbols))

    print(
        f"[INFO] instruments: {total} | "
        f"selected: {len(symbols)} | "
        f"excluded: {excluded}"
    )

    return symbols


# ============================================================
# CANDLES
# ============================================================

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

    now_ms = int(
        time.time() * 1000
    )

    interval_ms = {
        "4H":
            4 * 60 * 60 * 1000,

        "15m":
            15 * 60 * 1000,
    }[granularity]

    # 아직 진행 중인 봉 제거
    candles = [
        x
        for x in candles
        if x.ts + interval_ms <= now_ms
    ]

    return candles


# ============================================================
# HELPERS
# ============================================================

def bullish(c: Candle) -> bool:
    return c.c > c.o


def bearish(c: Candle) -> bool:
    return c.c < c.o


def body_ratio(c: Candle) -> float:

    candle_range = max(
        c.h - c.l,
        1e-12
    )

    return (
        abs(c.c - c.o)
        / candle_range
    )


def minutes_between(
    start_ts: int,
    end_ts: int
) -> float:

    return (
        end_ts - start_ts
    ) / 60000.0


def hours_between(
    start_ts: int,
    end_ts: int
) -> float:

    return (
        end_ts - start_ts
    ) / 3600000.0


def timing_bucket(
    minutes: float
) -> str:

    for name, low, high in STRUCTURE_TIME_BUCKETS:

        if high is None:

            if minutes >= low:
                return name

        else:

            if low <= minutes < high:
                return name

    return "UNKNOWN"


def timing_sub_bucket(
    minutes: float
) -> str:

    if 60 <= minutes < 120:
        return "1-2H"

    if 120 <= minutes < 240:
        return "2-4H"

    if 240 <= minutes <= 360:
        return "4-6H"

    return "OTHER"


def is_timing_candidate(
    minutes: float
) -> bool:

    return (
        TIMING_MINUTES
        <= minutes
        <= TIMING_MAX_MINUTES
    )


# ============================================================
# POST-STRUCTURE PERFORMANCE
# ============================================================

def find_candle_at_or_after(
    candles: List[Candle],
    target_ts: int
) -> Optional[Candle]:

    for candle in candles:

        if candle.ts >= target_ts:
            return candle

    return None


def calculate_post_performance(
    candles: List[Candle],
    structure_index: int,
    structure_ts: int,
    direction: str
) -> Dict[str, Optional[float]]:

    """
    Structure candle close를 기준으로 고정 시간 후 종가 수익률을 계산한다.

    LONG:
        상승 = +
        하락 = -

    SHORT:
        하락 = +
        상승 = -

    MFE / MAE:
        Structure 이후 최대 6H 구간의 고가/저가를 사용한다.
        Structure candle 자체의 high/low는 제외하고
        다음 15M candle부터 계산한다.
    """

    if (
        structure_index < 0
        or
        structure_index >= len(candles)
    ):

        return {
            "post_1h_pct": None,
            "post_2h_pct": None,
            "post_4h_pct": None,
            "post_6h_pct": None,
            "post_12h_pct": None,
            "post_24h_pct": None,
            "mfe_6h_pct": None,
            "mae_6h_pct": None,
        }

    entry_price = candles[
        structure_index
    ].c

    if entry_price <= 0:
        return {
            "post_1h_pct": None,
            "post_2h_pct": None,
            "post_4h_pct": None,
            "post_6h_pct": None,
            "post_12h_pct": None,
            "post_24h_pct": None,
            "mfe_6h_pct": None,
            "mae_6h_pct": None,
        }

    output: Dict[str, Optional[float]] = {}

    # --------------------------------------------------------
    # Fixed-time close performance
    # --------------------------------------------------------

    for hours in POST_PERFORMANCE_HOURS:

        target_ts = (
            structure_ts
            + hours * 60 * 60 * 1000
        )

        future_candle = (
            find_candle_at_or_after(
                candles,
                target_ts
            )
        )

        key = f"post_{hours}h_pct"

        if future_candle is None:

            output[key] = None
            continue

        raw_pct = (
            (
                future_candle.c
                - entry_price
            )
            / entry_price
            * 100
        )

        if direction == "SHORT":
            raw_pct = -raw_pct

        output[key] = raw_pct

    # --------------------------------------------------------
    # 6H MFE / MAE
    # --------------------------------------------------------

    mfe_end_ts = (
        structure_ts
        + MFE_MAE_HOURS
        * 60
        * 60
        * 1000
    )

    future_candles = [
        candle
        for candle in candles[
            structure_index + 1:
        ]
        if candle.ts <= mfe_end_ts
    ]

    if not future_candles:

        output["mfe_6h_pct"] = None
        output["mae_6h_pct"] = None

        return output

    if direction == "LONG":

        max_high = max(
            candle.h
            for candle in future_candles
        )

        min_low = min(
            candle.l
            for candle in future_candles
        )

        output["mfe_6h_pct"] = (
            (
                max_high
                - entry_price
            )
            / entry_price
            * 100
        )

        output["mae_6h_pct"] = (
            (
                min_low
                - entry_price
            )
            / entry_price
            * 100
        )

    else:

        min_low = min(
            candle.l
            for candle in future_candles
        )

        max_high = max(
            candle.h
            for candle in future_candles
        )

        # SHORT에서 가격 하락이 유리
        output["mfe_6h_pct"] = (
            (
                entry_price
                - min_low
            )
            / entry_price
            * 100
        )

        # SHORT에서 가격 상승이 불리
        output["mae_6h_pct"] = (
            (
                entry_price
                - max_high
            )
            / entry_price
            * 100
        )

    return output


# ============================================================
# 4H LIQUIDITY SWEEP
# ============================================================

def find_latest_sweep(
    candles: List[Candle],
    direction: str
) -> Optional[Dict[str, Any]]:

    if len(candles) < LIQUIDITY_LOOKBACK + 2:
        return None

    end = len(candles) - 1

    start = max(
        LIQUIDITY_LOOKBACK,
        end - MAX_4H_EVENT_BARS + 1
    )

    for i in range(
        end,
        start - 1,
        -1
    ):

        current = candles[i]

        previous = candles[
            i - LIQUIDITY_LOOKBACK:i
        ]

        if not previous:
            continue

        # LONG
        if direction == "LONG":

            prior_low = min(
                c.l for c in previous
            )

            if (
                current.l < prior_low
                and
                current.c > prior_low
            ):

                return {
                    "index": i,
                    "ts": current.ts,
                    "level": prior_low,
                }

        # SHORT
        else:

            prior_high = max(
                c.h for c in previous
            )

            if (
                current.h > prior_high
                and
                current.c < prior_high
            ):

                return {
                    "index": i,
                    "ts": current.ts,
                    "level": prior_high,
                }

    return None


# ============================================================
# 4H CISD
# ============================================================

def find_cisd_after_sweep(
    candles: List[Candle],
    direction: str,
    sweep: Dict[str, Any]
) -> Optional[Dict[str, Any]]:

    sweep_index = sweep["index"]

    start = sweep_index + 1

    end = min(
        len(candles) - 1,
        sweep_index
        + MAX_SWEEP_TO_CISD_BARS
    )

    if start > end:
        return None

    for i in range(
        start,
        end + 1
    ):

        cur = candles[i]
        ref = candles[i - 1]

        if direction == "LONG":

            if not bullish(cur):
                continue

            if cur.c <= ref.h:
                continue

            if (
                body_ratio(cur)
                <
                MIN_CISD_BODY_RATIO
            ):
                continue

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.h,
            }

        else:

            if not bearish(cur):
                continue

            if cur.c >= ref.l:
                continue

            if (
                body_ratio(cur)
                <
                MIN_CISD_BODY_RATIO
            ):
                continue

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.l,
            }

    return None


# ============================================================
# 15M SWINGS
# ============================================================

def is_swing_high(
    candles: List[Candle],
    idx: int
) -> bool:

    if (
        idx < SWING_LEFT
        or
        idx + SWING_RIGHT >= len(candles)
    ):
        return False

    current = candles[idx]

    left = [
        candles[j].h
        for j in range(
            idx - SWING_LEFT,
            idx
        )
    ]

    right = [
        candles[j].h
        for j in range(
            idx + 1,
            idx + SWING_RIGHT + 1
        )
    ]

    return (
        current.h >= max(left)
        and
        current.h >= max(right)
    )


def is_swing_low(
    candles: List[Candle],
    idx: int
) -> bool:

    if (
        idx < SWING_LEFT
        or
        idx + SWING_RIGHT >= len(candles)
    ):
        return False

    current = candles[idx]

    left = [
        candles[j].l
        for j in range(
            idx - SWING_LEFT,
            idx
        )
    ]

    right = [
        candles[j].l
        for j in range(
            idx + 1,
            idx + SWING_RIGHT + 1
        )
    ]

    return (
        current.l <= min(left)
        and
        current.l <= min(right)
    )


# ============================================================
# 15M STRUCTURE BREAK
# ============================================================

def find_structure_break(
    candles: List[Candle],
    direction: str,
    start_index: int
) -> Optional[Dict[str, Any]]:

    search_end = min(
        len(candles) - 1,
        start_index
        + MAX_15M_STRUCTURE_BARS
    )

    if start_index >= search_end:
        return None

    # ========================================================
    # LONG
    # ========================================================

    if direction == "LONG":

        for break_index in range(
            start_index,
            search_end + 1
        ):

            cur = candles[
                break_index
            ]

            if not bullish(cur):
                continue

            if (
                body_ratio(cur)
                <
                MIN_STRUCTURE_BODY_RATIO
            ):
                continue

            swing_end = (
                break_index
                - SWING_RIGHT
                - 1
            )

            if swing_end < start_index:
                continue

            for swing_index in range(
                swing_end,
                start_index - 1,
                -1
            ):

                if not is_swing_high(
                    candles,
                    swing_index
                ):
                    continue

                if (
                    break_index
                    -
                    swing_index
                    <
                    MIN_STRUCTURE_DISTANCE
                ):
                    continue

                level = candles[
                    swing_index
                ].h

                if cur.c <= level:
                    continue

                return {
                    "index":
                        break_index,

                    "ts":
                        cur.ts,

                    "level":
                        level,

                    "swing_ts":
                        candles[
                            swing_index
                        ].ts,
                }

    # ========================================================
    # SHORT
    # ========================================================

    else:

        for break_index in range(
            start_index,
            search_end + 1
        ):

            cur = candles[
                break_index
            ]

            if not bearish(cur):
                continue

            if (
                body_ratio(cur)
                <
                MIN_STRUCTURE_BODY_RATIO
            ):
                continue

            swing_end = (
                break_index
                - SWING_RIGHT
                - 1
            )

            if swing_end < start_index:
                continue

            for swing_index in range(
                swing_end,
                start_index - 1,
                -1
            ):

                if not is_swing_low(
                    candles,
                    swing_index
                ):
                    continue

                if (
                    break_index
                    -
                    swing_index
                    <
                    MIN_STRUCTURE_DISTANCE
                ):
                    continue

                level = candles[
                    swing_index
                ].l

                if cur.c >= level:
                    continue

                return {
                    "index":
                        break_index,

                    "ts":
                        cur.ts,

                    "level":
                        level,

                    "swing_ts":
                        candles[
                            swing_index
                        ].ts,
                }

    return None


# ============================================================
# CANDIDATE BUILDER
# ============================================================

def build_candidate(
    symbol: str,
    direction: str,
    sweep: Dict[str, Any],
    cisd: Dict[str, Any],
    structure: Dict[str, Any],
    c15: List[Candle]
) -> StructureCandidate:

    elapsed_minutes = (
        minutes_between(
            cisd["ts"],
            structure["ts"]
        )
    )

    elapsed_hours = (
        hours_between(
            cisd["ts"],
            structure["ts"]
        )
    )

    timing_candidate = (
        is_timing_candidate(
            elapsed_minutes
        )
    )

    timing_bucket_name = (
        timing_sub_bucket(
            elapsed_minutes
        )
    )

    post = calculate_post_performance(
        c15,
        structure["index"],
        structure["ts"],
        direction
    )

    return StructureCandidate(

        symbol=symbol,

        direction=direction,

        sweep_ts=sweep["ts"],

        sweep_level=sweep["level"],

        cisd_ts=cisd["ts"],

        cisd_level=cisd["level"],

        structure_ts=structure["ts"],

        structure_level=structure["level"],

        swing_ts=structure["swing_ts"],

        current_price=c15[-1].c,

        cisd_to_structure_minutes=
            elapsed_minutes,

        cisd_to_structure_hours=
            elapsed_hours,

        timing_candidate=
            timing_candidate,

        timing_bucket=
            timing_bucket_name,

        post_1h_pct=
            post["post_1h_pct"],

        post_2h_pct=
            post["post_2h_pct"],

        post_4h_pct=
            post["post_4h_pct"],

        post_6h_pct=
            post["post_6h_pct"],

        post_12h_pct=
            post["post_12h_pct"],

        post_24h_pct=
            post["post_24h_pct"],

        mfe_6h_pct=
            post["mfe_6h_pct"],

        mae_6h_pct=
            post["mae_6h_pct"],
    )


# ============================================================
# SYMBOL ANALYSIS
# ============================================================

def analyze_symbol(
    symbol: str
) -> Dict[str, Any]:

    result = {

        "symbol": symbol,

        "long_event": False,
        "short_event": False,

        "long_structure": False,
        "short_structure": False,

        "long_candidate": None,
        "short_candidate": None,

        "fifteen_min_status":
            "NOT_REQUESTED",

        "long_structure_status":
            "NOT_CHECKED",

        "short_structure_status":
            "NOT_CHECKED",

        "error_stage": None,
        "error": None,
    }

    # ========================================================
    # 4H DATA
    # ========================================================

    try:

        c4 = get_candles(
            symbol,
            "4H",
            HISTORY_LIMIT_4H
        )

    except Exception as exc:

        result["error_stage"] = "4H_CANDLES"
        result["error"] = str(exc)

        return result

    if len(c4) < 40:

        result["error_stage"] = "4H_DATA_LENGTH"

        result["error"] = (
            f"4H candles={len(c4)}"
        )

        return result

    # ========================================================
    # LONG 4H EVENT
    # ========================================================

    try:

        sweep_long = find_latest_sweep(
            c4,
            "LONG"
        )

        if sweep_long:

            cisd_long = find_cisd_after_sweep(
                c4,
                "LONG",
                sweep_long
            )

            if cisd_long:

                recency = (
                    len(c4)
                    - 1
                    -
                    cisd_long["index"]
                )

                if recency <= MAX_4H_RECENCY_BARS:

                    result[
                        "long_event"
                    ] = True

                    result[
                        "long_event_data"
                    ] = {
                        "sweep": sweep_long,
                        "cisd": cisd_long,
                        "recency_bars": recency,
                    }

    except Exception as exc:

        result["error_stage"] = (
            "4H_LONG_EVENT"
        )

        result["error"] = str(exc)

        return result

    # ========================================================
    # SHORT 4H EVENT
    # ========================================================

    try:

        sweep_short = find_latest_sweep(
            c4,
            "SHORT"
        )

        if sweep_short:

            cisd_short = find_cisd_after_sweep(
                c4,
                "SHORT",
                sweep_short
            )

            if cisd_short:

                recency = (
                    len(c4)
                    - 1
                    -
                    cisd_short["index"]
                )

                if recency <= MAX_4H_RECENCY_BARS:

                    result[
                        "short_event"
                    ] = True

                    result[
                        "short_event_data"
                    ] = {
                        "sweep": sweep_short,
                        "cisd": cisd_short,
                        "recency_bars": recency,
                    }

    except Exception as exc:

        result["error_stage"] = (
            "4H_SHORT_EVENT"
        )

        result["error"] = str(exc)

        return result

    # ========================================================
    # 4H EVENT가 없으면 15M API 호출 안 함
    # ========================================================

    if not (
        result["long_event"]
        or
        result["short_event"]
    ):

        return result

    # ========================================================
    # 15M DATA
    # ========================================================

    try:

        c15 = get_candles(
            symbol,
            "15m",
            HISTORY_LIMIT_15M
        )

        result[
            "fifteen_min_status"
        ] = "SUCCESS"

    except Exception as exc:

        result[
            "fifteen_min_status"
        ] = "ERROR"

        result["error_stage"] = (
            "15M_CANDLES"
        )

        result["error"] = str(exc)

        return result

    if len(c15) < 100:

        result[
            "fifteen_min_status"
        ] = "ERROR"

        result["error_stage"] = (
            "15M_DATA_LENGTH"
        )

        result["error"] = (
            f"15M candles={len(c15)}"
        )

        return result

    # ========================================================
    # LONG STRUCTURE
    # ========================================================

    if result["long_event"]:

        try:

            cisd_ts = result[
                "long_event_data"
            ]["cisd"]["ts"]

            start15 = next(
                (
                    i
                    for i, candle
                    in enumerate(c15)
                    if candle.ts > cisd_ts
                ),
                len(c15)
            )

            if (
                start15
                >=
                len(c15) - 10
            ):

                result[
                    "long_structure_status"
                ] = "INSUFFICIENT_TIME_WINDOW"

            else:

                structure = (
                    find_structure_break(
                        c15,
                        "LONG",
                        start15
                    )
                )

                if structure:

                    result[
                        "long_structure"
                    ] = True

                    result[
                        "long_structure_status"
                    ] = "FOUND"

                    sweep = result[
                        "long_event_data"
                    ]["sweep"]

                    cisd = result[
                        "long_event_data"
                    ]["cisd"]

                    result[
                        "long_candidate"
                    ] = build_candidate(
                        symbol,
                        "LONG",
                        sweep,
                        cisd,
                        structure,
                        c15
                    )

                else:

                    result[
                        "long_structure_status"
                    ] = "NOT_FOUND"

        except Exception as exc:

            result["error_stage"] = (
                "15M_LONG_STRUCTURE"
            )

            result["error"] = str(exc)

            return result

    # ========================================================
    # SHORT STRUCTURE
    # ========================================================

    if result["short_event"]:

        try:

            cisd_ts = result[
                "short_event_data"
            ]["cisd"]["ts"]

            start15 = next(
                (
                    i
                    for i, candle
                    in enumerate(c15)
                    if candle.ts > cisd_ts
                ),
                len(c15)
            )

            if (
                start15
                >=
                len(c15) - 10
            ):

                result[
                    "short_structure_status"
                ] = "INSUFFICIENT_TIME_WINDOW"

            else:

                structure = (
                    find_structure_break(
                        c15,
                        "SHORT",
                        start15
                    )
                )

                if structure:

                    result[
                        "short_structure"
                    ] = True

                    result[
                        "short_structure_status"
                    ] = "FOUND"

                    sweep = result[
                        "short_event_data"
                    ]["sweep"]

                    cisd = result[
                        "short_event_data"
                    ]["cisd"]

                    result[
                        "short_candidate"
                    ] = build_candidate(
                        symbol,
                        "SHORT",
                        sweep,
                        cisd,
                        structure,
                        c15
                    )

                else:

                    result[
                        "short_structure_status"
                    ] = "NOT_FOUND"

        except Exception as exc:

            result["error_stage"] = (
                "15M_SHORT_STRUCTURE"
            )

            result["error"] = str(exc)

            return result

    return result


# ============================================================
# FORMAT
# ============================================================

def fmt_price(
    value: float
) -> str:

    if value >= 1000:
        return f"{value:,.2f}"

    if value >= 1:
        return f"{value:,.4f}"

    return f"{value:.8f}"


def short_ts(
    ts: int
) -> str:

    return datetime.fromtimestamp(
        ts / 1000,
        tz=timezone.utc
    ).strftime(
        "%m-%d %H:%M UTC"
    )


def format_elapsed(
    minutes: float
) -> str:

    if minutes < 60:

        return (
            f"{minutes:.0f}분"
        )

    hours = minutes / 60

    return (
        f"{hours:.1f}시간"
    )


def format_pct(
    value: Optional[float]
) -> str:

    if value is None:
        return "N/A"

    return f"{value:+.2f}%"


def format_candidate(
    c: StructureCandidate,
    include_performance: bool = True
) -> str:

    icon = (
        "🟢"
        if c.direction == "LONG"
        else "🔴"
    )

    bucket = timing_bucket(
        c.cisd_to_structure_minutes
    )

    timing_mark = (
        " ⭐ TIMING 1-6H"
        if c.timing_candidate
        else ""
    )

    lines = [

        f"{icon} {c.symbol} "
        f"{c.direction}{timing_mark}",

        f"4H Sweep: "
        f"{fmt_price(c.sweep_level)} "
        f"({short_ts(c.sweep_ts)})",

        f"4H CISD: "
        f"{fmt_price(c.cisd_level)} "
        f"({short_ts(c.cisd_ts)})",

        f"15M Structure: "
        f"{fmt_price(c.structure_level)} "
        f"({short_ts(c.structure_ts)})",

        f"15M Swing: "
        f"{short_ts(c.swing_ts)}",

        f"CISD → Structure: "
        f"{format_elapsed(c.cisd_to_structure_minutes)} "
        f"[{bucket}]",
    ]

    if c.timing_candidate:

        lines.append(
            f"Timing Zone: "
            f"{c.timing_bucket}"
        )

    lines.append(
        f"현재가: "
        f"{fmt_price(c.current_price)}"
    )

    if include_performance:

        lines += [

            "",

            "📊 POST STRUCTURE",

            f"+1H  : "
            f"{format_pct(c.post_1h_pct)}",

            f"+2H  : "
            f"{format_pct(c.post_2h_pct)}",

            f"+4H  : "
            f"{format_pct(c.post_4h_pct)}",

            f"+6H  : "
            f"{format_pct(c.post_6h_pct)}",

            f"+12H : "
            f"{format_pct(c.post_12h_pct)}",

            f"+24H : "
            f"{format_pct(c.post_24h_pct)}",

            "",

            f"MFE 6H: "
            f"{format_pct(c.mfe_6h_pct)}",

            f"MAE 6H: "
            f"{format_pct(c.mae_6h_pct)}",
        ]

    return "\n".join(lines)


# ============================================================
# TIMING DISTRIBUTION
# ============================================================

def build_timing_distribution(
    candidates: List[StructureCandidate],
    direction: str
) -> List[str]:

    filtered = [
        c
        for c in candidates
        if c.direction == direction
    ]

    counts = {
        bucket: 0
        for bucket, _, _ in STRUCTURE_TIME_BUCKETS
    }

    for candidate in filtered:

        bucket = timing_bucket(
            candidate.cisd_to_structure_minutes
        )

        if bucket in counts:
            counts[bucket] += 1

    lines = []

    for bucket, _, _ in STRUCTURE_TIME_BUCKETS:

        lines.append(
            f"   └ {bucket:<6}: "
            f"{counts[bucket]}"
        )

    if filtered:

        values = [
            c.cisd_to_structure_minutes
            for c in filtered
        ]

        avg_minutes = (
            sum(values)
            /
            len(values)
        )

        min_minutes = min(values)
        max_minutes = max(values)

        lines.append(
            f"   └ 평균   : "
            f"{format_elapsed(avg_minutes)}"
        )

        lines.append(
            f"   └ 최소   : "
            f"{format_elapsed(min_minutes)}"
        )

        lines.append(
            f"   └ 최대   : "
            f"{format_elapsed(max_minutes)}"
        )

    return lines


# ============================================================
# POST PERFORMANCE DISTRIBUTION
# ============================================================

def avg_optional(
    values: List[Optional[float]]
) -> Optional[float]:

    valid = [
        x for x in values
        if x is not None
    ]

    if not valid:
        return None

    return (
        sum(valid)
        /
        len(valid)
    )


def build_performance_summary(
    candidates: List[StructureCandidate],
    direction: str
) -> List[str]:

    filtered = [
        c
        for c in candidates
        if c.direction == direction
        and c.timing_candidate
    ]

    lines = []

    if not filtered:

        lines.append(
            "   └ 데이터 없음"
        )

        return lines

    timing_groups = [
        "1-2H",
        "2-4H",
        "4-6H",
    ]

    for group in timing_groups:

        group_candidates = [
            c
            for c in filtered
            if c.timing_bucket == group
        ]

        if not group_candidates:

            lines.append(
                f"   └ {group:<4}: 0"
            )

            continue

        p1 = avg_optional([
            c.post_1h_pct
            for c in group_candidates
        ])

        p2 = avg_optional([
            c.post_2h_pct
            for c in group_candidates
        ])

        p4 = avg_optional([
            c.post_4h_pct
            for c in group_candidates
        ])

        p6 = avg_optional([
            c.post_6h_pct
            for c in group_candidates
        ])

        mfe = avg_optional([
            c.mfe_6h_pct
            for c in group_candidates
        ])

        mae = avg_optional([
            c.mae_6h_pct
            for c in group_candidates
        ])

        lines.append(
            f"   └ {group:<4}: "
            f"N={len(group_candidates)}"
        )

        lines.append(
            f"      +1H {format_pct(p1)} | "
            f"+2H {format_pct(p2)}"
        )

        lines.append(
            f"      +4H {format_pct(p4)} | "
            f"+6H {format_pct(p6)}"
        )

        lines.append(
            f"      MFE {format_pct(mfe)} | "
            f"MAE {format_pct(mae)}"
        )

    return lines


# ============================================================
# REPORT
# ============================================================

def build_report(
    diagnostics: List[Dict[str, Any]],
    symbol_count: int,
    errors: int
) -> str:

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    long_events = [
        x
        for x in diagnostics
        if x.get("long_event")
    ]

    short_events = [
        x
        for x in diagnostics
        if x.get("short_event")
    ]

    long_structure = [
        x["long_candidate"]
        for x in diagnostics
        if x.get("long_structure")
        and x.get("long_candidate")
    ]

    short_structure = [
        x["short_candidate"]
        for x in diagnostics
        if x.get("short_structure")
        and x.get("short_candidate")
    ]

    candidates = (
        long_structure
        +
        short_structure
    )

    # ========================================================
    # TIMING CANDIDATES
    # ========================================================

    timing_candidates = [
        c
        for c in candidates
        if c.timing_candidate
    ]

    timing_long = [
        c
        for c in timing_candidates
        if c.direction == "LONG"
    ]

    timing_short = [
        c
        for c in timing_candidates
        if c.direction == "SHORT"
    ]

    # ========================================================
    # 15M DATA STATUS
    # ========================================================

    fifteen_success = [
        x
        for x in diagnostics
        if x.get("fifteen_min_status")
        == "SUCCESS"
    ]

    fifteen_errors = [
        x
        for x in diagnostics
        if x.get("fifteen_min_status")
        == "ERROR"
    ]

    # ========================================================
    # STRUCTURE STATUS
    # ========================================================

    long_not_found = [
        x
        for x in diagnostics
        if x.get("long_structure_status")
        == "NOT_FOUND"
    ]

    short_not_found = [
        x
        for x in diagnostics
        if x.get("short_structure_status")
        == "NOT_FOUND"
    ]

    long_insufficient = [
        x
        for x in diagnostics
        if x.get("long_structure_status")
        == "INSUFFICIENT_TIME_WINDOW"
    ]

    short_insufficient = [
        x
        for x in diagnostics
        if x.get("short_structure_status")
        == "INSUFFICIENT_TIME_WINDOW"
    ]

    # ========================================================
    # TIMING RATE
    # ========================================================

    total_structure_count = len(candidates)

    timing_rate = (
        len(timing_candidates)
        /
        total_structure_count
        * 100
        if total_structure_count
        else 0
    )

    lines = [

        "🔎 4H→15M Structure Scanner v2.6",

        now,

        "",

        f"스캔 {symbol_count}개",

        "",

        "━━━━━━━━━━━━━━━━━━━━━━",

        "🔬 v2.6 TIMING + PERFORMANCE",

        "━━━━━━━━━━━━━━━━━━━━━━",

        "① 4H FINAL EVENT",

        f"   └ LONG  : "
        f"{len(long_events)}",

        f"   └ SHORT : "
        f"{len(short_events)}",

        "",

        "② 15M DATA",

        f"   └ SUCCESS : "
        f"{len(fifteen_success)}",

        f"   └ ERROR   : "
        f"{len(fifteen_errors)}",

        "",

        "③ 15M STRUCTURE",

        f"   └ LONG  : "
        f"{len(long_structure)}",

        f"   └ SHORT : "
        f"{len(short_structure)}",

        f"   └ TOTAL : "
        f"{len(candidates)}",

        "",

        "④ STRUCTURE SEARCH DIAGNOSTIC",

        f"   └ LONG NOT FOUND  : "
        f"{len(long_not_found)}",

        f"   └ SHORT NOT FOUND : "
        f"{len(short_not_found)}",

        f"   └ LONG WINDOW ERR : "
        f"{len(long_insufficient)}",

        f"   └ SHORT WINDOW ERR: "
        f"{len(short_insufficient)}",

        "",

        "⑤ CISD → STRUCTURE TIMING",

        f"   └ SEARCH WINDOW : "
        f"{STRUCTURE_WINDOW_HOURS:.0f}H",

        "",
        "   LONG",
    ]

    lines += build_timing_distribution(
        candidates,
        "LONG"
    )

    lines += [
        "",
        "   SHORT",
    ]

    lines += build_timing_distribution(
        candidates,
        "SHORT"
    )

    lines += [

        "",

        "⑥ ⭐ TIMING 1-6H",

        "   └ RANGE : 1H ~ 6H",

        f"   └ LONG  : "
        f"{len(timing_long)}",

        f"   └ SHORT : "
        f"{len(timing_short)}",

        f"   └ TOTAL : "
        f"{len(timing_candidates)}",

        f"   └ RATE  : "
        f"{timing_rate:.1f}%",

        "",

        "⑦ 📊 POST-STRUCTURE AVG",

        "   방향 기준 수익률",

        "",

        "   LONG",

    ]

    lines += build_performance_summary(
        candidates,
        "LONG"
    )

    lines += [
        "",
        "   SHORT",
    ]

    lines += build_performance_summary(
        candidates,
        "SHORT"
    )

    lines += [

        "",

        "⚠️ API/분석 오류 : "
        f"{errors}",

        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # ========================================================
    # TIMING CANDIDATES ONLY
    # ========================================================

    if timing_candidates:

        lines += [
            "",
            "⭐ TIMING CANDIDATES 1-6H",
            "",
        ]

        for candidate in sorted(
            timing_candidates,
            key=lambda x: (
                x.cisd_to_structure_minutes,
                x.symbol,
                x.direction
            )
        )[:20]:

            lines.append(
                format_candidate(
                    candidate,
                    include_performance=True
                )
            )

            lines.append(
                "────────────────"
            )

    else:

        lines += [
            "",
            "",
            "⭐ 1-6H TIMING CANDIDATE 없음",
        ]

    # ========================================================
    # ERROR DIAGNOSTIC
    # ========================================================

    error_items = [
        x
        for x in diagnostics
        if x.get("error")
    ]

    if error_items:

        lines += [
            "",
            "🔧 ERROR DIAGNOSTIC",
            ""
        ]

        for item in error_items[:10]:

            lines.append(
                f"❌ {item.get('symbol')}"
            )

            lines.append(
                f"   STEP : "
                f"{item.get('error_stage')}"
            )

            lines.append(
                f"   ERROR: "
                f"{item.get('error')}"
            )

            lines.append("")

        if len(error_items) > 10:

            lines.append(
                f"... 외 "
                f"{len(error_items) - 10}개"
            )

    return "\n".join(lines)


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_MAX_LENGTH = 4000


def split_telegram_text(
    text: str,
    max_length: int = TELEGRAM_MAX_LENGTH
) -> List[str]:

    if len(text) <= max_length:
        return [text]

    chunks = []

    current = ""

    for line in text.split("\n"):

        if len(line) > max_length:

            if current:
                chunks.append(
                    current.rstrip()
                )
                current = ""

            for i in range(
                0,
                len(line),
                max_length
            ):

                chunks.append(
                    line[i:i + max_length]
                )

            continue

        proposed = (
            current
            + line
            + "\n"
        )

        if len(proposed) > max_length:

            if current:
                chunks.append(
                    current.rstrip()
                )

            current = line + "\n"

        else:

            current = proposed

    if current:
        chunks.append(
            current.rstrip()
        )

    return chunks


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
            "[INFO] Telegram secrets "
            "are not set."
        )

        return

    chunks = split_telegram_text(text)

    print(
        f"[INFO] Telegram messages: "
        f"{len(chunks)}"
    )

    url = (
        "https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    for index, chunk in enumerate(
        chunks,
        start=1
    ):

        payload = urlencode(
            {
                "chat_id": chat_id,
                "text": chunk,
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
            timeout=REQUEST_TIMEOUT
        ) as resp:

            resp.read()

        if index < len(chunks):

            time.sleep(0.5)


# ============================================================
# JSON
# ============================================================

def save_json(
    diagnostics: List[Dict[str, Any]]
) -> None:

    json_results = []

    for item in diagnostics:

        for key in (
            "long_candidate",
            "short_candidate"
        ):

            candidate = item.get(key)

            if candidate:

                json_results.append(
                    asdict(candidate)
                )

    with open(
        "structure_scan_results.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            json_results,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"[INFO] JSON candidates saved: "
        f"{len(json_results)}"
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    started = time.time()

    print(
        "\n"
        "============================================\n"
        " Bitget 4H -> 15M Structure Scanner v2.6\n"
        "============================================\n"
    )

    print(
        "CRYPTO ONLY | "
        "USDT PERPETUAL ONLY | "
        "LONG + SHORT"
    )

    print(
        "4H Sweep -> CISD -> Recency -> "
        "15M Swing Structure"
    )

    print(
        "STRUCTURE SEARCH WINDOW: "
        f"{STRUCTURE_WINDOW_HOURS:.0f}H"
    )

    print(
        "TIMING EXPERIMENT: "
        "1-2H / 2-4H / 4-6H"
    )

    print(
        "POST PERFORMANCE: "
        "+1H / +2H / +4H / +6H / +12H / +24H"
    )

    print(
        "MFE / MAE WINDOW: "
        "6H"
    )

    print(
        "DEBUG MODE: ENABLED"
    )

    print(
        "No orders."
    )

    # ========================================================
    # SYMBOLS
    # ========================================================

    try:

        symbols = get_symbols()

    except Exception as exc:

        print(
            f"[FATAL] symbol loading failed: "
            f"{exc}",
            file=sys.stderr
        )

        return

    print(
        f"[INFO] selected symbols: "
        f"{len(symbols)}"
    )

    # ========================================================
    # SCAN
    # ========================================================

    diagnostics = []

    errors = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                symbol
            ): symbol
            for symbol in symbols
        }

        done = 0

        for future in as_completed(
            futures
        ):

            done += 1

            symbol = futures[
                future
            ]

            try:

                result = future.result()

                diagnostics.append(
                    result
                )

                if result.get("error"):

                    errors += 1

                    print(
                        f"[ERROR] "
                        f"{symbol} | "
                        f"{result.get('error_stage')} | "
                        f"{result.get('error')}",
                        file=sys.stderr
                    )

            except Exception as exc:

                errors += 1

                diagnostics.append({

                    "symbol":
                        symbol,

                    "error_stage":
                        "WORKER",

                    "error":
                        str(exc),

                    "long_event":
                        False,

                    "short_event":
                        False,

                    "long_structure":
                        False,

                    "short_structure":
                        False,

                    "long_candidate":
                        None,

                    "short_candidate":
                        None,

                    "fifteen_min_status":
                        "ERROR",

                    "long_structure_status":
                        "NOT_CHECKED",

                    "short_structure_status":
                        "NOT_CHECKED",
                })

                print(
                    f"[ERROR] WORKER | "
                    f"{symbol} | {exc}",
                    file=sys.stderr
                )

            if done % 50 == 0:

                print(
                    f"[INFO] progress "
                    f"{done}/{len(symbols)}"
                )

    # ========================================================
    # REPORT
    # ========================================================

    report = build_report(
        diagnostics,
        len(symbols),
        errors
    )

    print(
        "\n" + report
    )

    # ========================================================
    # TELEGRAM
    # ========================================================

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

    # ========================================================
    # JSON
    # ========================================================

    try:

        save_json(
            diagnostics
        )

    except Exception as exc:

        print(
            f"[WARN] JSON save failed: "
            f"{exc}",
            file=sys.stderr
        )

    # ========================================================
    # FINISH
    # ========================================================

    elapsed = (
        time.time()
        - started
    )

    print(
        f"\n[INFO] elapsed: "
        f"{elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
