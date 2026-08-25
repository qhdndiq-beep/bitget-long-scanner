#!/usr/bin/env python3

"""
Bitget 4H -> 15M Structure Scanner v1.3

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

IMPORTANT
---------
This is a heuristic scanner.
It is NOT an exact reproduction of TradingView/LuxAlgo CISD.

v1.3 focuses on reducing false positives.

Universe:
- Bitget USDT-FUTURES
- Crypto only
- Perpetual only
- USDT quoted
- Online
- RWA excluded

Signal requirements:
LONG
1. Recent 4H bullish CISD
2. 15M bullish structure break after 4H CISD
3. Actual 15M pullback after structure break
4. Pullback must interact with structure level / zone
5. 15M bullish confirmation after pullback

SHORT
1. Recent 4H bearish CISD
2. 15M bearish structure break after 4H CISD
3. Actual 15M pullback after structure break
4. Pullback must interact with structure level / zone
5. 15M bearish confirmation after pullback

No orders are placed.
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
HISTORY_LIMIT_15M = 300

MAX_WORKERS = 8

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3

# ------------------------------------------------------------
# 4H CISD freshness
# ------------------------------------------------------------

MAX_4H_CISD_BARS = 8
# 8 x 4H = maximum ~32 hours old

# ------------------------------------------------------------
# 15M structure window
# ------------------------------------------------------------

MAX_15M_STRUCTURE_BARS = 96
# ~24 hours

# ------------------------------------------------------------
# Pullback window after structure
# ------------------------------------------------------------

MAX_PULLBACK_BARS = 32
# ~8 hours

# ------------------------------------------------------------
# Confirmation window after pullback
# ------------------------------------------------------------

MAX_CONFIRMATION_BARS = 16
# ~4 hours

# ------------------------------------------------------------
# Minimum retracement
# ------------------------------------------------------------

MIN_PULLBACK_PCT = 0.0005
# 0.05%

# ------------------------------------------------------------
# Zone tolerance
# ------------------------------------------------------------

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
                        "bitget-4h15m-structure-scanner/1.3",
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

    # Remove incomplete candle.
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
        / candle_range(c)
    )


def local_low(
    candles: List[Candle],
    idx: int,
    radius: int = 2
) -> bool:

    lo = max(
        0,
        idx - radius
    )

    hi = min(
        len(candles),
        idx + radius + 1
    )

    return candles[idx].l <= min(
        x.l
        for x in candles[lo:hi]
    )


def local_high(
    candles: List[Candle],
    idx: int,
    radius: int = 2
) -> bool:

    lo = max(
        0,
        idx - radius
    )

    hi = min(
        len(candles),
        idx + radius + 1
    )

    return candles[idx].h >= max(
        x.h
        for x in candles[lo:hi]
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
        # LONG CISD
        # ----------------------------------------------------

        if direction == "LONG":

            if not (
                bullish(cur)
                and bearish(ref)
            ):
                continue

            # Body close must break reference HIGH.
            if cur.c <= ref.h:
                continue

            # Avoid extremely weak candle.
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

        # ----------------------------------------------------
        # SHORT CISD
        # ----------------------------------------------------

        else:

            if not (
                bearish(cur)
                and bullish(ref)
            ):
                continue

            # Body close must break reference LOW.
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

        if direction == "LONG":

            if a.h < c.l:

                zones.append(
                    Zone(
                        kind="FVG",
                        direction=direction,
                        low=a.h,
                        high=c.l,
                        ts=c.ts,
                        age_bars=
                            len(candles)
                            - 1
                            - i,
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
                        age_bars=
                            len(candles)
                            - 1
                            - i,
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
                    age_bars=
                        len(candles)
                        - 1
                        - i,
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
                    age_bars=
                        len(candles)
                        - 1
                        - i,
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

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------

        if direction == "LONG":

            if not (
                bullish(cur)
                and bearish(ref)
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

            if body_ratio(cur) < 0.30:
                continue

            return {
                "index": i,
                "ts": cur.ts,
                "level": ref.l,
            }

    return None


# ============================================================
# REAL PULLBACK + CONFIRMATION
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

    pullback_index = None

    # --------------------------------------------------------
    # STEP A
    # REAL PULLBACK
    # --------------------------------------------------------

    for i in range(
        pull_start,
        pull_end + 1
    ):

        c = candles[i]

        # LONG:
        # Price must actually return toward
        # the broken structure level.
        if direction == "LONG":

            retrace = (
                structure_level - c.l
            )

            if (
                c.l <= structure_level
                and
                c.c >= structure_level
                and
                retrace
                >= structure_level
                * MIN_PULLBACK_PCT
            ):

                pullback_index = i
                break

        # SHORT:
        # Price returns upward toward
        # broken structure level.
        else:

            retrace = (
                c.h - structure_level
            )

            if (
                c.h >= structure_level
                and
                c.c <= structure_level
                and
                retrace
                >= structure_level
                * MIN_PULLBACK_PCT
            ):

                pullback_index = i
                break

    if pullback_index is None:
        return None

    # --------------------------------------------------------
    # STEP B
    # CONFIRMATION AFTER PULLBACK
    # --------------------------------------------------------

    confirm_start = pullback_index + 1

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
        # LONG CONFIRMATION
        # ----------------------------------------------------

        if direction == "LONG":

            if not bullish(cur):
                continue

            # Must close above previous candle high.
            if cur.c <= prev.h:
                continue

            if body_ratio(cur) < 0.30:
                continue

            return {
                "pullback_index":
                    pullback_index,

                "pullback_ts":
                    candles[pullback_index].ts,

                "confirmation_index":
                    i,

                "confirmation_ts":
                    cur.ts,
            }

        # ----------------------------------------------------
        # SHORT CONFIRMATION
        # ----------------------------------------------------

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
                    candles[pullback_index].ts,

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
) -> tuple[
    Optional[float],
    Optional[float],
    bool,
    bool
]:

    structure_index = structure["index"]

    pullback_index = (
        pullback_confirmation[
            "pullback_index"
        ]
    )

    # Search zones created between structure
    # and confirmation.

    start = structure_index

    end = (
        pullback_confirmation[
            "confirmation_index"
        ]
        + 1
    )

    relevant = candles[
        start:end
    ]

    if len(relevant) < 3:
        return None, None, False, False

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
                        x.low
                        + x.high
                    ) / 2
                )
        )[0]

        return (
            z.low,
            z.high,
            fvg_hit,
            ob_hit
        )

    # If no FVG/OB, use pullback candle range.
    return (
        pullback.l,
        pullback.h,
        False,
        False
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
            or len(c15) < 100
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
                    for i, c in enumerate(c15)
                    if c.ts > cisd4["ts"]
                ),
                len(c15)
            )

            if start15 >= len(c15) - 10:
                continue

            # =================================================
            # 3. 15M STRUCTURE BREAK
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
            # 4. REAL PULLBACK + CONFIRMATION
            # =================================================

            pc = find_pullback_and_confirmation(
                c15,
                direction,
                structure
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
            # 6. 4H FVG / OB INFORMATION
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

            fvg4 = len(fvg4_zones) > 0

            ob4 = len(ob4_zones) > 0

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

                    cisd_ts=cisd4["ts"],
                    cisd_level=cisd4["level"],

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

        f"15M confirmation "
        f"{'✓' if s.confirmation_body_close else '-'} "
        f"| FVG "
        f"{'✓' if s.fvg_15m else '-'} "
        f"| OB "
        f"{'✓' if s.ob_15m else '-'}\n"

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
            "🔎 4H→15M Structure Scanner\n"
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
            "🔎 4H→15M Structure Scanner\n"
            f"{now}\n"
            f"스캔 {symbol_count}개\n"
            f"🔥 유효 후보 {len(signals)}개"
        )
    ]

    # Telegram noise reduction.
    # Only top 10 candidates.
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
        " Bitget 4H -> 15M Structure Scanner v1.3\n"
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
