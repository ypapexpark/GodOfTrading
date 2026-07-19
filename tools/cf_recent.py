#!/usr/bin/env python3
"""Read-only: last-3d counterfactual by gate x TF group. No bot-state writes."""
import json, random, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cf_categorize import categorize, FILES
import cf_sim

random.seed(7)
N_PER = 55

def tfg(tf):
    return 'scalp' if tf in ('1m','3m','5m','15m') else 'swing'

def main():
    maxts=0
    for f in FILES:
        for line in (ROOT / f).open(encoding='utf-8'):
            try: d=json.loads(line)
            except: continue
            maxts=max(maxts,d.get('timestamp',0))
    cut = maxts - 3*86400

    pools=defaultdict(list)  # (cat,grp)->list
    for f in FILES:
        for line in (ROOT / f).open(encoding='utf-8'):
            try: d=json.loads(line)
            except: continue
            if d.get('status')!='blocked': continue
            if d.get('timestamp',0)<cut: continue
            c=categorize(d.get('reason',''))
            pools[(c,tfg(d.get('tf','')))].append(d)

    targets=[('scalp_freshness','scalp'),('lowtf_reference_only','scalp'),
             ('MTF_higher_counter','scalp'),('MTF_higher_counter','swing'),
             ('GATE1_freshness','swing'),('GATE2_volume','swing'),
             ('subbar_mismatch','swing'),('MTF_not_aligned','swing'),
             ('GATE3_counter_candle','swing'),('GATE4_missed','swing'),
             ('scalp_vwap_hold','scalp')]
    print('%-24s %-5s  n  WR      totR    avgR   avgMFE medMAE skip'%('gate','grp'))
    out={}
    for cat,grp in targets:
        pool=pools.get((cat,grp),[])
        rawn=len(pool)
        if len(pool)>N_PER*4:
            step=len(pool)/(N_PER*4)
            pool=[pool[int(i*step)] for i in range(N_PER*4)]
        random.shuffle(pool)
        sims=[]; skip=0
        for cand in pool:
            if len(sims)>=N_PER: break
            r=cf_sim.simulate(cand)
            if r=='skip': skip+=1; continue
            if r: sims.append(r)
        out[cat+'|'+grp]=sims
        n=len(sims)
        if n:
            wr=sum(s['win'] for s in sims)/n
            tot=sum(s['r'] for s in sims); avg=tot/n
            mfe=sum(s['mfe'] for s in sims)/n
            maes=sorted(s['mae'] for s in sims); mm=maes[len(maes)//2]
            print('%-24s %-5s %3d %5.1f%% %+7.1f %+.3f %+.2f %+.2f  %d  (raw%d)'%(cat,grp,n,wr*100,tot,avg,mfe,mm,skip,rawn))
        else:
            print('%-24s %-5s   0  (nosim) skip%d raw%d'%(cat,grp,skip,rawn))
        sys.stdout.flush()
    (ROOT / 'counterfactual_recent_latest.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8'
    )

if __name__=='__main__':
    main()
