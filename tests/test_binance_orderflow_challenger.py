from __future__ import annotations

import time

from binance_orderflow_challenger import (
    FlowSnapshot,
    OrderFlowBook,
    PAPER_TP1_SIZE,
    _default_state,
    _graduation,
    _live_permission,
    _manage_paper,
    _maybe_notify_graduation_transition,
    _paper_open,
    _universe,
    build_plan,
    summarize_persistence,
)


def test_raw_trade_event_populates_signed_taker_flow():
    book = OrderFlowBook()
    book.on_payload({
        "stream": "testusdt@trade",
        "data": {
            "e": "trade", "s": "TESTUSDT", "p": "100", "q": "2", "m": False,
        },
    })
    book.on_payload({
        "stream": "testusdt@bookTicker",
        "data": {
            "e": "bookTicker", "s": "TESTUSDT", "b": "99.9", "a": "100.1",
            "B": "10", "A": "5",
        },
    })
    book.on_payload({
        "stream": "testusdt@depth5@100ms",
        "data": {
            "e": "depthUpdate", "s": "TESTUSDT",
            "b": [["99.9", "10"]], "a": [["100.1", "5"]],
        },
    })
    snapshot = book.snapshot("TESTUSDT")
    assert snapshot is not None
    assert snapshot.buy_quote == 200
    assert snapshot.sell_quote == 0
    assert snapshot.flow_imbalance == 1
    assert snapshot.book_imbalance > 0


def test_universe_uses_only_active_usdt_perpetuals_and_volume_rank(monkeypatch):
    import binance_orderflow_challenger as module

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    exchange_info = {"symbols": [
        {"symbol": "AAAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        {"symbol": "BBBUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        {"symbol": "OLDUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        {"symbol": "DELIVERYUSDT", "status": "TRADING", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT"},
    ]}
    tickers = [
        {"symbol": "AAAUSDT", "lastPrice": "1", "quoteVolume": "100"},
        {"symbol": "BBBUSDT", "lastPrice": "2", "quoteVolume": "200"},
        {"symbol": "OLDUSDT", "lastPrice": "3", "quoteVolume": "999"},
    ]
    responses = iter((Response(exchange_info), Response(tickers)))
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: next(responses))
    raw, canonical, volumes = _universe()
    assert raw == ["BBBUSDT", "AAAUSDT"]
    assert canonical["BBBUSDT"] == "BBB/USDT"
    assert volumes["AAAUSDT"] == 100


def _context(direction: str = "LONG") -> dict:
    return {
        "direction": direction,
        "last_5m": 100.0,
        "ema9": 100.0,
        "ema9_prev": 99.9 if direction == "LONG" else 100.1,
        "vwap20": 100.0,
        "atr5": 1.0,
        "atr_pct": 1.0,
        "recent_high": 101.0,
        "recent_low": 99.0,
        "volume_ratio": 1.5,
        "signal_bar": "2026-07-19 00:00:00+00:00",
        "return_15m_pct": 0.35,
        "breakout_valid": True,
        "breakout_level": 100.0,
        "breakout_bar": "2026-07-19 00:00:00+00:00",
        "breakout_age_bars": 1,
        "breakout_volume_ratio": 1.8,
    }


def _flow(direction: str = "LONG", price: float = 100.1) -> FlowSnapshot:
    long = direction == "LONG"
    return FlowSnapshot(
        symbol="TESTUSDT",
        price=price,
        bid=price - 0.01,
        ask=price + 0.01,
        spread_pct=0.02,
        buy_quote=80_000 if long else 20_000,
        sell_quote=20_000 if long else 80_000,
        flow_imbalance=0.60 if long else -0.60,
        book_imbalance=0.30 if long else -0.30,
        trade_quote=100_000,
        age_seconds=0.1,
    )


def _persistence() -> dict:
    return {
        "samples": 4,
        "avg_flow": 0.55,
        "min_flow": 0.35,
        "avg_book": 0.22,
        "positive_book_samples": 4,
        "min_trade_quote": 80_000,
    }


def _market() -> dict:
    return {"trend_long": True, "return_15m_pct": 0.05}


def _build(symbol: str = "TEST/USDT", flow: FlowSnapshot | None = None):
    return build_plan(
        symbol,
        flow or _flow(),
        _context(),
        min_flow_quote=10_000,
        persistence=_persistence(),
        market=_market(),
    )


def test_build_plan_requires_breakout_retest_persistent_flow_and_market():
    plan, reason = _build()
    assert reason == "ok"
    assert plan is not None and plan.eligible
    assert plan.direction == "LONG"
    assert plan.stop < plan.entry < plan.tps[0]["price"] < plan.tps[1]["price"]
    assert plan.required_win_rate < 0.50

    bad = FlowSnapshot(**{**_flow().__dict__, "flow_imbalance": -0.30})
    blocked, reason = _build(flow=bad)
    assert blocked is None
    assert reason == "current_flow"

    blocked, reason = build_plan(
        "TEST/USDT", _flow(), _context(), min_flow_quote=10_000,
        persistence={}, market=_market(),
    )
    assert blocked is None
    assert reason == "persistence"

    weak_context = {**_context(), "return_15m_pct": 0.10}
    blocked, reason = build_plan(
        "TEST/USDT", _flow(), weak_context, min_flow_quote=10_000,
        persistence=_persistence(), market=_market(),
    )
    assert blocked is None
    assert reason == "relative_strength"


def test_persistence_uses_only_recent_samples():
    summary = summarize_persistence(
        [
            (60.0, -1.0, -1.0, 1.0),
            (75.0, 0.2, 0.1, 20_000.0),
            (85.0, 0.4, 0.2, 30_000.0),
            (99.0, 0.6, 0.3, 40_000.0),
        ],
        100.0,
    )
    assert summary["samples"] == 3
    assert round(summary["avg_flow"], 6) == 0.4
    assert summary["min_trade_quote"] == 20_000


class _Feed:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)

    def snapshot(self, _raw, _now):
        return next(self.snapshots)


def test_paper_mirror_accounts_for_costs_and_partial_profit(monkeypatch, tmp_path):
    import binance_orderflow_challenger as module

    monkeypatch.setattr(module, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    state = _default_state()
    plan, _ = _build()
    assert plan is not None
    now = time.time()
    assert _paper_open(state, "TEST/USDT", plan, now)
    position = next(iter(state["positions"].values()))
    tp1 = position["tp1"]
    tp2 = position["tp2"]
    feed = _Feed([
        _flow(price=tp1 + 0.02),
        _flow(price=tp2 + 0.02),
    ])
    assert not _manage_paper(state, feed, {"TEST/USDT": "TESTUSDT"}, now + 5)
    position = next(iter(state["positions"].values()))
    assert position["tp1_done"]
    assert abs(position["remaining"] - (1 - PAPER_TP1_SIZE)) < 1e-9
    settled = _manage_paper(state, feed, {"TEST/USDT": "TESTUSDT"}, now + 10)
    assert len(settled) == 1
    assert settled[0]["net_usd"] > 0
    assert not state["positions"]


def test_paper_graduation_requires_50_robust_positive_cost_adjusted_closes(
    monkeypatch, tmp_path,
):
    import binance_orderflow_challenger as module

    monkeypatch.setattr(module, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    state = _default_state()
    pnls = [value for _ in range(20) for value in (1.0, -0.5)] + [1.0] * 10
    for pnl in pnls:
        module._journal("paper_close", net_usd=pnl)
    graduated, reasons = _graduation(state)
    assert graduated
    assert reasons == []
    assert _live_permission(state, armed=True) == (True, "paper_graduated")
    assert _live_permission(state, armed=False) == (False, "runtime_not_armed")


def test_graduation_transition_notifies_once_before_next_live_signal(
    monkeypatch, tmp_path,
):
    import binance_orderflow_challenger as module

    monkeypatch.setattr(module, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    delivered = []
    monkeypatch.setattr(module, "send_signal", lambda message: delivered.append(message) or True)
    state = _default_state()
    pnls = [value for _ in range(20) for value in (1.0, -0.5)] + [1.0] * 10
    for pnl in pnls:
        module._journal("paper_close", net_usd=pnl)
    assert _maybe_notify_graduation_transition(state)
    assert state["graduation_gate_status"] == "graduated"
    assert "승격 완료" in delivered[0]
    assert "사용자 확인 없이" in delivered[0]
    assert "다음 C1 v2 적합 신호" in delivered[0]
    assert not _maybe_notify_graduation_transition(state)
    assert len(delivered) == 1
