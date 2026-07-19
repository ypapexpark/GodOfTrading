import unittest
from unittest.mock import patch

import trader


class FakeBybitExchange:
    def __init__(self, fail_tp: bool = False):
        self.fail_tp = fail_tp
        self.position_qty = 0.0
        self.requested_leverage = 1
        self.orders = []
        self.cancelled_all = 0

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.001}}}

    def amount_to_precision(self, symbol, value):
        return f"{value:.3f}"

    def set_leverage(self, leverage, symbol):
        self.requested_leverage = leverage

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        params = dict(params or {})
        self.orders.append((symbol, order_type, side, amount, price, params))
        if order_type == "limit" and self.fail_tp:
            raise RuntimeError("tp rejected")
        if order_type == "market" and not params.get("reduceOnly") and not params.get("stopOrderType"):
            self.position_qty = float(amount)
        if order_type == "market" and params.get("reduceOnly") and not params.get("stopOrderType"):
            self.position_qty = 0.0
        return {"id": f"order-{len(self.orders)}"}

    def fetch_positions(self, symbols, params=None):
        if self.position_qty <= 0:
            return []
        return [{
            "contracts": self.position_qty,
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "leverage": self.requested_leverage,
        }]

    def cancel_all_orders(self, symbol, params=None):
        self.cancelled_all += 1


class BybitExecutionSafetyTest(unittest.TestCase):
    def _execute(self, fake, **extra):
        with patch.object(
            trader, "get_usdt_balance", return_value=100.0
        ), patch.object(
            trader, "get_usdt_equity", return_value=100.0
        ), patch.object(
            trader, "check_circuit_breaker", return_value=(True, "ok")
        ), patch.object(
            trader, "has_open_position", return_value=False
        ), patch.object(
            trader, "_ex", return_value=fake
        ), patch.object(
            trader, "_save_position"
        ) as save_position, patch.object(
            trader, "_clear_position"
        ) as clear_position, patch.object(
            trader.time, "sleep", return_value=None
        ):
            result = trader.execute(
                "BTC/USDT",
                "LONG",
                3,
                100.0,
                99.0,
                [{"price": 102.0, "pct": 100}],
                position_pct=0.08,
                max_margin_usd=8.0,
                min_margin_usd=1.0,
                require_full_protection=True,
                position_meta={"engine_version": "test-s1", "max_hold_minutes": 90},
                **extra,
            )
        return result, save_position, clear_position

    def test_risk_cap_blocks_before_market_order(self):
        fake = FakeBybitExchange()
        result, save_position, _ = self._execute(fake, max_sl_loss_usd=0.10)

        self.assertFalse(result["ok"])
        self.assertIn("예상 SL손실", result["error"])
        self.assertEqual(fake.orders, [])
        save_position.assert_not_called()

    def test_actual_fill_is_saved_with_engine_metadata(self):
        fake = FakeBybitExchange()
        result, save_position, _ = self._execute(fake, max_sl_loss_usd=1.0)

        self.assertTrue(result["ok"])
        self.assertLessEqual(result["estimated_sl_loss_usd"], 1.0)
        self.assertEqual(result["entry_price"], 100.0)
        self.assertEqual(
            save_position.call_args.kwargs["position_meta"]["engine_version"],
            "test-s1",
        )

    def test_tp_failure_closes_and_clears_only_after_close(self):
        fake = FakeBybitExchange(fail_tp=True)
        result, _, clear_position = self._execute(fake, max_sl_loss_usd=1.0)

        self.assertFalse(result["ok"])
        self.assertTrue(result["emergency_closed"])
        self.assertTrue(any(o[5].get("reduceOnly") and not o[5].get("stopOrderType") for o in fake.orders))
        clear_position.assert_called_once_with("BTC/USDT")


if __name__ == "__main__":
    unittest.main()
