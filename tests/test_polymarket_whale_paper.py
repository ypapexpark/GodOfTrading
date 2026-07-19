import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import polymarket_whale_paper_bot as paper


class PolymarketWhalePaperV4Test(unittest.TestCase):
    def market(self, price="0.5"):
        return {
            "id": "m",
            "closed": False,
            "question": "q",
            "outcomePrices": f'["{price}", "{1 - float(price)}"]',
            "clobTokenIds": '["t0", "t1"]',
        }

    def test_buy_quote_uses_executable_ask_vwap(self):
        book = {
            "asks": [
                {"price": "0.51", "size": "20"},
                {"price": "0.50", "size": "10"},
            ]
        }
        with patch.object(paper, "_get_json", return_value=book):
            quote = paper._paper_buy_quote(self.market(), 0, 10.0)
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["source"], "clob_ask_vwap")
        self.assertAlmostEqual(quote["book_vwap"], 10 / (10 + 5 / 0.51), places=6)
        self.assertAlmostEqual(
            quote["entry_price"],
            quote["book_vwap"] * (1 + paper.PAPER_EXECUTION_BUFFER_PCT),
            places=6,
        )

    def test_stale_gamma_price_cannot_create_fake_fill(self):
        book = {"asks": [{"price": "0.10", "size": "1000"}]}
        with patch.object(paper, "_get_json", return_value=book):
            quote = paper._paper_buy_quote(self.market("0.02"), 0, 20.0)
        self.assertFalse(quote["ok"])
        self.assertEqual(quote["reason"], "insufficient_asks_within_price_guard")
        self.assertEqual(quote["fillable_usd"], 0)

    def test_scan_discards_outcome_999_but_advances_cursor(self):
        state = {
            "wallets": {
                "w": {
                    "last_seen_ts": 0,
                    "net_usdc": {},
                    "signaled": {},
                    "last_error": "old transient error",
                }
            }
        }
        activity = [{
            "timestamp": 123,
            "conditionId": "c",
            "outcomeIndex": 999,
            "side": "BUY",
            "usdcSize": 5000,
        }]
        with patch.object(paper, "_fetch_wallet_activity", return_value=activity):
            signals = paper.scan_wallets(state)
        self.assertEqual(signals, [])
        self.assertEqual(state["wallets"]["w"]["last_seen_ts"], 123)
        self.assertEqual(state["wallets"]["w"]["net_usdc"], {})
        self.assertNotIn("last_error", state["wallets"]["w"])

    def test_live_scan_can_skip_suspended_high_frequency_wallet(self):
        state = {
            "wallets": {"w": {
                "status": "suspended", "last_seen_ts": 123,
                "net_usdc": {}, "signaled": {},
            }}
        }
        with patch.object(paper, "_fetch_wallet_activity") as fetch:
            signals = paper.scan_wallets(state, include_suspended=False)
        self.assertEqual(signals, [])
        fetch.assert_not_called()
        self.assertIn("live_scan_skipped_at", state["wallets"]["w"])

    def test_activity_fetch_paginates_in_ascending_order(self):
        first = [{"timestamp": i + 1} for i in range(500)]
        second = [{"timestamp": 501}]
        with (
            patch.object(paper, "ACTIVITY_PAGE_SIZE", 500),
            patch.object(paper, "ACTIVITY_MAX_OFFSET", 3000),
            patch.object(paper, "_get_json", side_effect=[first, second]) as get,
        ):
            rows = paper._fetch_wallet_activity("w", 100)
        self.assertEqual(len(rows), 501)
        self.assertEqual(get.call_args_list[0].args[1]["offset"], 0)
        self.assertEqual(get.call_args_list[1].args[1]["offset"], 500)
        self.assertEqual(get.call_args_list[0].args[1]["sortDirection"], "ASC")

    def test_activity_fetch_continues_from_last_timestamp_after_offset_cap(self):
        overflow = [{"timestamp": 102}, {"timestamp": 101}]
        tail = [{"timestamp": 102}, {"timestamp": 104}]
        with (
            patch.object(paper, "ACTIVITY_PAGE_SIZE", 2),
            patch.object(paper, "ACTIVITY_MAX_OFFSET", 0),
            patch.object(paper, "_now", return_value=104),
            patch.object(
                paper, "_get_json", side_effect=[overflow, tail, []]
            ) as get,
        ):
            rows = paper._fetch_wallet_activity("w", 100)
        self.assertEqual([row["timestamp"] for row in rows], [101, 102, 104])
        self.assertEqual(get.call_args_list[0].args[1]["start"], 100)
        self.assertEqual(get.call_args_list[1].args[1]["start"], 102)
        self.assertEqual(get.call_args_list[2].args[1]["start"], 104)
        self.assertTrue(all(
            call.args[1]["end"] == 104 for call in get.call_args_list
        ))

    def test_scan_evaluates_both_outcomes_before_emitting_signal(self):
        state = {
            "wallets": {"w": {
                "last_seen_ts": 0, "net_usdc": {}, "signaled": {},
            }}
        }
        activity = [
            {"timestamp": 1, "type": "TRADE", "conditionId": "c",
             "outcomeIndex": 0, "side": "BUY", "usdcSize": 1200,
             "price": 0.5, "asset": "t0"},
            {"timestamp": 2, "type": "TRADE", "conditionId": "c",
             "outcomeIndex": 1, "side": "BUY", "usdcSize": 1000,
             "price": 0.5, "asset": "t1"},
        ]
        with patch.object(paper, "_fetch_wallet_activity", return_value=activity):
            signals = paper.scan_wallets(state)
        self.assertEqual(signals, [])
        self.assertGreaterEqual(state["wallets"]["w"]["directional_blocks_v2"], 1)

    def test_fast_mirror_emits_new_signal_at_each_directional_usdc_step(self):
        state = {"wallets": {"w": {
            "status": "active", "last_seen_ts": 0,
            "net_usdc": {}, "signaled": {},
        }}}
        first_trade = [{
            "timestamp": 1, "type": "TRADE", "conditionId": "c",
            "outcomeIndex": 0, "side": "BUY", "usdcSize": 300,
            "size": 600, "price": 0.5, "asset": "t0",
        }]
        second_trade = [{
            "timestamp": 2, "type": "TRADE", "conditionId": "c",
            "outcomeIndex": 0, "side": "BUY", "usdcSize": 300,
            "size": 600, "price": 0.5, "asset": "t0",
        }]
        with (
            patch.object(paper, "MIN_NET_USDC", 250),
            patch.object(
                paper, "_fetch_wallet_activity",
                side_effect=[first_trade, second_trade],
            ),
        ):
            first = paper.scan_wallets(
                state, repeat_directional_steps=True,
                block_market_maker_wallets=False,
            )
            second = paper.scan_wallets(
                state, repeat_directional_steps=True,
                block_market_maker_wallets=False,
            )
        self.assertEqual(first[0]["signal_level"], 1)
        self.assertEqual(second[0]["signal_level"], 2)
        self.assertEqual(second[0]["net_usdc"], 600)
        self.assertEqual(second[0]["net_shares"], 1200)

    def test_fast_mirror_can_use_one_sided_market_from_maker_like_wallet(self):
        state = {"wallets": {"w": {
            "status": "active", "last_seen_ts": 0,
            "net_usdc": {}, "signaled": {},
            "market_flow_v2": {
                f"old{i}": {"buy_0": 1000, "buy_1": 900, "updated_ts": i}
                for i in range(10)
            },
        }}}
        activity = [{
            "timestamp": 20, "type": "TRADE", "conditionId": "new",
            "outcomeIndex": 0, "side": "BUY", "usdcSize": 300,
            "size": 600, "price": 0.5, "asset": "t0",
        }]
        with (
            patch.object(paper, "MIN_NET_USDC", 250),
            patch.object(paper, "_fetch_wallet_activity", return_value=activity),
        ):
            signals = paper.scan_wallets(
                state, repeat_directional_steps=True,
                block_market_maker_wallets=False,
            )
        self.assertTrue(state["wallets"]["w"]["classification_v2"]["market_maker_like"])
        self.assertEqual(len(signals), 1)

    def test_repeated_two_sided_markets_classify_market_maker(self):
        wstate = {"market_flow_v2": {
            f"c{i}": {"buy_0": 1000, "buy_1": 900}
            for i in range(10)
        }}
        result = paper._wallet_directional_classification(wstate)
        self.assertTrue(result["market_maker_like"])
        self.assertEqual(result["two_sided_rate"], 1.0)

    def test_price_edge_blocks_expensive_favorite(self):
        state = {
            "wallets": {
                "w": {"expected_win_rate": 0.63, "net_usdc": {"c:0": 1200}}
            }
        }
        sig = {
            "wallet": "w",
            "condition_id": "c",
            "outcome_index": 0,
            "detected_ts": paper._now(),
        }
        with patch.object(paper, "PAPER_EDGE_FILTER_ENABLED", True):
            ok, reason, _ = paper._paper_entry_quality(state, sig, 0.70)
            self.assertFalse(ok)
            self.assertEqual(reason, "insufficient_price_edge")
            ok, _, _ = paper._paper_entry_quality(state, sig, 0.50)
            self.assertTrue(ok)

    def test_two_sided_whale_is_not_directional_signal(self):
        state = {
            "wallets": {
                "w": {
                    "expected_win_rate": 0.70,
                    "net_usdc": {"c:0": 1200, "c:1": 700},
                }
            }
        }
        sig = {
            "wallet": "w",
            "condition_id": "c",
            "outcome_index": 0,
            "detected_ts": paper._now(),
        }
        with patch.object(paper, "PAPER_EDGE_FILTER_ENABLED", False):
            ok, reason, details = paper._paper_entry_quality(state, sig, 0.40)
        self.assertFalse(ok)
        self.assertEqual(reason, "two_sided_whale_exposure")
        self.assertGreater(details["opposite_ratio"], 0.5)

    def test_strong_same_whale_opposite_signal_is_recovery_exception(self):
        old = {
            "wallet": "w", "condition_id": "c", "outcome_index": 0,
            "bet_usd": 10.0, "shares_est": 15.151515,
            "signal_policy": paper.PAPER_SIGNAL_POLICY, "is_shadow": False,
        }
        state = {
            "wallets": {"w": {
                "status": "active", "expected_win_rate": 0.75,
                "net_usdc": {"c:0": 3405.44, "c:1": 3004.8},
            }},
            "consensus_candidates": {"c:0": {"wallets": ["w"]}},
            "open_positions": [old],
            "policy_bankroll": 1000.0,
        }
        market = {
            "id": "m", "closed": False, "question": "q",
            "outcomePrices": '["0.4", "0.6"]',
            "clobTokenIds": '["t0", "t1"]',
        }
        signal = {
            "wallet": "w", "condition_id": "c", "outcome_index": 1,
            "title": "q", "slug": "q", "source_trade_ts": paper._now(),
        }

        def quote(_market, _outcome, bet):
            return {
                "ok": True, "token_id": "t1", "gamma_price": 0.6,
                "max_price": 0.618, "best_ask": 0.6, "book_vwap": 0.6,
                "entry_price": 0.6, "shares": bet / 0.6,
                "source": "clob_ask_vwap",
            }

        with (
            patch.object(paper, "HOLD_TO_RESOLUTION", True),
            patch.object(paper, "_gamma_market_by_condition", return_value=market),
            patch.object(paper, "_paper_buy_quote", side_effect=quote),
            patch.object(paper, "_append_jsonl"),
        ):
            opened = paper.open_paper_positions([signal], state)
        self.assertEqual(opened, 1)
        self.assertEqual(len(state["open_positions"]), 2)
        recovery = state["open_positions"][1]
        self.assertEqual(recovery["position_role"], "same_whale_recovery_hedge")
        self.assertLessEqual(sum(p["bet_usd"] for p in state["open_positions"]), 45)

    def test_settlement_counts_policy_win_once(self):
        state = {
            "paper_policy": paper.PAPER_SIGNAL_POLICY,
            "bankroll": 1000.0,
            "policy_bankroll": 1000.0,
            "wallets": {
                "w": {
                    "status": "active",
                    "expected_win_rate": 0.60,
                    "policy_n": 0,
                    "policy_wins": 0,
                    "policy_pnl": 0,
                    "policy_bet": 0,
                }
            },
            "open_positions": [{
                "wallet": "w",
                "gamma_market_id": "m",
                "condition_id": "c",
                "outcome_index": 0,
                "entry_price": 0.5,
                "shares_est": 20.0,
                "bet_usd": 10.0,
                "signal_policy": paper.PAPER_SIGNAL_POLICY,
                "is_shadow": False,
            }],
        }
        market = {"closed": True, "outcomePrices": '["1", "0"]'}
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(paper, "JOURNAL_FILE", Path(td) / "journal.jsonl"),
                patch.object(paper, "_fetch_market_state", return_value=market),
                patch.object(paper, "_update_paper_policy_status"),
            ):
                self.assertEqual(paper.settle_positions(state), 1)
        self.assertEqual(state["wallets"]["w"]["policy_n"], 1)
        self.assertEqual(state["wallets"]["w"]["policy_wins"], 1)
        self.assertEqual(state["wallets"]["w"]["policy_pnl"], 10)
        self.assertEqual(state["policy_bankroll"], 1010)

    def test_policy_migration_keeps_legacy_bankroll_separate(self):
        state = {
            "bankroll": 1288.30,
            "wallets": {"w": {"status": "suspended"}},
        }
        with tempfile.TemporaryDirectory() as td, patch.object(
            paper, "JOURNAL_FILE", Path(td) / "journal.jsonl"
        ):
            paper._ensure_paper_policy_state(state)
        self.assertEqual(state["legacy_bankroll_at_v4"], 1288.30)
        self.assertEqual(state["policy_bankroll"], paper.INITIAL_BANKROLL)
        self.assertEqual(state["wallets"]["w"]["status"], "active")
        self.assertEqual(state["paper_policy"], paper.PAPER_SIGNAL_POLICY)

    def test_zero_policy_bankroll_blocks_new_risk(self):
        ok, reason = paper._paper_risk_ok(
            {"policy_bankroll": 0.0, "open_positions": []}, 10.0
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "policy_bankroll_depleted")


if __name__ == "__main__":
    unittest.main()
