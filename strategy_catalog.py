"""Strategy family labels used by alerts, journals, and learning reports."""
from __future__ import annotations

CORE_QUANT_FAMILY = "GOT Core Quant"
ASYMMETRIC_FAMILY = "GOT Asymmetric Edge"
BTC_SYNC_FAMILY = "Strategy 3 / BTC Sync"
BTC_MACRO_FAMILY = "Strategy 4 / BTC Macro"
HYPERLIQUID_LEAD_FAMILY = "Strategy 5 / Hyperliquid Lead"

CORE_STRATEGIES = {
    "multi_factor_confluence": {
        "label": "멀티팩터 컨플루언스",
        "desc": "다이버전스, MTF, EMA, 거래량, CVD/OBV, 구조레벨을 합산하는 기본 코어 전략",
    },
    "hidden_continuation": {
        "label": "히든 추세지속",
        "desc": "히든 다이버전스로 상위 추세의 재개 구간을 노리는 전략",
    },
    "countertrend_reversal": {
        "label": "역추세 반전",
        "desc": "상위 추세와 반대지만 고확신 반전 신호가 겹칠 때 소액으로 노리는 전략",
    },
    "ema_pullback": {
        "label": "EMA 눌림목",
        "desc": "EMA20/50 추세 방향 눌림 이후 재개를 노리는 전략",
    },
    "rsi_climax_reversal": {
        "label": "RSI 클라이맥스 반전",
        "desc": "RSI 극단값과 거래량 클라이맥스 이후 단기 반전을 노리는 전략",
    },
    "structure_breakout": {
        "label": "구조 돌파",
        "desc": "저항/지지 구조 돌파와 거래량 확인으로 추세 가속에 합류하는 전략",
    },
    "micro_breakout": {
        "label": "마이크로 돌파",
        "desc": "하위 구조 고점/저점 돌파로 빠른 추세 합류를 노리는 전략",
    },
    "bb_squeeze_breakout": {
        "label": "BB 스퀴즈 돌파",
        "desc": "볼린저밴드 압축 이후 변동성 확장 구간에 합류하는 전략",
    },
    "bb_mid_pullback_long": {
        "label": "BB 중단 내림롱",
        "desc": "주봉+3일봉 BB 중단 상방 유지 종목의 하위봉 눌림 반등을 노리는 전략",
    },
    "volume_momentum": {
        "label": "거래량 급등 추세",
        "desc": "뉴스성 수급처럼 거래량이 먼저 터지는 구간에서 추세 가속에 합류하는 전략",
    },
}

ASYMMETRIC_STRATEGY = {
    "label": "비대칭 러너",
    "desc": "승률보다 평균승리/평균손실/payoff 우위를 중시해 손절은 짧게, 잔량 목표는 길게 운용",
}

BTC_SYNC_STRATEGIES = {
    "btc_sync_momentum": {
        "label": "BTC 괴리 모멘텀",
        "desc": "BTC 베타 대비 초과 강세/약세가 거래량과 함께 이어지는 구간을 추세 동행 매매",
    },
    "btc_sync_reversion": {
        "label": "BTC 괴리 평균회귀",
        "desc": "BTC 베타 대비 과도한 이탈이 반대 캔들로 되돌기 시작할 때 스프레드 축소를 노리는 매매",
    },
}

BTC_MACRO_STRATEGY = {
    "label": "BTC 월봉 숏",
    "desc": "BTC 월봉/주봉/일봉 하락 우위에서 롱을 차단하고 숏 신호만 우대하는 전용 전략",
}

HYPERLIQUID_LEAD_STRATEGY = {
    "label": "HL 선행수급",
    "desc": "Hyperliquid의 거래량/OI/단기 모멘텀을 선행 수급 레이더로 활용해 Bybit 매매 후보를 보강",
}


def classify_strategy(strategy: str = "", signal_type: str = "",
                      is_divergence: bool = True, direction: str = "",
                      entry_context: dict | None = None,
                      asymmetric: bool = False) -> dict:
    """Return a stable family/sub-strategy profile for the current signal."""
    strategy = strategy or ""
    signal_type = signal_type or ""
    ctx = entry_context or {}
    reasons = " | ".join(str(r) for r in ctx.get("reasons", []) or [])
    asymmetric = bool(asymmetric or ctx.get("asymmetric_mode"))

    if (
        "BTC Sync" in strategy
        or "BTC괴리" in strategy
        or signal_type.startswith("btc_sync")
        or ctx.get("strategy_mode") == "btc_sync"
    ):
        sync_key = "btc_sync_reversion" if (
            "Reversion" in strategy
            or "평균회귀" in strategy
            or "reversion" in signal_type
            or (ctx.get("btc_sync_snapshot") or {}).get("sync_mode") == "reversion"
        ) else "btc_sync_momentum"
        meta = BTC_SYNC_STRATEGIES[sync_key]
        return {
            "family_key": "btc_sync",
            "family_label": BTC_SYNC_FAMILY,
            "strategy_key": sync_key,
            "strategy_label": meta["label"],
            "strategy_desc": meta["desc"],
        }

    if (
        "BTC Macro" in strategy
        or "BTC월봉숏" in strategy
        or "BTC 월봉 숏" in strategy
        or signal_type.startswith("btc_macro_short")
        or ctx.get("strategy_mode") == "btc_macro_short"
    ):
        return {
            "family_key": "btc_macro_short",
            "family_label": BTC_MACRO_FAMILY,
            "strategy_key": "btc_monthly_short",
            "strategy_label": BTC_MACRO_STRATEGY["label"],
            "strategy_desc": BTC_MACRO_STRATEGY["desc"],
        }

    if (
        "Hyperliquid" in strategy
        or "HL선행" in strategy
        or signal_type.startswith("hyperliquid_lead")
        or ctx.get("strategy_mode") == "hyperliquid_lead"
    ):
        return {
            "family_key": "hyperliquid_lead",
            "family_label": HYPERLIQUID_LEAD_FAMILY,
            "strategy_key": "hyperliquid_lead_flow",
            "strategy_label": HYPERLIQUID_LEAD_STRATEGY["label"],
            "strategy_desc": HYPERLIQUID_LEAD_STRATEGY["desc"],
        }

    if asymmetric:
        return {
            "family_key": "asymmetric_edge",
            "family_label": ASYMMETRIC_FAMILY,
            "strategy_key": "asymmetric_runner",
            "strategy_label": ASYMMETRIC_STRATEGY["label"],
            "strategy_desc": ASYMMETRIC_STRATEGY["desc"],
        }

    key = "multi_factor_confluence"
    if "BB중단" in strategy or signal_type == "bb_mid_pullback_long":
        key = "bb_mid_pullback_long"
    elif "거래량급등" in strategy or signal_type.startswith("volume_momentum"):
        key = "volume_momentum"
    elif strategy == "돌파" or signal_type.startswith("breakout"):
        key = "structure_breakout"
    elif "마이크로" in strategy or signal_type.startswith("micro_breakout"):
        key = "micro_breakout"
    elif "BB스퀴즈" in strategy or signal_type.startswith("bb_squeeze"):
        key = "bb_squeeze_breakout"
    elif "EMA" in strategy or signal_type.startswith("ema_"):
        key = "ema_pullback"
    elif "RSI" in strategy or signal_type.startswith("rsi_"):
        key = "rsi_climax_reversal"
    elif is_divergence and signal_type.startswith("hidden_"):
        key = "hidden_continuation"
    elif is_divergence and signal_type in {"bullish", "bearish"} and "역추세" in reasons:
        key = "countertrend_reversal"

    meta = CORE_STRATEGIES[key]
    return {
        "family_key": "core_quant",
        "family_label": CORE_QUANT_FAMILY,
        "strategy_key": key,
        "strategy_label": meta["label"],
        "strategy_desc": meta["desc"],
    }


def format_profile(profile: dict) -> str:
    if not profile:
        return CORE_QUANT_FAMILY
    return f"{profile.get('family_label', CORE_QUANT_FAMILY)} / {profile.get('strategy_label', '-')}"
