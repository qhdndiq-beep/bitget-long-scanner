#!/usr/bin/env python3

"""
Bitget 4H -> 15M Structure Scanner v1.8

DIAGNOSTIC BUILD

FLOW
----
4H directional event
    ↓
4H CISD
    ↓
15M Swing Structure
    ↓
15M Structure Break
    ↓
15M Pullback
    ↓
15M FVG / OB / Structure interaction
    ↓
15M Confirmation
    ↓
FINAL CANDIDATE

v1.8
----
1. 4H CISD detection widened to actual structure/liquidity behavior
2. Separates:
   - 4H directional event
   - 4H sweep
   - 4H CISD
3. Full diagnostic counters
4. LONG / SHORT counters separately
5. Does NOT place orders
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

# ------------------------------------------------------------
# 4H
# ------------------------------------------------------------

MAX_4H_LOOKBACK = 12

CISD_LOOKBACK = 8

MIN_CISD_BODY_RATIO = 0.25

# ------------------------------------------------------------
# 15M
# ------------------------------------------------------------

MAX_15M_STRUCTURE_BARS = 96

SWING_LEFT = 2
SWING_RIGHT = 2

MIN_STRUCTURE_DISTANCE = 2

MIN_STRUCTURE_BODY_RATIO = 0.25

# ------------------------------------------------------------
# Pullback
# ------------------------------------------------------------

MAX_PULLBACK_BARS = 32

MIN_PULLBACK_PCT = 0.0003

# ------------------------------------------------------------
# Confirmation
# ------------------------------------------------------------

MAX_CONFIRMATION_BARS = 16

MIN_CONFIRMATION_BODY_RATIO = 0.25

# ------------------------------------------------------------
# Zone
# ------------------------------------------------------------

ZONE_TOLERANCE_PCT = 0.002


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

    sweep: bool
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

    url = (
        BASE_URL
        + path
        + "?"
        + urlencode(params)
    )

    last_err = None

    for attempt in range(REQUEST_RETRIES):

        try:

            req = Request(
                url,
                headers={
                    "User-Agent":
                        "bitget-4h15m-scanner-v1.8",
                    "Accept":
                        "application/json",
                },
                method="GET",
            )

            with urlopen(
                req,
                timeout=REQUEST_TIMEOUT
            ) as response:

                data = json.loads(
                    response.read().decode()
                )

            if data.get("code") != "00000":

                raise RuntimeError(
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
        f"request failed: {last_err}"
    )


# ============================================================
# SYMBOLS
# ============================================================

def get_symbols() -> List[str]:

    data = get_json(
        "/api/v3/market/instruments",
        {
            "category": PRODUCT_TYPE
        }
    )

    symbols = []

    for item in data.get("data", []):

        symbol = str(
            item.get("symbol", "")
        ).strip()

        quote = str(
            item.get("quoteCoin", "")
        ).upper()

        symbol_type = str(
            item.get("symbolType", "")
        ).lower()

        contract_type = str(
            item.get("type", "")
        ).lower()

        status = str(
            item.get("status", "")
        ).lower()

        is_rwa = str(
            item.get("isRwa", "YES")
        ).upper()

        if (
            symbol.endswith("USDT")
            and quote == "USDT"
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
    limit: int
) -> List[Candle]:

    data = get_json(
        "/api/v2/mix/market/history-candles",
        {
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "granularity": granularity,
            "limit": limit,
        }
    )

    candles = []

    interval = {
        "4H": 4 * 60 * 60 * 1000,
        "15m": 15 * 60 * 1000,
    }[granularity]

    now = int(
        time.time() * 1000
    )

    for row in data.get("data", []):

        if len(row) < 6:
            continue

        ts = int(row[0])

        # Remove unfinished candle.
        if ts + interval > now:
            continue

        candles.append(
            Candle(
                ts=ts,
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

    return candles


# ============================================================
# HELPERS
# ============================================================

def bullish(c: Candle) -> bool:
    return c.c > c.o


def bearish(c: Candle) -> bool:
    return c.c < c.o


def body_ratio(c: Candle) -> float:

    rng = max(
        c.h - c.l,
        1e-12
    )

    return abs(
        c.c - c.o
    ) / rng


# ============================================================
# SWINGS
# ============================================================

def is_swing_high(
    candles: List[Candle],
    i: int
) -> bool:

    if (
        i < SWING_LEFT
        or
        i + SWING_RIGHT >= len(candles)
    ):
        return False

    h = candles[i].h

    for j in range(
        i - SWING_LEFT,
        i
    ):

        if h < candles[j].h:
            return False

    for j in range(
        i + 1,
        i + SWING_RIGHT + 1
    ):

        if h < candles[j].h:
            return False

    return True


def is_swing_low(
    candles: List[Candle],
    i: int
) -> bool:

    if (
        i < SWING_LEFT
        or
        i + SWING_RIGHT >= len(candles)
    ):
        return False

    l = candles[i].l

    for j in range(
        i - SWING_LEFT,
        i
    ):

        if l > candles[j].l:
            return False

    for j in range(
        i + 1,
        i + SWING_RIGHT + 1
    ):

        if l > candles[j].l:
            return False

    return True


# ============================================================
# 4H SWEEP
# ============================================================

def find_4h_sweep(
    candles: List[Candle],
    direction: str
) -> Optional[Dict[str, Any]]:

    end = len(candles) - 1

    start = max(
        3,
        end - MAX_4H_LOOKBACK
    )

    for i in range(
        end,
        start - 1,
        -1
    ):

        cur = candles[i]

        left_start = max(
            0,
            i - CISD_LOOKBACK
        )

        previous = candles[
            left_start:i
        ]

        if len(previous) < 3:
            continue

        if direction == "LONG":

            prior_low = min(
                c.l for c in previous
            )

            # Sweep below liquidity,
            # then recover above it.
            if (
                cur.l < prior_low
                and
                cur.c > prior_low
            ):

                return {
                    "index": i,
                    "ts": cur.ts,
                    "level": prior_low,
                }

        else:

            prior_high = max(
                c.h for c in previous
            )

            if (
                cur.h > prior_high
                and
                cur.c < prior_high
            ):

                return {
                    "index": i,
                    "ts": cur.ts,
                    "level": prior_high,
                }

    return None


# ============================================================
# 4H CISD
# ============================================================

def find_4h_cisd(
    candles: List[Candle],
    direction: str
) -> Optional[Dict[str, Any]]:

    if len(candles) < 20:
        return None

    end = len(candles) - 1

    start = max(
        3,
        end - MAX_4H_LOOKBACK
    )

    # --------------------------------------------------------
    # Look for a body-close structure shift.
    # --------------------------------------------------------

    for i in range(
        end,
        start - 1,
        -1
    ):

        cur = candles[i]

        left_start = max(
            0,
            i - CISD_LOOKBACK
        )

        previous = candles[
            left_start:i
        ]

        if len(previous) < 3:
            continue

        if (
            body_ratio(cur)
            < MIN_CISD_BODY_RATIO
        ):
            continue

        if direction == "LONG":

            reference_high = max(
                c.h for c in previous
            )

            if (
                bullish(cur)
                and
                cur.c > reference_high
            ):

                return {
                    "index": i,
                    "ts": cur.ts,
                    "level": reference_high,
                }

        else:

            reference_low = min(
                c.l for c in previous
            )

            if (
                bearish(cur)
                and
                cur.c < reference_low
            ):

                return {
                    "index": i,
                    "ts": cur.ts,
                    "level": reference_low,
                }

    return None


# ============================================================
# 15M STRUCTURE
# ============================================================

def find_structure(
    candles: List[Candle],
    direction: str,
    start: int
) -> Optional[Dict[str, Any]]:

    end = min(
        len(candles) - 1,
        start + MAX_15M_STRUCTURE_BARS
    )

    if start >= end:
        return None

    for break_i in range(
        start,
        end + 1
    ):

        cur = candles[break_i]

        if (
            body_ratio(cur)
            < MIN_STRUCTURE_BODY_RATIO
        ):
            continue

        if direction == "LONG":

            swings = []

            for j in range(
                max(SWING_LEFT, start),
                break_i - SWING_RIGHT
            ):

                if is_swing_high(
                    candles,
                    j
                ):

                    swings.append(j)

            if not swings:
                continue

            swing_i = swings[-1]

            if (
                break_i - swing_i
                < MIN_STRUCTURE_DISTANCE
            ):
                continue

            level = candles[
                swing_i
            ].h

            if (
                bullish(cur)
                and
                cur.c > level
            ):

                return {
                    "index": break_i,
                    "ts": cur.ts,
                    "level": level,
                }

        else:

            swings = []

            for j in range(
                max(SWING_LEFT, start),
                break_i - SWING_RIGHT
            ):

                if is_swing_low(
                    candles,
                    j
                ):

                    swings.append(j)

            if not swings:
                continue

            swing_i = swings[-1]

            if (
                break_i - swing_i
                < MIN_STRUCTURE_DISTANCE
            ):
                continue

            level = candles[
                swing_i
            ].l

            if (
                bearish(cur)
                and
                cur.c < level
            ):

                return {
                    "index": break_i,
                    "ts": cur.ts,
                    "level": level,
                }

    return None


# ============================================================
# PULLBACK
# ============================================================

def find_pullback(
    candles: List[Candle],
    direction: str,
    structure: Dict[str, Any]
) -> Optional[Dict[str, Any]]:

    start = (
        structure["index"] + 1
    )

    end = min(
        len(candles) - 1,
        structure["index"]
        + MAX_PULLBACK_BARS
    )

    level = structure["level"]

    for i in range(
        start,
        end + 1
    ):

        c = candles[i]

        if direction == "LONG":

            if (
                c.l <= level
                and
                c.c >= level
                and
                (
                    level - c.l
                )
                >=
                level * MIN_PULLBACK_PCT
            ):

                return {
                    "index": i,
                    "ts": c.ts,
                }

        else:

            if (
                c.h >= level
                and
                c.c <= level
                and
                (
                    c.h - level
                )
                >=
                level * MIN_PULLBACK_PCT
            ):

                return {
                    "index": i,
                    "ts": c.ts,
                }

    return None


# ============================================================
# CONFIRMATION
# ============================================================

def find_confirmation(
    candles: List[Candle],
    direction: str,
    pullback: Dict[str, Any]
) -> Optional[Dict[str, Any]]:

    start = (
        pullback["index"] + 1
    )

    end = min(
        len(candles) - 1,
        pullback["index"]
        + MAX_CONFIRMATION_BARS
    )

    for i in range(
        start,
        end + 1
    ):

        cur = candles[i]
        prev = candles[i - 1]

        if (
            body_ratio(cur)
            < MIN_CONFIRMATION_BODY_RATIO
        ):
            continue

        if direction == "LONG":

            if (
                bullish(cur)
                and
                cur.c > prev.h
            ):

                return {
                    "index": i,
                    "ts": cur.ts,
                }

        else:

            if (
                bearish(cur)
                and
                cur.c < prev.l
            ):

                return {
                    "index": i,
                    "ts": cur.ts,
                }

    return None


# ============================================================
# FVG / OB
# ============================================================

def find_fvg_ob(
    candles: List[Candle],
    direction: str
) -> Tuple[
    List[Zone],
    List[Zone]
]:

    fvgs = []
    obs = []

    for i in range(
        2,
        len(candles)
    ):

        a = candles[i - 2]
        c = candles[i]

        if (
            direction == "LONG"
            and
            a.h < c.l
        ):

            fvgs.append(
                Zone(
                    kind="FVG",
                    direction=direction,
                    low=a.h,
                    high=c.l,
                    ts=c.ts,
                    age_bars=
                        len(candles)
                        - 1 - i
                )
            )

        elif (
            direction == "SHORT"
            and
            a.l > c.h
        ):

            fvgs.append(
                Zone(
                    kind="FVG",
                    direction=direction,
                    low=c.h,
                    high=a.l,
                    ts=c.ts,
                    age_bars=
                        len(candles)
                        - 1 - i
                )
            )

    for i in range(
        0,
        len(candles) - 1
    ):

        cur = candles[i]
        nxt = candles[i + 1]

        if (
            direction == "LONG"
            and
            bearish(cur)
            and
            bullish(nxt)
            and
            nxt.c > cur.h
        ):

            obs.append(
                Zone(
                    kind="OB",
                    direction=direction,
                    low=cur.l,
                    high=cur.o,
                    ts=cur.ts,
                    age_bars=
                        len(candles)
                        - 1 - i
                )
            )

        elif (
            direction == "SHORT"
            and
            bullish(cur)
            and
            bearish(nxt)
            and
            nxt.c < cur.l
        ):

            obs.append(
                Zone(
                    kind="OB",
                    direction=direction,
                    low=cur.o,
                    high=cur.h,
                    ts=cur.ts,
                    age_bars=
                        len(candles)
                        - 1 - i
                )
            )

    return fvgs, obs


def zone_hit(
    candle: Candle,
    zone: Zone
) -> bool:

    tol = max(
        (
            candle.h
            -
            candle.l
        ),
        zone.high - zone.low,
        candle.c * ZONE_TOLERANCE_PCT
    )

    return (
        candle.h >= zone.low - tol
        and
        candle.l <= zone.high + tol
    )


# ============================================================
# SYMBOL ANALYSIS
# ============================================================

def analyze_symbol(
    symbol: str
) -> Tuple[
    List[Signal],
    Dict[str, int]
]:

    counters = {
        "4h_directional": 0,
        "4h_sweep": 0,
        "4h_cisd": 0,
        "15m_structure": 0,
        "15m_break": 0,
        "15m_pullback": 0,
        "15m_zone": 0,
        "15m_confirmation": 0,

        "long_4h_directional": 0,
        "short_4h_directional": 0,

        "long_4h_sweep": 0,
        "short_4h_sweep": 0,

        "long_4h_cisd": 0,
        "short_4h_cisd": 0,

        "long_structure": 0,
        "short_structure": 0,

        "long_pullback": 0,
        "short_pullback": 0,

        "long_zone": 0,
        "short_zone": 0,

        "long_confirmation": 0,
        "short_confirmation": 0,

        "long_final": 0,
        "short_final": 0,
    }

    signals = []

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
            or
            len(c15) < 80
        ):

            return signals, counters

        for direction in (
            "LONG",
            "SHORT"
        ):

            # =================================================
            # 1. DIRECTIONAL EVENT
            # =================================================

            cisd = find_4h_cisd(
                c4,
                direction
            )

            if cisd:

                counters[
                    "4h_directional"
                ] += 1

                counters[
                    f"{direction.lower()}_4h_directional"
                ] += 1

            else:
                continue

            # =================================================
            # 2. SWEEP
            # =================================================

            sweep = find_4h_sweep(
                c4,
                direction
            )

            if sweep:

                counters[
                    "4h_sweep"
                ] += 1

                counters[
                    f"{direction.lower()}_4h_sweep"
                ] += 1

            # =================================================
            # 3. CISD
            # =================================================

            # v1.8 intentionally allows CISD
            # with OR without sweep.
            #
            # This is important because a sweep is
            # supporting evidence, not mandatory.

            counters[
                "4h_cisd"
            ] += 1

            counters[
                f"{direction.lower()}_4h_cisd"
            ] += 1

            # =================================================
            # 4. 15M START
            # =================================================

            start15 = next(
                (
                    i
                    for i, c in enumerate(c15)
                    if c.ts > cisd["ts"]
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
            # 5. STRUCTURE
            # =================================================

            structure = find_structure(
                c15,
                direction,
                start15
            )

            if not structure:
                continue

            counters[
                "15m_structure"
            ] += 1

            counters[
                f"{direction.lower()}_structure"
            ] += 1

            counters[
                "15m_break"
            ] += 1

            # =================================================
            # 6. PULLBACK
            # =================================================

            pullback = find_pullback(
                c15,
                direction,
                structure
            )

            if not pullback:
                continue

            counters[
                "15m_pullback"
            ] += 1

            counters[
                f"{direction.lower()}_pullback"
            ] += 1

            # =================================================
            # 7. ZONE
            # =================================================

            relevant = c15[
                structure["index"]:
                pullback["index"] + 1
            ]

            fvgs, obs = find_fvg_ob(
                relevant,
                direction
            )

            pb_candle = c15[
                pullback["index"]
            ]

            fvg_hit = any(
                zone_hit(
                    pb_candle,
                    z
                )
                for z in fvgs
            )

            ob_hit = any(
                zone_hit(
                    pb_candle,
                    z
                )
                for z in obs
            )

            # Zone is supportive, not mandatory.
            if (
                fvg_hit
                or
                ob_hit
            ):

                counters[
                    "15m_zone"
                ] += 1

                counters[
                    f"{direction.lower()}_zone"
                ] += 1

            # =================================================
            # 8. CONFIRMATION
            # =================================================

            confirmation = find_confirmation(
                c15,
                direction,
                pullback
            )

            if not confirmation:
                continue

            counters[
                "15m_confirmation"
            ] += 1

            counters[
                f"{direction.lower()}_confirmation"
            ] += 1

            # =================================================
            # 9. SCORE
            # =================================================

            score = 70

            notes = [
                "4H CISD",
                "15M swing structure break",
                "15M pullback",
                "15M confirmation",
            ]

            if sweep:

                score += 7

                notes.append(
                    "4H liquidity sweep"
                )

            if fvg_hit:

                score += 5

                notes.append(
                    "15M FVG interaction"
                )

            if ob_hit:

                score += 5

                notes.append(
                    "15M OB interaction"
                )

            score = min(
                100,
                score
            )

            # =================================================
            # 10. FINAL
            # =================================================

            counters[
                f"{direction.lower()}_final"
            ] += 1

            signals.append(
                Signal(
                    symbol=symbol,
                    direction=direction,
                    score=score,
                    status="ENTRY_CANDIDATE",
                    price=c15[-1].c,

                    cisd_ts=cisd["ts"],
                    cisd_level=cisd["level"],

                    structure_ts=
                        structure["ts"],
                    structure_level=
                        structure["level"],

                    pullback_ts=
                        pullback["ts"],

                    confirmation_ts=
                        confirmation["ts"],

                    sweep=bool(sweep),

                    fvg_15m=fvg_hit,
                    ob_15m=ob_hit,

                    entry_low=
                        structure["level"],

                    entry_high=
                        structure["level"],

                    notes=notes,
                )
            )

    except Exception as exc:

        print(
            f"[WARN] {symbol}: {exc}",
            file=sys.stderr
        )

    return signals, counters


# ============================================================
# REPORT
# ============================================================

def build_report(
    signals: List[Signal],
    counters: Dict[str, int],
    symbol_count: int
) -> str:

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    signals.sort(
        key=lambda x: (
            -x.score,
            x.symbol
        )
    )

    text = []

    text.append(
        "🔎 4H→15M Structure Scanner v1.8"
    )

    text.append(now)

    text.append(
        f"\n스캔 {symbol_count}개"
    )

    text.append(
        "\n━━━━━━━━━━━━━━━━━━━━━━"
    )

    text.append(
        "🔬 v1.8 DIAGNOSTIC"
    )

    text.append(
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    text.append(
        f"① 4H 방향성 이벤트 : "
        f"{counters['4h_directional']}\n"
        f"   └ LONG  : "
        f"{counters['long_4h_directional']}\n"
        f"   └ SHORT : "
        f"{counters['short_4h_directional']}"
    )

    text.append(
        f"② 4H Liquidity Sweep : "
        f"{counters['4h_sweep']}\n"
        f"   └ LONG  : "
        f"{counters['long_4h_sweep']}\n"
        f"   └ SHORT : "
        f"{counters['short_4h_sweep']}"
    )

    text.append(
        f"③ 4H CISD 통과 : "
        f"{counters['4h_cisd']}\n"
        f"   └ LONG  : "
        f"{counters['long_4h_cisd']}\n"
        f"   └ SHORT : "
        f"{counters['short_4h_cisd']}"
    )

    text.append(
        f"④ 15M Structure : "
        f"{counters['15m_structure']}\n"
        f"   └ LONG  : "
        f"{counters['long_structure']}\n"
        f"   └ SHORT : "
        f"{counters['short_structure']}"
    )

    text.append(
        f"⑤ 15M Pullback : "
        f"{counters['15m_pullback']}\n"
        f"   └ LONG  : "
        f"{counters['long_pullback']}\n"
        f"   └ SHORT : "
        f"{counters['short_pullback']}"
    )

    text.append(
        f"⑥ 15M Zone Interaction : "
        f"{counters['15m_zone']}\n"
        f"   └ LONG  : "
        f"{counters['long_zone']}\n"
        f"   └ SHORT : "
        f"{counters['short_zone']}"
    )

    text.append(
        f"⑦ 15M Confirmation : "
        f"{counters['15m_confirmation']}\n"
        f"   └ LONG  : "
        f"{counters['long_confirmation']}\n"
        f"   └ SHORT : "
        f"{counters['short_confirmation']}"
    )

    text.append(
        f"\n🔥 FINAL CANDIDATE : "
        f"{len(signals)}"
    )

    text.append(
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    if not signals:

        text.append(
            "\n⚠️ 현재 최종 후보 없음."
        )

    else:

        text.append("")

        for s in signals[:10]:

            icon = (
                "🟢"
                if s.direction == "LONG"
                else "🔴"
            )

            text.append(
                f"{icon} {s.symbol} "
                f"{s.direction} | "
                f"점수 {s.score}"
            )

            text.append(
                f"현재가: {s.price}"
            )

            text.append(
                f"4H CISD: "
                f"{short_ts(s.cisd_ts)}"
            )

            text.append(
                f"15M Structure: "
                f"{short_ts(s.structure_ts)}"
            )

            text.append(
                f"15M Pullback: "
                f"{short_ts(s.pullback_ts)}"
            )

            text.append(
                f"15M Confirmation: "
                f"{short_ts(s.confirmation_ts)}"
            )

            text.append(
                " | ".join(s.notes)
            )

    return "\n".join(text)


def short_ts(ms: int) -> str:

    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).strftime(
        "%m-%d %H:%M UTC"
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
    ) as response:

        response.read()


# ============================================================
# MAIN
# ============================================================

def main():

    started = time.time()

    print(
        "\n"
        "============================================\n"
        " Bitget 4H -> 15M Structure Scanner v1.8\n"
        "============================================\n"
        "DIAGNOSTIC BUILD\n"
        "LONG + SHORT\n"
        "NO ORDERS\n"
    )

    symbols = get_symbols()

    print(
        f"[INFO] selected symbols: "
        f"{len(symbols)}"
    )

    all_signals = []

    total = {}

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

                signals, counters = (
                    future.result()
                )

                all_signals.extend(
                    signals
                )

                for key, value in counters.items():

                    total[key] = (
                        total.get(key, 0)
                        + value
                    )

            except Exception as exc:

                print(
                    f"[WARN] worker: {exc}",
                    file=sys.stderr
                )

            if done % 50 == 0:

                print(
                    f"[INFO] progress "
                    f"{done}/{len(symbols)}"
                )

    report = build_report(
        all_signals,
        total,
        len(symbols)
    )

    print(
        "\n" + report
    )

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

    with open(
        "structure_scan_results.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            [
                asdict(x)
                for x in all_signals
            ],
            f,
            ensure_ascii=False,
            indent=2
        )

    print(
        f"\n[INFO] elapsed: "
        f"{time.time() - started:.1f}s"
    )


if __name__ == "__main__":
    main()
