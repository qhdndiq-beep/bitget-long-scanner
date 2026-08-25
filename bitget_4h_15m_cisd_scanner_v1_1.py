#!/usr/bin/env python3
"""Bitget 4H -> 15M Structure Scanner v1.6
4H CISD -> 15M swing break -> real pullback -> zone interaction -> confirmation.
No orders are placed. Only completed candles are used.
"""
from __future__ import annotations
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_URL='https://api.bitget.com'; PRODUCT_TYPE='USDT-FUTURES'
HISTORY_LIMIT_4H=200; HISTORY_LIMIT_15M=300; MAX_WORKERS=8
REQUEST_TIMEOUT=12; REQUEST_RETRIES=3
MAX_4H_CISD_BARS=8; MAX_15M_STRUCTURE_BARS=96
SWING_LEFT=2; SWING_RIGHT=2; MIN_STRUCTURE_DISTANCE=3
MAX_PULLBACK_BARS=32; MAX_CONFIRMATION_BARS=16
ZONE_TOLERANCE_PCT=0.0025
MIN_CISD_BODY_RATIO=0.35; MIN_STRUCTURE_BODY_RATIO=0.30; MIN_CONFIRMATION_BODY_RATIO=0.25

@dataclass
class Candle: ts:int; o:float; h:float; l:float; c:float; v:float

@dataclass
class Signal:
    symbol:str; direction:str; score:int; status:str; price:float
    cisd_ts:int; cisd_level:float; structure_ts:int; structure_level:float
    pullback_ts:int; confirmation_ts:int; cisd_sweep:bool
    fvg_4h:bool; ob_4h:bool; fvg_15m:bool; ob_15m:bool; structure_retest:bool
    entry_low:Optional[float]; entry_high:Optional[float]; notes:List[str]


def get_json(path:str, params:Dict[str,Any])->Dict[str,Any]:
    url=f'{BASE_URL}{path}?{urlencode(params)}'; err=None

    for n in range(REQUEST_RETRIES):
        try:
            req=Request(
                url,
                headers={
                    'User-Agent':'bitget-4h15m-structure-scanner/1.6',
                    'Accept':'application/json'
                },
                method='GET'
            )

            with urlopen(req,timeout=REQUEST_TIMEOUT) as r:
                data=json.loads(r.read().decode())

            if data.get('code')!='00000':
                raise RuntimeError(
                    f"{data.get('code')} {data.get('msg')}"
                )

            return data

        except (
            HTTPError,
            URLError,
            TimeoutError,
            json.JSONDecodeError,
            RuntimeError
        ) as e:

            err=e
            time.sleep(.7*(n+1))

    raise RuntimeError(
        f'Request failed: {url} :: {err}'
    )


def get_symbols()->List[str]:

    d=get_json(
        '/api/v3/market/instruments',
        {'category':PRODUCT_TYPE}
    )

    out=[]

    for x in d.get('data',[]):

        s=str(
            x.get('symbol','')
        ).strip()

        q=str(
            x.get('quoteCoin','')
        ).upper().strip()

        st=str(
            x.get('symbolType','')
        ).lower().strip()

        typ=str(
            x.get('type','')
        ).lower().strip()

        status=str(
            x.get('status','')
        ).lower().strip()

        rwa=str(
            x.get('isRwa','YES')
        ).upper().strip()

        if (
            s.endswith('USDT')
            and q=='USDT'
            and st=='crypto'
            and typ=='perpetual'
            and status=='online'
            and rwa=='NO'
        ):
            out.append(s)

    out=sorted(set(out))

    print(
        f'[INFO] selected symbols: {len(out)}'
    )

    return out


def get_candles(
    symbol:str,
    tf:str,
    limit:int
)->List[Candle]:

    d=get_json(
        '/api/v2/mix/market/history-candles',
        {
            'symbol':symbol,
            'productType':PRODUCT_TYPE,
            'granularity':tf,
            'limit':limit
        }
    )

    interval={
        '4H':14400000,
        '15m':900000
    }[tf]

    now=int(
        time.time()*1000
    )

    out=[]

    for r in d.get('data',[]):

        if len(r)>=6:

            c=Candle(
                int(r[0]),
                float(r[1]),
                float(r[2]),
                float(r[3]),
                float(r[4]),
                float(r[5])
            )

            if c.ts+interval<=now:
                out.append(c)

    return sorted(
        out,
        key=lambda x:x.ts
    )


def bull(c:Candle)->bool:
    return c.c>c.o


def bear(c:Candle)->bool:
    return c.c<c.o


def body_ratio(c:Candle)->float:

    return (
        abs(c.c-c.o)
        /
        max(c.h-c.l,1e-12)
    )


def swing_high(
    cs:List[Candle],
    i:int
)->bool:

    return (
        i>=SWING_LEFT
        and
        i+SWING_RIGHT<len(cs)
        and
        cs[i].h>=max(
            x.h
            for x in cs[
                i-SWING_LEFT:i
            ]
        )
        and
        cs[i].h>=max(
            x.h
            for x in cs[
                i+1:i+SWING_RIGHT+1
            ]
        )
    )


def swing_low(
    cs:List[Candle],
    i:int
)->bool:

    return (
        i>=SWING_LEFT
        and
        i+SWING_RIGHT<len(cs)
        and
        cs[i].l<=min(
            x.l
            for x in cs[
                i-SWING_LEFT:i
            ]
        )
        and
        cs[i].l<=min(
            x.l
            for x in cs[
                i+1:i+SWING_RIGHT+1
            ]
        )
    )


def latest_swing(
    cs:List[Candle],
    start:int,
    before:int,
    direction:str
):

    for i in range(
        min(
            before-SWING_RIGHT,
            len(cs)-SWING_RIGHT-1
        ),
        max(start,SWING_LEFT)-1,
        -1
    ):

        if (
            direction=='LONG'
            and
            swing_high(cs,i)
        ):

            return {
                'index':i,
                'ts':cs[i].ts,
                'level':cs[i].h
            }

        if (
            direction=='SHORT'
            and
            swing_low(cs,i)
        ):

            return {
                'index':i,
                'ts':cs[i].ts,
                'level':cs[i].l
            }

    return None


def find_cisd(
    cs:List[Candle],
    direction:str
):

    end=len(cs)-1

    start=max(
        2,
        end-MAX_4H_CISD_BARS+1
    )

    for i in range(
        end,
        start-1,
        -1
    ):

        cur,ref=cs[i],cs[i-1]

        if direction=='LONG':

            if not (
                bull(cur)
                and
                bear(ref)
                and
                cur.c>ref.h
            ):
                continue

            if (
                body_ratio(cur)
                <
                MIN_CISD_BODY_RATIO
            ):
                continue

            sweep=False

            for j in range(
                max(2,i-8),
                i
            ):

                p=cs[
                    max(0,j-8):j
                ]

                if (
                    p
                    and
                    cs[j].l
                    <
                    min(x.l for x in p)
                    and
                    cs[j].c
                    >
                    min(x.l for x in p)
                ):

                    sweep=True
                    break

            return {
                'index':i,
                'ts':cur.ts,
                'level':ref.h,
                'sweep':sweep
            }

        else:

            if not (
                bear(cur)
                and
                bull(ref)
                and
                cur.c<ref.l
            ):
                continue

            if (
                body_ratio(cur)
                <
                MIN_CISD_BODY_RATIO
            ):
                continue

            sweep=False

            for j in range(
                max(2,i-8),
                i
            ):

                p=cs[
                    max(0,j-8):j
                ]

                if (
                    p
                    and
                    cs[j].h
                    >
                    max(x.h for x in p)
                    and
                    cs[j].c
                    <
                    max(x.h for x in p)
                ):

                    sweep=True
                    break

            return {
                'index':i,
                'ts':cur.ts,
                'level':ref.l,
                'sweep':sweep
            }

    return None


def find_structure(
    cs:List[Candle],
    direction:str,
    start:int
):

    end=min(
        len(cs)-1,
        start+MAX_15M_STRUCTURE_BARS
    )

    for bi in range(
        max(
            start,
            SWING_LEFT+SWING_RIGHT+1
        ),
        end+1
    ):

        cur=cs[bi]

        if (
            body_ratio(cur)
            <
            MIN_STRUCTURE_BODY_RATIO
        ):
            continue

        sw=latest_swing(
            cs,
            start,
            bi,
            direction
        )

        if (
            not sw
            or
            bi-sw['index']
            <
            MIN_STRUCTURE_DISTANCE
        ):
            continue

        if (
            direction=='LONG'
            and
            bull(cur)
            and
            cur.c>sw['level']
        ):

            return {
                'index':bi,
                'ts':cur.ts,
                'level':sw['level'],
                'swing_index':sw['index']
            }

        if (
            direction=='SHORT'
            and
            bear(cur)
            and
            cur.c<sw['level']
        ):

            return {
                'index':bi,
                'ts':cur.ts,
                'level':sw['level'],
                'swing_index':sw['index']
            }

    return None


def fvgs(
    cs:List[Candle],
    direction:str,
    max_age:int=60
):

    z=[]

    start=max(
        2,
        len(cs)-max_age
    )

    for i in range(
        start,
        len(cs)
    ):

        a,c=cs[i-2],cs[i]

        if (
            direction=='LONG'
            and
            a.h<c.l
        ):

            z.append(
                ('FVG',a.h,c.l)
            )

        if (
            direction=='SHORT'
            and
            a.l>c.h
        ):

            z.append(
                ('FVG',c.h,a.l)
            )

    return z


def obs(
    cs:List[Candle],
    direction:str,
    max_age:int=80
):

    z=[]

    start=max(
        1,
        len(cs)-max_age
    )

    for i in range(
        start,
        len(cs)-1
    ):

        a,b=cs[i],cs[i+1]

        if (
            direction=='LONG'
            and
            bear(a)
            and
            bull(b)
            and
            b.c>a.h
        ):

            z.append(
                ('OB',a.l,a.o)
            )

        if (
            direction=='SHORT'
            and
            bull(a)
            and
            bear(b)
            and
            b.c<a.l
        ):

            z.append(
                ('OB',a.o,a.h)
            )

    return z


def hit(
    c:Candle,
    zone
)->bool:

    _,lo,hi=zone

    lo,hi=min(lo,hi),max(lo,hi)

    mid=(lo+hi)/2

    tol=max(
        mid*ZONE_TOLERANCE_PCT,
        (hi-lo)*.15
    )

    return (
        c.h>=lo-tol
        and
        c.l<=hi+tol
    )


def pullback_confirmation(
    cs:List[Candle],
    direction:str,
    structure:Dict[str,Any]
):

    si=structure['index']

    level=structure['level']

    end=min(
        len(cs)-1,
        si+MAX_PULLBACK_BARS
    )

    rel=cs[
        max(0,si-30):
        end+1
    ]

    f=fvgs(
        rel,
        direction,
        40
    )

    o=obs(
        rel,
        direction,
        40
    )

    for pi in range(
        si+1,
        end+1
    ):

        c=cs[pi]

        if direction=='LONG':

            retest=(
                c.l
                <=
                level*(1+ZONE_TOLERANCE_PCT)
                and
                c.h
                >=
                level*(1-ZONE_TOLERANCE_PCT)
            )

            recovered=c.c>=level

        else:

            retest=(
                c.h
                >=
                level*(1-ZONE_TOLERANCE_PCT)
                and
                c.l
                <=
                level*(1+ZONE_TOLERANCE_PCT)
            )

            recovered=c.c<=level

        fh=any(
            hit(c,z)
            for z in f
        )

        oh=any(
            hit(c,z)
            for z in o
        )

        # A valid pullback is allowed to interact
        # with broken structure or a nearby FVG/OB.
        if (
            retest
            or
            fh
            or
            oh
        ):

            for ci in range(
                pi+1,
                min(
                    len(cs)-1,
                    pi+MAX_CONFIRMATION_BARS
                )+1
            ):

                cur,prev=cs[ci],cs[ci-1]

                if direction=='LONG':

                    ok=(
                        bull(cur)
                        and
                        cur.c>prev.h
                        and
                        cur.c>=level
                        and
                        body_ratio(cur)
                        >=
                        MIN_CONFIRMATION_BODY_RATIO
                    )

                else:

                    ok=(
                        bear(cur)
                        and
                        cur.c<prev.l
                        and
                        cur.c<=level
                        and
                        body_ratio(cur)
                        >=
                        MIN_CONFIRMATION_BODY_RATIO
                    )

                if ok:

                    return {
                        'pullback_index':pi,
                        'pullback_ts':c.ts,
                        'confirmation_index':ci,
                        'confirmation_ts':cur.ts,
                        'structure':retest,
                        'fvg':fh,
                        'ob':oh
                    }

    return None


def analyze(
    symbol:str
)->List[Signal]:

    try:

        c4=get_candles(
            symbol,
            '4H',
            HISTORY_LIMIT_4H
        )

        c15=get_candles(
            symbol,
            '15m',
            HISTORY_LIMIT_15M
        )

        if (
            len(c4)<40
            or
            len(c15)<100
        ):

            return []

        out=[]

        for d in (
            'LONG',
            'SHORT'
        ):

            cisd=find_cisd(
                c4,
                d
            )

            if not cisd:
                continue

            start=next(
                (
                    i
                    for i,c in enumerate(c15)
                    if c.ts>cisd['ts']
                ),
                len(c15)
            )

            if (
                start
                >=
                len(c15)-10
            ):
                continue

            st=find_structure(
                c15,
                d,
                start
            )

            if not st:
                continue

            pc=pullback_confirmation(
                c15,
                d,
                st
            )

            if not pc:
                continue

            c4a=c4[
                cisd['index']:
            ]

            f4=bool(
                fvgs(
                    c4a,
                    d,
                    40
                )
            )

            o4=bool(
                obs(
                    c4a,
                    d,
                    50
                )
            )

            score=(
                70
                +(7 if cisd['sweep'] else 0)
                +(3 if f4 else 0)
                +(3 if o4 else 0)
                +(5 if pc['fvg'] else 0)
                +(5 if pc['ob'] else 0)
                +(3 if pc['structure'] else 0)
            )

            lo,hi=(
                st['level'],
                st['level']
            )

            p=c15[
                pc['pullback_index']
            ]

            lo=min(
                lo,
                p.l
            )

            hi=max(
                hi,
                p.h
            )

            out.append(
                Signal(
                    symbol=d and symbol,
                    direction=d,
                    score=min(score,100),
                    status='ENTRY_CANDIDATE',
                    price=c15[-1].c,

                    cisd_ts=cisd['ts'],
                    cisd_level=cisd['level'],

                    structure_ts=st['ts'],
                    structure_level=st['level'],

                    pullback_ts=
                        pc['pullback_ts'],

                    confirmation_ts=
                        pc['confirmation_ts'],

                    cisd_sweep=
                        cisd['sweep'],

                    fvg_4h=f4,
                    ob_4h=o4,

                    fvg_15m=pc['fvg'],
                    ob_15m=pc['ob'],

                    structure_retest=
                        pc['structure'],

                    entry_low=lo,
                    entry_high=hi,

                    notes=[
                        '4H CISD',
                        '15M swing structure break',
                        '15M pullback',
                        '15M confirmation'
                    ]
                )
            )

        return out

    except Exception as e:

        print(
            f'[WARN] {symbol}: {e}',
            file=sys.stderr
        )

        return []


def price(
    x
):

    if x is None:
        return '-'

    if x>=1000:
        return f'{x:,.2f}'

    if x>=1:
        return f'{x:,.4f}'

    return f'{x:.8f}'


def ts(x):

    return datetime.fromtimestamp(
        x/1000,
        tz=timezone.utc
    ).strftime(
        '%m-%d %H:%M UTC'
    )


def format_signal(
    s:Signal
)->str:

    icon=(
        '🟢'
        if s.direction=='LONG'
        else
        '🔴'
    )

    return (
        f'{icon} {s.symbol} | '
        f'{s.direction}\n'

        f'점수 {s.score} | '
        f'{s.status}\n'

        f'현재가 '
        f'{price(s.price)}\n'

        f'4H CISD '
        f'{ts(s.cisd_ts)} | '
        f'level '
        f'{price(s.cisd_level)} | '
        f'sweep '
        f'{"✓" if s.cisd_sweep else "-"}\n'

        f'4H FVG '
        f'{"✓" if s.fvg_4h else "-"} | '
        f'OB '
        f'{"✓" if s.ob_4h else "-"}\n'

        f'15M SWING STRUCTURE ✓ | '
        f'{ts(s.structure_ts)} | '
        f'level '
        f'{price(s.structure_level)}\n'

        f'15M PULLBACK ✓ | '
        f'{ts(s.pullback_ts)} | '
        f'structure '
        f'{"✓" if s.structure_retest else "-"} | '
        f'FVG '
        f'{"✓" if s.fvg_15m else "-"} | '
        f'OB '
        f'{"✓" if s.ob_15m else "-"}\n'

        f'15M CONFIRMATION ✓ | '
        f'{ts(s.confirmation_ts)}\n'

        f'entry zone: '
        f'{price(s.entry_low)} ~ '
        f'{price(s.entry_high)}'
    )


def telegram(
    text:str
):

    token=os.getenv(
        'TELEGRAM_BOT_TOKEN',
        ''
    ).strip()

    chat=os.getenv(
        'TELEGRAM_CHAT_ID',
        ''
    ).strip()

    if not token or not chat:
        return

    data=urlencode(
        {
            'chat_id':chat,
            'text':text,
            'disable_web_page_preview':'true'
        }
    ).encode()

    req=Request(
        f'https://api.telegram.org/bot{token}/sendMessage',
        data=data,
        headers={
            'Content-Type':
                'application/x-www-form-urlencoded'
        },
        method='POST'
    )

    with urlopen(
        req,
        timeout=REQUEST_TIMEOUT
    ) as r:

        r.read()


def main():

    t=time.time()

    print(
        '\n'
        '============================================\n'
        ' Bitget 4H -> 15M Structure Scanner v1.6\n'
        '============================================'
    )

    print(
        '4H CISD -> '
        '15M SWING STRUCTURE -> '
        'PULLBACK -> '
        'CONFIRMATION'
    )

    symbols=get_symbols()

    signals=[]

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as ex:

        fs={
            ex.submit(
                analyze,
                s
            ):s

            for s in symbols
        }

        done=0

        for f in as_completed(fs):

            done+=1

            try:

                signals.extend(
                    f.result()
                )

            except Exception as e:

                print(
                    f'[WARN] worker: {e}',
                    file=sys.stderr
                )

            if done%50==0:

                print(
                    f'[INFO] progress '
                    f'{done}/{len(symbols)}'
                )

    signals.sort(
        key=lambda x:(
            -x.score,
            x.symbol,
            x.direction
        )
    )

    now=datetime.now(
        timezone.utc
    ).strftime(
        '%Y-%m-%d %H:%M UTC'
    )

    if not signals:

        report=(
            f'🔎 '
            f'4H→15M Structure Scanner v1.6\n'
            f'{now}\n\n'

            f'스캔 {len(symbols)}개\n'
            f'🔥 유효 후보 없음\n\n'

            f'조건:\n'
            f'4H CISD ✓\n'
            f'15M Swing Structure ✓\n'
            f'15M Pullback ✓\n'
            f'15M Confirmation ✓'
        )

    else:

        report='\n\n'.join(
            [
                f'🔎 '
                f'4H→15M Structure Scanner v1.6\n'
                f'{now}\n'
                f'스캔 {len(symbols)}개\n'
                f'🔥 유효 후보 '
                f'{len(signals)}개'
            ]
            +
            [
                format_signal(s)
                for s in signals[:10]
            ]
        )

    print(
        '\n'+report
    )

    try:

        telegram(
            report
        )

    except Exception as e:

        print(
            f'[WARN] Telegram failed: {e}',
            file=sys.stderr
        )

    with open(
        'structure_scan_results.json',
        'w',
        encoding='utf-8'
    ) as f:

        json.dump(
            [
                asdict(s)
                for s in signals
            ],
            f,
            ensure_ascii=False,
            indent=2
        )

    print(
        f'\n[INFO] elapsed: '
        f'{time.time()-t:.1f}s'
    )


if __name__=='__main__':
    main()
