import time
import requests
import pandas as pd

BASE = 'https://api.bitget.com'
PRODUCT_TYPE = 'USDT-FUTURES'
MIN_24H_TURNOVER = 30_000_000
MIN_VOL_RATIO = 1.30
MAX_MA20_DISTANCE = 0.06
TOP_N = 10

session = requests.Session()
session.headers.update({'User-Agent': 'Bitget-1H-Long-Scanner-v2.1/1.0'})

def get(path, params=None):
    r = session.get(BASE + path, params=params, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get('code') != '00000':
        raise RuntimeError(j)
    return j['data']

def get_symbols():
    data = get('/api/v2/mix/market/contracts', {'productType': PRODUCT_TYPE})
    return [x['symbol'] for x in data if x.get('quoteCoin') == 'USDT' and x.get('symbolType') != 'delivery']

def get_all_tickers():
    data = get('/api/v2/mix/market/tickers', {'productType': PRODUCT_TYPE})
    return {x['symbol']: x for x in data}

def get_candles(symbol, timeframe, limit=160):
    data = get('/api/v2/mix/market/candles', {'symbol': symbol, 'productType': PRODUCT_TYPE, 'granularity': timeframe, 'limit': str(limit)})
    df = pd.DataFrame(data, columns=['ts','open','high','low','close','volume','quote_volume'])
    for c in ['open','high','low','close','volume','quote_volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['ts'] = pd.to_numeric(df['ts'])
    df = df.sort_values('ts').reset_index(drop=True)
    return df.iloc[:-1].copy() if len(df) > 1 else df

def add_indicators(df):
    df = df.copy()
    for n in (20,60,120):
        df[f'ma{n}'] = df.close.rolling(n).mean()
    delta = df.close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    df['rsi14'] = 100 - (100 / (1 + rs))
    return df

def analyze(symbol, df1h, df4h, ticker):
    if len(df1h) < 125 or len(df4h) < 125:
        return None
    a, b = add_indicators(df1h), add_indicators(df4h)
    x, prev, h, hprev = a.iloc[-1], a.iloc[-2], b.iloc[-1], b.iloc[-2]
    price = float(ticker['lastPr'])
    turnover = float(ticker.get('usdtVolume') or 0)
    ma20, ma60, ma120 = x.ma20, x.ma60, x.ma120
    h20, h60, h120 = h.ma20, h.ma60, h.ma120
    if any(pd.isna(v) for v in [ma20,ma60,ma120,h20,h60,h120,x.rsi14]):
        return None
    if turnover < MIN_24H_TURNOVER:
        return None
    if not (price > ma20 > ma60 > ma120):
        return None
    ma20_up, ma60_up, ma120_up = ma20 > prev.ma20, ma60 > prev.ma60, ma120 >= prev.ma120
    avg20 = a.volume.iloc[-21:-1].mean()
    vol_ratio = float(x.volume / avg20) if avg20 else 0.0
    ma20_dist = float(price / ma20 - 1)
    rsi = float(x.rsi14)
    recent_high = float(a.high.iloc[-21:-1].max())
    breakout = price >= recent_high
    h_alignment = h.close > h20 > h60 > h120 and h20 > hprev.ma20 and h60 >= hprev.ma60
    h_partial = h.close > h20 and h20 > h60 and h20 > hprev.ma20
    if ma20_dist > 0.10:
        chase_reason = 'MA20 +10% 이상'
    elif rsi > 75:
        chase_reason = 'RSI 75 초과'
    elif ma20_dist > MAX_MA20_DISTANCE:
        chase_reason = 'MA20 이격 +6% 초과'
    else:
        chase_reason = ''
    score = 25
    score += 7 if ma20_up else 0
    score += 5 if ma60_up else 0
    score += 3 if ma120_up else 0
    score += 15 if vol_ratio >= 2 else (12 if vol_ratio >= 1.5 else (8 if vol_ratio >= MIN_VOL_RATIO else 0))
    score += 15 if turnover >= 300_000_000 else (12 if turnover >= 100_000_000 else 8)
    score += 10 if ma20_dist <= .03 else (7 if ma20_dist <= .05 else (4 if ma20_dist <= MAX_MA20_DISTANCE else 0))
    score += 10 if 50 <= rsi <= 68 else (6 if 45 <= rsi < 50 or 68 < rsi <= 72 else (3 if 40 <= rsi < 45 else 0))
    score += 5 if breakout else 0
    score += 5 if h_alignment else (2 if h_partial else 0)
    score = min(int(score),100)
    if chase_reason:
        classification = '🔴 추격 금지'
    elif score >= 85 and h_alignment and vol_ratio >= MIN_VOL_RATIO:
        classification = '🟢 A급 후보'
    elif score >= 75 and (h_alignment or h_partial):
        classification = '🟡 B급 후보'
    else:
        classification = '⚪ 관찰'
    return {'symbol':symbol,'score':score,'classification':classification,'price':price,'ma20_dist_pct':ma20_dist*100,'vol_ratio':vol_ratio,'turnover_24h':turnover,'rsi14':rsi,'breakout_20h':breakout,'4h_alignment':h_alignment,'24h_change_pct':float(ticker.get('change24h') or 0)*100,'reference_stop_ma20_minus_1pct':float(ma20*.99),'chase_reason':chase_reason}

def main():
    symbols = get_symbols()
    tickers = get_all_tickers()
    liquid = [s for s in symbols if s in tickers and float(tickers[s].get('usdtVolume') or 0) >= MIN_24H_TURNOVER]
    print(f'비트겟 USDT-FUTURES 전체 종목: {len(symbols)}개')
    print(f'24H USDT 거래대금 {MIN_24H_TURNOVER:,.0f} 이상: {len(liquid)}개')
    print('1H/4H 완성봉 기준으로 스캔합니다.\n')
    results=[]
    for i,symbol in enumerate(liquid,1):
        try:
            r=analyze(symbol,get_candles(symbol,'1H'),get_candles(symbol,'4H'),tickers[symbol])
            if r: results.append(r)
        except Exception as e:
            print(f'[skip] {symbol}: {e}')
        time.sleep(.03)
        if i%50==0: print(f'진행: {i}/{len(liquid)}')
    if not results:
        print('\n현재 조건을 만족하는 종목이 없습니다.'); return
    out=pd.DataFrame(results)
    out['class_rank']=out.classification.map({'🟢 A급 후보':0,'🟡 B급 후보':1,'⚪ 관찰':2,'🔴 추격 금지':3}).fillna(9)
    out=out.sort_values(['class_rank','score','4h_alignment','vol_ratio','turnover_24h'],ascending=[True,False,False,False,False]).head(TOP_N)
    pd.set_option('display.max_columns',None); pd.set_option('display.width',240)
    cols=['symbol','score','classification','price','ma20_dist_pct','vol_ratio','turnover_24h','rsi14','breakout_20h','4h_alignment','24h_change_pct','reference_stop_ma20_minus_1pct','chase_reason']
    print('\n'+'='*110); print('🔥 BITGET 1H / 4H LONG CANDIDATES v2.1 TOP 10'); print('='*110)
    print(out[cols].to_string(index=False,formatters={'price':'{:,.8g}'.format,'ma20_dist_pct':'{:+.2f}%'.format,'vol_ratio':'{:.2f}x'.format,'turnover_24h':'{:,.0f}'.format,'rsi14':'{:.1f}'.format,'24h_change_pct':'{:+.2f}%'.format,'reference_stop_ma20_minus_1pct':'{:,.8g}'.format}))
    out.drop(columns=['class_rank']).to_csv('bitget_1h_long_candidates_v2_1.csv',index=False)
    print('\n결과 저장: bitget_1h_long_candidates_v2_1.csv')
    print('※ A급/B급은 후보 분류이며 매수 신호가 아닙니다.')
    print('※ 자동 주문 기능은 없습니다.')

if __name__=='__main__': main()
