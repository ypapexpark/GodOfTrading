import unittest
from unittest.mock import patch

import polymarket_whale_live_bot as live
import polymarket_whale_paper_bot as paper


class PolymarketWhaleLiveTest(unittest.TestCase):
    def test_entry_requires_positive_wallet_edge(self):
        state = {"wallets": {"w": {"expected_win_rate": 0.63}}}
        with patch.object(live, "EDGE_FILTER_ENABLED", True):
            self.assertTrue(live._entry_edge_ok(state, {"wallet": "w"}, 0.57)[0])
            self.assertFalse(live._entry_edge_ok(state, {"wallet": "w"}, 0.59)[0])

    def test_edge_filter_defaults_to_paper_parity(self):
        state = {"wallets": {"w": {"expected_win_rate": 0.51}}}
        with patch.object(live, "EDGE_FILTER_ENABLED", False):
            self.assertTrue(live._entry_edge_ok(state, {"wallet": "w"}, 0.90)[0])

    def test_committed_cap_blocks_new_ticket(self):
        state = {
            "bankroll": 800.0,
            "daily_loss": 0.0,
            "open_positions": [
                {"bet_usd": 15.0, "live": True, "dry_run": False, "is_shadow": False}
                for _ in range(21)
            ],
        }
        ok, reason = live._risk_ok(state, 15.0)
        self.assertFalse(ok)
        self.assertIn("총투입", reason)

    def test_actual_position_summary_splits_profit_and_loss(self):
        summary = live._summarize_actual_positions([
            {"size": 10, "initialValue": 5, "currentValue": 7, "cashPnl": 2},
            {"size": 10, "initialValue": 6, "currentValue": 4, "cashPnl": -2},
            {"size": 0, "initialValue": 8, "currentValue": 0, "cashPnl": -8},
        ])
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["invested"], 11)
        self.assertEqual(summary["value"], 11)
        self.assertEqual(summary["profit"], 2)
        self.assertEqual(summary["loss"], -2)

    def test_actual_closed_summary_ignores_phantom_tokens(self):
        summary = live._summarize_actual_closed([
            {"asset": "real", "realizedPnl": 12, "timestamp": 0},
            {"asset": "real2", "realizedPnl": -5, "timestamp": 0},
            {"asset": "phantom", "realizedPnl": -99, "timestamp": 0},
        ], {"real", "real2"})
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["realized"], 7)

    def test_accounting_uses_cash_plus_position_value_against_principal(self):
        state = {"daily_loss": 999, "bankroll": 1}
        closed = {
            "ok": True, "count": 2, "wins": 1, "losses": 1, "flat": 0,
            "realized": 10, "profit": 15, "loss": -5, "today_loss": 5,
            "positions": [],
        }
        portfolio = {"ok": True, "value": 40}
        with (
            patch.object(live, "INITIAL_BANKROLL", 1100),
            patch.object(live, "_bot_token_ids", return_value={"token"}),
            patch.object(live, "_fetch_actual_closed", return_value=closed),
            patch.object(live, "_fetch_actual_portfolio", return_value=portfolio),
            patch.object(live, "get_usdc_balance_approx", return_value=900),
            patch.object(live, "_append"),
        ):
            result = live._sync_actual_accounting(state)
        self.assertTrue(result["ok"])
        self.assertEqual(state["bankroll"], 940)
        self.assertEqual(state["actual_accounting"]["all_time_pnl"], -160)
        self.assertEqual(state["daily_loss"], 5)

    def test_failed_flip_keeps_position_and_blocks_opposite_buy(self):
        old = {
            "wallet": "w", "condition_id": "c", "outcome_index": 0,
            "bet_usd": 15.0, "live": True, "dry_run": False, "is_shadow": False,
        }
        state = {
            "bankroll": 800.0, "daily_loss": 0.0, "orders_blocked": 0,
            "wallets": {"w": {"expected_win_rate": 0.70}},
            "open_positions": [old],
        }
        market = {"closed": False, "question": "q"}
        signal = {"wallet": "w", "condition_id": "c", "outcome_index": 1}
        with (
            patch.object(live, "HOLD_TO_RESOLUTION", False),
            patch.object(live.paper, "_gamma_market_by_condition", return_value=market),
            patch.object(live, "_live_early_exit", return_value=False),
            patch.object(live, "place_buy_usd") as buy,
            patch.object(live, "_append"),
        ):
            opened = live.open_live_positions([signal], state)
        self.assertEqual(opened, 0)
        self.assertEqual(state["open_positions"], [old])
        self.assertEqual(state["orders_blocked"], 1)
        buy.assert_not_called()

    def test_hold_policy_ignores_flip_without_selling_or_buying(self):
        old = {
            "wallet": "w", "condition_id": "c", "outcome_index": 0,
            "bet_usd": 15.0, "live": True, "dry_run": False, "is_shadow": False,
        }
        state = {
            "bankroll": 800.0, "daily_loss": 0.0, "orders_blocked": 0,
            "wallets": {"w": {"expected_win_rate": 0.70}},
            "open_positions": [old],
        }
        market = {"closed": False, "question": "q"}
        signal = {"wallet": "w", "condition_id": "c", "outcome_index": 1}
        with (
            patch.object(live, "HOLD_TO_RESOLUTION", True),
            patch.object(live.paper, "_gamma_market_by_condition", return_value=market),
            patch.object(live, "_live_early_exit") as early_exit,
            patch.object(live, "place_buy_usd") as buy,
            patch.object(live, "_append"),
        ):
            opened = live.open_live_positions([signal], state)
        self.assertEqual(opened, 0)
        self.assertEqual(state["open_positions"], [old])
        self.assertEqual(state["orders_blocked"], 1)
        early_exit.assert_not_called()
        buy.assert_not_called()

    def test_scaled_consensus_opens_first_second_and_third_tiers(self):
        state = {
            "bankroll": 1100.0,
            "daily_loss": 0.0,
            "orders_blocked": 0,
            "wallets": {f"w{i}": {"status": "active"} for i in range(1, 5)},
            "open_positions": [],
        }
        market = {
            "id": "m",
            "closed": False,
            "question": "q",
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["t0", "t1"]',
        }

        def signal(wallet):
            return {
                "wallet": wallet,
                "condition_id": "c",
                "outcome_index": 0,
                "title": "q",
                "slug": "q",
            }

        def fill(_token, bet, **_kwargs):
            return {
                "ok": True,
                "filled_usd": bet,
                "filled_shares": bet / 0.5,
                "fill_price": 0.5,
                "fill_status": "matched",
                "order_id": "o",
            }

        with (
            patch.object(live, "HOLD_TO_RESOLUTION", True),
            patch.object(live, "live_enabled", return_value=True),
            patch.object(live.paper, "_gamma_market_by_condition", return_value=market),
            patch.object(live, "place_buy_usd", side_effect=fill) as buy,
            patch.object(live, "_append"),
        ):
            self.assertEqual(live.open_live_positions([signal("w1")], state), 1)
            self.assertEqual(live.open_live_positions([signal("w2")], state), 1)
            self.assertEqual(live.open_live_positions([signal("w1")], state), 0)
            self.assertEqual(live.open_live_positions([signal("w3")], state), 1)
            self.assertEqual(live.open_live_positions([signal("w4")], state), 0)

        self.assertEqual(buy.call_count, 3)
        self.assertEqual(len(state["open_positions"]), 3)
        self.assertEqual(
            [p["consensus_rank"] for p in state["open_positions"]], [1, 2, 3]
        )
        self.assertEqual(
            [p["bet_usd"] for p in state["open_positions"]], [10.0, 15.0, 20.0]
        )
        self.assertEqual(
            state["consensus_candidates"]["c:0"]["wallets"],
            ["w1", "w2", "w3", "w4"],
        )

    def test_opposite_direction_candidate_blocks_entire_market(self):
        state = {
            "bankroll": 1100.0,
            "daily_loss": 0.0,
            "orders_blocked": 0,
            "wallets": {f"w{i}": {"status": "active"} for i in range(1, 4)},
            "open_positions": [],
        }
        market = {
            "id": "m",
            "closed": False,
            "question": "q",
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["t0", "t1"]',
        }
        signals = [
            {"wallet": "w1", "condition_id": "c", "outcome_index": 0},
            {"wallet": "w2", "condition_id": "c", "outcome_index": 1},
            {"wallet": "w3", "condition_id": "c", "outcome_index": 0},
        ]
        with (
            patch.object(live, "HOLD_TO_RESOLUTION", True),
            patch.object(live, "live_enabled", return_value=True),
            patch.object(live.paper, "_gamma_market_by_condition", return_value=market),
            patch.object(
                live,
                "place_buy_usd",
                return_value={
                    "ok": True, "filled_usd": 10.0, "filled_shares": 20.0,
                    "fill_price": 0.5, "fill_status": "matched",
                },
            ) as buy,
            patch.object(live, "_append"),
        ):
            opened = live.open_live_positions(signals, state)

        self.assertEqual(opened, 1)
        self.assertEqual(state["orders_blocked"], 2)
        self.assertEqual(len(state["open_positions"]), 1)
        self.assertEqual(state["open_positions"][0]["bet_usd"], 10.0)
        self.assertEqual(buy.call_count, 1)

    def test_hold_policy_disables_whale_reduce_exit(self):
        state = {"open_positions": [{"wallet": "w"}]}
        with (
            patch.object(live, "HOLD_TO_RESOLUTION", True),
            patch.object(live, "_live_early_exit") as early_exit,
        ):
            self.assertEqual(live.follow_whale_exits_live(state), 0)
        self.assertEqual(len(state["open_positions"]), 1)
        early_exit.assert_not_called()


class PolymarketWhalePaperParityTest(unittest.TestCase):
    def test_paper_uses_same_three_tier_whale_policy(self):
        state = {
            "wallets": {f"w{i}": {"status": "active"} for i in range(1, 5)},
            "open_positions": [],
        }
        market = {
            "id": "m",
            "closed": False,
            "question": "q",
            "outcomePrices": '["0.5", "0.5"]',
        }

        def signal(wallet):
            return {
                "wallet": wallet,
                "condition_id": "c",
                "outcome_index": 0,
                "title": "q",
                "slug": "q",
            }

        with (
            patch.object(paper, "HOLD_TO_RESOLUTION", True),
            patch.object(paper, "_gamma_market_by_condition", return_value=market),
            patch.object(paper, "_append_jsonl"),
        ):
            self.assertEqual(paper.open_paper_positions([signal("w1")], state), 1)
            self.assertEqual(paper.open_paper_positions([signal("w2")], state), 1)
            self.assertEqual(paper.open_paper_positions([signal("w3")], state), 1)
            self.assertEqual(paper.open_paper_positions([signal("w4")], state), 0)

        self.assertEqual(len(state["open_positions"]), 3)
        self.assertEqual(
            [p["consensus_rank"] for p in state["open_positions"]], [1, 2, 3]
        )
        self.assertEqual(
            [p["bet_usd"] for p in state["open_positions"]],
            [10.0, 15.0, 20.0],
        )

    def test_hold_policy_ignores_paper_flip(self):
        old = {"wallet": "w", "condition_id": "c", "outcome_index": 0}
        state = {"wallets": {"w": {}}, "open_positions": [old]}
        signal = {"wallet": "w", "condition_id": "c", "outcome_index": 1}
        market = {"closed": False, "question": "q"}
        with (
            patch.object(paper, "HOLD_TO_RESOLUTION", True),
            patch.object(paper, "_gamma_market_by_condition", return_value=market),
            patch.object(paper, "early_exit_position") as early_exit,
        ):
            opened = paper.open_paper_positions([signal], state)
        self.assertEqual(opened, 0)
        self.assertEqual(state["open_positions"], [old])
        early_exit.assert_not_called()

    def test_hold_policy_disables_paper_reduce_exit(self):
        state = {"open_positions": [{"wallet": "w"}]}
        with (
            patch.object(paper, "HOLD_TO_RESOLUTION", True),
            patch.object(paper, "early_exit_position") as early_exit,
        ):
            self.assertEqual(paper.follow_whale_exits(state), 0)
        self.assertEqual(len(state["open_positions"]), 1)
        early_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
