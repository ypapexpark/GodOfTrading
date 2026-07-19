#!/usr/bin/env python3
"""다이버전스 전용 반사실 분석 (분석 전용, 봇 로직 미변경).
1) trade_candidates*.jsonl 에서 status=blocked 이고 signal_type in {hidden_bullish,hidden_bearish,bullish,bearish}
   이며 차단사유가 GATE1 신선도 또는 5m 초단타 참고용 인 것 추출.
2) 원인(5m제외 vs GATE1신선도)으로 분류, 최근 데이터 위주 샘플링.
3) ccxt 공개데이터로 차단시점 이후 캔들 재조회, ATR기반 SL/TP 로 승/패·R 판정.
"""
import json, sys, time, warnings, math, random
warnings.filterwarnings("ignore")
import ccxt

FILES = ["trade_candidates.jsonl", "trade_candidates_binance.jsonl"]
DIV_TYPES = {"hidden_bullish", "hidden_bearish", "bullish", "bearish"}

def classify_reason(r):
    if not r: return None
    if "5m 초단타" in r or "5분봉" in r:
        return "5m_excluded"
    if "GATE1 신선도" in r:
        return "gate1_freshness"
    return None

def load_blocked():
    rows = []
    for f in FILES:
        try:
            fh = open(f, "r")
        except FileNotFoundError:
            continue
        for line in fh:
            if '"blocked"' not in line:
                continue
            if '"hidden_b' not in line and '"bearish"' not in line and '"bullish"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("status") != "blocked":
                continue
            st = d.get("signal_type")
            if st not in DIV_TYPES:
                continue
            cat = classify_reason(d.get("reason"))
            if cat is None:
                continue
            rows.append({
                "venue": d.get("venue"),
                "time": d.get("time"),
                "ts": d.get("timestamp"),
                "symbol": d.get("symbol"),
                "tf": d.get("tf"),
                "signal_type": st,
                "direction": d.get("direction"),
                "confirmed": d.get("confirmed_count"),
                "bars_ago": d.get("bars_ago"),
                "vol_ratio": d.get("vol_ratio"),
                "price": d.get("price"),
                "atr": d.get("atr"),
                "cat": cat,
                "reason": d.get("reason"),
            })
        fh.close()
    return rows

def dedup(rows):
    """동일 (symbol, tf, signal_type, price, bars_ago) 반복 로그를 하나로 (최초 발생시각 기준)."""
    seen = {}
    for r in rows:
        key = (r["symbol"], r["tf"], r["signal_type"], round(r["price"] or 0, 8))
        if key not in seen or (r["ts"] or 0) < (seen[key]["ts"] or 0):
            seen[key] = r
    return list(seen.values())

TF_MS = {"5m":300_000,"15m":900_000,"1h":3_600_000,"4h":14_400_000,"1d":86_400_000}
# 반사실 홀딩: tf별 이후 몇 봉 관찰
HORIZON = {"5m":48,"15m":32,"1h":24,"4h":18,"1d":10}

_exchanges = {}
def get_ex(venue):
    key = "binance" if venue == "binance" else "bybit"
    if key not in _exchanges:
        _exchanges[key] = ccxt.binance({"options":{"defaultType":"future"}}) if key=="binance" else ccxt.bybit()
    return _exchanges[key]

def sym_for(venue, symbol):
    if venue == "binance":
        return symbol.replace("/USDT", "/USDT:USDT") if ":" not in symbol else symbol
    return symbol.replace("/USDT", "/USDT:USDT") if ":" not in symbol else symbol

def simulate(r):
    """차단시점 price 진입 → ATR기반 SL/TP, 이후 캔들로 승패/R 판정."""
    tf = r["tf"]
    if tf not in TF_MS:
        return None
    ex = get_ex(r["venue"])
    sym = sym_for(r["venue"], r["symbol"])
    since = int((r["ts"] - TF_MS[tf]/1000 * 2) * 1000)  # 진입 약간 전부터
    try:
        ohlcv = ex.fetch_ohlcv(sym, tf, since=since, limit=HORIZON[tf] + 10)
    except Exception as e:
        return {"err": str(e)[:40]}
    if not ohlcv or len(ohlcv) < 5:
        return {"err": "no_data"}
    entry_ts_ms = int(r["ts"] * 1000)
    # 진입 봉: 차단시각 이후 첫 봉
    future = [c for c in ohlcv if c[0] >= entry_ts_ms - TF_MS[tf]]
    if len(future) < 3:
        return {"err": "insufficient_future"}
    entry = r["price"]
    atr = r["atr"] or 0
    if not atr or atr <= 0:
        # ATR 없으면 최근 캔들 레인지 평균으로 대체
        rng = [(c[2]-c[3]) for c in ohlcv[:14]]
        atr = sum(rng)/len(rng) if rng else entry*0.01
    long = r["direction"] == "LONG"
    # 전형적 스윙 SL 1.5 ATR, TP 2.0 ATR (R:R ~1.33)  — 단순화
    sl_dist = 1.5 * atr
    tp_dist = 2.0 * atr
    if long:
        sl = entry - sl_dist; tp = entry + tp_dist
    else:
        sl = entry + sl_dist; tp = entry - tp_dist
    obs = future[:HORIZON[tf]]
    outcome = None
    for c in obs:
        hi, lo = c[2], c[3]
        if long:
            hit_sl = lo <= sl; hit_tp = hi >= tp
        else:
            hit_sl = hi >= sl; hit_tp = lo <= tp
        if hit_sl and hit_tp:
            outcome = "loss"; break  # 보수적: 같은봉이면 SL먼저
        if hit_sl:
            outcome = "loss"; break
        if hit_tp:
            outcome = "win"; break
    if outcome is None:
        # 미결: 마지막 종가로 R 산정
        last = obs[-1][4]
        r_mult = ((last-entry) if long else (entry-last)) / sl_dist
        return {"outcome":"open","r":round(r_mult,2)}
    r_mult = (tp_dist/sl_dist) if outcome=="win" else -1.0
    return {"outcome":outcome,"r":round(r_mult,2)}

def main():
    random.seed(42)
    rows = dedup(load_blocked())
    # 시간순 정렬, 최근 위주
    rows.sort(key=lambda x: x["ts"] or 0, reverse=True)
    by_cat = {"5m_excluded":[], "gate1_freshness":[]}
    for r in rows:
        by_cat[r["cat"]].append(r)
    print(f"[분포] 전체 dedup 차단 다이버전스: {len(rows)}건")
    for k,v in by_cat.items():
        tfs = {}
        for r in v:
            tfs[r["tf"]] = tfs.get(r["tf"],0)+1
        print(f"  {k}: {len(v)}건  tf분포={tfs}")

    # 주식/원자재/지수 심볼 제외 (크립토 perp만) — 대문자 티커류 배제 휴리스틱
    STOCK = {"TSLA","NVDA","AAPL","MSFT","AMZN","META","GOOGL","GOOG","CL","GC","SI",
             "SPX","NDX","LAB","MSTR","COIN","AMD","NFLX","PLTR","HOOD","SPY","QQQ"}
    def is_crypto(r):
        base = (r["symbol"] or "").split("/")[0]
        return base not in STOCK

    # 관찰기간이 경과한 신호만 (미결 최소화 위해 horizon의 70%+ 경과)
    now = time.time()
    def elapsed_ok(r):
        need = HORIZON.get(r["tf"],20) * (TF_MS[r["tf"]]/1000) * 0.7
        return r["ts"] and (now - r["ts"]) >= need
    for k in by_cat:
        by_cat[k] = [r for r in by_cat[k] if elapsed_ok(r)]

    # gate1은 tf별로 층화 샘플링(각 tf 목표치), 5m제외는 단일
    results = {"5m_excluded":[], "gate1_freshness":[]}
    def collect(pool, target, bucket):
        random.shuffle(pool)
        tried=0
        for r in pool:
            if len(bucket)>=target: break
            tried+=1
            sim=simulate(r); time.sleep(0.1)
            if not sim or "err" in (sim or {}): continue
            bucket.append((r,sim))
        return tried
    # 5m 제외
    pool5=[r for r in by_cat["5m_excluded"] if is_crypto(r)][:500]
    t5=collect(pool5,30,results["5m_excluded"])
    print(f"[5m_excluded] 시도 {t5} → 유효 {len(results['5m_excluded'])}")
    # gate1 tf 층화: 1h/4h/1d 각 20
    for tf in ("1h","4h","1d"):
        poolg=[r for r in by_cat["gate1_freshness"] if is_crypto(r) and r["tf"]==tf][:800]
        before=len(results["gate1_freshness"])
        collect(poolg,before+20,results["gate1_freshness"])
        print(f"[gate1 {tf}] 유효 누적 {len(results['gate1_freshness'])}")
    print("")

    for cat, res in results.items():
        if not res:
            print(f"=== {cat}: 유효표본 0 ==="); continue
        closed = [x for x in res if x[1]["outcome"] in ("win","loss")]
        opens  = [x for x in res if x[1]["outcome"]=="open"]
        rs = [x[1]["r"] for x in res]
        wins = [x for x in closed if x[1]["outcome"]=="win"]
        avgR = sum(rs)/len(rs) if rs else 0
        wr = len(wins)/len(closed) if closed else 0
        print(f"=== {cat}: 유효 {len(res)}건 (확정 {len(closed)}, 미결 {len(opens)}) ===")
        print(f"    확정 승률 {wr:.0%} | 전체 평균R(미결 종가포함) {avgR:+.3f}")
        closedR = [x[1]['r'] for x in closed]
        if closedR:
            print(f"    확정만 평균R {sum(closedR)/len(closedR):+.3f}")
        # tf별
        tfagg = {}
        for r,sim in res:
            tfagg.setdefault(r["tf"], []).append(sim["r"])
        for tf,v in sorted(tfagg.items()):
            print(f"      {tf}: {len(v)}건 평균R {sum(v)/len(v):+.3f}")
        # 방향별
        diragg={}
        for r,sim in res:
            diragg.setdefault(r["direction"],[]).append(sim["r"])
        for d,v in diragg.items():
            print(f"      {d}: {len(v)}건 평균R {sum(v)/len(v):+.3f}")
        # signal_type별
        stagg={}
        for r,sim in res:
            stagg.setdefault(r["signal_type"],[]).append(sim["r"])
        for st,v in sorted(stagg.items()):
            print(f"      {st}: {len(v)}건 평균R {sum(v)/len(v):+.3f}")

if __name__ == "__main__":
    main()
