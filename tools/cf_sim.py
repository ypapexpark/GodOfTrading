#!/usr/bin/env python3
"""Read-only counterfactual sim of blocked gate candidates. No bot-state writes."""
import json, random, sys
from collections import defaultdict
from pathlib import Path
import ccxt
from cf_categorize import categorize, FILES

ROOT = Path(__file__).resolve().parents[1]

random.seed(42)

SL_PCT = {'1m':0.30,'3m':0.30,'5m':0.35,'15m':2.28,'1h':4.56,'4h':9.80,'1d':9.03}
RR = 2.0
HORIZON = 80
TF_MS = {'1m':60,'3m':180,'5m':300,'15m':900,'1h':3600,'4h':14400,'1d':86400}

TARGET = ['GATE1_freshness','GATE2_volume','scalp_freshness','lowtf_reference_only',
          'MTF_higher_counter','MTF_not_aligned','subbar_mismatch','GATE4_missed',
          'GATE3_counter_candle','MTF_soft_fail','subbar_vwap_chase']
N_PER = 70

# crypto-only: tokenized stock/commodity perps (NVDA,TSLA,MU,XAG...) are a separate
# universe and often unfetchable; skip non-USDT-crypto by a symbol heuristic below.
STOCKISH = {'MU','NVDA','TSLA','SPCX','SKHYNIX','XAG','XAU','EVAA','AAPL','AMZN','META',
            'MSFT','GOOGL','COIN','MSTR','HOOD','PLTR','AMD','NFLX','SPY','QQQ','GLD'}

exchanges = {
    'bybit': ccxt.bybit({'enableRateLimit': True}),
    'binance': ccxt.binanceusdm({'enableRateLimit': True}),
}
_cache = {}

def get_window(venue, symbol, tf, ts):
    since = int((ts - TF_MS[tf]*3) * 1000)
    bucket = since // (TF_MS[tf]*1000)
    key = (venue, symbol, tf, bucket)
    if key in _cache:
        return _cache[key]
    try:
        o = exchanges.get(venue, exchanges['bybit']).fetch_ohlcv(
            symbol, tf, since=since, limit=HORIZON+8
        )
    except Exception:
        o = None
    _cache[key] = o
    return o

def simulate(cand):
    sym = cand['symbol']; tf = cand.get('tf'); direction = cand.get('direction')
    entry = cand.get('price'); ts = cand.get('timestamp')
    venue = str(cand.get('venue') or 'bybit').lower()
    base = sym.split('/')[0] if sym else ''
    if base in STOCKISH:
        return 'skip'
    if not (sym and tf in TF_MS and entry and ts and direction in ('LONG','SHORT')):
        return None
    o = get_window(venue, sym, tf, ts)
    if not o or len(o) < 5:
        return None
    tms = ts * 1000
    idx = None
    for i, bar in enumerate(o):
        if bar[0] >= tms - TF_MS[tf]*1000:
            idx = i; break
    if idx is None or idx >= len(o) - 3:
        return None
    slpct = SL_PCT.get(tf, 2.0) / 100.0
    # Account-observed taker fees plus a conservative 3bp each-way slippage.
    round_trip_cost = (0.0016 if venue == 'binance' else 0.0017) / slpct
    if direction == 'LONG':
        sl = entry*(1-slpct); tp = entry*(1+slpct*RR)
    else:
        sl = entry*(1+slpct); tp = entry*(1-slpct*RR)
    fwd = o[idx+1: idx+1+HORIZON]
    if len(fwd) < 3:
        return None
    mfe = mae = 0.0
    for bar in fwd:
        hi, lo, cl = bar[2], bar[3], bar[4]
        if direction == 'LONG':
            mfe = max(mfe, (hi-entry)/entry/slpct); mae = min(mae, (lo-entry)/entry/slpct)
            hit_sl = lo <= sl; hit_tp = hi >= tp
        else:
            mfe = max(mfe, (entry-lo)/entry/slpct); mae = min(mae, (entry-hi)/entry/slpct)
            hit_sl = hi >= sl; hit_tp = lo <= tp
        if hit_sl:
            return {'r':-1.0-round_trip_cost,'win':0,'res':'SL','mfe':mfe,'mae':mae}
        if hit_tp:
            net_r = RR-round_trip_cost
            return {'r':net_r,'win':1 if net_r>0 else 0,'res':'TP','mfe':mfe,'mae':mae}
    cl = fwd[-1][4]
    r = ((cl-entry) if direction=='LONG' else (entry-cl))/entry/slpct - round_trip_cost
    return {'r':r,'win':1 if r>0 else 0,'res':'timeout','mfe':mfe,'mae':mae}

def main():
    by_cat = defaultdict(list)
    for f in FILES:
        for line in (ROOT / f).open(encoding='utf-8'):
            try: d = json.loads(line)
            except: continue
            if d.get('status') != 'blocked': continue
            c = categorize(d.get('reason',''))
            if c in TARGET: by_cat[c].append(d)
    out = {}
    print(f'{"category":22s}  n  cryptoWR   totR   avgR  avgMFE  medMAE  skipStock')
    for c in TARGET:
        pool = by_cat[c]
        # even spread across full time range
        if len(pool) > N_PER*3:
            step = len(pool)/(N_PER*3)
            pool = [pool[int(i*step)] for i in range(N_PER*3)]
        random.shuffle(pool)
        sims=[]; skipped=0
        for cand in pool:
            if len(sims) >= N_PER: break
            r = simulate(cand)
            if r == 'skip': skipped+=1; continue
            if r: sims.append(r)
        out[c]=sims
        n=len(sims)
        if n:
            wr=sum(s['win'] for s in sims)/n
            tot=sum(s['r'] for s in sims); avg=tot/n
            mfe=sum(s['mfe'] for s in sims)/n
            maes=sorted(s['mae'] for s in sims); medmae=maes[len(maes)//2]
            print(f'{c:22s} {n:2d}  {wr:6.1%}  {tot:+6.1f}  {avg:+.3f}  {mfe:+.2f}  {medmae:+.2f}   {skipped}')
        else:
            print(f'{c:22s} {n:2d}  (no simulable)  skipStock={skipped}')
        sys.stdout.flush()
    (ROOT / 'counterfactual_results_latest.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8'
    )

if __name__ == '__main__':
    main()
