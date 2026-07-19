#!/usr/bin/env python3
"""5%p 래칫 트레일링 스톱 소급 시뮬레이션 (읽기전용).

제안 규칙: 포지션 마진ROI가 +10% 도달 시 SL을 +10% 지점으로, +15%면 +15%로...
5%p 단위로 계속 래칫(한 번 넘은 문턱 아래로는 안 뺏김). 각 실체결 거래의 진입~청산
구간 실제 캔들(ccxt 공개데이터)을 재조회해 이 규칙 적용 시 청산시점/PnL을 재구성하고
실제 실현PnL과 비교한다. 라이브 코드 무수정.

마진ROI 기준 이유: 레버리지 8~15x라 "수익 10%"를 가격 10%로 보면 15m 알트가 거의
도달 못함 → 사용자가 보는 손익(마진ROI)=가격이동×레버리지 기준이 자연스럽고 유의미.
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import time

import ccxt

spec = importlib.util.spec_from_file_location("m", os.path.join(os.path.dirname(__file__), "ev_deep_diagnosis.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
         "1d": 86_400_000, "3m": 180_000, "30m": 1_800_000}
ROUND_TRIP_FEE = 0.0011
THRESHOLDS = [10, 15, 20, 25, 30, 40, 50, 70, 100, 150, 200, 300, 500]

_EX = {}


def get_ex(venue):
    if venue not in _EX:
        _EX[venue] = (ccxt.binanceusdm({"enableRateLimit": True})
                      if venue == "binance" else ccxt.bybit({"enableRateLimit": True}))
    return _EX[venue]


def exit_ms(t):
    ci = t.get("close_info") or {}
    ut = ci.get("updatedTime")
    if ut:
        try:
            return int(ut)
        except Exception:
            pass
    return None


def fetch_path(venue, symbol, tf, entry_ms, end_ms):
    tfms = TF_MS.get(tf)
    if not tfms:
        return None
    ex = get_ex(venue)
    span_bars = int((end_ms - entry_ms) / tfms) + 3 if end_ms else 60
    span_bars = max(3, min(span_bars, 300))
    for sym in (symbol, f"{symbol}:USDT"):
        try:
            raw = ex.fetch_ohlcv(sym, timeframe=tf, since=entry_ms - tfms, limit=span_bars + 4)
            if raw:
                # 진입 이후 봉만 (진입시각 포함 봉부터)
                path = [b for b in raw if b[0] + tfms > entry_ms and (end_ms is None or b[0] <= end_ms + tfms)]
                return path or None
        except Exception:
            continue
    return None


def simulate(t):
    """래칫 규칙 적용 시 (sim_exit_roi_pct, exit_kind) 반환. 실패 시 None."""
    direction = t.get("direction")
    entry = float(t.get("entry_price") or 0)
    sl = float(t.get("sl") or 0)
    lev = float(t.get("leverage") or 1)
    if entry <= 0 or sl <= 0 or direction not in ("LONG", "SHORT"):
        return None
    entry_ms = int(float(t.get("timestamp", 0) or 0) * 1000)
    e_ms = exit_ms(t)
    path = fetch_path(t["_venue"], t.get("symbol"), t.get("tf"), entry_ms, e_ms)
    if not path:
        return None

    def roi(price):  # 마진ROI% (부호 방향 반영)
        mv = (price / entry - 1) if direction == "LONG" else (1 - price / entry)
        return mv * lev * 100

    sl_roi = roi(sl)  # 음수
    locked = None     # 현재 잠긴 문턱(마진ROI%)
    for b in path:
        _, o, hi, lo, c, v = b
        fav = roi(hi if direction == "LONG" else lo)   # 유리한 극단
        adv = roi(lo if direction == "LONG" else hi)   # 불리한 극단
        # 1) 유리한 극단으로 래칫 상향
        for th in THRESHOLDS:
            if fav >= th:
                locked = th if locked is None else max(locked, th)
        # 2) 불리한 극단으로 스톱 체크
        stop = locked if locked is not None else sl_roi
        if adv <= stop:
            return stop, ("래칫청산" if locked is not None else "원SL청산")
    # 창 내 미청산 → 마지막 봉 종가로 청산(실제 청산과 동일 취급)
    return roi(path[-1][4]), "창끝종가"


def net_pnl(margin, lev, exit_roi_pct):
    return margin * (exit_roi_pct / 100.0) - margin * lev * ROUND_TRIP_FEE


def main():
    by = [t for t in m.load_trades() if t["_venue"] == "bybit"]
    WL = {"EMA눌림목+거래량급등", "EMA눌림목+돌파", "EMA눌림목+거래량급등+돌파",
          "EMA눌림목+BB중단", "hidden_bullish", "hidden_bearish"}

    def is_ema(t):
        return str(t.get("strategy") or "").startswith("EMA눌림목")

    rows = []
    for i, t in enumerate(by):
        r = simulate(t)
        time.sleep(0.12)
        if r is None:
            continue
        sim_roi, kind = r
        margin = float(t.get("margin") or 0)
        lev = float(t.get("leverage") or 1)
        sim_pnl = net_pnl(margin, lev, sim_roi)
        rows.append({
            "t": t, "actual": float(t.get("pnl_usd", 0) or 0),
            "sim": sim_pnl, "sim_roi": sim_roi, "kind": kind,
            "ema": is_ema(t) and t.get("strategy") in WL,
        })
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(by)}", flush=True)

    def report(rs, label):
        if not rs:
            print(f"  {label}: n=0")
            return
        a = sum(x["actual"] for x in rs)
        s = sum(x["sim"] for x in rs)
        aw = sum(1 for x in rs if x["actual"] > 0)
        sw = sum(1 for x in rs if x["sim"] > 0)
        print(f"  {label:26} n={len(rs):3} | 실제 총${a:+7.2f}(승{aw}) | "
              f"래칫 총${s:+7.2f}(승{sw}) | 차이${s-a:+7.2f}")

    print(f"\n시뮬 완료 {len(rows)}/{len(by)}건\n" + "=" * 74)
    report(rows, "전체(Bybit)")
    report([x for x in rows if x["ema"]], " EMA계열만")
    report([x for x in rows if x["t"].get("status") == "win"], " 실제 승리거래")
    report([x for x in rows if x["t"].get("status") == "loss"], " 실제 패배거래")
    print()
    # 청산유형 분포
    from collections import Counter
    print("래칫 청산유형:", dict(Counter(x["kind"] for x in rows)))
    print()
    # 큰 추세 승리가 래칫으로 깎였나: 실제 pnl 상위 거래들
    print("실제 수익 상위 8건 — 래칫이 깎았는지:")
    for x in sorted(rows, key=lambda r: -r["actual"])[:8]:
        t = x["t"]
        print(f"  {t.get('symbol'):11} {t.get('direction'):5} {t.get('tf'):3} lev{int(t.get('leverage',1)):>2} "
              f"실제${x['actual']:+6.2f} → 래칫${x['sim']:+6.2f}(ROI{x['sim_roi']:+.0f}%,{x['kind']}) "
              f"{'⬇깎임' if x['sim']<x['actual']-0.01 else '⬆개선' if x['sim']>x['actual']+0.01 else '='}")
    print("\n얕은 승리/작은손실(실제 -0.5~+0.5) — 래칫 영향:")
    shallow = [x for x in rows if -0.5 <= x["actual"] <= 0.5]
    report(shallow, " 얕은구간")


if __name__ == "__main__":
    main()
