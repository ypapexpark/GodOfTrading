import json

import config
import formatter
import main
import trader


def _ema_signal():
    return {
        "signal_type": "ema_long",
        "strategy": "EMA눌림목+거래량급등",
        "strength": "VERY STRONG 🔥",
        "confirmed_count": 6,
        "bars_ago": 0,
        "ema_trend": 1,
        "atr": 1.0,
        "pivot_price": 98.0,
        "is_divergence": False,
        "vol": {"ok": True, "value": 20.0},
    }


def test_v6_ema_asymmetric_still_disabled_but_mtf_soft_quality_ok():
    signal = _ema_signal()
    assert main._live_asymmetric_candidate(signal, "15m", signal["strategy"]) is False
    # 저품질 conf/vol 부족 → 여전히 거부
    weak = dict(signal)
    weak["confirmed_count"] = 3
    weak["vol"] = {"ok": True, "value": 0.5}
    denied = main._mtf_soft_override(
        weak,
        {"block": True, "strong": False, "score": 0, "max_score": 3},
        "15m",
        signal["strategy"],
        "LONG",
    )
    assert denied["allow"] is False
    # 고품질 LONG 15m → soft 허용
    decision = main._mtf_soft_override(
        signal,
        {"block": True, "strong": False, "score": 0, "max_score": 3},
        "15m",
        signal["strategy"],
        "LONG",
    )
    assert decision["allow"] is True
    assert decision["risk_mult"] == config.EMA_LIVE_MTF_SOFT_MULT


def test_v6_targets_report_structural_stop_in_atr():
    target = formatter.calc_targets(
        _ema_signal(), 100.0, "LONG", leverage=5, tf_key="15m", strength="VERY STRONG"
    )
    assert target is not None
    assert target["sl_atr"] == 3.5
    assert target["sl_pct"] == 3.5


def test_candidate_log_dedupes_same_signal_bar(tmp_path, monkeypatch):
    path = tmp_path / "candidates.jsonl"
    monkeypatch.setattr(trader, "CANDIDATE_FILE", path)
    monkeypatch.setattr(trader, "_CANDIDATE_EVENT_KEYS", None)
    monkeypatch.setattr(trader.time, "time", lambda: 1_800_000_123.0)

    kwargs = dict(
        symbol="BTC/USDT",
        tf_key="15m",
        strategy="EMA눌림목+돌파",
        direction="LONG",
        strength="VERY STRONG",
        reason="MTF 전 상위봉 역방향",
        signal_type="ema_long",
        price=100.0,
    )
    cid1 = trader.log_trade_candidate(status="blocked", **kwargs)
    cid2 = trader.log_trade_candidate(status="blocked", **kwargs)
    trader.log_trade_candidate(status="opened", **kwargs)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert cid1 == cid2
    assert [row["status"] for row in rows] == ["blocked", "opened"]
    assert rows[0]["candidate_id"] == rows[1]["candidate_id"]


def test_v6_live_scope_ema_core_and_binance_canary():
    assert config.LIVE_AUTO_TRADE_TIMEFRAMES == {"15m"}
    assert config.EMA_MACD_HARD_BLOCK is False
    assert config.BINANCE_CANARY_LIVE_ENABLED is True
    assert config.MAX_ENTRY_SL_PCT == 5.0
    assert config.MAX_ENTRY_SL_ATR == 3.0
    assert "EMA눌림목+거래량급등" in config.AUTO_TRADE_STRATEGY_WHITELIST
    assert config.EMA_COMPOUND_HTF_DOUBLE_BLOCK is True


def test_ema_lower_tf_soft_config_is_bounded():
    """경미 soft는 감액만 하고, 과열 한도보다 작아야 한다."""
    assert config.EMA_LIVE_LOWER_TF_SOFT_ENABLED is True
    assert 0.4 <= config.EMA_LIVE_LOWER_TF_SOFT_MULT <= 0.85
    assert config.EMA_LIVE_LOWER_TF_MAX_VWAP_EXT_PCT < config.EXTENSION_HARD_BLOCK_PCT
    assert config.EMA_LIVE_COUNTERTREND_MIN_CONFIRMED == 4
