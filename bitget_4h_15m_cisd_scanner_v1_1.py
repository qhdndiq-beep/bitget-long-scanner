#!/usr/bin/env python3

"""
Bitget 4H -> 15M Structure Scanner v1.5

CORE FLOW
---------
4H CISD
   ↓
15M SWING STRUCTURE BREAK
   ↓
15M REAL PULLBACK
   ↓
STRUCTURE / FVG / OB ZONE INTERACTION
   ↓
15M CONFIRMATION
   ↓
FINAL CANDIDATE

v1.5 CHANGE
-----------
The previous version treated "previous candle high/low break"
as a structure break.

v1.5 uses actual local swing highs/lows.

LONG
----
1. Recent 4H bullish CISD
2. Find a meaningful 15M swing high after the 4H CISD
3. Bullish candle BODY closes above that swing high
4. Price pulls back toward the broken structure
5. Pullback interacts with structure / FVG / OB
6. A later bullish confirmation candle closes above
   the previous confirmation candle high

SHORT
-----
1. Recent 4H bearish CISD
2. Find a meaningful 15M swing low after the 4H CISD
3. Bearish candle BODY closes below that swing low
4. Price pulls back toward the broken structure
5. Pullback interacts with structure / FVG / OB
6. A later bearish confirmation candle closes below
   the previous confirmation candle low

IMPORTANT
---------
This is a heuristic scanner.
It is NOT an exact reproduction of TradingView/LuxAlgo CISD.

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
from typing import Any, Dict, List, Optional, Tuple

from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://api.bitget.com"

PRODUCT_TYPE = "USDT-FUTURES"

# ------------------------------------------------------------
# History
# ------------------------------------------------------------

HISTORY_LIMIT_4H = 200
HISTORY_LIMIT_15M = 300

# ------------------------------------------------------------
# Workers / HTTP
# ------------------------------------------------------------

MAX_WORKERS = 8

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3

# ------------------------------------------------------------
# 4H CISD
# ------------------------------------------------------------

MAX_4H_CISD_BARS = 8
# Maximum ~32 hours old

# ------------------------------------------------------------
# 15M structure
# ------------------------------------------------------------

MAX_15M_STRUCTURE_BARS = 96
# Maximum ~24 hours after 4H CISD

SWING_LEFT = 2
SWING_RIGHT = 2

# Minimum number of candles between the 4H event
# and a usable swing structure.
MIN_STRUCTURE_DISTANCE = 3

# ------------------------------------------------------------
# Pullback
# ------------------------------------------------------------

MAX_PULLBACK_BARS = 32
# ~8 hours after structure break

MIN_PULLBACK_PCT = 0.0005
# 0.05%

# ------------------------------------------------------------
# Confirmation
# ------------------------------------------------------------

MAX_CONFIRMATION_BARS = 16
# ~4 hours after pullback

# ------------------------------------------------------------
# Zone
# ------------------------------------------------------------

ZONE_TOLERANCE_PCT = 0.0015

# ------------------------------------------------------------
# Signal quality
# ------------------------------------------------------------

MIN_CISD_BODY_RATIO = 0.35
MIN_STRUCTURE_BODY_RATIO = 0.30
MIN_CONFIRMATION_BODY_RATIO = 0.30


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
                        "bitget-4h15m-structure-scanner/1.5",
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

    """
    ONLY:

    crypto
    perpetual
    USDT
    online
    non-RWA
    """

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

    # Remove incomplete current candle.
    candles = [
        x for x in candles
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
        /
        candle_range(c)
    )


# ============================================================
# SWING HELPERS
# ============================================================

def is_swing_high(
    candles: List[Candle],
    idx: int,
    left: int = SWING_LEFT,
    right: int = SWING_RIGHT
) -> bool:

    if idx < left:
        return False

    if idx + right >= len(candles):
        return False

    current = candles[idx]

    left_values = [
        candles[j].h
        for j in range(
            idx - left,
            idx
        )
    ]

    right_values = [
        candles[j].h
        for j in range(
            idx + 1,
            idx + right + 1
        )
    ]

    return (
        current.h >= max(left_values)
        and
        current.h >= max(right_values)
    )


def is_swing_low(
    candles: List[Candle],
    idx: int,
    left: int = SWING_LEFT,
    right: int = SWING_RIGHT
) -> bool:

    if idx < left:
        return False

    if idx + right >= len(candles):
        return False

    current = candles[idx]

    left_values = [
        candles[j].l
        for j in range(
            idx - left,
            idx
        )
    ]

    right_values = [
        candles[j].l
        for j in range(
            idx + 1,
            idx + right + 1
        )
    ]

    return (
        current.l <= min(left_values)
        and
        current.l <= min(right_values)
    )


def find_recent_swing_high(
    candles: List[Candle],
    start_index: int,
    end_index: int
) -> Optional[Dict[str, Any]]:

    start = max(
        SWING_LEFT,
        start_index
    )

    end = min(
        len(candles) - SWING_RIGHT - 1,
        end_index
    )

    # Search newest first.
    for i in range(
        end,
        start - 1,
        -1
    ):

        if is_swing_high(
            candles,
            i
        ):

            return {
                "index": i,
                "ts": candles[i].ts,
                "level": candles[i].h,
            }

    return None


def find_recent_swing_low(
    candles: List[Candle],
    start_index: int,
    end_index: int
) -> Optional[Dict[str, Any]]:

    start = max(
        SWING_LEFT,
        start_index
    )

    end = min(
        len(candles) - SWING_RIGHT - 1,
        end_index
    )

    # Search newest first.
    for i in range(
        end,
        start - 1,
        -1
    ):

        if is_swing_low(
            candles,
            i
        ):

            return {
                "index": i,
                "ts": candles[i].ts,
                "level": candles[i].l,
            }

    return None


# ============================================================
# ZONE OVERLAP
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
        (
            (price_low + price_high)
            / 2
        )
        * ZONE_TOLERANCE_PCT
    )

    return (
        price_high >= zone.low - tol
        and
        price_low <= zone.high + tol
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

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------

        if direction == "LONG":

            if not (
                bullish(cur)
                and
                bearish(ref)
            ):
                continue

            if cur.c <= ref.h:
                continue

            if (
                body_ratio(cur)
                < MIN_CISD_BODY_RATIO
            ):
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

        # ----------------------------------------------------
        # SHORT
        # ----------------------------------------------------

        else:

            if not (
                bearish(cur)
                and
                bullish(ref)
            ):
                continue

            if cur.c >= ref.l:
                continue

            if (
                body_ratio(cur)
                < MIN_CISD_BODY_RATIO
            ):
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

    if len(candles) < 3:
        return zones

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

        # ----------------------------------------------------
        # Bullish FVG
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Bearish FVG
        # ----------------------------------------------------

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

    if len(candles) < 2:
        return zones

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

        # ----------------------------------------------------
        # LONG OB
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # SHORT OB
        # ----------------------------------------------------

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
# 15M SWING STRUCTURE BREAK
# ============================================================

def find_15m_structure(
    candles: List[Candle],
    direction: str,
    start_index: int,
    max_bars: int
) -> Optional[Dict[str, Any]]:

    """
    v1.5

    LONG:
        Find a confirmed swing high.
        Later candle must CLOSE above that swing high.

    SHORT:
        Find a confirmed swing low.
        Later candle must CLOSE below that swing low.

    We deliberately use confirmed local swings
    instead of simply using the previous candle.
    """

    search_start = max(
        2,
        start_index
    )

    search_end = min(
        len(candles) - 1,
        start_index + max_bars
    )

    if search_start >= search_end:
        return None

    # --------------------------------------------------------
    # LONG
    # --------------------------------------------------------

    if direction == "LONG":

        for break_index in range(
            search_start,
            search_end + 1
        ):

            cur = candles[
                break_index
            ]

            if (
                body_ratio(cur)
                < MIN_STRUCTURE_BODY_RATIO
            ):
                continue

            # Find the most recent confirmed swing high
            # BEFORE this breakout candle.
            swing = find_recent_swing_high(
                candles,
                search_start,
                break_index - SWING_RIGHT - 1
            )

            if not swing:
                continue

            swing_index = swing["index"]

            if (
                break_index
                - swing_index
                < MIN_STRUCTURE_DISTANCE
            ):
                continue

            # Body close must break swing high.
            if cur.c <= swing["level"]:
                continue

            # Break candle should be bullish.
            if not bullish(cur):
                continue

            return {
                "index": break_index,
                "ts": cur.ts,
                "level": swing["level"],
                "swing_index": swing_index,
                "swing_ts": swing["ts"],
            }

    # --------------------------------------------------------
    # SHORT
    # --------------------------------------------------------

    else:

        for break_index in range(
            search_start,
            search_end + 1
        ):

            cur = candles[
                break_index
            ]

            if (
                body_ratio(cur)
                < MIN_STRUCTURE_BODY_RATIO
            ):
                continue

            swing = find_recent_swing_low(
                candles,
                search_start,
                break_index - SWING_RIGHT - 1
            )

            if not swing:
                continue

            swing_index = swing["index"]

            if (
                break_index
                - swing_index
                < MIN_STRUCTURE_DISTANCE
            ):
                continue

            if cur.c >= swing["level"]:
                continue

            if not bearish(cur):
                continue

            return {
                "index": break_index,
                "ts": cur.ts,
                "level": swing["level"],
                "swing_index": swing_index,
                "swing_ts": swing["ts"],
            }

    return None


# ============================================================
# PULLBACK + CONFIRMATION
# ============================================================

def find_pullback_and_confirmation(
    candles: List[Candle],
    direction: str,
    structure: Dict[str, Any]
) -> Optional[Dict[str, Any]]:

    structure_index = structure[
        "index"
    ]

    structure_level = structure[
        "level"
    ]

    pull_start = (
        structure_index + 1
    )

    pull_end = min(
        len(candles) - 1,
        structure_index
        + MAX_PULLBACK_BARS
    )

    if pull_start > pull_end:
        return None

    # ========================================================
    # STEP A: REAL PULLBACK
    # ========================================================

    pullback_index = None

    for i in range(
        pull_start,
        pull_end + 1
    ):

        c = candles[i]

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------

        if direction == "LONG":

            retrace = (
                structure_level
                - c.l
            )

            # Price touches / penetrates
            # the broken structure level,
            # but closes back above it.
            if (
                c.l <= structure_level
                and
                c.c >= structure_level
                and
                retrace
                >=
                structure_level
                * MIN_PULLBACK_PCT
            ):

                pullback_index = i
                break

        # ----------------------------------------------------
        # SHORT
        # ----------------------------------------------------

        else:

            retrace = (
                c.h
                - structure_level
            )

            if (
                c.h >= structure_level
                and
                c.c <= structure_level
                and
                retrace
                >=
                structure_level
                * MIN_PULLBACK_PCT
            ):

                pullback_index = i
                break

    if pullback_index is None:
        return None

    # ========================================================
    # STEP B: CONFIRMATION
    # ========================================================

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

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------

        if direction == "LONG":

            if not bullish(cur):
                continue

            if (
                cur.c
                <=
                prev.h
            ):
                continue

            if (
                body_ratio(cur)
                <
                MIN_CONFIRMATION_BODY_RATIO
            ):
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
            }

        # ----------------------------------------------------
        # SHORT
        # ----------------------------------------------------

        else:

            if not bearish(cur):
                continue

            if (
                cur.c
                >=
                prev.l
            ):
                continue

            if (
                body_ratio(cur)
                <
                MIN_CONFIRMATION_BODY_RATIO
            ):
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
    Optional[float],
    bool,
    bool
]:

    structure_index = structure[
        "index"
    ]

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

    start = max(
        0,
        structure_index
    )

    end = min(
        len(candles),
        confirmation_index + 1
    )

    relevant = candles[
        start:end
    ]

    if len(relevant) < 3:
        return (
            structure["level"],
            structure["level"],
            False,
            False,
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

    fvg_hit = False
    ob_hit = False

    hits: List[Zone] = []

    # --------------------------------------------------------
    # FVG interaction
    # --------------------------------------------------------

    for z in fvgs:

        if overlaps(
            pullback.l,
            pullback.h,
            z
        ):

            fvg_hit = True
            hits.append(z)

    # --------------------------------------------------------
    # OB interaction
    # --------------------------------------------------------

    for z in obs:

        if overlaps(
            pullback.l,
            pullback.h,
            z
        ):

            ob_hit = True
            hits.append(z)

    # --------------------------------------------------------
    # Prefer FVG / OB zone
    # --------------------------------------------------------

    if hits:

        z = sorted(
            hits,
            key=lambda x:
                abs(
                    pullback.c
                    -
                    (
                        x.low
                        +
                        x.high
                    )
                    / 2
                )
        )[0]

        return (
            z.low,
            z.high,
            fvg_hit,
            ob_hit,
        )

    # --------------------------------------------------------
    # No FVG / OB
    #
    # Use the broken structure level plus
    # pullback candle as practical entry zone.
    # --------------------------------------------------------

    return (
        min(
            structure["level"],
            pullback.l
        ),

        max(
            structure["level"],
            pullback.h
        ),

        False,
        False,
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

        # ====================================================
        # LONG + SHORT
        # ====================================================

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
            # 2. 15M START AFTER 4H CISD
            # =================================================

            start15 = next(
                (
                    i
                    for i, c in enumerate(c15)
                    if c.ts > cisd4["ts"]
                ),
                len(c15)
            )

            if (
                start15
                >=
                len(c15) - 10
            ):
                continue

            # =================================================
            # 3. 15M SWING STRUCTURE BREAK
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
                entry_high,
                fvg15,
                ob15,
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
                cisd4["index"] :
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
                len(fvg4_zones)
                > 0
            )

            ob4 = (
                len(ob4_zones)
                > 0
            )

            # =================================================
            # 7. SCORE
            # =================================================

            score = 70

            notes = [
                "4H CISD body-close confirmed",
                "15M swing structure break confirmed",
                "15M real pullback confirmed",
                "15M confirmation body-close confirmed",
            ]

            # 4H liquidity sweep
            if cisd4["sweep"]:

                score += 7

                notes.append(
                    "4H liquidity sweep"
                )

            # 4H FVG
            if fvg4:

                score += 3

                notes.append(
                    "4H FVG exists"
                )

            # 4H OB
            if ob4:

                score += 3

                notes.append(
                    "4H OB exists"
                )

            # 15M FVG
            if fvg15:

                score += 5

                notes.append(
                    "15M pullback interacted with FVG"
                )

            # 15M OB
            if ob15:

                score += 5

                notes.append(
                    "15M pullback interacted with OB"
                )

            score = min(
                score,
                100
            )

            # =================================================
            # 8. FINAL SIGNAL
            # =================================================

            results.append(
                Signal(
                    symbol=symbol,
                    direction=direction,

                    score=score,
                    status="ENTRY_CANDIDATE",

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

                    fvg_4h=fvg4,
                    ob_4h=ob4,

                    fvg_15m=fvg15,
                    ob_15m=ob15,

                    entry_low=entry_low,
                    entry_high=entry_high,

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
        f"{icon} {s.symbol} | "
        f"{title}\n"

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

        f"15M SWING STRUCTURE "
        f"{'✓' if s.structure_break else '-'}\n"

        f"structure "
        f"{short_ts(s.structure_ts)} | "
        f"level "
        f"{fmt_price(s.structure_level)}\n"

        f"15M PULLBACK "
        f"{'✓' if s.pullback_confirmed else '-'}\n"

        f"pullback "
        f"{short_ts(s.pullback_ts)}\n"

        f"15M CONFIRMATION "
        f"{'✓' if s.confirmation_body_close else '-'}\n"

        f"confirmation "
        f"{short_ts(s.confirmation_ts)}\n"

        f"15M zone: "
        f"FVG "
        f"{'✓' if s.fvg_15m else '-'} "
        f"OB "
        f"{'✓' if s.ob_15m else '-'}\n"

        f"entry zone: "
        f"{zone}"
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
            "[INFO] Telegram secrets "
            "are not set."
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

    # --------------------------------------------------------
    # NO SIGNAL
    # --------------------------------------------------------

    if not signals:

        return (
            "🔎 "
            "4H→15M Structure Scanner v1.5\n"
            f"{now}\n\n"

            f"스캔 {symbol_count}개\n"

            "🔥 유효 후보 없음\n\n"

            "조건:\n"

            "4H CISD ✓\n"
            "15M Swing Structure ✓\n"
            "15M Pullback ✓\n"
            "15M Confirmation ✓"
        )

    # --------------------------------------------------------
    # SIGNALS
    # --------------------------------------------------------

    blocks = [
        (
            "🔎 "
            "4H→15M Structure Scanner v1.5\n"

            f"{now}\n"

            f"스캔 {symbol_count}개\n"

            f"🔥 유효 후보 "
            f"{len(signals)}개"
        )
    ]

    # Telegram noise reduction.
    # Only top 10.
    for signal in signals[:10]:

        blocks.append(
            format_signal(signal)
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
        " Bitget 4H -> 15M Structure Scanner v1.5\n"
        "============================================\n"
    )

    print(
        "CRYPTO ONLY | "
        "USDT PERPETUAL ONLY | "
        "LONG + SHORT"
    )

    print(
        "4H CISD -> "
        "15M SWING STRUCTURE -> "
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
