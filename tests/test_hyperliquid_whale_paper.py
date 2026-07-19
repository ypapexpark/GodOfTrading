from __future__ import annotations

import json

import hyperliquid_whale_paper_bot as module


WALLET = "0x" + "1" * 40


def _state() -> dict:
    return {
        "wallets": {
            WALLET: {
                "last_fill_time": 900_000,
                "status": "active",
                "copied_keys": {},
                "seeded": True,
            }
        },
        "open_positions": [],
        "bankroll": 1000.0,
        "policy": module.POLICY,
        "policy_bankrolls": {module.POLICY: 1000.0},
        "policy_diagnostics": {},
    }


def _config() -> dict:
    return {
        "whales": [WALLET],
        "params": {
            "min_aggregate_open_notional_usd": 50_000,
            "min_confirmed_position_notional_usd": 50_000,
            "min_position_equity_pct": 0.005,
            "max_signal_lag_seconds": 75,
            "require_taker_open": True,
            "slippage_bps": 15,
            "taker_fee_bps": 5,
            "max_hold_hours_v2": 168,
            "min_whale_flat_age_sec_v2": 30,
        },
    }


def test_scan_aggregates_taker_opens_and_confirms_actual_position_increase(monkeypatch):
    monkeypatch.setattr(module, "_now", lambda: 1000.0)
    monkeypatch.setattr(module, "_now_kst", lambda: "now")
    monkeypatch.setattr(module, "_user_fills_since", lambda *_: [
        {
            "time": 990_000, "coin": "BTC", "dir": "Open Long",
            "crossed": True, "px": "50000", "sz": "0.6", "startPosition": "0",
        },
        {
            "time": 995_000, "coin": "BTC", "dir": "Open Long",
            "crossed": True, "px": "50000", "sz": "0.6", "startPosition": "0.6",
        },
    ])
    monkeypatch.setattr(module, "_clearinghouse", lambda *_args, **_kwargs: {
        "marginSummary": {"accountValue": "1000000"},
        "assetPositions": [{"position": {
            "coin": "BTC", "szi": "2", "positionValue": "100000",
        }}],
    })
    state = _state()
    signals = module.scan_signals(state, _config())
    assert len(signals) == 1
    signal = signals[0]
    assert signal["policy"] == module.POLICY
    assert signal["aggregate_fills"] == 2
    assert signal["notional"] == 60_000
    assert signal["confirmed_increase_notional"] == 100_000
    assert signal["signal_lag_seconds"] == 5


def test_scan_rejects_passive_market_maker_fill(monkeypatch):
    monkeypatch.setattr(module, "_now", lambda: 1000.0)
    monkeypatch.setattr(module, "_now_kst", lambda: "now")
    monkeypatch.setattr(module, "_user_fills_since", lambda *_: [{
        "time": 995_000, "coin": "BTC", "dir": "Open Long",
        "crossed": False, "px": "50000", "sz": "2", "startPosition": "0",
    }])
    state = _state()
    assert module.scan_signals(state, _config()) == []
    diagnostic = state["policy_diagnostics"][module.POLICY]
    assert diagnostic["last"]["maker_open"] == 1


def test_hip3_mids_keep_dex_prefix(monkeypatch):
    def fake_post(body, **_kwargs):
        return {"AAPL": "250"} if body.get("dex") == "xyz" else {"BTC": "60000"}

    monkeypatch.setattr(module, "_post", fake_post)
    mids = module._mids(["xyz:AAPL"])
    assert mids["xyz:AAPL"] == 250
    assert mids["BTC"] == 60_000


def test_v2_settlement_charges_exit_slippage_and_both_taker_fees(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(module, "_now", lambda: 1060.0)
    monkeypatch.setattr(module, "_now_kst", lambda: "now")
    monkeypatch.setattr(module, "_mids", lambda _coins=None: {"BTC": 102.0})
    monkeypatch.setattr(module, "_clearinghouse", lambda *_args, **_kwargs: {
        "marginSummary": {"accountValue": "1000"}, "assetPositions": [],
    })
    state = _state()
    state["open_positions"] = [{
        "policy": module.POLICY,
        "wallet": WALLET,
        "coin": "BTC",
        "direction": "LONG",
        "entry_price": 100.0,
        "notional_usd": 25.0,
        "qty": 0.25,
        "opened_ts": 1000.0,
        "entry_fee_usd": 0.0125,
        "slippage_bps": 15,
        "taker_fee_bps": 5,
    }]
    assert module.mark_and_settle(state, _config()) == 1
    row = json.loads((tmp_path / "journal.jsonl").read_text().splitlines()[0])
    assert 0 < row["pnl_usd"] < 0.5
    assert row["fees_usd"] > 0.02
    assert row["exit_price"] < 102
    assert state["policy_bankrolls"][module.POLICY] > 1000
