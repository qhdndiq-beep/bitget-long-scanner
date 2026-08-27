import time
import requests
import os
import pandas as pd

BASE = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

# =========================================================
# SCANNER SETTINGS
# =========================================================

# 24H 최소 거래대금
MIN_24H_TURNOVER = 10_000_000

# 현재 봉 거래량 / 직전 20개 완성봉 평균 거래량
MIN_VOL_RATIO = 1.30

# 현재가가 MA20에서 너무 멀어지는 것 방지
MAX_MA20_DISTANCE = 0.06

# Telegram / 결과 출력 개수
TOP_N = 10


session = requests.Session()

session.headers.update({
    "User-Agent": "Bitget-1H-Long-Scanner-v2.2/1.0"
})


# =========================================================
# BITGET API
# =========================================================

def get(path, params=None):

    r = session.get(
        BASE + path,
        params=params,
        timeout=15
    )

    r.raise_for_status()

    j = r.json()

    if j.get("code") != "00000":
        raise RuntimeError(j)

    return j["data"]


# =========================================================
# SYMBOL FILTER
# =========================================================

def get_symbols():

    data = get(
        "/api/v2/mix/market/contracts",
        {
            "productType": PRODUCT_TYPE
        }
    )

    symbols = []

    for x in data:

        if x.get("quoteCoin") != "USDT":
            continue

        if x.get("symbolType") != "perpetual":
            continue

        if x.get("symbolStatus") != "normal":
            continue

        if x.get("isRwa") != "NO":
            continue

        symbol = x.get("symbol")

        if not symbol:
            continue

        symbols.append(symbol)

    return symbols


# =========================================================
# TICKERS
# =========================================================

def get_all_tickers():

    data = get(
        "/api/v2/mix/market/tickers",
        {
            "productType": PRODUCT_TYPE
        }
    )

    return {
        x["symbol"]: x
        for x in data
    }


# =========================================================
# CANDLE DATA
# =========================================================

def get_candles(symbol, timeframe, limit=160):

    data = get(
        "/api/v2/mix/market/candles",
        {
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "granularity": timeframe,
            "limit": str(limit)
        }
    )

    df = pd.DataFrame(
        data,
        columns=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume"
        ]
    )

    for c in [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume"
    ]:

        df[c] = pd.to_numeric(
            df[c],
            errors="coerce"
        )

    df["ts"] = pd.to_numeric(df["ts"])

    df = (
        df
        .sort_values("ts")
        .reset_index(drop=True)
    )

    # 현재 진행 중인 봉 제외
    if len(df) > 1:
        df = df.iloc[:-1].copy()

    return df


# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df):

    df = df.copy()

    # -----------------------------------------------------
    # MA
    # -----------------------------------------------------

    for n in (20, 60, 120):

        df[f"ma{n}"] = (
            df.close.rolling(n).mean()
        )

    # -----------------------------------------------------
    # RSI 14
    # -----------------------------------------------------

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

    rs = (
        avg_gain /
        avg_loss.replace(0, pd.NA)
    )

    df["rsi14"] = (
        100 -
        (100 / (1 + rs))
    )

    # -----------------------------------------------------
    # ATR 14
    # -----------------------------------------------------

    prev_close = df.close.shift(1)

    tr = pd.concat(
        [
            df.high - df.low,
            (df.high - prev_close).abs(),
            (df.low - prev_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["atr14"] = (
        tr.rolling(14).mean()
    )

    return df


# =========================================================
# SETUP CLASSIFICATION
# =========================================================

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

    # -----------------------------------------------------
    # 과열
    # -----------------------------------------------------

    if (
        ma20_dist > MAX_MA20_DISTANCE
        or rsi >= 75
        or change24h >= 12
    ):

        return (
            "🔴 추격 금지",
            "MA20 이격/RSI/24H 급등 중 하나가 과열 기준 초과"
        )

    # -----------------------------------------------------
    # 진입 가능
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # 눌림 대기
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # 돌파 대기
    # -----------------------------------------------------

    if (
        h_alignment
        and near_breakout
        and 45 <= rsi <= 70
    ):

        return (
            "🔵 돌파 대기",
            "20봉 고점 돌파 확인 필요"
        )

    # -----------------------------------------------------
    # 4H 부분 정배열
    # -----------------------------------------------------

    if (
        h_partial
        and ma20_up
        and 45 <= rsi <= 68
    ):

        return (
            "🟡 눌림 대기",
            "4H 추세는 양호하나 완전 정배열 전"
        )

    return (
        "⚪ 관찰",
        "핵심 진입 조건 일부 미충족"
    )


# =========================================================
# RISK SCORE
# =========================================================
#
# 손절폭은 실제 매매에서 매우 중요한 요소이므로
# 전체 점수 100점 중 35점을 배정한다.
#
# 짧은 손절 = 높은 점수
# 긴 손절 = 낮은 점수
#
# 단순히 손절폭만 보고 판단하지 않고
# 구조점수 65 + 리스크점수 35로 종합한다.
#
# ---------------------------------------------------------
#
# ≤ 1.00%  = 35점
# ≤ 1.50%  = 32점
# ≤ 2.00%  = 28점
# ≤ 2.50%  = 24점
# ≤ 3.00%  = 18점
# ≤ 3.50%  = 10점
# ≤ 4.00%  = 5점
# > 4.00%  = 0점
#
# =========================================================

def calculate_risk_score(stop_distance_pct):

    if stop_distance_pct <= 1.00:
        return 35

    if stop_distance_pct <= 1.50:
        return 32

    if stop_distance_pct <= 2.00:
        return 28

    if stop_distance_pct <= 2.50:
        return 24

    if stop_distance_pct <= 3.00:
        return 18

    if stop_distance_pct <= 3.50:
        return 10

    if stop_distance_pct <= 4.00:
        return 5

    return 0


# =========================================================
# ANALYSIS
# =========================================================

def analyze(
    symbol,
    df1h,
    df4h,
    ticker
):

    if (
        len(df1h) < 125
        or len(df4h) < 125
    ):

        return None

    a = add_indicators(df1h)

    b = add_indicators(df4h)

    x = a.iloc[-1]

    prev = a.iloc[-2]

    h = b.iloc[-1]

    hprev = b.iloc[-2]

    # -----------------------------------------------------
    # Market data
    # -----------------------------------------------------

    price = float(
        ticker["lastPr"]
    )

    turnover = float(
        ticker.get("usdtVolume") or 0
    )

    change24h = float(
        ticker.get("change24h") or 0
    ) * 100

    # -----------------------------------------------------
    # 1H MA
    # -----------------------------------------------------

    ma20 = x.ma20
    ma60 = x.ma60
    ma120 = x.ma120

    # -----------------------------------------------------
    # 4H MA
    # -----------------------------------------------------

    h20 = h.ma20
    h60 = h.ma60
    h120 = h.ma120

    atr = float(
        x.atr14
    )

    # -----------------------------------------------------
    # NaN check
    # -----------------------------------------------------

    if any(
        pd.isna(v)
        for v in [
            ma20,
            ma60,
            ma120,
            h20,
            h60,
            h120,
            x.rsi14,
            x.atr14
        ]
    ):

        return None

    # -----------------------------------------------------
    # 거래대금 필터
    # -----------------------------------------------------

    if turnover < MIN_24H_TURNOVER:
        return None

    # -----------------------------------------------------
    # 1H 완전 정배열
    # -----------------------------------------------------

    if not (
        price > ma20 > ma60 > ma120
    ):

        return None

    # -----------------------------------------------------
    # MA 방향
    # -----------------------------------------------------

    ma20_up = bool(
        ma20 > prev.ma20
    )

    ma60_up = bool(
        ma60 > prev.ma60
    )

    ma120_up = bool(
        ma120 >= prev.ma120
    )

    # -----------------------------------------------------
    # 거래량 비율
    # -----------------------------------------------------

    avg20_vol = (
        a.volume.iloc[-21:-1].mean()
    )

    if avg20_vol:

        vol_ratio = float(
            x.volume / avg20_vol
        )

    else:

        vol_ratio = 0.0

    # -----------------------------------------------------
    # MA20 이격
    # -----------------------------------------------------

    ma20_dist = float(
        price / ma20 - 1
    )

    rsi = float(
        x.rsi14
    )

    # -----------------------------------------------------
    # 최근 20봉 고점
    # -----------------------------------------------------

    recent_high_20 = float(
        a.high.iloc[-21:-1].max()
    )

    # -----------------------------------------------------
    # Breakout
    # -----------------------------------------------------

    breakout = bool(
        price >= recent_high_20
    )

    near_breakout = bool(
        not breakout
        and price >= recent_high_20 * 0.99
    )

    # -----------------------------------------------------
    # 4H 완전 정배열
    # -----------------------------------------------------

    h_alignment = bool(
        h.close > h20 > h60 > h120
        and h20 > hprev.ma20
        and h60 >= hprev.ma60
    )

    # -----------------------------------------------------
    # 4H 부분 정배열
    # -----------------------------------------------------

    h_partial = bool(
        h.close > h20
        and h20 > h60
        and h20 > hprev.ma20
    )

    # -----------------------------------------------------
    # Classification
    # -----------------------------------------------------

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

    # =====================================================
    # ENTRY / STOP / TARGET
    # =====================================================

    entry_reference = (
        recent_high_20
        if breakout
        else price
    )

    # -----------------------------------------------------
    # MA20 stop
    # -----------------------------------------------------

    ma_stop = float(
        ma20 * 0.99
    )

    # -----------------------------------------------------
    # ATR stop
    # -----------------------------------------------------

    atr_stop = float(
        entry_reference - 1.5 * atr
    )

    # -----------------------------------------------------
    # 기존 방식 유지
    #
    # MA20 stop과 ATR stop 중
    # 더 아래쪽 가격을 사용한다.
    # =====================================================

    reference_stop = min(
        ma_stop,
        atr_stop
    )

    if reference_stop <= 0:

        reference_stop = ma_stop

    # -----------------------------------------------------
    # Risk
    # -----------------------------------------------------

    risk = float(
        entry_reference -
        reference_stop
    )

    # -----------------------------------------------------
    # 2R
    # -----------------------------------------------------

    target_1 = (
        float(
            entry_reference +
            2 * risk
        )
        if risk > 0
        else float(entry_reference)
    )

    # -----------------------------------------------------
    # 손절거리 %
    # -----------------------------------------------------

    stop_distance_pct = (
        risk /
        entry_reference *
        100
        if entry_reference > 0
        else 0.0
    )

    # =====================================================
    # SCORE
    # =====================================================
    #
    # 총 100점
    #
    # 구조 65점
    # 리스크 35점
    #
    # -----------------------------------------------------
    # 구조 65점
    # -----------------------------------------------------
    #
    # 기본 추세        13
    # MA20 상승         6
    # MA60 상승         4
    # MA120 상승        2
    # 거래량           10
    # 거래대금           8
    # MA20 이격          8
    # RSI                7
    # Breakout           3
    # 4H 구조            4
    #
    # 합계 = 65
    #
    # -----------------------------------------------------
    # 리스크 35점
    #
    # 손절거리 기반
    #
    # =====================================================

    score = 13

    # -----------------------------------------------------
    # MA 방향
    # -----------------------------------------------------

    score += (
        6 if ma20_up else 0
    )

    score += (
        4 if ma60_up else 0
    )

    score += (
        2 if ma120_up else 0
    )

    # -----------------------------------------------------
    # 거래량
    # -----------------------------------------------------

    score += (
        10 if vol_ratio >= 2
        else 8 if vol_ratio >= 1.5
        else 6 if vol_ratio >= MIN_VOL_RATIO
        else 0
    )

    # -----------------------------------------------------
    # 거래대금
    # -----------------------------------------------------

    score += (
        8 if turnover >= 300_000_000
        else 7 if turnover >= 100_000_000
        else 5
    )

    # -----------------------------------------------------
    # MA20 이격
    # -----------------------------------------------------

    score += (
        8 if ma20_dist <= 0.03
        else 7 if ma20_dist <= 0.04
        else 5 if ma20_dist <= 0.05
        else 2 if ma20_dist <= MAX_MA20_DISTANCE
        else 0
    )

    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------

    score += (
        7 if 50 <= rsi <= 68
        else 5 if (
            45 <= rsi < 50
            or 68 < rsi <= 72
        )
        else 2 if 40 <= rsi < 45
        else 0
    )

    # -----------------------------------------------------
    # Breakout
    # -----------------------------------------------------

    score += (
        3 if breakout else 0
    )

    # -----------------------------------------------------
    # 4H
    # -----------------------------------------------------

    score += (
        4 if h_alignment
        else 2 if h_partial
        else 0
    )

    # =====================================================
    # RISK SCORE
    # =====================================================

    risk_score = calculate_risk_score(
        stop_distance_pct
    )

    score += risk_score

    # -----------------------------------------------------
    # 최종 점수
    # -----------------------------------------------------

    score = min(
        int(score),
        100
    )

    # =====================================================
    # RESULT
    # =====================================================

    return {

        "symbol":
            symbol,

        "score":
            score,

        "risk_score":
            risk_score,

        "classification":
            classification,

        "setup_reason":
            setup_reason,

        "price":
            price,

        "entry_reference":
            entry_reference,

        "reference_stop":
            reference_stop,

        "target_1_2R":
            target_1,

        "stop_distance_pct":
            stop_distance_pct,

        "ma20_dist_pct":
            ma20_dist * 100,

        "vol_ratio":
            vol_ratio,

        "turnover_24h":
            turnover,

        "rsi14":
            rsi,

        "atr14":
            atr,

        "breakout_20h":
            breakout,

        "near_breakout":
            near_breakout,

        "4h_alignment":
            h_alignment,

        "24h_change_pct":
            change24h
    }


# =========================================================
# TELEGRAM
# =========================================================

def telegram_send(out):

    token = os.getenv(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = os.getenv(
        "TELEGRAM_CHAT_ID"
    )

    if not token or not chat_id:

        print(
            "⚠️ Telegram Secret이 없습니다."
        )

        return

    rows = out.head(
        TOP_N
    )

    lines = [

        "🔥 BITGET 1H / 4H LONG CANDIDATES v2.2",

        "",

        "📌 USDT-FUTURES",

        "📌 일반 Perpetual",

        "📌 RWA 제외",

        "📌 24H 거래대금 ≥ 10M USDT",

        "",

        "📊 점수 = 구조 65 + 리스크 35",

        "📌 손절폭을 점수에 강하게 반영",

        ""
    ]

    for _, r in rows.iterrows():

        lines.append(

            f"{r['symbol']} | "
            f"{r['score']}점 | "
            f"{r['classification']}\n"

            f"{r['setup_reason']}\n"

            f"현재가: "
            f"{r['price']:.8g}\n"

            f"진입기준: "
            f"{r['entry_reference']:.8g}\n"

            f"손절기준: "
            f"{r['reference_stop']:.8g}\n"

            f"2R 목표: "
            f"{r['target_1_2R']:.8g}\n"

            f"손절거리: "
            f"{r['stop_distance_pct']:.2f}%\n"

            f"리스크점수: "
            f"{r['risk_score']}/35\n"

            f"RSI: "
            f"{r['rsi14']:.1f}\n"

            f"거래량: "
            f"{r['vol_ratio']:.2f}x\n"

        )

    message = "\n".join(
        lines
    )

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    try:

        response = requests.post(

            url,

            data={
                "chat_id": chat_id,
                "text": message
            },

            timeout=10
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):

            raise RuntimeError(
                result
            )

        print(
            "📨 Telegram 전송 완료"
        )

    except Exception as e:

        print(
            f"⚠️ Telegram 전송 실패: {e}"
        )


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "\n"
        "============================================"
    )

    print(
        "🔥 BITGET 1H / 4H LONG SCANNER v2.2"
    )

    print(
        "============================================"
    )

    print(
        "📊 SCORE = STRUCTURE 65 + RISK 35"
    )

    print(
        "📌 손절폭을 점수에 강하게 반영합니다."
    )

    symbols = get_symbols()

    tickers = get_all_tickers()

    # -----------------------------------------------------
    # 거래대금 필터
    # -----------------------------------------------------

    liquid = [

        s

        for s in symbols

        if s in tickers

        and float(
            tickers[s].get(
                "usdtVolume"
            ) or 0
        ) >= MIN_24H_TURNOVER

    ]

    print(
        f"비트겟 USDT-FUTURES "
        f"일반 Perpetual 종목: "
        f"{len(symbols)}개"
    )

    print(
        f"24H 거래대금 "
        f"{MIN_24H_TURNOVER:,.0f} USDT 이상: "
        f"{len(liquid)}개"
    )

    print(
        "1H / 4H 완성봉 기준으로 스캔합니다."
    )

    print(
        "거래량 기준: 평균 대비 "
        f"{MIN_VOL_RATIO:.2f}x 이상"
    )

    print()

    # =====================================================
    # SCAN
    # =====================================================

    results = []

    for i, symbol in enumerate(
        liquid,
        1
    ):

        try:

            df1h = get_candles(
                symbol,
                "1H"
            )

            df4h = get_candles(
                symbol,
                "4H"
            )

            r = analyze(
                symbol,
                df1h,
                df4h,
                tickers[symbol]
            )

            if r:

                results.append(r)

        except Exception as e:

            print(
                f"[skip] "
                f"{symbol}: {e}"
            )

        time.sleep(
            0.03
        )

        if i % 50 == 0:

            print(
                f"진행: "
                f"{i}/{len(liquid)}"
            )

    # =====================================================
    # NO RESULT
    # =====================================================

    if not results:

        print(
            "\n현재 조건을 만족하는 "
            "종목이 없습니다."
        )

        return

    # =====================================================
    # DATAFRAME
    # =====================================================

    out = pd.DataFrame(
        results
    )

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

    # =====================================================
    # SORT
    # =====================================================
    #
    # 1순위 : 총점
    # 2순위 : 손절폭이 짧은 종목
    # 3순위 : 실제 매매 상태
    # 4순위 : 4H 정배열
    # 5순위 : 거래량
    # 6순위 : 거래대금
    #
    # =====================================================

    out = (

        out

        .sort_values(

            [

                "score",

                "stop_distance_pct",

                "class_rank",

                "4h_alignment",

                "vol_ratio",

                "turnover_24h"

            ],

            ascending=[

                False,
                True,
                True,
                False,
                False,
                False

            ]

        )

        .head(
            TOP_N
        )

    )

    # =====================================================
    # DISPLAY
    # =====================================================

    pd.set_option(
        "display.max_columns",
        None
    )

    pd.set_option(
        "display.width",
        260
    )

    cols = [

        "symbol",

        "score",

        "risk_score",

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

    print(
        "\n" + "=" * 145
    )

    print(
        "🔥 BITGET 1H / 4H "
        "LONG CANDIDATES v2.2 TOP 10"
    )

    print(
        "📊 SCORE = STRUCTURE 65 + RISK 35"
    )

    print(
        "📌 RISK SCORE = STOP DISTANCE"
    )

    print(
        "=" * 145
    )

    print(

        out[cols].to_string(

            index=False,

            formatters={

                "price":
                    "{:,.8g}".format,

                "entry_reference":
                    "{:,.8g}".format,

                "reference_stop":
                    "{:,.8g}".format,

                "target_1_2R":
                    "{:,.8g}".format,

                "stop_distance_pct":
                    "{:.2f}%".format,

                "ma20_dist_pct":
                    "{:+.2f}%".format,

                "vol_ratio":
                    "{:.2f}x".format,

                "turnover_24h":
                    "{:,.0f}".format,

                "rsi14":
                    "{:.1f}".format,

                "24h_change_pct":
                    "{:+.2f}%".format

            }

        )

    )

    # =====================================================
    # SAVE CSV
    # =====================================================

    out.drop(
        columns=[
            "class_rank"
        ]
    ).to_csv(

        "bitget_1h_long_candidates_v2_2.csv",

        index=False

    )

    print(
        "\n결과 저장: "
        "bitget_1h_long_candidates_v2_2.csv"
    )

    print(
        "※ 점수는 구조 65 + 리스크 35의 "
        "후보 선별용 참고값입니다."
    )

    print(
        "※ 손절/목표가는 기술적 기준값이며 "
        "실제 체결 가격을 보장하지 않습니다."
    )

    print(
        "※ 자동 주문 기능은 없습니다."
    )

    # =====================================================
    # TELEGRAM
    # =====================================================

    telegram_send(
        out
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    main()
