#!/usr/bin/env python3

"""
Bitget 4H -> 15M Signal Scanner

PURPOSE
-------
Production-style signal scanner based on the validated v2.6 flow:

4H Liquidity Sweep
        ↓
4H CISD
        ↓
4H Recency
        ↓
15M Structure Break
        ↓
15M CURRENT CLOSED candle confirmation
        ↓
Telegram signal

IMPORTANT
---------
- Existing v2.6 scanner is NOT modified by this file.
- ONLY the latest CLOSED 15M candle can generate a new signal.
- Historical 15M structure breaks are NOT replayed as new signals.
- No trading orders are placed.
- Telegram receives ONLY newly confirmed signals.
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

# Keep v2.6 values
MAX_4H_EVENT_BARS = 8
MAX_SWEEP_TO_CISD_BARS = 8
MAX_4H_RECENCY_BARS = 8

LIQUIDITY_LOOKBACK = 8

MIN_CISD_BODY_RATIO = 0.30


# ============================================================
# 15M STRUCTURE
# ============================================================

# Keep v2.6 swing definition
SWING_LEFT = 2
SWING_RIGHT = 2

# Minimum distance between swing and current signal candle
MIN_STRUCTURE_DISTANCE = 3

# Minimum body ratio of the CURRENT signal candle
MIN_STRUCTURE_BODY_RATIO = 0.25


# ============================================================
# SIGNAL DE-DUPLICATION
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

def get_json(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    query = urlencode(params)
    url = f"{BASE_URL}{path}?{query}"

    last_err = None

    for attempt in range(REQUEST_RETRIES):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "bitget-4h15m-signal-scanner/1.0",
                    "Accept": "application/json",
                },
                method="GET",
            )

            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data.get("code") != "00000":
                raise RuntimeError(
                    f"Bitget API error: "
                    f"{data.get('code')} {data.get('msg')}"
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
            time.sleep(0.7 * (attempt + 1))

    raise RuntimeError(
        f"Request failed: {path} {params} :: {last_err}"
    )


# ============================================================
# SYMBOLS
# ============================================================

def get_symbols() -> List[str]:
    data = get_json(
        "/api/v3/market/instruments",
        {"category": PRODUCT_TYPE},
    )

    symbols = []

    for item in data.get("data", []):
        symbol = str(item.get("symbol", "")).strip()
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

    candles.sort(key=lambda x: x.ts)

    # --------------------------------------------------------
    # IMPORTANT
    # --------------------------------------------------------
    # Remove currently forming candle.
    # Only CLOSED candles remain.
    # --------------------------------------------------------

    now_ms = int(time.time() * 1000)

    interval_ms = {
        "4H": 4 * 60 * 60 * 1000,
        "15m": 15 * 60 * 1000,
    }[granularity]

    return [
        candle
        for candle in candles
        if candle.ts + interval_ms <= now_ms
    ]


# ============================================================
# HELPERS
# ============================================================

def bullish(candle: Candle) -> bool:
    return candle.c > candle.o


def bearish(candle: Candle) -> bool:
    return candle.c < candle.o


def body_ratio(candle: Candle) -> float:
    candle_range = max(candle.h - candle.l, 1e-12)

    return abs(candle.c - candle.o) / candle_range


# ============================================================
# 4H LIQUIDITY SWEEP
# ============================================================

def find_latest_sweep(
    candles: List[Candle],
    direction: str,
) -> Optional[Dict[str, Any]]:

    if len(candles) < LIQUIDITY_LOOKBACK + 2:
        return None

    end = len(candles) - 1

    start = max(
        LIQUIDITY_LOOKBACK,
        end - MAX_4H_EVENT_BARS + 1,
    )

    for i in range(end, start - 1, -1):

        current = candles[i]

        previous = candles[
            i - LIQUIDITY_LOOKBACK:i
        ]

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------

        if direction == "LONG":

            prior_low = min(
                candle.l for candle in previous
            )

            # Sweep below liquidity
            # Then close back above it
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

        # ----------------------------------------------------
        # SHORT
        # ----------------------------------------------------

        else:

            prior_high = max(
                candle.h for candle in previous
            )

            # Sweep above liquidity
            # Then close back below it
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

    for i in range(start, end + 1):

        current = candles[i]
        reference = candles[i - 1]

        # ----------------------------------------------------
        # LONG CISD
        # ----------------------------------------------------

        if direction == "LONG":

            if not bullish(current):
                continue

            if current.c <= reference.h:
                continue

            if body_ratio(current) < MIN_CISD_BODY_RATIO:
                continue

            return {
                "index": i,
                "ts": current.ts,
                "level": reference.h,
                "open": current.o,
                "close": current.c,
            }

        # ----------------------------------------------------
        # SHORT CISD
        # ----------------------------------------------------

        else:

            if not bearish(current):
                continue

            if current.c >= reference.l:
                continue

            if body_ratio(current) < MIN_CISD_BODY_RATIO:
                continue

            return {
                "index": i,
                "ts": current.ts,
                "level": reference.l,
                "open": current.o,
                "close": current.c,
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
        or idx + SWING_RIGHT >= len(candles)
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
        or idx + SWING_RIGHT >= len(candles)
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
# 15M CURRENT CLOSED CANDLE STRUCTURE BREAK
# ============================================================

def check_current_structure_break(
    candles: List[Candle],
    direction: str,
    start_index: int,
) -> Optional[Dict[str, Any]]:

    # --------------------------------------------------------
    # We ONLY evaluate the latest CLOSED 15M candle.
    # --------------------------------------------------------

    if len(candles) < SWING_LEFT + SWING_RIGHT + 5:
        return None

    current_index = len(candles) - 1

    current = candles[current_index]

    # --------------------------------------------------------
    # Current signal candle must be AFTER the 4H CISD.
    # --------------------------------------------------------

    if current_index <= start_index:
        return None

    # --------------------------------------------------------
    # Minimum distance:
    # current candle must be at least
    # MIN_STRUCTURE_DISTANCE candles away
    # from the swing.
    # --------------------------------------------------------

    latest_possible_swing = (
        current_index
        - SWING_RIGHT
        - 1
    )

    latest_possible_swing = min(
        latest_possible_swing,
        current_index - MIN_STRUCTURE_DISTANCE,
    )

    if latest_possible_swing < start_index:
        return None

    # --------------------------------------------------------
    # Current signal candle itself
    # --------------------------------------------------------

    if direction == "LONG":

        if not bullish(current):
            return None

        if body_ratio(current) < MIN_STRUCTURE_BODY_RATIO:
            return None

        # ----------------------------------------------------
        # Find the MOST RECENT confirmed swing high.
        # ----------------------------------------------------

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

            level = candles[swing_index].h

            # Current CLOSED candle must close above
            # the swing high.
            if current.c <= level:
                continue

            return {
                "index": current_index,
                "ts": current.ts,
                "level": level,
                "swing_ts": candles[swing_index].ts,
            }

    # --------------------------------------------------------
    # SHORT
    # --------------------------------------------------------

    else:

        if not bearish(current):
            return None

        if body_ratio(current) < MIN_STRUCTURE_BODY_RATIO:
            return None

        # ----------------------------------------------------
        # Find the MOST RECENT confirmed swing low.
        # ----------------------------------------------------

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

            level = candles[swing_index].l

            # Current CLOSED candle must close below
            # the swing low.
            if current.c >= level:
                continue

            return {
                "index": current_index,
                "ts": current.ts,
                "level": level,
                "swing_ts": candles[swing_index].ts,
            }

    return None


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_symbol(
    symbol: str,
) -> List[Dict[str, Any]]:

    signals: List[Dict[str, Any]] = []

    # --------------------------------------------------------
    # 4H DATA
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # LONG + SHORT
    # --------------------------------------------------------

    for direction in ("LONG", "SHORT"):

        # ----------------------------------------------------
        # 4H SWEEP
        # ----------------------------------------------------

        sweep = find_latest_sweep(
            c4,
            direction,
        )

        if not sweep:
            continue

        # ----------------------------------------------------
        # 4H CISD
        # ----------------------------------------------------

        cisd = find_cisd_after_sweep(
            c4,
            direction,
            sweep,
        )

        if not cisd:
            continue

        # ----------------------------------------------------
        # 4H RECENCY
        # ----------------------------------------------------

        recency = (
            len(c4)
            - 1
            - cisd["index"]
        )

        if recency > MAX_4H_RECENCY_BARS:
            continue

        # ----------------------------------------------------
        # 15M DATA
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Find first 15M candle AFTER 4H CISD.
        # ----------------------------------------------------

        start15 = next(
            (
                i
                for i, candle in enumerate(c15)
                if candle.ts > cisd["ts"]
            ),
            len(c15),
        )

        if start15 >= len(c15):
            continue

        # ----------------------------------------------------
        # IMPORTANT
        #
        # DO NOT search the previous 96 candles.
        #
        # Only the CURRENT CLOSED 15M candle is evaluated.
        # ----------------------------------------------------

        structure = check_current_structure_break(
            c15,
            direction,
            start15,
        )

        if not structure:
            continue

        # ----------------------------------------------------
        # Current signal candle
        # ----------------------------------------------------

        signal_candle = c15[
            structure["index"]
        ]

        # ----------------------------------------------------
        # Defensive CLOSED candle check
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Confirmed signal
        # ----------------------------------------------------

        signals.append(
            {
                "symbol": symbol,
                "direction": direction,

                "cisd_open": cisd["open"],

                "signal_open": signal_candle.o,
                "signal_close": signal_candle.c,

                "signal_ts": signal_candle.ts,

                "structure_level": structure["level"],

                "swing_ts": structure["swing_ts"],
            }
        )

    return signals


# ============================================================
# TELEGRAM
# ============================================================

def fmt_price(value: float) -> str:

    if value >= 1000:
        return f"{value:,.2f}"

    if value >= 1:
        return f"{value:,.4f}"

    return f"{value:.8f}"


def kst_time(ts: int) -> str:

    dt = datetime.fromtimestamp(
        ts / 1000,
        tz=timezone.utc,
    )

    dt = dt.astimezone(
        timezone(timedelta(hours=9))
    )

    return dt.strftime(
        "%H:%M KST"
    )


def make_message(
    signal: Dict[str, Any],
) -> str:

    icon = (
        "🟢"
        if signal["direction"] == "LONG"
        else "🔴"
    )

    return (
        f"{icon} {signal['direction']} SIGNAL\n\n"
        f"{signal['symbol']}\n\n"
        f"15M\n"
        f"CISD Break ✅\n\n"
        f"CISD 시가: "
        f"{fmt_price(signal['cisd_open'])}\n"
        f"봉마감 종가: "
        f"{fmt_price(signal['signal_close'])}\n\n"
        f"신호봉 마감\n"
        f"{kst_time(signal['signal_ts'])}"
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
            "[INFO] New Telegram secrets are not set."
        )
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": message,
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
        timeout=REQUEST_TIMEOUT,
    ) as resp:
        resp.read()


# ============================================================
# STATE / DUPLICATION
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

    # Keep state small.
    state["signals"] = (
        state.get("signals", [])[-500:]
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
        " Bitget 4H -> 15M Signal Scanner"
    )

    print(
        "============================================"
    )

    print(
        "4H SWEEP -> CISD -> RECENCY"
    )

    print(
        "15M CURRENT CLOSED CANDLE STRUCTURE BREAK"
    )

    print(
        "LONG + SHORT"
    )

    print(
        "NO ORDERS"
    )

    # --------------------------------------------------------
    # SYMBOLS
    # --------------------------------------------------------

    try:

        symbols = get_symbols()

    except Exception as exc:

        print(
            f"[FATAL] symbol loading failed: {exc}"
        )

        return

    print(
        f"[INFO] selected symbols: "
        f"{len(symbols)}"
    )

    # --------------------------------------------------------
    # SCAN
    # --------------------------------------------------------

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
                    f"[ERROR] "
                    f"{futures[future]}: "
                    f"{exc}"
                )

            if done % 50 == 0:

                print(
                    f"[INFO] progress "
                    f"{done}/{len(symbols)}"
                )

    # --------------------------------------------------------
    # DE-DUPLICATE SAME RUN
    # --------------------------------------------------------

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
            x["signal_ts"],
            x["symbol"],
            x["direction"],
        )
    )

    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    state = load_state()

    sent_keys = set(
        state.get(
            "signals",
            [],
        )
    )

    # --------------------------------------------------------
    # ONLY NEW SIGNALS
    # --------------------------------------------------------

    new_signals = [
        signal
        for signal in all_signals
        if signal_key(signal)
        not in sent_keys
    ]

    print(
        f"[INFO] confirmed signals: "
        f"{len(all_signals)}"
    )

    print(
        f"[INFO] new signals: "
        f"{len(new_signals)}"
    )

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    for signal in new_signals:

        message = make_message(
            signal
        )

        print(
            "\n" + message + "\n"
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
                "[WARN] Telegram failed: "
                f"{exc}"
            )

    # --------------------------------------------------------
    # SAVE STATE
    # --------------------------------------------------------

    save_state(
        state
    )

    print(
        "\n[INFO] scan finished."
    )


if __name__ == "__main__":
    main()
