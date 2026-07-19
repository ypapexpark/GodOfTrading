import json
import tempfile
import unittest
from pathlib import Path

from quant_governor import cohort_metrics, evaluate_live_candidate


APPROVED = ("EMA눌림목+거래량급등", "EMA눌림목+돌파")


def trade(pnl, *, version="v1", strategy=APPROVED[0], direction="LONG"):
    return {
        "status": "win" if pnl > 0 else "loss",
        "pnl_usd": pnl,
        "est_sl_loss": 1.0,
        "strategy": strategy,
        "direction": direction,
        "logic_stack_version": version,
    }


class QuantGovernorTest(unittest.TestCase):
    def _write(self, root: Path, venue: str, rows):
        name = "trade_state_binance.json" if venue == "binance" else "trade_state.json"
        (root / name).write_text(
            json.dumps({"trade_history": rows}), encoding="utf-8"
        )

    def test_metrics_include_profit_factor_expectancy_and_drawdown(self):
        m = cohort_metrics([trade(2), trade(-1), trade(3), trade(-1)])
        self.assertEqual(m.closed, 4)
        self.assertEqual(m.profit_factor, 2.5)
        self.assertEqual(m.expectancy_usd, 0.75)
        self.assertEqual(m.max_drawdown_usd, 1.0)

    def test_positive_legacy_cohort_runs_new_version_on_probation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [trade(2, version="legacy") for _ in range(12)]
            rows += [trade(-1, version="legacy") for _ in range(8)]
            self._write(root, "bybit", rows)
            d = evaluate_live_candidate(
                venue="bybit", strategy=APPROVED[0], direction="LONG",
                timeframe="15m", approved_strategies=APPROVED,
                logic_stack_version="v2", root=root,
            )
        self.assertTrue(d.allow)
        self.assertEqual(d.mode, "probation")
        self.assertEqual(d.risk_mult, 0.5)

    def test_negative_binance_cohort_is_shadow_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [trade(1) for _ in range(8)] + [trade(-3) for _ in range(12)]
            self._write(root, "binance", rows)
            d = evaluate_live_candidate(
                venue="binance", strategy=APPROVED[1], direction="LONG",
                timeframe="1h", approved_strategies=APPROVED,
                logic_stack_version="v1", root=root,
            )
        self.assertFalse(d.allow)
        self.assertEqual(d.mode, "shadow")
        self.assertEqual(d.risk_mult, 0.0)

    def test_unapproved_direction_never_borrows_champion_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "bybit", [trade(2) for _ in range(25)])
            d = evaluate_live_candidate(
                venue="bybit", strategy=APPROVED[0], direction="SHORT",
                timeframe="15m", approved_strategies=APPROVED,
                root=root,
            )
        self.assertFalse(d.allow)
        self.assertIn("EMA-LONG", d.reason)

    def test_negative_current_version_cannot_hide_behind_legacy_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [trade(2, version="legacy") for _ in range(60)]
            rows += [trade(1, version="v2") for _ in range(10)]
            rows += [trade(-2, version="v2") for _ in range(10)]
            self._write(root, "bybit", rows)
            d = evaluate_live_candidate(
                venue="bybit", strategy=APPROVED[0], direction="LONG",
                timeframe="15m", approved_strategies=APPROVED,
                logic_stack_version="v2", root=root,
            )
        self.assertFalse(d.allow)
        self.assertEqual(d.mode, "shadow")
        self.assertIn("현 버전", d.reason)

    def test_binance_canary_opt_in_allows_tiny_live_oos_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "binance", [trade(-3, version="legacy") for _ in range(20)])
            d = evaluate_live_candidate(
                venue="binance", strategy=APPROVED[0], direction="LONG",
                timeframe="15m", approved_strategies=APPROVED,
                logic_stack_version="v2", root=root,
                binance_canary_enabled=True,
            )
        self.assertTrue(d.allow)
        self.assertEqual(d.mode, "canary")
        self.assertEqual(d.risk_mult, 0.10)

    def test_binance_canary_stops_early_after_negative_eight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [trade(2, version="legacy") for _ in range(20)]
            rows += [trade(-1, version="v2") for _ in range(8)]
            self._write(root, "binance", rows)
            d = evaluate_live_candidate(
                venue="binance", strategy=APPROVED[1], direction="LONG",
                timeframe="1h", approved_strategies=APPROVED,
                logic_stack_version="v2", root=root,
                binance_canary_enabled=True,
            )
        self.assertFalse(d.allow)
        self.assertEqual(d.mode, "shadow")
        self.assertIn("조기중단", d.reason)

    def test_binance_positive_current_version_promotes_after_twenty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [trade(-3, version="legacy") for _ in range(20)]
            rows += [trade(2, version="v2") for _ in range(12)]
            rows += [trade(-1, version="v2") for _ in range(8)]
            self._write(root, "binance", rows)
            d = evaluate_live_candidate(
                venue="binance", strategy=APPROVED[0], direction="LONG",
                timeframe="15m", approved_strategies=APPROVED,
                logic_stack_version="v2", root=root,
                binance_canary_enabled=True,
            )
        self.assertTrue(d.allow)
        self.assertEqual(d.mode, "probation")
        self.assertEqual(d.risk_mult, 0.25)


if __name__ == "__main__":
    unittest.main()
