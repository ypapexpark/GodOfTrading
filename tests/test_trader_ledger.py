import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import trader


class TraderLedgerTest(unittest.TestCase):
    def test_position_keeps_exchange_entry_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "trade_state.json"
            with patch.object(trader, "STATE_FILE", state_path):
                trader._save_position(
                    "BTC/USDT", "LONG", 100.0, 0.1, 98.0,
                    entry_order_id="order-123",
                    entry_order_link_id="got-link-123",
                )
            state = json.loads(state_path.read_text(encoding="utf-8"))

        position = state["positions"]["BTC/USDT"]
        self.assertEqual(position["entry_order_id"], "order-123")
        self.assertEqual(position["entry_order_link_id"], "got-link-123")

    def test_stale_untracked_open_rows_are_quarantined_without_fake_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "trade_state.json"
            state_path.write_text(json.dumps({
                "positions": {"LIVE/USDT": {}},
                "trade_history": [
                    {"num": 1, "symbol": "STALE/USDT", "status": "open", "pnl_usd": 0},
                    {"num": 2, "symbol": "LIVE/USDT", "status": "open", "pnl_usd": 0},
                    {"num": 3, "symbol": "MANUAL/USDT", "status": "open", "pnl_usd": 0},
                ],
            }), encoding="utf-8")
            with patch.object(trader, "STATE_FILE", state_path):
                rows = trader.reconcile_stale_open_history({"MANUAL/USDT"})
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual([row["symbol"] for row in rows], ["STALE/USDT"])
        by_num = {row["num"]: row for row in state["trade_history"]}
        self.assertEqual(by_num[1]["status"], "ledger_orphan")
        self.assertEqual(by_num[1]["pnl_usd"], 0)
        self.assertEqual(by_num[2]["status"], "open")
        self.assertEqual(by_num[3]["status"], "open")


class ProfitLockStepTest(unittest.TestCase):
    def test_profit_lock_levels_step_by_five(self):
        self.assertIsNone(trader.profit_lock_level_for_roi(9.99))
        self.assertEqual(trader.profit_lock_level_for_roi(10.0), 10.0)
        self.assertEqual(trader.profit_lock_level_for_roi(14.9), 10.0)
        self.assertEqual(trader.profit_lock_level_for_roi(15.0), 15.0)
        self.assertEqual(trader.profit_lock_level_for_roi(19.9), 15.0)
        self.assertEqual(trader.profit_lock_level_for_roi(20.0), 20.0)
        self.assertEqual(trader.profit_lock_level_for_roi(33.0), 30.0)

    def test_position_records_profit_lock_level_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "trade_state.json"
            with patch.object(trader, "STATE_FILE", state_path):
                trader._save_position("ETH/USDT", "SHORT", 200.0, 0.5, 205.0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
        pos = state["positions"]["ETH/USDT"]
        self.assertEqual(pos["profit_lock_level"], 0.0)
        self.assertFalse(pos["profit_lock_10_done"])


if __name__ == "__main__":
    unittest.main()
