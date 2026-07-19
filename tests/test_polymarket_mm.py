from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import polymarket_mm_bot as mm
import polymarket_mm_exec as mm_exec
import polymarket_mm_stream as mm_stream


def book(token, bid, ask, *, tick=0.01, bid_size=20, min_size=5):
    return {
        "token_id": token,
        "condition_id": "c",
        "tick_size": tick,
        "min_order_size": min_size,
        "best_bid": bid,
        "best_ask": ask,
        "spread": ask - bid,
        "mid": (bid + ask) / 2,
        "bids": [(bid, bid_size)],
        "asks": [(ask, bid_size)],
    }


def market_state():
    return {
        "condition_id": "c",
        "gamma_market_id": "m",
        "title": "Will test happen?",
        "tokens": ["yes", "no"],
        "neg_risk": False,
        "inventory": [0.0, 0.0],
        "inventory_cost": [0.0, 0.0],
        "inventory_since_ts": [0.0, 0.0],
        "fills": 0,
        "pairs": 0.0,
        "realized_pnl": 0.0,
        "exit_pnl": 0.0,
        "active": True,
    }


class PolymarketMarketMakerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal_patch = patch.object(
            mm, "JOURNAL_FILE", Path(self.temp_dir.name) / "mm-test.jsonl"
        )
        self.journal_patch.start()

    def tearDown(self):
        self.journal_patch.stop()
        self.temp_dir.cleanup()

    def test_neg_risk_binary_market_is_allowed_in_paper_filter(self):
        row = {
            "acceptingOrders": True,
            "enableOrderBook": True,
            "negRisk": True,
            "clobTokenIds": '["a","b"]',
            "outcomePrices": '["0.5","0.5"]',
            "liquidityNum": 10000,
            "volume24hr": 10000,
            "endDate": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "rewardsMinSize": 5,
        }
        self.assertEqual(mm.cheap_market_filter(row), (True, ""))

    def test_large_reward_minimum_does_not_block_small_spread_quote(self):
        row = {
            "acceptingOrders": True,
            "enableOrderBook": True,
            "negRisk": False,
            "clobTokenIds": '["a","b"]',
            "outcomePrices": '["0.5","0.5"]',
            "liquidityNum": 10000,
            "volume24hr": 10000,
            "endDate": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "rewardsMinSize": 200,
        }
        self.assertEqual(mm.cheap_market_filter(row), (True, ""))

    def test_one_tick_spread_is_not_deep_enough_to_quote_post_only(self):
        row = {
            "conditionId": "c", "id": "m", "clobTokenIds": '["yes","no"]',
            "outcomePrices": '["0.5","0.5"]', "outcomes": '["Yes","No"]',
            "liquidityNum": 10000, "volume24hr": 10000,
        }
        books = [book("yes", 0.49, 0.50), book("no", 0.50, 0.51)]
        self.assertIsNone(mm.deep_market_candidate(row, books))

    def test_flat_inventory_quotes_both_outcomes_with_locked_edge(self):
        books = [book("yes", 0.48, 0.52), book("no", 0.48, 0.52)]
        quotes = mm.quote_targets(market_state(), books, cash=100, total_committed=0)
        self.assertEqual(len(quotes), 2)
        self.assertTrue(all(row["side"] == "BUY" for row in quotes))
        self.assertLessEqual(sum(row["price"] for row in quotes), mm.MAX_PAIR_COST)
        self.assertTrue(all(row["size"] >= 5 for row in quotes))

    def test_one_sided_inventory_sells_excess_and_buys_complement(self):
        market = market_state()
        market["inventory"] = [5.0, 0.0]
        market["inventory_cost"] = [2.0, 0.0]
        books = [book("yes", 0.42, 0.46), book("no", 0.52, 0.56)]
        quotes = mm.quote_targets(market, books, cash=100, total_committed=2)
        legs = {(row["outcome_index"], row["side"]) for row in quotes}
        self.assertIn((0, "SELL"), legs)
        self.assertIn((1, "BUY"), legs)
        self.assertNotIn((0, "BUY"), legs)

    def test_flat_inventory_never_posts_only_one_affordable_side(self):
        books = [book("yes", 0.78, 0.82), book("no", 0.18, 0.22)]
        quotes = mm.quote_targets(market_state(), books, cash=2, total_committed=0)
        self.assertEqual(quotes, [])

    def test_market_universe_uses_keyset_past_hundred_row_cap(self):
        rows = [{"id": str(index), "volume24hr": 10000} for index in range(250)]

        def fake_get_json(_url, params=None, timeout=15):
            offset = int((params or {}).get("after_cursor") or 0)
            limit = int((params or {}).get("limit", 100))
            end = min(offset + limit, len(rows))
            return {
                "markets": rows[offset:end],
                "next_cursor": str(end) if end < len(rows) else "",
            }

        with (
            patch.object(mm, "UNIVERSE_LIMIT", 250),
            patch.object(mm, "_get_json", side_effect=fake_get_json) as get_json,
        ):
            universe, truncated = mm.fetch_market_universe()
        self.assertEqual(len(universe), 250)
        self.assertFalse(truncated)
        self.assertEqual(get_json.call_count, 3)

    def test_stream_updates_l2_book_and_normalizes_trade(self):
        self.assertEqual(
            mm_stream.parse_message('{"event_type":"book","asset_id":"yes"}')[0]["asset_id"],
            "yes",
        )
        state = mm_stream.new_stream_state()
        mm_stream.apply_event(state, {
            "event_type": "book", "asset_id": "yes", "market": "c",
            "bids": [{"price": "0.48", "size": "10"}],
            "asks": [{"price": "0.52", "size": "10"}],
        })
        mm_stream.apply_event(state, {
            "event_type": "price_change", "timestamp": "2",
            "price_changes": [{
                "asset_id": "yes", "side": "BUY", "price": "0.49", "size": "7",
            }],
        })
        mm_stream.apply_event(state, {
            "event_type": "last_trade_price", "asset_id": "yes", "market": "c",
            "side": "SELL", "price": "0.49", "size": "5", "timestamp": "3",
        })
        self.assertEqual(state["books"]["yes"]["bids"][0]["price"], "0.49")
        self.assertEqual(state["trades"][-1]["asset"], "yes")
        self.assertEqual(state["trades"][-1]["side"], "SELL")
        self.assertEqual(state["event_types"]["last_trade_price"], 1)

    def test_fresh_stream_snapshot_supplies_books_without_rest(self):
        stream_file = Path(self.temp_dir.name) / "stream.json"
        stream_file.write_text(json.dumps({
            "connected": True,
            "heartbeat_ts": __import__("time").time(),
            "tokens": ["yes", "no"],
            "books": {
                "yes": {"bids": [{"price": "0.48", "size": "5"}],
                        "asks": [{"price": "0.52", "size": "5"}]},
                "no": {"bids": [{"price": "0.48", "size": "5"}],
                       "asks": [{"price": "0.52", "size": "5"}]},
            },
            "trades": [], "event_count": 2, "event_types": {"book": 2},
        }), encoding="utf-8")
        market = market_state()
        market["tick_sizes"] = [0.01, 0.01]
        market["min_order_sizes"] = [5, 5]
        with patch.object(mm, "STREAM_STATE_FILE", stream_file):
            books, trades, stats = mm.load_stream_market_data([market])
        self.assertEqual(len(books["c"]), 2)
        self.assertEqual(trades, [])
        self.assertTrue(stats["fresh"])

    def test_discovery_hysteresis_keeps_existing_market_inside_pool(self):
        snapshot_file = Path(self.temp_dir.name) / "candidates.json"
        snapshot_file.write_text(json.dumps({
            "ok": True, "generated_ts": 100, "generated_at": "now",
            "duration_seconds": 1,
            "candidates": [
                {"condition_id": "new", "tokens": ["n1", "n2"]},
                {"condition_id": "keep", "tokens": ["k1", "k2"]},
                {"condition_id": "other", "tokens": ["o1", "o2"]},
            ],
            "rejections": {"_universe_scanned": 3},
        }), encoding="utf-8")
        state = mm._new_state()
        state["selected_conditions"] = ["keep"]
        with (
            patch.object(mm, "DISCOVERY_SNAPSHOT_FILE", snapshot_file),
            patch.object(mm, "MAX_MARKETS", 2),
        ):
            applied, error = mm.apply_discovery_snapshot(state)
        self.assertTrue(applied)
        self.assertEqual(error, "")
        self.assertEqual(state["selected_conditions"], ["keep", "new"])

    def test_queue_model_does_not_fill_before_ahead_size_is_consumed(self):
        state = mm._new_state()
        state["trade_bootstrap_complete"] = True
        state["markets"] = {"c": market_state()}
        state["orders"] = {"c:0": {
            "condition_id": "c", "token_id": "yes", "outcome_index": 0,
            "price": 0.48, "remaining": 5.0, "queue_ahead": 10.0,
        }}
        first = {"transactionHash": "1", "asset": "yes", "side": "SELL",
                 "price": 0.48, "size": 8, "timestamp": 1}
        second = {"transactionHash": "2", "asset": "yes", "side": "SELL",
                  "price": 0.48, "size": 7, "timestamp": 2}
        self.assertEqual(mm.process_paper_trades(state, [first]), 0)
        self.assertEqual(mm.process_paper_trades(state, [first, second]), 1)
        self.assertAlmostEqual(state["markets"]["c"]["inventory"][0], 5.0)
        self.assertEqual(state["fills"], 1)

    def test_complete_pair_merge_locks_spread_profit(self):
        state = mm._new_state()
        state["cash"] = 95.2
        market = market_state()
        market["inventory"] = [5.0, 5.0]
        market["inventory_cost"] = [2.4, 2.4]
        market["inventory_since_ts"] = [1.0, 1.0]
        shares = mm.merge_complete_sets(state, market)
        self.assertEqual(shares, 5.0)
        self.assertAlmostEqual(state["cash"], 100.2)
        self.assertAlmostEqual(state["realized_pnl"], 0.2)
        self.assertEqual(market["inventory"], [0.0, 0.0])

    def test_maintenance_merge_does_not_count_as_performance_cycle(self):
        state = mm._new_state()
        state["cash"] = 95.0
        market = market_state()
        market["inventory"] = [5.0, 5.0]
        market["inventory_cost"] = [2.5, 2.5]
        mm.merge_complete_sets(state, market, count_performance=False)
        self.assertEqual(state["pair_cycles"], 0)
        self.assertEqual(state["paired_shares"], 0)
        self.assertEqual(state["cash"], 100.0)

    def test_stale_unmatched_inventory_exits_at_buffered_bid(self):
        state = mm._new_state()
        state["cash"] = 97.5
        market = market_state()
        market["inventory"] = [5.0, 0.0]
        market["inventory_cost"] = [2.5, 0.0]
        market["inventory_since_ts"] = [1.0, 0.0]
        pnl = mm.exit_stale_inventory(
            state, market, [book("yes", 0.40, 0.44), book("no", 0.56, 0.60)]
        )
        self.assertLess(pnl, 0)
        self.assertEqual(market["inventory"][0], 0.0)

    def test_promotion_requires_time_fills_pairs_profit_and_drawdown(self):
        state = mm._new_state()
        state.update({
            "started_ts": 1,
            "fills": mm.PROMOTION_MIN_FILLS,
            "sell_fills": mm.PROMOTION_MIN_SELL_FILLS,
            "pair_cycles": mm.PROMOTION_MIN_PAIRS,
            "realized_pnl": mm.PROMOTION_MIN_REALIZED + 1,
            "maker_spread_pnl": mm.PROMOTION_MIN_REALIZED + 1,
            "rebalance_pnl": 0,
            "equity": mm.PAPER_INITIAL_CASH + 1,
            "max_drawdown": 0,
        })
        self.assertTrue(mm.promotion_status(state)["approved"])
        state["fills"] = 1
        self.assertFalse(mm.promotion_status(state)["approved"])

    def test_split_inventory_funds_two_sided_sell_quotes(self):
        state = mm._new_state()
        market = market_state()
        state["markets"] = {"c": market}
        books = [book("yes", 0.48, 0.52), book("no", 0.48, 0.52)]
        split = mm.ensure_split_inventory(state, market, books)
        self.assertEqual(split, mm.TARGET_INVENTORY_SHARES)
        self.assertAlmostEqual(state["cash"], mm.PAPER_INITIAL_CASH - split)
        self.assertEqual(market["inventory"], [split, split])
        quotes = mm.quote_targets(market, books, cash=state["cash"], total_committed=split)
        self.assertEqual(
            {(row["outcome_index"], row["side"]) for row in quotes},
            {(0, "BUY"), (0, "SELL"), (1, "BUY"), (1, "SELL")},
        )

    def test_balanced_split_inventory_marks_at_merge_value(self):
        state = mm._new_state()
        market = market_state()
        market["inventory"] = [10.0, 10.0]
        market["inventory_cost"] = [5.0, 5.0]
        state["cash"] = 90.0
        state["markets"] = {"c": market}
        equity = mm.mark_equity(
            state, {"c": [book("yes", 0.40, 0.60), book("no", 0.40, 0.60)]}
        )
        self.assertEqual(equity, 100.0)

    def test_public_buy_trade_fills_our_sell_quote(self):
        state = mm._new_state()
        state["trade_bootstrap_complete"] = True
        market = market_state()
        market["inventory"] = [10.0, 10.0]
        market["inventory_cost"] = [5.0, 5.0]
        state["markets"] = {"c": market}
        state["orders"] = {"c:0:SELL": {
            "condition_id": "c", "token_id": "yes", "outcome_index": 0,
            "side": "SELL", "price": 0.52, "size": 5.0,
            "remaining": 5.0, "queue_ahead": 0.0,
        }}
        trade = {"transactionHash": "sell-fill", "asset": "yes", "side": "BUY",
                 "price": 0.52, "size": 5, "timestamp": 1}
        self.assertEqual(mm.process_paper_trades(state, [trade]), 1)
        self.assertEqual(state["sell_fills"], 1)
        self.assertAlmostEqual(market["inventory"][0], 5.0)
        self.assertGreater(state["realized_pnl"], 0)

    def test_first_trade_snapshot_only_seeds_baseline(self):
        state = mm._new_state()
        market = market_state()
        state["markets"] = {"c": market}
        state["orders"] = {"c:0:BUY": {
            "condition_id": "c", "token_id": "yes", "outcome_index": 0,
            "side": "BUY", "price": 0.48, "size": 5.0,
            "remaining": 5.0, "queue_ahead": 0.0,
        }}
        trade = {"transactionHash": "old", "asset": "yes", "side": "SELL",
                 "price": 0.48, "size": 5, "timestamp": 1}
        self.assertEqual(mm.process_paper_trades(state, [trade]), 0)
        self.assertTrue(state["trade_bootstrap_complete"])
        self.assertEqual(state["fills"], 0)
        self.assertEqual(market["flow"][0]["sell"], 0)

    def test_toxic_sell_flow_pauses_balanced_buy_pair(self):
        market = market_state()
        market["inventory"] = [10.0, 10.0]
        market["inventory_cost"] = [5.0, 5.0]
        market["flow"] = [
            {"buy": 0.0, "sell": 100.0, "updated_ts": __import__("time").time()},
            {"buy": 0.0, "sell": 0.0, "updated_ts": 0.0},
        ]
        quotes = mm.quote_targets(
            market, [book("yes", 0.48, 0.52), book("no", 0.48, 0.52)],
            cash=100, total_committed=10,
        )
        self.assertFalse(any(row["side"] == "BUY" for row in quotes))
        self.assertTrue(any(row["side"] == "SELL" for row in quotes))

    def test_requote_keeps_queue_when_target_moves_less_than_two_ticks(self):
        state = mm._new_state()
        market = market_state()
        target = {
            "condition_id": "c", "token_id": "yes", "outcome_index": 0,
            "side": "BUY", "price": 0.48, "size": 5.0, "tick_size": 0.01,
            "queue_ahead": 10.0,
        }
        self.assertEqual(mm._replace_paper_quotes(state, market, [target]), (1, 0))
        moved = {**target, "price": 0.49, "queue_ahead": 0.0}
        self.assertEqual(mm._replace_paper_quotes(state, market, [moved]), (0, 0))
        self.assertEqual(state["orders"]["c:0:BUY"]["price"], 0.48)

    def test_v1_migration_archives_loss_and_starts_v2_at_equity(self):
        legacy = {
            "version": 1, "started_at": "old", "cash": 91, "equity": 92,
            "realized_pnl": -8, "fills": 50, "pair_cycles": 2,
            "markets": {}, "orders": {},
        }
        migrated = mm._migrate_state(legacy)
        self.assertEqual(migrated["version"], 2)
        self.assertEqual(migrated["initial_cash"], 92)
        self.assertEqual(migrated["legacy_v1_summary"]["realized_pnl"], -8)

    def test_mm_execution_adapter_is_dry_run_by_default(self):
        with patch.object(mm_exec, "mm_live_enabled", return_value=False):
            result = mm_exec.post_buy_quotes([
                {"token_id": "yes", "price": 0.48, "size": 5},
                {"token_id": "no", "price": 0.48, "size": 5},
            ])
        self.assertTrue(all(row["ok"] and row["dry_run"] for row in result))

    def test_mm_execution_adapter_accepts_sell_in_dry_run(self):
        with patch.object(mm_exec, "mm_live_enabled", return_value=False):
            result = mm_exec.post_quotes([
                {"token_id": "yes", "side": "SELL", "price": 0.52, "size": 5},
            ])
        self.assertTrue(result[0]["ok"] and result[0]["dry_run"])
        self.assertEqual(result[0]["side"], "SELL")

    def test_report_escapes_promotion_comparison_for_telegram_html(self):
        state = mm._new_state()
        state["equity"] = 100
        state["promotion"] = {"approved": False, "reasons": ["maker fill 0 < 100"]}
        report = mm.build_report(state)
        self.assertIn("maker fill 0 &lt; 100", report)
        self.assertNotIn("maker fill 0 < 100", report)


if __name__ == "__main__":
    unittest.main()
