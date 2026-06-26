"""
패인 트레이드 분석 + 적응형 매매 필터 (자동 학습 엔진)

학습 항목:
  1. 전체 승률 → MIN_RR 자동 조정
  2. TF별 승률 → 저성과 TF 일시 제외
  3. 심볼 3연패 → 해당 코인 일시 제외
  4. STRONG 신호 패배 → confirmed 요구치 상향
  5. 역추세 진입 패배 → 역추세 통계 누적 (이미 5/5 강제 적용)
  6. 저볼륨 손실 패턴 → min_vol_ratio 자동 상향
  7. 오래된 신호 손실 패턴 → swing_freshness 자동 강화
"""
import time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

ANALYSIS_LOOKBACK  = 20
MIN_TRADES         = 5
MAX_MIN_RR         = 2.5
SKIP_DURATION_H    = 3    # 12 → 3시간: 스캘핑 TF 차단이 너무 길어 기회 소멸
MAX_VOL_RATIO      = 2.0   # 2.5 → 2.0: 상한 낮춤 (2.5는 대부분 신호 차단)
MIN_VOL_RATIO      = 1.5   # min_vol_ratio 하한 (완화 시 이 값까지만)

# SL 1.5 ATR 기준: 코인별 ATR% 감안. 너무 넓다 = 3.0 ATR 이상 (리스크 과대)
WIDE_SL_BY_TF = {"5m": 3.0, "15m": 4.0, "1h": 5.0, "4h": 8.0, "1d": 12.0}
TF_HOURS      = {"5m": 5/60, "15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}


def _load():
    from trader import _load_state
    return _load_state()

def _save(s: dict):
    from trader import _save_state
    _save_state(s)


def get_adaptive_filters() -> dict:
    defaults = {
        "min_rr":              1.5,
        "min_confirmed_by_tf": {},
        "skip_tfs":            {},
        "skip_symbols":        {},
        "min_vol_ratio":       1.5,    # 스윙 진입 볼륨 기준 (학습으로 자동 조정)
        "swing_freshness":     {},     # TF별 신선도 한도 덮어쓰기 (학습으로 강화)
        "ema_aligned_boost":   1.0,    # EMA 방향일치 진입 시 포지션 배율 (학습)
        "adjustments_log":     [],
    }
    s = _load()
    adaptive = s.get("adaptive", {})
    for k, v in defaults.items():
        adaptive.setdefault(k, v)
    return adaptive


def _save_adaptive(filters: dict):
    s = _load()
    s["adaptive"] = filters
    _save(s)


# ─── 승리 패턴 추출 ──────────────────────────────────────────────────────────

def _extract_win_patterns(recent: list) -> dict:
    """최근 거래에서 승리 공통 조건 추출 — 학습 강화 및 브리핑에 활용."""
    wins   = [t for t in recent if t["status"] == "win"]
    losses = [t for t in recent if t["status"] == "loss"]
    if not wins:
        return {}

    def _ema_aligned(t):
        return (
            (t.get("direction") == "LONG"  and t.get("ema_trend") == 1) or
            (t.get("direction") == "SHORT" and t.get("ema_trend") == -1)
        )

    ema_wins  = [t for t in wins   if _ema_aligned(t)]
    ema_all   = [t for t in recent if _ema_aligned(t)]
    hvol_wins = [t for t in wins   if t.get("vol_ratio", 0) >= 2.0]
    hvol_all  = [t for t in recent if t.get("vol_ratio", 0) >= 2.0]

    FRESH = {"1h": 6, "4h": 4, "1d": 2}
    fresh_wins = [t for t in wins   if t.get("bars_ago", 99) <= FRESH.get(t.get("tf",""), 99)]
    fresh_all  = [t for t in recent if t.get("bars_ago", 99) <= FRESH.get(t.get("tf",""), 99)]

    avg_win_pnl  = sum(t["pnl_usd"] for t in wins)   / max(len(wins), 1)
    avg_loss_pnl = sum(t["pnl_usd"] for t in losses) / max(len(losses), 1)  # 음수

    return {
        "total_wins":       len(wins),
        "total_losses":     len(losses),
        "ema_aligned_wr":   len(ema_wins)   / max(len(ema_all), 1),
        "high_vol_wr":      len(hvol_wins)  / max(len(hvol_all), 1),
        "fresh_signal_wr":  len(fresh_wins) / max(len(fresh_all), 1),
        "avg_win_pnl":      round(avg_win_pnl, 3),
        "avg_loss_pnl":     round(avg_loss_pnl, 3),
        "profit_factor":    round(abs(avg_win_pnl) / max(abs(avg_loss_pnl), 0.001), 2),
    }


# ─── 단일 패인 거래 상세 분석 ────────────────────────────────────────────────

def _analyze_failure(trade: dict) -> list[str]:
    """패인 거래 원인 분석 — 5가지 체크포인트."""
    from config import SWING_FRESHNESS, SCALP_FRESHNESS
    reasons = []
    tf        = trade.get("tf", "")
    direction = trade.get("direction", "")
    ema_trend = trade.get("ema_trend")
    bars_ago  = trade.get("bars_ago", 0)
    vol       = trade.get("vol_ratio", 0)
    sl_pct    = trade.get("sl_pct", 0)
    confirmed = trade.get("confirmed_count", 0)

    # 1. EMA 역추세 진입
    if ema_trend == -1 and direction == "LONG":
        reasons.append("🔴 EMA 하락추세에서 LONG 진입 (역추세)")
    elif ema_trend == 1 and direction == "SHORT":
        reasons.append("🔴 EMA 상승추세에서 SHORT 진입 (역추세)")
    elif ema_trend == 0:
        reasons.append("⚪ EMA 중립 구간 — 방향성 불명확")

    # 2. 신호 신뢰도
    if confirmed == 3:
        reasons.append("⚠️ 3/6 최소 확인 — 신뢰도 낮음")
    elif confirmed == 4:
        reasons.append("📊 4/6 확인 — 중간 신뢰도 (ELITE 아님)")

    # 3. 신호 신선도 (핵심 — 오래된 봉 = 기회 이미 지남)
    all_freshness = {**SCALP_FRESHNESS, **SWING_FRESHNESS}
    limit = all_freshness.get(tf, 99)
    if bars_ago and limit < 99:
        hours = bars_ago * TF_HOURS.get(tf, 1)
        if bars_ago > limit:
            reasons.append(f"⏰ 신호 너무 오래됨 ({bars_ago}봉전 ≈ {hours:.0f}h, 기준 {limit}봉)")
        elif bars_ago > limit * 0.6:
            reasons.append(f"⚠️ 신호 다소 오래됨 ({bars_ago}봉전 ≈ {hours:.0f}h) — 신선도 저하")

    # 4. 거래량 미약 (기준 1.5x — VOL_SPIKE_THRESHOLD와 동일)
    if vol and vol < 1.5:
        reasons.append(f"📉 거래량 미약 ({vol:.1f}x — 기준 1.5x 미달, 노이즈 신호)")
    elif vol and vol < 2.5:
        reasons.append(f"📊 거래량 보통 ({vol:.1f}x) — 강한 신호는 2.5x 이상")

    # 5. SL 너무 넓음
    wide_limit = WIDE_SL_BY_TF.get(tf, 99)
    if sl_pct and sl_pct > wide_limit:
        reasons.append(f"📏 SL 과도하게 넓음 ({sl_pct:.1f}% > {tf} 기준 {wide_limit}%)")

    if not reasons:
        reasons.append("일반 시장 노이즈 (조건상 문제 없음)")
    return reasons


def build_next_strategy(trade: dict) -> list[str]:
    """
    패인 거래 기반 → 구체적인 다음 진입 전략 생성.
    "다음 번 이 자리에선 이렇게 진입한다"를 명시.
    """
    from config import SWING_FRESHNESS
    tf        = trade.get("tf", "1h")
    symbol    = trade.get("symbol", "")
    direction = trade.get("direction", "")
    bars_ago  = trade.get("bars_ago", 0)
    vol_ratio = trade.get("vol_ratio", 0)
    ema_trend = trade.get("ema_trend", 0)
    confirmed = trade.get("confirmed_count", 0)
    coin      = symbol.split("/")[0] if symbol else "해당코인"
    opp_dir   = "SHORT" if direction == "LONG" else "LONG"
    tf_h      = TF_HOURS.get(tf, 1)
    reasons   = _analyze_failure(trade)

    plans = []

    # ── EMA 역추세였다면 ──────────────────────────────────────────────────────
    if (ema_trend == -1 and direction == "LONG") or (ema_trend == 1 and direction == "SHORT"):
        plans.append(f"  ① EMA 추세 전환 대기: {coin} {tf}봉 EMA20 > EMA50 전환 확인 후 {direction} 진입")
        plans.append(f"     또는 {opp_dir} 방향 신호 발생 시 순추세 매매로 전환")

    # ── 신호가 오래됐다면 ────────────────────────────────────────────────────
    if bars_ago > 5:
        fresh_limit = max(SWING_FRESHNESS.get(tf, 12) - 3, 3)
        fresh_h     = fresh_limit * tf_h
        plans.append(f"  ② 신선도 강화: {coin} {tf}봉 신호 {fresh_limit}봉({fresh_h:.0f}h) 이내만 진입")
        plans.append(f"     + 최근 3봉 중 방향일치 캔들 2봉 이상 확인 후 진입 (모멘텀 확인)")

    # ── 거래량 부족 ──────────────────────────────────────────────────────────
    if vol_ratio and vol_ratio < 2.0:
        target_vol = 2.0 if vol_ratio < 1.5 else 1.8
        plans.append(f"  ③ 볼륨 기준 상향: {coin} 다음 신호는 거래량 {target_vol}x 이상 확인 후 진입")

    # ── 신뢰도 부족 ──────────────────────────────────────────────────────────
    if confirmed < 5:
        plans.append(f"  ④ 신호 강도 조건: {coin} {tf}봉 다음 진입은 5/6 이상 (VERY STRONG 이상) 대기")

    # ── 공통 황금 진입 조건 ──────────────────────────────────────────────────
    if not plans:
        plans.append(f"  ① {coin} 다음 진입: 현재 설정 조건 유지, 시장 노이즈로 판단")
        plans.append(f"     → ELITE 6/6 + MTF 전정렬 + EMA정렬 황금 진입 기회 대기")
    else:
        plans.append(f"  💡 이상적 재진입: ELITE + MTF 전정렬 + 신선도 4봉 이내 황금 진입 조건 충족 시")

    return plans


# ─── 자동 조정 메인 함수 ────────────────────────────────────────────────────

def analyze_and_adjust() -> list[str]:
    s      = _load()
    all_t  = s.get("trade_history", [])
    recent = [t for t in all_t if t["status"] in ("win", "loss")][-ANALYSIS_LOOKBACK:]

    if len(recent) < MIN_TRADES:
        return []

    filters = get_adaptive_filters()
    now     = time.time()
    adjustments = []

    # ── 1. 전체 승률 → MIN_RR 조정 ──────────────────────────────────────────
    wins = sum(1 for t in recent if t["status"] == "win")
    wr   = wins / len(recent)
    cur_rr = filters["min_rr"]

    if wr < 0.40 and cur_rr < MAX_MIN_RR:
        new_rr = round(min(cur_rr + 0.2, MAX_MIN_RR), 1)
        filters["min_rr"] = new_rr
        adjustments.append(f"MIN_RR {cur_rr} → {new_rr} 강화 (승률 {wr*100:.0f}%↓)")
    elif wr > 0.65 and cur_rr > 1.5:
        new_rr = round(max(cur_rr - 0.1, 1.5), 1)
        filters["min_rr"] = new_rr
        adjustments.append(f"MIN_RR {cur_rr} → {new_rr} 완화 (승률 {wr*100:.0f}%↑)")

    # ── 2. TF별 승률 → 저성과 TF 일시 제외 ─────────────────────────────────
    tf_stat: dict = {}
    for t in recent:
        tf_stat.setdefault(t.get("tf", ""), {"w": 0, "l": 0})
        tf_stat[t.get("tf", "")][t["status"][0]] += 1

    skip_tfs = {k: v for k, v in filters["skip_tfs"].items() if v > now}
    for tf, st in tf_stat.items():
        n = st["w"] + st["l"]
        if n < 4:
            continue
        wr_tf = st["w"] / n
        if wr_tf < 0.35 and tf not in skip_tfs:
            until = now + SKIP_DURATION_H * 3600
            skip_tfs[tf] = until
            h = datetime.fromtimestamp(until, KST).strftime("%H:%M")
            adjustments.append(f"{tf}봉 {SKIP_DURATION_H}시간 제외 ({h} KST까지) — 승률 {wr_tf*100:.0f}%")
        elif wr_tf > 0.60 and tf in skip_tfs:
            del skip_tfs[tf]
            adjustments.append(f"{tf}봉 제외 해제 — 승률 회복 {wr_tf*100:.0f}%")
    filters["skip_tfs"] = skip_tfs

    # ── 3. 심볼별 3연패 → 일시 제외 ─────────────────────────────────────────
    skip_sym = {k: v for k, v in filters["skip_symbols"].items() if v > now}
    sym_seq: dict = {}
    for t in reversed(recent):
        sym_seq.setdefault(t["symbol"], []).append(t["status"])

    for sym, results in sym_seq.items():
        consec = 0
        for r in results:
            if r == "loss":
                consec += 1
            else:
                break
        if consec >= 3 and sym not in skip_sym:
            until = now + 3 * 3600   # 12h → 3h
            skip_sym[sym] = until
            coin = sym.split("/")[0]
            h    = datetime.fromtimestamp(until, KST).strftime("%H:%M")
            adjustments.append(f"{coin} 3시간 제외 ({h} KST까지) — 3연패")
        elif consec == 0 and sym in skip_sym:
            del skip_sym[sym]
            adjustments.append(f"{sym.split('/')[0]} 제외 해제")
    filters["skip_symbols"] = skip_sym

    # ── 4. STRONG 신호 패배 → confirmed 요구치 상향 ─────────────────────────
    mc = filters["min_confirmed_by_tf"]
    strong_stat: dict = {}
    for t in recent:
        raw = t.get("strength", "").replace(" 💎","").replace(" 🔥","").replace(" ⚡","")
        if raw != "STRONG":
            continue
        key = t.get("tf", "")
        strong_stat.setdefault(key, {"w": 0, "l": 0})
        strong_stat[key][t["status"][0]] += 1

    for tf, st in strong_stat.items():
        n = st["w"] + st["l"]
        if n < 3:
            continue
        wr_s = st["w"] / n
        if wr_s < 0.35:
            prev = mc.get(tf, 4)
            mc[tf] = min(prev + 1, 5)
            if mc[tf] > prev:
                adjustments.append(f"{tf}봉 STRONG 기준 {mc[tf]}/5로 강화 (승률 {wr_s*100:.0f}%)")
    filters["min_confirmed_by_tf"] = mc

    # ── 5. 저볼륨 손실 패턴 → min_vol_ratio 자동 조정 ─────────────────────────
    # 핵심 버그 수정: 4시간마다 동일한 historical 데이터 재분석 → 항상 "저볼륨 손실 >= 3"
    #   → vol_ratio 영구 MAX 고착 → 신호 전부 차단 → 자기 강화 데스스파이럴
    #
    # 해법: last_vol_adj_trade_count 추적
    #   - 마지막 조정 이후 새 거래가 없으면 vol_ratio 변경 없음
    #   - 12시간 이상 신규 거래 없으면 자동 완화 (필터가 너무 타이트한 신호)
    #   - 새 거래 >= 2개 있을 때만 새 데이터로 재평가
    cur_vol          = filters.get("min_vol_ratio", MIN_VOL_RATIO)
    last_adj_count   = filters.get("last_vol_adj_trade_count", 0)
    last_adj_time    = filters.get("last_vol_adj_time", 0.0)
    closed_count     = len([t for t in all_t if t["status"] in ("win", "loss")])
    new_since_adj    = closed_count - last_adj_count
    hours_since_adj  = (now - last_adj_time) / 3600 if last_adj_time > 0 else 0

    if new_since_adj == 0 and hours_since_adj > 12 and cur_vol > MIN_VOL_RATIO and last_adj_time > 0:
        # 12시간 이상 신규 거래 없음 = 필터가 기회를 막고 있다는 신호 → 완화
        new_vol = round(max(cur_vol - 0.2, MIN_VOL_RATIO), 1)
        filters["min_vol_ratio"]           = new_vol
        filters["last_vol_adj_time"]        = now
        adjustments.append(
            f"볼륨 기준 {cur_vol}x → {new_vol}x 자동완화 "
            f"(신규거래 없음 {hours_since_adj:.0f}h — 필터 과도)"
        )
    elif new_since_adj >= 2:
        # 새 거래 2개 이상 있을 때만 새 데이터로 재평가
        new_recent = [t for t in recent if t.get("timestamp", 0) > last_adj_time]
        low_vol_losses = [
            t for t in new_recent
            if t["status"] == "loss" and 0 < t.get("vol_ratio", 99) < cur_vol + 0.3
        ]
        high_vol_wins = [
            t for t in new_recent
            if t["status"] == "win" and t.get("vol_ratio", 0) >= cur_vol
        ]
        if len(low_vol_losses) >= 2:
            new_vol = round(min(cur_vol + 0.2, MAX_VOL_RATIO), 1)
            if new_vol > cur_vol:
                filters["min_vol_ratio"] = new_vol
                adjustments.append(
                    f"볼륨 기준 {cur_vol}x → {new_vol}x 강화 (새 저볼륨 손실 {len(low_vol_losses)}회)"
                )
        elif len(high_vol_wins) >= 1:
            new_vol = round(max(cur_vol - 0.2, MIN_VOL_RATIO), 1)
            if new_vol < cur_vol:
                filters["min_vol_ratio"] = new_vol
                adjustments.append(
                    f"볼륨 기준 {cur_vol}x → {new_vol}x 완화 (신규 고볼륨 승리 {len(high_vol_wins)}회)"
                )
        filters["last_vol_adj_trade_count"] = closed_count
        filters["last_vol_adj_time"]         = now

    # ── 6. swing_freshness 자동 강화 비활성화 ───────────────────────────────
    # PIVOT_RIGHT=5 때문에 강화 로직이 항상 진입 불가 상태를 만들었음.
    # config.py SWING_FRESHNESS(1h=20, 4h=20, 1d=8)가 이미 충분히 보수적.
    # 신선도는 소프트 스코어(get_freshness_score)로만 반영하고, 하드게이트는 고정.
    filters["swing_freshness"] = {}   # 항상 config 기본값 사용

    # ── 8. 승리 패턴 → EMA 정렬 포지션 부스트 자동 조정 ────────────────────────
    patterns = _extract_win_patterns(recent)
    if patterns and len(recent) >= MIN_TRADES:
        cur_boost = filters.get("ema_aligned_boost", 1.0)
        ema_wr    = patterns.get("ema_aligned_wr", 0.5)
        ema_total = patterns.get("total_wins", 0) + patterns.get("total_losses", 0)

        if ema_wr > 0.65 and ema_total >= 5 and cur_boost < 1.3:
            new_boost = round(min(cur_boost + 0.05, 1.3), 2)
            filters["ema_aligned_boost"] = new_boost
            adjustments.append(
                f"EMA일치 포지션 {cur_boost}x → {new_boost}x 부스트 "
                f"(EMA정렬 승률 {ema_wr*100:.0f}%)"
            )
        elif ema_wr < 0.45 and cur_boost > 1.0:
            new_boost = round(max(cur_boost - 0.05, 1.0), 2)
            filters["ema_aligned_boost"] = new_boost
            adjustments.append(
                f"EMA일치 포지션 {cur_boost}x → {new_boost}x 완화 "
                f"(EMA정렬 승률 {ema_wr*100:.0f}%↓)"
            )

    # ── 이력 저장 ─────────────────────────────────────────────────────────────
    if adjustments:
        log = filters.setdefault("adjustments_log", [])
        log.append({
            "time":  datetime.now(KST).strftime("%m/%d %H:%M KST"),
            "items": adjustments,
        })
        log[:] = log[-20:]

    _save_adaptive(filters)
    return adjustments


# ─── 브리핑용 패턴 요약 ──────────────────────────────────────────────────────

def build_loss_pattern_summary(losses: list[dict]) -> str:
    """최근 패인 거래들의 공통 패턴 요약 문자열."""
    if not losses:
        return ""

    anti_trend = sum(
        1 for t in losses if
        (t.get("direction") == "LONG"  and t.get("ema_trend") == -1) or
        (t.get("direction") == "SHORT" and t.get("ema_trend") == 1)
    )
    neutral_ema = sum(1 for t in losses if t.get("ema_trend") == 0)
    low_vol     = sum(1 for t in losses if 0 < t.get("vol_ratio", 99) < 1.5)
    old_sig     = sum(
        1 for t in losses
        if t.get("bars_ago", 0) > {"1h": 12, "4h": 8, "1d": 3}.get(t.get("tf",""), 99) * 0.7
    )
    low_conf    = sum(1 for t in losses if t.get("confirmed_count", 5) <= 4)

    patterns = []
    n = len(losses)
    if anti_trend >= 1:
        patterns.append(f"역추세 진입 {anti_trend}/{n}회")
    if neutral_ema >= 1:
        patterns.append(f"EMA 중립 구간 {neutral_ema}/{n}회")
    if low_vol >= 1:
        patterns.append(f"저볼륨(1.5x↓) {low_vol}/{n}회")
    if old_sig >= 1:
        patterns.append(f"오래된 신호 {old_sig}/{n}회")
    if low_conf >= 1:
        patterns.append(f"4/5 이하 확인 {low_conf}/{n}회")

    return "  |  ".join(patterns) if patterns else "패턴 불명확"


# ─── 결산 브리핑 보고서 ──────────────────────────────────────────────────────

def build_learning_report(recent_losses: list[dict]) -> str:
    filters = get_adaptive_filters()
    now     = time.time()
    lines   = ["🧠 <b>학습 상태</b>"]

    rr = filters["min_rr"]
    if rr != 1.5:
        lines.append(f"   MIN_RR <b>{rr}</b>  (기본 1.5에서 조정)")

    vol = filters.get("min_vol_ratio", 1.5)
    if vol != 1.5:
        lines.append(f"   볼륨기준 <b>{vol}x</b>  (학습 조정)")

    skip_tfs = {k: v for k, v in filters["skip_tfs"].items() if v > now}
    for tf, until in skip_tfs.items():
        lines.append(f"   ⛔ {tf}봉 제외 → {datetime.fromtimestamp(until, KST).strftime('%H:%M')} KST")

    skip_sym = {k: v for k, v in filters["skip_symbols"].items() if v > now}
    for sym, until in skip_sym.items():
        lines.append(f"   ⛔ {sym.split('/')[0]} 제외 → {datetime.fromtimestamp(until, KST).strftime('%H:%M')} KST")

    mc = filters["min_confirmed_by_tf"]
    for tf, v in mc.items():
        if v > 4:
            lines.append(f"   📈 {tf}봉 confirmed {v}/5 요구")

    sf = filters.get("swing_freshness", {})
    from config import SWING_FRESHNESS as CFG_FRESH
    for tf, limit in sf.items():
        default = CFG_FRESH.get(tf, 99)
        if limit < default:
            lines.append(f"   ⏰ {tf}봉 신선도 {limit}봉 (기본 {default}봉에서 강화)")

    if len(lines) == 1:
        lines.append("   조정 없음 (기본 설정 유지)")

    # 승리 패턴 통계
    s = _load()
    all_t   = s.get("trade_history", [])
    recent  = [t for t in all_t if t["status"] in ("win", "loss")][-ANALYSIS_LOOKBACK:]
    if recent:
        pat = _extract_win_patterns(recent)
        if pat:
            lines.append("")
            lines.append("🏆 <b>승리 패턴 분석</b>")
            lines.append(f"   EMA방향일치 승률: <b>{pat['ema_aligned_wr']*100:.0f}%</b>")
            lines.append(f"   고볼륨(2x+) 승률: <b>{pat['high_vol_wr']*100:.0f}%</b>")
            lines.append(f"   신선신호 승률:    <b>{pat['fresh_signal_wr']*100:.0f}%</b>")
            lines.append(f"   평균 수익: +${pat['avg_win_pnl']:.2f}  |  평균 손실: ${pat['avg_loss_pnl']:.2f}")
            lines.append(f"   Profit Factor: <b>{pat['profit_factor']:.2f}</b>  (1.0 이상 = 수익)")

    if recent_losses:
        lines.append("")
        lines.append("📉 <b>패인 분석 + 다음 전략</b>")
        lines.append(f"   공통패턴: {build_loss_pattern_summary(recent_losses)}")
        for t in recent_losses[-3:]:
            coin    = t["symbol"].split("/")[0]
            reasons = _analyze_failure(t)
            plans   = build_next_strategy(t)
            lines.append(f"\n   [{t['num']}회차 {coin} {t.get('tf','')} {t['direction']}]")
            lines.append("   ─ 원인:")
            for r in reasons:
                lines.append(f"   • {r}")
            lines.append("   ─ 다음 전략:")
            for p in plans:
                lines.append(f"   {p}")

    return "\n".join(lines)


# ─── 필터 유효성 체크 ─────────────────────────────────────────────────────────

def is_tradeable(symbol: str, tf_key: str) -> tuple[bool, str]:
    filters = get_adaptive_filters()
    now     = time.time()

    skip_tfs = filters.get("skip_tfs", {})
    if tf_key in skip_tfs and skip_tfs[tf_key] > now:
        until = datetime.fromtimestamp(skip_tfs[tf_key], KST).strftime("%H:%M")
        return False, f"{tf_key}봉 학습 제외 중 ({until} KST까지)"

    skip_sym = filters.get("skip_symbols", {})
    if symbol in skip_sym and skip_sym[symbol] > now:
        until = datetime.fromtimestamp(skip_sym[symbol], KST).strftime("%H:%M")
        return False, f"{symbol.split('/')[0]} 학습 제외 중 ({until} KST까지)"

    return True, "ok"


def get_adaptive_min_rr() -> float:
    return get_adaptive_filters().get("min_rr", 1.5)

def get_adaptive_min_vol() -> float:
    return get_adaptive_filters().get("min_vol_ratio", 1.5)

def get_adaptive_min_confirmed(tf_key: str, default: int = 4) -> int:
    mc = get_adaptive_filters().get("min_confirmed_by_tf", {})
    return mc.get(tf_key, default)

def get_adaptive_swing_freshness(tf_key: str, default: int = 99) -> int:
    sf = get_adaptive_filters().get("swing_freshness", {})
    return sf.get(tf_key, default)
