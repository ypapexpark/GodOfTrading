#!/usr/bin/env python3
from __future__ import annotations
"""
CryptoSignal — BTC/ETH 선물 다이버전스 스캐너 (RSI + MACD + OBV + StochRSI + Volume 5중 확인)
실행:
  python3 main.py              # 스캔 + 텔레그램 알림만
  python3 main.py --auto-trade # 스캔 + 텔레그램 + 자동매매
  python3 main.py --dry-run    # 출력만 (텔레그램/거래 없음)
"""
import math
import sys
import socket
import time
import warnings
from html import escape
from pathlib import Path
from dotenv import load_dotenv

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

from config import (SYMBOLS, TIMEFRAMES, STRICT_TF, SCALP_FRESHNESS, SWING_FRESHNESS,
                    BTC_MACRO_SHORT_BLOCK_LONG, BTC_MACRO_SHORT_CACHE_TTL,
                    BTC_MACRO_SHORT_LEVERAGE_MULT, BTC_MACRO_SHORT_MARGIN_PCT,
                    BTC_MACRO_SHORT_MARGIN_USD, BTC_MACRO_SHORT_MAX_ACCOUNT_RISK_PCT,
                    BTC_MACRO_SHORT_MAX_LEVERAGE, BTC_MACRO_SHORT_MIN_SCORE,
                    BTC_MACRO_SHORT_ONLY_ENABLED, BTC_MACRO_SHORT_POSITION_CAP,
                    BTC_MACRO_SHORT_RISK_MULT, BTC_MACRO_SHORT_SWING_MIN_VOL,
                    BTC_MACRO_SHORT_SYMBOL, BTC_MACRO_TREND_REFERENCE_ONLY,
                    BTC_SYNC_DIRECT_BASE_LEVERAGE, BTC_SYNC_DIRECT_COOLDOWN_MIN,
                    BTC_SYNC_DIRECT_MARGIN_USD, BTC_SYNC_DIRECT_MAX_LEVERAGE,
                    BTC_SYNC_DIRECT_MAX_SPREAD_PCT, BTC_SYNC_DIRECT_MIN_ABS_GAP_PCT,
                    BTC_SYNC_DIRECT_MIN_BEST_RR, BTC_SYNC_DIRECT_MIN_CORRELATION,
                    BTC_SYNC_DIRECT_MIN_TP1_RR, BTC_SYNC_DIRECT_MIN_VOL_RATIO,
                    BTC_SYNC_DIRECT_DAILY_SYMBOL_LOSS_LIMIT,
                    BTC_SYNC_DIRECT_MOMENTUM_MIN_ZSCORE,
                    BTC_SYNC_DIRECT_POSITION_CAP, BTC_SYNC_DIRECT_RISK_PCT,
                    BTC_SYNC_DIRECT_REVERSION_MIN_ZSCORE,
                    BTC_SYNC_DIRECT_STOP_ATR_MULT, BTC_SYNC_DIRECT_STOP_MIN_PCT,
                    BTC_SYNC_DIRECT_TIMEFRAME, BTC_SYNC_DIRECT_TOP_N,
                    BTC_SYNC_DIRECT_TP_PCT, BTC_SYNC_DIRECT_TP_RR,
                    BTC_SYNC_DIRECT_TRADE_ENABLED,
                    INITIAL_SL_MARGIN_ROI_CAP_PCT,
                    BTC_SYNC_RADAR_ENABLED, BTC_SYNC_SCAN_TOP_N,
                    HYPERLIQUID_LEAD_RISK_MULT, HYPERLIQUID_RADAR_ENABLED,
                    HYPERLIQUID_SCAN_TOP_N,
                    FAST_RADAR_ENABLED, FAST_RADAR_MAX_SYMBOLS,
                    FAST_RADAR_SURGE_TOP_N, FAST_RADAR_TIMEFRAMES,
                    RADAR_TOP_N, VOLUME_SURGE_TOP_N,
                    ASYMMETRIC_FUNDING_OVERRIDE_MULT, ASYMMETRIC_TF,
                    ASYMMETRIC_TIMING_OVERRIDE_MULT, ASYMMETRIC_TP_BY_STRENGTH,
                    ACTIVE_FAST_MIN_RR, ACTIVE_HIGH_VOL, ACTIVE_MAX_MIN_RR,
                    ACTIVE_MTF_REVERSAL_RISK_MULT, ACTIVE_ULTRA_VOL,
                    ELITE_MTF_HIDDEN_RISK_MULT, ELITE_MTF_REVERSAL_RISK_MULT,
                    EMA_NEUTRAL_MTF_RISK_MULT,
                    MTF_SOFT_HIDDEN_RISK_MULT, MTF_SOFT_MIN_CONFIRMED,
                    MTF_SOFT_MIN_DIVERGENCE, MTF_SOFT_MIN_VOL,
                    MTF_SOFT_REVERSAL_RISK_MULT, MTF_SOFT_OVERRIDE_LONG_ONLY,
                    CONVICTION_MARGIN_PCT_BY_TIER, CONVICTION_MARGIN_USD_BY_TIER,
                    CONVICTION_SIZING_ENABLED,
                    FAST_TP1_MIN_RR,
                    HIGH_OPPORTUNITY_DD_DISABLE_PCT,
                    HIGH_OPPORTUNITY_MAX_ACCOUNT_RISK_PCT,
                    HIGH_OPPORTUNITY_MIN_MARGIN_ROI_PCT,
                    HIGH_OPPORTUNITY_MIN_TP1_MARGIN_ROI_PCT,
                    HIGH_OPPORTUNITY_MIN_TP1_RR,
                    MIN_FALLBACK_TRADE_MARGIN_USD,
                    MIN_EXPECTED_MARGIN_ROI_PCT, MIN_TP1_MARGIN_ROI_PCT,
                    MIN_TRADE_MARGIN_MAX_BALANCE_PCT, MIN_TRADE_MARGIN_USD,
                    PORTFOLIO_DIRECTIONAL_HIGH_OPPORTUNITY_CAP,
                    PORTFOLIO_DIRECTIONAL_MARGIN_CAP,
                    PORTFOLIO_MARGIN_USAGE_CAP,
                    PORTFOLIO_MARGIN_USAGE_HIGH_OPPORTUNITY_CAP,
                    PORTFOLIO_MAX_OPEN_POSITIONS,
                    PORTFOLIO_MAX_OPEN_POSITIONS_HIGH_OPPORTUNITY,
                    PORTFOLIO_POSITION_QUERY_RETRIES,
                    PORTFOLIO_TOTAL_SL_RISK_CAP_PCT,
                    PORTFOLIO_TOTAL_SL_RISK_HIGH_OPPORTUNITY_CAP_PCT,
                    ROI_LEVERAGE_RESCUE_ENABLED, ROI_LEVERAGE_RESCUE_MAX,
                    ROI_RESCUE_MIN_BEST_RR, ROI_RESCUE_MIN_TP1_RR,
                    SIZING_MIN_BEST_RR_BY_TIER, SIZING_MIN_TP1_RR_BY_TIER,
                    PROFIT_SURGE_MAX_ACCOUNT_RISK_PCT,
                    PROFIT_SURGE_MIN_BEST_RR, PROFIT_SURGE_MIN_CONFIRMED,
                    PROFIT_SURGE_MIN_LEVERAGE, PROFIT_SURGE_MIN_MARGIN_ROI_PCT,
                    PROFIT_SURGE_MIN_TP1_MARGIN_ROI_PCT, PROFIT_SURGE_MIN_TP1_RR,
                    PROFIT_SURGE_MIN_VOL, PROFIT_SURGE_SIZING_ENABLED,
                    PROFIT_SURGE_STOP_ATR_MULT, PROFIT_SURGE_STOP_MAX_PCT_BY_TF,
                    PROFIT_SURGE_STOP_MIN_PCT, PROFIT_SURGE_TARGET_MARGIN_PCT,
                    PROFIT_SURGE_TIGHT_STOP_ENABLED,
                    MARGIN_BY_STRENGTH, MTF_POSITION_BOOST, MTF_POSITION_CAP, MODERATE_AUTO_TRADE,
                    GOLDEN_ENTRY_POSITION_PCT, GOLDEN_LEVERAGE_BOOST, GOLDEN_MAX_LEVERAGE,
                    PREMIUM_MTF_AUTO_STRENGTHS,
                    PAPER_ONLY_STRENGTHS, RISK_PCT_BY_STRENGTH, SCALP_RISK_MULT,
                    GOLDEN_ENTRY_RISK_PCT, MAX_ACCOUNT_RISK_PCT, AUTO_TRADE_DIAGNOSTICS,
                    ACTIVE_STRONG_STRATEGIES, STRONG_LIVE_MAX_BARS_AGO, STRONG_LIVE_MIN_VOL,
                    ASYMMETRIC_SYMBOL_DAILY_LOSS_LIMIT, SYMBOL_STRATEGY_DAILY_LOSS_LIMIT,
                    SYMBOL_DAILY_TOTAL_LOSS_LIMIT,
                    AUTO_TRADE_STRATEGY_WHITELIST, BLOCK_SHORT_AUTO_TRADE,
                    ROUND_TRIP_FEE)
from leading import get_market_context
from mtf import check_mtf, mtf_summary, get_macro_bias, get_daily_bias
from fetcher import (fetch_btc_sync_dislocations, fetch_ohlcv,
                     fetch_hyperliquid_lead_radar,
                     fetch_market_radar, fetch_volume_surge_radar,
                     CORE_SYMBOLS, STOCK_SYMBOLS)
from divergence import (detect, calc_vwap, detect_breakout,
                        get_freshness_score, check_candle_momentum, check_entry_zone)
from strategies import get_bb_midline_long_bias, scan_additional
from bithumb_screener import maybe_send_bithumb_ma200_alert
from krx_screener import maybe_send_krx_ma200_alert
from project_reminders import maybe_send_kis_api_review_reminder
from formatter import build_alert, build_summary, calc_targets, _get_leverage, _raw_strength, _round_price, SIGNAL_META
from publisher import send, send_market_screening, send_position_analysis, send_review, send_signal
from analyzer import (is_tradeable, get_adaptive_min_rr, get_adaptive_min_vol,
                      get_adaptive_min_confirmed, get_adaptive_swing_freshness,
                      get_adaptive_filters, analyze_and_adjust,
                      build_learning_report, build_loss_pattern_summary, build_next_strategy,
                      build_signal_quality_report, get_asymmetric_profile, get_risk_multiplier,
                      get_realized_trade_adjustment,
                      get_quality_leverage_adjustment, get_signal_quality_adjustment,
                      is_tradeable_with_strategy, get_cooldown_symbols)
from strategy_catalog import classify_strategy, format_profile
from venue_runtime import runtime_context, venue_label

load_dotenv(Path(__file__).parent / ".env")

DRY_RUN    = "--dry-run"    in sys.argv
AUTO_TRADE = "--auto-trade" in sys.argv
FAST_RADAR = "--fast-radar" in sys.argv
BITHUMB_ONLY = "--bithumb-ma200" in sys.argv
KRX_ONLY = "--krx-ma200" in sys.argv


def _read_int_arg(flag: str) -> int | None:
    """간단한 CLI 정수 옵션 파서.

    main.py는 전통적으로 argparse 없이 플래그 존재 여부만 읽고 있어서,
    스크리너 수동 테스트용 `--limit 10`만 가볍게 지원한다.
    """
    if flag not in sys.argv:
        return None
    idx = sys.argv.index(flag)
    try:
        return int(sys.argv[idx + 1])
    except Exception:
        print(f"[CLI] {flag} 뒤에는 정수가 필요합니다. 예: {flag} 10")
        return None


SCREEN_LIMIT = _read_int_arg("--limit")


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
SCALP_TF  = {"15m"}  # 15m = 최소 자동매매 판단봉
TIMING_ONLY_TF = {"5m"}  # 5m는 초단타 보조 참고용 — 단독 자동매매 금지
LOW_NOISE_TF   = {"5m", "15m"}  # 돌파/스윙 로직 제외용 하위 노이즈 TF
LOWER_TIMING_TF = {
    "1h":  "15m",
    "4h":  "1h",
    "1d":  "4h",
}


def _check_lower_tf_timing(symbol: str, decision_tf: str,
                           direction: str) -> dict:
    """
    판단봉의 방향은 유지하되, 필요할 때만 한 단계 아래의 안정적인 봉으로 확인한다.
    5m는 완전 초단타 보조 참고용이며 15m 자동매매의 필수 타점 필터로 쓰지 않는다.
    """
    lower_tf = LOWER_TIMING_TF.get(decision_tf)
    if not lower_tf:
        return {"ok": True, "tf": "", "note": "동일 판단봉 자체 확인"}

    try:
        lower_df = fetch_ohlcv(symbol, lower_tf, 120)
        if lower_df is None or len(lower_df) < 60:
            return {"ok": False, "tf": lower_tf, "note": "데이터 부족"}
    except Exception as e:
        return {"ok": False, "tf": lower_tf, "note": f"데이터 오류: {e}"}

    lower_price = float(lower_df["close"].iloc[-1])
    vwap = calc_vwap(lower_df)
    vwap_ok = (
        (direction == "LONG"  and lower_price <= vwap * 1.006) or
        (direction == "SHORT" and lower_price >= vwap * 0.994)
    )

    momentum = check_candle_momentum(lower_df, direction, bars=3, scalp=True)

    close = lower_df["close"]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    # 보조봉이 강하게 반대로 기울 때만 차단한다. 방향 판단은 판단봉/상위봉이 담당한다.
    ema_not_against = (
        (direction == "LONG"  and ema20 >= ema50 * 0.997) or
        (direction == "SHORT" and ema20 <= ema50 * 1.003)
    )

    ok = vwap_ok and momentum["ok"] and ema_not_against
    failed = []
    if not vwap_ok:
        failed.append(f"VWAP추격(price {lower_price:,.4f} / vwap {vwap:,.4f})")
    if not momentum["ok"]:
        failed.append(momentum["blocked_by"])
    if not ema_not_against:
        failed.append("하위봉 EMA 역방향")

    note = (
        f"{lower_tf} 보조확인 {'OK' if ok else '보류'}"
        f" | {momentum['note']}"
        f" | VWAP {'OK' if vwap_ok else 'NO'}"
        f" | EMA {'OK' if ema_not_against else 'NO'}"
    )
    if failed:
        note += " — " + ", ".join(failed)
    return {"ok": ok, "tf": lower_tf, "note": note}


def _indicator_snapshot(signal: dict) -> dict:
    """체결 당시 지표 상태를 JSON 저장 가능한 형태로 축약한다."""
    snapshot = {}
    for key in ("rsi", "cci", "macd", "obv", "srsi", "vol", "cvd"):
        data = signal.get(key, {}) or {}
        item = {"ok": bool(data.get("ok", False))}
        if "value" in data:
            value = data.get("value")
            try:
                value = round(float(value), 4)
            except Exception:
                value = str(value)
            item["value"] = value
        snapshot[key] = item
    return snapshot


_btc_macro_short_cache: dict = {"ts": 0.0, "data": {}}


def _sma_value(df, period: int) -> float:
    if df is None or len(df) < period:
        return 0.0
    try:
        return float(df["close"].rolling(period).mean().iloc[-1])
    except Exception:
        return 0.0


def _btc_macro_short_bias(force_refresh: bool = False) -> dict:
    """
    BTC 장기봉 참고 바이어스.

    월봉·주봉·일봉이 하락 우위인지 점수화해서 근거 메모로 남긴다.
    단타/스캘핑 자동매매에서는 이 값만으로 롱을 차단하거나 숏 시드/레버리지를
    키우지 않는다. 실제 진입은 15m·1h·4h 타점, R:R, ROI, SL위험이 결정한다.
    """
    if not BTC_MACRO_SHORT_ONLY_ENABLED:
        return {"active": False, "score": 0, "note": "BTC 장기봉 참고 모드 꺼짐"}

    now = time.time()
    if (
        not force_refresh
        and _btc_macro_short_cache["data"]
        and now - float(_btc_macro_short_cache["ts"] or 0) < BTC_MACRO_SHORT_CACHE_TTL
    ):
        return _btc_macro_short_cache["data"]

    result = {"active": False, "score": 0, "note": "BTC 매크로 데이터 부족"}
    try:
        monthly = fetch_ohlcv(BTC_MACRO_SHORT_SYMBOL, "1M", 18)
        weekly = fetch_ohlcv(BTC_MACRO_SHORT_SYMBOL, "1w", 80)
        daily = fetch_ohlcv(BTC_MACRO_SHORT_SYMBOL, "1d", 260)

        m = monthly.iloc[-1]
        w = weekly.iloc[-1]
        d = daily.iloc[-1]
        m_close = float(m["close"])
        m_open = float(m["open"])
        m_sma6 = _sma_value(monthly, 6)
        m_sma12 = _sma_value(monthly, 12)
        w_sma20 = _sma_value(weekly, 20)
        w_sma50 = _sma_value(weekly, 50)
        d_sma50 = _sma_value(daily, 50)
        d_sma200 = _sma_value(daily, 200)

        checks = []
        if m_close < m_open:
            checks.append("월봉 음봉")
        if m_sma6 and m_close < m_sma6:
            checks.append("월봉 6개월선 하회")
        if m_sma12 and m_close < m_sma12:
            checks.append("월봉 12개월선 하회")
        if w_sma20 and float(w["close"]) < w_sma20:
            checks.append("주봉 20선 하회")
        if w_sma50 and float(w["close"]) < w_sma50:
            checks.append("주봉 50선 하회")
        if d_sma50 and float(d["close"]) < d_sma50:
            checks.append("일봉 50선 하회")
        if d_sma200 and float(d["close"]) < d_sma200:
            checks.append("일봉 200선 하회")

        score = len(checks)
        active = score >= int(BTC_MACRO_SHORT_MIN_SCORE)
        note = (
            f"BTC 매크로 숏점수 {score}/7 — "
            f"월봉 종가 {m_close:,.0f} / 시가 {m_open:,.0f}, "
            f"M6 {m_sma6:,.0f}, M12 {m_sma12:,.0f}, "
            f"D200 {d_sma200:,.0f}; "
            + (", ".join(checks) if checks else "하락조건 부족")
        )
        result = {
            "active": active,
            "score": score,
            "checks": checks,
            "note": note,
            "monthly_close": round(m_close, 2),
            "monthly_open": round(m_open, 2),
            "monthly_sma6": round(m_sma6, 2),
            "monthly_sma12": round(m_sma12, 2),
            "daily_sma200": round(d_sma200, 2),
        }
    except Exception as e:
        result = {"active": False, "score": 0, "note": f"BTC 매크로 숏 데이터 오류: {e}"}

    _btc_macro_short_cache["data"] = result
    _btc_macro_short_cache["ts"] = now
    return result


def _price_text(price: float) -> str:
    p = abs(float(price))
    if p >= 1000:
        return f"${float(price):,.2f}"
    if p >= 1:
        return f"${float(price):,.4f}"
    return f"${float(price):,.6f}"


def _directional_move_pct(entry_price: float, target_price: float, direction: str) -> float:
    if entry_price <= 0 or target_price <= 0:
        return 0.0
    if direction == "LONG":
        return (target_price - entry_price) / entry_price * 100
    return (entry_price - target_price) / entry_price * 100


def _planned_roi_metrics(entry_price: float, direction: str,
                         tps: list[dict], leverage: int) -> dict:
    """TP 계획의 가격수익률과 레버리지 포함 증거금 ROI를 계산한다."""
    fee_pct = ROUND_TRIP_FEE * 100
    weighted_net_pct = 0.0
    max_margin_roi = 0.0
    tp1_margin_roi = 0.0
    for idx, tp in enumerate(tps or []):
        weight = float(tp.get("pct", 0) or 0) / 100
        price = float(tp.get("price", 0) or 0)
        gross_pct = _directional_move_pct(entry_price, price, direction)
        net_pct = gross_pct - fee_pct
        margin_roi = net_pct * leverage
        weighted_net_pct += net_pct * weight
        max_margin_roi = max(max_margin_roi, margin_roi)
        if idx == 0:
            tp1_margin_roi = margin_roi
    return {
        "weighted_net_pct": round(weighted_net_pct, 4),
        "weighted_margin_roi_pct": round(weighted_net_pct * leverage, 2),
        "tp1_margin_roi_pct": round(tp1_margin_roi, 2),
        "max_margin_roi_pct": round(max_margin_roi, 2),
    }


def _cap_leverage_for_initial_sl(leverage: int, sl_pct: float) -> tuple[int, list[str]]:
    """Compress leverage when the initial SL would consume too much margin."""
    lev = max(1, int(leverage or 1))
    sl_pct_f = float(sl_pct or 0.0)
    if sl_pct_f <= 0:
        return lev, []
    margin_loss_pct = sl_pct_f * lev
    cap = float(INITIAL_SL_MARGIN_ROI_CAP_PCT)
    if margin_loss_pct <= cap:
        return lev, []
    capped = max(1, int(math.floor(cap / sl_pct_f)))
    capped = min(capped, lev)
    if capped >= lev:
        return lev, []
    return capped, [
        (
            f"초기SL 증거금손실 상한: SL {sl_pct_f:.2f}% × {lev}x = "
            f"{margin_loss_pct:.1f}% > {cap:.0f}% → 레버리지 {lev}x→{capped}x"
        )
    ]


def _roi_gate_reason(entry_price: float, direction: str,
                     tps: list[dict], leverage: int) -> tuple[str, dict]:
    """레버리지 포함 기대 ROI가 너무 작은 매매를 차단한다."""
    metrics = _planned_roi_metrics(entry_price, direction, tps, leverage)
    weighted_roi = metrics["weighted_margin_roi_pct"]
    tp1_roi = metrics["tp1_margin_roi_pct"]
    if weighted_roi < MIN_EXPECTED_MARGIN_ROI_PCT:
        return (
            f"기대 증거금ROI {weighted_roi:.1f}% < 최소 {MIN_EXPECTED_MARGIN_ROI_PCT:.1f}% "
            f"(수수료 차감, 레버리지 포함)",
            metrics,
        )
    if tp1_roi < MIN_TP1_MARGIN_ROI_PCT:
        return (
            f"TP1 증거금ROI {tp1_roi:.1f}% < 최소 {MIN_TP1_MARGIN_ROI_PCT:.1f}% "
            f"(첫 익절이 너무 작음)",
            metrics,
        )
    return "", metrics


def _raise_leverage_for_roi(entry_price: float, direction: str,
                            tps: list[dict], leverage: int) -> tuple[int, dict, list[str]]:
    """ROI 부족이 유일한 문제인 좋은 신호는 필요한 최소 레버리지로 보정한다."""
    base_leverage = max(1, int(leverage or 1))
    metrics = _planned_roi_metrics(entry_price, direction, tps, base_leverage)
    gate_reason, _ = _roi_gate_reason(entry_price, direction, tps, base_leverage)
    if not ROI_LEVERAGE_RESCUE_ENABLED or not gate_reason:
        return base_leverage, metrics, []

    # 레버리지만 올려도 순가격수익률 자체가 음수면 수수료/노이즈 구간이므로 구조적으로 제외한다.
    weighted_net_pct = float(metrics.get("weighted_net_pct", 0) or 0)
    tp1_net_pct = 0.0
    if tps:
        tp1_price = float(tps[0].get("price", 0) or 0)
        tp1_net_pct = (
            _directional_move_pct(entry_price, tp1_price, direction)
            - ROUND_TRIP_FEE * 100
        )
    if weighted_net_pct <= 0 or tp1_net_pct <= 0:
        return base_leverage, metrics, []

    required_for_expected = math.ceil(MIN_EXPECTED_MARGIN_ROI_PCT / weighted_net_pct)
    required_for_tp1 = math.ceil(MIN_TP1_MARGIN_ROI_PCT / tp1_net_pct)
    max_leverage = max(base_leverage, int(ROI_LEVERAGE_RESCUE_MAX or base_leverage))
    target_leverage = min(
        max_leverage,
        max(base_leverage, required_for_expected, required_for_tp1),
    )
    if target_leverage <= base_leverage:
        return base_leverage, metrics, []

    boosted_metrics = _planned_roi_metrics(entry_price, direction, tps, target_leverage)
    boosted_reason, _ = _roi_gate_reason(entry_price, direction, tps, target_leverage)
    if boosted_reason:
        return base_leverage, metrics, []

    note = (
        f"ROI 보정 레버리지 {base_leverage}x→{target_leverage}x "
        f"(기대ROI {metrics['weighted_margin_roi_pct']:+.1f}%"
        f"→{boosted_metrics['weighted_margin_roi_pct']:+.1f}%, "
        f"TP1 {metrics['tp1_margin_roi_pct']:+.1f}%"
        f"→{boosted_metrics['tp1_margin_roi_pct']:+.1f}%)"
    )
    return target_leverage, boosted_metrics, [note]


def _conviction_margin_target(balance: float, raw_strength: str,
                              is_golden: bool, mtf_boost: float,
                              ema_aligned: bool, leverage_notes: list[str],
                              roi_metrics: dict,
                              strategy: str = "") -> tuple[float, str, list[str]]:
    """신호 확신도에 따라 목표 증거금 하한을 계산한다."""
    if not CONVICTION_SIZING_ENABLED:
        return MIN_TRADE_MARGIN_USD, "BASE", []

    raw = _raw_strength(raw_strength)
    tier = "BASE"
    reasons = []
    expected_roi = float(roi_metrics.get("weighted_margin_roi_pct", 0) or 0)

    if is_golden:
        tier = "GOLDEN"
        reasons.append("황금진입: ELITE + MTF 전정렬 + EMA 정렬")
    elif raw == "ELITE":
        tier = "ELITE"
        reasons.append("ELITE 고확신 신호")
        if mtf_boost > 1.0:
            reasons.append("MTF 정렬")
        if ema_aligned:
            reasons.append("EMA 방향일치")
    elif raw == "VERY STRONG" and (mtf_boost > 1.0 or ema_aligned or leverage_notes):
        tier = "VERY STRONG"
        reasons.append("VERY STRONG + 방향/학습 우위")
    elif raw == "STRONG" and (leverage_notes or expected_roi >= MIN_EXPECTED_MARGIN_ROI_PCT * 1.5):
        tier = "STRONG"
        reasons.append("STRONG 중 기대 ROI/사후승률 우위")

    fixed_usd = float(CONVICTION_MARGIN_USD_BY_TIER.get(tier, MIN_TRADE_MARGIN_USD))
    pct_floor = float(CONVICTION_MARGIN_PCT_BY_TIER.get(tier, 0.0))
    target = max(MIN_TRADE_MARGIN_USD, fixed_usd, balance * pct_floor)
    hard_cap_usd = balance * MIN_TRADE_MARGIN_MAX_BALANCE_PCT
    if hard_cap_usd >= MIN_TRADE_MARGIN_USD:
        target = min(target, hard_cap_usd)
    else:
        target = MIN_TRADE_MARGIN_USD
    note = (
        f"확신도 시드 {tier}: 목표증거금 ${target:.2f}"
        + (f" — {', '.join(reasons)}" if reasons else "")
    )
    return round(target, 2), tier, [note]


def _sizing_boost_allowed(conviction_tier: str, best_rr: float,
                          tp1_rr: float) -> tuple[bool, str]:
    """$20 이상 증거금 확대가 손익비 기준을 만족하는지 확인한다."""
    tier = conviction_tier if conviction_tier in SIZING_MIN_TP1_RR_BY_TIER else "BASE"
    min_tp1 = float(SIZING_MIN_TP1_RR_BY_TIER.get(tier, 1.2))
    min_best = float(SIZING_MIN_BEST_RR_BY_TIER.get(tier, 2.0))
    if tp1_rr < min_tp1 or best_rr < min_best:
        return False, (
            f"시드상향 보류: {tier} 손익비 기준 미달 "
            f"(TP1 R:R 1:{tp1_rr:.1f}/{min_tp1:.1f}, "
            f"최대 R:R 1:{best_rr:.1f}/{min_best:.1f})"
        )
    return True, ""


def _is_high_profit_opportunity(raw_strength: str, conviction_tier: str,
                                roi_metrics: dict,
                                leverage_notes: list[str] | None = None,
                                tp1_rr: float = 0.0) -> bool:
    """손실 소프트캡보다 기회 포착을 우선할 고기대수익 자리인지 판단한다.

    레버리지 ROI%만으로는 SHIB1000처럼 TP1 R:R 1:1.0인 동전던지기 자리도
    "고기대수익"으로 잡힌다 (2026-07-03 SHIB1000 -$1.09 사례). ROI% 조건에
    더해 TP1 R:R 하한을 요구해 실제 손익비가 뒷받침되는 자리만 통과시킨다.
    """
    raw = _raw_strength(raw_strength)
    weighted_roi = float(roi_metrics.get("weighted_margin_roi_pct", 0) or 0)
    tp1_roi = float(roi_metrics.get("tp1_margin_roi_pct", 0) or 0)
    max_roi = float(roi_metrics.get("max_margin_roi_pct", 0) or 0)
    high_roi = (
        weighted_roi >= HIGH_OPPORTUNITY_MIN_MARGIN_ROI_PCT
        and tp1_roi >= HIGH_OPPORTUNITY_MIN_TP1_MARGIN_ROI_PCT
    )
    very_high_runner = max_roi >= HIGH_OPPORTUNITY_MIN_MARGIN_ROI_PCT * 1.8
    high_quality = (
        conviction_tier in {"VERY STRONG", "ELITE", "GOLDEN"}
        or raw in {"VERY STRONG", "ELITE"}
        or bool(leverage_notes)
    )
    good_rr = float(tp1_rr or 0) >= HIGH_OPPORTUNITY_MIN_TP1_RR
    return good_rr and high_quality and (high_roi or very_high_runner)


def _opportunity_risk_cap(balance: float) -> float:
    return max(0.0, balance * HIGH_OPPORTUNITY_MAX_ACCOUNT_RISK_PCT)


def _risk_off_high_opportunity_reason(state: dict) -> str:
    """계좌 전체가 훼손된 상태(DD/하드스톱)에서만 고기대수익 예외를 막는다.

    2026-07-03 재검토: 당일 손실액 자체로는 더 이상 예외를 막지 않는다.
    손실은 손실이고, TP1 R:R 하한(HIGH_OPPORTUNITY_MIN_TP1_RR)을 통과한
    진짜 좋은 자리는 그날 손실과 무관하게 시도해서 멘징/수익전환 기회를
    준다 — 단건 리스크는 opportunity_risk_cap으로 계속 캡핑된다.
    DD%나 하드스톱처럼 계좌 자체가 위험한 상태만 전면 차단한다.
    """
    dd_pct = float(state.get("drawdown_pct", 0) or 0)
    dd_status = str(state.get("drawdown_status", "") or "")
    if dd_pct >= HIGH_OPPORTUNITY_DD_DISABLE_PCT * 100:
        return (
            f"고기대수익 예외 해제: 계좌 DD {dd_pct:.1f}% "
            f">= {HIGH_OPPORTUNITY_DD_DISABLE_PCT*100:.0f}%"
        )
    if dd_status == "hard_stop":
        return f"고기대수익 예외 해제: 계좌 상태 {dd_status}"
    return ""


def _today_mmdd_kst() -> str:
    return time.strftime("%m/%d", time.gmtime(time.time() + 9 * 3600))


def _today_loss_count(state: dict, symbol: str | None = None,
                      strategy_mode: str | None = None) -> int:
    """오늘 이미 손실을 낸 종목/전략 조합의 반복 진입 횟수를 센다."""
    today = _today_mmdd_kst()
    count = 0
    for trade in state.get("trade_history", []) or []:
        if trade.get("status") != "loss":
            continue
        closed_at = str(trade.get("closed_at") or trade.get("time") or "")
        if not closed_at.startswith(today):
            continue
        if symbol and trade.get("symbol") != symbol:
            continue
        mode = trade.get("strategy_mode") or (trade.get("entry_context") or {}).get("strategy_mode")
        if strategy_mode and mode != strategy_mode:
            continue
        count += 1
    return count


def _profit_surge_risk_cap(balance: float) -> float:
    """초고수익률 자리 전용 계좌위험 한도."""
    return max(0.0, balance * PROFIT_SURGE_MAX_ACCOUNT_RISK_PCT)


def _is_profit_surge_opportunity(signal: dict, raw_strength: str,
                                 strategy: str, roi_metrics: dict,
                                 best_rr: float, tp1_rr: float) -> bool:
    """TAIKO/ZBT급 고수익률·고거래량 자리인지 판단한다."""
    if not PROFIT_SURGE_SIZING_ENABLED:
        return False
    raw = _raw_strength(raw_strength)
    if raw not in {"VERY STRONG", "ELITE"}:
        return False
    try:
        confirmed = int(signal.get("confirmed_count", 0) or 0)
        vol = float(signal.get("vol", {}).get("value", 0) or 0)
    except Exception:
        confirmed, vol = 0, 0.0

    active_strategy = any(base in strategy for base in ACTIVE_STRONG_STRATEGIES)
    high_roi = (
        float(roi_metrics.get("weighted_margin_roi_pct", 0) or 0) >= PROFIT_SURGE_MIN_MARGIN_ROI_PCT
        and float(roi_metrics.get("tp1_margin_roi_pct", 0) or 0) >= PROFIT_SURGE_MIN_TP1_MARGIN_ROI_PCT
    )
    return (
        high_roi
        and confirmed >= PROFIT_SURGE_MIN_CONFIRMED
        and vol >= PROFIT_SURGE_MIN_VOL
        and tp1_rr >= PROFIT_SURGE_MIN_TP1_RR
        and best_rr >= PROFIT_SURGE_MIN_BEST_RR
        and (active_strategy or bool(signal.get("asymmetric_mode")))
    )


def _refresh_target_rr(t: dict, direction: str) -> dict:
    """SL 변경 후 TP별 R:R을 다시 계산한다."""
    entry = float(t.get("entry", 0) or 0)
    sl = float(t.get("sl", 0) or 0)
    risk = abs(entry - sl)
    if entry <= 0 or risk <= 0:
        return t
    new_t = dict(t)
    new_t["tps"] = []
    for tp in t.get("tps", []) or []:
        tp2 = dict(tp)
        price = float(tp2.get("price", 0) or 0)
        gain = (price - entry) if direction == "LONG" else (entry - price)
        tp2["rr"] = round(gain / risk, 1) if gain > 0 else 0
        new_t["tps"].append(tp2)
    return new_t


def _apply_profit_surge_tight_stop(signal: dict, t: dict,
                                   direction: str, tf_key: str) -> tuple[dict, list[str]]:
    """
    초고수익률 자리의 SL을 더 가까운 구조적 무효화 가격으로 조정한다.

    TP는 유지하고 SL만 줄여 같은 계좌위험 안에서 증거금 사용액을 키운다.
    단, ATR/타임프레임 최소폭을 남겨 너무 쉽게 털리는 SL은 피한다.
    """
    if not PROFIT_SURGE_TIGHT_STOP_ENABLED or not signal.get("profit_surge_mode"):
        return t, []

    entry = float(t.get("entry", 0) or 0)
    old_sl = float(t.get("sl", 0) or 0)
    if entry <= 0 or old_sl <= 0:
        return t, []

    old_sl_pct = abs(entry - old_sl) / entry
    try:
        atr_pct = abs(float(signal.get("atr", 0) or 0)) / entry
    except Exception:
        atr_pct = 0.0
    max_pct = float(PROFIT_SURGE_STOP_MAX_PCT_BY_TF.get(tf_key, 0.10))
    target_pct = max(float(PROFIT_SURGE_STOP_MIN_PCT), atr_pct * float(PROFIT_SURGE_STOP_ATR_MULT))
    target_pct = min(target_pct, max_pct)
    if target_pct <= 0 or target_pct >= old_sl_pct:
        return t, []

    if direction == "LONG":
        new_sl = _round_price(entry * (1 - target_pct))
    else:
        new_sl = _round_price(entry * (1 + target_pct))

    new_t = dict(t)
    new_t["original_sl"] = old_sl
    new_t["original_sl_pct"] = round(old_sl_pct * 100, 2)
    new_t["sl"] = new_sl
    new_t["sl_pct"] = round(abs(entry - new_sl) / entry * 100, 2)
    new_t["profit_surge_tight_stop"] = True
    new_t = _refresh_target_rr(new_t, direction)

    signal["profit_surge_tight_stop"] = True
    signal["profit_surge_original_sl"] = old_sl
    signal["profit_surge_original_sl_pct"] = round(old_sl_pct * 100, 2)
    signal["profit_surge_tight_sl"] = new_sl
    signal["profit_surge_tight_sl_pct"] = new_t["sl_pct"]
    note = (
        f"초고수익률 타이트SL: SL {old_sl:.8g}({old_sl_pct*100:.1f}%)"
        f"→{new_sl:.8g}({new_t['sl_pct']:.1f}%), TP는 유지"
    )
    return new_t, [note]


def _compress_leverage_for_profit_surge(leverage: int, entry_price: float,
                                        direction: str, tps: list[dict],
                                        sl_pct: float, target_margin: float,
                                        balance: float, max_margin_usd: float,
                                        risk_cap_usd: float) -> tuple[int, dict, list[str]]:
    """
    초고수익률·넓은 SL 자리에서는 레버리지를 낮춰 증거금 사용액을 키운다.

    같은 계좌위험 한도에서는 `증거금 × 레버리지 × SL폭`이 핵심이다.
    SL폭이 매우 큰 종목은 고레버리지보다 저레버리지+큰 증거금이 더 안정적으로
    수익금을 키운다.
    """
    base_leverage = max(1, int(leverage or 1))
    metrics = _planned_roi_metrics(entry_price, direction, tps, base_leverage)
    if not PROFIT_SURGE_SIZING_ENABLED or base_leverage <= PROFIT_SURGE_MIN_LEVERAGE:
        return base_leverage, metrics, []
    if balance <= 0 or target_margin <= 0 or risk_cap_usd <= 0:
        return base_leverage, metrics, []

    loss_pct = float(sl_pct or 0) / 100 + ROUND_TRIP_FEE
    if loss_pct <= 0:
        return base_leverage, metrics, []

    desired_margin = min(
        float(target_margin),
        float(max_margin_usd),
        balance * PROFIT_SURGE_TARGET_MARGIN_PCT,
    )
    if desired_margin <= 0:
        return base_leverage, metrics, []

    max_safe_leverage_for_target = int(math.floor(risk_cap_usd / (desired_margin * loss_pct)))
    if max_safe_leverage_for_target >= PROFIT_SURGE_MIN_LEVERAGE:
        target_leverage = min(base_leverage, max_safe_leverage_for_target)
    else:
        target_leverage = int(PROFIT_SURGE_MIN_LEVERAGE)

    if target_leverage >= base_leverage:
        return base_leverage, metrics, []

    compressed_metrics = _planned_roi_metrics(entry_price, direction, tps, target_leverage)
    gate_reason, _ = _roi_gate_reason(entry_price, direction, tps, target_leverage)
    if gate_reason:
        return base_leverage, metrics, []

    old_safe_margin = min(
        desired_margin,
        risk_cap_usd / (base_leverage * loss_pct),
        max_margin_usd,
        balance * PROFIT_SURGE_TARGET_MARGIN_PCT,
    )
    new_safe_margin = min(
        desired_margin,
        risk_cap_usd / (target_leverage * loss_pct),
        max_margin_usd,
        balance * PROFIT_SURGE_TARGET_MARGIN_PCT,
    )
    note = (
        f"초고수익률 시드극대화: 레버리지 {base_leverage}x→{target_leverage}x, "
        f"위험캡 내 가능증거금 ${old_safe_margin:.2f}→${new_safe_margin:.2f} "
        f"(기대ROI {metrics['weighted_margin_roi_pct']:+.1f}%"
        f"→{compressed_metrics['weighted_margin_roi_pct']:+.1f}%, "
        f"TP1 {metrics['tp1_margin_roi_pct']:+.1f}%"
        f"→{compressed_metrics['tp1_margin_roi_pct']:+.1f}%)"
    )
    return target_leverage, compressed_metrics, [note]


def _ceil_position_pct_for_margin(balance: float, margin: float) -> float:
    """목표 증거금보다 1~2센트 작게 계산되는 반올림 차단을 막는다."""
    if balance <= 0 or margin <= 0:
        return 0.0
    return math.ceil((margin / balance) * 10000) / 10000


def _apply_min_trade_margin(balance: float, position_pct: float, leverage: int,
                            entry_price: float, sl_price: float,
                            max_margin_usd: float,
                            remaining_daily_risk: float,
                            target_margin_usd: float | None = None,
                            label: str = "최소 진입증거금",
                            allow_opportunity_override: bool = False,
                            opportunity_risk_cap_usd: float = 0.0) -> tuple[float, float, list[str], str]:
    """목표 증거금을 우선 맞추되, 좋은 자리는 가능한 증거금으로 축소 진입한다."""
    target_margin = float(target_margin_usd or MIN_TRADE_MARGIN_USD)
    if target_margin <= 0:
        return position_pct, 0.0, [], ""
    if balance <= 0 or leverage <= 0 or entry_price <= 0 or sl_price <= 0:
        return position_pct, 0.0, [], f"{label} 계산 실패 — 잔고/레버리지/가격 오류"

    notes: list[str] = []
    max_balance_margin = balance * MIN_TRADE_MARGIN_MAX_BALANCE_PCT
    fallback_floor = min(
        MIN_FALLBACK_TRADE_MARGIN_USD,
        max(max_margin_usd, 0.0),
        max(max_balance_margin, 0.0),
    )
    current_margin = min(balance * position_pct, max_margin_usd)
    if current_margin >= target_margin:
        return position_pct, 0.0, [], ""

    feasible_target = min(target_margin, max_margin_usd, max_balance_margin)
    if feasible_target + 1e-9 < target_margin:
        if feasible_target + 1e-9 < fallback_floor:
            return position_pct, 0.0, [], (
                f"{label} ${target_margin:.2f} 축소진입 불가 — "
                f"가능증거금 ${feasible_target:.2f} < 실행하한 ${fallback_floor:.2f}"
            )
        notes.append(
            f"{label} ${target_margin:.2f} 미달 → 가능증거금 ${feasible_target:.2f}로 축소진입"
        )
        target_margin = feasible_target

    target_pct = target_margin / balance
    if target_pct > MIN_TRADE_MARGIN_MAX_BALANCE_PCT + 1e-9:
        target_margin = max_balance_margin
        target_pct = target_margin / balance
        notes.append(
            f"{label} 비중 하드캡 적용 → 잔고 {MIN_TRADE_MARGIN_MAX_BALANCE_PCT*100:.0f}%"
        )

    boosted_pct = max(position_pct, _ceil_position_pct_for_margin(balance, target_margin))
    boosted_pct = min(boosted_pct, MIN_TRADE_MARGIN_MAX_BALANCE_PCT)
    boosted_margin = min(balance * boosted_pct, max_margin_usd)
    if boosted_margin + 0.01 < target_margin:
        return position_pct, 0.0, [], (
            f"증거금 상한 ${max_margin_usd:.2f} 때문에 {label} ${target_margin:.2f} 진입 불가"
        )
    boosted_margin = min(boosted_margin, max_balance_margin, max_margin_usd)

    sl_pct = abs(entry_price - sl_price) / entry_price if entry_price > 0 else 0.0
    loss_pct = sl_pct + ROUND_TRIP_FEE
    boosted_loss = boosted_margin * leverage * (sl_pct + ROUND_TRIP_FEE)
    if boosted_loss > remaining_daily_risk:
        if not allow_opportunity_override:
            return position_pct, 0.0, [], (
                f"{label} 적용 시 SL위험 ${boosted_loss:.2f} > 남은 일손실한도 ${remaining_daily_risk:.2f}"
            )

        hard_loss_cap = max(opportunity_risk_cap_usd, remaining_daily_risk)
        if hard_loss_cap <= 0 or loss_pct <= 0 or leverage <= 0:
            return position_pct, 0.0, [], (
                f"{label} 고기대수익 예외 불가 — 계좌위험 한도 계산 실패"
            )

        if boosted_loss > hard_loss_cap:
            capped_margin = hard_loss_cap / (leverage * loss_pct)
            capped_margin = min(capped_margin, boosted_margin, max_margin_usd, max_balance_margin)
            if capped_margin + 1e-9 < fallback_floor:
                return position_pct, 0.0, [], (
                    f"고기대수익이나 계좌위험 하드캡 ${hard_loss_cap:.2f} 내에서 "
                    f"실행하한 ${fallback_floor:.2f} 유지 불가"
                )
            boosted_pct = capped_margin / balance
            boosted_pct = min(max(boosted_pct, 0.0), MIN_TRADE_MARGIN_MAX_BALANCE_PCT)
            boosted_margin = min(balance * boosted_pct, max_margin_usd)
            boosted_loss = boosted_margin * leverage * loss_pct
            notes.append(
                f"{label} 고기대수익 예외: 일손실 소프트캡 대신 계좌위험 하드캡 적용 "
                f"${current_margin:.2f} → ${boosted_margin:.2f}, SL위험 ${boosted_loss:.2f}"
            )
            return boosted_pct, round(boosted_loss, 4), notes, ""

        notes.append(
            f"{label} 고기대수익 예외: 남은 일손실한도 ${remaining_daily_risk:.2f} 초과이나 "
            f"계좌위험 하드캡 ${hard_loss_cap:.2f} 내 진입"
        )
        return boosted_pct, round(boosted_loss, 4), notes, ""

    notes.append(
        f"{label} 적용: ${current_margin:.2f} → ${boosted_margin:.2f} "
        f"(포지션 {position_pct*100:.1f}%→{boosted_pct*100:.1f}%)"
    )
    return boosted_pct, round(boosted_loss, 4), notes, ""


def _apply_portfolio_capacity_gate(balance: float, position_pct: float,
                                   est_sl_loss: float, max_margin_usd: float,
                                   direction: str,
                                   high_opportunity: bool = False,
                                   label: str = "포트폴리오",
                                   min_execution_margin_usd: float | None = None) -> tuple[float, float, list[str], str]:
    """
    고정 동시 포지션 개수 대신 계좌 전체 증거금/SL위험으로 신규 진입을 판단한다.

    한도를 넘는 경우 가능한 범위까지 포지션을 축소하고, 축소 후 최소 실행 증거금을
    유지할 수 없을 때만 차단한다.
    """
    if balance <= 0 or position_pct <= 0:
        return position_pct, est_sl_loss, [], f"{label} 용량 계산 실패 — 잔고/포지션 비율 오류"

    planned_margin = min(balance * position_pct, max_margin_usd)
    if planned_margin <= 0:
        return position_pct, est_sl_loss, [], f"{label} 예정 증거금 계산 실패"
    execution_floor = max(
        MIN_FALLBACK_TRADE_MARGIN_USD,
        float(min_execution_margin_usd or MIN_TRADE_MARGIN_USD),
    )

    try:
        from trade_router import get_portfolio_risk_snapshot
        snapshot = get_portfolio_risk_snapshot(PORTFOLIO_POSITION_QUERY_RETRIES)
    except Exception as e:
        snapshot = {
            "ok": False, "reason": str(e), "count": 0, "margin_used": 0.0,
            "long_margin": 0.0, "short_margin": 0.0, "sl_risk": 0.0,
            "equity": balance, "free": balance,
        }

    notes: list[str] = []
    equity = float(snapshot.get("equity", 0) or 0)
    margin_used = float(snapshot.get("margin_used", 0) or 0)
    if equity <= 0:
        equity = max(balance + margin_used, balance)

    if not snapshot.get("ok"):
        reason = str(snapshot.get("reason", "") or "unknown")
        notes.append(
            f"포지션 상세조회 불완전({reason[:80]}) — 고정 개수차단 없이 가용잔고/추정증거금 기준"
        )

    margin_cap_pct = (
        PORTFOLIO_MARGIN_USAGE_HIGH_OPPORTUNITY_CAP
        if high_opportunity else PORTFOLIO_MARGIN_USAGE_CAP
    )
    sl_cap_pct = (
        PORTFOLIO_TOTAL_SL_RISK_HIGH_OPPORTUNITY_CAP_PCT
        if high_opportunity else PORTFOLIO_TOTAL_SL_RISK_CAP_PCT
    )
    directional_cap_pct = (
        PORTFOLIO_DIRECTIONAL_HIGH_OPPORTUNITY_CAP
        if high_opportunity else PORTFOLIO_DIRECTIONAL_MARGIN_CAP
    )

    def _scale_to_margin(max_new_margin: float, reason: str) -> tuple[bool, str]:
        nonlocal position_pct, est_sl_loss, planned_margin
        max_new_margin = max(0.0, min(max_new_margin, max_margin_usd, balance))
        if planned_margin <= max_new_margin + 1e-9:
            return True, ""
        if max_new_margin + 1e-9 < execution_floor:
            return False, (
                f"{reason} — 가능 신규증거금 ${max_new_margin:.2f} "
                f"< 실행하한 ${execution_floor:.2f}"
            )
        scale = max_new_margin / planned_margin * 0.98
        old_margin = planned_margin
        position_pct = round(max(position_pct * scale, 0.0), 4)
        est_sl_loss = round(max(est_sl_loss * scale, 0.0), 4)
        planned_margin = min(balance * position_pct, max_margin_usd)
        notes.append(
            f"{reason} → 증거금 ${old_margin:.2f}에서 ${planned_margin:.2f}로 축소"
        )
        return True, ""

    portfolio_margin_cap = equity * margin_cap_pct
    ok, block = _scale_to_margin(
        portfolio_margin_cap - margin_used,
        (
            f"계좌 증거금 사용률 한도 {margin_cap_pct*100:.0f}% "
            f"(현재 ${margin_used:.2f} / equity ${equity:.2f})"
        ),
    )
    if not ok:
        return position_pct, est_sl_loss, notes, block

    direction_margin = (
        float(snapshot.get("long_margin", 0) or 0)
        if direction == "LONG" else
        float(snapshot.get("short_margin", 0) or 0)
    )
    directional_margin_cap = equity * directional_cap_pct
    ok, block = _scale_to_margin(
        directional_margin_cap - direction_margin,
        (
            f"{direction} 방향 증거금 쏠림 한도 {directional_cap_pct*100:.0f}% "
            f"(현재 ${direction_margin:.2f} / equity ${equity:.2f})"
        ),
    )
    if not ok:
        return position_pct, est_sl_loss, notes, block

    existing_sl_risk = float(snapshot.get("sl_risk", 0) or 0)
    total_sl_cap = equity * sl_cap_pct
    allowed_new_sl = total_sl_cap - existing_sl_risk
    if est_sl_loss > allowed_new_sl + 1e-9:
        if allowed_new_sl <= 0:
            return position_pct, est_sl_loss, notes, (
                f"총 SL위험 한도 {sl_cap_pct*100:.0f}% 소진 "
                f"(${existing_sl_risk:.2f} / ${total_sl_cap:.2f})"
            )
        scale = allowed_new_sl / est_sl_loss * 0.98
        old_margin = planned_margin
        old_loss = est_sl_loss
        position_pct = round(max(position_pct * scale, 0.0), 4)
        est_sl_loss = round(max(est_sl_loss * scale, 0.0), 4)
        planned_margin = min(balance * position_pct, max_margin_usd)
        if planned_margin + 1e-9 < execution_floor:
            return position_pct, est_sl_loss, notes, (
                f"총 SL위험 한도 내 축소 시 증거금 ${planned_margin:.2f} "
                f"< 실행하한 ${execution_floor:.2f}"
            )
        notes.append(
            f"총 SL위험 한도 {sl_cap_pct*100:.0f}% → "
            f"SL위험 ${old_loss:.2f}에서 ${est_sl_loss:.2f}, "
            f"증거금 ${old_margin:.2f}에서 ${planned_margin:.2f}로 축소"
        )

    open_count = snapshot.get("count")
    max_open_positions = (
        PORTFOLIO_MAX_OPEN_POSITIONS_HIGH_OPPORTUNITY
        if high_opportunity else PORTFOLIO_MAX_OPEN_POSITIONS
    )
    if isinstance(open_count, int) and open_count >= max_open_positions:
        return position_pct, est_sl_loss, notes, (
            f"동시 포지션 안전한도 {max_open_positions}개 도달 "
            f"(현재 {open_count}개) — 추가 진입보다 기존 포지션 관리 우선"
        )
    if isinstance(open_count, int) and open_count >= 4:
        notes.append(
            f"포지션 분산 안전한도 확인: 현재 {open_count}/{max_open_positions}개, "
            f"증거금 ${margin_used:.2f}→${margin_used + planned_margin:.2f} 기준 허용"
        )
    else:
        notes.append(
            f"포트폴리오 용량 확인: 증거금 ${margin_used:.2f}→${margin_used + planned_margin:.2f} "
            f"/ 한도 ${portfolio_margin_cap:.2f}"
        )

    return position_pct, est_sl_loss, notes, ""


def _build_open_summary(trade_num: int, symbol: str, direction: str,
                        tf_key: str, strategy: str, strength: str,
                        leverage: int, qty: float, entry_price: float,
                        sl: float, tps: list, est_sl_loss: float,
                        analysis_sent: bool,
                        strategy_profile: dict | None = None) -> str:
    coin = symbol.split("/")[0]
    dir_label = "롱 LONG" if direction == "LONG" else "숏 SHORT"
    tp1 = tps[0]["price"] if tps else 0
    profile = strategy_profile or classify_strategy(strategy, "", False, direction)
    detail_line = (
        "상세 진입근거/기대수익 분석도 갓오브트레이딩으로 발송"
        if analysis_sent else
        "갓오브트레이딩 매매봇 미설정 — .env에 TRADE_BOT_TOKEN / TRADE_CHAT_ID 필요"
    )
    lines = [
        f"✅ <b>[매매 체결 #{trade_num}] {coin} {dir_label}</b>",
        f"전략: {tf_key} / {strategy} / {strength}",
        f"전략군: <b>{format_profile(profile)}</b>",
        f"진입: <b>{_price_text(entry_price)}</b>  |  SL: <b>{_price_text(sl)}</b>",
        f"TP1: <b>{_price_text(tp1)}</b>" if tp1 else "TP1: 미설정",
        f"레버리지 {leverage}x  |  수량 {qty}  |  예상 SL손실 ~${est_sl_loss:.2f}",
        f"📌 {detail_line}",
    ]
    return "\n".join(lines)


def _build_entry_context(signal: dict, tf_key: str, direction: str,
                         strategy: str, timing: dict | None,
                         mtf_boost: float, ema_aligned: bool,
                         is_golden: bool, best_rr: float,
                         tp1_rr: float, risk_notes: list[str]) -> dict:
    """체결 알림과 학습 로그가 공통으로 쓰는 진입 근거 묶음."""
    meta = SIGNAL_META.get(signal.get("signal_type", ""), SIGNAL_META["bullish"])
    quality = signal.get("divergence_quality", {}) or {}
    is_divergence = bool(signal.get("is_divergence", True))
    div_count = signal.get("divergence_count", signal.get("confirmed_count", 0))
    confirmed = signal.get("confirmed_count", 0)
    max_div = quality.get("max_divergence", 6)
    max_conf = quality.get("max_confirmed", 7)
    try:
        vol = float(signal.get("vol", {}).get("value", 0) or 0)
    except Exception:
        vol = 0.0
    bars_ago = signal.get("bars_ago", 0)
    ema_note = "EMA 방향일치" if ema_aligned else "EMA 비정렬이나 상위 조건으로 허용"
    quality_line = (
        f"다이버전스 {div_count}/{max_div}, 전체 확인 {confirmed}/{max_conf}"
        if is_divergence
        else f"전략조건 {confirmed}/{max_conf}, 보조지표 확인 {div_count}/{max_div}"
    )
    profile_seed = {}
    if (
        is_divergence
        and signal.get("signal_type", "") in {"bullish", "bearish"}
        and (0 < mtf_boost < 1.0 or not ema_aligned)
    ):
        profile_seed["reasons"] = ["역추세 반전 조건"]
    profile = classify_strategy(
        strategy, signal.get("signal_type", ""), is_divergence, direction,
        entry_context=profile_seed,
        asymmetric=signal.get("asymmetric_mode", False),
    )

    reasons = [
        f"전략군: {format_profile(profile)}",
        f"{tf_key} {meta['label']} 기반 {direction} 진입",
        quality_line,
        f"거래량 {vol:.2f}x, 신호 신선도 {bars_ago}봉 전",
        ema_note,
        f"TP1 R:R 1:{tp1_rr}, 최대 R:R 1:{best_rr}",
    ]
    mtf_soft_note = signal.get("mtf_soft_override_note", "")
    if mtf_soft_note:
        reasons.append(mtf_soft_note)
    elif mtf_boost > 1.0:
        reasons.append(f"상위봉 MTF 정렬로 리스크/확신도 {mtf_boost:.2f}x 반영")
    elif 0 < mtf_boost < 1.0:
        reasons.append(f"MTF 역방향 리스크 감액 {mtf_boost:.2f}x 반영")
    if is_golden:
        reasons.append("ELITE + MTF 전정렬 + EMA 정렬 황금진입")
    if signal.get("asymmetric_mode"):
        reasons.append("비대칭 손익 모드: 손실은 짧게, 잔량 목표는 넓게 운용")
    if signal.get("profit_surge_mode"):
        reasons.append("초고수익률 시드극대화 모드: 레버리지를 낮춰 위험캡 내 증거금 사용액 확대")
    if signal.get("profit_surge_tight_stop"):
        reasons.append(
            "초고수익률 타이트SL 적용: "
            f"기존 SL {signal.get('profit_surge_original_sl_pct', 0):.1f}% "
            f"→ {signal.get('profit_surge_tight_sl_pct', 0):.1f}%"
        )
    hyper_lead = signal.get("hyperliquid_lead") or {}
    if hyper_lead:
        lead_dir = hyper_lead.get("direction", "-")
        agree_note = "진입방향 일치" if lead_dir == direction else "진입방향 불일치"
        reasons.append(
            "전략5 Hyperliquid 선행수급: "
            f"순위 #{hyper_lead.get('rank', '-')}, {lead_dir} {agree_note}, "
            f"15m {float(hyper_lead.get('ret_15m_pct', 0) or 0):+.2f}%, "
            f"1h {float(hyper_lead.get('ret_1h_pct', 0) or 0):+.2f}%, "
            f"VOL {float(hyper_lead.get('vol_ratio', 0) or 0):.2f}x, "
            f"OI {hyper_lead.get('open_interest_label', '-')}"
        )
    if risk_notes:
        reasons.append("리스크 거버너: " + " | ".join(risk_notes))

    return {
        "strategy": strategy,
        "strategy_profile": profile,
        "strategy_family": profile["family_label"],
        "core_strategy": profile["strategy_label"],
        "strategy_mode": profile["family_key"],
        "asymmetric_mode": bool(signal.get("asymmetric_mode")),
        "profit_surge_mode": bool(signal.get("profit_surge_mode")),
        "profit_surge_leverage": signal.get("profit_surge_leverage", 0),
        "profit_surge_tight_stop": bool(signal.get("profit_surge_tight_stop")),
        "profit_surge_original_sl": signal.get("profit_surge_original_sl", 0),
        "profit_surge_original_sl_pct": signal.get("profit_surge_original_sl_pct", 0),
        "signal_type": signal.get("signal_type", ""),
        "signal_label": meta["label"],
        "direction": direction,
        "tf": tf_key,
        "reasons": reasons,
        "timing_note": (timing or {}).get("note", ""),
        "timing_tf": (timing or {}).get("tf", ""),
        "indicator_snapshot": _indicator_snapshot(signal),
        "hyperliquid_lead": hyper_lead,
        "hyperliquid_lead_agrees": bool(hyper_lead and hyper_lead.get("direction") == direction),
        "is_divergence": is_divergence,
        "divergence_count": div_count,
        "confirmed_count": confirmed,
        "divergence_quality": quality,
        "bars_ago": bars_ago,
        "vol_ratio": vol,
        "ema_trend": signal.get("ema_trend", 0),
        "ema_aligned": ema_aligned,
        "mtf_boost": mtf_boost,
        "mtf_soft_override_kind": signal.get("mtf_soft_override_kind", ""),
        "mtf_soft_override_note": mtf_soft_note,
        "mtf_soft_risk_mult": signal.get("mtf_soft_risk_mult", 1.0),
        "is_golden": is_golden,
        "rr": {"tp1": tp1_rr, "best": best_rr},
    }


def _elite_mtf_override(signal: dict, mtf_info: dict) -> dict:
    """
    MTF 완전 역방향이어도 7/7 ELITE 다이버전스는 소액 허용한다.
    일반 다이버전스는 추세 전환 초입, 히든 다이버전스는 강한 추세 지속/재개 신호일 수 있다.
    다만 상위봉이 완전 반대이므로 리스크는 감액한다.
    """
    denied = {"allow": False, "kind": "", "risk_mult": 1.0}
    if not mtf_info.get("block"):
        return denied
    signal_type = signal.get("signal_type")
    if signal_type not in ("bullish", "bearish", "hidden_bullish", "hidden_bearish"):
        return denied
    if _raw_strength(signal.get("strength", "")) != "ELITE":
        return denied

    quality = signal.get("divergence_quality", {}) or {}
    max_conf = int(quality.get("max_confirmed", 7) or 7)
    max_div = int(quality.get("max_divergence", 6) or 6)
    confirmed = int(signal.get("confirmed_count", 0) or 0)
    div_count = int(signal.get("divergence_count", 0) or 0)
    if confirmed < max_conf or div_count < max_div:
        return denied

    if signal_type.startswith("hidden_"):
        return {
            "allow": True,
            "kind": "히든",
            "risk_mult": ELITE_MTF_HIDDEN_RISK_MULT,
        }
    return {
        "allow": True,
        "kind": "반전",
        "risk_mult": ELITE_MTF_REVERSAL_RISK_MULT,
    }


def _allow_elite_mtf_override(signal: dict, mtf_info: dict) -> bool:
    return _elite_mtf_override(signal, mtf_info)["allow"]


def _full_elite_divergence(signal: dict) -> dict:
    return _elite_mtf_override(signal, {"block": True})


def _live_asymmetric_candidate(signal: dict, tf_key: str, strategy: str) -> bool:
    """초고거래량 추세형 신호는 과거 표본이 부족해도 러너형 후보로 본다."""
    try:
        vol = float(signal.get("vol", {}).get("value", 0) or 0)
    except Exception:
        vol = 0.0
    active_strategy = any(base in strategy for base in ACTIVE_STRONG_STRATEGIES) or strategy == "돌파"
    return (
        tf_key in ASYMMETRIC_TF
        and active_strategy
        and int(signal.get("confirmed_count", 0) or 0) >= 5
        and vol >= ACTIVE_ULTRA_VOL
        and int(signal.get("bars_ago", 0) or 0) <= 1
    )


_DIVERGENCE_SIGNAL_TYPES = {"bullish", "bearish", "hidden_bullish", "hidden_bearish"}


def _mark_mtf_soft_override(signal: dict, override: dict) -> None:
    """체결 저널/알림에 MTF 완화 사유가 남도록 신호에 메타를 붙인다."""
    if not override.get("allow"):
        return
    signal["mtf_soft_override_kind"] = override.get("kind", "")
    signal["mtf_soft_override_note"] = override.get("note", "")
    signal["mtf_soft_risk_mult"] = override.get("risk_mult", 1.0)


def _mtf_soft_override(signal: dict, mtf_info: dict, tf_key: str,
                       strategy: str, direction: str) -> dict:
    """
    MTF 완전 역방향을 무조건 차단하지 않고, 전략 성격별로 감액 진입 가능성을 판단한다.

    MTF는 추세추종에는 강한 필터지만, 다이버전스/거래량 급등은 상위봉이 뒤늦게
    따라오는 구간에서 먼저 발생할 수 있다. 그래서 강한 신호만 살리고 리스크를 낮춘다.
    """
    denied = {"allow": False, "kind": "", "risk_mult": 1.0, "note": "", "elite": False}
    if not mtf_info.get("block"):
        return denied

    # 2026-07-03 리뷰: MTF 완전역방향 소프트 통과는 LONG 전용.
    # 통과 SHORT 실측 5건 2승 3패 -$3.39 (PYTH -2.95, TAIKO -1.96) → 하드 차단.
    if MTF_SOFT_OVERRIDE_LONG_ONLY and direction == "SHORT":
        return denied

    elite = _elite_mtf_override(signal, mtf_info)
    if elite["allow"]:
        note = (
            f"MTF 완전역방향이지만 7/7 ELITE {elite['kind']} 다이버전스 "
            f"→ 리스크×{elite['risk_mult']:.2f} 감액 진입"
        )
        return {
            "allow": True,
            "kind": f"ELITE {elite['kind']}",
            "risk_mult": elite["risk_mult"],
            "note": note,
            "elite": True,
        }

    raw = _raw_strength(str(signal.get("strength", "")))
    signal_type = signal.get("signal_type", "")
    try:
        confirmed = int(signal.get("confirmed_count", 0) or 0)
        div_count = int(signal.get("divergence_count", confirmed) or 0)
        bars_raw = signal.get("bars_ago", 99)
        bars_ago = int(99 if bars_raw is None else bars_raw)
        vol = float(signal.get("vol", {}).get("value", 0) or 0)
    except Exception:
        confirmed, div_count, bars_ago, vol = 0, 0, 99, 0.0

    is_divergence = signal_type in _DIVERGENCE_SIGNAL_TYPES
    is_hidden = signal_type.startswith("hidden_")

    if (
        is_divergence
        and raw in {"VERY STRONG", "ELITE"}
        and confirmed >= MTF_SOFT_MIN_CONFIRMED
        and div_count >= MTF_SOFT_MIN_DIVERGENCE
        and vol >= MTF_SOFT_MIN_VOL
    ):
        risk_mult = MTF_SOFT_HIDDEN_RISK_MULT if is_hidden else MTF_SOFT_REVERSAL_RISK_MULT
        if raw == "ELITE":
            risk_mult = min(risk_mult + 0.05, 0.75)
        kind = "히든 고확신" if is_hidden else "반전 고확신"
        note = (
            f"MTF 완전역방향이나 {kind} 다이버전스 "
            f"{confirmed}/7, D{div_count}/6, VOL {vol:.1f}x "
            f"→ 차단 대신 리스크×{risk_mult:.2f}"
        )
        return {
            "allow": True,
            "kind": kind,
            "risk_mult": risk_mult,
            "note": note,
            "elite": False,
        }

    active_strategy = any(base in strategy for base in ACTIVE_STRONG_STRATEGIES)
    ema_dir_ok = (
        (direction == "LONG" and signal.get("ema_trend") == 1) or
        (direction == "SHORT" and signal.get("ema_trend") == -1)
    )
    high_volume_current = (
        not is_divergence
        and active_strategy
        and tf_key in ASYMMETRIC_TF
        and confirmed >= 5
        and bars_ago <= 1
        and vol >= ACTIVE_HIGH_VOL
        and (ema_dir_ok or vol >= ACTIVE_ULTRA_VOL)
    )
    if high_volume_current or _live_asymmetric_candidate(signal, tf_key, strategy):
        note = (
            f"MTF 완전역방향이나 고거래량 현재봉 {strategy} "
            f"{confirmed}/6, VOL {vol:.1f}x "
            f"→ 차단 대신 리스크×{ACTIVE_MTF_REVERSAL_RISK_MULT:.2f}"
        )
        return {
            "allow": True,
            "kind": "고거래량 현재봉",
            "risk_mult": ACTIVE_MTF_REVERSAL_RISK_MULT,
            "note": note,
            "elite": False,
        }

    return denied


def _trend_min_confirm(tf_key: str, trend_score: int, is_continuation: bool) -> int | None:
    """
    추세 점수(주봉+일봉) 기반 최소 confirmed_count 반환.
    None = 진입 완전 차단.

    퀀트 원칙: 추세 이중 일치 = 포지션 보너스(+20%)이지 임계값 하향이 아님.
    임계값 하향은 노이즈 신호를 허용하는 것 — 퀀트는 품질을 타협하지 않는다.

      2/2 이중 일치: 약간 완화 (4,4,4,5,5) + 포지션 보너스 ×1.2
      1/2 단일 일치: 표준 임계값 (4,4,5,6,6)
      0/2 완전 역추세:
        continuation(hidden): 7/7 ELITE만 소액 허용
        reversal(bullish/bearish): 고확신 신호만
    """
    if trend_score == 2:
        return {"5m": 4, "15m": 4, "1h": 4, "4h": 5, "1d": 5}.get(tf_key, 4)
    if trend_score == 1:
        return {"5m": 4, "15m": 4, "1h": 5, "4h": 6, "1d": 6}.get(tf_key, 4)
    # trend_score == 0: 완전 역추세
    if is_continuation:
        return 7      # hidden divergence 완전 역추세 = 7/7 ELITE만 허용
    return 6          # 반전 신호만 ELITE로 허용


def _try_auto_trade(symbol: str, tf_key: str, signals: list,
                    current_price: float, scalp: bool = False,
                    mtf_boost: float = 1.0,
                    premium_mtf: bool = False):
    """신호가 있을 때 자동매매 실행 시도.
    복리형 베팅: 신호 강도별 계좌 위험률을 먼저 정하고,
    SL폭/레버리지로 증거금 비율을 역산한다.
    """
    from trade_router import (execute, get_usdt_balance, build_trade_notification,
                        _append_trade, add_trade_context, MAX_MARGIN_USD, MAX_SCALP_MARGIN_USD,
                        MAX_DAILY_LOSS,
                        get_daily_loss_limit, get_margin_cap, log_trade_candidate,
                        log_execution_journal, notify_trade_block,
                        position_pct_for_risk, _load_state)

    best      = max(signals, key=lambda x: x["confirmed_count"])
    meta      = SIGNAL_META.get(best["signal_type"], SIGNAL_META["bullish"])
    direction = meta["direction"]
    strength  = best["strength"]
    raw       = _raw_strength(strength)
    ema_trend = best.get("ema_trend", 0)
    strategy  = best.get("strategy", best.get("signal_type", "다이버전스"))
    btc_macro = _btc_macro_short_bias() if symbol == BTC_MACRO_SHORT_SYMBOL else {"active": False}
    btc_macro_reference = bool(btc_macro.get("active"))
    btc_macro_reference_note = ""
    if btc_macro_reference and BTC_MACRO_TREND_REFERENCE_ONLY:
        btc_macro_reference_note = (
            "BTC 장기봉 참고만 적용: 월봉/주봉은 배경 추세 메모이며 "
            f"진입·시드확대·롱차단에는 미사용 ({btc_macro.get('note', '')})"
        )
    btc_macro_short = bool(
        btc_macro_reference
        and direction == "SHORT"
        and not BTC_MACRO_TREND_REFERENCE_ONLY
    )
    if btc_macro_short:
        best = dict(best)
        strategy = "BTC Macro Short"
        best["strategy"] = strategy
        best["btc_macro_short_mode"] = True
        best["btc_macro_short_bias"] = btc_macro
    premium_mtf_entry = bool(premium_mtf and raw in PREMIUM_MTF_AUTO_STRENGTHS)
    ema_aligned = (
        (direction == "LONG"  and ema_trend == 1) or
        (direction == "SHORT" and ema_trend == -1)
    )
    asym_mult, asym_notes, _asym_profile = get_asymmetric_profile(symbol, tf_key, strategy, direction)
    live_asym = _live_asymmetric_candidate(best, tf_key, strategy)
    asymmetric_mode = bool(asym_mult > 1.0 or live_asym)
    if asymmetric_mode:
        best = dict(best)
        best["asymmetric_mode"] = True
        if live_asym and not asym_notes:
            asym_notes = [
                f"초고거래량 비대칭 후보 VOL {best.get('vol', {}).get('value', 0)}x → 러너형 TP"
            ]

    def _block(reason: str, send_diag: bool = False, **extra):
        block_strategy = extra.pop("strategy_override", strategy)
        notify_trade_block(
            symbol, tf_key, direction, strength, reason,
            strategy=block_strategy,
            send_telegram=(AUTO_TRADE_DIAGNOSTICS and send_diag),
            price=current_price,
            confirmed_count=best.get("confirmed_count", 0),
            bars_ago=best.get("bars_ago", 0),
            vol_ratio=best.get("vol", {}).get("value", 0),
            **extra,
        )

    repeat_profile = classify_strategy(
        strategy,
        best.get("signal_type", ""),
        best.get("is_divergence", True),
        direction,
        asymmetric=asymmetric_mode,
    )
    repeat_limit = (
        ASYMMETRIC_SYMBOL_DAILY_LOSS_LIMIT
        if repeat_profile["family_key"] == "asymmetric_edge"
        else SYMBOL_STRATEGY_DAILY_LOSS_LIMIT
    )

    # 심볼 당일 총 손실 한도 — 전략 불문 동일 심볼 재진입 차단 (DYDX 6회·TAIKO 6회 반복 방지)
    if SYMBOL_DAILY_TOTAL_LOSS_LIMIT > 0:
        _cur_state = _load_state()
        total_sym_losses = _today_loss_count(_cur_state, symbol=symbol)
        if total_sym_losses >= SYMBOL_DAILY_TOTAL_LOSS_LIMIT:
            _block(
                f"오늘 {symbol} 총 손실 {total_sym_losses}회 >= 한도 {SYMBOL_DAILY_TOTAL_LOSS_LIMIT}회 — 당일 재진입 차단",
                send_diag=True,
            )
            return
        # 연패 쿨다운 심볼 확인
        if symbol in get_cooldown_symbols():
            _block(f"{symbol} 연패 쿨다운 중 — 재진입 보류", send_diag=True)
            return

    if repeat_limit > 0:
        repeat_losses = _today_loss_count(
            _load_state(),
            symbol=symbol,
            strategy_mode=repeat_profile["family_key"],
        )
        if repeat_losses >= repeat_limit:
            _block(
                f"오늘 {symbol} {repeat_profile['family_label']} 손실 {repeat_losses}회 "
                f">= 한도 {repeat_limit}회 — 같은 조합 반복진입 차단",
                send_diag=True,
                strategy_mode=repeat_profile["family_key"],
                core_strategy=repeat_profile["strategy_label"],
            )
            return

    if tf_key in TIMING_ONLY_TF:
        _block("5m는 초단타 보조 참고용 — 15m 이상 판단봉 없이는 단독 실거래 금지", paper_only=True)
        return

    if (
        symbol == BTC_MACRO_SHORT_SYMBOL
        and BTC_MACRO_SHORT_BLOCK_LONG
        and not BTC_MACRO_TREND_REFERENCE_ONLY
        and btc_macro.get("active")
        and direction == "LONG"
    ):
        _block(
            "BTC 월봉 숏 전용 모드 — 롱 신호는 후보 기록만 "
            f"({btc_macro.get('note', '')})",
            send_diag=True,
            paper_only=True,
            strategy_override="BTC Macro Short",
            signal_type="btc_macro_short_block_long",
            strategy_mode="btc_macro_short",
            btc_macro_short_score=btc_macro.get("score", 0),
            btc_macro_short_note=btc_macro.get("note", ""),
        )
        return

    ok_strategy, why_strategy = is_tradeable_with_strategy(
        symbol, tf_key, strategy, direction,
        signal_type=best.get("signal_type", ""),
        is_divergence=best.get("is_divergence", True),
        asymmetric=asymmetric_mode,
    )
    if not ok_strategy:
        _block(why_strategy, send_diag=True)
        return

    timing_risk_mult = 1.0
    timing_risk_note = ""
    timing = _check_lower_tf_timing(symbol, tf_key, direction)
    if not timing["ok"]:
        vol_r = float(best.get("vol", {}).get("value", 0) or 0)
        high_quality_additional = (
            not best.get("is_divergence", True)
            and best.get("confirmed_count", 0) >= 5
            and vol_r >= ACTIVE_HIGH_VOL
            and (ema_aligned or mtf_boost >= 1.0 or vol_r >= ACTIVE_ULTRA_VOL)
        )
        vwap_chasing = "VWAP추격" in timing["note"] or "VWAP NO" in timing["note"]
        if high_quality_additional and vwap_chasing and vol_r < ACTIVE_ULTRA_VOL:
            _block(
                f"{timing['tf']} 보조봉 VWAP 추격 — 고거래량이라도 타점 과열 차단 | "
                f"{timing['note']}"
            )
            return
        high_conviction_timing = (
            raw in {"VERY STRONG", "ELITE"}
            and (premium_mtf_entry or btc_macro_short or ema_aligned or mtf_boost >= 1.2 or vol_r >= ACTIVE_HIGH_VOL)
            and "VWAP OK" in timing["note"]
            and "EMA OK" in timing["note"]
        )
        if high_quality_additional or asymmetric_mode or high_conviction_timing:
            if asymmetric_mode:
                label = "비대칭 러너형"
            elif high_quality_additional:
                label = "고거래량 추가전략"
            elif btc_macro_short:
                label = "BTC 월봉 숏 고확신"
            else:
                label = "고확신 판단봉"
            timing_risk_mult = ASYMMETRIC_TIMING_OVERRIDE_MULT
            timing_risk_note = (
                f"{label}: {timing['tf']} 보조봉 미일치 → "
                f"차단 대신 리스크×{timing_risk_mult:.2f}"
            )
            print(f"  [보조확인완화] {timing_risk_note} | {timing['note']}")
        else:
            _block(f"{timing['tf']} 보조봉 확인 불일치 — {timing['note']}")
            return
    if timing["tf"]:
        print(f"  [보조확인] {timing['note']}")

    # 고정 동시 포지션 개수 제한은 쓰지 않는다.
    # 신규 포지션의 실제 허용 여부는 사이징 후 포트폴리오 증거금/SL위험으로 판단한다.

    # 전략 화이트리스트: EMA 계열 외 전략은 후보 기록만, 실거래 없음
    if AUTO_TRADE_STRATEGY_WHITELIST and strategy not in AUTO_TRADE_STRATEGY_WHITELIST:
        _block(
            f"{strategy} — 화이트리스트 미포함, EMA 계열만 실거래 허용",
            paper_only=True,
        )
        return

    # SHORT 임시 차단: SHORT 22건 36% 승률·손실 84% → EMA LONG 집중
    if BLOCK_SHORT_AUTO_TRADE and direction == "SHORT":
        _block("SHORT 실거래 임시 차단 — EMA LONG 전략 집중", paper_only=True)
        return

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
    vol_for_ema_gate = float(best.get("vol", {}).get("value", 0) or 0)
    ultra_vol_additional = (
        not best.get("is_divergence", True)
        and best.get("confirmed_count", 0) >= 5
        and vol_for_ema_gate >= ACTIVE_ULTRA_VOL
    )
    ema_neutral_mtf_override = (
        ema_trend == 0
        and raw in {"VERY STRONG", "ELITE"}
        and best.get("confirmed_count", 0) >= 6
        and mtf_boost > 1.0
        and vol_for_ema_gate >= get_adaptive_min_vol()
    )
    if (
        ema_trend == 0
        and "ELITE" not in strength
        and not ultra_vol_additional
        and not ema_neutral_mtf_override
    ):
        _block("EMA 중립 — ELITE 아님")
        return
    if ema_trend == 0 and ultra_vol_additional:
        print(f"  [EMA중립완화] 초고거래량 추가전략 VOL {vol_for_ema_gate:.1f}x → 중립 게이트 통과")
    if ema_neutral_mtf_override:
        print(
            f"  [EMA중립완화] MTF/확인수/볼륨 우위 "
            f"({raw}, {best.get('confirmed_count', 0)}/7, VOL {vol_for_ema_gate:.1f}x) "
            f"→ 리스크 {EMA_NEUTRAL_MTF_RISK_MULT:.2f}x 감액 진입"
        )

    # ── 황금 진입 판정: ELITE + MTF 전정렬 + EMA 방향일치 ──────────────────────
    is_golden = ("ELITE" in strength) and (mtf_boost > 1.0) and ema_aligned

    # 황금 진입이면 레버리지를 먼저 부스트해서 calc_targets에도 반영
    leverage = _get_leverage(strength, tf_key)
    if is_golden:
        leverage = min(int(leverage * GOLDEN_LEVERAGE_BOOST), GOLDEN_MAX_LEVERAGE)

    leverage_notes = []
    leverage, leverage_notes = get_quality_leverage_adjustment(
        symbol, tf_key, strategy, direction, leverage
    )
    quality_leverage_notes = list(leverage_notes)
    for note in leverage_notes:
        print(f"  [레버리지학습] {note}")
    if btc_macro_short:
        base_btc_leverage = leverage
        leverage = min(
            int(math.ceil(leverage * float(BTC_MACRO_SHORT_LEVERAGE_MULT))),
            int(BTC_MACRO_SHORT_MAX_LEVERAGE),
        )
        if leverage > base_btc_leverage:
            note = (
                f"BTC 월봉 숏 전략 레버리지 {base_btc_leverage}x→{leverage}x "
                f"(숏점수 {btc_macro.get('score', 0)}/7)"
            )
            leverage_notes.append(note)
            quality_leverage_notes.append(note)
            print(f"  [BTC월봉숏] {note}")

    t = calc_targets(best, current_price, direction, leverage, tf_key, strength)
    if not t:
        _block("타겟 계산 실패", send_diag=True)
        return
    if asymmetric_mode:
        print(f"  [비대칭TP] 손실 짧게 / 러너 길게 — {' | '.join(asym_notes)}")

    leverage, roi_metrics, roi_leverage_notes = _raise_leverage_for_roi(
        t["entry"], direction, t["tps"], leverage
    )
    if roi_leverage_notes:
        leverage_notes.extend(roi_leverage_notes)
        for note in roi_leverage_notes:
            print(f"  [ROI레버리지] {note}")
    if btc_macro_short and leverage > BTC_MACRO_SHORT_MAX_LEVERAGE:
        leverage = int(BTC_MACRO_SHORT_MAX_LEVERAGE)
        roi_metrics = _planned_roi_metrics(t["entry"], direction, t["tps"], leverage)
        cap_note = f"BTC 월봉 숏 전략 레버리지 상한 {BTC_MACRO_SHORT_MAX_LEVERAGE}x 적용"
        leverage_notes.append(cap_note)
        print(f"  [BTC월봉숏] {cap_note}")

    leverage, sl_leverage_notes = _cap_leverage_for_initial_sl(leverage, t.get("sl_pct", 0))
    if sl_leverage_notes:
        roi_metrics = _planned_roi_metrics(t["entry"], direction, t["tps"], leverage)
        leverage_notes.extend(sl_leverage_notes)
        quality_leverage_notes.extend(sl_leverage_notes)
        for note in sl_leverage_notes:
            print(f"  [SL레버리지] {note}")

    roi_reason, roi_metrics = _roi_gate_reason(t["entry"], direction, t["tps"], leverage)
    if roi_reason:
        _block(roi_reason, send_diag=True, expected_margin_roi=roi_metrics["weighted_margin_roi_pct"])
        return

    # R:R 필터
    # TP1 R:R: SL 1.5 ATR 기준 TP1이 최소 1.0:1 이상이어야 진입 가치 있음
    # best_rr(TP3 기준)만 보면 7ATR이 안 닿아도 통과 → TP1도 별도 체크
    best_rr  = max(tp["rr"] for tp in t["tps"])
    tp1_rr   = t["tps"][0]["rr"] if t["tps"] else 0
    profit_surge = _is_profit_surge_opportunity(
        best, raw, strategy, roi_metrics, best_rr, tp1_rr
    )
    profit_surge_guard_notes: list[str] = []
    if profit_surge and timing_risk_mult < 1.0:
        vol_r = float(best.get("vol", {}).get("value", 0) or 0)
        if vol_r < ACTIVE_ULTRA_VOL:
            profit_surge = False
            note = (
                f"보조봉 미일치 상태라 수익극대화 시드확대 해제 "
                f"(거래량 {vol_r:.2f}x < 초고거래량 {ACTIVE_ULTRA_VOL:.2f}x)"
            )
            profit_surge_guard_notes.append(note)
            print(f"  [수익극대화] {note}")
    profit_surge_stop_notes: list[str] = []
    if profit_surge:
        best["profit_surge_mode"] = True
        t, profit_surge_stop_notes = _apply_profit_surge_tight_stop(
            best, t, direction, tf_key
        )
        if profit_surge_stop_notes:
            best_rr = max(tp["rr"] for tp in t["tps"])
            tp1_rr = t["tps"][0]["rr"] if t["tps"] else 0
            for note in profit_surge_stop_notes:
                print(f"  [수익극대화] {note}")
    base_min_rr = min(get_adaptive_min_rr(), ACTIVE_MAX_MIN_RR)
    if t.get("fast_exit"):
        active_min_rr = ACTIVE_FAST_MIN_RR
    elif not best.get("is_divergence", True):
        active_min_rr = min(base_min_rr, ACTIVE_MAX_MIN_RR)
    else:
        active_min_rr = base_min_rr * (0.8 if is_golden else 1.0)
    if best_rr < active_min_rr:
        _block(f"R:R {best_rr} < {active_min_rr:.1f}", best_rr=best_rr, min_rr=active_min_rr)
        return
    tp1_min_rr = FAST_TP1_MIN_RR if t.get("fast_exit") else 1.0
    if tp1_rr < tp1_min_rr:
        _block(f"TP1 R:R {tp1_rr} < {tp1_min_rr:.2f} — 첫 TP조차 손절보다 작음", tp1_rr=tp1_rr)
        return
    if roi_leverage_notes and (
        tp1_rr < ROI_RESCUE_MIN_TP1_RR or best_rr < ROI_RESCUE_MIN_BEST_RR
    ):
        _block(
            f"ROI 레버리지 보정 차단 — 손익비 부족 "
            f"(TP1 R:R 1:{tp1_rr:.1f}/{ROI_RESCUE_MIN_TP1_RR:.1f}, "
            f"최대 R:R 1:{best_rr:.1f}/{ROI_RESCUE_MIN_BEST_RR:.1f})",
            send_diag=True,
            tp1_rr=tp1_rr,
            best_rr=best_rr,
            expected_margin_roi=roi_metrics["weighted_margin_roi_pct"],
        )
        return

    quality_mult, quality_notes = get_signal_quality_adjustment(
        symbol, tf_key, strategy, direction
    )
    if quality_mult <= 0:
        _block("후보 사후승률 음수 기대값 — " + " | ".join(quality_notes), send_diag=True)
        return
    realized_mult, realized_notes = get_realized_trade_adjustment(
        symbol, tf_key, strategy, direction,
        signal_type=best.get("signal_type", ""),
        is_divergence=best.get("is_divergence", True),
        asymmetric=asymmetric_mode,
    )
    if realized_mult <= 0:
        _block("실체결 손익학습 차단 — " + " | ".join(realized_notes), send_diag=True)
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
        send_signal(
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
        elif 0 < mtf_boost < 1.0:
            risk_pct *= mtf_boost
            print(f"  [MTF감액] 상위봉 역방향 조건부 진입 → 리스크 {mtf_boost:.2f}x ({risk_pct*100:.2f}%)")
        if ema_neutral_mtf_override:
            risk_pct *= EMA_NEUTRAL_MTF_RISK_MULT
            print(f"  [EMA중립감액] 방향 중립 보정 → 리스크 {EMA_NEUTRAL_MTF_RISK_MULT:.2f}x ({risk_pct*100:.2f}%)")

        max_position_pct = min(MARGIN_BY_STRENGTH.get(raw, 0.25), MTF_POSITION_CAP)

    btc_macro_notes = []
    if btc_macro_short:
        old_risk_pct = risk_pct
        risk_pct = min(
            risk_pct * float(BTC_MACRO_SHORT_RISK_MULT),
            float(BTC_MACRO_SHORT_MAX_ACCOUNT_RISK_PCT),
            MAX_ACCOUNT_RISK_PCT,
        )
        old_cap = max_position_pct
        max_position_pct = min(
            max(max_position_pct, float(BTC_MACRO_SHORT_POSITION_CAP)),
            MIN_TRADE_MARGIN_MAX_BALANCE_PCT,
        )
        btc_macro_notes.append(
            f"BTC 월봉 숏 전략: 롱 차단/숏 우대, 리스크 "
            f"{old_risk_pct*100:.2f}%→{risk_pct*100:.2f}%, "
            f"포지션캡 {old_cap*100:.0f}%→{max_position_pct*100:.0f}%"
        )
        btc_macro_notes.append(btc_macro.get("note", ""))
        print(f"  [BTC월봉숏] {btc_macro_notes[0]}")

    risk_mult, risk_notes = get_risk_multiplier(
        tf_key, strategy, symbol, premium_mtf=premium_mtf_entry
    )
    if risk_mult < 1.0:
        risk_pct *= risk_mult
        print(f"  [리스크거버너] {' | '.join(risk_notes)} → 목표리스크 {risk_pct*100:.2f}%")
    elif premium_mtf_entry and risk_notes:
        print(f"  [MTF고확신] {' | '.join(risk_notes)} → 정상 리스크 유지")
    if quality_mult != 1.0:
        risk_pct = min(risk_pct * quality_mult, MAX_ACCOUNT_RISK_PCT)
        print(f"  [후보사후평가] {' | '.join(quality_notes)} → 목표리스크 {risk_pct*100:.2f}%")
    if realized_mult != 1.0:
        risk_pct = min(risk_pct * realized_mult, MAX_ACCOUNT_RISK_PCT)
        print(f"  [실체결학습] {' | '.join(realized_notes)} → 목표리스크 {risk_pct*100:.2f}%")
    if timing_risk_mult < 1.0:
        risk_pct *= timing_risk_mult
        risk_notes.append(timing_risk_note)
    if quality_notes:
        risk_notes.extend(quality_notes)
    if realized_notes:
        risk_notes.extend(realized_notes)
    if profit_surge_guard_notes:
        risk_notes.extend(profit_surge_guard_notes)
    if btc_macro_notes:
        risk_notes.extend([n for n in btc_macro_notes if n])
    if btc_macro_reference_note:
        risk_notes.append(btc_macro_reference_note)
    if leverage_notes:
        risk_notes.extend(leverage_notes)
    if asym_notes:
        for note in asym_notes:
            if note not in risk_notes:
                risk_notes.append(note)
    if ema_neutral_mtf_override:
        risk_notes.append(f"EMA 중립이나 MTF/확인수/볼륨 우위로 리스크×{EMA_NEUTRAL_MTF_RISK_MULT:.2f}")
    hyper_lead = best.get("hyperliquid_lead") or {}
    if hyper_lead:
        if _hyperliquid_lead_agrees(best, direction) and not hyper_lead.get("funding_overheated"):
            risk_pct = min(risk_pct * HYPERLIQUID_LEAD_RISK_MULT, MAX_ACCOUNT_RISK_PCT)
            note = (
                f"전략5 HL선행수급 방향일치 리스크×{HYPERLIQUID_LEAD_RISK_MULT:.2f} "
                f"(15m {float(hyper_lead.get('ret_15m_pct', 0) or 0):+.2f}%, "
                f"1h {float(hyper_lead.get('ret_1h_pct', 0) or 0):+.2f}%, "
                f"VOL {float(hyper_lead.get('vol_ratio', 0) or 0):.2f}x)"
            )
            risk_notes.append(note)
            print(f"  [전략5] {note} → 목표리스크 {risk_pct*100:.2f}%")
        elif hyper_lead.get("funding_overheated"):
            risk_notes.append("전략5 HL선행수급 감지, 펀딩 과열로 가산점 제외")
        else:
            risk_notes.append("전략5 HL선행수급 감지, 진입 방향 불일치로 가산점 제외")

    # SL 기준 계좌 위험률로 증거금 비율 역산
    position_pct, est_sl_loss = position_pct_for_risk(
        balance_now, leverage, current_price, t["sl"], risk_pct, max_position_pct
    )
    if position_pct <= 0:
        _block("리스크 기반 수량 계산 실패", send_diag=True, risk_pct=risk_pct)
        return

    max_m_final  = get_margin_cap(balance_now, scalp=scalp)
    target_margin, conviction_tier, conviction_notes = _conviction_margin_target(
        balance_now, raw, is_golden, mtf_boost, ema_aligned,
        quality_leverage_notes, roi_metrics, strategy,
    )
    if btc_macro_short:
        btc_target = max(
            float(BTC_MACRO_SHORT_MARGIN_USD),
            balance_now * float(BTC_MACRO_SHORT_MARGIN_PCT),
        )
        btc_target = min(btc_target, balance_now * MIN_TRADE_MARGIN_MAX_BALANCE_PCT)
        if btc_target > target_margin:
            conviction_tier = "BTC-MACRO"
            target_margin = round(btc_target, 2)
            conviction_notes.append(
                f"BTC 월봉 숏 전략 목표증거금 ${target_margin:.2f} "
                f"(고정 ${BTC_MACRO_SHORT_MARGIN_USD:.0f} 또는 잔고 {BTC_MACRO_SHORT_MARGIN_PCT*100:.0f}%)"
            )
    current_margin_before_boost = min(balance_now * position_pct, max_m_final)
    boost_allowed, boost_block_reason = _sizing_boost_allowed(
        conviction_tier, best_rr, tp1_rr
    )
    if not boost_allowed:
        if current_margin_before_boost + 1e-9 < MIN_TRADE_MARGIN_USD:
            _block(
                f"{boost_block_reason} — 계획증거금 ${current_margin_before_boost:.2f} "
                f"< 최소 ${MIN_TRADE_MARGIN_USD:.2f}; 저손익비 자리는 $20 강제진입 금지",
                send_diag=True,
                tp1_rr=tp1_rr,
                best_rr=best_rr,
            )
            return
        target_margin = current_margin_before_boost
        conviction_notes = [
            f"{boost_block_reason} — 현재 리스크 산출 증거금 ${current_margin_before_boost:.2f}만 사용"
        ]
    high_opportunity = _is_high_profit_opportunity(
        raw, conviction_tier, roi_metrics, quality_leverage_notes, tp1_rr
    )
    state_now = _load_state()
    daily_limit = get_daily_loss_limit(balance_now)
    daily_loss = float(state_now.get("daily_loss", 0) or 0)
    high_opportunity_block = (
        _risk_off_high_opportunity_reason(state_now)
        if high_opportunity else ""
    )
    if high_opportunity_block:
        high_opportunity = False
        risk_notes.append(high_opportunity_block)
        print(f"  [리스크오프] {high_opportunity_block}")
    opportunity_risk_cap = _opportunity_risk_cap(balance_now)
    if profit_surge:
        best["profit_surge_mode"] = True
        if profit_surge_stop_notes:
            risk_notes.extend(profit_surge_stop_notes)
        surge_risk_cap = _profit_surge_risk_cap(balance_now)
        if surge_risk_cap > opportunity_risk_cap:
            risk_notes.append(
                f"초고수익률 시드극대화: 계좌위험 하드캡 "
                f"${opportunity_risk_cap:.2f}→${surge_risk_cap:.2f}"
            )
            opportunity_risk_cap = surge_risk_cap

        new_leverage, compressed_roi_metrics, surge_notes = _compress_leverage_for_profit_surge(
            leverage, t["entry"], direction, t["tps"], t["sl_pct"],
            target_margin, balance_now, max_m_final, opportunity_risk_cap,
        )
        if surge_notes:
            leverage = new_leverage
            roi_metrics = compressed_roi_metrics
            position_pct, est_sl_loss = position_pct_for_risk(
                balance_now, leverage, current_price, t["sl"], risk_pct, max_position_pct
            )
            if position_pct <= 0:
                _block("초고수익률 레버리지 압축 후 수량 계산 실패", send_diag=True)
                return
            current_margin_before_boost = min(balance_now * position_pct, max_m_final)
            high_opportunity = _is_high_profit_opportunity(
                raw, conviction_tier, roi_metrics, quality_leverage_notes, tp1_rr
            )
            best["profit_surge_leverage"] = leverage
            for note in surge_notes:
                risk_notes.append(note)
                print(f"  [수익극대화] {note}")
    if conviction_notes:
        risk_notes.extend(conviction_notes)
        for note in conviction_notes:
            print(f"  [확신도시드] {note}")
    if high_opportunity:
        risk_notes.append(
            f"고기대수익 기회: 일손실은 소프트캡, 계좌위험 하드캡 ${opportunity_risk_cap:.2f}"
        )
        print(
            f"  [기회예외] 기대ROI {roi_metrics['weighted_margin_roi_pct']:+.1f}% "
            f"/ TP1 {roi_metrics['tp1_margin_roi_pct']:+.1f}% → 차단보다 축소진입 우선"
        )

    remaining_daily_risk = max(daily_limit - daily_loss, 0)
    if est_sl_loss > remaining_daily_risk:
        if high_opportunity:
            if opportunity_risk_cap <= 0:
                _block("고기대수익 예외 불가 — 계좌위험 하드캡 계산 실패", send_diag=True)
                return
            if est_sl_loss > opportunity_risk_cap:
                scale = opportunity_risk_cap / est_sl_loss * 0.95
                position_pct = round(position_pct * scale, 4)
                est_sl_loss = round(est_sl_loss * scale, 4)
                print(
                    f"  [기회축소] SL위험 ${est_sl_loss:.2f}로 계좌위험 하드캡 "
                    f"${opportunity_risk_cap:.2f} 내 조정"
                )
            else:
                print(
                    f"  [기회예외] 남은 일손실한도 ${remaining_daily_risk:.2f} 초과지만 "
                    f"계좌위험 하드캡 ${opportunity_risk_cap:.2f} 내 유지"
                )
        elif remaining_daily_risk <= 0:
            _block(f"일손실 한도 소진 ${daily_loss:.2f} / ${daily_limit:.2f}", send_diag=True)
            return
        else:
            scale = remaining_daily_risk / est_sl_loss * 0.9
            position_pct = round(position_pct * scale, 4)
            est_sl_loss = round(est_sl_loss * scale, 4)
            print(f"  [리스크] 남은 일손실 한도 ${remaining_daily_risk:.2f} → 포지션 {position_pct*100:.1f}%로 조정")

    boosted_pct, boosted_loss, margin_notes, margin_block = _apply_min_trade_margin(
        balance_now, position_pct, leverage, current_price, t["sl"], max_m_final,
        remaining_daily_risk,
        target_margin_usd=target_margin,
        label=f"{conviction_tier} 목표증거금",
        allow_opportunity_override=high_opportunity,
        opportunity_risk_cap_usd=opportunity_risk_cap,
    )
    if margin_block:
        _block(margin_block, send_diag=True)
        return
    if margin_notes:
        position_pct = boosted_pct
        est_sl_loss = boosted_loss
        risk_notes.extend(margin_notes)
        for note in margin_notes:
            print(f"  [시드상향] {note}  |  예상 SL손실 ${est_sl_loss:.2f}")

    position_pct, est_sl_loss, portfolio_notes, portfolio_block = _apply_portfolio_capacity_gate(
        balance_now, position_pct, est_sl_loss, max_m_final, direction,
        high_opportunity=high_opportunity,
        label=f"{strategy} 포트폴리오 용량",
        min_execution_margin_usd=(MIN_FALLBACK_TRADE_MARGIN_USD if high_opportunity else MIN_TRADE_MARGIN_USD),
    )
    if portfolio_block:
        _block(portfolio_block, send_diag=True)
        return
    if portfolio_notes:
        risk_notes.extend(portfolio_notes)
        for note in portfolio_notes:
            print(f"  [포트폴리오] {note}")

    pct_label = f"{position_pct*100:.0f}%"

    # 실제 예상 SL 손실액 (황금 진입도 표시용으로 계산)
    est_margin_f = min(balance_now * position_pct, max_m_final)
    est_sl_loss  = min(est_sl_loss, est_margin_f * leverage * (t["sl_pct"] / 100 + ROUND_TRIP_FEE))

    # 5m 스캘핑: 단일 TP로 강제 (빠른 확정, 보유 없음)
    if tf_key == "5m" and len(t["tps"]) > 1:
        tps = [{"price": t["tps"][0]["price"], "pct": 100}]
        print("  [스캘핑] 5분봉 → 단일 TP 강제 (빠른 확정)")
    else:
        tps = [{"price": tp["price"], "pct": tp["pct"]} for tp in t["tps"]]

    golden_tag = " 💰황금진입" if is_golden else ""
    print(f"  [베팅{golden_tag}] {raw} → 잔고의 {pct_label}  목표리스크 {risk_pct*100:.1f}%  SL위험 ~${est_sl_loss:.1f}")

    entry_context = _build_entry_context(
        best, tf_key, direction, strategy, timing,
        mtf_boost, ema_aligned, is_golden,
        best_rr, tp1_rr, risk_notes,
    )
    if t.get("fast_exit"):
        entry_context["reasons"].append("15m 추가전략 빠른 TP + 0.8R 수익보호 모드")
    entry_context["reasons"].append(
        f"기대 증거금ROI {roi_metrics['weighted_margin_roi_pct']:+.1f}% "
        f"(TP1 {roi_metrics['tp1_margin_roi_pct']:+.1f}%)"
    )
    entry_context.update({
        "entry_price": current_price,
        "sl": t["sl"],
        "sl_pct": t["sl_pct"],
        "tps": tps,
        "risk_pct": risk_pct,
        "position_pct": position_pct,
        "est_sl_loss": est_sl_loss,
        "leverage": leverage,
        "fast_exit": bool(t.get("fast_exit")),
        "expected_margin_roi_pct": roi_metrics["weighted_margin_roi_pct"],
        "tp1_margin_roi_pct": roi_metrics["tp1_margin_roi_pct"],
        "conviction_tier": conviction_tier,
        "target_margin_usd": target_margin,
    })
    if btc_macro_short:
        entry_context.update({
            "btc_macro_short_mode": True,
            "btc_macro_short_score": btc_macro.get("score", 0),
            "btc_macro_short_checks": btc_macro.get("checks", []),
            "btc_macro_short_note": btc_macro.get("note", ""),
            "btc_macro_short_snapshot": {
                "monthly_close": btc_macro.get("monthly_close", 0),
                "monthly_open": btc_macro.get("monthly_open", 0),
                "monthly_sma6": btc_macro.get("monthly_sma6", 0),
                "monthly_sma12": btc_macro.get("monthly_sma12", 0),
                "daily_sma200": btc_macro.get("daily_sma200", 0),
            },
        })
    elif btc_macro_reference_note:
        entry_context.update({
            "btc_macro_reference_only": True,
            "btc_macro_short_score": btc_macro.get("score", 0),
            "btc_macro_short_checks": btc_macro.get("checks", []),
            "btc_macro_short_note": btc_macro.get("note", ""),
        })

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
        min_margin_usd = (MIN_FALLBACK_TRADE_MARGIN_USD if high_opportunity else MIN_TRADE_MARGIN_USD),
        allow_pause_override = high_opportunity,
        pause_override_reason = (
            f"{tf_key} {raw} 고기대수익 기회"
            if high_opportunity else f"{tf_key} {raw} MTF 전정렬"
        ),
    )

    if result["ok"]:
        # 거래 이력 기록
        max_m    = max_m_final
        margin_r = min(balance_now * position_pct, max_m)
        trade_num = _append_trade(
            symbol, direction, tf_key, strength,
            result["leverage"], result["qty"],
            current_price, t["sl"], margin_r,
            tps=tps,
            entry_reasons=entry_context["reasons"],
            entry_context=entry_context,
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=entry_context["asymmetric_mode"],
        )
        # 분석용 컨텍스트 추가 기록 (패인 분석에 사용)
        add_trade_context(
            trade_num,
            ema_trend       = best.get("ema_trend", 0),
            confirmed_count = best["confirmed_count"],
            divergence_count = entry_context["divergence_count"],
            vol_ratio       = best["vol"]["value"],
            bars_ago        = best.get("bars_ago", 0),
            sl_pct          = t["sl_pct"],
            risk_pct        = risk_pct,
            est_sl_loss     = est_sl_loss,
            strategy        = strategy,
            signal_type     = best.get("signal_type", ""),
            is_divergence   = entry_context["is_divergence"],
            fast_exit       = entry_context["fast_exit"],
            best_rr         = best_rr,
            tp1_rr          = tp1_rr,
            strategy_family = entry_context["strategy_family"],
            core_strategy   = entry_context["core_strategy"],
            strategy_mode   = entry_context["strategy_mode"],
            asymmetric_mode = entry_context["asymmetric_mode"],
            btc_macro_short_mode = bool(entry_context.get("btc_macro_short_mode", False)),
            btc_macro_short_score = entry_context.get("btc_macro_short_score", 0),
            btc_macro_short_note = entry_context.get("btc_macro_short_note", ""),
        )
        log_trade_candidate(
            symbol, tf_key, strategy, direction, strength, "opened",
            price=current_price, leverage=result["leverage"], qty=result["qty"],
            position_pct=position_pct, risk_pct=risk_pct,
            est_sl_loss=est_sl_loss, rr=best_rr,
            sl=t["sl"], sl_pct=t["sl_pct"], tps=tps,
            entry_reasons=entry_context["reasons"],
            entry_context=entry_context,
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=entry_context["asymmetric_mode"],
            btc_macro_short_mode=bool(entry_context.get("btc_macro_short_mode", False)),
            btc_macro_short_score=entry_context.get("btc_macro_short_score", 0),
        )
        log_execution_journal(
            trade_num, "opened",
            symbol=symbol, tf=tf_key, strategy=strategy,
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=entry_context["asymmetric_mode"],
            direction=direction, strength=strength,
            signal_type=best.get("signal_type", ""),
            entry_price=current_price, sl=t["sl"], tps=tps,
            leverage=result["leverage"], qty=result["qty"],
            margin=margin_r, balance=balance_now,
            risk_pct=risk_pct, position_pct=position_pct,
            est_sl_loss=est_sl_loss,
            btc_macro_short_mode=bool(entry_context.get("btc_macro_short_mode", False)),
            btc_macro_short_score=entry_context.get("btc_macro_short_score", 0),
            btc_macro_short_note=entry_context.get("btc_macro_short_note", ""),
            entry_context=entry_context,
        )
        scalp_tag = " [스캘핑]" if scalp else ""
        notif     = build_trade_notification(
            symbol, direction, result["leverage"],
            result["qty"], current_price, t["sl"], tps, balance_now,
            tf_key=tf_key, strength=strength, strategy=strategy,
            trade_num=trade_num, reasons=entry_context["reasons"],
            timing_note=entry_context["timing_note"],
            rr=best_rr, risk_pct=risk_pct, est_sl_loss=est_sl_loss,
            sl_pct=t["sl_pct"], signal_type=best.get("signal_type", ""),
            confirmed_count=best["confirmed_count"],
            divergence_count=entry_context["divergence_count"],
            is_divergence=entry_context["is_divergence"],
            entry_context=entry_context,
        )
        analysis_sent = send_position_analysis(notif)
        send(_build_open_summary(
            trade_num, symbol, direction, tf_key, strategy, strength,
            result["leverage"], result["qty"], current_price, t["sl"], tps,
            est_sl_loss, analysis_sent, entry_context["strategy_profile"],
        ))
        print(f"  [자동매매{scalp_tag}] ✅ {trade_num}회차 기록 ({pct_label}) — 텔레그램 분리 발송")
    else:
        print(f"  [자동매매] ❌ 주문 실패: {result['error']}")
        log_trade_candidate(
            symbol, tf_key, strategy, direction, strength, "order_failed",
            result["error"], price=current_price, leverage=result["leverage"],
            position_pct=position_pct, risk_pct=risk_pct,
        )
        if AUTO_TRADE_DIAGNOSTICS:
            err = escape(str(result["error"]))
            send_signal(
                f"❌ <b>[자동매매 주문 실패]</b> {symbol.split('/')[0]} {tf_key} {direction}\n"
                f"강도: <b>{escape(str(strength))}</b>\n"
                f"사유: {err}"
            )


def _log_gate_block(symbol: str, tf_key: str, signal: dict,
                    direction: str, reason: str, strategy: str = "게이트"):
    """_try_auto_trade 전 단계에서 차단된 후보도 학습용으로 남긴다."""
    if not AUTO_TRADE:
        return
    try:
        from trade_router import log_trade_candidate
        strategy_name = signal.get("strategy", strategy)
        profile = classify_strategy(
            strategy_name,
            signal.get("signal_type", ""),
            signal.get("is_divergence", True),
            direction,
            asymmetric=signal.get("asymmetric_mode", False),
        )
        log_trade_candidate(
            symbol, tf_key, strategy_name, direction,
            signal.get("strength", ""), "blocked", reason,
            signal_type=signal.get("signal_type", ""),
            price=signal.get("current_price", signal.get("pivot_price", 0)),
            confirmed_count=signal.get("confirmed_count", 0),
            bars_ago=signal.get("bars_ago", 0),
            vol_ratio=signal.get("vol", {}).get("value", 0),
            ema_trend=signal.get("ema_trend", 0),
            strategy_family=profile["family_label"],
            core_strategy=profile["strategy_label"],
            strategy_mode=profile["family_key"],
            asymmetric_mode=bool(signal.get("asymmetric_mode", False)),
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
    from trade_router import (execute, get_usdt_balance, build_trade_notification,
                        _append_trade, add_trade_context, MAX_MARGIN_USD,
                        MAX_DAILY_LOSS,
                        has_open_position, get_daily_loss_limit, get_margin_cap,
                        log_trade_candidate, log_execution_journal, notify_trade_block,
                        position_pct_for_risk, _load_state)
    from config import TP_BY_STRENGTH, SL_ATR_MULT

    # 이미 포지션 있으면 스킵
    if has_open_position(symbol):
        notify_trade_block(symbol, tf_key, bsig["direction"], bsig["strength"],
                           "이미 오픈 포지션 있음", strategy="돌파")
        return

    # 고정 동시 포지션 개수 제한은 쓰지 않는다.
    # 돌파도 최종 사이징 후 포트폴리오 증거금/SL위험으로 허용 여부를 판단한다.

    balance_now = get_usdt_balance()
    state_now = _load_state()
    daily_loss  = float(state_now.get("daily_loss", 0) or 0)
    daily_limit = get_daily_loss_limit(balance_now)
    if daily_loss >= daily_limit:
        print(
            f"  [돌파리스크] 일일 손실한도 도달 ${daily_loss:.2f}/${daily_limit:.2f} — "
            "ROI/확신도 확인 후 고기대수익 예외 여부 판단"
        )

    direction = bsig["direction"]
    atr       = bsig["atr"]
    strength  = bsig["strength"]
    raw_strength = _raw_strength(strength)
    btc_macro = _btc_macro_short_bias() if symbol == BTC_MACRO_SHORT_SYMBOL else {"active": False}
    btc_macro_reference = bool(btc_macro.get("active"))
    btc_macro_reference_note = ""
    if btc_macro_reference and BTC_MACRO_TREND_REFERENCE_ONLY:
        btc_macro_reference_note = (
            "BTC 장기봉 참고만 적용: 월봉/주봉은 배경 추세 메모이며 "
            f"돌파 진입·시드확대·롱차단에는 미사용 ({btc_macro.get('note', '')})"
        )
    btc_macro_short = bool(
        btc_macro_reference
        and direction == "SHORT"
        and not BTC_MACRO_TREND_REFERENCE_ONLY
    )
    trade_strategy = "BTC Macro Short" if btc_macro_short else "돌파"

    # 돌파 경로: 전략 화이트리스트 + SHORT 차단 (순수 "돌파" 0% 승률, BTC Macro Short 0% 승률)
    if AUTO_TRADE_STRATEGY_WHITELIST and trade_strategy not in AUTO_TRADE_STRATEGY_WHITELIST:
        notify_trade_block(
            symbol, tf_key, direction, strength,
            f"{trade_strategy} — 화이트리스트 미포함, EMA 계열만 실거래 허용",
            strategy=trade_strategy,
            send_telegram=False,
        )
        return
    if BLOCK_SHORT_AUTO_TRADE and direction == "SHORT":
        notify_trade_block(
            symbol, tf_key, direction, strength,
            "SHORT 실거래 임시 차단 — EMA LONG 전략 집중",
            strategy=trade_strategy,
            send_telegram=False,
        )
        return

    if (
        symbol == BTC_MACRO_SHORT_SYMBOL
        and BTC_MACRO_SHORT_BLOCK_LONG
        and not BTC_MACRO_TREND_REFERENCE_ONLY
        and btc_macro.get("active")
        and direction == "LONG"
    ):
        notify_trade_block(
            symbol, tf_key, direction, strength,
            "BTC 월봉 숏 전용 모드 — 롱 돌파는 후보 기록만 "
            f"({btc_macro.get('note', '')})",
            strategy="BTC Macro Short", send_telegram=AUTO_TRADE_DIAGNOSTICS,
            signal_type="btc_macro_short_block_long",
            is_divergence=False,
            btc_macro_short_score=btc_macro.get("score", 0),
            btc_macro_short_note=btc_macro.get("note", ""),
        )
        return
    breakout_asym = _live_asymmetric_candidate(bsig, tf_key, "돌파")
    repeat_profile = classify_strategy(
        trade_strategy,
        bsig.get("signal_type", "breakout"),
        False,
        direction,
        asymmetric=breakout_asym,
    )
    repeat_limit = (
        ASYMMETRIC_SYMBOL_DAILY_LOSS_LIMIT
        if repeat_profile["family_key"] == "asymmetric_edge"
        else SYMBOL_STRATEGY_DAILY_LOSS_LIMIT
    )

    # 돌파 경로: 심볼 당일 총 손실 한도 + 쿨다운 확인
    if SYMBOL_DAILY_TOTAL_LOSS_LIMIT > 0:
        total_sym_losses_bt = _today_loss_count(state_now, symbol=symbol)
        if total_sym_losses_bt >= SYMBOL_DAILY_TOTAL_LOSS_LIMIT:
            notify_trade_block(
                symbol, tf_key, direction, strength,
                f"오늘 {symbol} 총 손실 {total_sym_losses_bt}회 >= 한도 {SYMBOL_DAILY_TOTAL_LOSS_LIMIT}회 — 당일 재진입 차단",
                strategy=trade_strategy,
                send_telegram=AUTO_TRADE_DIAGNOSTICS,
            )
            return
        if symbol in get_cooldown_symbols():
            notify_trade_block(
                symbol, tf_key, direction, strength,
                f"{symbol} 연패 쿨다운 중 — 돌파 재진입 보류",
                strategy=trade_strategy,
                send_telegram=AUTO_TRADE_DIAGNOSTICS,
            )
            return

    repeat_losses = _today_loss_count(
        state_now,
        symbol=symbol,
        strategy_mode=repeat_profile["family_key"],
    )
    if repeat_limit > 0 and repeat_losses >= repeat_limit:
        notify_trade_block(
            symbol, tf_key, direction, strength,
            f"오늘 {symbol} {repeat_profile['family_label']} 손실 {repeat_losses}회 "
            f">= 한도 {repeat_limit}회 — 같은 돌파 조합 반복진입 차단",
            strategy=trade_strategy,
            send_telegram=AUTO_TRADE_DIAGNOSTICS,
            strategy_mode=repeat_profile["family_key"],
            core_strategy=repeat_profile["strategy_label"],
        )
        return
    asym_timing_mult = 1.0
    timing_risk_note = ""
    ok_strategy, why_strategy = is_tradeable_with_strategy(
        symbol, tf_key, trade_strategy, direction,
        signal_type=bsig.get("signal_type", "breakout"),
        is_divergence=False,
        asymmetric=breakout_asym,
    )
    if not ok_strategy:
        notify_trade_block(symbol, tf_key, direction, strength, why_strategy,
                           strategy=trade_strategy, send_telegram=AUTO_TRADE_DIAGNOSTICS)
        return
    timing = _check_lower_tf_timing(symbol, tf_key, direction)
    if not timing["ok"]:
        high_conviction_breakout = (
            raw_strength in {"VERY STRONG", "ELITE"}
            and "VWAP OK" in timing["note"]
            and "EMA OK" in timing["note"]
        )
        if breakout_asym or high_conviction_breakout:
            asym_timing_mult = ASYMMETRIC_TIMING_OVERRIDE_MULT
            label = "초고거래량 돌파" if breakout_asym else "고확신 돌파"
            timing_risk_note = f"{label} 보조봉 미일치 리스크×{asym_timing_mult:.2f}"
            print(
                f"  [돌파보조완화] {label} → {timing['note']} "
                f"차단 대신 리스크×{ASYMMETRIC_TIMING_OVERRIDE_MULT:.2f}"
            )
        else:
            notify_trade_block(symbol, tf_key, direction, strength,
                               f"{timing['tf']} 보조봉 확인 불일치 — {timing['note']}",
                               strategy=trade_strategy)
            return
    if timing["tf"]:
        print(f"  [돌파보조확인] {timing['note']}")

    # 돌파 SL: 돌파 레벨 바로 밖 1.0 ATR (추세를 못 이어가면 즉시 철수)
    sl_mult   = 1.0
    sl        = (current_price - atr * sl_mult) if direction == "LONG" else (current_price + atr * sl_mult)

    # TP: VERY STRONG 기준 (4개 조건 확인 = VERY STRONG 수준)
    tp_plan = (
        ASYMMETRIC_TP_BY_STRENGTH.get("VERY STRONG", TP_BY_STRENGTH["VERY STRONG"])
        if breakout_asym else
        TP_BY_STRENGTH.get("VERY STRONG", TP_BY_STRENGTH["VERY STRONG"])
    )
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
                           strategy=trade_strategy, tp1_rr=tps[0]["rr"])
        return

    # 포지션 크기: 복리 리스크 엔진 적용
    from config import MARGIN_BY_STRENGTH, LEVERAGE_MAP
    position_cap = MARGIN_BY_STRENGTH.get(raw_strength, MARGIN_BY_STRENGTH.get("VERY STRONG", 0.25))
    leverage     = LEVERAGE_MAP.get((raw_strength, tf_key), 7)
    leverage_notes = []
    leverage, leverage_notes = get_quality_leverage_adjustment(
        symbol, tf_key, trade_strategy, direction, leverage
    )
    quality_leverage_notes = list(leverage_notes)
    for note in leverage_notes:
        print(f"  [레버리지학습] {note}")
    if btc_macro_short:
        old_lev = leverage
        leverage = min(
            int(math.ceil(leverage * float(BTC_MACRO_SHORT_LEVERAGE_MULT))),
            int(BTC_MACRO_SHORT_MAX_LEVERAGE),
        )
        if leverage > old_lev:
            note = (
                f"BTC 월봉 숏 돌파 레버리지 {old_lev}x→{leverage}x "
                f"(숏점수 {btc_macro.get('score', 0)}/7)"
            )
            leverage_notes.append(note)
            quality_leverage_notes.append(note)
            print(f"  [BTC월봉숏] {note}")

    leverage, roi_metrics, roi_leverage_notes = _raise_leverage_for_roi(
        current_price, direction, tps, leverage
    )
    if roi_leverage_notes:
        leverage_notes.extend(roi_leverage_notes)
        for note in roi_leverage_notes:
            print(f"  [ROI레버리지] {note}")
    if btc_macro_short and leverage > BTC_MACRO_SHORT_MAX_LEVERAGE:
        leverage = int(BTC_MACRO_SHORT_MAX_LEVERAGE)
        roi_metrics = _planned_roi_metrics(current_price, direction, tps, leverage)
        note = f"BTC 월봉 숏 돌파 레버리지 상한 {BTC_MACRO_SHORT_MAX_LEVERAGE}x 적용"
        leverage_notes.append(note)
        print(f"  [BTC월봉숏] {note}")

    roi_reason, roi_metrics = _roi_gate_reason(current_price, direction, tps, leverage)
    if roi_reason:
        notify_trade_block(
            symbol, tf_key, direction, strength, roi_reason,
            strategy=trade_strategy, send_telegram=AUTO_TRADE_DIAGNOSTICS,
            expected_margin_roi=roi_metrics["weighted_margin_roi_pct"],
        )
        return

    sl_pct = abs(current_price - sl) / current_price * 100
    risk_pct = RISK_PCT_BY_STRENGTH.get(raw_strength, RISK_PCT_BY_STRENGTH["VERY STRONG"])
    quality_mult, quality_notes = get_signal_quality_adjustment(
        symbol, tf_key, trade_strategy, direction
    )
    if quality_mult <= 0:
        notify_trade_block(symbol, tf_key, direction, strength,
                           "후보 사후승률 음수 기대값 — " + " | ".join(quality_notes),
                           strategy=trade_strategy, send_telegram=AUTO_TRADE_DIAGNOSTICS)
        return
    realized_mult, realized_notes = get_realized_trade_adjustment(
        symbol, tf_key, trade_strategy, direction,
        signal_type=bsig.get("signal_type", "breakout"),
        is_divergence=False,
        asymmetric=breakout_asym,
    )
    if realized_mult <= 0:
        notify_trade_block(symbol, tf_key, direction, strength,
                           "실체결 손익학습 차단 — " + " | ".join(realized_notes),
                           strategy=trade_strategy, send_telegram=AUTO_TRADE_DIAGNOSTICS)
        return
    risk_mult, risk_notes = get_risk_multiplier(tf_key, trade_strategy, symbol)
    if risk_mult < 1.0:
        risk_pct *= risk_mult
        print(f"  [리스크거버너] {' | '.join(risk_notes)} → 돌파 목표리스크 {risk_pct*100:.2f}%")
    if quality_mult != 1.0:
        risk_pct = min(risk_pct * quality_mult, MAX_ACCOUNT_RISK_PCT)
        print(f"  [후보사후평가] {' | '.join(quality_notes)} → 돌파 목표리스크 {risk_pct*100:.2f}%")
    if realized_mult != 1.0:
        risk_pct = min(risk_pct * realized_mult, MAX_ACCOUNT_RISK_PCT)
        print(f"  [실체결학습] {' | '.join(realized_notes)} → 돌파 목표리스크 {risk_pct*100:.2f}%")
    if quality_notes:
        risk_notes.extend(quality_notes)
    if realized_notes:
        risk_notes.extend(realized_notes)
    if btc_macro_reference_note:
        risk_notes.append(btc_macro_reference_note)
    if leverage_notes:
        risk_notes.extend(leverage_notes)
    if asym_timing_mult < 1.0:
        risk_pct *= asym_timing_mult
        risk_notes.append(timing_risk_note or f"돌파 보조봉 미일치 리스크×{asym_timing_mult:.2f}")
        print(f"  [돌파비대칭감액] 목표리스크 {risk_pct*100:.2f}%")
    if breakout_asym:
        risk_notes.append("초고거래량 비대칭 돌파 — 러너형 TP 적용")
    if btc_macro_short:
        old_risk = risk_pct
        old_cap = position_cap
        risk_pct = min(
            risk_pct * float(BTC_MACRO_SHORT_RISK_MULT),
            float(BTC_MACRO_SHORT_MAX_ACCOUNT_RISK_PCT),
            MAX_ACCOUNT_RISK_PCT,
        )
        position_cap = min(
            max(position_cap, float(BTC_MACRO_SHORT_POSITION_CAP)),
            MIN_TRADE_MARGIN_MAX_BALANCE_PCT,
        )
        risk_notes.append(
            f"BTC 월봉 숏 돌파 우대: 리스크 {old_risk*100:.2f}%→{risk_pct*100:.2f}%, "
            f"포지션캡 {old_cap*100:.0f}%→{position_cap*100:.0f}%"
        )
        risk_notes.append(btc_macro.get("note", ""))
        print(f"  [BTC월봉숏] 돌파 우대 적용 → 목표리스크 {risk_pct*100:.2f}%")
    hyper_lead = bsig.get("hyperliquid_lead") or {}
    if hyper_lead:
        if _hyperliquid_lead_agrees(bsig, direction) and not hyper_lead.get("funding_overheated"):
            risk_pct = min(risk_pct * HYPERLIQUID_LEAD_RISK_MULT, MAX_ACCOUNT_RISK_PCT)
            note = (
                f"전략5 HL선행수급 돌파 방향일치 리스크×{HYPERLIQUID_LEAD_RISK_MULT:.2f} "
                f"(15m {float(hyper_lead.get('ret_15m_pct', 0) or 0):+.2f}%, "
                f"1h {float(hyper_lead.get('ret_1h_pct', 0) or 0):+.2f}%, "
                f"VOL {float(hyper_lead.get('vol_ratio', 0) or 0):.2f}x)"
            )
            risk_notes.append(note)
            print(f"  [전략5] {note} → 돌파 목표리스크 {risk_pct*100:.2f}%")
        elif hyper_lead.get("funding_overheated"):
            risk_notes.append("전략5 HL선행수급 감지, 펀딩 과열로 돌파 가산점 제외")
        else:
            risk_notes.append("전략5 HL선행수급 감지, 돌파 방향 불일치로 가산점 제외")
    position_pct, est_sl_loss = position_pct_for_risk(
        balance_now, leverage, current_price, sl, risk_pct, position_cap
    )
    if position_pct <= 0:
        notify_trade_block(symbol, tf_key, direction, strength,
                           "리스크 기반 수량 계산 실패", strategy=trade_strategy,
                           send_telegram=AUTO_TRADE_DIAGNOSTICS)
        return

    max_margin = get_margin_cap(balance_now, scalp=False)
    best_rr = max(tp["rr"] for tp in tps)
    tp1_rr = tps[0]["rr"] if tps else 0
    target_margin, conviction_tier, conviction_notes = _conviction_margin_target(
        balance_now, raw_strength, False, 1.0, True,
        quality_leverage_notes, roi_metrics, trade_strategy,
    )
    if btc_macro_short:
        btc_target = max(
            float(BTC_MACRO_SHORT_MARGIN_USD),
            balance_now * float(BTC_MACRO_SHORT_MARGIN_PCT),
        )
        btc_target = min(btc_target, balance_now * MIN_TRADE_MARGIN_MAX_BALANCE_PCT)
        if btc_target > target_margin:
            conviction_tier = "BTC-MACRO"
            target_margin = round(btc_target, 2)
            conviction_notes.append(
                f"BTC 월봉 숏 돌파 목표증거금 ${target_margin:.2f}"
            )
    current_margin_before_boost = min(balance_now * position_pct, max_margin)
    boost_allowed, boost_block_reason = _sizing_boost_allowed(
        conviction_tier, best_rr, tp1_rr
    )
    if not boost_allowed:
        if current_margin_before_boost + 1e-9 < MIN_TRADE_MARGIN_USD:
            notify_trade_block(
                symbol, tf_key, direction, strength,
                f"{boost_block_reason} — 계획증거금 ${current_margin_before_boost:.2f} "
                f"< 최소 ${MIN_TRADE_MARGIN_USD:.2f}; 저손익비 돌파는 $20 강제진입 금지",
                strategy=trade_strategy, send_telegram=AUTO_TRADE_DIAGNOSTICS,
                tp1_rr=tp1_rr, best_rr=best_rr,
            )
            return
        target_margin = current_margin_before_boost
        conviction_notes = [
            f"{boost_block_reason} — 현재 리스크 산출 증거금 ${current_margin_before_boost:.2f}만 사용"
        ]
    high_opportunity = _is_high_profit_opportunity(
        raw_strength, conviction_tier, roi_metrics, quality_leverage_notes, tp1_rr
    )
    high_opportunity_block = (
        _risk_off_high_opportunity_reason(state_now)
        if high_opportunity else ""
    )
    if high_opportunity_block:
        high_opportunity = False
        risk_notes.append(high_opportunity_block)
        print(f"  [리스크오프] {high_opportunity_block}")
    opportunity_risk_cap = _opportunity_risk_cap(balance_now)
    if conviction_notes:
        risk_notes.extend(conviction_notes)
        for note in conviction_notes:
            print(f"  [확신도시드] {note}")
    if high_opportunity:
        risk_notes.append(
            f"고기대수익 돌파: 일손실은 소프트캡, 계좌위험 하드캡 ${opportunity_risk_cap:.2f}"
        )
        print(
            f"  [기회예외] 돌파 기대ROI {roi_metrics['weighted_margin_roi_pct']:+.1f}% "
            f"/ TP1 {roi_metrics['tp1_margin_roi_pct']:+.1f}% → 차단보다 축소진입 우선"
        )

    remaining_daily_risk = max(daily_limit - daily_loss, 0)
    if est_sl_loss > remaining_daily_risk:
        if high_opportunity:
            if opportunity_risk_cap <= 0:
                notify_trade_block(
                    symbol, tf_key, direction, strength,
                    "고기대수익 돌파 예외 불가 — 계좌위험 하드캡 계산 실패",
                    strategy=trade_strategy, send_telegram=AUTO_TRADE_DIAGNOSTICS,
                )
                return
            if est_sl_loss > opportunity_risk_cap:
                scale = opportunity_risk_cap / est_sl_loss * 0.95
                position_pct = round(position_pct * scale, 4)
                est_sl_loss = round(est_sl_loss * scale, 4)
                print(
                    f"  [기회축소] 돌파 SL위험 ${est_sl_loss:.2f}로 계좌위험 하드캡 "
                    f"${opportunity_risk_cap:.2f} 내 조정"
                )
            else:
                print(
                    f"  [기회예외] 돌파 남은 일손실한도 ${remaining_daily_risk:.2f} 초과지만 "
                    f"계좌위험 하드캡 ${opportunity_risk_cap:.2f} 내 유지"
                )
        else:
            scale = remaining_daily_risk / est_sl_loss * 0.9 if est_sl_loss > 0 else 0
            if scale <= 0:
                notify_trade_block(symbol, tf_key, direction, strength,
                                   "남은 일손실 한도 없음", strategy=trade_strategy,
                                   send_telegram=AUTO_TRADE_DIAGNOSTICS)
                return
            position_pct = round(position_pct * scale, 4)
            est_sl_loss = round(est_sl_loss * scale, 4)

    boosted_pct, boosted_loss, margin_notes, margin_block = _apply_min_trade_margin(
        balance_now, position_pct, leverage, current_price, sl, max_margin,
        remaining_daily_risk,
        target_margin_usd=target_margin,
        label=f"{conviction_tier} 목표증거금",
        allow_opportunity_override=high_opportunity,
        opportunity_risk_cap_usd=opportunity_risk_cap,
    )
    if margin_block:
        notify_trade_block(
            symbol, tf_key, direction, strength, margin_block,
            strategy=trade_strategy, send_telegram=AUTO_TRADE_DIAGNOSTICS,
        )
        return
    if margin_notes:
        position_pct = boosted_pct
        est_sl_loss = boosted_loss
        risk_notes.extend(margin_notes)
        for note in margin_notes:
            print(f"  [시드상향] {note}  |  예상 SL손실 ${est_sl_loss:.2f}")

    position_pct, est_sl_loss, portfolio_notes, portfolio_block = _apply_portfolio_capacity_gate(
        balance_now, position_pct, est_sl_loss, max_margin, direction,
        high_opportunity=high_opportunity,
        label=f"{trade_strategy} 포트폴리오 용량",
        min_execution_margin_usd=(MIN_FALLBACK_TRADE_MARGIN_USD if high_opportunity else MIN_TRADE_MARGIN_USD),
    )
    if portfolio_block:
        notify_trade_block(
            symbol, tf_key, direction, strength, portfolio_block,
            strategy=trade_strategy, send_telegram=AUTO_TRADE_DIAGNOSTICS,
        )
        return
    if portfolio_notes:
        risk_notes.extend(portfolio_notes)
        for note in portfolio_notes:
            print(f"  [포트폴리오] {note}")

    print(f"  [돌파매매] {direction}  레벨:{bsig['breakout_level']:,.2f}  "
          f"vol:{bsig['vol']['value']:.1f}x  ATR확장:{bsig['atr_expand']}")
    print(f"  [돌파SL] ±{sl_pct:.1f}%  TP1 R:R {tps[0]['rr']:.1f}:1  "
          f"리스크 {risk_pct*100:.1f}%  포지션 {position_pct*100:.1f}%")

    max_rr = max(tp["rr"] for tp in tps)
    breakout_reasons = [
        f"{tf_key} 구조 레벨 {bsig['breakout_level']:,.4f} 돌파",
        f"거래량 {bsig['vol']['value']:.2f}x, ATR 확장 {bsig['atr_expand']}",
        f"돌파 실패 시 {sl_mult:.1f}ATR 밖 손절로 빠른 무효화",
        f"TP1 R:R 1:{tps[0]['rr']}, 최대 R:R 1:{max_rr}",
        f"기대 증거금ROI {roi_metrics['weighted_margin_roi_pct']:+.1f}% "
        f"(TP1 {roi_metrics['tp1_margin_roi_pct']:+.1f}%)",
    ]
    if timing.get("note"):
        breakout_reasons.append(f"보조봉 확인: {timing['note']}")
    if hyper_lead:
        breakout_reasons.append(
            "전략5 Hyperliquid 선행수급: "
            f"순위 #{hyper_lead.get('rank', '-')}, {hyper_lead.get('direction', '-')} "
            f"15m {float(hyper_lead.get('ret_15m_pct', 0) or 0):+.2f}%, "
            f"1h {float(hyper_lead.get('ret_1h_pct', 0) or 0):+.2f}%, "
            f"VOL {float(hyper_lead.get('vol_ratio', 0) or 0):.2f}x, "
            f"OI {hyper_lead.get('open_interest_label', '-')}"
        )
    if risk_notes:
        breakout_reasons.append("리스크 거버너: " + " | ".join(risk_notes))
    breakout_profile = classify_strategy(
        trade_strategy, bsig.get("signal_type", "breakout"), False, direction,
        asymmetric=breakout_asym,
    )
    breakout_reasons.insert(0, f"전략군: {format_profile(breakout_profile)}")
    entry_context = {
        "strategy": trade_strategy,
        "strategy_profile": breakout_profile,
        "strategy_family": breakout_profile["family_label"],
        "core_strategy": breakout_profile["strategy_label"],
        "strategy_mode": breakout_profile["family_key"],
        "asymmetric_mode": bool(breakout_asym),
        "signal_type": bsig.get("signal_type", "breakout"),
        "signal_label": "구조 돌파",
        "is_divergence": False,
        "direction": direction,
        "tf": tf_key,
        "reasons": breakout_reasons,
        "timing_note": timing.get("note", ""),
        "timing_tf": timing.get("tf", ""),
        "indicator_snapshot": _indicator_snapshot(bsig),
        "hyperliquid_lead": hyper_lead,
        "hyperliquid_lead_agrees": bool(hyper_lead and hyper_lead.get("direction") == direction),
        "breakout_level": bsig["breakout_level"],
        "atr_expand": bsig["atr_expand"],
        "vol_ratio": bsig["vol"]["value"],
        "confirmed_count": bsig["confirmed_count"],
        "rr": {"tp1": tps[0]["rr"], "best": max_rr},
        "entry_price": current_price,
        "sl": sl,
        "sl_pct": sl_pct,
        "tps": tps,
        "risk_pct": risk_pct,
        "position_pct": position_pct,
        "est_sl_loss": est_sl_loss,
        "leverage": leverage,
        "expected_margin_roi_pct": roi_metrics["weighted_margin_roi_pct"],
        "tp1_margin_roi_pct": roi_metrics["tp1_margin_roi_pct"],
        "conviction_tier": conviction_tier,
        "target_margin_usd": target_margin,
    }
    if btc_macro_short:
        entry_context.update({
            "btc_macro_short_mode": True,
            "btc_macro_short_score": btc_macro.get("score", 0),
            "btc_macro_short_checks": btc_macro.get("checks", []),
            "btc_macro_short_note": btc_macro.get("note", ""),
            "btc_macro_short_snapshot": {
                "monthly_close": btc_macro.get("monthly_close", 0),
                "monthly_open": btc_macro.get("monthly_open", 0),
                "monthly_sma6": btc_macro.get("monthly_sma6", 0),
                "monthly_sma12": btc_macro.get("monthly_sma12", 0),
                "daily_sma200": btc_macro.get("daily_sma200", 0),
            },
        })
    elif btc_macro_reference_note:
        entry_context.update({
            "btc_macro_reference_only": True,
            "btc_macro_short_score": btc_macro.get("score", 0),
            "btc_macro_short_checks": btc_macro.get("checks", []),
            "btc_macro_short_note": btc_macro.get("note", ""),
        })

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
        min_margin_usd = (MIN_FALLBACK_TRADE_MARGIN_USD if high_opportunity else MIN_TRADE_MARGIN_USD),
        allow_pause_override = high_opportunity,
        pause_override_reason = f"{tf_key} 돌파 고기대수익 기회",
    )

    if result["ok"]:
        margin_r = min(balance_now * position_pct, max_margin)
        trade_num = _append_trade(
            symbol, direction, tf_key, strength,
            result["leverage"], result["qty"],
            current_price, sl, margin_r,
            tps=tps,
            entry_reasons=entry_context["reasons"],
            entry_context=entry_context,
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=entry_context["asymmetric_mode"],
        )
        add_trade_context(
            trade_num,
            ema_trend       = bsig.get("ema_trend", 0),
            confirmed_count = bsig["confirmed_count"],
            divergence_count= 0,
            vol_ratio       = bsig["vol"]["value"],
            bars_ago        = 0,
            sl_pct          = sl_pct,
            risk_pct        = risk_pct,
            est_sl_loss     = est_sl_loss,
            strategy        = trade_strategy,
            signal_type     = bsig.get("signal_type", ""),
            is_divergence   = False,
            best_rr         = max_rr,
            tp1_rr          = tps[0]["rr"],
            strategy_family = entry_context["strategy_family"],
            core_strategy   = entry_context["core_strategy"],
            strategy_mode   = entry_context["strategy_mode"],
            asymmetric_mode = entry_context["asymmetric_mode"],
            btc_macro_short_mode = bool(entry_context.get("btc_macro_short_mode", False)),
            btc_macro_short_score = entry_context.get("btc_macro_short_score", 0),
            btc_macro_short_note = entry_context.get("btc_macro_short_note", ""),
        )
        log_trade_candidate(
            symbol, tf_key, trade_strategy, direction, strength, "opened",
            price=current_price, leverage=result["leverage"], qty=result["qty"],
            position_pct=position_pct, risk_pct=risk_pct,
            est_sl_loss=est_sl_loss, rr=max_rr,
            sl=sl, sl_pct=sl_pct, tps=tps,
            entry_reasons=entry_context["reasons"],
            entry_context=entry_context,
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=entry_context["asymmetric_mode"],
            btc_macro_short_mode=bool(entry_context.get("btc_macro_short_mode", False)),
            btc_macro_short_score=entry_context.get("btc_macro_short_score", 0),
        )
        log_execution_journal(
            trade_num, "opened",
            symbol=symbol, tf=tf_key, strategy=trade_strategy,
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=entry_context["asymmetric_mode"],
            direction=direction, strength=strength,
            signal_type=bsig.get("signal_type", "breakout"),
            entry_price=current_price, sl=sl, tps=tps,
            leverage=result["leverage"], qty=result["qty"],
            margin=margin_r, balance=balance_now,
            risk_pct=risk_pct, position_pct=position_pct,
            est_sl_loss=est_sl_loss,
            btc_macro_short_mode=bool(entry_context.get("btc_macro_short_mode", False)),
            btc_macro_short_score=entry_context.get("btc_macro_short_score", 0),
            btc_macro_short_note=entry_context.get("btc_macro_short_note", ""),
            entry_context=entry_context,
        )
        notif = build_trade_notification(
            symbol, direction, result["leverage"],
            result["qty"], current_price, sl, tps, balance_now,
            tf_key=tf_key, strength=strength, strategy=trade_strategy,
            trade_num=trade_num, reasons=entry_context["reasons"],
            timing_note=entry_context["timing_note"],
            rr=max_rr, risk_pct=risk_pct, est_sl_loss=est_sl_loss,
            sl_pct=sl_pct, signal_type=bsig.get("signal_type", "breakout"),
            confirmed_count=bsig["confirmed_count"],
            divergence_count=0,
            is_divergence=False,
            entry_context=entry_context,
        )
        analysis_sent = send_position_analysis(notif)
        send(_build_open_summary(
            trade_num, symbol, direction, tf_key, trade_strategy, strength,
            result["leverage"], result["qty"], current_price, sl, tps,
            est_sl_loss, analysis_sent, entry_context["strategy_profile"],
        ))
        print(f"  [돌파매매] ✅ {trade_num}회차 기록 — 텔레그램 분리 발송")
    else:
        print(f"  [돌파매매] ❌ 주문 실패: {result['error']}")
        log_trade_candidate(
            symbol, tf_key, trade_strategy, direction, strength, "order_failed",
            result["error"], price=current_price, leverage=result["leverage"],
            position_pct=position_pct, risk_pct=risk_pct,
        )
        if AUTO_TRADE_DIAGNOSTICS:
            err = escape(str(result["error"]))
            send_signal(
                f"❌ <b>[돌파 주문 실패]</b> {symbol.split('/')[0]} {tf_key} {direction}\n"
                f"강도: <b>{escape(str(strength))}</b>\n"
                f"사유: {err}"
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
    from trade_router import (execute, get_usdt_balance, add_pyramid_entry,
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
        # 불타기는 이미 +1.5~3.0 ATR 수익 중이고 EMA 추세도 재확인된 자리에서만
        # 호출된다 (호출부 조건 참고) — 당일 손실액으로 이 확인된 추세지속
        # 자리까지 막지 않는다. 계좌생존(DD/하드스톱)만 상위에서 계속 방어.
        allow_pause_override = True,
        pause_override_reason = f"불타기{pyramid_level}회 추세지속 확인",
    )

    if result["ok"]:
        margin_r = min(balance_now * position_pct, MAX_MARGIN_USD)
        add_pyramid_entry(symbol, tf_key, current_price, margin_r, result["qty"])
        notif = build_pyramid_notification(
            symbol, direction, tf_key,
            pyramid_level, current_price, margin_r, profit_atr, balance_now,
        )
        send(notif)
        print(f"  [불타기{pyramid_level}] ✅ 추가진입 완료 — 매매내역방 발송")
    else:
        print(f"  [불타기{pyramid_level}] ❌ 주문 실패: {result['error']}")
        if AUTO_TRADE_DIAGNOSTICS:
            err = escape(str(result["error"]))
            send_signal(
                f"❌ <b>[불타기 주문 실패]</b> {symbol.split('/')[0]} {tf_key} {direction}\n"
                f"단계: <b>{pyramid_level}</b>\n"
                f"사유: {err}"
            )


def _fmt_pnl(pnl: float) -> str:
    return f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"


def _trade_move_pct(entry: float, target: float, direction: str) -> float:
    if entry <= 0 or target <= 0:
        return 0.0
    if direction == "LONG":
        return (target - entry) / entry * 100
    return (entry - target) / entry * 100


def _planned_expectation_text(t: dict) -> str:
    entry = float(t.get("entry_price", 0) or 0)
    qty = float(t.get("qty", 0) or 0)
    leverage = float(t.get("leverage", 1) or 1)
    direction = t.get("direction", "")
    tps = t.get("tps", []) or []
    if entry <= 0 or not tps:
        return ""

    fee_pct = ROUND_TRIP_FEE * 100
    weighted_net_pct = 0.0
    for tp in tps:
        weight = float(tp.get("pct", 0) or 0) / 100
        gross_pct = _trade_move_pct(entry, float(tp.get("price", 0) or 0), direction)
        weighted_net_pct += (gross_pct - fee_pct) * weight
    expected_usd = qty * entry * (weighted_net_pct / 100)
    tp1 = tps[0]
    tp1_pct = _trade_move_pct(entry, float(tp1.get("price", 0) or 0), direction)
    sign = "+" if expected_usd >= 0 else ""
    return (
        f"계획: TP1 {tp1_pct:+.2f}%  |  전체목표 {weighted_net_pct:+.2f}%"
        f" / {sign}${expected_usd:.2f}  |  증거금ROI {weighted_net_pct * leverage:+.1f}%"
    )


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
        if t.get("strategy"):
            lines.append(f"   전략: {t.get('strategy')}  |  종료사유: {t.get('exit_reason', '-')}")

        plan_text = _planned_expectation_text(t)
        if plan_text:
            lines.append(f"   {plan_text}")

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
    from trade_router import MAX_DAILY_LOSS
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

    lines.append("")
    lines.append(build_signal_quality_report())

    return "\n".join(lines)


def _maybe_send_periodic_report():
    """4시간마다 거래 결산 + 학습 분석 텔레그램 발송."""
    from trade_router import (_load_state, _save_state,
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
        send_signal(adj_msg)
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


def _print_surge_radar(surge: list[dict]) -> None:
    """직전 스냅샷 대비 거래량 급증 종목 출력."""
    if not surge:
        return
    print("  ⚡ 거래량 급증 편입 후보")
    print("  ─" * 30)
    print(f"  {'#':>2}  {'심볼':<10} {'증가':>8}  {'증가율':>7}  {'24h거래대금':>10}")
    print("  ─" * 30)
    for r in surge:
        coin = r["symbol"].split("/")[0]
        print(
            f"  #{r['rank']:>2}  {coin:<10} {r['volume_delta_label']:>8}"
            f"  +{r['volume_growth_pct']:>5.1f}%  {r['volume_label']:>10}"
        )
    print()


def _print_btc_sync_radar(candidates: list[dict]) -> None:
    """BTC 대비 가격 괴리 후보를 콘솔에 표시한다."""
    if not candidates:
        print("  🧭 BTC 동조/괴리 후보: 없음")
        return
    print("=" * 60)
    print("  🧭 BTC Sync Dislocation Radar")
    print("  ─" * 30)
    print("   #  심볼       모드     방향   베타괴리   원괴리     z     VOL")
    print("  ─" * 30)
    for r in candidates[:BTC_SYNC_SCAN_TOP_N]:
        coin = r["symbol"].split("/")[0]
        icon = "▲" if r["direction"] == "LONG" else "▼"
        mode = "MOM" if r.get("sync_mode") == "momentum" else "REV"
        print(
            f"  #{r['rank']:>2}  {coin:<8} {mode:<7} {icon}{r['direction']:<5}"
            f" {r['gap_pct']:+7.2f}% {r.get('raw_gap_pct', 0):+7.2f}%"
            f" {r.get('gap_zscore', 0):+5.1f}  {r['vol_ratio']:>5.1f}x"
        )
    print()


def _print_hyperliquid_lead_radar(candidates: list[dict]) -> None:
    """Hyperliquid 선행 수급 후보를 콘솔에 표시한다."""
    if not candidates:
        print("  🧲 Hyperliquid 선행수급 후보: 없음")
        return
    print("=" * 60)
    print("  🧲 Strategy 5 / Hyperliquid Lead Radar")
    print("  ─" * 30)
    print("   #  심볼       방향   15m     1h      VOL    OI      FUND")
    print("  ─" * 30)
    for r in candidates[:HYPERLIQUID_SCAN_TOP_N]:
        coin = r["symbol"].split("/")[0]
        icon = "▲" if r["direction"] == "LONG" else "▼"
        funding = float(r.get("funding", 0) or 0) * 100
        print(
            f"  #{r['rank']:>2}  {coin:<8} {icon}{r['direction']:<5}"
            f" {r.get('ret_15m_pct', 0):+6.2f}% {r.get('ret_1h_pct', 0):+6.2f}%"
            f" {r.get('vol_ratio', 0):>5.1f}x {r.get('open_interest_label', '-'):>7}"
            f" {funding:+6.3f}%"
        )
    print()


def _hyperliquid_lead_agrees(signal: dict, direction: str) -> bool:
    lead = signal.get("hyperliquid_lead") or {}
    return bool(lead and lead.get("direction") == direction)


def _attach_hyperliquid_lead(signal: dict, lead: dict | None) -> dict:
    """신호에 Hyperliquid 선행수급 스냅샷을 붙인다."""
    if not lead:
        return signal
    signal["hyperliquid_lead"] = {
        "rank": lead.get("rank"),
        "symbol": lead.get("symbol"),
        "hyperliquid_coin": lead.get("hyperliquid_coin"),
        "direction": lead.get("direction"),
        "lead_ret_pct": lead.get("lead_ret_pct"),
        "ret_15m_pct": lead.get("ret_15m_pct"),
        "ret_1h_pct": lead.get("ret_1h_pct"),
        "vol_ratio": lead.get("vol_ratio"),
        "day_change_pct": lead.get("day_change_pct"),
        "bybit_change_pct": lead.get("bybit_change_pct"),
        "venue_change_pct": lead.get("venue_change_pct"),
        "lead_gap_pct": lead.get("lead_gap_pct"),
        "volume_usd": lead.get("volume_usd"),
        "volume_label": lead.get("volume_label"),
        "open_interest_usd": lead.get("open_interest_usd"),
        "open_interest_label": lead.get("open_interest_label"),
        "oi_growth_pct": lead.get("oi_growth_pct"),
        "funding": lead.get("funding"),
        "funding_overheated": lead.get("funding_overheated", False),
        "score": lead.get("score"),
        "last_ts": lead.get("last_ts"),
    }
    return signal


def _open_position_symbols_for_fast_radar() -> list[str]:
    """Fast Radar가 보유 중인 종목도 짧은 주기로 재확인할 수 있게 한다."""
    if not AUTO_TRADE:
        return []
    try:
        from trade_router import get_portfolio_risk_snapshot
        snapshot = get_portfolio_risk_snapshot(1)
        return [
            str(p.get("symbol", ""))
            for p in snapshot.get("positions", []) or []
            if p.get("symbol")
        ]
    except Exception as e:
        print(f"  [FastRadar] 오픈 포지션 조회 실패: {e}")
        return []


def _atr_from_df(df, period: int = 14) -> float:
    """OHLCV 데이터에서 ATR을 계산한다. 전략 3은 ATR 기반으로 SL폭을 정한다."""
    if df is None or len(df) < period + 2:
        return 0.0
    try:
        tr = (df["high"] - df["low"]).to_frame("hl")
        tr["hp"] = (df["high"] - df["close"].shift(1)).abs()
        tr["lp"] = (df["low"] - df["close"].shift(1)).abs()
        atr = float(tr.max(axis=1).rolling(period).mean().iloc[-1])
        return atr if math.isfinite(atr) and atr > 0 else 0.0
    except Exception:
        return 0.0


def _btc_sync_strength(candidate: dict) -> str:
    """괴리율과 거래량 확장 정도로 전략 3의 강도를 정한다."""
    gap = abs(float(candidate.get("gap_pct", 0) or 0))
    vol = float(candidate.get("vol_ratio", 0) or 0)
    zscore = abs(float(candidate.get("gap_zscore", 0) or 0))
    if gap >= BTC_SYNC_DIRECT_MIN_ABS_GAP_PCT * 2 and vol >= 2.5 and zscore >= 2.0:
        return "ELITE 💎"
    return "VERY STRONG 🔥"


def _btc_sync_leverage(candidate: dict) -> int:
    """전략 3 기본 레버리지. ROI 부족 시 뒤에서 필요한 범위까지만 보정한다."""
    gap = abs(float(candidate.get("gap_pct", 0) or 0))
    vol = float(candidate.get("vol_ratio", 0) or 0)
    zscore = abs(float(candidate.get("gap_zscore", 0) or 0))
    leverage = int(BTC_SYNC_DIRECT_BASE_LEVERAGE)
    if gap >= BTC_SYNC_DIRECT_MIN_ABS_GAP_PCT * 1.5 and vol >= 2.0:
        leverage = max(leverage, int(round(BTC_SYNC_DIRECT_BASE_LEVERAGE * 1.4)))
    if gap >= BTC_SYNC_DIRECT_MIN_ABS_GAP_PCT * 2 and vol >= 2.5 and zscore >= 2.0:
        leverage = BTC_SYNC_DIRECT_MAX_LEVERAGE
    return max(1, min(int(leverage), int(BTC_SYNC_DIRECT_MAX_LEVERAGE)))


def _btc_sync_cooldown_left(symbol: str, direction: str, state: dict) -> int:
    """같은 종목/방향으로 너무 잦은 재진입을 막는다."""
    cooldown = int(BTC_SYNC_DIRECT_COOLDOWN_MIN) * 60
    if cooldown <= 0:
        return 0
    key = f"{symbol}:{direction}"
    last_ts = float((state.get("btc_sync_direct_last_ts", {}) or {}).get(key, 0) or 0)
    return max(0, int(cooldown - (time.time() - last_ts)))


def _mark_btc_sync_trade(symbol: str, direction: str, state: dict) -> None:
    state.setdefault("btc_sync_direct_last_ts", {})[f"{symbol}:{direction}"] = time.time()


def _try_btc_sync_direct_trade(candidate: dict) -> bool:
    """
    매매전략 3: BTC Sync Dislocation.

    기존 GOT Core Quant의 다이버전스/MTF 검증을 통과시키지 않고, BTC 대비 1시간
    괴리와 거래량 확장을 별도 알파로 본다. 다만 실거래 공통 안전장치인 기대ROI,
    R:R, 포트폴리오 증거금 용량, 일손실/DD 방어는 그대로 적용한다.
    """
    from trade_router import (execute, get_usdt_balance, build_trade_notification,
                        _append_trade, add_trade_context,
                        has_open_position, get_daily_loss_limit,
                        get_margin_cap, log_trade_candidate, log_execution_journal,
                        notify_trade_block, position_pct_for_risk, _load_state,
                        _save_state)

    symbol = candidate.get("symbol", "")
    direction = candidate.get("direction", "")
    sync_mode = candidate.get("sync_mode", "momentum")
    strategy = "BTC Sync Reversion" if sync_mode == "reversion" else "BTC Sync Momentum"
    tf_key = BTC_SYNC_DIRECT_TIMEFRAME
    strength = _btc_sync_strength(candidate)
    signal_type = f"btc_sync_{sync_mode}_{direction.lower()}"
    gap = float(candidate.get("gap_pct", 0) or 0)
    abs_gap = abs(gap)
    raw_gap = float(candidate.get("raw_gap_pct", gap) or 0)
    expected_ret = float(candidate.get("expected_ret_pct", 0) or 0)
    beta = float(candidate.get("beta", 1.0) or 1.0)
    corr = float(candidate.get("correlation", 0) or 0)
    zscore = float(candidate.get("gap_zscore", 0) or 0)
    spread_pct = float(candidate.get("spread_pct", 0) or 0)
    vol_ratio = float(candidate.get("vol_ratio", 0) or 0)

    def _block(reason: str, send_diag: bool = False, **extra):
        notify_trade_block(
            symbol, tf_key, direction, strength, reason,
            strategy=strategy,
            send_telegram=(AUTO_TRADE_DIAGNOSTICS and send_diag),
            signal_type=signal_type,
            is_divergence=False,
            btc_sync_gap_pct=gap,
            btc_sync_raw_gap_pct=raw_gap,
            btc_sync_expected_ret_pct=expected_ret,
            btc_sync_beta=beta,
            btc_sync_correlation=corr,
            btc_sync_gap_zscore=zscore,
            btc_sync_mode=sync_mode,
            btc_sync_spread_pct=spread_pct,
            btc_sync_vol_ratio=vol_ratio,
            btc_sync_btc_ret_pct=candidate.get("btc_ret_pct", 0),
            btc_sync_symbol_ret_pct=candidate.get("symbol_ret_pct", 0),
            **extra,
        )

    if not symbol or direction not in {"LONG", "SHORT"}:
        return False
    if abs_gap < BTC_SYNC_DIRECT_MIN_ABS_GAP_PCT:
        _block(
            f"전략3 괴리율 {abs_gap:.2f}% < 실거래 기준 {BTC_SYNC_DIRECT_MIN_ABS_GAP_PCT:.2f}%",
            btc_sync_rank=candidate.get("rank"),
        )
        return False
    if vol_ratio < BTC_SYNC_DIRECT_MIN_VOL_RATIO:
        _block(
            f"전략3 거래량 {vol_ratio:.2f}x < 실거래 기준 {BTC_SYNC_DIRECT_MIN_VOL_RATIO:.2f}x",
            btc_sync_rank=candidate.get("rank"),
        )
        return False
    if spread_pct > BTC_SYNC_DIRECT_MAX_SPREAD_PCT:
        _block(
            f"전략3 호가스프레드 {spread_pct:.3f}% > 실거래 기준 {BTC_SYNC_DIRECT_MAX_SPREAD_PCT:.3f}%",
            btc_sync_rank=candidate.get("rank"),
        )
        return False
    if abs(corr) < BTC_SYNC_DIRECT_MIN_CORRELATION:
        _block(
            f"전략3 BTC 상관 {corr:+.2f} < 실거래 기준 {BTC_SYNC_DIRECT_MIN_CORRELATION:.2f}",
            btc_sync_rank=candidate.get("rank"),
        )
        return False
    z_abs = abs(zscore)
    if sync_mode == "momentum" and z_abs < BTC_SYNC_DIRECT_MOMENTUM_MIN_ZSCORE:
        _block(
            f"전략3 모멘텀 z {zscore:+.2f} < 실거래 기준 {BTC_SYNC_DIRECT_MOMENTUM_MIN_ZSCORE:.2f}",
            btc_sync_rank=candidate.get("rank"),
        )
        return False
    if sync_mode == "reversion" and z_abs < BTC_SYNC_DIRECT_REVERSION_MIN_ZSCORE:
        _block(
            f"전략3 평균회귀 z {zscore:+.2f} < 실거래 기준 {BTC_SYNC_DIRECT_REVERSION_MIN_ZSCORE:.2f}",
            btc_sync_rank=candidate.get("rank"),
        )
        return False

    state = _load_state()
    today_symbol_losses = _today_loss_count(state, symbol=symbol, strategy_mode="btc_sync")
    if (
        BTC_SYNC_DIRECT_DAILY_SYMBOL_LOSS_LIMIT > 0
        and today_symbol_losses >= BTC_SYNC_DIRECT_DAILY_SYMBOL_LOSS_LIMIT
    ):
        _block(
            f"전략3 오늘 {symbol} 손실 {today_symbol_losses}회 기록 — "
            "같은 괴리 패턴 반복 진입 차단",
            send_diag=True,
        )
        return False

    cooldown_left = _btc_sync_cooldown_left(symbol, direction, state)
    if cooldown_left > 0:
        _block(f"전략3 동일 종목/방향 쿨다운 {cooldown_left // 60 + 1}분 남음")
        return False

    tracked_positions = state.get("positions", {}) or {}
    if symbol in tracked_positions:
        _block("이미 추적 중인 오픈 포지션 있음")
        return False
    if has_open_position(symbol):
        _block("이미 오픈 포지션 있음")
        return False
    # 전략3도 고정 개수 제한 없이, 최종 사이징 후 포트폴리오 용량으로 판단한다.

    try:
        df = fetch_ohlcv(symbol, tf_key, 120)
    except Exception as e:
        _block(f"전략3 {tf_key} 데이터 오류: {e}", send_diag=True)
        return False
    if df is None or len(df) < 40:
        _block(f"전략3 {tf_key} 데이터 부족", send_diag=True)
        return False

    entry_price = float(df["close"].iloc[-1])
    atr = _atr_from_df(df)
    if entry_price <= 0 or atr <= 0:
        _block("전략3 ATR/현재가 계산 실패", send_diag=True)
        return False

    last_open = float(df["open"].iloc[-1])
    last_close = float(df["close"].iloc[-1])
    last_body = abs(last_close - last_open)
    hard_reverse = (
        (direction == "LONG" and last_close < last_open and last_body > atr * 0.35)
        or (direction == "SHORT" and last_close > last_open and last_body > atr * 0.35)
    )
    if hard_reverse:
        _block(
            f"전략3 최근 {tf_key} 캔들이 강한 반대방향 몸통 — 추격 진입 보류",
            price=entry_price,
        )
        return False

    stop_dist = max(
        atr * float(BTC_SYNC_DIRECT_STOP_ATR_MULT),
        entry_price * float(BTC_SYNC_DIRECT_STOP_MIN_PCT),
    )
    sl = entry_price - stop_dist if direction == "LONG" else entry_price + stop_dist
    if sl <= 0:
        _block("전략3 SL 계산 실패", send_diag=True)
        return False

    tps = []
    for rr, pct in zip(BTC_SYNC_DIRECT_TP_RR, BTC_SYNC_DIRECT_TP_PCT):
        tp_price = entry_price + stop_dist * float(rr) if direction == "LONG" else entry_price - stop_dist * float(rr)
        if tp_price <= 0:
            _block("전략3 TP 계산 실패", send_diag=True)
            return False
        tps.append({"price": round(tp_price, 6), "pct": int(pct), "rr": round(float(rr), 2)})

    tp1_rr = float(tps[0]["rr"]) if tps else 0.0
    best_rr = max(float(tp["rr"]) for tp in tps) if tps else 0.0
    if tp1_rr < BTC_SYNC_DIRECT_MIN_TP1_RR or best_rr < BTC_SYNC_DIRECT_MIN_BEST_RR:
        _block(
            f"전략3 손익비 기준 미달 (TP1 R:R 1:{tp1_rr:.1f}, 최대 1:{best_rr:.1f})",
            tp1_rr=tp1_rr,
            best_rr=best_rr,
        )
        return False

    leverage = _btc_sync_leverage(candidate)
    leverage, roi_metrics, roi_leverage_notes = _raise_leverage_for_roi(
        entry_price, direction, tps, leverage
    )
    leverage_notes = list(roi_leverage_notes)
    if leverage > BTC_SYNC_DIRECT_MAX_LEVERAGE:
        leverage = int(BTC_SYNC_DIRECT_MAX_LEVERAGE)
        roi_metrics = _planned_roi_metrics(entry_price, direction, tps, leverage)
        leverage_notes.append(f"전략3 레버리지 상한 {BTC_SYNC_DIRECT_MAX_LEVERAGE}x 적용")
    for note in leverage_notes:
        print(f"  [전략3 ROI레버리지] {note}")

    roi_reason, roi_metrics = _roi_gate_reason(entry_price, direction, tps, leverage)
    if roi_reason:
        _block(roi_reason, send_diag=True, expected_margin_roi=roi_metrics["weighted_margin_roi_pct"])
        return False

    balance_now = get_usdt_balance()
    if balance_now <= 0:
        _block("잔고 조회 실패 또는 잔고 0", send_diag=True)
        return False

    risk_pct = min(float(BTC_SYNC_DIRECT_RISK_PCT), MAX_ACCOUNT_RISK_PCT)
    position_pct, est_sl_loss = position_pct_for_risk(
        balance_now, leverage, entry_price, sl, risk_pct,
        float(BTC_SYNC_DIRECT_POSITION_CAP),
    )
    if position_pct <= 0:
        _block("전략3 리스크 기반 수량 계산 실패", send_diag=True, risk_pct=risk_pct)
        return False

    daily_limit = get_daily_loss_limit(balance_now)
    daily_loss = float(state.get("daily_loss", 0) or 0)
    remaining_daily_risk = max(daily_limit - daily_loss, 0)
    high_opportunity = _is_high_profit_opportunity(
        "VERY STRONG", "VERY STRONG", roi_metrics, leverage_notes, tp1_rr
    )
    high_opportunity_block = (
        _risk_off_high_opportunity_reason(state)
        if high_opportunity else ""
    )
    if high_opportunity_block:
        high_opportunity = False
        print(f"  [전략3 리스크오프] {high_opportunity_block}")
    opportunity_risk_cap = _opportunity_risk_cap(balance_now)
    risk_notes = [
        "전략3 독립 진입: 기존 GOT Core Quant 지표 검증 없이 BTC 괴리 전용 조건으로 판단",
        f"목표 증거금 ${BTC_SYNC_DIRECT_MARGIN_USD:.2f}",
    ]
    if leverage_notes:
        risk_notes.extend(leverage_notes)
    if high_opportunity:
        risk_notes.append(
            f"전략3 고기대수익: 일손실은 소프트캡, 계좌위험 하드캡 ${opportunity_risk_cap:.2f}"
        )
    elif high_opportunity_block:
        risk_notes.append(high_opportunity_block)

    if est_sl_loss > remaining_daily_risk and not high_opportunity:
        _block(
            f"전략3 SL위험 ${est_sl_loss:.2f} > 남은 일손실한도 ${remaining_daily_risk:.2f}",
            send_diag=True,
            est_sl_loss=est_sl_loss,
        )
        return False
    if est_sl_loss > opportunity_risk_cap and high_opportunity and opportunity_risk_cap > 0:
        scale = opportunity_risk_cap / est_sl_loss * 0.95
        position_pct = round(position_pct * scale, 4)
        est_sl_loss = round(est_sl_loss * scale, 4)
        risk_notes.append(f"전략3 계좌위험 하드캡 내 축소: SL위험 ${est_sl_loss:.2f}")

    max_margin = get_margin_cap(balance_now, scalp=True)
    boosted_pct, boosted_loss, margin_notes, margin_block = _apply_min_trade_margin(
        balance_now, position_pct, leverage, entry_price, sl, max_margin,
        remaining_daily_risk,
        target_margin_usd=BTC_SYNC_DIRECT_MARGIN_USD,
        label="전략3 목표증거금",
        allow_opportunity_override=high_opportunity,
        opportunity_risk_cap_usd=opportunity_risk_cap,
    )
    if margin_block:
        _block(margin_block, send_diag=True)
        return False
    if margin_notes:
        position_pct = boosted_pct
        est_sl_loss = boosted_loss
        risk_notes.extend(margin_notes)

    position_pct, est_sl_loss, portfolio_notes, portfolio_block = _apply_portfolio_capacity_gate(
        balance_now, position_pct, est_sl_loss, max_margin, direction,
        high_opportunity=high_opportunity,
        label=f"{strategy} 포트폴리오 용량",
        min_execution_margin_usd=(
            MIN_FALLBACK_TRADE_MARGIN_USD
            if high_opportunity else min(MIN_TRADE_MARGIN_USD, BTC_SYNC_DIRECT_MARGIN_USD)
        ),
    )
    if portfolio_block:
        _block(portfolio_block, send_diag=True)
        return False
    if portfolio_notes:
        risk_notes.extend(portfolio_notes)
        for note in portfolio_notes:
            print(f"  [포트폴리오] {note}")

    sl_pct = abs(entry_price - sl) / entry_price * 100
    est_margin = min(balance_now * position_pct, max_margin)
    est_sl_loss = min(est_sl_loss, est_margin * leverage * (sl_pct / 100 + ROUND_TRIP_FEE))
    confirmed_count = 5 + int(abs_gap >= BTC_SYNC_DIRECT_MIN_ABS_GAP_PCT * 1.5) + int(vol_ratio >= 2.0)

    profile = classify_strategy(strategy, signal_type, False, direction)
    mode_label = "모멘텀 추격" if sync_mode == "momentum" else "평균회귀"
    reasons = [
        f"전략군: {format_profile(profile)}",
        f"전략3 하위모드: {mode_label}",
        (
            f"BTC 1h {float(candidate.get('btc_ret_pct', 0) or 0):+.2f}% vs "
            f"{symbol.split('/')[0]} 1h {float(candidate.get('symbol_ret_pct', 0) or 0):+.2f}% "
            f"→ 원괴리 {raw_gap:+.2f}%"
        ),
        (
            f"베타 {beta:.2f}, 기대수익 {expected_ret:+.2f}% 기준 "
            f"베타보정 괴리 {gap:+.2f}% / z {zscore:+.2f}"
        ),
        f"거래량 {vol_ratio:.2f}x, 24h 거래대금 {candidate.get('volume_label', '-')}",
        f"상관 {corr:+.2f}, 호가스프레드 {spread_pct:.3f}%, 공통캔들 {candidate.get('last_ts', '-')}",
        f"{tf_key} ATR 기반 SL {sl_pct:.2f}% / 최근 강한 반대 캔들 없음",
        f"TP1 R:R 1:{tp1_rr:.1f}, 최대 R:R 1:{best_rr:.1f}",
        (
            f"기대 증거금ROI {roi_metrics['weighted_margin_roi_pct']:+.1f}% "
            f"(TP1 {roi_metrics['tp1_margin_roi_pct']:+.1f}%)"
        ),
    ]
    if risk_notes:
        reasons.append("리스크 거버너: " + " | ".join(risk_notes))

    entry_context = {
        "strategy": strategy,
        "strategy_profile": profile,
        "strategy_family": profile["family_label"],
        "core_strategy": profile["strategy_label"],
        "strategy_mode": profile["family_key"],
        "asymmetric_mode": False,
        "signal_type": signal_type,
        "signal_label": "BTC 괴리 모멘텀",
        "is_divergence": False,
        "direction": direction,
        "tf": tf_key,
        "reasons": reasons,
        "timing_note": f"{tf_key} 최근 강한 반대 캔들 없음",
        "timing_tf": tf_key,
        "indicator_snapshot": {},
        "btc_sync_snapshot": {
            "rank": candidate.get("rank"),
            "sync_mode": sync_mode,
            "gap_pct": round(gap, 2),
            "raw_gap_pct": round(raw_gap, 2),
            "expected_ret_pct": round(expected_ret, 2),
            "beta": round(beta, 3),
            "correlation": round(corr, 3),
            "gap_zscore": round(zscore, 2),
            "spread_pct": round(spread_pct, 3),
            "last_body_pct": candidate.get("last_body_pct", 0),
            "last_ts": candidate.get("last_ts", ""),
            "btc_ret_pct": candidate.get("btc_ret_pct", 0),
            "symbol_ret_pct": candidate.get("symbol_ret_pct", 0),
            "vol_ratio": round(vol_ratio, 2),
            "volume_usd": candidate.get("volume_usd", 0),
        },
        "confirmed_count": confirmed_count,
        "divergence_count": 0,
        "vol_ratio": vol_ratio,
        "bars_ago": 0,
        "rr": {"tp1": tp1_rr, "best": best_rr},
        "entry_price": entry_price,
        "sl": sl,
        "sl_pct": sl_pct,
        "tps": tps,
        "risk_pct": risk_pct,
        "position_pct": position_pct,
        "est_sl_loss": est_sl_loss,
        "leverage": leverage,
        "expected_margin_roi_pct": roi_metrics["weighted_margin_roi_pct"],
        "tp1_margin_roi_pct": roi_metrics["tp1_margin_roi_pct"],
        "conviction_tier": "STRATEGY3",
        "target_margin_usd": BTC_SYNC_DIRECT_MARGIN_USD,
    }

    log_trade_candidate(
        symbol, tf_key, strategy, direction, strength, "candidate",
        "전략3 BTC 괴리 직접매매 후보",
        price=entry_price, rr=best_rr, tp1_rr=tp1_rr,
        sl=sl, sl_pct=sl_pct, tps=tps,
        leverage=leverage, position_pct=position_pct, risk_pct=risk_pct,
        est_sl_loss=est_sl_loss, entry_reasons=reasons,
        entry_context=entry_context,
        btc_sync_gap_pct=gap,
        btc_sync_raw_gap_pct=raw_gap,
        btc_sync_expected_ret_pct=expected_ret,
        btc_sync_beta=beta,
        btc_sync_correlation=corr,
        btc_sync_gap_zscore=zscore,
        btc_sync_mode=sync_mode,
        btc_sync_spread_pct=spread_pct,
        btc_sync_vol_ratio=vol_ratio,
        signal_type=signal_type,
        is_divergence=False,
        strategy_family=entry_context["strategy_family"],
        core_strategy=entry_context["core_strategy"],
        strategy_mode=entry_context["strategy_mode"],
        asymmetric_mode=False,
    )

    result = execute(
        symbol=symbol,
        direction=direction,
        leverage=leverage,
        entry_price=entry_price,
        sl=sl,
        tps=[{"price": tp["price"], "pct": tp["pct"]} for tp in tps],
        position_pct=position_pct,
        atr=atr,
        is_elite=("ELITE" in strength),
        max_margin_usd=max_margin,
        min_margin_usd=(
            MIN_FALLBACK_TRADE_MARGIN_USD
            if high_opportunity else min(MIN_TRADE_MARGIN_USD, BTC_SYNC_DIRECT_MARGIN_USD)
        ),
        allow_pause_override=high_opportunity,
        pause_override_reason="전략3 BTC 괴리 고기대수익 기회",
    )

    if result["ok"]:
        margin_r = min(balance_now * position_pct, max_margin)
        trade_num = _append_trade(
            symbol, direction, tf_key, strength,
            result["leverage"], result["qty"],
            entry_price, sl, margin_r,
            strategy=strategy,
            signal_type=signal_type,
            is_divergence=False,
            tps=tps,
            entry_reasons=reasons,
            entry_context=entry_context,
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=False,
        )
        add_trade_context(
            trade_num,
            ema_trend=0,
            confirmed_count=confirmed_count,
            divergence_count=0,
            vol_ratio=vol_ratio,
            bars_ago=0,
            sl_pct=sl_pct,
            risk_pct=risk_pct,
            est_sl_loss=est_sl_loss,
            strategy=strategy,
            signal_type=signal_type,
            is_divergence=False,
            fast_exit=True,
            best_rr=best_rr,
            tp1_rr=tp1_rr,
            btc_sync_gap_pct=gap,
            btc_sync_raw_gap_pct=raw_gap,
            btc_sync_expected_ret_pct=expected_ret,
            btc_sync_beta=beta,
            btc_sync_correlation=corr,
            btc_sync_gap_zscore=zscore,
            btc_sync_mode=sync_mode,
            btc_sync_spread_pct=spread_pct,
            btc_sync_vol_ratio=vol_ratio,
            btc_sync_btc_ret_pct=candidate.get("btc_ret_pct", 0),
            btc_sync_symbol_ret_pct=candidate.get("symbol_ret_pct", 0),
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=False,
        )
        log_trade_candidate(
            symbol, tf_key, strategy, direction, strength, "opened",
            price=entry_price, leverage=result["leverage"], qty=result["qty"],
            position_pct=position_pct, risk_pct=risk_pct,
            est_sl_loss=est_sl_loss, rr=best_rr, tp1_rr=tp1_rr,
            sl=sl, sl_pct=sl_pct, tps=tps,
            entry_reasons=reasons,
            entry_context=entry_context,
            btc_sync_gap_pct=gap,
            btc_sync_raw_gap_pct=raw_gap,
            btc_sync_expected_ret_pct=expected_ret,
            btc_sync_beta=beta,
            btc_sync_correlation=corr,
            btc_sync_gap_zscore=zscore,
            btc_sync_mode=sync_mode,
            btc_sync_spread_pct=spread_pct,
            btc_sync_vol_ratio=vol_ratio,
            signal_type=signal_type,
            is_divergence=False,
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=False,
        )
        log_execution_journal(
            trade_num, "opened",
            symbol=symbol, tf=tf_key, strategy=strategy,
            strategy_family=entry_context["strategy_family"],
            core_strategy=entry_context["core_strategy"],
            strategy_mode=entry_context["strategy_mode"],
            asymmetric_mode=False,
            direction=direction, strength=strength,
            signal_type=signal_type,
            entry_price=entry_price, sl=sl, tps=tps,
            leverage=result["leverage"], qty=result["qty"],
            margin=margin_r, balance=balance_now,
            risk_pct=risk_pct, position_pct=position_pct,
            est_sl_loss=est_sl_loss,
            btc_sync_gap_pct=gap,
            btc_sync_raw_gap_pct=raw_gap,
            btc_sync_expected_ret_pct=expected_ret,
            btc_sync_beta=beta,
            btc_sync_correlation=corr,
            btc_sync_gap_zscore=zscore,
            btc_sync_mode=sync_mode,
            btc_sync_spread_pct=spread_pct,
            btc_sync_vol_ratio=vol_ratio,
            entry_context=entry_context,
        )
        cooldown_state = _load_state()
        _mark_btc_sync_trade(symbol, direction, cooldown_state)
        _save_state(cooldown_state)
        notif = build_trade_notification(
            symbol, direction, result["leverage"], result["qty"],
            entry_price, sl, tps, balance_now,
            tf_key=tf_key, strength=strength, strategy=strategy,
            trade_num=trade_num, reasons=reasons,
            timing_note=entry_context["timing_note"],
            rr=best_rr, risk_pct=risk_pct, est_sl_loss=est_sl_loss,
            sl_pct=sl_pct, signal_type=signal_type,
            confirmed_count=confirmed_count,
            divergence_count=0,
            is_divergence=False,
            entry_context=entry_context,
        )
        analysis_sent = send_position_analysis(notif)
        send(_build_open_summary(
            trade_num, symbol, direction, tf_key, strategy, strength,
            result["leverage"], result["qty"], entry_price, sl, tps,
            est_sl_loss, analysis_sent, entry_context["strategy_profile"],
        ))
        print(f"  [전략3] ✅ {trade_num}회차 BTC 괴리 독립 진입 기록")
        return True

    print(f"  [전략3] ❌ 주문 실패: {result['error']}")
    log_trade_candidate(
        symbol, tf_key, strategy, direction, strength, "order_failed",
        result["error"], price=entry_price, leverage=result["leverage"],
        position_pct=position_pct, risk_pct=risk_pct,
        entry_reasons=reasons, entry_context=entry_context,
        btc_sync_gap_pct=gap,
        btc_sync_raw_gap_pct=raw_gap,
        btc_sync_expected_ret_pct=expected_ret,
        btc_sync_beta=beta,
        btc_sync_correlation=corr,
        btc_sync_gap_zscore=zscore,
        btc_sync_mode=sync_mode,
        btc_sync_spread_pct=spread_pct,
        btc_sync_vol_ratio=vol_ratio,
        signal_type=signal_type, is_divergence=False,
        strategy_family=entry_context["strategy_family"],
        core_strategy=entry_context["core_strategy"],
        strategy_mode=entry_context["strategy_mode"],
        asymmetric_mode=False,
    )
    if AUTO_TRADE_DIAGNOSTICS:
        err = escape(str(result["error"]))
        send_signal(
            f"❌ <b>[전략3 주문 실패]</b> {symbol.split('/')[0]} {tf_key} {direction}\n"
            f"전략군: <b>{escape(format_profile(entry_context['strategy_profile']))}</b>\n"
            f"강도: <b>{escape(str(strength))}</b>\n"
            f"사유: {err}"
        )
    return False


def _run_btc_sync_direct_trades(candidates: list[dict]) -> None:
    """레이더 후보 상위 N개를 전략 3 독립 매매 엔진으로 평가한다."""
    if not BTC_SYNC_DIRECT_TRADE_ENABLED or not candidates:
        return
    print(f"  [전략3] BTC Sync 독립매매 평가 {min(len(candidates), BTC_SYNC_DIRECT_TOP_N)}개")
    opened = 0
    evaluated = 0
    seen_symbols: set[str] = set()
    for candidate in candidates:
        symbol = str(candidate.get("symbol", "") or "")
        if not symbol:
            continue
        if symbol in seen_symbols:
            print(f"  [전략3] {symbol} 중복 후보 스킵 — 한 스캔 1종목 1회만 평가")
            continue
        seen_symbols.add(symbol)
        evaluated += 1
        if evaluated > BTC_SYNC_DIRECT_TOP_N:
            break
        if _try_btc_sync_direct_trade(candidate):
            opened += 1
    if opened:
        print(f"  [전략3] 이번 스캔 신규 진입 {opened}개")


def _reconcile_orphan_positions():
    """
    Bybit 실제 포지션 vs trade_state.json 추적 포지션 비교.
    미추적(orphan) 포지션 발견 시:
      - 텔레그램 경고 발송
      - PnL > -30% 손실: 경고만 (SL 미설정)
      - PnL <= -30% 손실: 긴급 SL 자동 설정 (현재가 기준 3ATR 위 / 아래)
    퀀트 원칙: SL 없는 포지션은 즉시 관리 대상.
    """
    from trade_router import fetch_all_positions_raw, place_emergency_sl, _load_state, _save_state

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

    venue = venue_label(runtime_context()["execution_venue"])
    lines = [f"⚠️ <b>[{venue} 미추적 포지션 발견 — SL 없음]</b>\n봇이 열지 않은 포지션 {len(fresh_orphans)}개\n"]
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
                lines.append(f"  ❌ 긴급 SL 설정 실패 — {venue}에서 직접 설정 필요")
        else:
            lines.append(f"  📌 SL 미설정 — {venue}에서 직접 설정 권장")
        lines.append("")
        alerts[alert_key] = now

    lines.append(f"💡 관리 방법: {venue}에서 해당 포지션에 SL을 설정하거나 청산하세요.")
    send("\n".join(lines))
    _save_state(s)
    print(f"[조정] 미추적 포지션 {len(fresh_orphans)}개 처리 완료")


def scan():
    # ── Step 0: 포지션 모니터링 먼저 ───────────────────────────────────────────
    if AUTO_TRADE:
        from trade_router import monitor_positions, evaluate_trade_candidates
        monitor_positions()
        _reconcile_orphan_positions()
        if not FAST_RADAR:
            eval_notes = evaluate_trade_candidates()
            if eval_notes:
                print(f"  [후보평가] {len(eval_notes)}개 완료 — MFE/MAE 학습 로그 누적")
            _maybe_send_periodic_report()

    # ── Step 1: 바이빗 거래량 Top10 + 급증 거래량 조회 ─────────────────────────
    # 진입 전 항상 현재 시장 거래량 상위/급증 종목 파악 → 유동성 집중 종목 우선 스캔
    fast_mode = bool(FAST_RADAR and FAST_RADAR_ENABLED)
    radar        = [] if fast_mode else fetch_market_radar(n=RADAR_TOP_N)
    surge_n      = FAST_RADAR_SURGE_TOP_N if fast_mode else VOLUME_SURGE_TOP_N
    surge_radar  = fetch_volume_surge_radar(n=surge_n)
    hyperliquid_radar = fetch_hyperliquid_lead_radar(
        n=HYPERLIQUID_SCAN_TOP_N
    ) if HYPERLIQUID_RADAR_ENABLED else []
    btc_sync_radar = fetch_btc_sync_dislocations() if BTC_SYNC_RADAR_ENABLED else []
    radar_syms   = [r["symbol"] for r in radar]
    surge_syms   = [r["symbol"] for r in surge_radar]
    hyperliquid_syms = [r["symbol"] for r in hyperliquid_radar[:HYPERLIQUID_SCAN_TOP_N]]
    hyperliquid_by_symbol = {r["symbol"]: r for r in hyperliquid_radar}
    btc_sync_syms = [r["symbol"] for r in btc_sync_radar[:BTC_SYNC_SCAN_TOP_N]]

    if fast_mode:
        open_syms = _open_position_symbols_for_fast_radar()
        scan_symbols = list(dict.fromkeys(
            surge_syms + hyperliquid_syms + btc_sync_syms + open_syms
        ))[:FAST_RADAR_MAX_SYMBOLS]
    else:
        open_syms = []
        # Top10 → 거래량 급증 → Hyperliquid 선행수급 → BTC괴리 → 코어/토큰화주식 순서로 우선순위
        scan_symbols = list(dict.fromkeys(
            radar_syms + surge_syms + hyperliquid_syms + btc_sync_syms + CORE_SYMBOLS + STOCK_SYMBOLS
        ))

    if DRY_RUN:
        mode_tag = "[FAST RADAR DRY RUN]" if fast_mode else "[DRY RUN]"
    elif AUTO_TRADE:
        mode_tag = "[FAST RADAR AUTO TRADE]" if fast_mode else "[AUTO TRADE]"
    else:
        mode_tag = "[FAST RADAR]" if fast_mode else ""
    print(f"\n{'='*60}")
    ctx = runtime_context()
    venue = venue_label(ctx["execution_venue"])
    data_venue = venue_label(ctx["market_data_venue"])
    data_note = "" if venue == data_venue else f" / Data:{data_venue}"
    print(f"  📡 CryptoSignal 스캔  [{venue}{data_note}] {mode_tag}")
    print(f"{'='*60}")

    # ── Step 2: Market Radar 출력 ──────────────────────────────────────────────
    if fast_mode:
        _print_surge_radar(surge_radar)
        if HYPERLIQUID_RADAR_ENABLED:
            _print_hyperliquid_lead_radar(hyperliquid_radar)
        if BTC_SYNC_RADAR_ENABLED:
            _print_btc_sync_radar(btc_sync_radar)
        print(
            f"  → FastRadar: 급증{len(surge_syms)} + HL선행{len(hyperliquid_syms)} "
            f"+ BTC괴리{len(btc_sync_syms)} + 보유{len(open_syms)} "
            f"기준 스캔 대상: {len(scan_symbols)}종목"
        )
        if scan_symbols:
            coins = [s.split("/")[0] for s in scan_symbols]
            print(f"  → 빠른 확인: {', '.join(coins[:FAST_RADAR_MAX_SYMBOLS])}")
        else:
            print("  → FastRadar 후보 없음 — 포지션 모니터링만 수행")
    elif radar:
        _print_radar(radar)
        _print_surge_radar(surge_radar)
        if HYPERLIQUID_RADAR_ENABLED:
            _print_hyperliquid_lead_radar(hyperliquid_radar)
        if BTC_SYNC_RADAR_ENABLED:
            _print_btc_sync_radar(btc_sync_radar)
        new_coins = [s.split("/")[0] for s in radar_syms if s not in CORE_SYMBOLS]
        surge_coins = [s.split("/")[0] for s in surge_syms if s not in radar_syms]
        hyperliquid_coins = [
            s.split("/")[0] for s in hyperliquid_syms
            if s not in radar_syms and s not in surge_syms
        ]
        sync_coins = [
            s.split("/")[0] for s in btc_sync_syms
            if s not in radar_syms and s not in surge_syms and s not in hyperliquid_syms
        ]
        print(
            f"  → Top{RADAR_TOP_N} + 급증{len(surge_syms)} "
            f"+ HL선행{len(hyperliquid_syms)} + BTC괴리{len(btc_sync_syms)} "
            f"기준 스캔 대상: {len(scan_symbols)}종목"
        )
        if new_coins:
            print(f"  → 동적 편입: {', '.join(new_coins[:5])}{'...' if len(new_coins)>5 else ''}")
        if surge_coins:
            print(f"  → 급증 편입: {', '.join(surge_coins[:5])}{'...' if len(surge_coins)>5 else ''}")
        if hyperliquid_coins:
            print(f"  → HL선행 편입: {', '.join(hyperliquid_coins[:5])}{'...' if len(hyperliquid_coins)>5 else ''}")
        if sync_coins:
            print(f"  → BTC괴리 편입: {', '.join(sync_coins[:5])}{'...' if len(sync_coins)>5 else ''}")
    else:
        print("  [Radar] 데이터 없음 — 기본 종목으로 진행")
        scan_symbols = list(SYMBOLS)

    print()
    if AUTO_TRADE and not DRY_RUN and BTC_SYNC_DIRECT_TRADE_ENABLED:
        _run_btc_sync_direct_trades(btc_sync_radar)

    total_signals = 0
    total_scanned = 0
    bb_mid_watch = []

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
        hl_lead = hyperliquid_by_symbol.get(symbol)
        if hl_lead:
            hl_dir_icon = "▲" if hl_lead.get("direction") == "LONG" else "▼"
            hl_funding = float(hl_lead.get("funding", 0) or 0) * 100
            print(
                f"  [전략5 HL선행] #{hl_lead.get('rank')} {hl_dir_icon}{hl_lead.get('direction')} "
                f"15m {float(hl_lead.get('ret_15m_pct', 0) or 0):+.2f}% / "
                f"1h {float(hl_lead.get('ret_1h_pct', 0) or 0):+.2f}%  "
                f"VOL {float(hl_lead.get('vol_ratio', 0) or 0):.2f}x  "
                f"OI {hl_lead.get('open_interest_label', '-')}  FUND {hl_funding:+.3f}%"
            )
        # 주봉 + 일봉 추세 바이어스 — 심볼당 1회 조회 (각각 1h/30분 캐시)
        macro = get_macro_bias(symbol)
        daily = get_daily_bias(symbol)
        _bicon = {"LONG": "📈", "SHORT": "📉", "NEUTRAL": "➡️"}
        print(f"  [주봉] {macro['note']}  {_bicon[macro['direction']]} {macro['direction']} ({macro['strength']})")
        print(f"  [일봉] {daily['note']}  {_bicon[daily['direction']]} {daily['direction']} ({daily['strength']})")
        bb_mid_bias = get_bb_midline_long_bias(symbol)
        if bb_mid_bias.get("ok"):
            bb_mid_watch.append(f"{coin}({bb_mid_bias.get('score', 0)}/6)")
            print(f"  [BB중단] ✅ 내림롱 관심 — {bb_mid_bias['note']}")
        else:
            print(f"  [BB중단] - {bb_mid_bias.get('note', '조건 미충족')}")

        timeframes_to_scan = (
            {k: v for k, v in TIMEFRAMES.items() if k in FAST_RADAR_TIMEFRAMES}
            if fast_mode else TIMEFRAMES
        )
        for tf_key, tf_info in timeframes_to_scan.items():
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
                _attach_hyperliquid_lead(_sig, hl_lead)

            # 초기 스캔 단계에서는 심볼/봉만으로 넓게 차단하지 않는다.
            # 실제 손익학습 차단은 전략/방향이 확정된 진입 직전에 수행한다.
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
                    q = s.get("divergence_quality", {})
                    div_count = s.get("divergence_count", s["confirmed_count"])
                    max_div = q.get("max_divergence", 6)
                    max_conf = q.get("max_confirmed", 7)
                    marks = (
                        "✅" if s["rsi"]["ok"]  else "❌",
                        "✅" if s.get("cci", {}).get("ok") else "❌",
                        "✅" if s["macd"]["ok"] else "❌",
                        "✅" if s["obv"]["ok"]  else "❌",
                        "✅" if s["srsi"]["ok"] else "❌",
                        "✅" if s["vol"]["ok"]  else "❌",
                        "✅" if s.get("cvd", {}).get("ok") else "❌",
                    )
                    print(
                        f"    {s['signal_type']:15s}  RSI{marks[0]} CCI{marks[1]} "
                        f"MACD{marks[2]} OBV{marks[3]} SRSI{marks[4]} VOL{marks[5]} CVD{marks[6]}  "
                        f"(D{div_count}/{max_div}, T{s['confirmed_count']}/{max_conf})"
                    )

                # MTF 확인 (AUTO_TRADE 여부와 무관하게 항상 조회해서 알림에 표시)
                mtf_info = check_mtf(symbol, tf_key, direction)
                mtf_override = _mtf_soft_override(
                    best, mtf_info, tf_key, best.get("strategy", "다이버전스"), direction
                )
                if mtf_override["allow"]:
                    mtf_info = dict(mtf_info)
                    if mtf_override.get("elite"):
                        mtf_info["elite_mtf_override"] = mtf_override["kind"]
                        mtf_info["elite_mtf_risk_mult"] = mtf_override["risk_mult"]
                    else:
                        mtf_info["soft_mtf_override"] = mtf_override["kind"]
                        mtf_info["soft_mtf_risk_mult"] = mtf_override["risk_mult"]
                        mtf_info["soft_mtf_note"] = mtf_override["note"]
                if mtf_info["strong"]:
                    mtf_label = "✅전정렬"
                    mtf_suffix = "(포지션 부스트)"
                elif mtf_override["allow"]:
                    mtf_label = f"⚠️역방향-{mtf_override['kind']}"
                    mtf_suffix = f"(감액 {mtf_override['risk_mult']:.2f}x)"
                elif mtf_info["block"]:
                    mtf_label = "⛔역방향"
                    mtf_suffix = "(차단됨)"
                else:
                    mtf_label = "⚡부분"
                    mtf_suffix = ""
                print(f"  [{tf_label}] MTF({mtf_info['score']}/{mtf_info['max_score']}): "
                      f"{mtf_label}  {mtf_suffix}")

                msg = build_alert(symbol, tf_label, tf_key, signals, current_price,
                                  mtf_info=mtf_info)
                if FAST_RADAR:
                    msg = "📡 [FastRadar — 정보알림, 실거래 없음]\n" + msg
                total_signals += len(signals)

                if DRY_RUN:
                    print(msg)
                else:
                    ok = send_signal(msg)
                    print(f"  시그널 텔레그램: {'✅' if ok else '스킵'}")

                    if AUTO_TRADE:
                        if tf_key in TIMING_ONLY_TF:
                            print(f"  [{tf_label}] 5m는 초단타 보조 참고용 — 단독 자동매매 스킵")
                            _log_gate_block(symbol, tf_key, best, direction,
                                            "5m 초단타 보조 참고용 — 15m 이상 판단봉 필요")
                            continue
                        if mtf_override["allow"]:
                            _mark_mtf_soft_override(best, mtf_override)
                            print(f"  [MTF완화] {mtf_override['note']}")
                        # MODERATE 신호 — MTF 전정렬 시만 허용 (추세점수는 downstream 필터로 처리)
                        _moderate_ok = (not is_moderate) or MODERATE_AUTO_TRADE or mtf_info["strong"]
                        if not _moderate_ok:
                            print(f"  [{tf_label}] MODERATE 알림만 (MTF 전정렬 아님)")
                            _log_gate_block(symbol, tf_key, best, direction,
                                            "MODERATE 알림 전용 — MTF 전정렬 아님")
                        # MTF 역방향 차단
                        elif mtf_info["block"] and not mtf_override["allow"]:
                            print(f"  [MTF] 전 상위봉 역방향 → 자동매매 차단")
                            _log_gate_block(symbol, tf_key, best, direction,
                                            "MTF 전 상위봉 역방향")
                            send_signal(f"⛔ <b>[MTF 차단]</b> {symbol.split('/')[0]} {tf_label}\n"
                                        f"{mtf_summary(mtf_info)}\n역방향 추세 — 자동매매 스킵")
                        # 전략/방향 없는 초기 학습 필터. 실제 차단은 _try_auto_trade에서 수행.
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
                                    print(f"  [추세] ❌ 역추세 허용 ({best_fresh['confirmed_count']}/7 고확신)")
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
                                        elif mtf_info["block"]:
                                            _fresh_override = _mtf_soft_override(
                                                best_fresh_s,
                                                mtf_info,
                                                tf_key,
                                                best_fresh_s.get("strategy", "다이버전스"),
                                                direction,
                                            )
                                            if _fresh_override["allow"]:
                                                _sc_boost *= _fresh_override["risk_mult"]
                                                _mark_mtf_soft_override(best_fresh_s, _fresh_override)
                                                print(f"  [MTF감액] {_fresh_override['note']} → 최종 {_sc_boost:.2f}x")
                                            else:
                                                print(f"  [{tf_label}] MTF 완전역방향 → 소프트 허용 기준 미달 차단")
                                                _log_gate_block(symbol, tf_key, best_fresh_s, direction,
                                                                "MTF 완전역방향 소프트 허용 기준 미달")
                                                continue
                                        _try_auto_trade(symbol, tf_key, fresh, current_price,
                                                        scalp=True, mtf_boost=_sc_boost,
                                                        premium_mtf=mtf_info["strong"])
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
                                btc_macro_gate = (
                                    symbol == BTC_MACRO_SHORT_SYMBOL
                                    and direction == "SHORT"
                                    and not BTC_MACRO_TREND_REFERENCE_ONLY
                                    and _btc_macro_short_bias().get("active")
                                )
                                if btc_macro_gate and min_vol > BTC_MACRO_SHORT_SWING_MIN_VOL:
                                    print(
                                        f"  [BTC월봉숏] 거래량 게이트 완화 "
                                        f"{min_vol:.2f}x→{BTC_MACRO_SHORT_SWING_MIN_VOL:.2f}x"
                                    )
                                    min_vol = float(BTC_MACRO_SHORT_SWING_MIN_VOL)
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
                                            elif mtf_info["block"]:
                                                _swing_override = _mtf_soft_override(
                                                    best_swing,
                                                    mtf_info,
                                                    tf_key,
                                                    best_swing.get("strategy", "다이버전스"),
                                                    direction,
                                                )
                                                if _swing_override["allow"]:
                                                    boost *= _swing_override["risk_mult"]
                                                    _mark_mtf_soft_override(best_swing, _swing_override)
                                                    score_log.append(f"MTF역방향-{_swing_override['kind']}×{_swing_override['risk_mult']:.2f}")
                                                    print(f"  [MTF감액] {_swing_override['note']}")
                                                else:
                                                    print(f"  [{tf_label}] ⛔ MTF 완전역방향 → 자동매매 차단")
                                                    _log_gate_block(symbol, tf_key, best_swing, direction,
                                                                    "MTF 완전역방향 소프트 허용 기준 미달")
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
                                                _hidden_override = _full_elite_divergence(best_swing)
                                                if _hidden_override["allow"]:
                                                    boost *= _hidden_override["risk_mult"]
                                                    score_log.append(f"역추세히든ELITE×{_hidden_override['risk_mult']:.2f}")
                                                    print(f"  [{tf_label}] 역추세 히든 ELITE → 차단하지 않고 소액 허용")
                                                else:
                                                    print(f"  [{tf_label}] 역추세 히든다이버전스 → 7/7 ELITE 미달 차단")
                                                    _log_gate_block(symbol, tf_key, best_swing, direction,
                                                                    "역추세 히든다이버전스 7/7 ELITE 미달")
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
                                                            current_price, mtf_boost=boost,
                                                            premium_mtf=mtf_info["strong"])

                    time.sleep(1)
            else:
                print(f"  [{tf_label}] 다이버전스 없음  (${current_price:,.2f})")

            # ── 추가 전략 스캔 (RSI반전 / EMA눌림목) ─────────────────────────
            # 다이버전스 여부와 무관하게 항상 실행
            add_sigs = scan_additional(df, tf_key, higher_bias=bb_mid_bias)
            for asig in add_sigs:
                asig["current_price"] = current_price
                _attach_hyperliquid_lead(asig, hl_lead)
                strategy_tag = asig.get("strategy", "추가전략")
                adirection   = "LONG" if asig["signal_type"].endswith("long") else "SHORT"

                # ── 추세 게이트 (주봉+일봉 통합 — 추가전략) ─────────────────
                _ad_ts = sum(
                    1 for b in [macro, daily]
                    if b["direction"] not in ("NEUTRAL",) and b["direction"] == adirection
                )
                _ad_min = _trend_min_confirm(tf_key, _ad_ts, is_continuation=False)
                active_strategy_name = any(
                    base in strategy_tag for base in ACTIVE_STRONG_STRATEGIES
                )
                high_vol_current = (
                    active_strategy_name
                    and tf_key in {"15m", "1h"}
                    and asig["confirmed_count"] >= 5
                    and float(asig.get("vol", {}).get("value", 0) or 0) >= ACTIVE_HIGH_VOL
                    and asig.get("bars_ago", 0) <= 1
                )
                if (_ad_min is None or asig["confirmed_count"] < _ad_min) and not high_vol_current:
                    _ts_tag = ["❌역추세", "⭐단일일치", "⭐⭐이중일치"][_ad_ts]
                    print(f"  [{tf_label}] {strategy_tag} {_ts_tag} — "
                          f"{asig['confirmed_count']}/{_ad_min or 6} 미달 스킵")
                    _log_gate_block(symbol, tf_key, asig, adirection,
                                    f"{strategy_tag} 추세 기준 미달 {asig['confirmed_count']}/{_ad_min or 6}",
                                    strategy=strategy_tag)
                    continue
                if high_vol_current and (_ad_min is None or asig["confirmed_count"] < _ad_min):
                    print(
                        f"  [{tf_label}] {strategy_tag} 고거래량 현재봉 예외 허용 — "
                        f"{asig['confirmed_count']}/{_ad_min or 6}, VOL {asig['vol']['value']:.1f}x"
                    )

                print(f"  [{tf_label}] ⚡ {strategy_tag} {adirection}  "
                      f"RSI:{asig['rsi']['value']}  VOL:{asig['vol']['value']}x  "
                      f"({asig['confirmed_count']}/6)")
                total_signals += 1

                if not DRY_RUN and AUTO_TRADE:
                    if tf_key in TIMING_ONLY_TF:
                        print(f"  [{strategy_tag}] 5m는 초단타 보조 참고용 — 단독 자동매매 스킵")
                        _log_gate_block(symbol, tf_key, asig, adirection,
                                        "5m 초단타 보조 참고용 — 15m 이상 판단봉 필요",
                                        strategy=strategy_tag)
                        continue

                    # 전략/방향 없는 초기 학습 필터. 실제 차단은 _try_auto_trade에서 수행.
                    ok_trade, why = is_tradeable(symbol, tf_key)
                    if not ok_trade:
                        print(f"  [학습] {why} → 스킵")
                        _log_gate_block(symbol, tf_key, asig, adirection, why,
                                        strategy=strategy_tag)
                        continue

                    # 이미 오픈 포지션 있으면 스킵 (중복 방지)
                    from trade_router import has_open_position
                    if has_open_position(symbol):
                        print(f"  [{strategy_tag}] {symbol} 포지션 이미 있음 → 스킵")
                        _log_gate_block(symbol, tf_key, asig, adirection,
                                        "이미 오픈 포지션 있음", strategy=strategy_tag)
                        continue

                    # MTF 체크: 고거래량 현재봉 추가전략은 완전 역방향이어도 감액 진입 허용
                    mtf_a = check_mtf(symbol, tf_key, adirection)
                    mtf_reverse_override = {"allow": False, "risk_mult": 1.0, "note": ""}
                    if mtf_a["block"]:
                        mtf_reverse_override = _mtf_soft_override(
                            asig, mtf_a, tf_key, strategy_tag, adirection
                        )
                        if mtf_reverse_override["allow"]:
                            _mark_mtf_soft_override(asig, mtf_reverse_override)
                            print(f"  [MTF완화] {mtf_reverse_override['note']}")
                        else:
                            print(f"  [MTF] 역방향 → {strategy_tag} 스킵")
                            _log_gate_block(symbol, tf_key, asig, adirection,
                                            "MTF 역방향 소프트 허용 기준 미달", strategy=strategy_tag)
                            continue

                    asym_mult_a, asym_notes_a, _ = get_asymmetric_profile(
                        symbol, tf_key, strategy_tag, adirection
                    )
                    asym_live_a = _live_asymmetric_candidate(asig, tf_key, strategy_tag)
                    asym_candidate_a = bool(asym_mult_a > 1.0 or asym_live_a)
                    funding_override_mult = 1.0

                    # 펀딩비 게이트 — 비대칭 초고거래량 후보는 차단 대신 감액한다.
                    lctx_a = get_market_context(symbol, adirection)
                    if not lctx_a["favorable"]:
                        if asym_candidate_a:
                            funding_override_mult = ASYMMETRIC_FUNDING_OVERRIDE_MULT
                            asig["asymmetric_mode"] = True
                            print(
                                f"  [비대칭펀딩완화] {strategy_tag} {adirection} — "
                                f"{lctx_a['reason']} → 차단 대신 리스크×{funding_override_mult:.2f}"
                            )
                        else:
                            print(f"  [선행] 펀딩비 비우호 → {strategy_tag} 스킵")
                            _log_gate_block(symbol, tf_key, asig, adirection,
                                            f"펀딩비 비우호: {lctx_a['reason']}",
                                            strategy=strategy_tag)
                            continue

                    boost_a = (
                        MTF_POSITION_BOOST if mtf_a["strong"]
                        else mtf_reverse_override["risk_mult"] if mtf_reverse_override["allow"]
                        else 1.0
                    )
                    boost_a *= (1.20 if _ad_ts == 2 else 1.0)  # 이중 추세 보너스
                    boost_a *= funding_override_mult
                    scalp_a = tf_key in SCALP_TF
                    print(f"  [{strategy_tag}] 자동매매 진입  레버:{_get_leverage(asig['strength'], tf_key)}x  부스트:{boost_a:.2f}x")
                    _try_auto_trade(symbol, tf_key, [asig], current_price,
                                    scalp=scalp_a, mtf_boost=boost_a,
                                    premium_mtf=mtf_a["strong"])

            # ══════════════════════════════════════════════════════════════
            # 돌파 추세 매매 (Breakout) + 불타기 (Pyramid)
            # 다이버전스/추가전략과 무관하게 항상 체크
            # ══════════════════════════════════════════════════════════════
            if AUTO_TRADE and not DRY_RUN:
                from trade_router import (has_open_position, can_pyramid,
                                    get_open_positions_detail)

                # ── 1. 돌파 신호 체크 ────────────────────────────────────
                # 1h 이상 TF에서만 돌파 매매 (노이즈 방지)
                if tf_key not in LOW_NOISE_TF and not has_open_position(symbol):
                    bsig = detect_breakout(df)
                    if bsig:
                        bsig["current_price"] = current_price
                        _attach_hyperliquid_lead(bsig, hl_lead)
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

    if bb_mid_watch:
        print(f"  📌 BB중단 내림롱 관심종목: {', '.join(bb_mid_watch[:12])}")

    if total_signals == 0 and not DRY_RUN and not fast_mode:
        send_signal(build_summary(total_scanned))

    print(f"\n{'='*55}")
    print(f"  ✅ 완료 — {total_scanned}회 검사  |  신호 {total_signals}개")
    if AUTO_TRADE:
        print(f"  🤖 자동매매 모드 활성")
    print(f"{'='*55}")

    if (
        not DRY_RUN
        and not fast_mode
        and runtime_context()["state_namespace"] == "bybit"
    ):
        maybe_send_bithumb_ma200_alert(send_market_screening)
        maybe_send_krx_ma200_alert(send_market_screening)
        maybe_send_kis_api_review_reminder(send)


if __name__ == "__main__":
    if not wait_for_network():
        print("네트워크 연결 실패 — 종료")
        sys.exit(1)
    if BITHUMB_ONLY:
        maybe_send_bithumb_ma200_alert(
            send_market_screening, dry_run=DRY_RUN, force=True, limit=SCREEN_LIMIT
        )
        sys.exit(0)
    if KRX_ONLY:
        maybe_send_krx_ma200_alert(
            send_market_screening, dry_run=DRY_RUN, force=True, limit=SCREEN_LIMIT
        )
        sys.exit(0)
    scan()
