import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import polymarket_whale_live_bot as live
import polymarket_whale_paper_bot as paper


class PolymarketWhaleLiveTest(unittest.TestCase):
    @staticmethod
    def executable_quote(bet=10.0):
        return {
            "ok": True,
            "best_ask": 0.5,
            "worst_ask": 0.5,
            "vwap": 0.5,
            "shares": bet / 0.5,
            "fillable_usd": bet,
            "error": "",
        }

    def test_entry_requires_positive_wallet_edge(self):
        state = {"wallets": {"w": {"expected_win_rate": 0.63}}}
        with patch.object(live, "EDGE_FILTER_ENABLED", True):
            self.assertTrue(live._entry_edge_ok(state, {"wallet": "w"}, 0.54)[0])
            self.assertFalse(live._entry_edge_ok(state, {"wallet": "w"}, 0.55)[0])

    def test_only_live_approved_discovery_wallet_enters_live_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            watchlist = Path(tmp) / "watchlist.json"
            radar = Path(tmp) / "radar.json"
            watchlist.write_text(json.dumps({
                "paper": [{"wallet": "0xpaper", "expected_win_rate": 0.8}],
                "live_approved": [
                    {
                        "wallet": "0xapproved",
                        "expected_win_rate": 0.7,
                        "stage": "live_canary",
                        "live_risk_mult": 0.5,
                    }
                ],
            }))
            radar.write_text(json.dumps({
                "wallets": {"0xapproved": {
                    "last_seen_ts": 1234,
                    "directional_signal_levels_v5": {"c:0": 4},
                    "market_flow_v2": {"c": {"buy_0": 1000}},
                    "activity_seen_v2": ["already-seen"],
                    "classification_v2": {"market_maker_like": False},
                }}
            }))
            state = {"wallets": {}}
            with (
                patch.object(live, "DISCOVERY_WATCHLIST_FILE", watchlist),
                patch.object(live, "DISCOVERY_RADAR_STATE_FILE", radar),
                patch.object(live.paper, "_load_config", return_value={"whales": []}),
                patch.object(live.paper, "_load_state", return_value={"wallets": {}}),
            ):
                live._ensure_wallets_from_config(state)
            self.assertIn("0xapproved", state["wallets"])
            self.assertNotIn("0xpaper", state["wallets"])
            self.assertEqual(state["wallets"]["0xapproved"]["last_seen_ts"], 1234)
            self.assertEqual(
                state["wallets"]["0xapproved"]["live_risk_mult"], 0.5
            )
            self.assertEqual(
                state["wallets"]["0xapproved"]["promotion_stage"],
                "live_canary",
            )
            self.assertEqual(
                state["wallets"]["0xapproved"]["directional_signal_levels_v5"],
                {"c:0": 4},
            )
            self.assertEqual(
                state["wallets"]["0xapproved"]["activity_seen_v2"],
                ["already-seen"],
            )

    def test_rejected_legacy_wallet_is_disabled_for_new_live_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            watchlist = Path(tmp) / "watchlist.json"
            radar = Path(tmp) / "radar.json"
            watchlist.write_text(json.dumps({
                "blocked_live": [{"wallet": "0xlegacy"}],
                "live_approved": [],
            }))
            radar.write_text("{}")
            state = {"wallets": {"0xlegacy": {"status": "active"}}}
            with (
                patch.object(live, "DISCOVERY_WATCHLIST_FILE", watchlist),
                patch.object(live, "DISCOVERY_RADAR_STATE_FILE", radar),
                patch.object(live.paper, "_load_config", return_value={
                    "whales": [{"wallet": "0xlegacy", "expected_win_rate": 0.7}]
                }),
                patch.object(live.paper, "_load_state", return_value={"wallets": {}}),
            ):
                live._ensure_wallets_from_config(state)
            self.assertEqual(
                state["wallets"]["0xlegacy"]["status"],
                "legacy_validation_rejected",
            )

    def test_edge_filter_defaults_to_paper_parity(self):
        state = {"wallets": {"w": {"expected_win_rate": 0.51}}}
        with patch.object(live, "EDGE_FILTER_ENABLED", False):
            self.assertTrue(live._entry_edge_ok(state, {"wallet": "w"}, 0.90)[0])

    def test_stale_whale_signal_is_blocked_before_market_lookup(self):
        state = {
            "bankroll": 1100.0,
            "daily_loss": 0.0,
            "orders_blocked": 0,
            "wallets": {"w": {"status": "active"}},
            "open_positions": [],
        }
        signal = {
            "wallet": "w", "condition_id": "c", "outcome_index": 0,
            "source_trade_ts": live._now() - 601,
        }
        with (
            patch.object(live, "MAX_SIGNAL_AGE_SECONDS", 600),
            patch.object(live.paper, "_gamma_market_by_condition") as market,
            patch.object(live, "place_buy_usd") as buy,
            patch.object(live, "_append"),
        ):
            opened = live.open_live_positions([signal], state)
        self.assertEqual(opened, 0)
        self.assertEqual(state["orders_blocked"], 1)
        market.assert_not_called()
        buy.assert_not_called()

    def test_invalid_outcome_is_blocked_before_market_lookup(self):
        state = {
            "bankroll": 1100.0,
            "daily_loss": 0.0,
            "orders_blocked": 0,
            "wallets": {"w": {"status": "active"}},
            "open_positions": [],
        }
        signal = {"wallet": "w", "condition_id": "c", "outcome_index": 999}
        with (
            patch.object(live.paper, "_gamma_market_by_condition") as market,
            patch.object(live, "_append"),
        ):
            opened = live.open_live_positions([signal], state)
        self.assertEqual(opened, 0)
        self.assertEqual(state["orders_blocked"], 1)
        market.assert_not_called()

    def test_invalid_historical_outcome_state_is_pruned_once(self):
        state = {
            "wallets": {"w": {
                "net_usdc": {"c:0": 1000, "c:999": 2000},
                "signaled": {"c:0": True, "c:999": True},
            }},
            "consensus_candidates": {
                "c:0": {"wallets": ["w"]},
                "c:999": {"wallets": ["w"]},
            },
        }
        with patch.object(live, "_append"):
            removed = live._prune_invalid_outcome_state(state)
            removed_again = live._prune_invalid_outcome_state(state)
        self.assertEqual(removed, 3)
        self.assertEqual(removed_again, 0)
        self.assertEqual(state["wallets"]["w"]["net_usdc"], {"c:0": 1000})
        self.assertNotIn("c:999", state["consensus_candidates"])

    def test_prequote_without_full_depth_blocks_live_order(self):
        state = {
            "bankroll": 1100.0,
            "daily_loss": 0.0,
            "orders_blocked": 0,
            "wallets": {
                "w1": {"status": "active", "expected_win_rate": 0.80},
                "w2": {"status": "active", "expected_win_rate": 0.80},
            },
            "open_positions": [],
        }
        market = {
            "id": "m", "closed": False, "question": "q",
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["t0", "t1"]',
        }
        signals = [
            {"wallet": "w1", "condition_id": "c", "outcome_index": 0,
             "source_trade_price": 0.5},
        ]
        with (
            patch.object(live, "live_enabled", return_value=True),
            patch.object(live.paper, "_gamma_market_by_condition", return_value=market),
            patch.object(live, "quote_buy_usd", return_value={
                "ok": False, "error": "insufficient asks",
                "best_ask": 0.5, "fillable_usd": 4.0,
            }),
            patch.object(live, "place_buy_usd") as buy,
            patch.object(live, "_append"),
        ):
            opened = live.open_live_positions(signals, state)
        self.assertEqual(opened, 0)
        self.assertEqual(state["orders_blocked"], 1)
        buy.assert_not_called()

    def test_same_whale_strong_opposite_signal_never_opens_recovery_hedge(self):
        old = {
            "wallet": "w", "condition_id": "c", "outcome_index": 0,
            "bet_usd": 10.0, "shares_est": 15.151515,
            "live": True, "dry_run": False, "is_shadow": False,
        }
        state = {
            "bankroll": 1100.0, "daily_loss": 0.0, "orders_blocked": 0,
            "wallets": {"w": {
                "status": "active",
                "net_usdc": {"c:0": 3405.44, "c:1": 3004.8},
            }},
            "consensus_candidates": {"c:0": {"wallets": ["w"]}},
            "open_positions": [old],
        }
        market = {
            "id": "m", "closed": False, "question": "q",
            "outcomePrices": '["0.4", "0.6"]',
            "clobTokenIds": '["t0", "t1"]',
        }
        signal = {
            "wallet": "w", "condition_id": "c", "outcome_index": 1,
            "source_trade_ts": live._now(),
        }

        def quote(_token, bet, **_kwargs):
            return {
                "ok": True, "best_ask": 0.6, "worst_ask": 0.6,
                "vwap": 0.6, "shares": bet / 0.6,
                "fillable_usd": bet, "error": "",
            }

        def fill(_token, bet, **_kwargs):
            return {
                "ok": True, "filled_usd": bet, "filled_shares": bet / 0.6,
                "fill_price": 0.6, "fill_status": "matched",
            }

        with (
            patch.object(live, "HOLD_TO_RESOLUTION", True),
            patch.object(live, "live_enabled", return_value=True),
            patch.object(live.paper, "_gamma_market_by_condition", return_value=market),
            patch.object(live, "quote_buy_usd", side_effect=quote),
            patch.object(live, "place_buy_usd", side_effect=fill) as buy,
            patch.object(live, "_live_early_exit") as early_exit,
            patch.object(live, "_append"),
        ):
            opened = live.open_live_positions([signal], state)

        self.assertEqual(opened, 0)
        self.assertEqual(state["open_positions"], [old])
        self.assertEqual(state["orders_blocked"], 1)
        early_exit.assert_not_called()
        buy.assert_not_called()

    def test_same_whale_recovery_is_blocked_when_break_even_needs_over_45(self):
        old = {
            "wallet": "w", "condition_id": "c", "outcome_index": 0,
            "bet_usd": 10.0, "shares_est": 15.0,
            "live": True, "dry_run": False, "is_shadow": False,
        }
        state = {
            "bankroll": 1100.0, "daily_loss": 0.0, "orders_blocked": 0,
            "wallets": {"w": {
                "status": "active", "net_usdc": {"c:0": 3000, "c:1": 4000},
            }},
            "consensus_candidates": {"c:0": {"wallets": ["w"]}},
            "open_positions": [old],
        }
        market = {
            "id": "m", "closed": False, "question": "q",
            "outcomePrices": '["0.08", "0.92"]',
            "clobTokenIds": '["t0", "t1"]',
        }
        signal = {"wallet": "w", "condition_id": "c", "outcome_index": 1}
        with (
            patch.object(live, "HOLD_TO_RESOLUTION", True),
            patch.object(live, "live_enabled", return_value=True),
            patch.object(live.paper, "_gamma_market_by_condition", return_value=market),
            patch.object(live, "quote_buy_usd") as quote,
            patch.object(live, "place_buy_usd") as buy,
            patch.object(live, "_append"),
        ):
            opened = live.open_live_positions([signal], state)
        self.assertEqual(opened, 0)
        self.assertEqual(state["open_positions"], [old])
        self.assertEqual(state["orders_blocked"], 1)
        quote.assert_not_called()
        buy.assert_not_called()

    def test_committed_cap_blocks_new_ticket(self):
        state = {
            "bankroll": 800.0,
            "daily_loss": 0.0,
            "open_positions": [
                {"bet_usd": 15.0, "live": True, "dry_run": False, "is_shadow": False}
                for _ in range(21)
            ],
        }
        with patch.object(live, "MAX_OPEN", 0):
            ok, reason = live._risk_ok(state, 8.0)
        self.assertFalse(ok)
        self.assertIn("총투입", reason)

    def test_ticket_budget_is_never_rounded_above_equity_fraction(self):
        state = {"bankroll": 900.586629}
        ticket = live._ticket_usd(state)
        self.assertLessEqual(ticket, state["bankroll"] * live.BET_FRACTION)
        self.assertEqual(ticket, 9.0058)

    def test_temporary_risk_block_is_retried_then_cleared_after_fill(self):
        state = {
            "bankroll": 1000.0, "orders_blocked": 0,
            "wallets": {"w": {"status": "active", "expected_win_rate": 0.8}},
            "open_positions": [],
        }
        signal = {
            "wallet": "w", "condition_id": "c", "outcome_index": 0,
            "source_trade_ts": live._now(), "source_trade_price": 0.5,
            "signal_level": 1, "net_usdc": 250, "net_shares": 500,
        }
        market = {
            "id": "m", "closed": False, "question": "q",
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["t0", "t1"]',
        }
        with (
            patch.object(live, "live_enabled", return_value=True),
            patch.object(live.paper, "_gamma_market_by_condition", return_value=market),
            patch.object(live, "_risk_ok", return_value=(False, "temporary cap")),
            patch.object(live, "_append"),
        ):
            self.assertEqual(live.open_live_positions([signal], state), 0)
        pending = live._fresh_pending_mirror_signals(state)
        self.assertEqual(len(pending), 1)

        with (
            patch.object(live, "live_enabled", return_value=True),
            patch.object(live.paper, "_gamma_market_by_condition", return_value=market),
            patch.object(live, "_risk_ok", return_value=(True, "")),
            patch.object(live, "quote_buy_usd", return_value=self.executable_quote(10)),
            patch.object(live, "place_buy_usd", return_value={
                "ok": True, "filled_usd": 9.0, "filled_shares": 18,
                "fill_price": 0.5, "fill_status": "matched",
            }),
            patch.object(live, "_append"),
        ):
            self.assertEqual(live.open_live_positions(pending, state), 1)
        self.assertEqual(state["pending_mirror_signals_v5"], {})

    def test_equity_daily_drawdown_hard_stops_new_entries(self):
        state = {
            "bankroll": 1000.0,
            "open_positions": [],
            "risk_policy_v4": {
                "day": live._today(), "day_start_equity": 1000.0,
                "policy_start_equity": 1000.0, "day_turnover_usd": 0,
            },
        }
        with patch.object(live, "MAX_DAILY_LOSS_FRACTION", 0.03):
            risk = live._update_equity_risk_state(state, 969.0)
            ok, reason = live._risk_ok(state, 9.0)
        self.assertTrue(risk["halted"])
        self.assertFalse(ok)
        self.assertIn("일손실", reason)

    def test_v4_policy_migration_preserves_cursor_but_clears_partial_flows(self):
        state = {
            "signal_policy": "scaled_whale_consensus_v3",
            "bankroll": 900.0,
            "wallets": {"w": {
                "last_seen_ts": 123, "net_usdc": {"c:0": 5000},
                "net_shares_v5": {"c:0": 10000},
                "signaled": {"c:0": True}, "market_flow_v2": {"c": {}},
                "directional_signal_levels_v5": {"c:0": 20},
            }},
            "consensus_candidates": {"c:0": {"wallets": ["w"]}},
        }
        with patch.object(live, "_append"):
            changed = live._ensure_safe_policy_state(state)
        self.assertTrue(changed)
        self.assertEqual(state["wallets"]["w"]["last_seen_ts"], 123)
        self.assertEqual(state["wallets"]["w"]["net_usdc"], {})
        self.assertEqual(state["wallets"]["w"]["net_shares_v5"], {})
        self.assertEqual(
            state["wallets"]["w"]["directional_signal_levels_v5"], {}
        )
        self.assertEqual(state["live_consensus_v4"], {})
        self.assertEqual(state["risk_policy_v4"]["policy_start_equity"], 900.0)

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
        state = {
            "daily_loss": 999,
            "bankroll": 1,
            "risk_policy_v4": {
                "day": live._today(), "day_start_equity": 945,
                "policy_start_equity": 950, "day_turnover_usd": 0,
            },
        }
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
            patch.object(live, "_fetch_accounting_snapshot", return_value={
                "ok": True, "cash": 900, "position_value": 40,
                "equity": 940, "valuation_time": "t", "position_rows": 2,
                "zero_value_position_rows": 1,
            }),
            patch.object(live, "_append"),
        ):
            result = live._sync_actual_accounting(state)
        self.assertTrue(result["ok"])
        self.assertEqual(state["bankroll"], 940)
        self.assertEqual(state["actual_accounting"]["all_time_pnl"], -160)
        self.assertEqual(state["daily_loss"], 5)

    def test_wallet_closed_api_reconciles_manual_sale_and_removes_local_tickets(self):
        state = {
            "wallets": {
                "w1": {"signaled": {"c:0": True}},
                "w2": {"signaled": {"c:0": True}},
            },
            "open_positions": [
                {
                    "wallet": "w1", "condition_id": "c", "outcome_index": 0,
                    "token_id": "t0", "opened_ts": 100, "bet_usd": 10,
                    "shares_est": 20, "live": True, "dry_run": False,
                    "is_shadow": False, "title": "q",
                },
                {
                    "wallet": "w2", "condition_id": "c", "outcome_index": 0,
                    "token_id": "t0", "opened_ts": 110, "bet_usd": 15,
                    "shares_est": 30, "live": True, "dry_run": False,
                    "is_shadow": False, "title": "q",
                },
            ],
        }
        closed = {
            "ok": True,
            "positions": [{
                "asset": "t0", "timestamp": 200, "realizedPnl": 7.5,
                "avgPrice": 0.5, "totalBought": 50,
            }],
        }
        with (
            patch.object(live, "_fetch_actual_portfolio", return_value={
                "ok": True, "positions": [],
            }),
            patch.object(live, "_fetch_actual_closed", return_value=closed),
            patch.object(live, "_append") as append,
        ):
            reconciled = live._reconcile_wallet_closed_positions(state)
        self.assertEqual(reconciled, 1)
        self.assertEqual(state["open_positions"], [])
        self.assertFalse(state["wallets"]["w1"]["signaled"]["c:0"])
        self.assertEqual(state["confirmed_close_ledger_v1"]["realized_pnl"], 7.5)
        row = append.call_args.args[0]
        self.assertEqual(row["settlement_source"], "polymarket_closed_positions_api")
        self.assertEqual(row["local_ticket_count"], 2)

    def test_missing_closed_api_row_never_removes_local_position(self):
        position = {
            "wallet": "w", "condition_id": "c", "outcome_index": 0,
            "token_id": "t0", "opened_ts": 100, "bet_usd": 10,
            "live": True, "dry_run": False, "is_shadow": False,
        }
        state = {"wallets": {"w": {}}, "open_positions": [position]}
        with (
            patch.object(live, "_fetch_actual_portfolio", return_value={
                "ok": True, "positions": [],
            }),
            patch.object(live, "_fetch_actual_closed", return_value={
                "ok": True, "positions": [],
            }),
            patch.object(live, "_append") as append,
        ):
            reconciled = live._reconcile_wallet_closed_positions(state)
        self.assertEqual(reconciled, 0)
        self.assertEqual(state["open_positions"], [position])
        append.assert_not_called()

    def test_report_equity_uses_same_current_position_value_it_displays(self):
        state = {
            "bankroll": 979.25,
            "daily_loss": 0,
            "wallets": {},
            "open_positions": [],
            "actual_accounting": {"cash": 912.67, "equity": 979.25},
        }
        portfolio = {
            "ok": True, "count": 3, "invested": 30, "value": 68.34,
            "unrealized": 38.34, "profit": 43.49, "loss": -5.15,
            "profit_count": 2, "loss_count": 1, "flat_count": 0,
            "positions": [],
        }
        closed = {
            "ok": True, "count": 0, "wins": 0, "losses": 0, "flat": 0,
            "realized": 0, "profit": 0, "loss": 0, "today_loss": 0,
            "positions": [],
        }
        with (
            patch.object(live, "INITIAL_BANKROLL", 1100),
            patch.object(live, "_journal_rows", return_value=[]),
            patch.object(live, "_fetch_actual_portfolio", return_value=portfolio),
            patch.object(live, "_fetch_actual_closed", return_value=closed),
            patch.object(live, "_paper_comparison", return_value={
                "all_count": 0, "all_pnl": 0, "same_count": 0, "same_pnl": 0,
                "same_roi": 0, "largest_win": 0, "all_without_largest": 0,
            }),
            patch("polymarket_whale_insights.build_insight_comments", return_value=[]),
        ):
            report = live.build_report(state)
        self.assertIn("현재 총자산 <b>$979.25</b>", report)
        self.assertIn("총손익 <b>$-120.75</b>", report)

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

    def test_v5_single_wallet_can_add_twice_within_market_risk_cap(self):
        state = {
            "bankroll": 1100.0,
            "daily_loss": 0.0,
            "orders_blocked": 0,
            "wallets": {
                f"w{i}": {"status": "active", "expected_win_rate": 0.80}
                for i in range(1, 5)
            },
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
                "source_trade_price": 0.5,
                "net_usdc": 250,
                "net_shares": 500,
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
            patch.object(live, "quote_buy_usd", return_value=self.executable_quote(20)),
            patch.object(live, "place_buy_usd", side_effect=fill) as buy,
            patch.object(live, "_append"),
        ):
            first = signal("w1")
            first["signal_level"] = 1
            second = signal("w1")
            second["signal_level"] = 2
            third = signal("w1")
            third["signal_level"] = 3
            self.assertEqual(live.open_live_positions([first], state), 1)
            self.assertEqual(live.open_live_positions([second], state), 1)
            self.assertEqual(live.open_live_positions([third], state), 0)

        self.assertEqual(buy.call_count, 2)
        self.assertAlmostEqual(buy.call_args_list[0].kwargs["price_hint"], 0.5075)
        self.assertEqual(len(state["open_positions"]), 2)
        self.assertEqual(state["open_positions"][0]["consensus_rank"], 1)
        self.assertEqual(state["open_positions"][0]["signal_policy"], live.LIVE_SIGNAL_POLICY)
        self.assertLessEqual(state["open_positions"][0]["cash_risk_usd"], 10.01)

    def test_live_canary_uses_half_of_normal_cash_risk_budget(self):
        state = {
            "bankroll": 1000.0,
            "daily_loss": 0.0,
            "orders_blocked": 0,
            "wallets": {
                "w": {
                    "status": "active",
                    "expected_win_rate": 0.80,
                    "live_risk_mult": 0.5,
                    "promotion_stage": "live_canary",
                }
            },
            "open_positions": [],
        }
        market = {
            "id": "m",
            "closed": False,
            "question": "q",
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["t0", "t1"]',
        }
        signal = {
            "wallet": "w",
            "condition_id": "c",
            "outcome_index": 0,
            "title": "q",
            "source_trade_price": 0.5,
            "net_usdc": 250,
            "net_shares": 500,
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
            patch.object(live, "live_enabled", return_value=True),
            patch.object(
                live.paper, "_gamma_market_by_condition", return_value=market
            ),
            patch.object(
                live, "quote_buy_usd", return_value=self.executable_quote(5)
            ),
            patch.object(live, "place_buy_usd", side_effect=fill) as buy,
            patch.object(live, "_append"),
        ):
            self.assertEqual(live.open_live_positions([signal], state), 1)

        position = state["open_positions"][0]
        self.assertEqual(position["cash_risk_budget_usd"], 5.0)
        self.assertEqual(position["wallet_risk_mult"], 0.5)
        self.assertEqual(position["wallet_promotion_stage"], "live_canary")
        self.assertEqual(buy.call_args.args[1], 4.6728)

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
            patch.object(live, "quote_buy_usd", return_value=self.executable_quote(20)),
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

        self.assertEqual(opened, 0)
        self.assertEqual(state["orders_blocked"], 3)
        self.assertEqual(len(state["open_positions"]), 0)
        self.assertEqual(buy.call_count, 0)

    def test_hold_policy_disables_whale_reduce_exit(self):
        state = {"open_positions": [{"wallet": "w"}]}
        with (
            patch.object(live, "HOLD_TO_RESOLUTION", True),
            patch.object(live, "_live_early_exit") as early_exit,
        ):
            self.assertEqual(live.follow_whale_exits_live(state), 0)
        self.assertEqual(len(state["open_positions"]), 1)
        early_exit.assert_not_called()

    def test_v5_exits_when_whale_becomes_two_sided(self):
        position = {
            "wallet": "w", "condition_id": "c", "outcome_index": 0,
            "signal_policy": live.LIVE_SIGNAL_POLICY,
            "whale_net_shares_at_entry": 200,
        }
        state = {
            "wallets": {"w": {
                "net_usdc": {"c:0": 1000},
                "net_shares_v5": {"c:0": 200},
                "market_flow_v2": {"c": {"buy_0": 1000, "buy_1": 300}},
            }},
            "open_positions": [position],
        }
        with patch.object(
            live, "_live_early_exit", return_value=True
        ) as early_exit:
            self.assertEqual(live.follow_whale_exits_live(state), 1)
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(
            early_exit.call_args.kwargs["reason"], "whale_became_two_sided"
        )


class PolymarketWhalePaperParityTest(unittest.TestCase):
    def test_paper_uses_same_three_tier_whale_policy(self):
        state = {
            "wallets": {
                f"w{i}": {"status": "active", "expected_win_rate": 0.70}
                for i in range(1, 5)
            },
            "open_positions": [],
            "policy_bankroll": 1000.0,
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

        def quote(_market, _outcome, bet):
            return {
                "ok": True,
                "token_id": "t0",
                "gamma_price": 0.5,
                "max_price": 0.515,
                "best_ask": 0.5,
                "book_vwap": 0.5,
                "entry_price": 0.5075,
                "shares": bet / 0.5075,
                "source": "clob_ask_vwap",
            }

        with (
            patch.object(paper, "HOLD_TO_RESOLUTION", True),
            patch.object(paper, "_gamma_market_by_condition", return_value=market),
            patch.object(paper, "_paper_buy_quote", side_effect=quote),
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
