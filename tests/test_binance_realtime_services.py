import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import binance_trader
import fetcher
import service_status
import trader


class BinanceRealtimeServicesTest(unittest.TestCase):
    def test_non_ascii_market_names_never_share_a_cache_file(self):
        paths = {
            fetcher._ohlcv_cache_path(symbol, "5m").name
            for symbol in ("币安人生/USDT", "龙虾/USDT", "我踏马来了/USDT")
        }
        self.assertEqual(len(paths), 3)

    def test_ohlcv_cache_reuses_completed_bar_and_refreshes_next_boundary(self):
        interval = 300_000
        now = [10 * interval + 20_000]
        expected = 9 * interval

        def raw(_symbol, _timeframe, _limit, since_ms=None, end_ms=None):
            newest = ((int(now[0]) // interval) * interval) - interval
            return [
                [newest - 2 * interval, 1, 2, 0.5, 1.5, 10],
                [newest - interval, 1.5, 2.5, 1, 2, 11],
                [newest, 2, 3, 1.5, 2.5, 12],
            ]

        with TemporaryDirectory() as tmp, patch.object(
            fetcher, "_OHLCV_CACHE_DIR", Path(tmp)
        ), patch.object(
            fetcher, "market_data_venue", return_value="binance"
        ), patch.object(
            fetcher.time, "time", side_effect=lambda: now[0] / 1000
        ), patch.object(
            fetcher, "_fetch_binance_ohlcv_raw", side_effect=raw
        ) as fetch_raw:
            first = fetcher.fetch_ohlcv("BTC/USDT", "5m", 3)
            second = fetcher.fetch_ohlcv("BTC/USDT", "5m", 3)
            self.assertEqual(fetch_raw.call_count, 1)
            self.assertEqual(int(first.index[-1].timestamp() * 1000), expected)
            self.assertEqual(second.attrs["source"], "cache")

            now[0] += interval
            third = fetcher.fetch_ohlcv("BTC/USDT", "5m", 3)
            self.assertEqual(fetch_raw.call_count, 2)
            self.assertEqual(
                int(third.index[-1].timestamp() * 1000), expected + interval
            )

    def test_monitor_fetches_account_positions_once_for_multiple_symbols(self):
        state = {
            "positions": {
                "BTC/USDT": {
                    "direction": "LONG", "entry_price": 100, "initial_qty": 1,
                    "sl_price": 95, "initial_sl_price": 95, "leverage": 2,
                },
                "ETH/USDT": {
                    "direction": "SHORT", "entry_price": 50, "initial_qty": 2,
                    "sl_price": 53, "initial_sl_price": 53, "leverage": 2,
                },
            }
        }

        class Exchange:
            calls = 0

            def load_markets(self):
                return None

            def fetch_positions(self, symbols=None):
                self.calls += 1
                return [
                    {
                        "symbol": "BTC/USDT:USDT", "contracts": 1,
                        "markPrice": 100, "entryPrice": 100, "leverage": 2,
                    },
                    {
                        "symbol": "ETH/USDT:USDT", "contracts": 2,
                        "markPrice": 50, "entryPrice": 50, "leverage": 2,
                    },
                ]

        exchange = Exchange()
        with patch.object(
            binance_trader, "is_execution_api_healthy", return_value=True
        ), patch.object(
            binance_trader, "_ex", return_value=exchange
        ), patch.object(
            trader, "_load_state", side_effect=lambda: state
        ), patch.object(trader, "_save_state", return_value=None):
            summary = binance_trader.monitor_positions()

        self.assertEqual(exchange.calls, 1)
        self.assertEqual(summary["tracked"], 2)
        self.assertEqual(summary["live"], 2)

    def test_d2_partial_tp_always_resizes_stop_to_live_quantity(self):
        state = {
            "positions": {
                "BTC/USDT": {
                    "direction": "LONG", "entry_price": 100, "initial_qty": 1,
                    "sl_price": 99, "initial_sl_price": 99, "leverage": 10,
                    "atr": 1, "opened_ts": time.time(),
                    "exit_policy": "d2_asymmetric", "tp1_lock_r": 0.2,
                    "max_hold_minutes": 90, "progress_check_minutes": 30,
                    "progress_min_r": 0.5,
                }
            }
        }

        class Exchange:
            def load_markets(self):
                return None

            def fetch_positions(self, symbols=None):
                return [{
                    "symbol": "BTC/USDT:USDT", "contracts": 0.7,
                    "markPrice": 100.1, "entryPrice": 100, "leverage": 10,
                }]

        calls = []
        with patch.object(
            binance_trader, "is_execution_api_healthy", return_value=True
        ), patch.object(
            binance_trader, "_ex", return_value=Exchange()
        ), patch.object(
            trader, "_load_state", side_effect=lambda: state
        ), patch.object(trader, "_save_state", return_value=None), patch.object(
            binance_trader,
            "_set_stop_loss",
            side_effect=lambda ex, fsym, direction, qty, stop: calls.append(
                (qty, stop)
            ) or stop,
        ):
            binance_trader.monitor_positions()

        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(calls[0][0], 0.7)
        self.assertGreaterEqual(calls[0][1], 99.0)
        self.assertTrue(state["positions"]["BTC/USDT"]["sl_qty_synced_after_tp1"])

    def test_heartbeat_freshness_requires_ok(self):
        with TemporaryDirectory() as tmp, patch.object(
            service_status, "STATUS_DIR", Path(tmp)
        ):
            service_status.write_status("manager", {"ok": True})
            self.assertTrue(service_status.heartbeat_is_fresh("manager", 10))
            service_status.write_status("manager", {"ok": False})
            self.assertFalse(service_status.heartbeat_is_fresh("manager", 10))


if __name__ == "__main__":
    unittest.main()
