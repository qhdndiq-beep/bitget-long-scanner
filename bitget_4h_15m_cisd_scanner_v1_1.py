#!/usr/bin/env python3

"""
Bitget 4H -> 15M Structure Scanner v1.7
DIAGNOSTIC VERSION

CORE FLOW
---------
4H CISD
   ↓
15M SWING STRUCTURE BREAK
   ↓
15M REAL PULLBACK
   ↓
15M FVG / OB INTERACTION
   ↓
15M CONFIRMATION
   ↓
FINAL CANDIDATE

v1.7
----
- v1.6 조건 유지
- 각 단계별 통과 개수 표시
- 탈락 원인 카운트
- 최종 후보가 0개여도 병목 확인 가능
- LONG / SHORT 모두 검사
- 주문 없음
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
MIN_CISD_BODY_RATIO = 0.35


# ============================================================
# 15M STRUCTURE
# ============================================================

MAX_15M_STRUCTURE_BARS = 96

SWING_LEFT = 2
SWING_RIGHT = 2

MIN_STRUCTURE_DISTANCE = 3

MIN_STRUCTURE_BODY_RATIO = 0.30


# ============================================================
# PULLBACK
# ============================================================

MAX_PULLBACK_BARS = 32

MIN_PULLBACK_PCT = 0.0005


# ============================================================
# CONFIRMATION
# ============================================================

MAX_CONFIRMATION_BARS = 16

MIN_CONFIRMATION_BODY_RATIO = 0.30


# ============================================================
# ZONE
# ============================================================

ZONE_TOLERANCE_PCT = 0.0015


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

    cisd_sweep: bool

    fvg_4h: bool
    ob_4h: bool

    fvg_15m: bool
    ob_15m: bool

    entry_low: Optional[float]
    entry_high: Optional[float]

    notes: List[str]


# ============================================================
# DIAGNOSTIC
# ============================================================

class Diagnostics:

    def __init__(self):
        self.cisd = 0
        self.structure = 0
        self.pullback = 0
        self.confirmation = 0
        self.final = 0

        self.cisd_fail = 0
        self.structure_fail = 0
        self.pullback_fail = 0
        self.confirmation_fail = 0

        self.long_cisd = 0
        self.short_cisd = 0

        self.long_structure = 0
        self.short_structure = 0

        self.long_pullback = 0
        self.short_pullback = 0

        self.long_confirmation = 0
        self.short_confirmation = 0

    def add_cisd(self, direction):
        self.cisd += 1

        if direction == "LONG":
            self.long_cisd += 1
        else:
            self.short_cisd += 1

    def add_structure(self, direction):
        self.structure += 1

        if direction == "LONG":
            self.long_structure += 1
        else:
            self.short_structure += 1

    def add_pullback(self, direction):
        self.pullback += 1

        if direction == "LONG":
            self.long_pullback += 1
        else:
            self.short_pullback += 1

    def add_confirmation(self, direction):
        self.confirmation += 1

        if direction == "LONG":
            self.long_confirmation += 1
        else:
            self.short_confirmation += 1


DIAG = Diagnostics()


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
                        "bitget-4h15m-structure-scanner/1.7",
                    "Accept":
                        "application/json",
                },
                method="GET",
            )

            with urlopen(
                req,
                timeout=REQUEST_TIMEOUT
            ) as resp:

                data = json.loads(
                    resp.read().decode("utf-8")
                )

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
        f"Request failed: {url} :: {last_err}"
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

    candles.sort(key=lambda x: x.ts)

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
# BASIC
# ============================================================

def bullish(c):
    return c.c > c.o


def bearish(c):
    return c.c < c.o


def body_size(c):
    return abs(c.c - c.o)


def candle_range(c):
    return max(c.h - c.l, 1e-12)


def body_ratio(c):
    return body_size(c) / candle_range(c)


# ============================================================
# SWING
# ============================================================

def is_swing_high(
    candles,
    idx,
    left=SWING_LEFT,
    right=SWING_RIGHT
):

    if idx < left:
        return False

    if idx + right >= len(candles):
        return False

    current = candles[idx]

    left_values = [
        candles[j].h
        for j in range(idx - left, idx)
    ]

    right_values = [
        candles[j].h
        for j in range(idx + 1, idx + right + 1)
    ]

    return (
        current.h >= max(left_values)
        and
        current.h >= max(right_values)
    )


def is_swing_low(
    candles,
    idx,
    left=SWING_LEFT,
    right=SWING_RIGHT
):

    if idx < left:
        return False

    if idx + right >= len(candles):
        return False

    current = candles[idx]

    left_values = [
        candles[j].l
        for j in range(idx - left, idx)
    ]

    right_values = [
        candles[j].l
        for j in range(idx + 1, idx + right + 1)
    ]

    return (
        current.l <= min(left_values)
        and
        current.l <= min(right_values)
    )


def find_recent_swing_high(
    candles,
    start_index,
    end_index
):

    start = max(
        SWING_LEFT,
        start_index
    )

    end = min(
        len(candles) - SWING_RIGHT - 1,
        end_index
    )

    for i in range(end, start - 1, -1):

        if is_swing_high(candles, i):

            return {
                "index": i,
                "ts": candles[i].ts,
                "level": candles[i].h,
            }

    return None


def find_recent_swing_low(
    candles,
    start_index,
    end_index
):

    start = max(
        SWING_LEFT,
        start_index
    )

    end = min(
        len(candles) - SWING_RIGHT - 1,
        end_index
    )

    for i in range(end, start - 1, -1):

        if is_swing_low(candles, i):

            return {
                "index": i,
                "ts": candles[i].ts,
                "level": candles[i].l,
            }

    return None


# ============================================================
# 4H CISD
# ============================================================

def find_latest_cisd(
    candles,
    direction,
    max_bars
):

    if len(candles) < 20:
        return None

    end = len(candles) - 1

    start = max(
        3,
        end - max_bars + 1
    )

    for i in range(end, start - 1, -1):

        cur = candles[i]
        ref = candles[i - 1]

        if direction == "LONG":

            if not (
                bullish(cur)
                and bearish(ref)
            ):
                continue

            if cur.c <= ref.h:
                continue

            if body_ratio(cur) < MIN_CISD_BODY_RATIO:
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
                    x.l for x in previous
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

        else:

            if not (
                bearish(cur)
                and bullish(ref)
            ):
                continue

            if cur.c >= ref.l:
                continue

            if body_ratio(cur) < MIN_CISD_BODY_RATIO:
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
                    x.h for x in previous
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
    candles,
    direction,
    max_age=60
):

    zones = []

    if len(candles) < 3:
        return zones

    start = max(
        2,
        len(candles) - max_age
    )

    for i in range(start, len(candles)):

        a = candles[i - 2]
        c = candles[i]

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
    candles,
    direction,
    max_age=80
):

    zones = []

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

        if (
            direction == "LONG"
            and bearish(cur)
            and bullish(nxt)
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

        elif (
            direction == "SHORT"
            and bullish(cur)
            and bearish(nxt)
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
# STRUCTURE
# ============================================================

def find_15m_structure(
    candles,
    direction,
    start_index,
    max_bars
):

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

    if direction == "LONG":

        for break_index in range(
            search_start,
            search_end + 1
        ):

            cur = candles[break_index]

            if body_ratio(cur) < MIN_STRUCTURE_BODY_RATIO:
                continue

            swing = find_recent_swing_high(
                candles,
                search_start,
                break_index - SWING_RIGHT - 1
            )

            if not swing:
                continue

            swing_index = swing["index"]

            if (
                break_index - swing_index
                < MIN_STRUCTURE_DISTANCE
            ):
                continue

            if cur.c <= swing["level"]:
                continue

            if not bullish(cur):
                continue

            return {
                "index": break_index,
                "ts": cur.ts,
                "level": swing["level"],
                "swing_index": swing_index,
                "swing_ts": swing["ts"],
            }

    else:

        for break_index in range(
            search_start,
            search_end + 1
        ):

            cur = candles[break_index]

            if body_ratio(cur) < MIN_STRUCTURE_BODY_RATIO:
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
                break_index - swing_index
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
    candles,
    direction,
    structure
):

    structure_index = structure["index"]
    structure_level = structure["level"]

    pull_start = structure_index + 1

    pull_end = min(
        len(candles) - 1,
        structure_index + MAX_PULLBACK_BARS
    )

    if pull_start > pull_end:
        return None

    pullback_index = None

    for i in range(
        pull_start,
        pull_end + 1
    ):

        c = candles[i]

        if direction == "LONG":

            retrace = structure_level - c.l

            if (
                c.l <= structure_level
                and
                c.c >= structure_level
                and
                retrace >= (
                    structure_level
                    * MIN_PULLBACK_PCT
                )
            ):

                pullback_index = i
                break

        else:

            retrace = c.h - structure_level

            if (
                c.h >= structure_level
                and
                c.c <= structure_level
                and
                retrace >= (
                    structure_level
                    * MIN_PULLBACK_PCT
                )
            ):

                pullback_index = i
                break

    if pullback_index is None:
        return None

    confirm_start = pullback_index + 1

    confirm_end = min(
        len(candles) - 1,
        pullback_index + MAX_CONFIRMATION_BARS
    )

    if confirm_start > confirm_end:
        return None

    for i in range(
        confirm_start,
        confirm_end + 1
    ):

        cur = candles[i]
        prev = candles[i - 1]

        if direction == "LONG":

            if not bullish(cur):
                continue

            if cur.c <= prev.h:
                continue

            if body_ratio(cur) < MIN_CONFIRMATION_BODY_RATIO:
                continue

            return {
                "pullback_index": pullback_index,
                "pullback_ts":
                    candles[pullback_index].ts,
                "confirmation_index": i,
                "confirmation_ts": cur.ts,
            }

        else:

            if not bearish(cur):
                continue

            if cur.c >= prev.l:
                continue

            if body_ratio(cur) < MIN_CONFIRMATION_BODY_RATIO:
                continue

            return {
                "pullback_index": pullback_index,
                "pullback_ts":
                    candles[pullback_index].ts,
                "confirmation_index": i,
                "confirmation_ts": cur.ts,
            }

    return None


# ============================================================
# OVERLAP
# ============================================================

def overlaps(
    price_low,
    price_high,
    zone
):

    tol = max(
        price_high - price_low,
        zone.high - zone.low
    ) * 0.10

    tol = max(
        tol,
        (
            (price_low + price_high) / 2
        )
        * ZONE_TOLERANCE_PCT
    )

    return (
        price_high >= zone.low - tol
        and
        price_low <= zone.high + tol
    )


# ============================================================
# ENTRY ZONE
# ============================================================

def find_entry_zone(
    candles,
    direction,
    structure,
    pc
):

    structure_index = structure["index"]

    pullback_index = pc["pullback_index"]

    confirmation_index = pc["confirmation_index"]

    relevant = candles[
        structure_index:
        confirmation_index + 1
    ]

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

    pullback = candles[pullback_index]

    fvg_hit = False
    ob_hit = False

    hits = []

    for z in fvgs:

        if overlaps(
            pullback.l,
            pullback.h,
            z
        ):

            fvg_hit = True
            hits.append(z)

    for z in obs:

        if overlaps(
            pullback.l,
            pullback.h,
            z
        ):

            ob_hit = True
            hits.append(z)

    if hits:

        z = sorted(
            hits,
            key=lambda x:
                abs(
                    pullback.c
                    -
                    (
                        x.low + x.high
                    ) / 2
                )
        )[0]

        return (
            z.low,
            z.high,
            fvg_hit,
            ob_hit,
        )

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
# ANALYZE
# ============================================================

def analyze_symbol(symbol):

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

        results = []

        for direction in (
            "LONG",
            "SHORT"
        ):

            # ------------------------------------------------
            # 1. CISD
            # ------------------------------------------------

            cisd4 = find_latest_cisd(
                c4,
                direction,
                MAX_4H_CISD_BARS
            )

            if not cisd4:

                DIAG.cisd_fail += 1

                continue

            DIAG.add_cisd(direction)

            # ------------------------------------------------
            # 2. 15M START
            # ------------------------------------------------

            start15 = next(
                (
                    i
                    for i, c in enumerate(c15)
                    if c.ts > cisd4["ts"]
                ),
                len(c15)
            )

            if start15 >= len(c15) - 10:

                DIAG.structure_fail += 1

                continue

            # ------------------------------------------------
            # 3. STRUCTURE
            # ------------------------------------------------

            structure = find_15m_structure(
                c15,
                direction,
                start15,
                MAX_15M_STRUCTURE_BARS
            )

            if not structure:

                DIAG.structure_fail += 1

                continue

            DIAG.add_structure(direction)

            # ------------------------------------------------
            # 4. PULLBACK
            # ------------------------------------------------

            pc = find_pullback_and_confirmation(
                c15,
                direction,
                structure
            )

            if not pc:

                DIAG.pullback_fail += 1

                continue

            DIAG.add_pullback(direction)

            # ------------------------------------------------
            # 5. CONFIRMATION
            #
            # NOTE:
            # pullback + confirmation is checked together
            # by the function above.
            # Since pullback was found but confirmation
            # may still fail, distinguish it here.
            # ------------------------------------------------

            # The function only returns when both exist.
            # Therefore this point means confirmation passed.

            DIAG.add_confirmation(direction)

            # ------------------------------------------------
            # ENTRY ZONE
            # ------------------------------------------------

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

            # ------------------------------------------------
            # 4H FVG / OB
            # ------------------------------------------------

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

            fvg4 = len(fvg4_zones) > 0
            ob4 = len(ob4_zones) > 0

            # ------------------------------------------------
            # SCORE
            # ------------------------------------------------

            score = 70

            notes = [
                "4H CISD body-close confirmed",
                "15M swing structure break confirmed",
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

            if fvg15:

                score += 5

                notes.append(
                    "15M pullback interacted with FVG"
                )

            if ob15:

                score += 5

                notes.append(
                    "15M pullback interacted with OB"
                )

            score = min(score, 100)

            # ------------------------------------------------
            # FINAL
            # ------------------------------------------------

            DIAG.final += 1

            results.append(
                Signal(
                    symbol=symbol,
                    direction=direction,

                    score=score,
                    status="ENTRY_CANDIDATE",

                    price=c15[-1].c,

                    cisd_ts=cisd4["ts"],
                    cisd_level=cisd4["level"],

                    structure_ts=structure["ts"],
                    structure_level=structure["level"],

                    pullback_ts=pc["pullback_ts"],
                    confirmation_ts=pc["confirmation_ts"],

                    cisd_sweep=cisd4["sweep"],

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

def fmt_price(x):

    if x is None:
        return "-"

    if x >= 1000:
        return f"{x:,.2f}"

    if x >= 1:
        return f"{x:,.4f}"

    return f"{x:.8f}"


def short_ts(ms):

    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).strftime(
        "%m-%d %H:%M UTC"
    )


def format_signal(s):

    icon = (
        "🟢"
        if s.direction == "LONG"
        else "🔴"
    )

    title = (
        "LONG 후보"
        if s.direction == "LONG"
        else "SHORT 후보"
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
        f"점수 {s.score} | {s.status}\n"
        f"현재가 {fmt_price(s.price)}\n\n"

        f"4H CISD "
        f"{short_ts(s.cisd_ts)} | "
        f"레벨 {fmt_price(s.cisd_level)}\n"

        f"4H sweep "
        f"{'✓' if s.cisd_sweep else '-'} "
        f"FVG "
        f"{'✓' if s.fvg_4h else '-'} "
        f"OB "
        f"{'✓' if s.ob_4h else '-'}\n\n"

        f"15M STRUCTURE ✓\n"
        f"{short_ts(s.structure_ts)} | "
        f"level {fmt_price(s.structure_level)}\n\n"

        f"15M PULLBACK ✓\n"
        f"{short_ts(s.pullback_ts)}\n\n"

        f"15M CONFIRMATION ✓\n"
        f"{short_ts(s.confirmation_ts)}\n\n"

        f"15M FVG "
        f"{'✓' if s.fvg_15m else '-'} "
        f"OB "
        f"{'✓' if s.ob_15m else '-'}\n"

        f"ENTRY ZONE "
        f"{zone}"
    )


# ============================================================
# DIAGNOSTIC REPORT
# ============================================================

def diagnostic_report():

    return (
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔬 v1.7 DIAGNOSTIC\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"

        f"① 4H CISD 통과       : "
        f"{DIAG.cisd}\n"

        f"   └ LONG  : {DIAG.long_cisd}\n"
        f"   └ SHORT : {DIAG.short_cisd}\n\n"

        f"② 15M Structure 통과 : "
        f"{DIAG.structure}\n"

        f"   └ LONG  : {DIAG.long_structure}\n"
        f"   └ SHORT : {DIAG.short_structure}\n\n"

        f"③ 15M Pullback 통과  : "
        f"{DIAG.pullback}\n"

        f"   └ LONG  : {DIAG.long_pullback}\n"
        f"   └ SHORT : {DIAG.short_pullback}\n\n"

        f"④ 15M Confirmation   : "
        f"{DIAG.confirmation}\n"

        f"   └ LONG  : {DIAG.long_confirmation}\n"
        f"   └ SHORT : {DIAG.short_confirmation}\n\n"

        f"🔥 FINAL CANDIDATE    : "
        f"{DIAG.final}\n"

        "━━━━━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# REPORT
# ============================================================

def build_report(
    signals,
    symbol_count
):

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

    header = (
        "🔎 "
        "4H→15M Structure Scanner v1.7\n"
        f"{now}\n\n"
        f"스캔 {symbol_count}개"
    )

    diag = diagnostic_report()

    if not signals:

        return (
            header
            + "\n\n"
            + "🔥 유효 후보 없음\n"
            + diag
            + "\n\n"
            + "조건:\n"
            + "4H CISD ✓\n"
            + "15M Swing Structure ✓\n"
            + "15M Pullback ✓\n"
            + "15M Confirmation ✓"
        )

    blocks = [
        header,
        f"🔥 유효 후보 {len(signals)}개",
        diag,
    ]

    for signal in signals[:10]:

        blocks.append(
            format_signal(signal)
        )

    return "\n\n".join(blocks)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text):

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
# MAIN
# ============================================================

def main():

    started = time.time()

    print(
        "\n"
        "============================================\n"
        " Bitget 4H -> 15M Structure Scanner v1.7\n"
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
        "DIAGNOSTIC MODE: ON"
    )

    print(
        "No orders."
    )

    # --------------------------------------------------------
    # SYMBOLS
    # --------------------------------------------------------

    symbols = get_symbols()

    print(
        f"[INFO] selected symbols: "
        f"{len(symbols)}"
    )

    # --------------------------------------------------------
    # SCAN
    # --------------------------------------------------------

    all_signals = []

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
                    f"[WARN] worker error: {exc}",
                    file=sys.stderr
                )

            if done % 50 == 0:

                print(
                    f"[INFO] progress "
                    f"{done}/{len(symbols)}"
                )

    # --------------------------------------------------------
    # SORT
    # --------------------------------------------------------

    all_signals.sort(
        key=lambda x: (
            -x.score,
            x.symbol,
            x.direction
        )
    )

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    report = build_report(
        all_signals,
        len(symbols)
    )

    print(
        "\n" + report
    )

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    try:

        send_telegram(report)

    except Exception as exc:

        print(
            f"[WARN] Telegram failed: {exc}",
            file=sys.stderr
        )

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # FINISH
    # --------------------------------------------------------

    elapsed = time.time() - started

    print(
        f"\n[INFO] elapsed: "
        f"{elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
