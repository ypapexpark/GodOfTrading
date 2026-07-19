#!/usr/bin/env python3
"""Read-only: categorize blocked candidates by gate reason. No writes to bot state."""
import json, re
from collections import Counter, defaultdict

FILES = ['trade_candidates.jsonl', 'trade_candidates_binance.jsonl']

def categorize(reason: str) -> str:
    r = reason or ''
    if 'GATE1' in r or ('신선도 초과' in r and 'GATE' in r):
        return 'GATE1_freshness'
    if 'GATE2' in r or ('볼륨' in r and 'x <' in r):
        return 'GATE2_volume'
    if 'GATE4' in r or '기회지남' in r:
        return 'GATE4_missed'
    if 'GATE3' in r or '강한 역방향 지속' in r:
        return 'GATE3_counter_candle'
    if '방향일치 봉 없음' in r:
        return 'GATE_no_reversal'
    if '스캘핑 신선도' in r:
        return 'scalp_freshness'
    if '초단타 보조 참고용' in r:
        return 'lowtf_reference_only'
    if 'MTF 전 상위봉 역방향' in r:
        return 'MTF_higher_counter'
    if 'MTF 전정렬 아님' in r or 'MODERATE 알림 전용' in r:
        return 'MTF_not_aligned'
    if 'MTF 역방향 소프트' in r:
        return 'MTF_soft_fail'
    if '화이트리스트 미포함' in r:
        return 'whitelist_excluded'
    if '보조봉' in r and 'VWAP 추격' in r:
        return 'subbar_vwap_chase'
    if '보조봉' in r:
        return 'subbar_mismatch'
    if 'EMA눌림목+돌파 추세 기준 미달' in r or 'EMA눌림목 추세 기준 미달' in r:
        return 'EMA_trend_short'
    if '돌파 추세' in r or '마이크로돌파' in r:
        return 'breakout_trend_short'
    if 'RSI반전 추세 기준 미달' in r:
        return 'RSI_trend_short'
    if '히든다이버전스' in r and 'ELITE 미달' in r:
        return 'hidden_div_elite'
    if 'EMA 중립' in r and 'ELITE' in r:
        return 'ema_neutral_elite'
    if '추세 기준 미달' in r:
        return 'generic_trend_short'
    if '스캘핑 보류' in r and 'VWAP' in r:
        return 'scalp_vwap_hold'
    if '스캘핑 보류' in r:
        return 'scalp_hold'
    # account/risk gates (safety — not removal candidates)
    if '증거금 사용률 한도' in r or '포트폴리오' in r:
        return 'RISK_margin_cap'
    if '일손실한도' in r or 'SL위험' in r:
        return 'RISK_daily_loss'
    if '이미 오픈 포지션' in r:
        return 'RISK_already_open'
    if '과열' in r or 'EXTENSION' in r or '이격' in r:
        return 'RISK_overheat'
    return 'other'

def main():
    cat = Counter()
    by_cat = defaultdict(list)
    for f in FILES:
        for line in open(f):
            try: d = json.loads(line)
            except: continue
            if d.get('status') != 'blocked':
                continue
            c = categorize(d.get('reason', ''))
            cat[c] += 1
            by_cat[c].append(d)
    print('=== Category frequency (blocked only) ===')
    tot = sum(cat.values())
    for k, n in cat.most_common():
        print(f'{n:7d}  {100*n/tot:5.1f}%  {k}')
    print('TOTAL', tot)
    return by_cat

if __name__ == '__main__':
    main()
