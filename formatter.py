"""텔레그램 메시지 포맷 — 다이버전스 신뢰도 + ELITE 등급 + 강도별 TP 전략."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from config import ROUND_TRIP_FEE, SL_ATR_MULT, LEVERAGE_MAP, MIN_GROSS_PCT, TP_BY_STRENGTH

KST = timezone(timedelta(hours=9))

SIGNAL_META = {
    "bullish": {
        "emoji": "📈", "label": "상승 다이버전스",
        "desc": "가격 저점↓ + 지표 저점↑ → 반등 가능성",
        "direction": "LONG",
    },
    "bearish": {
        "emoji": "📉", "label": "하락 다이버전스",
        "desc": "가격 고점↑ + 지표 고점↓ → 하락 가능성",
        "direction": "SHORT",
    },
    "hidden_bullish": {
        "emoji": "🔵", "label": "히든 불리시 다이버전스",
        "desc": "가격 저점↑ + 지표 저점↓ → 상승 추세 지속",
        "direction": "LONG",
    },
    "hidden_bearish": {
        "emoji": "🟠", "label": "히든 베어리시 다이버전스",
        "desc": "가격 고점↓ + 지표 고점↑ → 하락 추세 지속 (분산 구간)",
        "direction": "SHORT",
    },
    # ── 추가 전략 신호 ────────────────────────────────────────────────────────
    "rsi_long": {
        "emoji": "⚡", "label": "RSI 과매도 반전",
        "desc": "RSI 극단 과매도 + 볼륨 클라이맥스 → 단기 반등",
        "direction": "LONG",
    },
    "rsi_short": {
        "emoji": "⚡", "label": "RSI 과매수 반전",
        "desc": "RSI 극단 과매수 + 볼륨 클라이맥스 → 단기 하락",
        "direction": "SHORT",
    },
    "ema_long": {
        "emoji": "🔄", "label": "EMA 눌림목 LONG",
        "desc": "상승 추세 중 EMA20 되돌림 → 추세 재개 진입",
        "direction": "LONG",
    },
    "ema_short": {
        "emoji": "🔄", "label": "EMA 반등매도 SHORT",
        "desc": "하락 추세 중 EMA20 반등 → 추세 재개 진입",
        "direction": "SHORT",
    },
    "bb_squeeze_long": {
        "emoji": "💥", "label": "BB 스퀴즈 돌파 LONG",
        "desc": "BB 압축(에너지 축적) → 상단 돌파 = 추세 가속 (VCP 원리)",
        "direction": "LONG",
    },
    "bb_squeeze_short": {
        "emoji": "💥", "label": "BB 스퀴즈 돌파 SHORT",
        "desc": "BB 압축(에너지 축적) → 하단 돌파 = 추세 가속 (VCP 원리)",
        "direction": "SHORT",
    },
    "micro_breakout_long": {
        "emoji": "🚀", "label": "마이크로 구조 돌파 LONG",
        "desc": "최근 구조 고점 돌파 + 추세/거래량 확인 → 단기 추세 합류",
        "direction": "LONG",
    },
    "micro_breakout_short": {
        "emoji": "🚀", "label": "마이크로 구조 돌파 SHORT",
        "desc": "최근 구조 저점 이탈 + 추세/거래량 확인 → 단기 추세 합류",
        "direction": "SHORT",
    },
}

TF_ENTRY = {
    "5m":  ("15분봉", "15m"),
    "15m": ("1시간봉", "1h"),
    "1h":  ("4시간봉", "4h"),
    "4h":  ("일봉",    "1d"),
    "1d":  ("주봉",    "1w"),
}

TF_NOTE = {
    "5m":  "🔎 타점 확인 전용 — 15m 이상 신호 없이는 단독 자동매매 금지",
    "15m": "⚡ 최소 자동매매 판단봉 — 5m로 타점 확인 후 진입",
    "1h":  "✅ 주요 진입 타임프레임",
    "4h":  "✅ 메인 포지션 타임프레임",
    "1d":  "✅ 대형 포지션 타임프레임",
}

EMA_NOTE = {
    1:  "🟢 EMA 추세: 상승 (EMA20 &gt; EMA50) — 롱 유리",
    -1: "🔴 EMA 추세: 하락 (EMA20 &lt; EMA50) — 숏 유리",
    0:  "⚪ EMA 추세: 중립 — 방향성 불명확",
}

STRENGTH_NOTE = {
    7: "6개 다이버전스 + 거래량 확인 → ELITE 최고 신뢰도",
    6: "5개 이상 다이버전스 확인 → 매우 높은 신뢰도",
    5: "4개 이상 다이버전스 확인 → 높은 신뢰도",
    4: "3개 이상 다이버전스 + 거래량 확인 → 조건부 신뢰",
    3: "3개 다이버전스 확인 → 후보 관찰",
}

# 강도별 TP 아이콘 + 전략 라벨
TP_STRATEGY_LABEL = {
    "MODERATE":    "⚡ 단일 TP — 빠른 확정",
    "STRONG":      "⚡ 2분할 — TP1 확정 + TP2 홈런",
    "VERY STRONG": "🔥 3분할 — TP1 비용회수 + TP2 코어 + TP3 홈런",
    "ELITE":       "💎 3분할 — TP1 최소확정 + TP2 코어 + TP3 대형홈런 (수익극대화)",
}

TP_ICONS = {
    1: ["🎯"],
    2: ["🥇", "🏆"],
    3: ["🥇", "🏆", "💰"],
}


def _raw_strength(strength: str) -> str:
    return strength.replace(" 💎", "").replace(" 🔥", "").replace(" ⚡", "")


def _get_leverage(strength: str, tf_key: str) -> int:
    return LEVERAGE_MAP.get((_raw_strength(strength), tf_key), 2)


def _round_price(price: float) -> float:
    """코인 가격대별 반올림. 저가 코인에서 2자리 반올림은 TP/SL을 망가뜨린다."""
    p = abs(float(price))
    if p >= 1000:
        digits = 2
    elif p >= 100:
        digits = 3
    elif p >= 1:
        digits = 4
    elif p >= 0.01:
        digits = 6
    else:
        digits = 8
    return round(float(price), digits)


def _calc_targets(sig: dict, current_price: float,
                  direction: str, leverage: int,
                  tf_key: str, strength: str = "STRONG") -> dict | None:
    atr = sig["atr"]
    if atr <= 0 or current_price <= 0:
        return None

    raw         = _raw_strength(strength)
    tp_plan     = TP_BY_STRENGTH.get(raw, TP_BY_STRENGTH["STRONG"])
    fee_total   = ROUND_TRIP_FEE * leverage
    min_gross   = MIN_GROSS_PCT.get(tf_key, 2.0) / 100

    if direction == "LONG":
        entry = _round_price(current_price - 0.15 * atr)
        sl    = _round_price(sig["pivot_price"] - SL_ATR_MULT * atr)
        risk  = entry - sl
        if risk <= 0:
            return None

        tps = []
        for tp_def in tp_plan:
            tp_price_atr = entry + tp_def["atr_mult"] * atr
            tp_price_min = entry * (1 + min_gross + fee_total)
            tp_price = _round_price(max(tp_price_atr, tp_price_min))
            gain      = tp_price - entry
            gross_pct = round(gain / entry * 100, 2)
            net_pct   = round(gross_pct - fee_total * 100, 2)
            rr        = round(gain / risk, 1) if risk > 0 else 0
            tps.append({"price": tp_price, "pct": tp_def["pct"],
                        "gross_pct": gross_pct, "net_pct": net_pct, "rr": rr})

    else:  # SHORT
        entry = _round_price(current_price + 0.15 * atr)
        sl    = _round_price(sig["pivot_price"] + SL_ATR_MULT * atr)
        risk  = sl - entry
        if risk <= 0:
            return None

        tps = []
        for tp_def in tp_plan:
            tp_price_atr = entry - tp_def["atr_mult"] * atr
            tp_price_min = entry * (1 - min_gross - fee_total)
            tp_price = _round_price(min(tp_price_atr, tp_price_min))
            gain      = entry - tp_price
            gross_pct = round(gain / entry * 100, 2)
            net_pct   = round(gross_pct - fee_total * 100, 2)
            rr        = round(gain / risk, 1) if risk > 0 else 0
            tps.append({"price": tp_price, "pct": tp_def["pct"],
                        "gross_pct": gross_pct, "net_pct": net_pct, "rr": rr})

    return {
        "entry": entry, "sl": sl,
        "sl_pct": round(abs(entry - sl) / entry * 100, 2),
        "tps": tps,
        "fee_pct": round(fee_total * 100, 3),
    }


def calc_targets(sig: dict, current_price: float,
                 direction: str, leverage: int,
                 tf_key: str, strength: str = "STRONG") -> dict | None:
    """main.py / trader.py에서 직접 호출 가능한 공개 타겟 계산 함수."""
    return _calc_targets(sig, current_price, direction, leverage, tf_key, strength)


def build_alert(symbol: str, tf_label: str, tf_key: str,
                signals: list, current_price: float,
                mtf_info: dict | None = None) -> str:
    now_kst = datetime.now(KST).strftime("%m/%d %H:%M KST")
    coin = symbol.split("/")[0]

    best      = max(signals, key=lambda x: x["confirmed_count"])
    meta      = SIGNAL_META.get(best["signal_type"], SIGNAL_META["bullish"])
    direction = meta["direction"]
    strength  = best["strength"]
    confirmed = best["confirmed_count"]
    leverage  = _get_leverage(strength, tf_key)
    t         = _calc_targets(best, current_price, direction, leverage, tf_key, strength)
    ema_trend = best.get("ema_trend", 0)
    raw       = _raw_strength(strength)
    entry_tf_label, _ = TF_ENTRY.get(tf_key, (tf_label, tf_key))
    icons     = TP_ICONS.get(len(t["tps"]) if t else 1, ["🎯"])

    lines = [
        f"🚨 <b>[CryptoSignal] {coin} {tf_label}  |  {strength}</b>",
        f"💰 현재가: <b>${current_price:,.4f}</b>   |   {now_kst}",
        "",
    ]

    for s in signals:
        m = SIGNAL_META.get(s["signal_type"], meta)
        q = s.get("divergence_quality", {})
        div_count = s.get("divergence_count", s["confirmed_count"])
        max_div = q.get("max_divergence", 6)
        max_conf = q.get("max_confirmed", 7)
        cci = s.get("cci", {"ok": False, "value": 0.0})
        lines += [
            f"{m['emoji']} <b>{m['label']}</b>  "
            f"(Divergence {div_count}/{max_div} | Total {s['confirmed_count']}/{max_conf})",
            f"   {m['desc']}",
            f"   {'✅' if s['rsi']['ok']  else '❌'} RSI      {s['rsi']['value']}",
            f"   {'✅' if cci['ok']       else '❌'} CCI      {cci['value']}",
            f"   {'✅' if s['macd']['ok'] else '❌'} MACD     {'+' if s['macd']['value'] >= 0 else ''}{s['macd']['value']:.4f}",
            f"   {'✅' if s['obv']['ok']  else '❌'} OBV      {'매집 감지 ✓' if s['obv']['ok'] else '미확인'}",
            f"   {'✅' if s['srsi']['ok'] else '❌'} StochRSI {s['srsi']['value']}",
            f"   {'✅' if s['vol']['ok']  else '❌'} Volume   {s['vol']['value']}x 평균거래량",
            f"   {'✅' if s.get('cvd', {}).get('ok') else '❌'} CVD      {'확인' if s.get('cvd', {}).get('ok') else '미확인'}",
        "",
        ]
        if q.get("note"):
            lines += [f"   신뢰도 기준: {q['note']}", ""]

    lines += [
        f"📊 <b>신호 강도: {strength}</b>   |   ⚙️ 추천 레버리지: <b>{leverage}x</b>",
        f"   {STRENGTH_NOTE.get(confirmed, '')}",
        f"   {TF_NOTE.get(tf_key, '')}",
        f"   {EMA_NOTE.get(ema_trend, '')}",
        f"   💸 실질 수수료: {t['fee_pct'] if t else '-'}% (왕복 × {leverage}x)",
        "",
    ]

    if t:
        strategy_label = TP_STRATEGY_LABEL.get(raw, "")
        lines += [
            f"📐 <b>매매 제안 ({direction}) — {tf_label} 신호 &lt; {entry_tf_label} 진입</b>",
            f"   전략: {strategy_label}",
            "",
            f"   💵 진입가:  ${t['entry']:,.4f}",
            f"   🛑 손절가:  ${t['sl']:,.4f}  (-{t['sl_pct']}%)",
            "",
        ]
        for i, tp in enumerate(t["tps"]):
            icon = icons[i] if i < len(icons) else "🎯"
            label = f"TP{i+1}" if len(t["tps"]) > 1 else "TP"
            lines.append(
                f"   {icon} {label} [{tp['pct']}%]  ${tp['price']:,.4f}"
                f"  (+{tp['gross_pct']}% / 순익 <b>+{tp['net_pct']}%</b>)  R:R 1:{tp['rr']}"
            )

    # MTF 확인 결과 표시
    if mtf_info and mtf_info.get("details", []) != ["최상위봉 — MTF 불필요"]:
        score = mtf_info["score"]
        n     = mtf_info["max_score"]
        if mtf_info["strong"]:
            mtf_label = f"✅✅ <b>전 TF 정렬</b> ({score}/{n}) — 최고 진입 기회 🔥"
        elif mtf_info["block"]:
            mtf_label = f"⛔ <b>전 TF 역방향</b> ({score}/{n}) — 자동매매 차단"
        else:
            mtf_label = f"⚡ 부분 정렬 ({score}/{n})"
        lines += [
            "",
            f"🔭 <b>MTF 확인</b>: {mtf_label}",
        ]
        for d in mtf_info["details"]:
            lines.append(f"   {d}")

    lines += [
        "",
        f'⏱ {tf_label} ({tf_key})   🔗 <a href="https://www.bybit.com/trade/usdt/{coin}USDT">Bybit 차트</a>',
    ]

    return "\n".join(lines)


def build_summary(scanned: int) -> str:
    now_kst = datetime.now(KST).strftime("%m/%d %H:%M KST")
    return (
        f"🤖 <b>CryptoSignal 스캔 완료</b> — {now_kst}\n"
        f"11개 심볼 × 5 타임프레임 ({scanned}회) — 유효 신호 없음"
    )
