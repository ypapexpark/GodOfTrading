from __future__ import annotations

from pathlib import Path

import telegram_engine_commands as module


def test_performance_includes_cost_after_pnl_and_r_multiple():
    rows = [
        {"pnl_usd": 2.0, "est_sl_loss": 1.0},
        {"pnl_usd": -1.0, "est_sl_loss": 1.0},
        {"pnl_usd": 0.0, "est_sl_loss": 1.0},
    ]
    perf = module._performance(rows, reference_equity=100.0, open_count=2)
    assert perf["wins"] == 1
    assert perf["losses"] == 1
    assert perf["be"] == 1
    assert perf["net"] == 1.0
    assert perf["pf"] == 2.0
    assert perf["contribution_pct"] == 1.0
    assert round(perf["avg_r"], 6) == round(1 / 3, 6)
    assert perf["open"] == 2


def test_dispatch_supports_aliases_and_never_exposes_order_command():
    assert "GodOfTrading" in module.dispatch("/help")
    assert "엔진" in module.dispatch("/engine c1")
    assert module.dispatch("일반 메시지") is None
    response = module.dispatch("/buy BTC")
    assert "조회 명령" in response
    assert "주문을 낼 수 없습니다" in response


def test_results_group_live_engines(monkeypatch, tmp_path: Path):
    import json

    path = tmp_path / "trade.json"
    path.write_text(json.dumps({
        "equity_start": 100.0,
        "positions": {},
        "trade_history": [
            {"strategy": module.C1_STRATEGY, "status": "win", "pnl_usd": 2.0},
            {"strategy": module.C1_STRATEGY, "status": "loss", "pnl_usd": -1.0},
            {"strategy": module.D2_STRATEGY, "status": "loss", "pnl_usd": -3.0},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(module, "BINANCE_TRADE_STATE", path)
    monkeypatch.setattr(module, "BYBIT_TRADE_STATE", path)
    text = module.build_results("c1")
    assert "1W/1L" in text
    assert "PF 2.00" in text
    assert "+$1.00" in text
    assert "C1 v1 LIVE (폐기)" in text


def test_journal_performance_filters_policy(monkeypatch, tmp_path: Path):
    import json

    journal = tmp_path / "journal.jsonl"
    journal.write_text("\n".join([
        json.dumps({"event": "paper_close", "policy": "v1", "net_usd": -5}),
        json.dumps({"event": "paper_close", "policy": "v2", "net_usd": 2}),
    ]) + "\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "policy": "v2", "initial_bankroll": 1000, "positions": {"x": {}},
    }), encoding="utf-8")
    perf = module._journal_perf(journal, "paper_close", state, policy="v2")
    assert perf["n"] == 1
    assert perf["net"] == 2
    assert perf["open"] == 1


def test_hyperliquid_command_reports_cost_adjusted_result_and_open_positions(
    monkeypatch, tmp_path: Path,
):
    import json

    journal = tmp_path / "hl.jsonl"
    journal.write_text("\n".join([
        json.dumps({
            "event": "settled", "pnl_usd": 2, "wallet": "0xabcdef123456789",
            "settle_reason": "whale_flat", "policy": module.HL_WHALE_POLICY,
        }),
        json.dumps({
            "event": "settled", "pnl_usd": -1, "wallet": "0xabcdef123456789",
            "settle_reason": "max_hold_48.0h", "policy": module.HL_WHALE_POLICY,
        }),
        json.dumps({
            "event": "settled", "pnl_usd": 0, "wallet": "0x999999999999999",
            "settle_reason": "whale_flat", "policy": module.HL_WHALE_POLICY,
        }),
    ]) + "\n", encoding="utf-8")
    state = tmp_path / "hl-state.json"
    state.write_text(json.dumps({
        "bankroll": 1001,
        "wallets": {"a": {}, "b": {}},
        "open_positions": [{
            "direction": "LONG", "coin": "BTC", "unrealized_pnl": 0.5,
            "policy": module.HL_WHALE_POLICY,
        }],
        "policy_bankrolls": {module.HL_WHALE_POLICY: 1001},
        "last_scan": {"time": "2026-07-19 20:00 KST", "wallets": 2},
    }), encoding="utf-8")
    monkeypatch.setattr(module, "HL_WHALE_JOURNAL", journal)
    monkeypatch.setattr(module, "HL_WHALE_STATE", state)
    text = module.dispatch("/hyperliquid")
    assert "1W/1L/1BE" in text
    assert "PF 2.00" in text
    assert "누적 $+1.00" in text
    assert "오픈 1건" in text
    assert "미실현 $+0.50" in text
