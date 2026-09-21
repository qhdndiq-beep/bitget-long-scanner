#!/usr/bin/env python3
"""
Post-Signal Tracker for the Bitget 4H -> 15M Signal Scanner.

Purpose
-------
Track each confirmed scanner signal after it fires, using ONLY CLOSED 15M
candles. This module is research/measurement only; it does not place orders.

It records:
- immutable signal snapshot
- 15M checkpoints
- MFE / MAE in percent and ATR
- MA120 / MA200 relationship
- structure-level reclaim/loss
- reversal WATCH and CONFIRMED events
- first favorable/adverse thresholds
- final outcome after a fixed tracking horizon

Expected input
--------------
signal_snapshots.json produced by the scanner integration patch.

Output
------
post_signal_tracker.json
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ============================================================
# Bitget API
# ============================================================

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

SNAPSHOT_FILE = "signal_snapshots.json"
TRACKER_FILE = "post_signal_tracker.json"

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3

# Bitget v2 history-candles API maximum.
# Do not increase above 200.
HISTORY_LIMIT_15M = 200
MAX_HISTORY_CANDLES = 200


# ============================================================
# Tracking configuration
# ============================================================

TRACKING_HOURS = 24
TRACKING_BARS = TRACKING_HOURS * 4

# Research thresholds.
# These are intentionally fixed before data collection.
FAVORABLE_1_ATR = 1.0
FAVORABLE_1_5_ATR = 1.5
ADVERSE_1_ATR = 1.0


# ============================================================
# Reversal definition
# ============================================================
#
# Confirmed when two consecutive CLOSED 15M candles satisfy both:
#
#   1) price has reclaimed the original structure level
#   2) price has reclaimed the 15M SMA200
#
# The opposite-direction candle body is also recorded,
# but is not required for confirmation.
#
REVERSAL_CONSECUTIVE_BARS = 2


# ============================================================
# Bitget HTTP helper
# ============================================================

def get_json(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    GET JSON from Bitget with retry handling.

    Any Bitget API error is converted into RuntimeError so the caller
    can handle the failure per symbol without killing the entire tracker.
    """

    query = urlencode(params)
    url = f"{BASE_URL}{path}?{query}"

    last_err: Optional[Exception] = None

    for attempt in range(REQUEST_RETRIES):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "bitget-post-signal-tracker/1.0",
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

            # Retry with a small progressive delay.
            time.sleep(0.7 * (attempt + 1))

    raise RuntimeError(
        f"Request failed: {path} {params} :: {last_err}"
    )


# ============================================================
# Candle model
# ============================================================

class Candle:
    __slots__ = ("ts", "o", "h", "l", "c", "v")

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
# Candle loading
# ============================================================

def get_candles(
    symbol: str,
    limit: int = HISTORY_LIMIT_15M,
) -> List[Candle]:
    """
    Fetch 15M historical candles for one symbol.

    Important:
    Bitget v2 history-candles has a maximum limit of 200.
    The requested value is therefore clamped to 1..200 so that
    accidental future changes can never generate an invalid request.
    """

    try:
        requested_limit = int(limit)
    except (TypeError, ValueError):
        requested_limit = HISTORY_LIMIT_15M

    safe_limit = max(
        1,
        min(requested_limit, MAX_HISTORY_CANDLES),
    )

    data = get_json(
        "/api/v2/mix/market/history-candles",
        {
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "granularity": "15m",
            "limit": safe_limit,
        },
    )

    candles: List[Candle] = []

    for row in data.get("data", []):
        if len(row) < 6:
            continue

        try:
            candles.append(
                Candle(
                    int(row[0]),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                )
            )
        except (TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x.ts)

    # Only CLOSED candles are allowed.
    now_ms = int(time.time() * 1000)
    interval_ms = 15 * 60 * 1000

    closed_candles = [
        c
        for c in candles
        if c.ts + interval_ms <= now_ms
    ]

    return closed_candles


# ============================================================
# Technical calculations
# ============================================================

def sma(
    values: List[float],
    period: int,
) -> Optional[float]:
    if len(values) < period:
        return None

    return sum(values[-period:]) / period


def atr(
    candles: List[Candle],
    period: int = 14,
) -> Optional[float]:
    if len(candles) < period + 1:
        return None

    trs: List[float] = []

    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]

        trs.append(
            max(
                cur.h - cur.l,
                abs(cur.h - prev.c),
                abs(cur.l - prev.c),
            )
        )

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


# ============================================================
# Utility functions
# ============================================================

def iso_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(
        ts_ms / 1000,
        tz=timezone.utc,
    ).isoformat()


def pct_change(
    price: float,
    base: float,
) -> float:
    if base == 0:
        return 0.0

    return (price / base - 1.0) * 100.0


def signal_id(
    signal: Dict[str, Any],
) -> str:
    return (
        f"{signal['symbol']}:"
        f"{signal['direction']}:"
        f"{signal['signal_ts']}"
    )


# ============================================================
# JSON persistence
# ============================================================

def load_json(
    path: str,
    default: Any,
) -> Any:
    if not os.path.exists(path):
        return default

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)

    except Exception:
        return default


def save_json(
    path: str,
    data: Any,
) -> None:
    tmp = f"{path}.tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(tmp, path)


def load_snapshots() -> List[Dict[str, Any]]:
    data = load_json(
        SNAPSHOT_FILE,
        [],
    )

    if isinstance(data, dict):
        data = data.get("snapshots", [])

    return data if isinstance(data, list) else []


def load_tracker() -> Dict[str, Any]:
    data = load_json(
        TRACKER_FILE,
        None,
    )

    if not isinstance(data, dict):
        data = {}

    data.setdefault(
        "schema_version",
        1,
    )

    data.setdefault(
        "signals",
        {},
    )

    return data


# ============================================================
# Checkpoint generation
# ============================================================

def checkpoint_for_signal(
    snapshot: Dict[str, Any],
    candles: List[Candle],
) -> Optional[Dict[str, Any]]:

    signal = snapshot["signal"]
    sid = snapshot["signal_id"]

    direction = signal["direction"]
    entry = float(signal["signal_close"])
    signal_ts = int(signal["signal_ts"])

    structure_level = float(
        signal.get(
            "structure_level",
            0.0,
        )
        or 0.0
    )

    base_atr = float(
        signal.get(
            "atr",
            0.0,
        )
        or 0.0
    )

    tracked_until = (
        signal_ts
        + TRACKING_HOURS * 60 * 60 * 1000
    )

    # Only candles AFTER the signal candle and
    # inside the fixed 24h research horizon.
    after = [
        c
        for c in candles
        if signal_ts < c.ts <= tracked_until
    ]

    if not after:
        return None

    latest = after[-1]

    closes = [
        c.c
        for c in candles
        if c.ts <= latest.ts
    ]

    ma120 = sma(
        closes,
        120,
    )

    ma200 = sma(
        closes,
        200,
    )

    current_atr = atr(
        [
            c
            for c in candles
            if c.ts <= latest.ts
        ],
        14,
    )

    # Use scanner signal ATR as primary normalization basis.
    # If missing/invalid, fall back to current 15M ATR.
    norm_atr = (
        base_atr
        if base_atr > 0
        else (current_atr or 0.0)
    )

    favorable_price = max(
        c.h
        for c in after
    )

    adverse_price = min(
        c.l
        for c in after
    )

    # ========================================================
    # Directional calculations
    # ========================================================

    if direction == "LONG":

        mfe_pct = pct_change(
            favorable_price,
            entry,
        )

        mae_pct = pct_change(
            adverse_price,
            entry,
        )

        current_move_pct = pct_change(
            latest.c,
            entry,
        )

        favorable_excursion = (
            favorable_price - entry
        )

        adverse_excursion = (
            entry - adverse_price
        )

        structure_reclaimed = (
            structure_level > 0
            and latest.c > structure_level
        )

        ma200_reclaimed = (
            ma200 is not None
            and latest.c > ma200
        )

        ma120_reclaimed = (
            ma120 is not None
            and latest.c > ma120
        )

    else:

        mfe_pct = -pct_change(
            adverse_price,
            entry,
        )

        mae_pct = -pct_change(
            favorable_price,
            entry,
        )

        current_move_pct = -pct_change(
            latest.c,
            entry,
        )

        favorable_excursion = (
            entry - adverse_price
        )

        adverse_excursion = (
            favorable_price - entry
        )

        structure_reclaimed = (
            structure_level > 0
            and latest.c < structure_level
        )

        ma200_reclaimed = (
            ma200 is not None
            and latest.c < ma200
        )

        ma120_reclaimed = (
            ma120 is not None
            and latest.c < ma120
        )

    mfe_atr = (
        favorable_excursion / norm_atr
        if norm_atr > 0
        else None
    )

    mae_atr = (
        adverse_excursion / norm_atr
        if norm_atr > 0
        else None
    )

    # ========================================================
    # Opposite-direction reclaim
    # ========================================================

    if direction == "SHORT":

        opposite_structure_reclaim = (
            structure_level > 0
            and latest.c > structure_level
        )

        opposite_ma200_reclaim = (
            ma200 is not None
            and latest.c > ma200
        )

        opposite_candle = (
            latest.c > latest.o
        )

    else:

        opposite_structure_reclaim = (
            structure_level > 0
            and latest.c < structure_level
        )

        opposite_ma200_reclaim = (
            ma200 is not None
            and latest.c < ma200
        )

        opposite_candle = (
            latest.c < latest.o
        )

    reversal_condition = (
        opposite_structure_reclaim
        and opposite_ma200_reclaim
    )

    # ========================================================
    # Consecutive reversal confirmation
    # ========================================================

    consecutive = 0

    for c in reversed(after):

        all_closes = [
            x.c
            for x in candles
            if x.ts <= c.ts
        ]

        c_ma200 = sma(
            all_closes,
            200,
        )

        if direction == "SHORT":

            cond = (
                structure_level > 0
                and c.c > structure_level
                and c_ma200 is not None
                and c.c > c_ma200
            )

        else:

            cond = (
                structure_level > 0
                and c.c < structure_level
                and c_ma200 is not None
                and c.c < c_ma200
            )

        if not cond:
            break

        consecutive += 1

    reversal_confirmed = (
        consecutive
        >= REVERSAL_CONSECUTIVE_BARS
    )

    reversal_watch = reversal_condition

    # ========================================================
    # Checkpoint
    # ========================================================

    return {
        "signal_id": sid,
        "ts": latest.ts,
        "time": iso_utc(latest.ts),
        "close": latest.c,
        "high": latest.h,
        "low": latest.l,

        "current_move_pct": round(
            current_move_pct,
            4,
        ),

        "mfe_pct": round(
            mfe_pct,
            4,
        ),

        "mae_pct": round(
            mae_pct,
            4,
        ),

        "mfe_atr": (
            round(mfe_atr, 4)
            if mfe_atr is not None
            else None
        ),

        "mae_atr": (
            round(mae_atr, 4)
            if mae_atr is not None
            else None
        ),

        "ma120": (
            round(ma120, 10)
            if ma120 is not None
            else None
        ),

        "ma200": (
            round(ma200, 10)
            if ma200 is not None
            else None
        ),

        "atr14": (
            round(current_atr, 10)
            if current_atr is not None
            else None
        ),

        "structure_level": structure_level,

        "structure_reclaimed": bool(
            opposite_structure_reclaim
        ),

        "ma120_side_relation": (
            "ABOVE"
            if (
                ma120 is not None
                and latest.c > ma120
            )
            else (
                "BELOW"
                if ma120 is not None
                else "UNKNOWN"
            )
        ),

        "ma200_reclaimed": bool(
            opposite_ma200_reclaim
        ),

        "opposite_candle": bool(
            opposite_candle
        ),

        "reversal_watch": bool(
            reversal_watch
        ),

        "reversal_confirmed": bool(
            reversal_confirmed
        ),

        "reversal_consecutive_bars": consecutive,
    }


# ============================================================
# Signal record update
# ============================================================

def update_signal_record(
    record: Dict[str, Any],
    candles: List[Candle],
) -> bool:

    snapshot = record["snapshot"]
    signal = snapshot["signal"]

    sid = record["signal_id"]

    signal_ts = int(
        signal["signal_ts"]
    )

    entry = float(
        signal["signal_close"]
    )

    direction = signal["direction"]

    norm_atr = float(
        signal.get(
            "atr",
            0.0,
        )
        or 0.0
    )

    if norm_atr <= 0:

        norm_atr = (
            atr(
                [
                    c
                    for c in candles
                    if c.ts <= signal_ts
                ],
                14,
            )
            or 0.0
        )

    eligible = [
        c
        for c in candles
        if c.ts > signal_ts
    ]

    if not eligible:
        return False

    tracked_until = (
        signal_ts
        + TRACKING_HOURS * 60 * 60 * 1000
    )

    eligible = [
        c
        for c in eligible
        if c.ts <= tracked_until
    ]

    if not eligible:
        return False

    checkpoint = checkpoint_for_signal(
        snapshot,
        candles,
    )

    if checkpoint is None:
        return False

    # ========================================================
    # Add latest checkpoint without duplicating timestamps
    # ========================================================

    existing_by_ts = {
        int(x["ts"]): x
        for x in record.get(
            "checkpoints",
            [],
        )
    }

    existing_by_ts[
        int(checkpoint["ts"])
    ] = checkpoint

    checkpoints = [
        existing_by_ts[k]
        for k in sorted(existing_by_ts)
    ]

    record["checkpoints"] = (
        checkpoints[-TRACKING_BARS:]
    )

    # ========================================================
    # Recalculate MFE / MAE over full currently available
    # tracking horizon.
    # ========================================================

    favorable_high = max(
        c.h
        for c in eligible
    )

    favorable_low = min(
        c.l
        for c in eligible
    )

    if direction == "LONG":

        mfe_price = favorable_high
        mae_price = favorable_low

        mfe_move = (
            mfe_price - entry
        )

        mae_move = (
            entry - mae_price
        )

    else:

        mfe_price = favorable_low
        mae_price = favorable_high

        mfe_move = (
            entry - mfe_price
        )

        mae_move = (
            mae_price - entry
        )

    record["metrics"] = {
        "entry": entry,
        "latest_close": eligible[-1].c,

        "mfe_price": mfe_price,
        "mae_price": mae_price,

        "mfe_pct": round(
            (mfe_move / entry) * 100.0,
            4,
        ),

        "mae_pct": round(
            (mae_move / entry) * 100.0,
            4,
        ),

        "mfe_atr": (
            round(
                mfe_move / norm_atr,
                4,
            )
            if norm_atr > 0
            else None
        ),

        "mae_atr": (
            round(
                mae_move / norm_atr,
                4,
            )
            if norm_atr > 0
            else None
        ),
    }

    # ========================================================
    # Events
    # ========================================================

    events = record.setdefault(
        "events",
        {},
    )

    def favorable_threshold(
        threshold: float,
    ) -> bool:

        return (
            (mfe_move / norm_atr) >= threshold
            if norm_atr > 0
            else False
        )

    # --------------------------------------------------------
    # +1 ATR
    # --------------------------------------------------------

    if (
        favorable_threshold(
            FAVORABLE_1_ATR
        )
        and "favorable_1atr_ts" not in events
    ):

        for c in eligible:

            move = (
                c.h - entry
                if direction == "LONG"
                else entry - c.l
            )

            if (
                norm_atr > 0
                and move / norm_atr
                >= FAVORABLE_1_ATR
            ):

                events[
                    "favorable_1atr_ts"
                ] = c.ts

                break

    # --------------------------------------------------------
    # +1.5 ATR
    # --------------------------------------------------------

    if (
        favorable_threshold(
            FAVORABLE_1_5_ATR
        )
        and "favorable_1_5atr_ts" not in events
    ):

        for c in eligible:

            move = (
                c.h - entry
                if direction == "LONG"
                else entry - c.l
            )

            if (
                norm_atr > 0
                and move / norm_atr
                >= FAVORABLE_1_5_ATR
            ):

                events[
                    "favorable_1_5atr_ts"
                ] = c.ts

                break

    # --------------------------------------------------------
    # -1 ATR
    # --------------------------------------------------------

    if (
        norm_atr > 0
        and (
            mae_move / norm_atr
        ) >= ADVERSE_1_ATR
        and "adverse_1atr_ts" not in events
    ):

        for c in eligible:

            move = (
                entry - c.l
                if direction == "LONG"
                else c.h - entry
            )

            if (
                move / norm_atr
                >= ADVERSE_1_ATR
            ):

                events[
                    "adverse_1atr_ts"
                ] = c.ts

                break

    # ========================================================
    # Reversal events
    # ========================================================

    latest = checkpoints[-1]

    if (
        latest.get("reversal_watch")
        and "reversal_watch_ts" not in events
    ):

        events[
            "reversal_watch_ts"
        ] = latest["ts"]

    if (
        latest.get("reversal_confirmed")
        and "reversal_confirmed_ts" not in events
    ):

        events[
            "reversal_confirmed_ts"
        ] = latest["ts"]

    # ========================================================
    # First decisive event
    # ========================================================

    if "first_decisive" not in record:

        decisive_candidates: List[
            Tuple[int, str]
        ] = []

        if "favorable_1_5atr_ts" in events:

            decisive_candidates.append(
                (
                    events[
                        "favorable_1_5atr_ts"
                    ],
                    "DIRECTIONAL_EXPANSION",
                )
            )

        if "adverse_1atr_ts" in events:

            decisive_candidates.append(
                (
                    events[
                        "adverse_1atr_ts"
                    ],
                    "DIRECTIONAL_FAILURE",
                )
            )

        if "reversal_confirmed_ts" in events:

            reversal_outcome = (
                "REVERSAL_LONG"
                if direction == "SHORT"
                else "REVERSAL_SHORT"
            )

            decisive_candidates.append(
                (
                    events[
                        "reversal_confirmed_ts"
                    ],
                    reversal_outcome,
                )
            )

        if decisive_candidates:

            decisive_candidates.sort(
                key=lambda x: x[0]
            )

            record["first_decisive"] = {
                "ts": decisive_candidates[0][0],
                "outcome": decisive_candidates[0][1],
            }

    record["last_update_ts"] = latest["ts"]

    # ========================================================
    # Final 24H outcome
    # ========================================================

    horizon_reached = (
        eligible[-1].ts
        >= tracked_until
    )

    if horizon_reached:

        record["completed"] = True

        record["completed_at"] = (
            eligible[-1].ts
        )

        if latest.get(
            "reversal_confirmed"
        ):

            record["outcome"] = (
                "REVERSAL_LONG"
                if direction == "SHORT"
                else "REVERSAL_SHORT"
            )

        elif favorable_threshold(
            FAVORABLE_1_5_ATR
        ):

            record["outcome"] = (
                "DIRECTIONAL_EXPANSION"
            )

        elif (
            norm_atr > 0
            and (
                mae_move / norm_atr
            ) >= ADVERSE_1_ATR
        ):

            record["outcome"] = (
                "DIRECTIONAL_FAILURE"
            )

        else:

            record["outcome"] = (
                "NO_EXPANSION"
            )

    else:

        record["completed"] = False
        record["outcome"] = "OPEN"

    return True


# ============================================================
# Main
# ============================================================

def main() -> None:

    snapshots = load_snapshots()

    tracker = load_tracker()

    records: Dict[
        str,
        Dict[str, Any],
    ] = tracker["signals"]

    if not snapshots:

        print(
            "[INFO] No signal snapshots found."
        )

        save_json(
            TRACKER_FILE,
            tracker,
        )

        return

    # ========================================================
    # Only active signals inside the 24h research horizon
    # need fresh Bitget requests.
    # ========================================================

    now_ms = int(
        time.time() * 1000
    )

    active_snapshots: List[
        Dict[str, Any]
    ] = []

    for snapshot in snapshots:

        if (
            not isinstance(
                snapshot,
                dict,
            )
            or "signal" not in snapshot
        ):
            continue

        signal = snapshot["signal"]

        try:

            signal_ts = int(
                signal["signal_ts"]
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            continue

        if (
            signal_ts
            + TRACKING_HOURS
            * 60
            * 60
            * 1000
            >= now_ms
        ):

            active_snapshots.append(
                snapshot
            )

    # ========================================================
    # One market-data request per ACTIVE symbol.
    #
    # This avoids:
    # - one request per signal
    # - duplicate requests for the same symbol
    # - unnecessary requests for expired snapshots
    # ========================================================

    symbols = sorted(
        {
            s["signal"]["symbol"]
            for s in active_snapshots
            if (
                isinstance(s, dict)
                and "signal" in s
            )
        }
    )

    candles_by_symbol: Dict[
        str,
        List[Candle],
    ] = {}

    for symbol in symbols:

        try:

            candles_by_symbol[
                symbol
            ] = get_candles(symbol)

        except Exception as exc:

            print(
                f"[ERROR] {symbol}: {exc}"
            )

    # ========================================================
    # Update every snapshot using cached symbol candles.
    # ========================================================

    updated = 0

    for snapshot in snapshots:

        if (
            not isinstance(
                snapshot,
                dict,
            )
            or "signal" not in snapshot
        ):
            continue

        sid = (
            snapshot.get(
                "signal_id"
            )
            or signal_id(
                snapshot["signal"]
            )
        )

        symbol = snapshot[
            "signal"
        ]["symbol"]

        candles = candles_by_symbol.get(
            symbol,
            [],
        )

        if not candles:
            continue

        record = records.get(sid)

        if record is None:

            record = {
                "signal_id": sid,
                "snapshot": snapshot,
                "checkpoints": [],
                "events": {},
                "completed": False,
                "outcome": "OPEN",
                "created_at": int(
                    time.time() * 1000
                ),
            }

            records[sid] = record

        try:

            if update_signal_record(
                record,
                candles,
            ):

                updated += 1

        except Exception as exc:

            record["last_error"] = str(
                exc
            )

            print(
                f"[ERROR] tracking "
                f"{sid}: {exc}"
            )

    # ========================================================
    # Keep enough history for research.
    # ========================================================

    ordered = sorted(
        records.items(),
        key=lambda kv: int(
            kv[1][
                "snapshot"
            ][
                "signal"
            ][
                "signal_ts"
            ]
        ),
        reverse=True,
    )

    tracker["signals"] = dict(
        ordered[:1000]
    )

    tracker["updated_at"] = int(
        time.time() * 1000
    )

    tracker[
        "updated_at_iso"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    save_json(
        TRACKER_FILE,
        tracker,
    )

    # ========================================================
    # Summary
    # ========================================================

    open_count = sum(
        1
        for r in tracker[
            "signals"
        ].values()
        if r.get("outcome") == "OPEN"
    )

    reversal_count = sum(
        1
        for r in tracker[
            "signals"
        ].values()
        if str(
            r.get(
                "outcome",
                "",
            )
        ).startswith(
            "REVERSAL_"
        )
    )

    print(
        f"[INFO] tracker "
        f"updated={updated} "
        f"total={len(tracker['signals'])} "
        f"open={open_count} "
        f"reversals={reversal_count}"
    )


if __name__ == "__main__":
    main()
