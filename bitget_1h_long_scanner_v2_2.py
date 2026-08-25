import time
import requests
import os
import pandas as pd

BASE = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
MIN_24H_TURNOVER = 30_000_000
MIN_VOL_RATIO = 1.30
MAX_MA20_DISTANCE = 0.06
TOP_N = 10

session = requests.Session()
session.headers.update({"User-Agent": "Bitget-1H-Long-Scanner-v2.2/1.0"})


def get(path, params=None):
    r = session.get(BASE + path, params=params, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "00000":
        raise RuntimeError(j)
    return j["data"]


def get_symbols():
    data = get("/api/v2/mix/market/contracts", {"productType": PRODUCT_TYPE})
    return [
        x["symbol"]
        for x in data
        if x.get("quoteCoin") == "USDT"
        and x.get("symbolType") != "delivery"
    ]


def get_all_tickers():
    data = get("/api/v2/mix/market/tickers", {"productType": PRODUCT_TYPE})
    return {x["symbol"]: x for x in data}


def get_candles(symbol, timeframe, limit=160):
    data = get("/api/v2/mix/market/candles", {
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
        "granularity": timeframe,
        "limit": str(limit)
    })

    df = pd.DataFrame(
        data,
        columns=["ts", "open", "high", "low", "close", "volume", "quote_volume"]
    )

    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["ts"] = pd.to_numeric(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)

    return df.iloc[:-1].copy() if len(df) > 1 else df


def add_indicators(df):
    df = df.copy()

    for n in (20, 60, 120):
        df[f"ma{n}"] = df.close.rolling(n).mean()

    delta = df.close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14
    ).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    df["rsi14"] = 100 - (100 / (1 + rs))

    prev_close = df.close.shift(1)

    tr = pd.concat([
        df.high - df.low,
        (df.high - prev_close).abs(),
        (df.low - prev_close).abs()
    ], axis=1).max(axis=1)

    df["atr14"] = tr.rolling(14).mean()

    return df


def classify_setup(
    ma20_dist,
    rsi,
    vol_ratio,
    breakout,
    near_breakout,
    h_alignment,
    h_partial,
    ma20_up,
    change24h
):
    if ma20_dist > MAX_MA20_DISTANCE or rsi >= 75 or change24h >= 12:
        return (
            "🔴 추격 금지",
            "MA20 이격/RSI/24H 급등 중 하나가 과열 기준 초과"
        )

    if (
        h_alignment
        and breakout
        and ma20_dist <= 0.04
        and 50 <= rsi <= 70
        and vol_ratio >= MIN_VOL_RATIO
    ):
        return (
            "🟢 진입 가능",
            "추세 + 돌파 + 거래량 확인"
        )

    if (
        h_alignment
        and ma20_up
        and ma20_dist <= 0.035
        and 45 <= rsi <= 68
    ):
        return (
            "🟡 눌림 대기",
            "상승 추세 유지, MA20 부근 눌림 확인"
        )

    if h_alignment and near_breakout and 45 <= rsi <= 70:
        return (
            "🔵 돌파 대기",
            "20봉 고점 돌파 확인 필요"
        )

    if h_partial and ma20_up and 45 <= rsi <= 68:
        return (
            "🟡 눌림 대기",
            "4H 추세는 양호하나 완전 정배열 전"
        )

    return (
        "⚪ 관찰",
        "핵심 진입 조건 일부 미충족"
    )


def analyze(symbol, df1h, df4h, ticker):
    if len(df1h) < 125 or len(df4h) < 125:
        return None

    a = add_indicators(df1h)
    b = add_indicators(df4h)

    x = a.iloc[-1]
    prev = a.iloc[-2]
    h = b.iloc[-1]
    hprev = b.iloc[-2]

    price = float(ticker["lastPr"])
    turnover = float(ticker.get("usdtVolume") or 0)
    change24h = float(ticker.get("change24h") or 0) * 100

    ma20 = x.ma20
    ma60 = x.ma60
    ma120 = x.ma120

    h20 = h.ma20
    h60 = h.ma60
    h120 = h.ma120

    atr = float(x.atr14)

    if any(pd.isna(v) for v in [
        ma20,
        ma60,
        ma120,
        h20,
        h60,
        h120,
        x.rsi14,
        x.atr14
    ]):
        return None

    if turnover < MIN_24H_TURNOVER:
        return None

    if not (price > ma20 > ma60 > ma120):
        return None

    ma20_up = bool(ma20 > prev.ma20)
    ma60_up = bool(ma60 > prev.ma60)
    ma120_up = bool(ma120 >= prev.ma120)

    avg20_vol = a.volume.iloc[-21:-1].mean()
    vol_ratio = float(x.volume / avg20_vol) if avg20_vol else 0.0

    ma20_dist = float(price / ma20 - 1)
    rsi = float(x.rsi14)

    recent_high_20 = float(a.high.iloc[-21:-1].max())

    breakout = bool(price >= recent_high_20)

    near_breakout = bool(
        not breakout
        and price >= recent_high_20 * 0.99
    )

    h_alignment = bool(
        h.close > h20 > h60 > h120
        and h20 > hprev.ma20
        and h60 >= hprev.ma60
    )

    h_partial = bool(
        h.close > h20
        and h20 > h60
        and h20 > hprev.ma20
    )

    classification, setup_reason = classify_setup(
        ma20_dist,
        rsi,
        vol_ratio,
        breakout,
        near_breakout,
        h_alignment,
        h_partial,
        ma20_up,
        change24h
    )

    score = 25

    score += 7 if ma20_up else 0
    score += 5 if ma60_up else 0
    score += 3 if ma120_up else 0

    score += (
        15 if vol_ratio >= 2
        else 12 if vol_ratio >= 1.5
        else 8 if vol_ratio >= MIN_VOL_RATIO
        else 0
    )

    score += (
        15 if turnover >= 300_000_000
        else 12 if turnover >= 100_000_000
        else 8
    )

    score += (
        10 if ma20_dist <= 0.03
        else 7 if ma20_dist <= 0.05
        else 4 if ma20_dist <= MAX_MA20_DISTANCE
        else 0
    )

    score += (
        10 if 50 <= rsi <= 68
        else 6 if 45 <= rsi < 50 or 68 < rsi <= 72
        else 3 if 40 <= rsi < 45
        else 0
    )

    score += 5 if breakout else 0
    score += 5 if h_alignment else (2 if h_partial else 0)

    score = min(int(score), 100)

    entry_reference = recent_high_20 if breakout else price

    ma_stop = float(ma20 * 0.99)
    atr_stop = float(entry_reference - 1.5 * atr)

    reference_stop = min(ma_stop, atr_stop)

    if reference_stop <= 0:
        reference_stop = ma_stop

    risk = float(entry_reference - reference_stop)

    target_1 = (
        float(entry_reference + 2 * risk)
        if risk > 0
        else float(entry_reference)
    )

    stop_distance_pct = (
        risk / entry_reference * 100
        if entry_reference > 0
        else 0.0
    )

    return {
        "symbol": symbol,
        "score": score,
        "classification": classification,
        "setup_reason": setup_reason,
        "price": price,
        "entry_reference": entry_reference,
        "reference_stop": reference_stop,
        "target_1_2R": target_1,
        "stop_distance_pct": stop_distance_pct,
        "ma20_dist_pct": ma20_dist * 100,
        "vol_ratio": vol_ratio,
        "turnover_24h": turnover,
        "rsi14": rsi,
        "atr14": atr,
        "breakout_20h": breakout,
        "near_breakout": near_breakout,
        "4h_alignment": h_alignment,
        "24h_change_pct": change24h,
    }


def telegram_send(out):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("⚠️ Telegram Secret이 없습니다.")
        return

    rows = out.head(TOP_N)

    lines = [
        "🔥 BITGET 1H / 4H LONG CANDIDATES v2.2",
        ""
    ]

    for _, r in rows.iterrows():
        lines.append(
            f"{r['symbol']} | {r['score']} | {r['classification']}\n"
            f"{r['setup_reason']}\n"
            f"현재가: {r['price']:.8g}\n"
            f"진입기준: {r['entry_reference']:.8g}\n"
            f"손절기준: {r['reference_stop']:.8g}\n"
            f"2R 목표: {r['target_1_2R']:.8g}\n"
            f"손절거리: {r['stop_distance_pct']:.2f}%\n"
            f"RSI: {r['rsi14']:.1f}\n"
        )

    message = "\n".join(lines)

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message,
            },
            timeout=10,
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(result)

        print("📨 Telegram 전송 완료")

    except Exception as e:
        print(f"⚠️ Telegram 전송 실패: {e}")


def main():
    symbols = get_symbols()
    tickers = get_all_tickers()

    liquid = [
        s
        for s in symbols
        if s in tickers
        and float(tickers[s].get("usdtVolume") or 0) >= MIN_24H_TURNOVER
    ]

    print(f"비트겟 USDT-FUTURES 전체 종목: {len(symbols)}개")
    print(
        f"24H USDT 거래대금 {MIN_24H_TURNOVER:,.0f} 이상: "
        f"{len(liquid)}개"
    )
    print("1H/4H 완성봉 기준으로 스캔합니다.")
    print(
        "v2.2: 진입/눌림/돌파/추격 구분 + "
        "기준 손절/2R 목표를 계산합니다.\n"
    )

    results = []

    for i, symbol in enumerate(liquid, 1):
        try:
            r = analyze(
                symbol,
                get_candles(symbol, "1H"),
                get_candles(symbol, "4H"),
                tickers[symbol]
            )

            if r:
                results.append(r)

        except Exception as e:
            print(f"[skip] {symbol}: {e}")

        time.sleep(0.03)

        if i % 50 == 0:
            print(f"진행: {i}/{len(liquid)}")

    if not results:
        print("\n현재 조건을 만족하는 종목이 없습니다.")
        return

    out = pd.DataFrame(results)

    class_rank = {
        "🟢 진입 가능": 0,
        "🟡 눌림 대기": 1,
        "🔵 돌파 대기": 2,
        "⚪ 관찰": 3,
        "🔴 추격 금지": 4
    }

    out["class_rank"] = (
        out["classification"]
        .map(class_rank)
        .fillna(9)
    )

    out = out.sort_values(
        [
            "class_rank",
            "score",
            "4h_alignment",
            "vol_ratio",
            "turnover_24h"
        ],
        ascending=[
            True,
            False,
            False,
            False,
            False
        ]
    ).head(TOP_N)

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 260)

    cols = [
        "symbol",
        "score",
        "classification",
        "setup_reason",
        "price",
        "entry_reference",
        "reference_stop",
        "target_1_2R",
        "stop_distance_pct",
        "ma20_dist_pct",
        "vol_ratio",
        "turnover_24h",
        "rsi14",
        "breakout_20h",
        "near_breakout",
        "4h_alignment",
        "24h_change_pct"
    ]

    print("\n" + "=" * 125)
    print("🔥 BITGET 1H / 4H LONG CANDIDATES v2.2 TOP 10")
    print("=" * 125)

    print(
        out[cols].to_string(
            index=False,
            formatters={
                "price": "{:,.8g}".format,
                "entry_reference": "{:,.8g}".format,
                "reference_stop": "{:,.8g}".format,
                "target_1_2R": "{:,.8g}".format,
                "stop_distance_pct": "{:.2f}%".format,
                "ma20_dist_pct": "{:+.2f}%".format,
                "vol_ratio": "{:.2f}x".format,
                "turnover_24h": "{:,.0f}".format,
                "rsi14": "{:.1f}".format,
                "24h_change_pct": "{:+.2f}%".format,
            }
        )
    )

    out.drop(
        columns=["class_rank"]
    ).to_csv(
        "bitget_1h_long_candidates_v2_2.csv",
        index=False
    )

    print("\n결과 저장: bitget_1h_long_candidates_v2_2.csv")
    print(
        "※ 분류/점수/가격은 후보 선별용 참고값이며 "
        "매매 신호가 아닙니다."
    )
    print(
        "※ 손절/목표가는 기술적 기준값이며 "
        "실제 체결 가격을 보장하지 않습니다."
    )
    print("※ 자동 주문 기능은 없습니다.")

    telegram_send(out)


if __name__ == "__main__":
    main()
