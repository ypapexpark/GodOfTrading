import os
import unittest
from unittest.mock import patch

import binance_trader
import trader


class FakeExchange:
    def __init__(self, *, fail_stops=False, fail_tps=False, actual_leverage=None):
        self.fail_stops = fail_stops
        self.fail_tps = fail_tps
        self.actual_leverage = actual_leverage
        self.requested_leverage = 1
        self.position_qty = 0.0
        self.orders = []
        self.cancelled = []

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"id": "BTCUSDT", "limits": {"amount": {"min": 0.001}}}

    def amount_to_precision(self, symbol, value):
        return f"{value:.3f}"

    def price_to_precision(self, symbol, value):
        return f"{value:.2f}"

    def set_margin_mode(self, mode, symbol):
        return None

    def set_leverage(self, leverage, symbol):
        self.requested_leverage = leverage
        return None

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        params = dict(params or {})
        self.orders.append((symbol, order_type, side, amount, price, params))
        if order_type == "STOP_MARKET" and self.fail_stops:
            raise RuntimeError("stop rejected")
        if order_type == "limit" and self.fail_tps:
            raise RuntimeError("tp rejected")
        if order_type == "market" and not params.get("reduceOnly"):
            self.position_qty = float(amount)
        return {"id": f"order-{len(self.orders)}", "info": {}}

    def fetch_positions(self, symbols):
        if self.position_qty <= 0:
            return []
        return [{
            "contracts": self.position_qty,
            "entryPrice": 100.0,
            "leverage": self.actual_leverage or self.requested_leverage,
        }]

    def fetch_open_orders(self, symbol):
        return [
            {
                "id": "tp-keep",
                "type": "limit",
                "reduceOnly": True,
                "info": {"type": "LIMIT", "reduceOnly": True, "stopPrice": "0"},
            },
            {
                "id": "old-stop",
                "type": "stop_market",
                "info": {"origType": "STOP_MARKET", "reduceOnly": True, "stopPrice": "95"},
            },
        ]

    def cancel_order(self, order_id, symbol):
        self.cancelled.append(order_id)


class BinanceExecutionSafetyTest(unittest.TestCase):
    def _execute(self, fake, save_position, clear_position, tps=None):
        env = {
            "BINANCE_API_KEY": "key",
            "BINANCE_API_SECRET": "secret",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            binance_trader, "_live_enabled", return_value=True
        ), patch.object(
            binance_trader, "is_execution_api_healthy", return_value=True
        ), patch.object(
            binance_trader, "get_usdt_balance", return_value=100.0
        ), patch.object(
            binance_trader, "get_usdt_equity", return_value=100.0
        ), patch.object(
            binance_trader, "has_open_position", return_value=False
        ), patch.object(
            binance_trader, "_ex", return_value=fake
        ), patch.object(
            binance_trader.time, "sleep", return_value=None
        ), patch.object(
            trader, "check_circuit_breaker", return_value=(True, "")
        ), patch.object(
            trader, "_save_position", save_position
        ), patch.object(
            trader, "_clear_position", clear_position
        ):
            return binance_trader.execute(
                "BTC/USDT",
                "LONG",
                2,
                100.0,
                95.0,
                tps or [{"price": 110.0, "pct": 100}],
                position_pct=0.1,
                max_margin_usd=10.0,
                min_margin_usd=1.0,
            )

    def test_entry_is_saved_immediately_with_exchange_and_client_ids(self):
        fake = FakeExchange()
        saved = []
        result = self._execute(fake, lambda *a, **kw: saved.append((a, kw)), lambda *_: None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["entry_order_id"], "order-1")
        self.assertTrue(result["entry_order_link_id"].startswith("got-"))
        self.assertLessEqual(len(result["entry_order_link_id"]), 36)
        self.assertEqual(
            fake.orders[0][5]["newClientOrderId"], result["entry_order_link_id"]
        )
        self.assertEqual(saved[0][1]["entry_order_id"], "order-1")
        self.assertEqual(
            saved[0][1]["entry_order_link_id"], result["entry_order_link_id"]
        )

    def test_three_stop_failures_force_reduce_only_emergency_close(self):
        fake = FakeExchange(fail_stops=True)
        saved = []
        cleared = []
        result = self._execute(
            fake,
            lambda *a, **kw: saved.append((a, kw)),
            lambda symbol: cleared.append(symbol),
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["emergency_closed"])
        self.assertEqual(len([o for o in fake.orders if o[1] == "STOP_MARKET"]), 3)
        self.assertTrue(any(o[1] == "market" and o[5].get("reduceOnly") for o in fake.orders))
        self.assertEqual(cleared, ["BTC/USDT"])
        self.assertEqual(len(saved), 1)

    def test_three_tp_failures_force_reduce_only_emergency_close(self):
        fake = FakeExchange(fail_tps=True)
        cleared = []
        result = self._execute(
            fake,
            lambda *a, **kw: None,
            lambda symbol: cleared.append(symbol),
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["emergency_closed"])
        self.assertEqual(len(result["tp_errors"]), 1)
        self.assertEqual(len([o for o in fake.orders if o[1] == "limit"]), 3)
        self.assertTrue(any(o[1] == "STOP_MARKET" for o in fake.orders))
        self.assertTrue(any(o[1] == "market" and o[5].get("reduceOnly") for o in fake.orders))
        self.assertEqual(cleared, ["BTC/USDT"])

    def test_partial_tp_setup_is_cancelled_before_emergency_close(self):
        class PartialTpFailureExchange(FakeExchange):
            def __init__(self):
                super().__init__()
                self.limit_calls = 0

            def create_order(self, symbol, order_type, side, amount, price=None, params=None):
                if order_type == "limit":
                    self.limit_calls += 1
                    if self.limit_calls >= 2:
                        self.orders.append(
                            (symbol, order_type, side, amount, price, dict(params or {}))
                        )
                        raise RuntimeError("second tp rejected")
                return super().create_order(symbol, order_type, side, amount, price, params)

        fake = PartialTpFailureExchange()
        result = self._execute(
            fake,
            lambda *a, **kw: None,
            lambda *_: None,
            tps=[
                {"price": 105.0, "pct": 50},
                {"price": 110.0, "pct": 50},
            ],
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["emergency_closed"])
        self.assertIn("order-3", fake.cancelled)
        self.assertTrue(any(o[1] == "market" and o[5].get("reduceOnly") for o in fake.orders))

    def test_effective_leverage_that_breaks_margin_cap_forces_close(self):
        fake = FakeExchange(actual_leverage=1)
        result = self._execute(fake, lambda *a, **kw: None, lambda *_: None)

        self.assertFalse(result["ok"])
        self.assertTrue(result["emergency_closed"])
        self.assertIn("위험캡 초과", result["error"])
        self.assertTrue(any(o[1] == "market" and o[5].get("reduceOnly") for o in fake.orders))

    def test_raw_binance_leverage_prevents_false_five_x_margin_alarm(self):
        class RawLeverageExchange(FakeExchange):
            def fetch_positions(self, symbols):
                if self.position_qty <= 0:
                    return []
                return [{
                    "contracts": self.position_qty,
                    "entryPrice": 100.0,
                    "leverage": None,
                    "initialMargin": None,
                    "initialMarginPercentage": 1 / self.requested_leverage,
                    "info": {
                        "leverage": None,
                        "positionInitialMargin": str(
                            self.position_qty * 100.0 / self.requested_leverage
                        ),
                    },
                }]

        fake = RawLeverageExchange()
        result = self._execute(fake, lambda *a, **kw: None, lambda *_: None)

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["leverage"], 2)
        self.assertAlmostEqual(
            result["actual_margin_usd"], result["margin_usd"], places=6
        )
        self.assertFalse(any(o[1] == "market" and o[5].get("reduceOnly") for o in fake.orders))

    def test_tradfi_perpetual_is_rejected_before_entry_order(self):
        class TradFiExchange(FakeExchange):
            def market(self, symbol):
                return {
                    "id": "AAPLUSDT",
                    "limits": {"amount": {"min": 0.001}},
                    "info": {"contractType": "TRADIFI_PERPETUAL"},
                }

        fake = TradFiExchange()
        result = self._execute(fake, lambda *a, **kw: None, lambda *_: None)

        self.assertFalse(result["ok"])
        self.assertIn("TRADIFI_PERPETUAL", result["error"])
        self.assertEqual(fake.orders, [])

    def test_stop_replacement_keeps_reduce_only_take_profit(self):
        fake = FakeExchange()

        price = binance_trader._set_stop_loss(
            fake, "BTC/USDT:USDT", "LONG", 0.1, 99.0
        )

        self.assertEqual(price, 99.0)
        self.assertEqual(fake.cancelled, ["old-stop"])
        self.assertNotIn("tp-keep", fake.cancelled)

    def test_stop_replacement_cancels_old_algo_stop(self):
        class AlgoExchange(FakeExchange):
            def __init__(self):
                super().__init__()
                self.cancelled_algos = []

            def fapiPrivateGetOpenAlgoOrders(self):
                return [
                    {
                        "algoId": 101,
                        "symbol": "BTCUSDT",
                        "orderType": "STOP_MARKET",
                        "algoStatus": "NEW",
                    },
                    {
                        "algoId": 102,
                        "symbol": "ETHUSDT",
                        "orderType": "STOP_MARKET",
                        "algoStatus": "NEW",
                    },
                ]

            def fapiPrivateDeleteAlgoOrder(self, params):
                self.cancelled_algos.append(params["algoId"])

        fake = AlgoExchange()
        binance_trader._set_stop_loss(
            fake, "BTC/USDT:USDT", "LONG", 0.1, 99.0
        )

        self.assertEqual(fake.cancelled_algos, [101])

    def test_cleanup_removes_orphan_and_duplicate_reduce_only_stops(self):
        class CleanupExchange:
            def __init__(self):
                self.cancelled = []
                self.cancelled_algos = []

            def fetch_positions(self):
                return [{
                    "symbol": "BTC/USDT:USDT",
                    "contracts": 0.7,
                }]

            def fetch_open_orders(self):
                return [
                    {
                        "id": "eth-orphan-tp",
                        "symbol": "ETH/USDT:USDT",
                        "reduceOnly": True,
                        "info": {},
                    },
                    {
                        "id": "manual-entry",
                        "symbol": "BTC/USDT:USDT",
                        "reduceOnly": False,
                        "info": {},
                    },
                ]

            def cancel_order(self, order_id, symbol):
                self.cancelled.append(order_id)

            def fapiPrivateGetOpenAlgoOrders(self):
                return [
                    {
                        "algoId": 1, "symbol": "BTCUSDT",
                        "orderType": "STOP_MARKET", "algoStatus": "NEW",
                        "reduceOnly": True, "quantity": "0.7",
                        "triggerPrice": "99",
                    },
                    {
                        "algoId": 2, "symbol": "BTCUSDT",
                        "orderType": "STOP_MARKET", "algoStatus": "NEW",
                        "reduceOnly": True, "quantity": "1.0",
                        "triggerPrice": "95",
                    },
                    {
                        "algoId": 3, "symbol": "ETHUSDT",
                        "orderType": "STOP_MARKET", "algoStatus": "NEW",
                        "reduceOnly": True, "quantity": "2.0",
                        "triggerPrice": "45",
                    },
                ]

            def fapiPrivateDeleteAlgoOrder(self, params):
                self.cancelled_algos.append(params["algoId"])

        fake = CleanupExchange()
        state = {"positions": {"BTC/USDT": {"sl_price": 99.0}}}
        with patch.object(
            binance_trader, "api_backoff_remaining", return_value=0
        ), patch.object(
            binance_trader, "_ex", return_value=fake
        ), patch.object(trader, "_load_state", return_value=state):
            result = binance_trader.cleanup_orphan_protective_orders()

        self.assertTrue(result["ok"])
        self.assertEqual(result["cancelled"], 3)
        self.assertEqual(fake.cancelled, ["eth-orphan-tp"])
        self.assertEqual(fake.cancelled_algos, [2, 3])

    def test_realized_pnl_uses_1000_limit_and_keeps_attribution_ids(self):
        class TradeExchange:
            calls = []

            def fetch_my_trades(self, symbol, since, limit):
                self.calls.append((symbol, since, limit))
                return [{
                    "id": "trade-1",
                    "timestamp": since,
                    "info": {"realizedPnl": "2.5", "commission": "0.1"},
                }]

        fake = TradeExchange()
        pnl, info = binance_trader._realized_pnl_since(
            fake, "BTC/USDT:USDT", 1234, "entry-1", "client-1"
        )

        self.assertAlmostEqual(pnl, 2.4)
        self.assertEqual(fake.calls[0][2], 1000)
        self.assertEqual(info["entry_order_id"], "entry-1")
        self.assertEqual(info["entry_order_link_id"], "client-1")

    def test_seed_sizing_scales_margin_and_loss_with_live_equity(self):
        fake = FakeExchange()

        full = binance_trader._seed_sizing_plan(
            "BTC/USDT", 100.0, 98.0, 10,
            173.28364464, 173.28364464, 0.50, 100.0, fake,
        )
        small = binance_trader._seed_sizing_plan(
            "BTC/USDT", 100.0, 98.0, 10,
            50.0, 50.0, 0.50, 100.0, fake,
        )

        self.assertTrue(full["ok"])
        self.assertTrue(small["ok"])
        self.assertLessEqual(full["margin_usd"], 173.28364464 * 0.10)
        self.assertLessEqual(full["estimated_sl_loss_usd"], 173.28364464 * 0.005)
        self.assertLessEqual(small["margin_usd"], 50.0 * 0.10)
        self.assertLessEqual(small["estimated_sl_loss_usd"], 50.0 * 0.005)
        self.assertLess(small["margin_usd"], full["margin_usd"])

    def test_exchange_minimum_never_raises_requested_leverage(self):
        class LargeMinimumExchange(FakeExchange):
            def market(self, symbol):
                return {
                    "id": "BTCUSDT",
                    "limits": {"amount": {"min": 1.0}, "cost": {"min": 5.0}},
                }

        plan = binance_trader._calc_order_plan(
            "BTC/USDT", 100.0, 2, 10.0, 0.10, 1.0,
            LargeMinimumExchange(),
        )

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["leverage"], 2)
        self.assertIn("레버리지 자동상향 금지", plan["error"])

    def test_minimum_notional_blocks_instead_of_oversizing_seed(self):
        class MinimumCostExchange(FakeExchange):
            def market(self, symbol):
                return {
                    "id": "BTCUSDT",
                    "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
                }

        plan = binance_trader._calc_order_plan(
            "BTC/USDT", 100.0, 2, 10.0, 0.10, 1.0,
            MinimumCostExchange(),
        )

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["leverage"], 2)
        self.assertIn("거래소 최소주문", plan["error"])


if __name__ == "__main__":
    unittest.main()
