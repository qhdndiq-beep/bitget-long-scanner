#!/usr/bin/env python3

"""
Bitget 4H -> 15M Structure Scanner v1.4

CORE FLOW
---------
4H CISD
   ↓
15M Structure Break
   ↓
15M Pullback
   ↓
15M Confirmation
   ↓
Telegram Alert

v1.4
----
- 4H CISD 유지
- 15M Structure Break 유지
- Pullback 조건 완화
- Structure level / FVG / OB 중 하나에 실제 접근 또는 접촉
- Pullback 봉이 반드시 구조 레벨 아래/위에서 종가 확정될 필요 없음
- Pullback 이후 새로운 15M body-close confirmation 필요
- LONG / SHORT 동시 검색
- Crypto USDT perpetual만 검색
- 주문 기능 없음

IMPORTANT
---------
Heuristic scanner.
TradingView / LuxAlgo CISD의 정확한 복제본이 아님.
"""


from __future__ import annotations

import json
import os
import sys
import time

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://api.bitget.com"

PRODUCT_TYPE = "USDT-FUTURES"

HISTORY_LIMIT_4H = 200
HISTORY_LIMIT_15M = 300

MAX_WORKERS = 8

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3


# ============================================================
# 4H CISD
# ============================================================

MAX_4H_CISD_BARS = 8


# ============================================================
# 15M STRUCTURE
# ============================================================

MAX_15M_STRUCTURE_BARS = 96


# ============================================================
# PULLBACK
# ============================================================

MAX_PULLBACK_BARS = 32

# 최소 되돌림.
# v1.3의 0.05%는 유지하되,
# 구조 레벨/FVG/OB 접근 조건을 유연하게 처리한다.
MIN_PULLBACK_PCT = 0.0005

# 구조 레벨에서 허용하는 거리.
# 예: BTC라면 구조 레벨 근처를 조금 넓게 인정.
STRUCTURE_ZONE_TOLERANCE_PCT = 0.0015


# ============================================================
# CONFIRMATION
# ============================================================

MAX_CONFIRMATION_BARS = 16


# ============================================================
# ZONE
# ============================================================

ZONE_TOLERANCE_PCT = 0.0015


# ============================================================
# DATA STRUCTURES
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

    structure_ts: int
    structure_level: float

    pullback_ts: int
    confirmation_ts: int

    cisd_body_close: bool
    cisd_sweep: bool

    structure_break: bool
    pullback_confirmed: bool
    confirmation_body_close: bool

    pullback_structure_touch: bool
    pullback_fvg: bool
    pullback_ob: bool

    fvg_4h: bool
    ob_4h: bool

    fvg_15m: bool
    ob_15m: bool

    entry_low: Optional[float]
    entry_high: Optional[float]

    notes: List[str]


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
                        "bitget-4h15m-structure-scanner/1.4",
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
        f"{url} :: {last_err}"
    )


# ============================================================
# SYMBOL UNIVERSE
# ============================================================

def get_symbols() -> List[str]:

    data = get_json(
        "/api/v3/market/instruments",
        {
            "category": PRODUCT_TYPE
        },
    )

    symbols: List[str] = []

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
        f"crypto perpetual selected: {len(symbols)} | "
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

    candles: List[Candle] = []

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

    candles = [
        x
        for x in candles
        if x.ts + interval_ms <= now_ms
    ]

    return candles


# ============================================================
# BASIC HELPERS
# ============================================================

def bullish(c: Candle) -> bool:
    return c.c > c.o


def bearish(c: Candle) -> bool:
    return c.c < c.o


def body_size(c: Candle) -> float:
    return abs(c.c - c.o)


def candle_range(c: Candle) -> float:
    return max(
        c.h - c.l,
        1e-12
    )


def body_ratio(c: Candle) -> float:

    return (
        body_size(c)
        / candle_range(c)
    )


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
        (
            (price_low + price_high)
            / 2
        ) * ZONE_TOLERANCE_PCT
    )

    return (
        price_high >= zone.low - tol
        and
        price_low <= zone.high + tol
    )


def price_near_level(
    candle: Candle,
    level: float,
    tolerance_pct: float
) -> bool:

    tolerance = (
        level
        * tolerance_pct
    )

    return (
        candle.l <= level + tolerance
        and
        candle.h >= level - tolerance
    )


# ============================================================
# 4H CISD
# ============================================================

def find_latest_cisd(
    candles: List[Candle],
    direction: str,
    max_bars: int
) -> Optional[Dict[str, Any]]:

    if len(candles) < 20:
        return None

    end = len(candles) - 1

    start = max(
        3,
        end - max_bars + 1
    )

    for i in range(
        end,
        start - 1,
        -1
    ):

        cur = candles[i]

        ref = candles[i - 1]

        # ====================================================
        # LONG
        # ====================================================

        if direction == "LONG":

            if not (
                bullish(cur)
                and
                bearish(ref)
            ):
                continue

            # 반드시 종가가 reference high 돌파
            if cur.c <= ref.h:
                continue

            # 너무 약한 돌파 방지
            if body_ratio(cur) < 0.35:
                continue

            sweep = False

            for j in range(
                max(2, i - 8),
                max(2, i - 1)
            ):

                previous = candles[
                    max(0, j - 8):j
                ]

                if not previous:
                    continue

                prior_low = min(
                    x.l
                    for x in previous
                )

                if (
                    candles[j].l < prior_low
                    and
                    candles[j].c > prior_low
                ):

                    sweep = True
                    break

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.h,
                "sweep": sweep,
            }

        # ====================================================
        # SHORT
        # ====================================================

        else:

            if not (
                bearish(cur)
                and
                bullish(ref)
            ):
                continue

            if cur.c >= ref.l:
                continue

            if body_ratio(cur) < 0.35:
                continue

            sweep = False

            for j in range(
                max(2, i - 8),
                max(2, i - 1)
            ):

                previous = candles[
                    max(0, j - 8):j
                ]

                if not previous:
                    continue

                prior_high = max(
                    x.h
                    for x in previous
                )

                if (
                    candles[j].h > prior_high
                    and
                    candles[j].c < prior_high
                ):

                    sweep = True
                    break

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.l,
                "sweep": sweep,
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

        a = candles[i - 2]

        c = candles[i]

        # ====================================================
        # LONG FVG
        # ====================================================

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
                            len(candles)
                            - 1
                            - i
                        ),
                    )
                )

        # ====================================================
        # SHORT FVG
        # ====================================================

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
                            len(candles)
                            - 1
                            - i
                        ),
                    )
                )

    return zones


# ============================================================
# ORDER BLOCK
# ============================================================

def find_obs(
    candles: List[Candle],
    direction: str,
    max_age: int = 80
) -> List[Zone]:

    zones: List[Zone] = []

    start = max(
        1,
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
            and
            bearish(cur)
            and
            bullish(nxt)
            and
            nxt.c > cur.h
        ):

            zones.append(
                Zone(
                    kind="OB",
                    direction=direction,
                    low=cur.l,
                    high=cur.o,
                    ts=cur.ts,
                    age_bars=(
                        len(candles)
                        - 1
                        - i
                    ),
                )
            )

        # SHORT OB
        elif (
            direction == "SHORT"
            and
            bullish(cur)
            and
            bearish(nxt)
            and
            nxt.c < cur.l
        ):

            zones.append(
                Zone(
                    kind="OB",
                    direction=direction,
                    low=cur.o,
                    high=cur.h,
                    ts=cur.ts,
                    age_bars=(
                        len(candles)
                        - 1
                        - i
                    ),
                )
            )

    return zones


# ============================================================
# 15M STRUCTURE BREAK
# ============================================================

def find_15m_structure(
    candles: List[Candle],
    direction: str,
    start_index: int,
    max_bars: int
) -> Optional[Dict[str, Any]]:

    start_index = max(
        2,
        start_index
    )

    end_index = min(
        len(candles) - 1,
        start_index + max_bars
    )

    for i in range(
        start_index,
        end_index
    ):

        cur = candles[i]

        ref = candles[i - 1]

        # ====================================================
        # LONG STRUCTURE BREAK
        # ====================================================

        if direction == "LONG":

            if not (
                bullish(cur)
                and
                bearish(ref)
            ):
                continue

            if cur.c <= ref.h:
                continue

            if body_ratio(cur) < 0.30:
                continue

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.h,
            }

        # ====================================================
        # SHORT STRUCTURE BREAK
        # ====================================================

        else:

            if not (
                bearish(cur)
                and
                bullish(ref)
            ):
                continue

            if cur.c >= ref.l:
                continue

            if body_ratio(cur) < 0.30:
                continue

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.l,
            }

    return None


# ============================================================
# PULLBACK + CONFIRMATION v1.4
# ============================================================

def find_pullback_and_confirmation(
    candles: List[Candle],
    direction: str,
    structure: Dict[str, Any]
) -> Optional[Dict[str, Any]]:

    structure_index = structure["index"]

    structure_level = structure["level"]

    pull_start = structure_index + 1

    pull_end = min(
        len(candles) - 1,
        structure_index
        + MAX_PULLBACK_BARS
    )

    if pull_start >= pull_end:
        return None

    # --------------------------------------------------------
    # Build zones AFTER structure break.
    # --------------------------------------------------------

    zone_slice_end = min(
        len(candles),
        pull_end + 1
    )

    zone_candles = candles[
        structure_index:
        zone_slice_end
    ]

    fvgs = find_fvgs(
        zone_candles,
        direction,
        max_age=40
    )

    obs = find_obs(
        zone_candles,
        direction,
        max_age=40
    )

    pullback_index = None

    pullback_structure_touch = False

    pullback_fvg = False

    pullback_ob = False

    # --------------------------------------------------------
    # STEP A
    # Find actual pullback.
    # --------------------------------------------------------

    for i in range(
        pull_start,
        pull_end + 1
    ):

        c = candles[i]

        # ====================================================
        # LONG
        # ====================================================

        if direction == "LONG":

            # -----------------------------------------------
            # 1. Structure level touch / near touch
            # -----------------------------------------------

            structure_touch = price_near_level(
                c,
                structure_level,
                STRUCTURE_ZONE_TOLERANCE_PCT
            )

            # -----------------------------------------------
            # 2. FVG interaction
            # -----------------------------------------------

            fvg_touch = any(
                overlaps(
                    c.l,
                    c.h,
                    z
                )
                for z in fvgs
            )

            # -----------------------------------------------
            # 3. OB interaction
            # -----------------------------------------------

            ob_touch = any(
                overlaps(
                    c.l,
                    c.h,
                    z
                )
                for z in obs
            )

            # -----------------------------------------------
            # 4. Minimum retracement
            # -----------------------------------------------

            retrace = max(
                0.0,
                structure_level - c.l
            )

            minimum_retrace = (
                structure_level
                * MIN_PULLBACK_PCT
            )

            enough_retrace = (
                retrace
                >= minimum_retrace
            )

            # -----------------------------------------------
            # Pullback accepted if:
            #
            # structure touch
            # OR FVG
            # OR OB
            #
            # AND some actual retracement exists.
            # -----------------------------------------------

            if (
                enough_retrace
                and
                (
                    structure_touch
                    or
                    fvg_touch
                    or
                    ob_touch
                )
            ):

                pullback_index = i

                pullback_structure_touch = (
                    structure_touch
                )

                pullback_fvg = fvg_touch

                pullback_ob = ob_touch

                break

        # ====================================================
        # SHORT
        # ====================================================

        else:

            structure_touch = price_near_level(
                c,
                structure_level,
                STRUCTURE_ZONE_TOLERANCE_PCT
            )

            fvg_touch = any(
                overlaps(
                    c.l,
                    c.h,
                    z
                )
                for z in fvgs
            )

            ob_touch = any(
                overlaps(
                    c.l,
                    c.h,
                    z
                )
                for z in obs
            )

            retrace = max(
                0.0,
                c.h - structure_level
            )

            minimum_retrace = (
                structure_level
                * MIN_PULLBACK_PCT
            )

            enough_retrace = (
                retrace
                >= minimum_retrace
            )

            if (
                enough_retrace
                and
                (
                    structure_touch
                    or
                    fvg_touch
                    or
                    ob_touch
                )
            ):

                pullback_index = i

                pullback_structure_touch = (
                    structure_touch
                )

                pullback_fvg = fvg_touch

                pullback_ob = ob_touch

                break

    if pullback_index is None:
        return None

    # --------------------------------------------------------
    # STEP B
    # Confirmation AFTER pullback only.
    # --------------------------------------------------------

    confirm_start = (
        pullback_index + 1
    )

    confirm_end = min(
        len(candles) - 1,
        pullback_index
        + MAX_CONFIRMATION_BARS
    )

    if confirm_start > confirm_end:
        return None

    for i in range(
        confirm_start,
        confirm_end + 1
    ):

        cur = candles[i]

        prev = candles[i - 1]

        # ====================================================
        # LONG CONFIRMATION
        # ====================================================

        if direction == "LONG":

            if not bullish(cur):
                continue

            if cur.c <= prev.h:
                continue

            if body_ratio(cur) < 0.30:
                continue

            return {
                "pullback_index":
                    pullback_index,

                "pullback_ts":
                    candles[
                        pullback_index
                    ].ts,

                "confirmation_index":
                    i,

                "confirmation_ts":
                    cur.ts,

                "structure_touch":
                    pullback_structure_touch,

                "fvg_touch":
                    pullback_fvg,

                "ob_touch":
                    pullback_ob,
            }

        # ====================================================
        # SHORT CONFIRMATION
        # ====================================================

        else:

            if not bearish(cur):
                continue

            if cur.c >= prev.l:
                continue

            if body_ratio(cur) < 0.30:
                continue

            return {
                "pullback_index":
                    pullback_index,

                "pullback_ts":
                    candles[
                        pullback_index
                    ].ts,

                "confirmation_index":
                    i,

                "confirmation_ts":
                    cur.ts,

                "structure_touch":
                    pullback_structure_touch,

                "fvg_touch":
                    pullback_fvg,

                "ob_touch":
                    pullback_ob,
            }

    return None


# ============================================================
# ENTRY ZONE
# ============================================================

def find_entry_zone(
    candles: List[Candle],
    direction: str,
    structure: Dict[str, Any],
    pullback_confirmation: Dict[str, Any]
) -> Tuple[
    Optional[float],
    Optional[float]
]:

    structure_index = (
        structure["index"]
    )

    pullback_index = (
        pullback_confirmation[
            "pullback_index"
        ]
    )

    confirmation_index = (
        pullback_confirmation[
            "confirmation_index"
        ]
    )

    relevant = candles[
        structure_index:
        confirmation_index + 1
    ]

    if len(relevant) < 2:

        return (
            None,
            None
        )

    fvgs = find_fvgs(
        relevant,
        direction,
        max_age=40
    )

    obs = find_obs(
        relevant,
        direction,
        max_age=40
    )

    pullback = candles[
        pullback_index
    ]

    hits: List[Zone] = []

    for z in fvgs:

        if overlaps(
            pullback.l,
            pullback.h,
            z
        ):

            hits.append(z)

    for z in obs:

        if overlaps(
            pullback.l,
            pullback.h,
            z
        ):

            hits.append(z)

    if hits:

        z = sorted(
            hits,
            key=lambda x:
                abs(
                    pullback.c
                    -
                    (
                        x.low
                        + x.high
                    ) / 2
                )
        )[0]

        return (
            z.low,
            z.high
        )

    # --------------------------------------------------------
    # No FVG / OB:
    # Use structure level + pullback range.
    # --------------------------------------------------------

    structure_level = (
        structure["level"]
    )

    if direction == "LONG":

        low = min(
            pullback.l,
            structure_level
        )

        high = max(
            pullback.h,
            structure_level
        )

    else:

        low = min(
            pullback.l,
            structure_level
        )

        high = max(
            pullback.h,
            structure_level
        )

    return (
        low,
        high
    )


# ============================================================
# SYMBOL ANALYSIS
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
            len(c4) < 40
            or
            len(c15) < 100
        ):

            return []

        results: List[Signal] = []

        for direction in (
            "LONG",
            "SHORT"
        ):

            # =================================================
            # 1. 4H CISD
            # =================================================

            cisd4 = find_latest_cisd(
                c4,
                direction,
                MAX_4H_CISD_BARS
            )

            if not cisd4:
                continue

            # =================================================
            # 2. FIND 15M START
            # =================================================

            start15 = next(
                (
                    i
                    for i, c
                    in enumerate(c15)
                    if c.ts > cisd4["ts"]
                ),
                len(c15)
            )

            if (
                start15
                >= len(c15) - 10
            ):

                continue

            # =================================================
            # 3. STRUCTURE
            # =================================================

            structure = find_15m_structure(
                c15,
                direction,
                start15,
                MAX_15M_STRUCTURE_BARS
            )

            if not structure:
                continue

            # =================================================
            # 4. PULLBACK + CONFIRMATION
            # =================================================

            pc = (
                find_pullback_and_confirmation(
                    c15,
                    direction,
                    structure
                )
            )

            if not pc:
                continue

            # =================================================
            # 5. ENTRY ZONE
            # =================================================

            (
                entry_low,
                entry_high
            ) = find_entry_zone(
                c15,
                direction,
                structure,
                pc
            )

            # =================================================
            # 6. 4H FVG / OB
            # =================================================

            c4_after = c4[
                cisd4["index"]:
            ]

            fvg4_zones = find_fvgs(
                c4_after,
                direction,
                max_age=40
            )

            ob4_zones = find_obs(
                c4_after,
                direction,
                max_age=50
            )

            fvg4 = (
                len(fvg4_zones) > 0
            )

            ob4 = (
                len(ob4_zones) > 0
            )

            # =================================================
            # 7. SCORE
            # =================================================

            score = 70

            notes = [
                "4H CISD body-close confirmed",
                "15M structure break confirmed",
                "15M real pullback confirmed",
                "15M confirmation body-close confirmed",
            ]

            if cisd4["sweep"]:

                score += 7

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

            if pc["structure_touch"]:

                score += 5

                notes.append(
                    "15M pullback touched structure level"
                )

            if pc["fvg_touch"]:

                score += 5

                notes.append(
                    "15M pullback interacted with FVG"
                )

            if pc["ob_touch"]:

                score += 5

                notes.append(
                    "15M pullback interacted with OB"
                )

            score = min(
                score,
                100
            )

            # =================================================
            # 8. SIGNAL
            # =================================================

            results.append(
                Signal(
                    symbol=symbol,
                    direction=direction,

                    score=score,

                    status=
                        "ENTRY_CANDIDATE",

                    price=c15[-1].c,

                    cisd_ts=
                        cisd4["ts"],

                    cisd_level=
                        cisd4["level"],

                    structure_ts=
                        structure["ts"],

                    structure_level=
                        structure["level"],

                    pullback_ts=
                        pc["pullback_ts"],

                    confirmation_ts=
                        pc["confirmation_ts"],

                    cisd_body_close=True,

                    cisd_sweep=
                        cisd4["sweep"],

                    structure_break=True,

                    pullback_confirmed=True,

                    confirmation_body_close=True,

                    pullback_structure_touch=
                        pc["structure_touch"],

                    pullback_fvg=
                        pc["fvg_touch"],

                    pullback_ob=
                        pc["ob_touch"],

                    fvg_4h=fvg4,

                    ob_4h=ob4,

                    fvg_15m=
                        pc["fvg_touch"],

                    ob_15m=
                        pc["ob_touch"],

                    entry_low=
                        entry_low,

                    entry_high=
                        entry_high,

                    notes=notes,
                )
            )

        return results

    except Exception as exc:

        print(
            f"[WARN] {symbol}: {exc}",
            file=sys.stderr
        )

        return []


# ============================================================
# FORMAT
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


def short_ts(
    ms: int
) -> str:

    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).strftime(
        "%m-%d %H:%M UTC"
    )


def format_signal(
    s: Signal
) -> str:

    if s.direction == "LONG":

        icon = "🟢"

        title = "LONG 후보"

    else:

        icon = "🔴"

        title = (
            "SHORT 후보 / "
            "LONG 청산경고"
        )

    zone = "-"

    if (
        s.entry_low is not None
        and
        s.entry_high is not None
    ):

        zone = (
            f"{fmt_price(s.entry_low)}"
            f" ~ "
            f"{fmt_price(s.entry_high)}"
        )

    return (
        f"{icon} {s.symbol} | {title}\n"

        f"점수 {s.score} | "
        f"{s.status}\n"

        f"현재가 "
        f"{fmt_price(s.price)}\n"

        f"4H CISD "
        f"{short_ts(s.cisd_ts)} | "
        f"레벨 "
        f"{fmt_price(s.cisd_level)}\n"

        f"4H: body✓ "
        f"sweep "
        f"{'✓' if s.cisd_sweep else '-'} "
        f"FVG "
        f"{'✓' if s.fvg_4h else '-'} "
        f"OB "
        f"{'✓' if s.ob_4h else '-'}\n"

        f"15M structure "
        f"{'✓' if s.structure_break else '-'} "
        f"| pullback "
        f"{'✓' if s.pullback_confirmed else '-'}\n"

        f"15M pullback: "
        f"structure "
        f"{'✓' if s.pullback_structure_touch else '-'} "
        f"FVG "
        f"{'✓' if s.pullback_fvg else '-'} "
        f"OB "
        f"{'✓' if s.pullback_ob else '-'}\n"

        f"15M confirmation "
        f"{'✓' if s.confirmation_body_close else '-'}\n"

        f"structure "
        f"{short_ts(s.structure_ts)}\n"

        f"pullback "
        f"{short_ts(s.pullback_ts)}\n"

        f"confirmation "
        f"{short_ts(s.confirmation_ts)}\n"

        f"zone: {zone}"
    )


# ============================================================
# TELEGRAM
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
            "[INFO] Telegram secrets are not set."
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
                text,

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


# ============================================================
# REPORT
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
            -x.score,
            x.symbol,
            x.direction
        )
    )

    if not signals:

        return (
            "🔎 4H→15M Structure Scanner v1.4\n"
            f"{now}\n\n"

            f"스캔 {symbol_count}개\n"

            "🔥 유효 후보 없음\n\n"

            "조건:\n"
            "4H CISD ✓\n"
            "15M Structure ✓\n"
            "15M Pullback ✓\n"
            "15M Confirmation ✓"
        )

    blocks = [
        (
            "🔎 4H→15M Structure Scanner v1.4\n"
            f"{now}\n"
            f"스캔 {symbol_count}개\n"
            f"🔥 유효 후보 {len(signals)}개"
        )
    ]

    # Telegram noise reduction.
    # Top 10 only.
    for s in signals[:10]:

        blocks.append(
            format_signal(s)
        )

    return "\n\n".join(
        blocks
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    started = time.time()

    print(
        "\n"
        "============================================\n"
        " Bitget 4H -> 15M Structure Scanner v1.4\n"
        "============================================\n"
    )

    print(
        "CRYPTO ONLY | "
        "USDT PERPETUAL ONLY | "
        "LONG + SHORT"
    )

    print(
        "4H CISD -> "
        "15M STRUCTURE -> "
        "PULLBACK -> "
        "CONFIRMATION"
    )

    print(
        "No orders."
    )

    # ========================================================
    # SYMBOLS
    # ========================================================

    symbols = get_symbols()

    print(
        f"[INFO] selected symbols: "
        f"{len(symbols)}"
    )

    # ========================================================
    # SCAN
    # ========================================================

    all_signals: List[Signal] = []

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

    # ========================================================
    # SORT
    # ========================================================

    all_signals.sort(
        key=lambda x: (
            -x.score,
            x.symbol,
            x.direction
        )
    )

    # ========================================================
    # REPORT
    # ========================================================

    report = build_report(
        all_signals,
        len(symbols)
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

    with open(
        "structure_scan_results.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            [
                asdict(signal)
                for signal in all_signals
            ],
            f,
            ensure_ascii=False,
            indent=2,
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
