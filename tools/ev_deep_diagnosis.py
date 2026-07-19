#!/usr/bin/env python3
"""전면 EV 재진단 — 실체결 거래의 지표조건별 승률/PnL 분해 (읽기전용).

trade_state.json(+아카이브 dedup) / trade_state_binance.json의 체결기록을 써서
전략/방향/TF/지표조건(indicator_snapshot ok·value)/리스크거버너/신호신선도별로
승률·평균PnL·정규화R을 비교한다. 라이브 코드는 건드리지 않는다.
20건 미만 서브그룹은 결론 보류(표본부족 표시).
"""
from __future__ import annotations

import glob
import json
import os
import re
from collections import defaultdict


def load_trades():
    out = []
    for venue, f in (("bybit", "trade_state.json"), ("binance", "trade_state_binance.json")):
        if os.path.exists(f):
            for t in json.load(open(f)).get("trade_history", []):
                if t.get("status") in ("win", "loss"):
                    out.append({**t, "_venue": venue})
    for f in sorted(glob.glob("archive/trade_history_*.json")):
        for t in json.load(open(f)):
            if t.get("status") in ("win", "loss"):
                out.append({**t, "_venue": "bybit"})
    seen, ded = set(), []
    for t in out:
        k = (t.get("_venue"), round(float(t.get("timestamp", 0) or 0), 1),
             t.get("symbol"), t.get("direction"), t.get("status"),
             round(float(t.get("pnl_usd", 0) or 0), 2))
        if k not in seen:
            seen.add(k)
            ded.append(t)
    return ded


def rr(t):
    p, r = t.get("pnl_usd"), t.get("est_sl_loss")
    if p is None or not r:
        return None
    try:
        return p / abs(r)
    except Exception:
        return None


def stat(trades, label):
    n = len(trades)
    if n == 0:
        return None
    w = sum(1 for t in trades if t["status"] == "win")
    pnl = sum(t.get("pnl_usd", 0) or 0 for t in trades)
    rs = [x for x in (rr(t) for t in trades) if x is not None]
    ar = sum(rs) / len(rs) if rs else None
    flag = " <표본부족" if n < 20 else ""
    art = f" avgR={ar:+.2f}" if ar is not None else ""
    return (f"  {label:34} n={n:3} WR={w/n:5.1%} 총${pnl:+8.2f} "
            f"평균${pnl/n:+6.3f}{art}{flag}")


def grp(trades, keyfn, title, min_show=1):
    print(f"\n[{title}]")
    buckets = defaultdict(list)
    for t in trades:
        k = keyfn(t)
        if k is None:
            continue
        buckets[k].append(t)
    for k in sorted(buckets, key=lambda x: (-len(buckets[x]))):
        if len(buckets[k]) >= min_show:
            print(stat(buckets[k], str(k)))


def ec(t, *path, default=None):
    cur = t.get("entry_context", {})
    if not isinstance(cur, dict):
        return default
    for p in path[:-1]:
        cur = cur.get(p, {})
        if not isinstance(cur, dict):
            return default
    return cur.get(path[-1], default)


def snap_ok(t, ind):
    s = ec(t, "indicator_snapshot", default={}) or {}
    v = s.get(ind)
    return v.get("ok") if isinstance(v, dict) else None


def snap_val(t, ind):
    s = ec(t, "indicator_snapshot", default={}) or {}
    v = s.get(ind)
    return v.get("value") if isinstance(v, dict) else None


def has_risk_gov_cut(t):
    """리스크거버너/장세게이트 감액(리스크×0.xx, x<1)이 걸렸는지."""
    reasons = " ".join(str(r) for r in (t.get("entry_reasons") or []))
    for m in re.findall(r"리스크[×x]([0-9.]+)", reasons):
        try:
            if float(m) < 1.0:
                return True
        except Exception:
            pass
    return False


def main():
    all_t = load_trades()
    by = [t for t in all_t if t["_venue"] == "bybit"]
    print(f"유니크 체결: 전체 {len(all_t)} (bybit {len(by)}, binance {len(all_t)-len(by)})")
    print("=" * 70)
    print("아래 분석은 하드스톱 대상인 BYBIT {}건 중심 (binance는 4건뿐 별도표기)".format(len(by)))
    print(stat(by, "BYBIT 전체"))

    grp(by, lambda t: t.get("core_strategy") or t.get("strategy"), "전략별(core_strategy)")
    grp(by, lambda t: t.get("direction"), "방향별")
    grp(by, lambda t: t.get("tf"), "타임프레임별")
    grp(by, lambda t: t.get("conviction_tier") or ec(t, "conviction_tier"), "확신도티어별")

    print("\n" + "=" * 70)
    print("지표조건별 (indicator_snapshot ok=true/false)")
    for ind in ("rsi", "cci", "macd", "vol", "obv", "srsi", "cvd"):
        okg = [t for t in by if snap_ok(t, ind) is True]
        nog = [t for t in by if snap_ok(t, ind) is False]
        if okg or nog:
            print(f"\n  -- {ind} --")
            if okg:
                print(stat(okg, f"{ind} ok=True"))
            if nog:
                print(stat(nog, f"{ind} ok=False"))

    # RSI value 구간
    def rsi_bucket(t):
        v = snap_val(t, "rsi")
        if v is None:
            return None
        v = float(v)
        if v == 0:
            return None  # 미집계
        return "RSI<35" if v < 35 else "RSI35-50" if v < 50 else "RSI50-65" if v < 65 else "RSI>=65"
    grp(by, rsi_bucket, "RSI 값 구간별")

    # vol_ratio 구간
    def vol_bucket(t):
        v = t.get("vol_ratio") or ec(t, "vol_ratio")
        if v is None:
            return None
        v = float(v)
        return "vol<1.5" if v < 1.5 else "vol1.5-2.5" if v < 2.5 else "vol2.5-5" if v < 5 else "vol>=5"
    grp(by, vol_bucket, "거래량배수(vol_ratio) 구간별")

    # 신호신선도
    def fresh(t):
        b = t.get("bars_ago")
        if b is None:
            b = ec(t, "bars_ago")
        if b is None:
            return None
        b = int(b)
        return f"{b}봉전" if b <= 2 else "3-5봉전" if b <= 5 else "6봉+전"
    grp(by, fresh, "신호신선도(bars_ago)별")

    # 리스크거버너 감액
    print("\n[리스크거버너/장세게이트 감액 여부]")
    print(stat([t for t in by if has_risk_gov_cut(t)], "감액 걸림(리스크×<1)"))
    print(stat([t for t in by if not has_risk_gov_cut(t)], "감액 없음"))

    # ema_aligned, mtf
    grp(by, lambda t: f"ema_aligned={ec(t,'ema_aligned')}", "EMA 방향일치 여부")
    grp(by, lambda t: f"golden={ec(t,'is_golden')}", "황금진입 여부", min_show=1)

    # 최근 5연패 (timestamp 순 정렬 후 마지막 연속 loss)
    print("\n" + "=" * 70)
    print("최근 손실 흐름 (Bybit, 시간순 마지막 8건)")
    bt = sorted(by, key=lambda t: float(t.get("timestamp", 0) or 0))
    for t in bt[-8:]:
        oks = {i: snap_ok(t, i) for i in ("rsi", "cci", "macd", "vol")}
        print(f"  {t.get('time','')[:14]} {t.get('symbol'):11} {t.get('direction'):5} "
              f"{t.get('tf'):3} {t.get('status'):4} ${t.get('pnl_usd',0):+6.2f} "
              f"strat={t.get('core_strategy') or t.get('strategy')} "
              f"vol={t.get('vol_ratio')} bars={t.get('bars_ago')} "
              f"gov_cut={has_risk_gov_cut(t)} ok={oks}")


if __name__ == "__main__":
    main()
