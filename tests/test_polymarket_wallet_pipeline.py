import time
import unittest
from unittest.mock import patch

import polymarket_wallet_pipeline as pipeline


def trade(ts, condition, outcome, side, usd, price=0.5):
    return {
        "timestamp": ts,
        "conditionId": condition,
        "outcomeIndex": outcome,
        "side": side,
        "usdcSize": usd,
        "size": usd / price,
        "price": price,
    }


class PolymarketWalletPipelineTest(unittest.TestCase):
    def test_profile_detects_gross_two_sided_market_maker_flow(self):
        now = int(time.time())
        rows = []
        for index in range(6):
            condition = f"c{index}"
            rows += [
                trade(now, condition, 0, "BUY", 500),
                trade(now + 1, condition, 1, "BUY", 450),
                trade(now + 2, condition, 0, "SELL", 480),
                trade(now + 3, condition, 1, "SELL", 430),
            ]
        profile = pipeline.profile_trade_rows(rows, rows[:8])
        self.assertEqual(profile["material_market_count"], 6)
        self.assertEqual(profile["two_sided_markets"], 6)
        self.assertEqual(profile["two_sided_rate"], 1.0)
        self.assertEqual(profile["directional_market_count"], 0)

    def test_profile_keeps_one_sided_directional_wallet(self):
        now = int(time.time())
        rows = [
            trade(now + index, f"c{index}", 0, "BUY", 300, 0.4)
            for index in range(8)
        ]
        profile = pipeline.profile_trade_rows(rows, rows)
        self.assertEqual(profile["directional_market_count"], 8)
        self.assertEqual(profile["two_sided_rate"], 0.0)
        self.assertEqual(profile["estimated_taker_share"], 1.0)

    def test_backtest_entry_includes_five_percent_execution_penalty(self):
        rows = [trade(1, "c", 0, "BUY", 250, 0.4)]
        result = pipeline.simulate_condition(rows, winner=0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["entry_price"], 0.42)
        self.assertTrue(result["won"])
        self.assertAlmostEqual(result["pnl_unit"], 1 / 0.42 - 1)

    def test_backtest_follows_strong_opposite_flow_exit(self):
        rows = [
            trade(1, "c", 0, "BUY", 250, 0.4),
            trade(2, "c", 1, "BUY", 100, 0.3),
        ]
        result = pipeline.simulate_condition(rows, winner=None)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["exit_reason"], "opposite_flow")
        self.assertAlmostEqual(result["exit_price"], 0.665)

    def test_market_maker_profile_is_never_promoted(self):
        stage, reasons = pipeline.candidate_stage(
            {"market_maker_like": True},
            {"settled": 100, "roi": 1.0, "bootstrap_p5": 0.5},
            {"settled": 100, "roi": 1.0, "bootstrap_p5": 0.5},
        )
        self.assertEqual(stage, "rejected")
        self.assertTrue(reasons)

    def test_backtest_pass_still_requires_forward_paper(self):
        profile = {
            "market_maker_like": False,
            "active_age_hours": 1,
            "two_sided_rate": 0.0,
            "directional_market_count": 20,
        }
        backtest = {
            "settled": 40,
            "roi": 0.15,
            "bootstrap_p5": 0.03,
            "largest_win_share": 0.2,
        }
        stage, _ = pipeline.candidate_stage(profile, backtest, {"settled": 0})
        self.assertEqual(stage, "paper")

    def test_exceptional_diversified_backtest_enters_half_risk_live_canary(self):
        wallet = "0xcanary"
        candidates = {
            wallet: {
                "profile": {
                    "market_maker_like": False,
                    "active_age_hours": 1,
                    "two_sided_rate": 0.0,
                    "directional_market_count": 80,
                    "estimated_taker_share": 1.0,
                },
                "backtest": {
                    "settled": 72,
                    "roi": 0.34,
                    "bootstrap_p5": 0.17,
                    "max_drawdown_units": 4.3,
                    "largest_win_share": 0.12,
                    "win_rate": 0.79,
                },
            }
        }
        watchlist = pipeline.build_watchlist(candidates, {})
        self.assertEqual(candidates[wallet]["stage"], "live_canary")
        self.assertEqual(watchlist["live_approved"][0]["wallet"], wallet)
        self.assertEqual(
            watchlist["live_approved"][0]["live_risk_mult"],
            0.5,
        )
        self.assertEqual(watchlist["counts"]["live_canary"], 1)

    def test_jackpot_concentrated_backtest_cannot_enter_live_canary(self):
        profile = {
            "market_maker_like": False,
            "active_age_hours": 1,
            "two_sided_rate": 0.0,
            "directional_market_count": 80,
            "estimated_taker_share": 1.0,
        }
        backtest = {
            "settled": 72,
            "roi": 0.34,
            "bootstrap_p5": 0.17,
            "max_drawdown_units": 4.3,
            "largest_win_share": 0.70,
        }
        self.assertEqual(
            pipeline.candidate_stage(profile, backtest, {"settled": 0})[0],
            "screened",
        )

    def test_current_activity_age_is_recomputed_from_last_trade(self):
        profile = {
            "market_maker_like": False,
            "active_age_hours": 1,
            "last_activity_ts": time.time() - 72 * 3600,
            "two_sided_rate": 0.0,
            "directional_market_count": 80,
            "estimated_taker_share": 1.0,
        }
        backtest = {
            "settled": 72,
            "roi": 0.34,
            "bootstrap_p5": 0.17,
            "max_drawdown_units": 4.3,
            "largest_win_share": 0.12,
        }
        stage, reasons = pipeline.candidate_stage(
            profile, backtest, {"settled": 0}
        )
        self.assertEqual(stage, "screened")
        self.assertIn("최근 48시간 거래 없음", reasons)

    def test_live_promotion_and_demotion_use_forward_results(self):
        profile = {
            "market_maker_like": False,
            "active_age_hours": 1,
            "two_sided_rate": 0.0,
            "directional_market_count": 20,
        }
        backtest = {
            "settled": 40,
            "roi": 0.15,
            "bootstrap_p5": 0.03,
            "largest_win_share": 0.2,
        }
        forward = {
            "settled": 35,
            "roi": 0.10,
            "bootstrap_p5": 0.01,
            "max_drawdown_units": 2.0,
        }
        self.assertEqual(
            pipeline.candidate_stage(profile, backtest, forward)[0],
            "live_approved",
        )
        poor = {**forward, "roi": -0.10}
        self.assertEqual(
            pipeline.candidate_stage(
                profile, backtest, poor, previous="live_approved"
            )[0],
            "suspended",
        )
        mild_decay = {
            **forward,
            "roi": 0.02,
            "bootstrap_p5": -0.01,
        }
        self.assertEqual(
            pipeline.candidate_stage(
                profile, backtest, mild_decay, previous="live_approved"
            )[0],
            "live_approved",
        )

    def test_watchlist_caps_live_wallets(self):
        candidates = {}
        for index in range(7):
            wallet = f"0x{index:040x}"
            candidates[wallet] = {
                "stage": "paper",
                "profile": {
                    "market_maker_like": False,
                    "active_age_hours": 1,
                    "two_sided_rate": 0.0,
                    "directional_market_count": 20,
                },
                "backtest": {
                    "settled": 40,
                    "roi": 0.15,
                    "bootstrap_p5": 0.03,
                    "largest_win_share": 0.2,
                    "win_rate": 0.7,
                },
            }
        radar = {
            "wallets": {
                wallet: {"pnl_samples": [0.2] * 31}
                for wallet in candidates
            }
        }
        with patch.object(pipeline, "LIVE_MAX_WALLETS", 3):
            watchlist = pipeline.build_watchlist(candidates, radar)
        self.assertEqual(len(watchlist["live_approved"]), 3)

    def test_rejected_legacy_wallet_is_exported_to_live_blocklist(self):
        wallet = "0xlegacy"
        candidates = {
            wallet: {
                "discovery": {"legacy_live": True},
                "profile": {"market_maker_like": True},
                "backtest": {},
            }
        }
        watchlist = pipeline.build_watchlist(candidates, {})
        self.assertEqual(
            [row["wallet"] for row in watchlist["blocked_live"]],
            [wallet],
        )
        self.assertEqual(watchlist["counts"]["blocked_legacy_live"], 1)

    def test_unapproved_screened_legacy_wallet_is_also_live_blocked(self):
        wallet = "0xstalelegacy"
        candidates = {
            wallet: {
                "discovery": {"legacy_live": True},
                "profile": {
                    "market_maker_like": False,
                    "active_age_hours": 72,
                },
                "backtest": {},
            }
        }
        watchlist = pipeline.build_watchlist(candidates, {})
        self.assertEqual(candidates[wallet]["stage"], "screened")
        self.assertEqual(
            [row["wallet"] for row in watchlist["blocked_live"]],
            [wallet],
        )

    def test_radar_wallet_starts_at_current_cursor_not_historical_zero(self):
        before = int(time.time())
        state = {"wallets": {}}
        added = pipeline._sync_radar_wallets(state, {
            "paper": [{"wallet": "0xabc", "expected_win_rate": 0.7}],
            "live_approved": [],
        })
        self.assertEqual(added, 1)
        self.assertGreaterEqual(state["wallets"]["0xabc"]["last_seen_ts"], before)


if __name__ == "__main__":
    unittest.main()
