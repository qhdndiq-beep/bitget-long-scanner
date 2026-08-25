#!/usr/bin/env python3

"""
Bitget 4H -> 15M CISD Scanner v1.9

PURPOSE
-------
v1.9 is a DIAGNOSTIC VERSION.

The previous versions returned:

4H Directional Event = 0
4H Liquidity Sweep   = 0
4H CISD              = 0

So v1.9 temporarily isolates the 4H logic.

It does NOT attempt to generate final trading candidates.

The purpose is to determine exactly where 4H candidates disappear.

DIAGNOSTIC FLOW
---------------

465 symbols
    ↓
① 4H directional event
    ↓
② 4H liquidity sweep
    ↓
③ 4H CISD

LONG and SHORT are counted separately.

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

# ------------------------------------------------------------
# 4H diagnostic window
# ------------------------------------------------------------

MAX_4H_LOOKBACK = 20

# ------------------------------------------------------------
# Body quality
# ------------------------------------------------------------

MIN_DIRECTION_BODY_RATIO = 0.15
MIN_CISD_BODY_RATIO = 0.20

# ------------------------------------------------------------
# Liquidity sweep
# ------------------------------------------------------------

SWEEP_LOOKBACK = 8


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
                        "bitget-4h-15m-cisd-scanner/1.9",
                    "Accept":
                        "application/json",
                },
                method="GET",
            )

            with urlopen(
                req,
                timeout=REQUEST_TIMEOUT,
            ) as resp:

                body = resp.read().decode(
                    "utf-8"
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
# SYMBOLS
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
# CANDLES
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

    # Remove incomplete current candle.
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
# ① DIRECTIONAL EVENT
# ============================================================

def find_directional_events(
    candles: List[Candle],
) -> Dict[str, Optional[Dict[str, Any]]]:

    """
    Very broad first-stage filter.

    LONG:
        Recent bullish 4H candle.

    SHORT:
        Recent bearish 4H candle.

    This stage is intentionally loose.

    We want to know whether the scanner is
    seeing normal directional movement at all.
    """

    result = {
        "LONG": None,
        "SHORT": None,
    }

    if len(candles) < 5:
        return result

    end = len(candles) - 1

    start = max(
        1,
        end - MAX_4H_LOOKBACK + 1,
    )

    for i in range(
        end,
        start - 1,
        -1,
    ):

        c = candles[i]

        ratio = body_ratio(c)

        if (
            bullish(c)
            and
            ratio >= MIN_DIRECTION_BODY_RATIO
            and
            result["LONG"] is None
        ):

            result["LONG"] = {
                "index": i,
                "ts": c.ts,
                "level": c.h,
            }

        if (
            bearish(c)
            and
            ratio >= MIN_DIRECTION_BODY_RATIO
            and
            result["SHORT"] is None
        ):

            result["SHORT"] = {
                "index": i,
                "ts": c.ts,
                "level": c.l,
            }

        if (
            result["LONG"] is not None
            and
            result["SHORT"] is not None
        ):

            break

    return result


# ============================================================
# ② LIQUIDITY SWEEP
# ============================================================

def find_liquidity_sweep(
    candles: List[Candle],
    direction: str,
) -> Optional[Dict[str, Any]]:

    """
    LONG sweep:

        Current candle takes a previous low
        and closes back above that low.

    SHORT sweep:

        Current candle takes a previous high
        and closes back below that high.

    This is deliberately broad in v1.9.
    """

    if len(candles) < 12:
        return None

    end = len(candles) - 1

    start = max(
        SWEEP_LOOKBACK + 1,
        end - MAX_4H_LOOKBACK + 1,
    )

    for i in range(
        end,
        start - 1,
        -1,
    ):

        current = candles[i]

        previous_start = max(
            0,
            i - SWEEP_LOOKBACK,
        )

        previous = candles[
            previous_start:i
        ]

        if not previous:
            continue

        # ----------------------------------------------------
        # LONG
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
# ③ CISD
# ============================================================

def find_cisd(
    candles: List[Candle],
    direction: str,
) -> Optional[Dict[str, Any]]:

    """
    v1.9 CISD diagnostic.

    We deliberately separate:

        direction
        sweep
        CISD

    CISD definition used here:

    LONG:
        bullish candle body closes above
        the previous bearish candle high.

    SHORT:
        bearish candle body closes below
        the previous bullish candle low.

    This is still heuristic.
    """

    if len(candles) < 20:
        return None

    end = len(candles) - 1

    start = max(
        2,
        end - MAX_4H_LOOKBACK + 1,
    )

    for i in range(
        end,
        start - 1,
        -1,
    ):

        current = candles[i]
        previous = candles[i - 1]

        if (
            body_ratio(current)
            < MIN_CISD_BODY_RATIO
        ):
            continue

        # ----------------------------------------------------
        # LONG CISD
        # ----------------------------------------------------

        if direction == "LONG":

            if not bullish(current):
                continue

            if not bearish(previous):
                continue

            if current.c <= previous.h:
                continue

            return {
                "index": i,
                "ts": current.ts,
                "level": previous.h,
            }

        # ----------------------------------------------------
        # SHORT CISD
        # ----------------------------------------------------

        else:

            if not bearish(current):
                continue

            if not bullish(previous):
                continue

            if current.c >= previous.l:
                continue

            return {
                "index": i,
                "ts": current.ts,
                "level": previous.l,
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

        "direction_long": False,
        "direction_short": False,

        "sweep_long": False,
        "sweep_short": False,

        "cisd_long": False,
        "cisd_short": False,

        "direction_long_data": None,
        "direction_short_data": None,

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

        if len(candles) < 30:

            return result

        # ====================================================
        # ① DIRECTION
        # ====================================================

        directions = (
            find_directional_events(
                candles
            )
        )

        if directions["LONG"]:

            result[
                "direction_long"
            ] = True

            result[
                "direction_long_data"
            ] = directions["LONG"]

        if directions["SHORT"]:

            result[
                "direction_short"
            ] = True

            result[
                "direction_short_data"
            ] = directions["SHORT"]

        # ====================================================
        # ② SWEEP
        # ====================================================

        sweep_long = (
            find_liquidity_sweep(
                candles,
                "LONG",
            )
        )

        sweep_short = (
            find_liquidity_sweep(
                candles,
                "SHORT",
            )
        )

        if sweep_long:

            result[
                "sweep_long"
            ] = True

            result[
                "sweep_long_data"
            ] = sweep_long

        if sweep_short:

            result[
                "sweep_short"
            ] = True

            result[
                "sweep_short_data"
            ] = sweep_short

        # ====================================================
        # ③ CISD
        # ====================================================

        cisd_long = (
            find_cisd(
                candles,
                "LONG",
            )
        )

        cisd_short = (
            find_cisd(
                candles,
                "SHORT",
            )
        )

        if cisd_long:

            result[
                "cisd_long"
            ] = True

            result[
                "cisd_long_data"
            ] = cisd_long

        if cisd_short:

            result[
                "cisd_short"
            ] = True

            result[
                "cisd_short_data"
            ] = cisd_short

        return result

    except Exception as exc:

        result["error"] = str(exc)

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

    # --------------------------------------------------------
    # COUNTS
    # --------------------------------------------------------

    direction_long = sum(
        1
        for x in diagnostics
        if x["direction_long"]
    )

    direction_short = sum(
        1
        for x in diagnostics
        if x["direction_short"]
    )

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

    errors = sum(
        1
        for x in diagnostics
        if x["error"]
    )

    # --------------------------------------------------------
    # FINAL CISD
    # --------------------------------------------------------

    final_long = [
        x
        for x in diagnostics
        if x["cisd_long"]
    ]

    final_short = [
        x
        for x in diagnostics
        if x["cisd_short"]
    ]

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    lines = []

    lines.append(
        "🔎 "
        "4H→15M Structure Scanner v1.9"
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
        "🔬 v1.9 4H DIAGNOSTIC"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append("")

    lines.append(
        "① 4H 방향성 이벤트"
    )

    lines.append(
        f"   └ LONG  : {direction_long}"
    )

    lines.append(
        f"   └ SHORT : {direction_short}"
    )

    lines.append("")

    lines.append(
        "② 4H Liquidity Sweep"
    )

    lines.append(
        f"   └ LONG  : {sweep_long}"
    )

    lines.append(
        f"   └ SHORT : {sweep_short}"
    )

    lines.append("")

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

    lines.append(
        f"⚠️ API/분석 오류 : {errors}"
    )

    lines.append("")

    # --------------------------------------------------------
    # CISD SYMBOLS
    # --------------------------------------------------------

    if final_long:

        lines.append(
            "🟢 LONG CISD 후보"
        )

        for x in final_long[:10]:

            data = (
                x["cisd_long_data"]
            )

            lines.append(
                f"   {x['symbol']} | "
                f"level {data['level']}"
            )

        lines.append("")

    if final_short:

        lines.append(
            "🔴 SHORT CISD 후보"
        )

        for x in final_short[:10]:

            data = (
                x["cisd_short_data"]
            )

            lines.append(
                f"   {x['symbol']} | "
                f"level {data['level']}"
            )

        lines.append("")

    # --------------------------------------------------------
    # FINAL
    # --------------------------------------------------------

    if (
        cisd_long == 0
        and
        cisd_short == 0
    ):

        lines.append(
            "🔥 4H CISD 후보 없음"
        )

    else:

        lines.append(
            "🔥 4H CISD 후보 발견"
        )

    lines.append("")

    lines.append(
        "※ v1.9는 4H 진단 전용입니다."
    )

    lines.append(
        "※ 15M Structure/Pullback/"
        "Confirmation은 아직 실행하지 않습니다."
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
        " Bitget 4H -> 15M CISD Scanner v1.9\n"
        "============================================\n"
    )

    print(
        "DIAGNOSTIC MODE"
    )

    print(
        "4H Direction -> "
        "Liquidity Sweep -> "
        "CISD"
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
        "cisd_v1_9_diagnostic.json",
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
