"""
Bitget USDT-FUTURES 1H/4H Long Scanner v2
- 1H MA20/60/120 bullish alignment
- 1H volume expansion
- 24h turnover filter
- Price distance from MA20
- 1H RSI(14)
- Recent high breakout
- 4H trend confirmation
- 100-point scoring
- Entry / pullback / chase-risk classification

No trading orders are placed. Market data only.
"""

import time
import requests
import pandas as pd

BASE = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

MIN_24H_TURNOVER = 30_000_000
MIN_VOL_RATIO = 1.30
MAX_MA20_DISTANCE = 0.06
TOP_N = 10

session = requests.Session()
session.headers.update({"User-Agent": "Bitget-1H-Long-Scanner-v2/1.0"})


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
        x["symbol"] for x in data
        if x.get("quoteCoin") == "USDT"
        and x.get("symbolType") != "delivery"
    ]


def get_candles(symbol, timeframe, limit=200):
    data = get("/api/v2/mix/market/candles", {
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
        "granularity": timeframe,
        "limit": str(limit),
    })

    df = pd.DataFrame(data, columns=[
        "ts", "open", "high", "low", "close", "volume", "quote_volume"
    ])

    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)

    # 진행 중인 봉은 제외
    if len(df) > 1:
        df = df.iloc[:-1].copy()

    return df


def get_ticker(symbol):
    data = get("/api/v2/mix/market/ticker", {
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
    })
    return data[0]


def add_indicators(df):
    df = df.copy()

    for n in (20, 60, 120):
        df[f"ma{n}"] = df.close.rolling(n).mean()

    # RSI(14)
    delta = df.close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    df["rsi14"] = 100 - (100 / (1 + rs))

    return df


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
    ma20, ma60, ma120 = x.ma20, x.ma60, x.ma120

    if pd.isna(ma120):
        return None

    # 1H 핵심 정배열
    alignment = price > ma20 > ma60 > ma120
    if not alignment:
        return None

    # 1H 추세
    ma20_up = ma20 > prev.ma20
    ma60_up = ma60 > prev.ma60
    ma120_up = ma120 >= prev.ma120

    # 1H 거래량
    avg20_vol = a.volume.iloc[-21:-1].mean()
    vol_ratio = float(x.volume / avg20_vol) if avg20_vol else 0

    # 24H 거래대금
    turnover = float(ticker.get("usdtVol") or 0)
    if turnover <= 0:
        turnover = float(a.quote_volume.iloc[-24:].sum())

    ma20_dist = float(price / ma20 - 1)

    # 최근 20개 완성 1H 봉의 고점 돌파 여부
    recent_high = float(a.high.iloc[-21:-1].max())
    breakout = price >= recent_high

    # 4H 확인
    h_ma20, h_ma60, h_ma120 = h.ma20, h.ma60, h.ma120
    h_alignment = (
        h.close > h_ma20 > h_ma60 > h_ma120
        and h_ma20 > hprev.ma20
        and h_ma60 >= hprev.ma60
    )

    # 4H가 완전 정배열은 아니어도 MA20/60이 상승하면 부분점수
    h_partial = (
        h.close > h_ma20
        and h_ma20 > h_ma60
        and h_ma20 > hprev.ma20
    )

    rsi = float(x.rsi14)

    # 점수
    score = 0

    # 1H 구조 30
    score += 30

    # MA 방향 20
    score += 10 if ma20_up else 0
    score += 7 if ma60_up else 0
    score += 3 if ma120_up else 0

    # 거래량 15 + 강한 유입 5
    score += 15 if vol_ratio >= 1.30 else 0
    score += 5 if vol_ratio >= 2.00 else 0

    # 거래대금 10 + 우수 5
    score += 10 if turnover >= 30_000_000 else 0
    score += 5 if turnover >= 100_000_000 else 0

    # MA20 거리 5
    if ma20_dist <= 0.03:
        score += 5
    elif ma20_dist <= 0.06:
        score += 3

    # RSI 5: 너무 낮거나 과열보다 50~68을 선호
    if 50 <= rsi <= 68:
        score += 5
    elif 45 <= rsi < 50 or 68 < rsi <= 72:
        score += 3

    # 돌파 5
    if breakout:
        score += 5

    # 4H 확인 10
    if h_alignment:
        score += 10
    elif h_partial:
        score += 5

    # 분류
    if ma20_dist > 0.10 or rsi > 75:
        classification = "🔴 추격 금지"
    elif h_alignment and score >= 85 and ma20_dist <= 0.06:
        classification = "🟢 진입 검토"
    elif score >= 75:
        classification = "🟡 눌림 대기"
    else:
        classification = "⚪ 관찰"

    # 기준 손절 참고값: MA20 아래 1% (자동 주문 아님)
    reference_stop = float(ma20 * 0.99)

    return {
        "symbol": symbol,
        "score": int(min(score, 100)),
        "classification": classification,
        "price": price,
        "ma20": float(ma20),
        "ma60": float(ma60),
        "ma120": float(ma120),
        "ma20_dist_pct": ma20_dist * 100,
        "vol_ratio": vol_ratio,
        "turnover_24h": turnover,
        "rsi14": rsi,
        "breakout_20h": breakout,
        "4h_alignment": h_alignment,
        "24h_change_pct": float(ticker.get("change24h") or 0) * 100,
        "reference_stop_ma20_minus_1pct": reference_stop,
    }


def main():
    syms = get_symbols()
    results = []

    print(f"비트겟 USDT-FUTURES 스캔 대상: {len(syms)}개")
    print("1H/4H 완성봉 기준으로 스캔합니다.\n")

    for i, symbol in enumerate(syms, 1):
        try:
            df1h = get_candles(symbol, "1H", 160)
            df4h = get_candles(symbol, "4H", 160)

            if len(df1h) < 125 or len(df4h) < 125:
                continue

            ticker = get_ticker(symbol)

            # 기본 유동성 필터를 먼저 적용
            turnover = float(ticker.get("usdtVol") or 0)
            if turnover and turnover < MIN_24H_TURNOVER:
                continue

            r = analyze(symbol, df1h, df4h, ticker)
            if r:
                results.append(r)

        except Exception as e:
            print(f"[skip] {symbol}: {e}")

        time.sleep(0.03)

        if i % 50 == 0:
            print(f"진행: {i}/{len(syms)}")

    if not results:
        print("\n현재 조건을 만족하는 종목이 없습니다.")
        return

    out = pd.DataFrame(results)
    out = out.sort_values(
        ["score", "4h_alignment", "vol_ratio", "turnover_24h"],
        ascending=[False, False, False, False]
    ).head(TOP_N)

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 220)

    display_cols = [
        "symbol", "score", "classification", "price",
        "ma20_dist_pct", "vol_ratio", "turnover_24h",
        "rsi14", "breakout_20h", "4h_alignment",
        "24h_change_pct", "reference_stop_ma20_minus_1pct"
    ]

    print("\n" + "=" * 100)
    print("🔥 BITGET 1H / 4H LONG CANDIDATES TOP 10")
    print("=" * 100)
    print(out[display_cols].to_string(
        index=False,
        formatters={
            "price": "{:,.6g}".format,
            "ma20_dist_pct": "{:+.2f}%".format,
            "vol_ratio": "{:.2f}x".format,
            "turnover_24h": "{:,.0f}".format,
            "rsi14": "{:.1f}".format,
            "24h_change_pct": "{:+.2f}%".format,
            "reference_stop_ma20_minus_1pct": "{:,.6g}".format,
        }
    ))

    out.to_csv("bitget_1h_long_candidates_v2.csv", index=False)
    print("\n결과 저장: bitget_1h_long_candidates_v2.csv")
    print("※ 점수와 분류는 매매 신호가 아니라 후보 선별용입니다.")


if __name__ == "__main__":
    main()
