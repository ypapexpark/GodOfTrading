#!/usr/bin/env python3
from __future__ import annotations
"""
CryptoSignal — BTC/ETH 선물 다이버전스 스캐너 (RSI + MACD + OBV + StochRSI + Volume 5중 확인)
실행:
  python3 main.py              # 스캔 + 텔레그램 알림만
  python3 main.py --auto-trade # 스캔 + 텔레그램 + 자동매매
  python3 main.py --dry-run    # 출력만 (텔레그램/거래 없음)
"""
import sys
import socket
import time
from pathlib import Path
from dotenv import load_dotenv

from config import (SYMBOLS, TIMEFRAMES, STRICT_TF, SCALP_FRESHNESS, SWING_FRESHNESS,
                    MARGIN_BY_STRENGTH, MTF_POSITION_BOOST, MTF_POSITION_CAP, MODERATE_AUTO_TRADE,
                    GOLDEN_ENTRY_POSITION_PCT, GOLDEN_LEVERAGE_BOOST, GOLDEN_MAX_LEVERAGE,
                    PAPER_ONLY_STRENGTHS, RISK_PCT_BY_STRENGTH, SCALP_RISK_MULT,
                    GOLDEN_ENTRY_RISK_PCT, MAX_ACCOUNT_RISK_PCT, AUTO_TRADE_DIAGNOSTICS,
                    ACTIVE_STRONG_STRATEGIES, STRONG_LIVE_MAX_BARS_AGO, STRONG_LIVE_MIN_VOL)
from leading import get_market_context
from mtf import check_mtf, mtf_summary, get_macro_bias, get_daily_bias
from fetcher import fetch_ohlcv, fetch_market_radar, CORE_SYMBOLS, STOCK_SYMBOLS
from divergence import (detect, calc_vwap, detect_breakout,
                        get_freshness_score, check_candle_momentum, check_entry_zone)
from strategies import scan_additional
from formatter import build_alert, build_summary, calc_targets, _get_leverage, _raw_strength, SIGNAL_META
from publisher import send, send_review
from analyzer import (is_tradeable, get_adaptive_min_rr, get_adaptive_min_vol,
                      get_adaptive_min_confirmed, get_adaptive_swing_freshness,
                      get_adaptive_filters, analyze_and_adjust,
                      build_learning_report, build_loss_pattern_summary, build_next_strategy)

load_dotenv(Path(__file__).parent / ".env")

DRY_RUN    = "--dry-run"    in sys.argv
AUTO_TRADE = "--auto-trade" in sys.argv


def wait_for_network(host="8.8.8.8", port=53, max_wait=90) -> bool:
    for i in range(max_wait // 5):
        try:
            socket.setdefaulttimeout(3)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            return True
        except OSError:
            print(f"[네트워크] 대기 중... {(i+1)*5}초")
            time.sleep(5)
    return False


MIN_RR    = 1.5   # 이 R:R 미만이면 자동매매 스킵
SCALP_TF  = {"5m", "15m"}  # 스캘핑 모드 타임프레임 (신선도 체크 + 소액)

def _trend_min_confirm(tf_key: str, trend_score: int, is_continuation: bool) -> int | None:
    """
    추세 점수(주봉+일봉) 기반 최소 confirmed_count 반환.
    None = 진입 완전 차단.

    퀀트 원칙: 추세 이중 일치 = 포지션 보너스(+20%)이지 임계값 하향이 아님.
    임계값 하향은 노이즈 신호를 허용하는 것 — 퀀트는 품질을 타협하지 않는다.

      2/2 이중 일치: 약간 완화 (4,4,4,5,5) + 포지션 보너스 ×1.2
      1/2 단일 일치: 표준 임계값 (4,4,5,6,6)
      0/2 완전 역추세:
        continuation(hidden): 완전 차단 (역방향 추세추종 = 모순)
        reversal(bullish/bearish): ELITE(6/6)만
    """
    if trend_score == 2:
        return {"5m": 4, "15m": 4, "1h": 4, "4h": 5, "1d": 5}.get(tf_key, 4)
    if trend_score == 1:
        return {"5m": 4, "15m": 4, "1h": 5, "4h": 6, "1d": 6}.get(tf_key, 4)
    # trend_score == 0: 완전 역추세
    if is_continuation:
        return None   # hidden divergence 완전 역추세 = 차단
    return 6          # 반전 신호만 ELITE로 허용


def _try_auto_trade(symbol: str, tf_key: str, signals: list,
                    current_price: float, scalp: bool = False,
                    mtf_boost: float = 1.0):
    """신호가 있을 때 자동매매 실행 시도.
    복리형 베팅: 신호 강도별 계좌 위험률을 먼저 정하고,
    SL폭/레버리지로 증거금 비율을 역산한다.
    """
    from trader import (execute, get_usdt_balance, build_trade_notification,
                        _append_trade, add_trade_context, MAX_MARGIN_USD, MAX_SCALP_MARGIN_USD,
                        MAX_DAILY_LOSS, get_open_position_count, MAX_CONCURRENT,
                        get_daily_loss_limit, get_margin_cap, log_trade_candidate,
                        notify_trade_block, position_pct_for_risk, _load_state)

    best      = max(signals, key=lambda x: x["confirmed_count"])
    meta      = SIGNAL_META.get(best["signal_type"], SIGNAL_META["bullish"])
    direction = meta["direction"]
    strength  = best["strength"]
    raw       = _raw_strength(strength)
    ema_trend = best.get("ema_trend", 0)
    strategy  = best.get("strategy", best.get("signal_type", "다이버전스"))

    def _block(reason: str, send_diag: bool = False, **extra):
        notify_trade_block(
            symbol, tf_key, direction, strength, reason,
            strategy=strategy,
            send_telegram=(AUTO_TRADE_DIAGNOSTICS and send_diag),
            price=current_price,
            confirmed_count=best.get("confirmed_count", 0),
            bars_ago=best.get("bars_ago", 0),
            vol_ratio=best.get("vol", {}).get("value", 0),
            **extra,
        )

    # ── 동시 포지션 상한 (자본 집중 원칙) ──────────────────────────────────────
    open_cnt = get_open_position_count()
    if open_cnt >= MAX_CONCURRENT:
        _block(f"동시 {MAX_CONCURRENT}개 한도 도달({open_cnt}개) 또는 포지션 조회 실패", send_diag=True)
        return

    ema_aligned = (
        (direction == "LONG"  and ema_trend == 1) or
        (direction == "SHORT" and ema_trend == -1)
    )

    # MODERATE는 후보만 기록한다.
    # STRONG은 현재봉 기반 전략 + 거래량 + 방향성까지 맞을 때만 실거래로 승격한다.
    if raw in PAPER_ONLY_STRENGTHS:
        _block(f"{raw}는 후보 로그 전용 — 실거래는 VERY STRONG 이상부터", paper_only=True)
        return

    if raw == "STRONG":
        vol_r = best.get("vol", {}).get("value", 0)
        bars_ago = best.get("bars_ago", 99)
        current_strategy = strategy in ACTIVE_STRONG_STRATEGIES
        directional_ok = ema_aligned or mtf_boost > 1.0
        if not current_strategy:
            _block(f"STRONG 다이버전스는 후보 로그 전용 — 현재봉 전략 아님({strategy})", paper_only=True)
            return
        if bars_ago > STRONG_LIVE_MAX_BARS_AGO:
            _block(f"STRONG 현재봉 아님 — {bars_ago}봉 전 > {STRONG_LIVE_MAX_BARS_AGO}봉")
            return
        if vol_r < STRONG_LIVE_MIN_VOL:
            _block(f"STRONG 거래량 {vol_r:.1f}x < {STRONG_LIVE_MIN_VOL}x")
            return
        if not directional_ok:
            _block("STRONG 방향성 부족 — EMA 정렬 또는 MTF 부스트 없음")
            return
        print(f"  [STRONG-LIVE] {strategy} 현재봉 조건 충족 → 소액 실거래 후보")

    # ── EMA 중립 게이트 (VERY STRONG+ 대상) ──────────────────────────────────────
    if ema_trend == 0 and "ELITE" not in strength:
        _block("EMA 중립 — ELITE 아님")
        return

    # ── 황금 진입 판정: ELITE + MTF 전정렬 + EMA 방향일치 ──────────────────────
    is_golden = ("ELITE" in strength) and (mtf_boost > 1.0) and ema_aligned

    # 황금 진입이면 레버리지를 먼저 부스트해서 calc_targets에도 반영
    leverage = _get_leverage(strength, tf_key)
    if is_golden:
        leverage = min(int(leverage * GOLDEN_LEVERAGE_BOOST), GOLDEN_MAX_LEVERAGE)

    t = calc_targets(best, current_price, direction, leverage, tf_key, strength)
    if not t:
        _block("타겟 계산 실패", send_diag=True)
        return

    # R:R 필터
    # TP1 R:R: SL 1.5 ATR 기준 TP1이 최소 1.0:1 이상이어야 진입 가치 있음
    # best_rr(TP3 기준)만 보면 7ATR이 안 닿아도 통과 → TP1도 별도 체크
    active_min_rr = get_adaptive_min_rr() * (0.8 if is_golden else 1.0)
    best_rr  = max(tp["rr"] for tp in t["tps"])
    tp1_rr   = t["tps"][0]["rr"] if t["tps"] else 0
    if best_rr < active_min_rr:
        _block(f"R:R {best_rr} < {active_min_rr:.1f}", best_rr=best_rr, min_rr=active_min_rr)
        return
    if tp1_rr < 1.0:
        _block(f"TP1 R:R {tp1_rr} < 1.0 — 첫 TP조차 손절보다 작음", tp1_rr=tp1_rr)
        return

    balance_now = get_usdt_balance()
    if balance_now <= 0:
        _block("잔고 조회 실패 또는 잔고 0", send_diag=True)
        return

    # ── 포지션 비율 결정 ────────────────────────────────────────────────────────
    if is_golden:
        risk_pct = GOLDEN_ENTRY_RISK_PCT
        max_position_pct = GOLDEN_ENTRY_POSITION_PCT
        print(f"  💰 [황금진입💎] ELITE + MTF전정렬 + EMA정렬")
        print(f"     → 레버리지 {leverage}x | 목표 리스크 {risk_pct*100:.1f}% | 복리 최대 베팅")
        send(
            f"💰 <b>[황금 진입 발동 💎]</b> {symbol.split('/')[0]} {tf_key}\n"
            f"ELITE 신호 + MTF 전정렬 + EMA 방향일치\n"
            f"레버리지 <b>{leverage}x</b>  |  목표 계좌위험 <b>{risk_pct*100:.1f}%</b>\n"
            f"복리 최대 베팅 모드"
        )
    else:
        risk_pct = RISK_PCT_BY_STRENGTH.get(raw, 0.0)
        if risk_pct <= 0:
            _block(f"{raw} 리스크 설정 없음 — 후보만 기록", paper_only=True)
            return
        if scalp:
            risk_pct *= SCALP_RISK_MULT

        # EMA 방향일치 학습 부스트
        if ema_aligned:
            boost = get_adaptive_filters().get("ema_aligned_boost", 1.0)
            if boost > 1.0:
                risk_pct = min(risk_pct * boost, MAX_ACCOUNT_RISK_PCT)
                print(f"  [학습부스트] EMA 방향일치 → 리스크 {boost:.2f}x ({risk_pct*100:.1f}%)")

        # MTF 부스트
        if mtf_boost > 1.0:
            risk_pct = min(risk_pct * mtf_boost, MAX_ACCOUNT_RISK_PCT)
            print(f"  [MTF부스트] 상위봉 정렬 → 리스크 {mtf_boost:.2f}x ({risk_pct*100:.1f}%)")

        max_position_pct = min(MARGIN_BY_STRENGTH.get(raw, 0.25), MTF_POSITION_CAP)

    # SL 기준 계좌 위험률로 증거금 비율 역산
    position_pct, est_sl_loss = position_pct_for_risk(
        balance_now, leverage, current_price, t["sl"], risk_pct, max_position_pct
    )
    if position_pct <= 0:
        _block("리스크 기반 수량 계산 실패", send_diag=True, risk_pct=risk_pct)
        return

    daily_limit = get_daily_loss_limit(balance_now)
    daily_loss  = _load_state().get("daily_loss", 0)
    remaining_daily_risk = max(daily_limit - daily_loss, 0)
    if est_sl_loss > remaining_daily_risk:
        if remaining_daily_risk <= 0:
            _block(f"일손실 한도 소진 ${daily_loss:.2f} / ${daily_limit:.2f}", send_diag=True)
            return
        scale = remaining_daily_risk / est_sl_loss * 0.9
        position_pct = round(position_pct * scale, 4)
        est_sl_loss = round(est_sl_loss * scale, 4)
        print(f"  [리스크] 남은 일손실 한도 ${remaining_daily_risk:.2f} → 포지션 {position_pct*100:.1f}%로 조정")

    pct_label = f"{position_pct*100:.0f}%"

    # 실제 예상 SL 손실액 (황금 진입도 표시용으로 계산)
    max_m_final  = get_margin_cap(balance_now, scalp=scalp)
    est_margin_f = min(balance_now * position_pct, max_m_final)
    est_sl_loss  = min(est_sl_loss, est_margin_f * leverage * t["sl_pct"] / 100)

    # 5m 스캘핑: 단일 TP로 강제 (빠른 확정, 보유 없음)
    if tf_key == "5m" and len(t["tps"]) > 1:
        tps = [{"price": t["tps"][0]["price"], "pct": 100}]
        print("  [스캘핑] 5분봉 → 단일 TP 강제 (빠른 확정)")
    else:
        tps = [{"price": tp["price"], "pct": tp["pct"]} for tp in t["tps"]]

    golden_tag = " 💰황금진입" if is_golden else ""
    print(f"  [베팅{golden_tag}] {raw} → 잔고의 {pct_label}  목표리스크 {risk_pct*100:.1f}%  SL위험 ~${est_sl_loss:.1f}")

    result = execute(
        symbol       = symbol,
        direction    = direction,
        leverage     = leverage,
        entry_price  = current_price,
        sl           = t["sl"],
        tps          = tps,
        position_pct = position_pct,
        atr          = best.get("atr", 0.0),
        is_elite     = ("ELITE" in strength),
        max_margin_usd = max_m_final,
    )

    if result["ok"]:
        # 거래 이력 기록
        max_m    = max_m_final
        margin_r = min(balance_now * position_pct, max_m)
        trade_num = _append_trade(
            symbol, direction, tf_key, strength,
            result["leverage"], result["qty"],
            current_price, t["sl"], margin_r,
        )
        # 분석용 컨텍스트 추가 기록 (패인 분석에 사용)
        add_trade_context(
            trade_num,
            ema_trend       = best.get("ema_trend", 0),
            confirmed_count = best["confirmed_count"],
            vol_ratio       = best["vol"]["value"],
            bars_ago        = best.get("bars_ago", 0),
            sl_pct          = t["sl_pct"],
            risk_pct        = risk_pct,
            est_sl_loss     = est_sl_loss,
        )
        log_trade_candidate(
            symbol, tf_key, strategy, direction, strength, "opened",
            price=current_price, leverage=result["leverage"], qty=result["qty"],
            position_pct=position_pct, risk_pct=risk_pct,
            est_sl_loss=est_sl_loss, rr=best_rr,
        )
        scalp_tag = " [스캘핑]" if scalp else ""
        notif     = build_trade_notification(
            symbol, direction, result["leverage"],
            result["qty"], current_price, t["sl"], tps, balance_now,
        )
        send(notif)
        print(f"  [자동매매{scalp_tag}] ✅ {trade_num}회차 기록 ({pct_label}) — 텔레그램 발송")
    else:
        print(f"  [자동매매] ❌ 주문 실패: {result['error']}")
        log_trade_candidate(
            symbol, tf_key, strategy, direction, strength, "order_failed",
            result["error"], price=current_price, leverage=result["leverage"],
            position_pct=position_pct, risk_pct=risk_pct,
        )
        if AUTO_TRADE_DIAGNOSTICS:
            send(
                f"❌ <b>[자동매매 주문 실패]</b> {symbol.split('/')[0]} {tf_key} {direction}\n"
                f"강도: <b>{strength}</b>\n"
                f"사유: {result['error']}"
            )


def _log_gate_block(symbol: str, tf_key: str, signal: dict,
                    direction: str, reason: str, strategy: str = "게이트"):
    """_try_auto_trade 전 단계에서 차단된 후보도 학습용으로 남긴다."""
    if not AUTO_TRADE:
        return
    try:
        from trader import log_trade_candidate
        log_trade_candidate(
            symbol, tf_key, signal.get("strategy", strategy), direction,
            signal.get("strength", ""), "blocked", reason,
            signal_type=signal.get("signal_type", ""),
            price=signal.get("current_price", signal.get("pivot_price", 0)),
            confirmed_count=signal.get("confirmed_count", 0),
            bars_ago=signal.get("bars_ago", 0),
            vol_ratio=signal.get("vol", {}).get("value", 0),
            ema_trend=signal.get("ema_trend", 0),
        )
    except Exception as e:
        print(f"  [후보로그] 기록 실패: {e}")


def _try_breakout_trade(symbol: str, tf_key: str, bsig: dict, current_price: float):
    """
    추세 돌파 신호 자동매매.
    다이버전스와 달리 추세가 이미 결정된 뒤 합류 → 즉시 진입.

    SL: 돌파 레벨 바로 아래/위 (다이버전스 SL보다 타이트: 1.0 ATR)
    TP: VERY STRONG 기준 적용 (45%@2ATR + 35%@3.5ATR + 20%@5ATR)
    """
    from trader import (execute, get_usdt_balance, build_trade_notification,
                        _append_trade, add_trade_context, MAX_MARGIN_USD,
                        MAX_DAILY_LOSS, get_open_position_count, MAX_CONCURRENT,
                        has_open_position, get_daily_loss_limit, get_margin_cap,
                        log_trade_candidate, notify_trade_block,
                        position_pct_for_risk, _load_state)
    from config import TP_BY_STRENGTH, SL_ATR_MULT

    # 이미 포지션 있으면 스킵
    if has_open_position(symbol):
        notify_trade_block(symbol, tf_key, bsig["direction"], bsig["strength"],
                           "이미 오픈 포지션 있음", strategy="돌파")
        return

    open_cnt = get_open_position_count()
    if open_cnt >= MAX_CONCURRENT:
        notify_trade_block(symbol, tf_key, bsig["direction"], bsig["strength"],
                           "동시 포지션 한도 또는 포지션 조회 실패", strategy="돌파",
                           send_telegram=AUTO_TRADE_DIAGNOSTICS)
        return

    balance_now = get_usdt_balance()
    daily_loss  = _load_state().get("daily_loss", 0)
    daily_limit = get_daily_loss_limit(balance_now)
    if daily_loss >= daily_limit:
        notify_trade_block(symbol, tf_key, bsig["direction"], bsig["strength"],
                           f"일일 손실 ${daily_loss:.2f} / 한도 ${daily_limit:.2f}",
                           strategy="돌파", send_telegram=AUTO_TRADE_DIAGNOSTICS)
        return

    direction = bsig["direction"]
    atr       = bsig["atr"]
    strength  = bsig["strength"]

    # 돌파 SL: 돌파 레벨 바로 밖 1.0 ATR (추세를 못 이어가면 즉시 철수)
    sl_mult   = 1.0
    sl        = (current_price - atr * sl_mult) if direction == "LONG" else (current_price + atr * sl_mult)

    # TP: VERY STRONG 기준 (4개 조건 확인 = VERY STRONG 수준)
    tp_plan = TP_BY_STRENGTH.get("VERY STRONG", TP_BY_STRENGTH["VERY STRONG"])
    tps = []
    for tp in tp_plan:
        tp_price = (current_price + atr * tp["atr_mult"]) if direction == "LONG" \
                   else (current_price - atr * tp["atr_mult"])
        rr = (abs(tp_price - current_price) / abs(current_price - sl)) if abs(current_price - sl) > 0 else 0
        tps.append({"price": round(tp_price, 4), "pct": tp["pct"], "rr": round(rr, 2)})

    # TP1 R:R 체크
    if tps[0]["rr"] < 1.0:
        notify_trade_block(symbol, tf_key, direction, strength,
                           f"TP1 R:R {tps[0]['rr']} < 1.0",
                           strategy="돌파", tp1_rr=tps[0]["rr"])
        return

    # 포지션 크기: 복리 리스크 엔진 적용
    from config import MARGIN_BY_STRENGTH, LEVERAGE_MAP
    raw_strength = _raw_strength(strength)
    position_cap = MARGIN_BY_STRENGTH.get(raw_strength, MARGIN_BY_STRENGTH.get("VERY STRONG", 0.25))
    leverage     = LEVERAGE_MAP.get((raw_strength, tf_key), 7)

    sl_pct = abs(current_price - sl) / current_price * 100
    risk_pct = RISK_PCT_BY_STRENGTH.get(raw_strength, RISK_PCT_BY_STRENGTH["VERY STRONG"])
    position_pct, est_sl_loss = position_pct_for_risk(
        balance_now, leverage, current_price, sl, risk_pct, position_cap
    )
    if position_pct <= 0:
        notify_trade_block(symbol, tf_key, direction, strength,
                           "리스크 기반 수량 계산 실패", strategy="돌파",
                           send_telegram=AUTO_TRADE_DIAGNOSTICS)
        return
    remaining_daily_risk = max(daily_limit - daily_loss, 0)
    if est_sl_loss > remaining_daily_risk:
        scale = remaining_daily_risk / est_sl_loss * 0.9 if est_sl_loss > 0 else 0
        if scale <= 0:
            notify_trade_block(symbol, tf_key, direction, strength,
                               "남은 일손실 한도 없음", strategy="돌파",
                               send_telegram=AUTO_TRADE_DIAGNOSTICS)
            return
        position_pct = round(position_pct * scale, 4)
        est_sl_loss = round(est_sl_loss * scale, 4)

    print(f"  [돌파매매] {direction}  레벨:{bsig['breakout_level']:,.2f}  "
          f"vol:{bsig['vol']['value']:.1f}x  ATR확장:{bsig['atr_expand']}")
    print(f"  [돌파SL] ±{sl_pct:.1f}%  TP1 R:R {tps[0]['rr']:.1f}:1  "
          f"리스크 {risk_pct*100:.1f}%  포지션 {position_pct*100:.1f}%")

    max_margin = get_margin_cap(balance_now, scalp=False)

    result = execute(
        symbol       = symbol,
        direction    = direction,
        leverage     = leverage,
        entry_price  = current_price,
        sl           = sl,
        tps          = tps,
        position_pct = position_pct,
        atr          = atr,
        is_elite     = ("ELITE" in strength),
        max_margin_usd = max_margin,
    )

    if result["ok"]:
        margin_r = min(balance_now * position_pct, max_margin)
        trade_num = _append_trade(
            symbol, direction, tf_key, strength,
            result["leverage"], result["qty"],
            current_price, sl, margin_r,
        )
        add_trade_context(
            trade_num,
            ema_trend       = bsig.get("ema_trend", 0),
            confirmed_count = bsig["confirmed_count"],
            vol_ratio       = bsig["vol"]["value"],
            bars_ago        = 0,
            sl_pct          = sl_pct,
            risk_pct        = risk_pct,
            est_sl_loss     = est_sl_loss,
        )
        log_trade_candidate(
            symbol, tf_key, "돌파", direction, strength, "opened",
            price=current_price, leverage=result["leverage"], qty=result["qty"],
            position_pct=position_pct, risk_pct=risk_pct,
            est_sl_loss=est_sl_loss, rr=max(tp["rr"] for tp in tps),
        )
        notif = build_trade_notification(
            symbol, direction, result["leverage"],
            result["qty"], current_price, sl, tps, balance_now,
        )
        send(f"📈 <b>[돌파 자동매매]</b> {symbol.split('/')[0]} {tf_key} {direction}\n"
             f"구조 레벨 {bsig['breakout_level']:,.2f} 돌파  VOL {bsig['vol']['value']:.1f}x\n\n" + notif)
        print(f"  [돌파매매] ✅ {trade_num}회차 기록")
    else:
        print(f"  [돌파매매] ❌ 주문 실패: {result['error']}")
        log_trade_candidate(
            symbol, tf_key, "돌파", direction, strength, "order_failed",
            result["error"], price=current_price, leverage=result["leverage"],
            position_pct=position_pct, risk_pct=risk_pct,
        )


def _do_pyramid(symbol: str, tf_key: str, direction: str,
                entry_price: float, current_price: float,
                atr: float, pyramid_level: int):
    """
    불타기(Pyramid) 진입 실행.

    진입 조건:
    - 1회: 원래 진입가 대비 +1.5 ATR 수익 중
    - 2회: 원래 진입가 대비 +3.0 ATR 수익 중

    포지션 크기: 감소형 (1회=원래의 60% / 2회=원래의 30%)
    SL: 기존 SL 유지 (추세가 반전되면 전체 청산)
    """
    from trader import (execute, get_usdt_balance, add_pyramid_entry,
                        build_pyramid_notification, MAX_MARGIN_USD)
    from config import MARGIN_BY_STRENGTH, LEVERAGE_MAP, TP_BY_STRENGTH

    balance_now  = get_usdt_balance()
    base_pct     = MARGIN_BY_STRENGTH.get("VERY STRONG", 0.18)
    pyramid_pcts = [base_pct * 0.60, base_pct * 0.30]   # 1회: 60%, 2회: 30%
    position_pct = pyramid_pcts[pyramid_level - 1]
    leverage     = LEVERAGE_MAP.get(("VERY STRONG", tf_key), 7)

    # 현재 수익 ATR 계산
    profit_atr = (
        (current_price - entry_price) / atr if direction == "LONG"
        else (entry_price - current_price) / atr
    )

    # TP: 현재 가격 기준 추가 이익 목표 (동일 비율)
    tp_plan = TP_BY_STRENGTH.get("VERY STRONG", [])
    tps = []
    for tp in tp_plan:
        tp_price = (current_price + atr * tp["atr_mult"]) if direction == "LONG" \
                   else (current_price - atr * tp["atr_mult"])
        tps.append({"price": round(tp_price, 4), "pct": tp["pct"], "rr": tp["atr_mult"]})

    print(f"  [불타기{pyramid_level}] {direction}  진입가:{entry_price:,.2f}→{current_price:,.2f}"
          f"  수익:{profit_atr:+.1f}ATR  추가 {position_pct*100:.0f}%")

    result = execute(
        symbol       = symbol,
        direction    = direction,
        leverage     = leverage,
        entry_price  = current_price,
        sl           = entry_price if direction == "LONG" else entry_price,  # breakeven SL
        tps          = tps,
        position_pct = position_pct,
        atr          = atr,
        is_elite     = False,
    )

    if result["ok"]:
        margin_r = min(balance_now * position_pct, MAX_MARGIN_USD)
        add_pyramid_entry(symbol, tf_key, current_price, margin_r, result["qty"])
        notif = build_pyramid_notification(
            symbol, direction, tf_key,
            pyramid_level, current_price, margin_r, profit_atr, balance_now,
        )
        send(notif)
        print(f"  [불타기{pyramid_level}] ✅ 추가진입 완료 — 텔레그램 발송")
    else:
        print(f"  [불타기{pyramid_level}] ❌ 주문 실패: {result['error']}")


def _fmt_pnl(pnl: float) -> str:
    return f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"


def _build_cumulative_section(cs: dict, balance: float) -> list[str]:
    """섹션 1: 전체 누적 성과 블록."""
    lines = ["━━━ 📈 전체 누적 성과 ━━━"]

    if cs["closed"] == 0:
        lines.append("아직 종료된 거래 없음")
        return lines

    pnl_sign = "+" if cs["total_pnl"] >= 0 else ""
    lines += [
        f"총 거래 <b>{cs['total']}회</b>  (종료 {cs['closed']}회 | 진행중 {cs['open_cnt']}회)",
        f"승/패: <b>{cs['wins']}승 {cs['losses']}패</b>  |  승률 <b>{cs['win_rate']}%</b>",
        f"누적 PnL: <b>{pnl_sign}${cs['total_pnl']}</b>",
        f"잔고: <b>${balance:,.2f}</b>",
        "",
        f"평균 이익: +${cs['avg_win']:.2f}  |  평균 손실: ${cs['avg_loss']:.2f}",
        f"Profit Factor: <b>{cs['profit_factor']:.2f}</b>  "
        f"{'✅ 양의 기댓값' if cs['profit_factor'] >= 1.5 else '⚠️ 개선 필요' if cs['profit_factor'] >= 1.0 else '❌ 음의 기댓값'}",
    ]

    if cs["best_trade"]:
        b = cs["best_trade"]
        lines.append(f"최대 이익: +${b['pnl_usd']:.2f}  ({b['symbol'].split('/')[0]} {b.get('tf','')} #{b['num']})")
    if cs["worst_trade"]:
        w = cs["worst_trade"]
        lines.append(f"최대 손실: ${w['pnl_usd']:.2f}  ({w['symbol'].split('/')[0]} {w.get('tf','')} #{w['num']})")

    # 연속 기록
    cw_tag = f"  ← 현재 {cs['cur_consec_win']}연승 중 🔥" if cs["cur_consec_win"] >= 2 else ""
    cl_tag = f"  ← 현재 {cs['cur_consec_loss']}연패 중 ⚠️" if cs["cur_consec_loss"] >= 2 else ""
    lines += [
        f"최장 연승: {cs['max_consec_win']}회{cw_tag}",
        f"최장 연패: {cs['max_consec_loss']}회{cl_tag}",
    ]
    return lines


def _build_today_section(today_trades: list) -> list[str]:
    """섹션 2: 오늘의 매매내역 블록."""
    from datetime import datetime, timezone, timedelta
    from analyzer import _analyze_failure

    KST_tz   = timezone(timedelta(hours=9))
    today_str = datetime.now(KST_tz).strftime("%m/%d")
    STATUS    = {"win": ("🟢", "✅ 이익"), "loss": ("🔴", "❌ 손실"),
                 "breakeven": ("⚪", "〰 본전"), "open": ("🟡", "⏳ 진행중")}
    TREND_LABEL = {1: "📈상승", -1: "📉하락", 0: "➡️중립"}

    lines = [f"━━━ 📅 오늘 매매내역 ({today_str}) ━━━"]

    if not today_trades:
        lines.append("오늘 거래 없음")
        return lines

    wins   = [t for t in today_trades if t["status"] == "win"]
    losses = [t for t in today_trades if t["status"] == "loss"]
    closed = len(wins) + len(losses)
    today_pnl  = round(sum(t["pnl_usd"] for t in today_trades), 2)
    pnl_sign   = "+" if today_pnl >= 0 else ""
    wr_str     = f"{len(wins)/closed*100:.0f}%" if closed > 0 else "-"

    lines += [
        f"총 <b>{len(today_trades)}</b>회  |  {len(wins)}승 {len(losses)}패  "
        f"|  오늘 PnL <b>{pnl_sign}${today_pnl}</b>  |  승률 {wr_str}",
        "",
    ]

    for t in reversed(today_trades):  # 최신 순
        emoji, label = STATUS.get(t["status"], ("🟡", ""))
        coin = t["symbol"].split("/")[0]
        pnl_str = f"  {_fmt_pnl(t['pnl_usd'])}" if t["status"] in ("win", "loss", "breakeven") else ""

        lines.append(
            f"{emoji} <b>{t['num']}회차</b>  {coin} {t.get('tf','')}  "
            f"{t['strength']}  {t['direction']}"
        )
        lines.append(
            f"   진입 ${t['entry_price']:,.2f}  ×{t['leverage']}x"
            f"  |  {label}{pnl_str}"
        )

        ctx = []
        if t.get("ema_trend") is not None:
            ctx.append(f"EMA:{TREND_LABEL.get(t['ema_trend'], '')}")
        if t.get("vol_ratio"):
            ctx.append(f"VOL:{t['vol_ratio']:.1f}x")
        if t.get("bars_ago"):
            ctx.append(f"신호{t['bars_ago']}봉전")
        if ctx:
            lines.append("   " + "  ".join(ctx))

        if t["status"] == "loss":
            reasons = _analyze_failure(t)
            plans   = build_next_strategy(t)
            lines.append("   ─ 원인:")
            for r in reasons:
                lines.append(f"   └ {r}")
            lines.append("   ─ 📝 다음 전략:")
            for p in plans:
                lines.append(f"   {p}")

        if t.get("closed_at"):
            lines.append(f"   청산 {t['closed_at']}")
        elif t["status"] == "open":
            lines.append(f"   진입 {t.get('time','')}")
        lines.append("")

    return lines


def _build_trade_report(today_trades: list, cs: dict,
                        daily_loss: float, balance: float,
                        learning_notes: list) -> str:
    """4시간 결산 텔레그램 메시지 — 누적 성과 + 오늘 매매내역 2섹션."""
    from trader import MAX_DAILY_LOSS
    from datetime import datetime, timezone, timedelta

    KST_tz  = timezone(timedelta(hours=9))
    now_kst = datetime.now(KST_tz).strftime("%m/%d %H:%M KST")

    lines = [
        f"📊 <b>[CryptoSignal 4시간 결산]</b> — {now_kst}",
        "",
    ]

    # ── 섹션 1: 전체 누적 성과 ───────────────────────────────────────────────
    lines += _build_cumulative_section(cs, balance)
    lines.append("")

    # ── 섹션 2: 오늘 매매내역 ────────────────────────────────────────────────
    lines += _build_today_section(today_trades)

    # ── 하단: 리스크/학습 요약 ───────────────────────────────────────────────
    lines += [
        "─────────────────────",
        f"💼 오늘 누적손실: ${daily_loss:.2f} / 한도 ${MAX_DAILY_LOSS:.0f}",
    ]

    today_losses = [t for t in today_trades if t["status"] == "loss"]
    if today_losses:
        pattern = build_loss_pattern_summary(today_losses)
        if pattern:
            lines.append(f"🔎 공통 패인: {pattern}")

    if learning_notes:
        lines.append("🔧 학습 조정:")
        for note in learning_notes:
            lines.append(f"   • {note}")

    return "\n".join(lines)


def _maybe_send_periodic_report():
    """4시간마다 거래 결산 + 학습 분석 텔레그램 발송."""
    from trader import (_load_state, _save_state,
                        get_today_trades, get_cumulative_stats, get_usdt_balance)

    s = _load_state()
    if time.time() - s.get("last_report_time", 0) < 4 * 3600:
        return

    # 학습 파라미터 자동 조정
    adjustments = analyze_and_adjust()
    if adjustments:
        adj_msg = (
            "🔧 <b>[CryptoSignal 자동 학습]</b>\n" +
            "\n".join(f"  • {a}" for a in adjustments)
        )
        send(adj_msg)
        print(f"  [학습] {len(adjustments)}개 파라미터 조정 → 텔레그램 발송")

    today_trades = get_today_trades()
    cs           = get_cumulative_stats()
    daily_loss   = s.get("daily_loss", 0.0)
    balance      = get_usdt_balance()

    msg = _build_trade_report(today_trades, cs, daily_loss, balance, adjustments)
    delivered = send_review(msg) or send(msg)
    if delivered:
        s = _load_state()
        s["last_report_time"] = time.time()
        _save_state(s)
        print("  [결산] 4시간 거래 결산 발송 완료")


def _print_radar(radar: list[dict]) -> None:
    """거래량 Top10 Market Radar 출력."""
    import datetime, zoneinfo
    KST = zoneinfo.ZoneInfo("Asia/Seoul")
    now = datetime.datetime.now(KST).strftime("%H:%M KST")

    print("=" * 60)
    print(f"  🎯  바이빗 선물 Market Radar  ({now})")
    print("  ─" * 30)
    print(f"  {'#':>2}  {'심볼':<10} {'현재가':>14}  {'24h':>7}  {'거래대금':>8}")
    print("  ─" * 30)
    for r in radar:
        coin    = r["symbol"].split("/")[0]
        chg     = r["change_pct"]
        arrow   = "▲" if chg > 0 else ("▼" if chg < 0 else "─")
        chg_str = f"{arrow}{abs(chg):.1f}%"
        print(f"  #{r['rank']:>2}  {coin:<10} ${r['last']:>13,.4f}  {chg_str:>7}  {r['volume_label']:>8}")
    print("=" * 60)


def _reconcile_orphan_positions():
    """
    Bybit 실제 포지션 vs trade_state.json 추적 포지션 비교.
    미추적(orphan) 포지션 발견 시:
      - 텔레그램 경고 발송
      - PnL > -30% 손실: 경고만 (SL 미설정)
      - PnL <= -30% 손실: 긴급 SL 자동 설정 (현재가 기준 3ATR 위 / 아래)
    퀀트 원칙: SL 없는 포지션은 즉시 관리 대상.
    """
    from trader import fetch_all_positions_raw, place_emergency_sl, _load_state, _save_state

    all_positions = fetch_all_positions_raw()
    if not all_positions:
        return

    s = _load_state()
    tracked = set(s.get("positions", {}).keys())

    orphans = [p for p in all_positions if p["symbol"] not in tracked]
    if not orphans:
        return

    now = time.time()
    cooldown = 6 * 3600
    alerts = s.setdefault("orphan_alerts", {})
    fresh_orphans = []
    for p in orphans:
        key = f"{p['symbol']}:{p['direction']}:{p['entry_price']}:{p['qty']}"
        if now - alerts.get(key, 0) >= cooldown:
            fresh_orphans.append((key, p))

    if not fresh_orphans:
        return

    lines = [f"⚠️ <b>[미추적 포지션 발견 — SL 없음]</b>\n봇이 열지 않은 포지션 {len(fresh_orphans)}개\n"]
    for alert_key, o in fresh_orphans:
        sym       = o["symbol"]
        direction = o["direction"]
        entry     = o["entry_price"]
        mark      = o["mark_price"]
        qty       = o["qty"]
        lev       = int(o["leverage"])
        pnl       = o["unrealized_pnl"]
        margin    = o.get("margin", 0) or (entry * qty / max(lev, 1))
        margin    = margin if margin > 0 else abs(pnl) + 0.01

        move_pct = ((mark - entry) / entry * 100) if entry > 0 else 0
        if direction == "SHORT":
            move_pct = -move_pct
        margin_pct = (pnl / margin * 100) if margin > 0 else 0

        sign = "+" if pnl >= 0 else ""
        emoji = "🟢" if pnl >= 0 else "🔴"

        lines.append(
            f"{emoji} <b>{sym.split('/')[0]} {direction}</b>  {lev}x\n"
            f"  진입 ${entry:,.2f} → 현재 ${mark:,.2f}  ({move_pct:+.1f}%)\n"
            f"  미실현: <b>{sign}${pnl:.2f}</b>  (증거금대비 {margin_pct:+.0f}%)"
        )

        # 증거금 -30% 초과 손실 = 긴급 SL 자동 설정
        if margin_pct <= -30 and mark > 0:
            # ATR 대체값: 현재가의 1.5% (실시간 ATR 계산 없이 보수적 거리)
            atr_proxy = mark * 0.015
            if direction == "LONG":
                sl_price = round(mark * 0.985, 4)   # 현재가 -1.5%: 추가 하락 여유
            else:
                sl_price = round(mark * 1.015, 4)   # 현재가 +1.5%

            ok = place_emergency_sl(sym, direction, qty, sl_price)
            if ok:
                lines.append(f"  🛑 긴급 SL 자동 설정: ${sl_price:,.2f}  (현재가 ±1.5%)")
            else:
                lines.append(f"  ❌ 긴급 SL 설정 실패 — Bybit에서 직접 설정 필요")
        else:
            lines.append(f"  📌 SL 미설정 — Bybit에서 직접 설정 권장")
        lines.append("")
        alerts[alert_key] = now

    lines.append("💡 관리 방법: Bybit에서 해당 포지션에 SL을 설정하거나 청산하세요.")
    send("\n".join(lines))
    _save_state(s)
    print(f"[조정] 미추적 포지션 {len(fresh_orphans)}개 처리 완료")


def scan():
    # ── Step 0: 포지션 모니터링 먼저 ───────────────────────────────────────────
    if AUTO_TRADE:
        from trader import monitor_positions, evaluate_trade_candidates
        monitor_positions()
        _reconcile_orphan_positions()
        eval_notes = evaluate_trade_candidates()
        if eval_notes:
            print(f"  [후보평가] {len(eval_notes)}개 완료 — MFE/MAE 학습 로그 누적")
        _maybe_send_periodic_report()

    # ── Step 1: 바이빗 거래량 Top10 조회 ──────────────────────────────────────
    # 진입 전 항상 현재 시장 거래량 상위 종목 파악 → 유동성 집중 종목 우선 스캔
    radar        = fetch_market_radar(n=10)
    radar_syms   = [r["symbol"] for r in radar]

    # 코어 종목(BTC/ETH/SOL) + Top10 합산, Top10 순서 우선
    # Top10 → 코어(BTC/ETH/SOL) → 토큰화주식(NVDA/TSLA) 순서로 우선순위
    scan_symbols = list(dict.fromkeys(radar_syms + CORE_SYMBOLS + STOCK_SYMBOLS))

    mode_tag = "[DRY RUN]" if DRY_RUN else ("[AUTO TRADE]" if AUTO_TRADE else "")
    print(f"\n{'='*60}")
    print(f"  📡 CryptoSignal 스캔  {mode_tag}")
    print(f"{'='*60}")

    # ── Step 2: Market Radar 출력 ──────────────────────────────────────────────
    if radar:
        _print_radar(radar)
        new_coins = [s.split("/")[0] for s in radar_syms if s not in CORE_SYMBOLS]
        print(f"  → Top10 기준 스캔 대상: {len(scan_symbols)}종목")
        if new_coins:
            print(f"  → 동적 편입: {', '.join(new_coins[:5])}{'...' if len(new_coins)>5 else ''}")
    else:
        print("  [Radar] 데이터 없음 — 기본 종목으로 진행")
        scan_symbols = list(SYMBOLS)

    print()

    total_signals = 0
    total_scanned = 0

    for symbol in scan_symbols:
        coin = symbol.split("/")[0]
        # 심볼 유효성 사전 체크 — Bybit에 없는 종목(ZEC 등) 스캔 낭비 방지
        try:
            _chk = fetch_ohlcv(symbol, "1h", 5)
            if _chk is None or len(_chk) == 0:
                print(f"\n⚠️  {coin} — 데이터 없음, 스킵")
                continue
        except Exception:
            print(f"\n⚠️  {coin} — Bybit 미지원 심볼, 스킵")
            continue

        print(f"\n🔍 {coin} 스캔 중...")
        # 주봉 + 일봉 추세 바이어스 — 심볼당 1회 조회 (각각 1h/30분 캐시)
        macro = get_macro_bias(symbol)
        daily = get_daily_bias(symbol)
        _bicon = {"LONG": "📈", "SHORT": "📉", "NEUTRAL": "➡️"}
        print(f"  [주봉] {macro['note']}  {_bicon[macro['direction']]} {macro['direction']} ({macro['strength']})")
        print(f"  [일봉] {daily['note']}  {_bicon[daily['direction']]} {daily['direction']} ({daily['strength']})")

        for tf_key, tf_info in TIMEFRAMES.items():
            tf_label = tf_info["label"]
            limit    = tf_info["limit"]
            total_scanned += 1

            try:
                df = fetch_ohlcv(symbol, tf_key, limit)
            except Exception as e:
                print(f"  [{tf_label}] 데이터 오류: {e}")
                continue

            signals = detect(df)
            current_price = float(df["close"].iloc[-1])
            for _sig in signals:
                _sig["current_price"] = current_price

            # 학습 필터: 이 TF / 심볼이 현재 제외 중인지 확인
            if AUTO_TRADE:
                tradeable, reason = is_tradeable(symbol, tf_key)
                if not tradeable:
                    print(f"  [{tf_label}] ⛔ {reason}")
                    # 알림은 보내되 자동매매만 스킵 (신호는 계속 표시)

            # 5m/15m는 적응형 confirmed 기준 적용 (기본 4, 학습으로 5까지 올라갈 수 있음)
            if tf_key in STRICT_TF:
                min_c = get_adaptive_min_confirmed(tf_key, default=4)
                signals = [s for s in signals if s["confirmed_count"] >= min_c]

            if signals:
                best      = max(signals, key=lambda x: x["confirmed_count"])
                meta      = SIGNAL_META.get(best["signal_type"], SIGNAL_META["bullish"])
                direction = meta["direction"]
                strength  = best["strength"]
                is_moderate = "MODERATE" in strength

                # ── 추세 점수 (주봉 + 일봉) ─────────────────────────────────
                trend_score = sum(
                    1 for b in [macro, daily]
                    if b["direction"] not in ("NEUTRAL",) and b["direction"] == direction
                )
                is_continuation = best["signal_type"] in ("hidden_bullish", "hidden_bearish")
                # 이중 일치 = 포지션 보너스(임계값 하향 아님)
                trend_boost = 1.20 if trend_score == 2 else 1.0
                _ts_label = ["❌ 역추세(0/2)", "⭐ 추세1/2", "⭐⭐ 추세2/2(+보너스)"][trend_score]
                print(f"  [추세점수] {_ts_label}  {'continuation' if is_continuation else 'reversal'}")

                print(f"  [{tf_label}] {strength}  |  {len(signals)}개 신호  |  ${current_price:,.2f}")
                for s in signals:
                    marks = (
                        "✅" if s["rsi"]["ok"]  else "❌",
                        "✅" if s["macd"]["ok"] else "❌",
                        "✅" if s["obv"]["ok"]  else "❌",
                        "✅" if s["srsi"]["ok"] else "❌",
                        "✅" if s["vol"]["ok"]  else "❌",
                        "✅" if s.get("cvd", {}).get("ok") else "❌",
                    )
                    print(f"    {s['signal_type']:15s}  RSI{marks[0]} MACD{marks[1]} OBV{marks[2]} SRSI{marks[3]} VOL{marks[4]} CVD{marks[5]}  ({s['confirmed_count']}/6)")

                # MTF 확인 (AUTO_TRADE 여부와 무관하게 항상 조회해서 알림에 표시)
                mtf_info = check_mtf(symbol, tf_key, direction)
                print(f"  [{tf_label}] MTF({mtf_info['score']}/{mtf_info['max_score']}): "
                      f"{'✅전정렬' if mtf_info['strong'] else '⛔역방향' if mtf_info['block'] else '⚡부분'}"
                      f"  {'(포지션 부스트)' if mtf_info['strong'] else '(차단됨)' if mtf_info['block'] else ''}")

                msg = build_alert(symbol, tf_label, tf_key, signals, current_price,
                                  mtf_info=mtf_info)
                total_signals += len(signals)

                if DRY_RUN:
                    print(msg)
                else:
                    ok = send(msg)
                    print(f"  텔레그램: {'✅' if ok else '❌'}")

                    if AUTO_TRADE:
                        # MODERATE 신호 — MTF 전정렬 시만 허용 (추세점수는 downstream 필터로 처리)
                        _moderate_ok = (not is_moderate) or MODERATE_AUTO_TRADE or mtf_info["strong"]
                        if not _moderate_ok:
                            print(f"  [{tf_label}] MODERATE(3/6) 알림만 (MTF 전정렬 아님)")
                            _log_gate_block(symbol, tf_key, best, direction,
                                            "MODERATE 알림 전용 — MTF 전정렬 아님")
                        # MTF 역방향 차단
                        elif mtf_info["block"]:
                            print(f"  [MTF] 전 상위봉 역방향 → 자동매매 차단")
                            _log_gate_block(symbol, tf_key, best, direction,
                                            "MTF 전 상위봉 역방향")
                            send(f"⛔ <b>[MTF 차단]</b> {symbol.split('/')[0]} {tf_label}\n"
                                 f"{mtf_summary(mtf_info)}\n역방향 추세 — 자동매매 스킵")
                        # 학습 필터: 제외 중인 심볼/TF는 자동매매 스킵
                        elif not is_tradeable(symbol, tf_key)[0]:
                            _ok2, _why2 = is_tradeable(symbol, tf_key)
                            print(f"  [학습] {_why2} → 자동매매 스킵")
                            _log_gate_block(symbol, tf_key, best, direction, _why2)
                        elif tf_key in SCALP_TF:
                            # 스캘핑: 신선도 체크
                            max_bars = SCALP_FRESHNESS.get(tf_key, 8)
                            fresh = [s for s in signals if s.get("bars_ago", 99) <= max_bars]
                            if not fresh:
                                oldest = min(s.get("bars_ago", 99) for s in signals)
                                print(f"  [{tf_label}] 신호 {oldest}봉 전 — 스캘핑은 {max_bars}봉 이내만 (스킵)")
                                _log_gate_block(symbol, tf_key, best, direction,
                                                f"스캘핑 신선도 초과 {oldest}봉 > {max_bars}봉")
                            else:
                                # ── 프로 스캘핑 필터 (VWAP + 캔들 전환) ──────────
                                best_fresh = max(fresh, key=lambda x: x["confirmed_count"])
                                direction  = SIGNAL_META.get(
                                    best_fresh["signal_type"], SIGNAL_META["bullish"]
                                )["direction"]
                                # ── 추세 게이트 (주봉+일봉 통합 — scalp) ──────
                                _sc_ts = sum(
                                    1 for b in [macro, daily]
                                    if b["direction"] not in ("NEUTRAL",) and b["direction"] == direction
                                )
                                _sc_cont = best_fresh["signal_type"] in ("hidden_bullish", "hidden_bearish")
                                _sc_min  = _trend_min_confirm(tf_key, _sc_ts, _sc_cont)
                                if _sc_min is None:
                                    print(f"  [{tf_label}] 역추세 continuation — 차단")
                                    _log_gate_block(symbol, tf_key, best_fresh, direction,
                                                    "역추세 continuation")
                                    continue
                                if best_fresh["confirmed_count"] < _sc_min:
                                    _ts_tag = ["❌역추세", "⭐단일일치", "⭐⭐이중일치"][_sc_ts]
                                    print(f"  [{tf_label}] {_ts_tag} {best_fresh['confirmed_count']}/{_sc_min} 미달 스킵")
                                    _log_gate_block(symbol, tf_key, best_fresh, direction,
                                                    f"추세 기준 미달 {best_fresh['confirmed_count']}/{_sc_min}")
                                    continue
                                if _sc_ts == 2:
                                    print(f"  [추세] ⭐⭐ 이중 추세 일치 → 포지션 +20% 보너스")
                                elif _sc_ts == 0:
                                    print(f"  [추세] ❌ 역추세 허용 ({best_fresh['confirmed_count']}/6 ELITE)")
                                vwap       = calc_vwap(df)
                                last_c     = df.iloc[-1]
                                # VWAP: LONG은 VWAP 위로 너무 올라간 건 쫓지 않음
                                #        SHORT은 VWAP 아래로 너무 빠진 건 쫓지 않음
                                vwap_ok = (
                                    (direction == "LONG"  and current_price <= vwap * 1.005) or
                                    (direction == "SHORT" and current_price >= vwap * 0.995)
                                )
                                # 캔들 전환: 방향 일치 캔들이 마지막에 출현해야 진입
                                candle_ok = (
                                    (direction == "LONG"  and last_c["close"] > last_c["open"]) or
                                    (direction == "SHORT" and last_c["close"] < last_c["open"])
                                )
                                if vwap_ok and candle_ok:
                                    # 스캘핑도 진입 구간 체크 (1.5 ATR 이내)
                                    best_fresh_s = max(fresh, key=lambda x: x["confirmed_count"])
                                    ez_scalp = check_entry_zone(best_fresh_s, current_price, direction)
                                    if not ez_scalp["ok"]:
                                        print(f"  [{tf_label}] 스캘핑 기회 지남({ez_scalp['moved_atr']:.1f}ATR) — 스킵")
                                        _log_gate_block(symbol, tf_key, best_fresh_s, direction,
                                                        f"스캘핑 기회지남 {ez_scalp['moved_atr']:.1f}ATR")
                                    else:
                                        print(f"  [{tf_label}] VWAP ${vwap:,.2f} ✅  캔들전환 ✅  스캘핑 진입")
                                        _sc_boost = (MTF_POSITION_BOOST if mtf_info["strong"] else 1.0)
                                        _sc_boost *= (1.20 if _sc_ts == 2 else 1.0)  # 추세 이중일치 보너스
                                        if mtf_info["strong"]:
                                            print(f"  [MTF부스트] 전 TF 정렬 → {_sc_boost:.2f}x")
                                        _try_auto_trade(symbol, tf_key, fresh, current_price,
                                                        scalp=True, mtf_boost=_sc_boost)
                                else:
                                    reasons = []
                                    if not vwap_ok:
                                        reasons.append(f"VWAP ${vwap:,.2f} 기준 역방향")
                                    if not candle_ok:
                                        reasons.append("캔들 미전환")
                                    print(f"  [{tf_label}] 스캘핑 보류 — {', '.join(reasons)}")
                                    _log_gate_block(symbol, tf_key, best_fresh, direction,
                                                    "스캘핑 보류 — " + ", ".join(reasons))
                        else:
                            # ══════════════════════════════════════════════════
                            # 스윙 자동매매 — 퀀트 원칙: 4 HARD GATE + SOFT SCORE
                            #
                            # HARD GATE (진입 차단, 4개만):
                            #   1. 신선도 — 오래된 신호는 기회 지남 (타이밍)
                            #   2. 거래량  — 세력 미참여 = 노이즈  (참여도)
                            #   3. 모멘텀  — 반전 시작 확인         (방향 확인)
                            #   4. 진입구간 — 피봇에서 너무 멈       (타점)
                            #
                            # SOFT SCORE (포지션 크기 조정, 나머지):
                            #   MTF 정렬, 추세점수, CVD/OBV, 신선도 점수,
                            #   펀딩비 → 전부 베팅 크기 ±로 반영
                            # ══════════════════════════════════════════════════

                            # ── HARD GATE 1: 신선도 ────────────────────────
                            cfg_limit      = SWING_FRESHNESS.get(tf_key, 99)
                            adp_limit      = get_adaptive_swing_freshness(tf_key, cfg_limit)
                            max_bars_swing = min(cfg_limit, adp_limit)
                            fresh_swing    = [s for s in signals
                                              if s.get("bars_ago", 99) <= max_bars_swing]
                            if not fresh_swing:
                                oldest = min(s.get("bars_ago", 99) for s in signals)
                                print(f"  [{tf_label}] ❌GATE1 신호 {oldest}봉전 > 한도{max_bars_swing}봉 (스킵)")
                                _log_gate_block(symbol, tf_key, best, direction,
                                                f"GATE1 신선도 초과 {oldest}봉 > {max_bars_swing}봉")
                            else:
                                # ── HARD GATE 2: 거래량 ────────────────────
                                best_swing = max(fresh_swing, key=lambda x: x["confirmed_count"])
                                vol_r      = best_swing["vol"]["value"]
                                min_vol    = get_adaptive_min_vol()
                                if vol_r < min_vol:
                                    print(f"  [{tf_label}] ❌GATE2 볼륨 {vol_r:.1f}x < {min_vol}x (스킵)")
                                    _log_gate_block(symbol, tf_key, best_swing, direction,
                                                    f"GATE2 볼륨 {vol_r:.1f}x < {min_vol}x")
                                else:
                                    # ── HARD GATE 3: 캔들 모멘텀 ──────────
                                    momentum = check_candle_momentum(df, direction, bars=3, scalp=False)
                                    print(f"  [모멘텀] {momentum['note']}")
                                    if not momentum["ok"]:
                                        print(f"  [{tf_label}] ❌GATE3 {momentum['blocked_by']} (스킵)")
                                        _log_gate_block(symbol, tf_key, best_swing, direction,
                                                        f"GATE3 {momentum['blocked_by']}")
                                    else:
                                        # ── HARD GATE 4: 진입 구간 ─────────
                                        ez = check_entry_zone(best_swing, current_price, direction)
                                        print(f"  [진입구간] {ez['note']}")
                                        if not ez["ok"]:
                                            print(f"  [{tf_label}] ❌GATE4 기회지남 {ez['moved_atr']:.1f}ATR (스킵)")
                                            _log_gate_block(symbol, tf_key, best_swing, direction,
                                                            f"GATE4 기회지남 {ez['moved_atr']:.1f}ATR")
                                        else:
                                            # ════════════════════════════════
                                            # 4 GATE 통과 → SOFT SCORE 계산
                                            # 각 인자가 베팅 크기를 조절
                                            # ════════════════════════════════
                                            score_log = []
                                            boost     = 1.0

                                            # [+] MTF 전정렬: +30%
                                            if mtf_info["strong"]:
                                                boost *= MTF_POSITION_BOOST
                                                score_log.append(f"MTF✅+{int((MTF_POSITION_BOOST-1)*100)}%")
                                            # [경고] MTF 완전 역방향은 아직 차단
                                            # (전 TF가 반대 = 너무 위험)
                                            elif mtf_info["block"]:
                                                print(f"  [{tf_label}] ⛔ MTF 완전역방향 → 자동매매 차단")
                                                _log_gate_block(symbol, tf_key, best_swing, direction,
                                                                "MTF 완전역방향")
                                                continue

                                            # [+/-] 추세점수: +20% / -10%
                                            _sw_ts   = sum(
                                                1 for b in [macro, daily]
                                                if b["direction"] not in ("NEUTRAL",) and b["direction"] == direction
                                            )
                                            _sw_cont = best_swing["signal_type"] in ("hidden_bullish", "hidden_bearish")
                                            if _sw_ts == 2:
                                                boost *= 1.20
                                                score_log.append("추세2/2+20%")
                                            elif _sw_ts == 0 and _sw_cont:
                                                # 역추세 continuation만 차단 (반전은 허용)
                                                print(f"  [{tf_label}] 역추세 히든다이버전스 → 방향 모순 차단")
                                                _log_gate_block(symbol, tf_key, best_swing, direction,
                                                                "역추세 히든다이버전스 방향 모순")
                                                continue
                                            elif _sw_ts == 0:
                                                boost *= 0.80   # 역추세 반전 = 신중하게 80%
                                                score_log.append("역추세-20%")

                                            # [+] CVD + OBV 모두 확인: +15%
                                            # [+] 둘 중 하나: ±0% (중립)
                                            # [-] 모두 미확인: -15%
                                            _cvd_ok = best_swing.get("cvd", {}).get("ok", False)
                                            _obv_ok = best_swing.get("obv", {}).get("ok", False)
                                            if _cvd_ok and _obv_ok:
                                                boost *= 1.15
                                                score_log.append("CVD+OBV+15%")
                                            elif not _cvd_ok and not _obv_ok:
                                                boost *= 0.85
                                                score_log.append("CVD+OBV없음-15%")
                                            else:
                                                score_log.append("CVD/OBV중1개±0%")

                                            # [+/-] 신선도 점수: 0.5~1.0
                                            bars_ago_sw = best_swing.get("bars_ago", 0)
                                            fresh_score = get_freshness_score(bars_ago_sw, tf_key)
                                            boost      *= fresh_score
                                            if fresh_score < 1.0:
                                                score_log.append(f"신선도×{fresh_score:.1f}")

                                            # [+/-] 구조 레벨: 지지/저항 레벨에서 다이버전스 = +20%
                                            # 레벨 밖(>2ATR) = -20% (레벨없는 다이버전스 = 노이즈)
                                            _level = best_swing.get("at_key_level", {})
                                            print(f"  [레벨] {_level.get('note', '레벨정보없음')}")
                                            if _level.get("ok", False):
                                                boost *= 1.20
                                                score_log.append("구조레벨+20%")
                                            elif _level.get("nearest_atr", 99) > 2.0:
                                                boost *= 0.80
                                                score_log.append(f"레벨외{_level.get('nearest_atr',99):.1f}ATR-20%")

                                            # [-] 펀딩비 과열: -10% (차단 아님)
                                            lctx = get_market_context(symbol, direction)
                                            if not lctx["favorable"]:
                                                boost *= 0.90
                                                score_log.append(f"펀딩비{lctx['funding']:+.3f}%-10%")
                                                print(f"  [펀딩] {lctx['reason']} → 포지션 -10%")

                                            score_str = " | ".join(score_log) if score_log else "기본"
                                            print(f"  [스코어] {score_str} → 부스트 {boost:.2f}x")

                                            _try_auto_trade(symbol, tf_key, fresh_swing,
                                                            current_price, mtf_boost=boost)

                    time.sleep(1)
            else:
                print(f"  [{tf_label}] 다이버전스 없음  (${current_price:,.2f})")

            # ── 추가 전략 스캔 (RSI반전 / EMA눌림목) ─────────────────────────
            # 다이버전스 여부와 무관하게 항상 실행
            add_sigs = scan_additional(df, tf_key)
            for asig in add_sigs:
                asig["current_price"] = current_price
                strategy_tag = asig.get("strategy", "추가전략")
                adirection   = "LONG" if asig["signal_type"].endswith("long") else "SHORT"

                # ── 추세 게이트 (주봉+일봉 통합 — 추가전략) ─────────────────
                _ad_ts = sum(
                    1 for b in [macro, daily]
                    if b["direction"] not in ("NEUTRAL",) and b["direction"] == adirection
                )
                _ad_min = _trend_min_confirm(tf_key, _ad_ts, is_continuation=False)
                if _ad_min is None or asig["confirmed_count"] < _ad_min:
                    _ts_tag = ["❌역추세", "⭐단일일치", "⭐⭐이중일치"][_ad_ts]
                    print(f"  [{tf_label}] {strategy_tag} {_ts_tag} — "
                          f"{asig['confirmed_count']}/{_ad_min or 6} 미달 스킵")
                    _log_gate_block(symbol, tf_key, asig, adirection,
                                    f"{strategy_tag} 추세 기준 미달 {asig['confirmed_count']}/{_ad_min or 6}",
                                    strategy=strategy_tag)
                    continue

                print(f"  [{tf_label}] ⚡ {strategy_tag} {adirection}  "
                      f"RSI:{asig['rsi']['value']}  VOL:{asig['vol']['value']}x  "
                      f"({asig['confirmed_count']}/6)")
                total_signals += 1

                if not DRY_RUN and AUTO_TRADE:
                    # 서킷브레이커 / 학습 필터
                    ok_trade, why = is_tradeable(symbol, tf_key)
                    if not ok_trade:
                        print(f"  [학습] {why} → 스킵")
                        _log_gate_block(symbol, tf_key, asig, adirection, why,
                                        strategy=strategy_tag)
                        continue

                    # 이미 오픈 포지션 있으면 스킵 (중복 방지)
                    from trader import has_open_position
                    if has_open_position(symbol):
                        print(f"  [{strategy_tag}] {symbol} 포지션 이미 있음 → 스킵")
                        _log_gate_block(symbol, tf_key, asig, adirection,
                                        "이미 오픈 포지션 있음", strategy=strategy_tag)
                        continue

                    # MTF 체크 (차단이면 스킵, 정렬이면 부스트)
                    mtf_a = check_mtf(symbol, tf_key, adirection)
                    if mtf_a["block"]:
                        print(f"  [MTF] 역방향 → {strategy_tag} 스킵")
                        _log_gate_block(symbol, tf_key, asig, adirection,
                                        "MTF 역방향", strategy=strategy_tag)
                        continue

                    # 펀딩비 게이트
                    lctx_a = get_market_context(symbol, adirection)
                    if not lctx_a["favorable"]:
                        print(f"  [선행] 펀딩비 비우호 → {strategy_tag} 스킵")
                        _log_gate_block(symbol, tf_key, asig, adirection,
                                        f"펀딩비 비우호: {lctx_a['reason']}",
                                        strategy=strategy_tag)
                        continue

                    boost_a = MTF_POSITION_BOOST if mtf_a["strong"] else 1.0
                    boost_a *= (1.20 if _ad_ts == 2 else 1.0)  # 이중 추세 보너스
                    scalp_a = tf_key in SCALP_TF
                    print(f"  [{strategy_tag}] 자동매매 진입  레버:{_get_leverage(asig['strength'], tf_key)}x  부스트:{boost_a:.2f}x")
                    _try_auto_trade(symbol, tf_key, [asig], current_price,
                                    scalp=scalp_a, mtf_boost=boost_a)

            # ══════════════════════════════════════════════════════════════
            # 돌파 추세 매매 (Breakout) + 불타기 (Pyramid)
            # 다이버전스/추가전략과 무관하게 항상 체크
            # ══════════════════════════════════════════════════════════════
            if AUTO_TRADE and not DRY_RUN:
                from trader import (has_open_position, can_pyramid,
                                    get_open_positions_detail)

                # ── 1. 돌파 신호 체크 ────────────────────────────────────
                # 1h 이상 TF에서만 돌파 매매 (노이즈 방지)
                if tf_key not in SCALP_TF and not has_open_position(symbol):
                    bsig = detect_breakout(df)
                    if bsig:
                        bsig["current_price"] = current_price
                        bdir = bsig["direction"]
                        # 돌파 방향이 주봉/일봉 추세와 일치해야 진입
                        _bt_ts = sum(
                            1 for b in [macro, daily]
                            if b["direction"] not in ("NEUTRAL",) and b["direction"] == bdir
                        )
                        expand_tag = "⚡ATR확장" if bsig["atr_expand"] else ""
                        print(f"\n  [{tf_label}] 🚀 돌파 감지! {bdir}  레벨:{bsig['breakout_level']:,.2f}"
                              f"  VOL:{bsig['vol']['value']:.1f}x  {expand_tag}"
                              f"  추세:{_bt_ts}/2")
                        if _bt_ts >= 1:   # 주봉 또는 일봉 중 최소 1개 추세 일치
                            _try_breakout_trade(symbol, tf_key, bsig, current_price)
                        else:
                            print(f"  [돌파] 추세 미일치(0/2) → 역돌파 스킵")
                            _log_gate_block(symbol, tf_key, bsig, bdir,
                                            "돌파 추세 미일치 0/2", strategy="돌파")
                    # else: 돌파 없음 — 출력 생략

                # ── 2. 불타기 체크 ───────────────────────────────────────
                # 이미 오픈 포지션 있는 경우: 수익 중이면 추가진입
                ok_pyr, pyr_msg = can_pyramid(symbol, tf_key)
                if ok_pyr:
                    open_pos = get_open_positions_detail()
                    for pos in open_pos:
                        if pos["symbol"] != symbol or pos["tf"] != tf_key:
                            continue
                        pos_dir      = pos["direction"]
                        entry_p      = pos.get("entry_price", current_price)
                        pos_atr      = float(df.iloc[-1]["close"] - df.iloc[-1]["open"])  # 현재봉 몸통
                        atr_now      = float(df["high"].iloc[-1] - df["low"].iloc[-1])
                        if atr_now <= 0:
                            continue

                        profit_atr = (
                            (current_price - entry_p) / atr_now if pos_dir == "LONG"
                            else (entry_p - current_price) / atr_now
                        )
                        pyr_count = pos.get("pyramid_count", 0)

                        # 불타기 임계값: 1회=+1.5ATR, 2회=+3.0ATR
                        thresholds = [1.5, 3.0]
                        target_atr = thresholds[pyr_count] if pyr_count < 2 else 999

                        # EMA 방향 여전히 일치하는지 확인
                        from divergence import _ema_trend, calc_atr
                        ema_now = _ema_trend(df["close"])
                        ema_ok  = (pos_dir == "LONG" and ema_now >= 0) or \
                                  (pos_dir == "SHORT" and ema_now <= 0)

                        if profit_atr >= target_atr and ema_ok:
                            print(f"\n  [{tf_label}] 🔥 불타기 {pyr_count+1}회 조건 충족!"
                                  f"  수익:{profit_atr:+.1f}ATR  EMA{'✅' if ema_ok else '❌'}")
                            _do_pyramid(symbol, tf_key, pos_dir,
                                        entry_p, current_price,
                                        atr_now, pyr_count + 1)
                        elif profit_atr > 0:
                            print(f"  [불타기] {pos_dir}  수익:{profit_atr:+.1f}ATR"
                                  f"  (목표:{target_atr:.1f}ATR  EMA:{'✅' if ema_ok else '❌'})")

            time.sleep(0.3)

    if total_signals == 0 and not DRY_RUN:
        send(build_summary(total_scanned))

    print(f"\n{'='*55}")
    print(f"  ✅ 완료 — {total_scanned}회 검사  |  신호 {total_signals}개")
    if AUTO_TRADE:
        print(f"  🤖 자동매매 모드 활성")
    print(f"{'='*55}")


if __name__ == "__main__":
    if not wait_for_network():
        print("네트워크 연결 실패 — 종료")
        sys.exit(1)
    scan()
