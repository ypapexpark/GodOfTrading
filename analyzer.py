"""
패인 트레이드 분석 + 적응형 매매 필터 (자동 학습 엔진)

학습 항목:
  1. 전체 승률 → MIN_RR 자동 조정
  2. TF별 승률 → 약한 TF 패턴 기록
  3. 심볼 연패 → 해당 코인 패인 기록
  4. STRONG 신호 패배 → confirmed 요구치 상향
  5. 역추세 진입 패배 → 역추세 통계 누적 (고확신 신호만 허용)
  6. 저볼륨 손실 패턴 → min_vol_ratio 자동 상향
  7. 오래된 신호 손실 패턴 → 복기/스코어에 반영
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from config import (ACTIVE_MAX_MIN_RR, ACTIVE_MAX_MIN_VOL,
                    ASYMMETRIC_MIN_AVG_WIN, ASYMMETRIC_MIN_EDGE_FLOOR,
                    ASYMMETRIC_MIN_MFE_MAE, ASYMMETRIC_MIN_PAYOFF,
                    ASYMMETRIC_MIN_SAMPLES, ASYMMETRIC_RISK_MULT,
                    CANDIDATE_LOG_FILE,
                    DRAWDOWN_RISK_MULT, DRAWDOWN_RISK_OFF_PCT,
                    DRAWDOWN_WARN_PCT, MIN_DYNAMIC_RISK_MULT,
                    SYMBOL_COOLDOWN_HOURS,
                    REALIZED_BLOCK_EXACT_MIN_TRADES,
                    REALIZED_BLOCK_MIN_PNL_USD,
                    REALIZED_BLOCK_MODE_TF_MIN_TRADES,
                    REALIZED_BLOCK_SYMBOL_MODE_MIN_TRADES,
                    REALIZED_BLOCK_WIN_RATE,
                    REALIZED_BOOST_MIN_PNL_USD, REALIZED_BOOST_MIN_TRADES,
                    REALIZED_BOOST_MULT, REALIZED_BOOST_WIN_RATE,
                    REALIZED_MODE_BOOST_MULT,
                    REALIZED_TRADE_LEARNING_ENABLED,
                    SIGNAL_QUALITY_BAD_EDGE, SIGNAL_QUALITY_BAD_WR,
                    SIGNAL_QUALITY_GOOD_EDGE, SIGNAL_QUALITY_GOOD_MULT,
                    SIGNAL_QUALITY_GOOD_WR, SIGNAL_QUALITY_HORIZON,
                    SIGNAL_QUALITY_LOOKBACK_DAYS, SIGNAL_QUALITY_MIN_EXACT,
                    SIGNAL_QUALITY_MIN_STRATEGY, SIGNAL_QUALITY_MIN_TF,
                    SIGNAL_QUALITY_WEAK_MULT, SIGNAL_QUALITY_WEAK_WR,
                    WINRATE_LEVERAGE_ENABLED, WINRATE_LEVERAGE_EXCELLENT_EDGE,
                    WINRATE_LEVERAGE_EXCELLENT_MULT, WINRATE_LEVERAGE_EXCELLENT_WR,
                    WINRATE_LEVERAGE_GOOD_EDGE, WINRATE_LEVERAGE_GOOD_MULT,
                    WINRATE_LEVERAGE_GOOD_WR, WINRATE_LEVERAGE_GREAT_EDGE,
                    WINRATE_LEVERAGE_GREAT_MULT, WINRATE_LEVERAGE_GREAT_WR,
                    WINRATE_LEVERAGE_MAX, WINRATE_LEVERAGE_MIN_SAMPLES)
from strategy_catalog import classify_strategy

KST = timezone(timedelta(hours=9))

ANALYSIS_LOOKBACK  = 20
MIN_TRADES         = 5
MAX_MIN_RR         = 2.5
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
        "cooldown_symbols":    {},     # 연패 심볼 쿨다운: {symbol: expiry_ts}
        "min_vol_ratio":       1.5,    # 스윙 진입 볼륨 기준 (학습으로 자동 조정)
        "swing_freshness":     {},     # TF별 신선도 한도 덮어쓰기 (학습으로 강화)
        "ema_aligned_boost":   1.0,    # EMA 방향일치 진입 시 포지션 배율 (학습)
        "adjustments_log":     [],
    }
    s = _load()
    adaptive = s.get("adaptive", {})
    if not isinstance(adaptive, dict):
        adaptive = {}
    for k, v in defaults.items():
        adaptive.setdefault(k, v)
    return adaptive


def get_cooldown_symbols() -> set[str]:
    """현재 쿨다운 중인 심볼 집합 반환. 만료된 항목은 자동 제외."""
    filters = get_adaptive_filters()
    now = time.time()
    return {sym for sym, ts in filters.get("cooldown_symbols", {}).items() if ts > now}


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
        reasons.append("⚠️ 3개 다이버전스 최소 확인 — 후보급 신뢰도")
    elif confirmed == 4:
        reasons.append("📊 4/7 확인 — 조건부 신뢰도 (ELITE 아님)")

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
        plans.append(f"  ④ 신호 강도 조건: {coin} {tf}봉 다음 진입은 5/7 이상 (VERY STRONG 이상) 대기")

    # ── 공통 황금 진입 조건 ──────────────────────────────────────────────────
    if not plans:
        plans.append(f"  ① {coin} 다음 진입: 현재 설정 조건 유지, 시장 노이즈로 판단")
        plans.append(f"     → ELITE급 + MTF 전정렬 + EMA정렬 황금 진입 기회 대기")
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

    # ── 1. 전체 승률 → MIN_RR 조정 기록 ─────────────────────────────────────
    # 연패를 이유로 진입 빈도를 줄이지 않기 위해 RR 필터는 상향 고착시키지 않는다.
    # 손실은 패인 분석/전략 개선에 쓰고, 하드 방어는 일손실/DD 한도로만 처리한다.
    wins = sum(1 for t in recent if t["status"] == "win")
    wr   = wins / len(recent)
    cur_rr = filters["min_rr"]

    if cur_rr > ACTIVE_MAX_MIN_RR:
        new_rr = ACTIVE_MAX_MIN_RR
        filters["min_rr"] = new_rr
        adjustments.append(f"MIN_RR {cur_rr} → {new_rr} 복구 (연패로 매매빈도 축소 금지)")
    elif wr > 0.65 and cur_rr > 1.5:
        new_rr = round(max(cur_rr - 0.1, 1.5), 1)
        filters["min_rr"] = new_rr
        adjustments.append(f"MIN_RR {cur_rr} → {new_rr} 완화 (승률 {wr*100:.0f}%↑)")

    # ── 2. TF별 승률 → 저성과 TF 기록만 남김 ────────────────────────────────
    tf_stat: dict = {}
    for t in recent:
        tf_stat.setdefault(t.get("tf", ""), {"w": 0, "l": 0})
        tf_stat[t.get("tf", "")][t["status"][0]] += 1

    weak_tfs = {}
    for tf, st in tf_stat.items():
        n = st["w"] + st["l"]
        if n < 4:
            continue
        wr_tf = st["w"] / n
        weak_tfs[tf] = {"win_rate": round(wr_tf, 3), "trades": n}
        if wr_tf < 0.35:
            adjustments.append(
                f"{tf}봉 저성과 기록 — 승률 {wr_tf*100:.0f}% (실체결 손익학습에 반영)"
            )
    filters["weak_tfs"] = weak_tfs
    filters["skip_tfs"] = {}

    # ── 3. 심볼별 연패 → 쿨다운 적용 ────────────────────────────────────────
    sym_seq: dict = {}
    for t in reversed(recent):
        sym_seq.setdefault(t["symbol"], []).append(t["status"])

    symbol_loss_streaks = {}
    cooldown_symbols = filters.get("cooldown_symbols", {})
    # 만료된 쿨다운 제거
    cooldown_symbols = {sym: ts for sym, ts in cooldown_symbols.items() if ts > now}

    for sym, results in sym_seq.items():
        consec = 0
        for r in results:
            if r == "loss":
                consec += 1
            else:
                break
        if consec:
            symbol_loss_streaks[sym] = consec
        if consec >= 3:
            coin = sym.split("/")[0]
            expiry_ts = now + SYMBOL_COOLDOWN_HOURS * 3600
            cooldown_symbols[sym] = expiry_ts
            expiry_str = datetime.fromtimestamp(expiry_ts, KST).strftime("%H:%M")
            adjustments.append(
                f"{coin} {consec}연패 → {SYMBOL_COOLDOWN_HOURS}h 쿨다운 ({expiry_str}KST까지)"
            )
    filters["symbol_loss_streaks"] = symbol_loss_streaks
    filters["cooldown_symbols"] = cooldown_symbols
    filters["skip_symbols"] = {}  # 레거시 호환 (main.py 미사용)

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
                adjustments.append(f"{tf}봉 STRONG 기준 {mc[tf]}/7로 강화 (승률 {wr_s*100:.0f}%)")
    filters["min_confirmed_by_tf"] = mc

    # ── 5. 저볼륨 손실 패턴 → min_vol_ratio 자동 조정 ─────────────────────────
    # 핵심 버그 수정: 4시간마다 동일한 historical 데이터 재분석 → 항상 "저볼륨 손실 >= 3"
    #   → vol_ratio 영구 MAX 고착 → 신호 전부 차단 → 자기 강화 데스스파이럴
    #
    # 해법: last_vol_adj_trade_count 추적
    #   - 마지막 조정 이후 새 거래가 없으면 vol_ratio 변경 없음
    #   - 12시간 이상 신규 거래 없으면 자동 완화 (필터가 너무 타이트한 신호)
    #   - 새 거래 >= 2개 있을 때만 새 데이터로 재평가
    cur_vol          = min(filters.get("min_vol_ratio", MIN_VOL_RATIO), ACTIVE_MAX_MIN_VOL)
    if filters.get("min_vol_ratio", MIN_VOL_RATIO) > ACTIVE_MAX_MIN_VOL:
        adjustments.append(
            f"볼륨 기준 {filters.get('min_vol_ratio')}x → {ACTIVE_MAX_MIN_VOL}x 복구 "
            "(필터 과도 차단 방지)"
        )
        filters["min_vol_ratio"] = ACTIVE_MAX_MIN_VOL
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

    # ── 이력 저장 (동일 내용 중복 방지) ────────────────────────────────────────
    if adjustments:
        log = filters.setdefault("adjustments_log", [])
        if log and log[-1].get("items") == adjustments:
            # 내용 동일하면 시간만 갱신 (9분마다 같은 메시지가 쌓이던 버그 수정)
            log[-1]["time"] = datetime.now(KST).strftime("%m/%d %H:%M KST")
        else:
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
        patterns.append(f"4/7 이하 확인 {low_conf}/{n}회")

    return "  |  ".join(patterns) if patterns else "패턴 불명확"


# ─── 결산 브리핑 보고서 ──────────────────────────────────────────────────────

def build_learning_report(recent_losses: list[dict]) -> str:
    filters = get_adaptive_filters()
    lines   = ["🧠 <b>학습 상태</b>"]

    rr = filters["min_rr"]
    if rr != 1.5:
        lines.append(f"   MIN_RR <b>{rr}</b>  (기본 1.5에서 조정)")

    vol = filters.get("min_vol_ratio", 1.5)
    if vol != 1.5:
        lines.append(f"   볼륨기준 <b>{vol}x</b>  (학습 조정)")

    weak_tfs = filters.get("weak_tfs", {})
    for tf, stat in weak_tfs.items():
        if stat.get("win_rate", 1.0) < 0.35:
            lines.append(
                f"   📊 {tf}봉 저성과 기록: 승률 {stat['win_rate']*100:.0f}% "
                "(동일 전략군 손익학습에 반영)"
            )

    symbol_streaks = filters.get("symbol_loss_streaks", {})
    for sym, streak in symbol_streaks.items():
        if streak >= 3:
            lines.append(f"   📉 {sym.split('/')[0]} {streak}연패 기록 (동일 조건 재진입 차단 가능)")

    mc = filters["min_confirmed_by_tf"]
    for tf, v in mc.items():
        if v > 4:
            lines.append(f"   📈 {tf}봉 confirmed {v}/7 요구")

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
    dd = float(s.get("drawdown_pct", 0) or 0)
    if dd > 0:
        status = s.get("drawdown_status", "normal")
        lines.append(f"   계좌 DD <b>{dd:.1f}%</b>  |  상태: {status}")

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
    # 전략/방향이 정해지지 않은 초기 스캔 단계에서는 넓은 심볼 차단을 하지 않는다.
    # 실제 손익학습 차단은 _try_auto_trade/_try_breakout_trade가 구체 전략과 함께 수행한다.
    return True, "ok"


def _closed_trades(hours: float | None = None) -> list[dict]:
    cutoff = time.time() - hours * 3600 if hours else 0
    return [
        t for t in _load().get("trade_history", [])
        if t.get("status") in ("win", "loss", "breakeven")
        and t.get("timestamp", 0) >= cutoff
    ]


def _trade_family_key(trade: dict) -> str:
    ctx = trade.get("entry_context") or {}
    mode = trade.get("strategy_mode") or ctx.get("strategy_mode")
    if mode:
        return str(mode)
    profile = classify_strategy(
        trade.get("strategy", ""),
        trade.get("signal_type", ""),
        bool(trade.get("is_divergence", True)),
        trade.get("direction", ""),
        ctx,
        bool(trade.get("asymmetric_mode", False)),
    )
    return profile.get("family_key", "core_quant")


def _input_family_key(strategy: str, direction: str = "",
                      signal_type: str = "",
                      is_divergence: bool = True,
                      asymmetric: bool = False) -> str:
    profile = classify_strategy(
        strategy or "",
        signal_type or "",
        bool(is_divergence),
        direction or "",
        asymmetric=asymmetric,
    )
    return profile.get("family_key", "core_quant")


def _realized_stat(rows: list[dict]) -> dict:
    n = len(rows)
    wins = [t for t in rows if float(t.get("pnl_usd", 0) or 0) > 0]
    losses = [t for t in rows if float(t.get("pnl_usd", 0) or 0) < 0]
    pnl = sum(float(t.get("pnl_usd", 0) or 0) for t in rows)
    avg_win = sum(float(t.get("pnl_usd", 0) or 0) for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(float(t.get("pnl_usd", 0) or 0) for t in losses) / len(losses) if losses else 0.0
    return {
        "samples": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n if n else 0.0,
        "pnl": pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


def _realized_quality_groups(symbol: str, tf_key: str, strategy: str,
                             direction: str, family_key: str) -> list[tuple[str, list[dict], int]]:
    rows = _closed_trades()
    same_direction = [t for t in rows if not direction or t.get("direction") == direction]
    exact = [
        t for t in same_direction
        if t.get("symbol") == symbol
        and t.get("tf") == tf_key
        and t.get("strategy", "") == strategy
    ]
    symbol_mode = [
        t for t in same_direction
        if t.get("symbol") == symbol and _trade_family_key(t) == family_key
    ]
    mode_tf = [
        t for t in same_direction
        if t.get("tf") == tf_key and _trade_family_key(t) == family_key
    ]
    return [
        ("동일조건", exact, REALIZED_BLOCK_EXACT_MIN_TRADES),
        ("동일심볼전략군", symbol_mode, REALIZED_BLOCK_SYMBOL_MODE_MIN_TRADES),
        ("동일봉전략군", mode_tf, REALIZED_BLOCK_MODE_TF_MIN_TRADES),
    ]


def get_realized_trade_adjustment(symbol: str, tf_key: str,
                                  strategy: str, direction: str,
                                  signal_type: str = "",
                                  is_divergence: bool = True,
                                  asymmetric: bool = False) -> tuple[float, list[str]]:
    """실제 체결 손익 기반 차단/강화 필터."""
    if not REALIZED_TRADE_LEARNING_ENABLED:
        return 1.0, []

    family_key = _input_family_key(strategy, direction, signal_type, is_divergence, asymmetric)
    block_notes: list[str] = []
    boost_candidates: list[tuple[float, str]] = []

    for scope, group, min_samples in _realized_quality_groups(
        symbol, tf_key, strategy, direction, family_key
    ):
        if len(group) < min_samples:
            continue
        st = _realized_stat(group)
        note = (
            f"실체결학습 {scope} {st['samples']}건: "
            f"{st['wins']}승 {st['losses']}패, 승률 {st['win_rate']*100:.0f}%, "
            f"누적손익 ${st['pnl']:+.2f}"
        )
        losing = (
            st["pnl"] <= REALIZED_BLOCK_MIN_PNL_USD
            and st["win_rate"] <= REALIZED_BLOCK_WIN_RATE
        )
        if losing:
            block_notes.append(note + " → 손실 우위 반복, 실거래 차단")
            continue
        winning = (
            st["samples"] >= REALIZED_BOOST_MIN_TRADES
            and st["pnl"] >= REALIZED_BOOST_MIN_PNL_USD
            and st["win_rate"] >= REALIZED_BOOST_WIN_RATE
        )
        if winning:
            mult = REALIZED_BOOST_MULT if scope != "동일봉전략군" else REALIZED_MODE_BOOST_MULT
            boost_candidates.append((mult, note + f" → 승리 패턴 강화 리스크×{mult:.2f}"))

    if block_notes:
        return 0.0, block_notes
    if boost_candidates:
        mult, note = max(boost_candidates, key=lambda x: x[0])
        return mult, [note]
    return 1.0, []


def is_tradeable_with_strategy(symbol: str, tf_key: str,
                               strategy: str = "",
                               direction: str = "",
                               signal_type: str = "",
                               is_divergence: bool = True,
                               asymmetric: bool = False) -> tuple[bool, str]:
    mult, notes = get_realized_trade_adjustment(
        symbol, tf_key, strategy, direction,
        signal_type=signal_type,
        is_divergence=is_divergence,
        asymmetric=asymmetric,
    )
    if mult <= 0:
        return False, " | ".join(notes)
    return True, "ok"




_NON_COMPARABLE_REASONS = (
    "5m 초단타",
    "신선도 초과",
    "볼륨",
    "MODERATE 알림",
    "추세 기준 미달",
    "Bybit 미지원",
)


def _candidate_eval_rows(days: float = SIGNAL_QUALITY_LOOKBACK_DAYS,
                         comparable_only: bool = False) -> list[dict]:
    path = Path(CANDIDATE_LOG_FILE)
    if not path.exists():
        return []
    cutoff = time.time() - days * 86400
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") != "evaluated":
                continue
            if comparable_only:
                reason = str(row.get("source_reason") or row.get("reason") or "")
                if any(key in reason for key in _NON_COMPARABLE_REASONS):
                    continue
            if float(row.get("timestamp", 0) or 0) < cutoff:
                continue
            ev = row.get("eval", {}) or {}
            h = ev.get(SIGNAL_QUALITY_HORIZON) or ev.get("now")
            if not h:
                continue
            try:
                row["_edge"] = float(h.get("edge_score", 0) or 0)
                row["_mfe"] = float(h.get("mfe_pct", h.get("move_pct", 0)) or 0)
                row["_mae"] = float(h.get("mae_pct", 0) or 0)
            except Exception:
                continue
            rows.append(row)
    except Exception:
        return []
    return rows


def _quality_stat(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {}
    wins = [r for r in rows if float(r.get("_edge", 0) or 0) > 0]
    losses = [r for r in rows if float(r.get("_edge", 0) or 0) <= 0]
    avg_edge = sum(float(r.get("_edge", 0) or 0) for r in rows) / n
    avg_mfe = sum(float(r.get("_mfe", 0) or 0) for r in rows) / n
    avg_mae = sum(float(r.get("_mae", 0) or 0) for r in rows) / n
    avg_adverse = sum(abs(min(float(r.get("_mae", 0) or 0), 0.0)) for r in rows) / n
    avg_win = sum(float(r.get("_edge", 0) or 0) for r in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(float(r.get("_edge", 0) or 0) for r in losses) / len(losses)) if losses else 0.0
    payoff = avg_win / max(avg_loss, 0.001)
    mfe_mae = max(avg_mfe, 0.0) / max(avg_adverse, 0.001)
    return {
        "samples": n,
        "win_rate": len(wins) / n,
        "avg_edge": avg_edge,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
        "avg_adverse": avg_adverse,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": payoff,
        "mfe_mae": mfe_mae,
    }


def _same_strategy(row: dict, tf_key: str, strategy: str, direction: str) -> bool:
    return (
        row.get("tf") == tf_key
        and row.get("strategy", "") == strategy
        and row.get("direction", "") == direction
    )


def _row_core_strategy(row: dict) -> tuple[str, str, str]:
    profile = classify_strategy(
        row.get("strategy", ""),
        row.get("signal_type", ""),
        bool(row.get("is_divergence", row.get("signal_type", "") in {
            "bullish", "bearish", "hidden_bullish", "hidden_bearish",
        })),
        row.get("direction", ""),
        {"reasons": [row.get("reason", "") or row.get("source_reason", "")]},
        bool(row.get("asymmetric_mode", False)),
    )
    family = row.get("strategy_family") or profile["family_label"]
    core = row.get("core_strategy") or profile["strategy_label"]
    return family, core, row.get("direction", "")


def _select_quality_group(rows: list[dict], symbol: str, tf_key: str,
                          strategy: str, direction: str,
                          exact_min: int = SIGNAL_QUALITY_MIN_EXACT,
                          strategy_min: int = SIGNAL_QUALITY_MIN_STRATEGY,
                          tf_min: int = SIGNAL_QUALITY_MIN_TF) -> tuple[str, dict]:
    exact = [
        r for r in rows
        if r.get("symbol") == symbol and _same_strategy(r, tf_key, strategy, direction)
    ]
    strat = [r for r in rows if _same_strategy(r, tf_key, strategy, direction)]
    tf_rows = [
        r for r in rows
        if r.get("tf") == tf_key and r.get("direction", "") == direction
    ]

    for label, group, minimum in (
        ("동일심볼", exact, exact_min),
        ("동일전략", strat, strategy_min),
        ("동일TF", tf_rows, tf_min),
    ):
        if len(group) >= minimum:
            return label, _quality_stat(group)
    return "", {}


def get_asymmetric_profile(symbol: str, tf_key: str,
                           strategy: str, direction: str) -> tuple[float, list[str], dict]:
    """
    낮은 승률이어도 평균 수익폭이 손실폭보다 큰 양의 비대칭 신호군을 식별한다.
    반환: (risk_multiplier, notes, profile)
    """
    rows = _candidate_eval_rows(comparable_only=True)
    if not rows:
        return 1.0, [], {}

    scope, st = _select_quality_group(
        rows, symbol, tf_key, strategy, direction,
        exact_min=ASYMMETRIC_MIN_SAMPLES,
        strategy_min=max(ASYMMETRIC_MIN_SAMPLES, SIGNAL_QUALITY_MIN_STRATEGY),
        tf_min=SIGNAL_QUALITY_MIN_TF,
    )
    if not st:
        return 1.0, [], {}

    payoff = st.get("payoff", 0.0)
    avg_win = st.get("avg_win", 0.0)
    mfe_mae = st.get("mfe_mae", 0.0)
    edge = st.get("avg_edge", 0.0)
    ok = (
        payoff >= ASYMMETRIC_MIN_PAYOFF
        and avg_win >= ASYMMETRIC_MIN_AVG_WIN
        and mfe_mae >= ASYMMETRIC_MIN_MFE_MAE
        and edge >= ASYMMETRIC_MIN_EDGE_FLOOR
    )
    if not ok:
        return 1.0, [], st

    note = (
        f"비대칭손익 {scope} {st['samples']}개: 승률 {st['win_rate']*100:.0f}%, "
        f"평균승리 {avg_win:+.2f}%, 평균손실 {st['avg_loss']:.2f}%, "
        f"payoff {payoff:.1f}x"
    )
    return ASYMMETRIC_RISK_MULT, [note + f" → 러너형 TP/리스크×{ASYMMETRIC_RISK_MULT:.2f}"], st


def get_signal_quality_adjustment(symbol: str, tf_key: str,
                                  strategy: str, direction: str) -> tuple[float, list[str]]:
    """
    실제 체결되지 않은 후보까지 포함한 사후평가 기반 리스크 조정.
    20봉 이후 MFE/MAE edge가 반복적으로 음수인 조합은 paper-only 또는 감액한다.
    """
    rows = _candidate_eval_rows(comparable_only=True)
    if not rows:
        return 1.0, []

    scope, selected = _select_quality_group(
        rows, symbol, tf_key, strategy, direction
    )

    if not selected:
        return 1.0, []

    n = selected["samples"]
    wr = selected["win_rate"]
    edge = selected["avg_edge"]
    note = (
        f"후보사후평가 {scope} {n}개: 승률 {wr*100:.0f}%, "
        f"edge {edge:+.2f}%"
    )

    asym_mult, asym_notes, _ = get_asymmetric_profile(symbol, tf_key, strategy, direction)
    if asym_mult > 1.0:
        return asym_mult, asym_notes

    if wr < SIGNAL_QUALITY_BAD_WR and edge < SIGNAL_QUALITY_BAD_EDGE:
        return 0.0, [note + " → 음수 기대값 paper-only"]
    if wr < SIGNAL_QUALITY_WEAK_WR or edge < 0:
        return SIGNAL_QUALITY_WEAK_MULT, [note + f" → 리스크×{SIGNAL_QUALITY_WEAK_MULT:.2f}"]
    if wr >= SIGNAL_QUALITY_GOOD_WR and edge >= SIGNAL_QUALITY_GOOD_EDGE:
        return SIGNAL_QUALITY_GOOD_MULT, [note + f" → 리스크×{SIGNAL_QUALITY_GOOD_MULT:.2f}"]
    return 1.0, [note + " → 중립"]


def get_quality_leverage_adjustment(symbol: str, tf_key: str, strategy: str,
                                    direction: str, base_leverage: int) -> tuple[int, list[str]]:
    """후보 사후승률/edge가 충분히 좋은 조합만 레버리지를 단계적으로 높인다."""
    if not WINRATE_LEVERAGE_ENABLED or base_leverage <= 0:
        return base_leverage, []

    rows = _candidate_eval_rows(comparable_only=True)
    if not rows:
        return base_leverage, []

    scope, selected = _select_quality_group(
        rows, symbol, tf_key, strategy, direction,
        exact_min=WINRATE_LEVERAGE_MIN_SAMPLES,
        strategy_min=max(WINRATE_LEVERAGE_MIN_SAMPLES, SIGNAL_QUALITY_MIN_STRATEGY),
        tf_min=max(WINRATE_LEVERAGE_MIN_SAMPLES * 2, SIGNAL_QUALITY_MIN_TF),
    )
    if not selected:
        return base_leverage, []

    wr = float(selected.get("win_rate", 0) or 0)
    edge = float(selected.get("avg_edge", 0) or 0)
    samples = int(selected.get("samples", 0) or 0)

    mult = 1.0
    tier = ""
    if wr >= WINRATE_LEVERAGE_EXCELLENT_WR and edge >= WINRATE_LEVERAGE_EXCELLENT_EDGE:
        mult = WINRATE_LEVERAGE_EXCELLENT_MULT
        tier = "EXCELLENT"
    elif wr >= WINRATE_LEVERAGE_GREAT_WR and edge >= WINRATE_LEVERAGE_GREAT_EDGE:
        mult = WINRATE_LEVERAGE_GREAT_MULT
        tier = "GREAT"
    elif wr >= WINRATE_LEVERAGE_GOOD_WR and edge >= WINRATE_LEVERAGE_GOOD_EDGE:
        mult = WINRATE_LEVERAGE_GOOD_MULT
        tier = "GOOD"

    if mult <= 1.0:
        return base_leverage, []

    new_leverage = min(max(int(round(base_leverage * mult)), base_leverage), WINRATE_LEVERAGE_MAX)
    if new_leverage == base_leverage:
        return base_leverage, []

    note = (
        f"승률기반 레버리지 {tier} {scope} {samples}개: "
        f"승률 {wr*100:.0f}%, edge {edge:+.2f}% → {base_leverage}x→{new_leverage}x"
    )
    return new_leverage, [note]


def build_signal_quality_report(limit: int = 5) -> str:
    rows = _candidate_eval_rows()
    if not rows:
        return "📡 <b>후보 신호 사후평가</b>\n   평가 데이터 부족"

    groups: dict[tuple, list] = defaultdict(list)
    core_groups: dict[tuple, list] = defaultdict(list)
    for row in rows:
        key = (row.get("tf", ""), row.get("strategy", ""), row.get("direction", ""))
        groups[key].append(row)
        core_groups[_row_core_strategy(row)].append(row)

    stats = []
    for key, group in groups.items():
        if len(group) < 5:
            continue
        st = _quality_stat(group)
        stats.append((st["avg_edge"], st["win_rate"], key, st))

    if not stats:
        return "📡 <b>후보 신호 사후평가</b>\n   유의미한 표본 부족"

    positive = [s for s in stats if s[0] > 0]
    negative = [s for s in stats if s[0] < 0]
    best = sorted(positive, key=lambda x: (x[0], x[1]), reverse=True)[:limit]
    worst = sorted(negative, key=lambda x: (x[0], x[1]))[:limit]

    lines = ["📡 <b>후보 신호 사후평가</b>"]
    lines.append(f"   최근 {SIGNAL_QUALITY_LOOKBACK_DAYS}일 평가후보 {len(rows)}개 기준")
    core_stats = []
    for key, group in core_groups.items():
        if len(group) < 5:
            continue
        st = _quality_stat(group)
        core_stats.append((st["avg_edge"], st["win_rate"], key, st))
    lines.append("   🧭 코어 전략군별")
    if core_stats:
        for edge, wr, key, st in sorted(core_stats, key=lambda x: x[0], reverse=True)[:limit]:
            family, core, direction = key
            lines.append(
                f"   • {family} / {core} {direction}: "
                f"{st['samples']}개 / 승률 {wr*100:.0f}% / edge {edge:+.2f}%"
            )
    else:
        lines.append("   • 전략군 표본 부족")
    lines.append("   ✅ 우수 조합")
    if best:
        for edge, wr, key, st in best:
            tf, strategy, direction = key
            lines.append(
                f"   • {tf} {strategy or '-'} {direction}: "
                f"{st['samples']}개 / 승률 {wr*100:.0f}% / edge {edge:+.2f}%"
            )
    else:
        lines.append("   • 양수 edge 조합 부족")
    lines.append("   ⚠️ 취약 조합")
    if worst:
        for edge, wr, key, st in worst:
            tf, strategy, direction = key
            lines.append(
                f"   • {tf} {strategy or '-'} {direction}: "
                f"{st['samples']}개 / 승률 {wr*100:.0f}% / edge {edge:+.2f}%"
            )
    else:
        lines.append("   • 음수 edge 조합 없음")

    asym = [
        item for item in stats
        if item[3].get("payoff", 0) >= ASYMMETRIC_MIN_PAYOFF
        and item[3].get("avg_win", 0) >= ASYMMETRIC_MIN_AVG_WIN
        and item[3].get("mfe_mae", 0) >= ASYMMETRIC_MIN_MFE_MAE
        and item[3].get("avg_edge", 0) >= ASYMMETRIC_MIN_EDGE_FLOOR
    ]
    asym = sorted(
        asym,
        key=lambda x: (x[3].get("payoff", 0), x[3].get("avg_win", 0)),
        reverse=True,
    )[:limit]
    lines.append("   🎯 비대칭 손익 조합")
    if asym:
        for _, wr, key, st in asym:
            tf, strategy, direction = key
            lines.append(
                f"   • {tf} {strategy or '-'} {direction}: "
                f"{st['samples']}개 / 승률 {wr*100:.0f}% / "
                f"평균승리 {st['avg_win']:+.2f}% / 평균손실 {st['avg_loss']:.2f}% / "
                f"payoff {st['payoff']:.1f}x"
            )
    else:
        lines.append("   • 조건 충족 조합 부족")

    exact_groups: dict[tuple, list] = defaultdict(list)
    for row in rows:
        key = (
            row.get("symbol", ""),
            row.get("tf", ""),
            row.get("strategy", ""),
            row.get("direction", ""),
        )
        exact_groups[key].append(row)
    exact_asym = []
    for key, group in exact_groups.items():
        if len(group) < ASYMMETRIC_MIN_SAMPLES:
            continue
        st = _quality_stat(group)
        if (
            st.get("payoff", 0) >= ASYMMETRIC_MIN_PAYOFF
            and st.get("avg_win", 0) >= ASYMMETRIC_MIN_AVG_WIN
            and st.get("mfe_mae", 0) >= ASYMMETRIC_MIN_MFE_MAE
            and st.get("avg_edge", 0) >= ASYMMETRIC_MIN_EDGE_FLOOR
        ):
            exact_asym.append((st.get("payoff", 0), st.get("avg_win", 0), key, st))
    exact_asym = sorted(
        exact_asym,
        key=lambda x: (x[0], x[1]),
        reverse=True,
    )[:limit]
    lines.append("   💎 심볼별 비대칭 우위")
    if exact_asym:
        for _, _, key, st in exact_asym:
            symbol, tf, strategy, direction = key
            coin = symbol.split("/")[0] if symbol else "-"
            lines.append(
                f"   • {coin} {tf} {strategy or '-'} {direction}: "
                f"{st['samples']}개 / 승률 {st['win_rate']*100:.0f}% / "
                f"평균승리 {st['avg_win']:+.2f}% / 평균손실 {st['avg_loss']:.2f}% / "
                f"payoff {st['payoff']:.1f}x"
            )
    else:
        lines.append("   • 표본 충족 조합 부족")
    return "\n".join(lines)


def get_risk_multiplier(tf_key: str, strategy: str = "",
                        symbol: str = "",
                        premium_mtf: bool = False) -> tuple[float, list[str]]:
    """
    계좌 드로우다운만 포지션 크기에 반영한다.
    반복 손실 조건의 실제 차단/강화는 get_realized_trade_adjustment에서 수행한다.
    반환: (risk_multiplier, notes)
    """
    s = _load()
    mult = 1.0
    notes = []

    drawdown_pct = float(s.get("drawdown_pct", 0) or 0)
    if drawdown_pct >= DRAWDOWN_RISK_OFF_PCT * 100:
        mult *= DRAWDOWN_RISK_MULT
        notes.append(f"계좌 DD {drawdown_pct:.1f}% 리스크×{DRAWDOWN_RISK_MULT:.2f}")
    elif drawdown_pct >= DRAWDOWN_WARN_PCT * 100:
        mult *= 0.50
        notes.append(f"계좌 DD {drawdown_pct:.1f}% 경고 리스크×0.50")

    return round(max(mult, MIN_DYNAMIC_RISK_MULT), 3), notes


def get_adaptive_min_rr() -> float:
    return min(get_adaptive_filters().get("min_rr", 1.5), ACTIVE_MAX_MIN_RR)

def get_adaptive_min_vol() -> float:
    return min(get_adaptive_filters().get("min_vol_ratio", 1.5), ACTIVE_MAX_MIN_VOL)

def get_adaptive_min_confirmed(tf_key: str, default: int = 4) -> int:
    mc = get_adaptive_filters().get("min_confirmed_by_tf", {})
    return mc.get(tf_key, default)

def get_adaptive_swing_freshness(tf_key: str, default: int = 99) -> int:
    sf = get_adaptive_filters().get("swing_freshness", {})
    return sf.get(tf_key, default)
