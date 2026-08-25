#!/usr/bin/env python3

"""
Bitget 4H -> 15M Structure Scanner v2.1

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
15M STRUCTURE CANDIDATE

v2.1 PURPOSE
-------------
v2.0에서 검증된 4H Sweep -> CISD -> Recency 이벤트를
15M 실제 Swing Structure Break와 연결한다.

IMPORTANT
---------
이번 버전에서는 Pullback / FVG / OB / Confirmation을
아직 최종 조건에 넣지 않는다.

목적:
"4H에서 만들어진 방향성이 15M 구조까지 실제로 연결되는가?"
를 확인하는 것.

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
HISTORY_LIMIT_15M = 300

MAX_WORKERS = 8

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3


# ============================================================
# 4H EVENT
# ============================================================

MAX_4H_EVENT_BARS = 8

# Sweep 이후 CISD가 발생할 수 있는 최대 거리
MAX_SWEEP_TO_CISD_BARS = 8

# 최근성
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

MAX_15M_STRUCTURE_BARS = 96

MIN_STRUCTURE_DISTANCE = 3

MIN_STRUCTURE_BODY_RATIO = 0.25


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
class Event:
    symbol: str
    direction: str

    sweep_ts: int
    sweep_level: float

    cisd_ts: int
    cisd_level: float

    recency_bars: int


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
                        "bitget-4h15m-structure-scanner/2.1",
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
# SYMBOLS
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
        x for x in candles
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
        /
        candle_range
    )


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

        # ----------------------------------------------------
        # LONG
        #
        # Sell-side liquidity sweep
        # Previous lows taken
        # Then candle closes back above
        # ----------------------------------------------------

        if direction == "LONG":

            prior_low = min(
                c.l
                for c in previous
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

        # ----------------------------------------------------
        # SHORT
        #
        # Buy-side liquidity sweep
        # Previous highs taken
        # Then candle closes back below
        # ----------------------------------------------------

        else:

            prior_high = max(
                c.h
                for c in previous
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
# 4H CISD AFTER SWEEP
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

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # SHORT
        # ----------------------------------------------------

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
# 15M SWING
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

    # --------------------------------------------------------
    # LONG
    # --------------------------------------------------------

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

            # Find newest confirmed swing high
            # BEFORE the breakout candle.
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

                # BODY CLOSE above structure.
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

    # --------------------------------------------------------
    # SHORT
    # --------------------------------------------------------

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

                # BODY CLOSE below structure.
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
# SYMBOL ANALYSIS
# ============================================================

def analyze_symbol(
    symbol: str
) -> Dict[str, Any]:

    diagnostic = {
        "long_event": False,
        "short_event": False,

        "long_structure": False,
        "short_structure": False,

        "long_candidate": None,
        "short_candidate": None,
    }

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

            return diagnostic

        # ====================================================
        # LONG
        # ====================================================

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

                    diagnostic[
                        "long_event"
                    ] = True

                    # Find first 15M candle
                    # AFTER the 4H CISD.
                    start15 = next(
                        (
                            i
                            for i, c
                            in enumerate(c15)
                            if c.ts >
                            cisd_long["ts"]
                        ),
                        len(c15)
                    )

                    if (
                        start15
                        <
                        len(c15) - 10
                    ):

                        structure = (
                            find_structure_break(
                                c15,
                                "LONG",
                                start15
                            )
                        )

                        if structure:

                            diagnostic[
                                "long_structure"
                            ] = True

                            diagnostic[
                                "long_candidate"
                            ] = StructureCandidate(
                                symbol=symbol,
                                direction="LONG",

                                sweep_ts=
                                    sweep_long["ts"],

                                sweep_level=
                                    sweep_long["level"],

                                cisd_ts=
                                    cisd_long["ts"],

                                cisd_level=
                                    cisd_long["level"],

                                structure_ts=
                                    structure["ts"],

                                structure_level=
                                    structure["level"],

                                swing_ts=
                                    structure["swing_ts"],

                                current_price=
                                    c15[-1].c,
                            )

        # ====================================================
        # SHORT
        # ====================================================

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

                    diagnostic[
                        "short_event"
                    ] = True

                    start15 = next(
                        (
                            i
                            for i, c in enumerate(c15)
                            if c.ts >
                            cisd_short["ts"]
                        ),
                        len(c15)
                    )

                    if (
                        start15
                        <
                        len(c15) - 10
                    ):

                        structure = (
                            find_structure_break(
                                c15,
                                "SHORT",
                                start15
                            )
                        )

                        if structure:

                            diagnostic[
                                "short_structure"
                            ] = True

                            diagnostic[
                                "short_candidate"
                            ] = StructureCandidate(
                                symbol=symbol,
                                direction="SHORT",

                                sweep_ts=
                                    sweep_short["ts"],

                                sweep_level=
                                    sweep_short["level"],

                                cisd_ts=
                                    cisd_short["ts"],

                                cisd_level=
                                    cisd_short["level"],

                                structure_ts=
                                    structure["ts"],

                                structure_level=
                                    structure["level"],

                                swing_ts=
                                    structure["swing_ts"],

                                current_price=
                                    c15[-1].c,
                            )

        return diagnostic

    except Exception as exc:

        diagnostic["error"] = str(exc)

        return diagnostic


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


def format_candidate(
    c: StructureCandidate
) -> str:

    icon = (
        "🟢"
        if c.direction == "LONG"
        else "🔴"
    )

    return (
        f"{icon} {c.symbol} "
        f"{c.direction}\n"

        f"4H Sweep: "
        f"{fmt_price(c.sweep_level)} "
        f"({short_ts(c.sweep_ts)})\n"

        f"4H CISD: "
        f"{fmt_price(c.cisd_level)} "
        f"({short_ts(c.cisd_ts)})\n"

        f"15M Structure: "
        f"{fmt_price(c.structure_level)} "
        f"({short_ts(c.structure_ts)})\n"

        f"15M Swing: "
        f"{short_ts(c.swing_ts)}\n"

        f"현재가: "
        f"{fmt_price(c.current_price)}"
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

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    lines = [

        "🔎 "
        "4H→15M Structure Scanner v2.1",

        now,

        "",

        f"스캔 {symbol_count}개",

        "",

        "━━━━━━━━━━━━━━━━━━━━━━",

        "🔬 v2.1 DIAGNOSTIC",

        "━━━━━━━━━━━━━━━━━━━━━━",

        "① 4H FINAL EVENT",

        f"   └ LONG  : "
        f"{len(long_events)}",

        f"   └ SHORT : "
        f"{len(short_events)}",

        "",

        "② 15M Structure",

        f"   └ LONG  : "
        f"{len(long_structure)}",

        f"   └ SHORT : "
        f"{len(short_structure)}",

        "",

        f"⚠️ API/분석 오류 : "
        f"{errors}",

        "",

        f"🔥 FINAL 4H→15M STRUCTURE "
        f": {len(candidates)}개",

        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # --------------------------------------------------------
    # CANDIDATES
    # --------------------------------------------------------

    if candidates:

        lines += [
            "",
            "🔥 STRUCTURE CANDIDATES",
            ""
        ]

        for candidate in sorted(
            candidates,
            key=lambda x: (
                x.symbol,
                x.direction
            )
        )[:20]:

            lines.append(
                format_candidate(
                    candidate
                )
            )

            lines.append(
                "────────────────"
            )

    else:

        lines += [
            "",
            "🔥 유효 15M Structure 후보 없음",
            "",
            "다음 단계:",
            "4H 이벤트 → 15M Structure "
            "연결 검증 필요",
        ]

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    started = time.time()

    print(
        "\n"
        "============================================\n"
        " Bitget 4H -> 15M Structure Scanner v2.1\n"
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

    diagnostics: List[
        Dict[str, Any]
    ] = []

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

            try:

                result = future.result()

                diagnostics.append(
                    result
                )

                if result.get("error"):
                    errors += 1

            except Exception as exc:

                errors += 1

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
