#!/usr/bin/env python3

"""
Bitget 4H -> 15M CISD Scanner v2.0

CORE IDEA
---------
4H LIQUIDITY SWEEP
        ↓
4H CISD
        ↓
15M STRUCTURE
        ↓
15M PULLBACK
        ↓
15M ZONE
        ↓
15M CONFIRMATION

v2.0 CHANGE
-----------
v1.9 calculated:

    Sweep
    CISD

independently.

That created too many false CISD candidates.

v2.0 requires the correct chronological sequence:

LONG
----
1. Confirmed 4H swing low
2. Price sweeps below that swing low
3. Sweep candle closes back above the swing low
4. A later bullish CISD occurs
5. CISD candle body closes above the latest bearish reference candle high

SHORT
-----
1. Confirmed 4H swing high
2. Price sweeps above that swing high
3. Sweep candle closes back below the swing high
4. A later bearish CISD occurs
5. CISD candle body closes below the latest bullish reference candle low

IMPORTANT
---------
This version is still diagnostic.

15M logic is NOT executed yet.

The purpose is to validate the 4H event definition first.

No trading orders are placed.

This is a heuristic scanner.
It is NOT an exact reproduction of TradingView/LuxAlgo CISD.
"""

from __future__ import annotations

import json
import os
import sys
import time

from concurrent.futures import ThreadPoolExecutor, as_completed
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

MAX_WORKERS = 8

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3


# ============================================================
# 4H EVENT CONFIG
# ============================================================

# Search recent candles only.
MAX_EVENT_LOOKBACK = 20

# Sweep must happen within this many candles
# before the CISD.
MAX_CISD_AFTER_SWEEP = 6

# Maximum age of final event.
MAX_FINAL_EVENT_AGE = 8


# ============================================================
# SWING CONFIG
# ============================================================

SWING_LEFT = 2
SWING_RIGHT = 2

SWING_LOOKBACK = 30


# ============================================================
# BODY CONFIG
# ============================================================

MIN_SWEEP_BODY_RATIO = 0.10

MIN_CISD_BODY_RATIO = 0.30


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

    for attempt in range(
        REQUEST_RETRIES
    ):

        try:

            req = Request(
                url,
                headers={
                    "User-Agent":
                        "bitget-4h-15m-cisd-scanner/2.0",

                    "Accept":
                        "application/json",
                },
                method="GET",
            )

            with urlopen(
                req,
                timeout=REQUEST_TIMEOUT,
            ) as resp:

                body = (
                    resp
                    .read()
                    .decode("utf-8")
                )

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
            "category": PRODUCT_TYPE,
        },
    )

    symbols = []

    total = 0
    excluded = 0

    for item in data.get(
        "data",
        [],
    ):

        total += 1

        symbol = str(
            item.get(
                "symbol",
                "",
            )
        ).strip()

        quote_coin = str(
            item.get(
                "quoteCoin",
                "",
            )
        ).upper().strip()

        symbol_type = str(
            item.get(
                "symbolType",
                "",
            )
        ).lower().strip()

        contract_type = str(
            item.get(
                "type",
                "",
            )
        ).lower().strip()

        status = str(
            item.get(
                "status",
                "",
            )
        ).lower().strip()

        is_rwa = str(
            item.get(
                "isRwa",
                "YES",
            )
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

    symbols = sorted(
        set(symbols)
    )

    print(
        f"[INFO] instruments: {total} | "
        f"selected: {len(symbols)} | "
        f"excluded: {excluded}"
    )

    return symbols


# ============================================================
# 4H CANDLES
# ============================================================

def get_4h_candles(
    symbol: str,
) -> List[Candle]:

    data = get_json(
        "/api/v2/mix/market/history-candles",
        {
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "granularity": "4H",
            "limit": HISTORY_LIMIT_4H,
        },
    )

    candles = []

    for row in data.get(
        "data",
        [],
    ):

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
    # Remove incomplete current candle.
    # --------------------------------------------------------

    now_ms = int(
        time.time() * 1000
    )

    interval_ms = (
        4
        * 60
        * 60
        * 1000
    )

    candles = [
        c
        for c in candles
        if c.ts + interval_ms <= now_ms
    ]

    return candles


# ============================================================
# BASIC HELPERS
# ============================================================

def bullish(
    c: Candle,
) -> bool:

    return c.c > c.o


def bearish(
    c: Candle,
) -> bool:

    return c.c < c.o


def body_size(
    c: Candle,
) -> float:

    return abs(
        c.c - c.o
    )


def candle_range(
    c: Candle,
) -> float:

    return max(
        c.h - c.l,
        1e-12,
    )


def body_ratio(
    c: Candle,
) -> float:

    return (
        body_size(c)
        /
        candle_range(c)
    )


# ============================================================
# SWING HIGH / LOW
# ============================================================

def is_swing_low(
    candles: List[Candle],
    index: int,
) -> bool:

    if index < SWING_LEFT:
        return False

    if (
        index + SWING_RIGHT
        >= len(candles)
    ):
        return False

    current = candles[index]

    left = candles[
        index - SWING_LEFT:
        index
    ]

    right = candles[
        index + 1:
        index + SWING_RIGHT + 1
    ]

    return (
        current.l
        <= min(
            x.l for x in left
        )
        and
        current.l
        <= min(
            x.l for x in right
        )
    )


def is_swing_high(
    candles: List[Candle],
    index: int,
) -> bool:

    if index < SWING_LEFT:
        return False

    if (
        index + SWING_RIGHT
        >= len(candles)
    ):
        return False

    current = candles[index]

    left = candles[
        index - SWING_LEFT:
        index
    ]

    right = candles[
        index + 1:
        index + SWING_RIGHT + 1
    ]

    return (
        current.h
        >= max(
            x.h for x in left
        )
        and
        current.h
        >= max(
            x.h for x in right
        )
    )


# ============================================================
# RECENT SWING LOW
# ============================================================

def find_recent_swing_low(
    candles: List[Candle],
    before_index: int,
) -> Optional[Dict[str, Any]]:

    start = max(
        SWING_LEFT,
        before_index
        - SWING_LOOKBACK,
    )

    end = min(
        before_index
        - SWING_RIGHT
        - 1,
        len(candles)
        - SWING_RIGHT
        - 1,
    )

    if end < start:
        return None

    for i in range(
        end,
        start - 1,
        -1,
    ):

        if is_swing_low(
            candles,
            i,
        ):

            return {
                "index": i,
                "ts": candles[i].ts,
                "level": candles[i].l,
            }

    return None


# ============================================================
# RECENT SWING HIGH
# ============================================================

def find_recent_swing_high(
    candles: List[Candle],
    before_index: int,
) -> Optional[Dict[str, Any]]:

    start = max(
        SWING_LEFT,
        before_index
        - SWING_LOOKBACK,
    )

    end = min(
        before_index
        - SWING_RIGHT
        - 1,
        len(candles)
        - SWING_RIGHT
        - 1,
    )

    if end < start:
        return None

    for i in range(
        end,
        start - 1,
        -1,
    ):

        if is_swing_high(
            candles,
            i,
        ):

            return {
                "index": i,
                "ts": candles[i].ts,
                "level": candles[i].h,
            }

    return None


# ============================================================
# LONG SWEEP
# ============================================================

def find_long_sweep(
    candles: List[Candle],
) -> Optional[Dict[str, Any]]:

    """
    LONG:

        confirmed swing low
              ↓
        price trades below it
              ↓
        candle closes back above it
    """

    end = len(candles) - 1

    start = max(
        SWING_LEFT + SWING_RIGHT + 1,
        end - MAX_EVENT_LOOKBACK,
    )

    for sweep_index in range(
        end,
        start - 1,
        -1,
    ):

        sweep = candles[
            sweep_index
        ]

        if (
            body_ratio(sweep)
            < MIN_SWEEP_BODY_RATIO
        ):
            continue

        swing = (
            find_recent_swing_low(
                candles,
                sweep_index,
            )
        )

        if not swing:
            continue

        level = swing[
            "level"
        ]

        # Must actually sweep the swing low.
        if sweep.l >= level:
            continue

        # Must reclaim the level.
        if sweep.c <= level:
            continue

        return {
            "index": sweep_index,
            "ts": sweep.ts,
            "level": level,
            "swing_index":
                swing["index"],
            "swing_ts":
                swing["ts"],
        }

    return None


# ============================================================
# SHORT SWEEP
# ============================================================

def find_short_sweep(
    candles: List[Candle],
) -> Optional[Dict[str, Any]]:

    """
    SHORT:

        confirmed swing high
              ↓
        price trades above it
              ↓
        candle closes back below it
    """

    end = len(candles) - 1

    start = max(
        SWING_LEFT + SWING_RIGHT + 1,
        end - MAX_EVENT_LOOKBACK,
    )

    for sweep_index in range(
        end,
        start - 1,
        -1,
    ):

        sweep = candles[
            sweep_index
        ]

        if (
            body_ratio(sweep)
            < MIN_SWEEP_BODY_RATIO
        ):
            continue

        swing = (
            find_recent_swing_high(
                candles,
                sweep_index,
            )
        )

        if not swing:
            continue

        level = swing[
            "level"
        ]

        # Must actually sweep the swing high.
        if sweep.h <= level:
            continue

        # Must reclaim below the level.
        if sweep.c >= level:
            continue

        return {
            "index": sweep_index,
            "ts": sweep.ts,
            "level": level,
            "swing_index":
                swing["index"],
            "swing_ts":
                swing["ts"],
        }

    return None


# ============================================================
# LONG CISD AFTER SWEEP
# ============================================================

def find_long_cisd_after_sweep(
    candles: List[Candle],
    sweep: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

    sweep_index = sweep[
        "index"
    ]

    end = min(
        len(candles) - 1,
        sweep_index
        + MAX_CISD_AFTER_SWEEP,
    )

    # CISD MUST happen after sweep.
    for cisd_index in range(
        sweep_index + 1,
        end + 1,
    ):

        current = candles[
            cisd_index
        ]

        # Need a bearish reference candle.
        reference = None

        for j in range(
            cisd_index - 1,
            sweep_index,
            -1,
        ):

            if bearish(
                candles[j]
            ):

                reference = candles[j]

                break

        if reference is None:
            continue

        # Bullish displacement.
        if not bullish(current):
            continue

        if (
            body_ratio(current)
            < MIN_CISD_BODY_RATIO
        ):
            continue

        # BODY CLOSE above bearish reference high.
        if current.c <= reference.h:
            continue

        return {
            "index": cisd_index,
            "ts": current.ts,
            "level": reference.h,
            "reference_ts":
                reference.ts,
        }

    return None


# ============================================================
# SHORT CISD AFTER SWEEP
# ============================================================

def find_short_cisd_after_sweep(
    candles: List[Candle],
    sweep: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

    sweep_index = sweep[
        "index"
    ]

    end = min(
        len(candles) - 1,
        sweep_index
        + MAX_CISD_AFTER_SWEEP,
    )

    # CISD MUST happen after sweep.
    for cisd_index in range(
        sweep_index + 1,
        end + 1,
    ):

        current = candles[
            cisd_index
        ]

        # Need a bullish reference candle.
        reference = None

        for j in range(
            cisd_index - 1,
            sweep_index,
            -1,
        ):

            if bullish(
                candles[j]
            ):

                reference = candles[j]

                break

        if reference is None:
            continue

        # Bearish displacement.
        if not bearish(current):
            continue

        if (
            body_ratio(current)
            < MIN_CISD_BODY_RATIO
        ):
            continue

        # BODY CLOSE below bullish reference low.
        if current.c >= reference.l:
            continue

        return {
            "index": cisd_index,
            "ts": current.ts,
            "level": reference.l,
            "reference_ts":
                reference.ts,
        }

    return None


# ============================================================
# SYMBOL DIAGNOSTIC
# ============================================================

def diagnose_symbol(
    symbol: str,
) -> Dict[str, Any]:

    result = {

        "symbol": symbol,

        # Stage 1
        "sweep_long": False,
        "sweep_short": False,

        # Stage 2
        "sequence_long": False,
        "sequence_short": False,

        # Stage 3
        "cisd_long": False,
        "cisd_short": False,

        # Stage 4
        "recent_long": False,
        "recent_short": False,

        "sweep_long_data": None,
        "sweep_short_data": None,

        "cisd_long_data": None,
        "cisd_short_data": None,

        "error": None,
    }

    try:

        candles = get_4h_candles(
            symbol
        )

        if len(candles) < 50:
            return result

        # ====================================================
        # ① SWEEP
        # ====================================================

        long_sweep = (
            find_long_sweep(
                candles
            )
        )

        short_sweep = (
            find_short_sweep(
                candles
            )
        )

        if long_sweep:

            result[
                "sweep_long"
            ] = True

            result[
                "sweep_long_data"
            ] = long_sweep

        if short_sweep:

            result[
                "sweep_short"
            ] = True

            result[
                "sweep_short_data"
            ] = short_sweep

        # ====================================================
        # ② SWEEP → CISD SEQUENCE
        # ====================================================

        long_cisd = None

        if long_sweep:

            long_cisd = (
                find_long_cisd_after_sweep(
                    candles,
                    long_sweep,
                )
            )

            if long_cisd:

                result[
                    "sequence_long"
                ] = True

        short_cisd = None

        if short_sweep:

            short_cisd = (
                find_short_cisd_after_sweep(
                    candles,
                    short_sweep,
                )
            )

            if short_cisd:

                result[
                    "sequence_short"
                ] = True

        # ====================================================
        # ③ CISD
        # ====================================================

        if long_cisd:

            result[
                "cisd_long"
            ] = True

            result[
                "cisd_long_data"
            ] = long_cisd

        if short_cisd:

            result[
                "cisd_short"
            ] = True

            result[
                "cisd_short_data"
            ] = short_cisd

        # ====================================================
        # ④ RECENTNESS
        # ====================================================

        latest_index = (
            len(candles) - 1
        )

        if long_cisd:

            age = (
                latest_index
                -
                long_cisd["index"]
            )

            if age <= MAX_FINAL_EVENT_AGE:

                result[
                    "recent_long"
                ] = True

        if short_cisd:

            age = (
                latest_index
                -
                short_cisd["index"]
            )

            if age <= MAX_FINAL_EVENT_AGE:

                result[
                    "recent_short"
                ] = True

        return result

    except Exception as exc:

        result[
            "error"
        ] = str(exc)

        print(
            f"[WARN] {symbol}: {exc}",
            file=sys.stderr,
        )

        return result


# ============================================================
# REPORT
# ============================================================

def build_report(
    diagnostics: List[Dict[str, Any]],
    symbol_count: int,
) -> str:

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    # ========================================================
    # COUNTS
    # ========================================================

    sweep_long = sum(
        1
        for x in diagnostics
        if x["sweep_long"]
    )

    sweep_short = sum(
        1
        for x in diagnostics
        if x["sweep_short"]
    )

    sequence_long = sum(
        1
        for x in diagnostics
        if x["sequence_long"]
    )

    sequence_short = sum(
        1
        for x in diagnostics
        if x["sequence_short"]
    )

    cisd_long = sum(
        1
        for x in diagnostics
        if x["cisd_long"]
    )

    cisd_short = sum(
        1
        for x in diagnostics
        if x["cisd_short"]
    )

    recent_long = sum(
        1
        for x in diagnostics
        if x["recent_long"]
    )

    recent_short = sum(
        1
        for x in diagnostics
        if x["recent_short"]
    )

    errors = sum(
        1
        for x in diagnostics
        if x["error"]
    )

    # ========================================================
    # FINAL
    # ========================================================

    final_long = [
        x
        for x in diagnostics
        if x["recent_long"]
    ]

    final_short = [
        x
        for x in diagnostics
        if x["recent_short"]
    ]

    # ========================================================
    # REPORT
    # ========================================================

    lines = []

    lines.append(
        "🔎 "
        "4H→15M Structure Scanner v2.0"
    )

    lines.append(
        now
    )

    lines.append("")

    lines.append(
        f"스캔 {symbol_count}개"
    )

    lines.append("")

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🔬 v2.0 4H EVENT DIAGNOSTIC"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append("")

    # --------------------------------------------------------
    # ①
    # --------------------------------------------------------

    lines.append(
        "① 4H Liquidity Sweep"
    )

    lines.append(
        f"   └ LONG  : {sweep_long}"
    )

    lines.append(
        f"   └ SHORT : {sweep_short}"
    )

    lines.append("")

    # --------------------------------------------------------
    # ②
    # --------------------------------------------------------

    lines.append(
        "② Sweep → CISD 순서 성립"
    )

    lines.append(
        f"   └ LONG  : {sequence_long}"
    )

    lines.append(
        f"   └ SHORT : {sequence_short}"
    )

    lines.append("")

    # --------------------------------------------------------
    # ③
    # --------------------------------------------------------

    lines.append(
        "③ 4H CISD"
    )

    lines.append(
        f"   └ LONG  : {cisd_long}"
    )

    lines.append(
        f"   └ SHORT : {cisd_short}"
    )

    lines.append("")

    # --------------------------------------------------------
    # ④
    # --------------------------------------------------------

    lines.append(
        "④ 최근성 조건 통과"
    )

    lines.append(
        f"   └ LONG  : {recent_long}"
    )

    lines.append(
        f"   └ SHORT : {recent_short}"
    )

    lines.append("")

    lines.append(
        f"⚠️ API/분석 오류 : {errors}"
    )

    lines.append("")

    # ========================================================
    # FINAL SYMBOLS
    # ========================================================

    if final_long:

        lines.append(
            "🟢 FINAL 4H LONG EVENT"
        )

        for x in final_long[:10]:

            sweep = (
                x["sweep_long_data"]
            )

            cisd = (
                x["cisd_long_data"]
            )

            lines.append(
                f"   {x['symbol']} | "
                f"Sweep {sweep['level']} | "
                f"CISD {cisd['level']}"
            )

        lines.append("")

    if final_short:

        lines.append(
            "🔴 FINAL 4H SHORT EVENT"
        )

        for x in final_short[:10]:

            sweep = (
                x["sweep_short_data"]
            )

            cisd = (
                x["cisd_short_data"]
            )

            lines.append(
                f"   {x['symbol']} | "
                f"Sweep {sweep['level']} | "
                f"CISD {cisd['level']}"
            )

        lines.append("")

    # ========================================================
    # FINAL MESSAGE
    # ========================================================

    final_count = (
        len(final_long)
        +
        len(final_short)
    )

    if final_count == 0:

        lines.append(
            "🔥 최종 4H 이벤트 없음"
        )

    else:

        lines.append(
            f"🔥 최종 4H 이벤트 "
            f"{final_count}개"
        )

    lines.append("")

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "다음 단계:"
    )

    lines.append(
        "4H 이벤트 검증 후 "
        "15M Structure 연결"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    return "\n".join(lines)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(
    text: str,
) -> None:

    token = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        "",
    ).strip()

    chat_id = os.getenv(
        "TELEGRAM_CHAT_ID",
        "",
    ).strip()

    if (
        not token
        or
        not chat_id
    ):

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
        timeout=REQUEST_TIMEOUT,
    ) as resp:

        resp.read()


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    started = time.time()

    print(
        "\n"
        "============================================\n"
        " Bitget 4H -> 15M CISD Scanner v2.0\n"
        "============================================\n"
    )

    print(
        "DIAGNOSTIC MODE"
    )

    print(
        "4H SWEEP -> "
        "4H CISD"
    )

    print(
        "15M logic is DISABLED."
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

    diagnostics = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                diagnose_symbol,
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

                result = (
                    future.result()
                )

                diagnostics.append(
                    result
                )

            except Exception as exc:

                symbol = futures[
                    future
                ]

                print(
                    f"[WARN] worker error "
                    f"{symbol}: {exc}",
                    file=sys.stderr,
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
            file=sys.stderr,
        )

    # ========================================================
    # JSON
    # ========================================================

    with open(
        "cisd_v2_0_diagnostic.json",
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            diagnostics,
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
