import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tools import weekly_learning_report as report


class WeeklyLearningReportTest(unittest.TestCase):
    def test_close_timestamp_prefers_exchange_close_over_entry(self):
        now = datetime.now(report.KST)
        entry_ts = (now - timedelta(days=9)).timestamp()
        close_ts = (now - timedelta(days=1)).timestamp()
        trade = {
            "timestamp": entry_ts,
            "closed_at": (now - timedelta(days=2)).strftime("%m/%d %H:%M KST"),
            "close_info": {"updatedTime": str(int(close_ts * 1000))},
        }

        self.assertAlmostEqual(report._close_timestamp(trade, now), close_ts, delta=0.01)

    def test_venue_block_filters_by_close_time(self):
        now = datetime.now(report.KST)
        cutoff = (now - timedelta(days=7)).timestamp()
        old_entry_recent_close = {
            "status": "win",
            "timestamp": (now - timedelta(days=10)).timestamp(),
            "closed_at": (now - timedelta(days=1)).strftime("%m/%d %H:%M KST"),
            "pnl_usd": 2.0,
            "strategy": "EMA",
        }
        recent_entry_old_close = {
            "status": "loss",
            "timestamp": (now - timedelta(days=1)).timestamp(),
            "closed_at": (now - timedelta(days=9)).strftime("%m/%d %H:%M KST"),
            "pnl_usd": -9.0,
            "strategy": "OLD",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trade_state.json").write_text(
                json.dumps({"trade_history": [old_entry_recent_close, recent_entry_old_close]}),
                encoding="utf-8",
            )
            with patch.object(report, "ROOT", root):
                block = report._venue_block("bybit", cutoff, include_api=False)

        self.assertEqual(block["closed"], 1)
        self.assertEqual(block["wins"], 1)
        self.assertEqual(block["pnl"], 2.0)

    def test_suggestions_never_merge_venues(self):
        base = {
            "closed": 30,
            "ledger_reliable": True,
            "ledger_delta": 0.0,
            "causes": report.Counter(),
            "strat_pnl": {},
            "version_counts": report.Counter({"v1": 30}),
        }
        bybit = {**base, "venue": "bybit", "strat_pnl": {"EMA": 2.0}}
        binance = {**base, "venue": "binance", "strat_pnl": {"EMA": -5.0}}

        tips = report._suggestions([bybit, binance])

        self.assertFalse(any("BYBIT" in t and "-5.00" in t for t in tips))
        self.assertTrue(any("BINANCE" in t and "-5.00" in t for t in tips))

    def test_missing_required_exchange_ledger_blocks_strategy_suggestion(self):
        block = {
            "venue": "bybit",
            "closed": 30,
            "ledger_reliable": False,
            "ledger_delta": None,
            "causes": report.Counter({"fast_stop": 20}),
            "strat_pnl": {"EMA": -10.0},
            "version_counts": report.Counter({"v1": 30}),
        }

        tips = report._suggestions([block])

        self.assertEqual(tips, ["[BYBIT] 거래소 원장 검증 실패 — 전략 변경 보류"])

    def test_bybit_query_never_exceeds_seven_day_api_limit(self):
        import trader

        class FakeExchange:
            params = None

            def privateGetV5PositionClosedPnl(self, params):
                self.params = dict(params)
                return {"result": {"list": [], "nextPageCursor": ""}}

        fake = FakeExchange()
        end = 2_000_000_000.0
        with patch.object(trader, "_ex", return_value=fake), patch.object(
            report.time, "time", return_value=end
        ):
            result = report._bybit_api_summary(end - 7 * 86400)

        self.assertTrue(result["ok"])
        self.assertLessEqual(
            fake.params["endTime"] - fake.params["startTime"],
            7 * 86400 * 1000,
        )

    def test_binance_income_separates_trade_net_funding_and_transfer(self):
        import binance_trader

        class FakeExchange:
            params = None

            def fapiPrivateGetIncome(self, params):
                self.params = dict(params)
                return [
                    {"incomeType": "REALIZED_PNL", "income": "-10", "tranId": "1"},
                    {"incomeType": "COMMISSION", "income": "-0.5", "tranId": "2"},
                    {"incomeType": "FUNDING_FEE", "income": "0.2", "tranId": "3"},
                    {"incomeType": "TRANSFER", "income": "-1100", "tranId": "4"},
                ]

        fake = FakeExchange()
        end = 2_000_000_000.0
        with patch.object(binance_trader, "_ex", return_value=fake), patch.object(
            report.time, "time", return_value=end
        ):
            result = report._binance_api_summary(end - 7 * 86400)

        self.assertTrue(result["ok"])
        self.assertEqual(result["pnl"], -10.5)
        self.assertEqual(result["account_net"], -10.3)
        self.assertEqual(result["transfer"], -1100.0)
        self.assertLessEqual(
            fake.params["endTime"] - fake.params["startTime"],
            7 * 86400 * 1000,
        )


if __name__ == "__main__":
    unittest.main()
