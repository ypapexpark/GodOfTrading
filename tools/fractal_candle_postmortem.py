#!/usr/bin/env python3
"""프랙탈/캔들패턴 사후 태깅 분석 (라이브 체결 거래에만 대입).

목적: 봇이 실제로 진입했던 거래들의 진입 직전 캔들에 Bill Williams 프랙탈이나
기본 캔들패턴(엔걸핑/해머/도지)이 있었는지 사후 태깅하고, 존재유무에 따라
승률/PnL이 갈리는지 비교한다. 전체 히스토리 백테스트가 아니라 실체결 거래 한정.

라이브 코드(main/trader/config)는 건드리지 않는다. 공개 OHLCV만 재조회(주문 API 아님).
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from collections import defaultdict

import ccxt

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000,
}

_EX_CACHE: dict = {}


def get_ex(venue: str):
    if venue in _EX_CACHE:
        return _EX_CACHE[venue]
    ex = (ccxt.binanceusdm({"enableRateLimit": True})
          if venue == "binance" else ccxt.bybit({"enableRateLimit": True}))
    _EX_CACHE[venue] = ex
    return ex


def load_trades() -> list[dict]:
    """Bybit(현재+아카이브) + Binance 체결 거래를 venue 태그와 함께 모은다."""
    out = []
    sources = [
        ("bybit", "trade_state.json"),
        ("binance", "trade_state_binance.json"),
    ]
    for venue, f in sources:
        if not os.path.exists(f):
            continue
        s = json.load(open(f))
        for t in s.get("trade_history", []):
            if t.get("status") in ("win", "loss"):
                out.append({**t, "_venue": venue})
    for f in sorted(glob.glob("archive/trade_history_*.json")):
        for t in json.load(open(f)):
            if t.get("status") in ("win", "loss"):
                out.append({**t, "_venue": "bybit"})
    # 아카이브는 리셋 시점 스냅샷이라 현재 trade_state.json과 겹칠 수 있다.
    # (timestamp,symbol,direction,status,pnl) 기준으로 중복 제거.
    seen = set()
    deduped = []
    for t in out:
        key = (t.get("_venue"), round(float(t.get("timestamp", 0) or 0), 1),
               t.get("symbol"), t.get("direction"), t.get("status"),
               round(float(t.get("pnl_usd", 0) or 0), 2))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)
    return deduped


def fetch_bars(venue: str, symbol: str, tf: str, entry_ms: int, n_before: int = 8):
    """진입시각 이전의 완결봉 n_before개(+여유)를 가져온다."""
    tfms = TF_MS.get(tf)
    if not tfms:
        return None
    ex = get_ex(venue)
    since = entry_ms - tfms * (n_before + 4)
    for sym in (symbol, f"{symbol}:USDT"):
        try:
            raw = ex.fetch_ohlcv(sym, timeframe=tf, since=since, limit=n_before + 6)
            if raw:
                # 진입봉 이전 완결봉만 (open_time + tfms <= entry_ms => 완결)
                completed = [b for b in raw if b[0] + tfms <= entry_ms]
                return completed[-n_before:] if completed else None
        except Exception:
            continue
    return None


# ── 표준 정의 ────────────────────────────────────────────────────────────────
def bill_williams_fractal(bars: list, direction: str) -> bool:
    """Bill Williams 5봉 프랙탈. 방향 일치하는 확정 프랙탈이 진입 직전에 있었나.

    bars: 진입 직전 완결봉 리스트 [ts,o,h,l,c,v] (오래된→최신).
    LONG: 하단(bullish) 프랙탈 — 가운데 저점이 좌우 2봉씩보다 낮음.
    SHORT: 상단(bearish) 프랙탈 — 가운데 고점이 좌우 2봉씩보다 높음.
    확정은 가운데 봉 +2봉 필요 → center를 뒤에서 3번째 이내로 제한(진입 직전 1~3봉).
    """
    if len(bars) < 5:
        return False
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    n = len(bars)
    # center는 2..n-3 (좌2/우2 필요). 진입 직전성 위해 뒤쪽 3개 center만 검사.
    centers = [c for c in range(2, n - 2) if c >= n - 5]
    for c in centers:
        if direction == "LONG":
            if lows[c] < lows[c-1] and lows[c] < lows[c-2] and \
               lows[c] < lows[c+1] and lows[c] < lows[c+2]:
                return True
        else:  # SHORT
            if highs[c] > highs[c-1] and highs[c] > highs[c-2] and \
               highs[c] > highs[c+1] and highs[c] > highs[c+2]:
                return True
    return False


def candle_patterns(bars: list, direction: str) -> dict:
    """진입 직전 완결봉(마지막봉) 기준 표준 캔들패턴 태깅."""
    res = {"engulfing": False, "hammer": False, "doji": False}
    if len(bars) < 2:
        return res
    o1, h1, l1, c1 = bars[-2][1], bars[-2][2], bars[-2][3], bars[-2][4]
    o2, h2, l2, c2 = bars[-1][1], bars[-1][2], bars[-1][3], bars[-1][4]
    rng = h2 - l2
    if rng <= 0:
        return res
    body = abs(c2 - o2)
    upper = h2 - max(o2, c2)
    lower = min(o2, c2) - l2

    # 도지: 몸통이 전체 범위의 10% 미만
    res["doji"] = body <= 0.10 * rng

    # 엔걸핑: 직전봉 몸통을 현재봉 몸통이 방향맞게 완전히 감쌈
    prev_body_lo, prev_body_hi = min(o1, c1), max(o1, c1)
    cur_body_lo, cur_body_hi = min(o2, c2), max(o2, c2)
    if direction == "LONG":
        res["engulfing"] = (c2 > o2 and c1 < o1 and
                            cur_body_lo <= prev_body_lo and cur_body_hi >= prev_body_hi)
        # 해머: 아래꼬리 >= 몸통 2배, 위꼬리 작음, 몸통 상단부
        res["hammer"] = (lower >= 2 * body and upper <= body and body > 0)
    else:  # SHORT
        res["engulfing"] = (c2 < o2 and c1 > o1 and
                            cur_body_lo <= prev_body_lo and cur_body_hi >= prev_body_hi)
        # 슈팅스타/행잉맨(하락신호): 위꼬리 >= 몸통 2배
        res["hammer"] = (upper >= 2 * body and lower <= body and body > 0)
    return res


def r_multiple(t: dict) -> float | None:
    pnl = t.get("pnl_usd")
    risk = t.get("est_sl_loss")
    if pnl is None or not risk:
        return None
    try:
        return pnl / abs(risk)
    except Exception:
        return None


def summarize(trades: list, label: str):
    n = len(trades)
    if n == 0:
        return f"  {label:28} 표본 0"
    wins = [t for t in trades if t["status"] == "win"]
    wr = len(wins) / n
    pnl = sum(t.get("pnl_usd", 0) or 0 for t in trades)
    avg = pnl / n
    rs = [r for r in (r_multiple(t) for t in trades) if r is not None]
    avg_r = sum(rs) / len(rs) if rs else None
    small = " ⚠️표본부족" if n < 20 else ""
    rtxt = f" avgR={avg_r:+.2f}" if avg_r is not None else ""
    return (f"  {label:28} n={n:3} WR={wr:5.1%} 총PnL=${pnl:+8.2f} "
            f"평균=${avg:+6.3f}{rtxt}{small}")


def main():
    trades = load_trades()
    print(f"수집된 체결(win/loss) 거래: {len(trades)}건 "
          f"(bybit {sum(1 for t in trades if t['_venue']=='bybit')}, "
          f"binance {sum(1 for t in trades if t['_venue']=='binance')})\n")

    tagged = []
    skipped = 0
    for i, t in enumerate(trades):
        sym = t.get("symbol"); tf = t.get("tf"); direction = t.get("direction")
        ts = t.get("timestamp")
        if not (sym and tf and direction and ts):
            skipped += 1
            continue
        entry_ms = int(float(ts) * 1000)
        bars = fetch_bars(t["_venue"], sym, tf, entry_ms)
        if not bars or len(bars) < 5:
            skipped += 1
            continue
        frac = bill_williams_fractal(bars, direction)
        pats = candle_patterns(bars, direction)
        t2 = {**t, "_fractal": frac, **{f"_{k}": v for k, v in pats.items()}}
        tagged.append(t2)
        time.sleep(0.15)  # rate-limit 여유
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(trades)} 처리", file=sys.stderr)

    print(f"태깅 완료: {len(tagged)}건, 데이터부족 스킵: {skipped}건\n")
    if not tagged:
        print("태깅 표본 없음 — 종료")
        return

    print("=== 전체 (Bybit+Binance) ===")
    print(summarize(tagged, "ALL"))
    print()
    print("[Bill Williams 프랙탈]")
    print(summarize([t for t in tagged if t["_fractal"]], "프랙탈 있음(방향일치)"))
    print(summarize([t for t in tagged if not t["_fractal"]], "프랙탈 없음"))
    print()
    print("[캔들패턴 — 진입직전봉]")
    for pat in ("engulfing", "hammer", "doji"):
        key = f"_{pat}"
        print(summarize([t for t in tagged if t.get(key)], f"{pat} 있음"))
    print(summarize([t for t in tagged
                     if not (t.get("_engulfing") or t.get("_hammer") or t.get("_doji"))],
                    "패턴 아무것도 없음"))
    print()
    print("[프랙탈 OR 캔들패턴 = '있음' 통합]")
    has = [t for t in tagged if t["_fractal"] or t.get("_engulfing")
           or t.get("_hammer") or t.get("_doji")]
    no = [t for t in tagged if not (t["_fractal"] or t.get("_engulfing")
          or t.get("_hammer") or t.get("_doji"))]
    print(summarize(has, "신호 있음"))
    print(summarize(no, "신호 없음"))
    print()

    print("=== venue별 분해 ===")
    for venue in ("bybit", "binance"):
        vt = [t for t in tagged if t["_venue"] == venue]
        if not vt:
            continue
        print(f"[{venue}] 전체 {len(vt)}건")
        print(summarize([t for t in vt if t["_fractal"]], " 프랙탈 있음"))
        print(summarize([t for t in vt if not t["_fractal"]], " 프랙탈 없음"))
    print()

    print("=== 방향별 프랙탈 (표본 작을 수 있음) ===")
    for d in ("LONG", "SHORT"):
        dt = [t for t in tagged if t.get("direction") == d]
        print(f"[{d}] 전체 {len(dt)}건")
        print(summarize([t for t in dt if t["_fractal"]], " 프랙탈 있음"))
        print(summarize([t for t in dt if not t["_fractal"]], " 프랙탈 없음"))


if __name__ == "__main__":
    main()
