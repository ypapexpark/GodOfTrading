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


def test_v6_ema_never_uses_asymmetric_or_mtf_reverse_override():
    signal = _ema_signal()
    assert main._live_asymmetric_candidate(signal, "15m", signal["strategy"]) is False
    decision = main._mtf_soft_override(
        signal,
        {"block": True, "strong": False, "score": 0, "max_score": 3},
        "15m",
        signal["strategy"],
        "LONG",
    )
    assert decision["allow"] is False


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


def test_v6_live_scope_is_narrow_and_binance_is_shadow():
    assert config.LIVE_AUTO_TRADE_TIMEFRAMES == {"15m"}
    assert config.EMA_MACD_HARD_BLOCK is True
    assert config.BINANCE_CANARY_LIVE_ENABLED is False
    assert config.MAX_ENTRY_SL_PCT == 5.0
    assert config.MAX_ENTRY_SL_ATR == 3.0
