import unittest
from unittest.mock import Mock, patch

import polymarket_clob_exec as clob


class PolymarketClobExecutionTest(unittest.TestCase):
    def test_buy_prequote_consumes_asks_and_returns_vwap(self):
        response = Mock()
        response.json.return_value = {
            "asks": [
                {"price": "0.60", "size": "20"},
                {"price": "0.50", "size": "10"},
            ]
        }
        with patch.object(clob.requests, "get", return_value=response):
            quote = clob.quote_buy_usd("token", 10.0, max_price=0.60)
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["best_ask"], 0.5)
        self.assertEqual(quote["worst_ask"], 0.6)
        self.assertAlmostEqual(quote["shares"], 10 + 5 / 0.6)
        self.assertAlmostEqual(quote["vwap"], 10 / (10 + 5 / 0.6))

    def test_buy_prequote_rejects_partial_depth_inside_price_limit(self):
        response = Mock()
        response.json.return_value = {
            "asks": [
                {"price": "0.50", "size": "10"},
                {"price": "0.60", "size": "20"},
            ]
        }
        with patch.object(clob.requests, "get", return_value=response):
            quote = clob.quote_buy_usd("token", 10.0, max_price=0.55)
        self.assertFalse(quote["ok"])
        self.assertEqual(quote["fillable_usd"], 5.0)
        self.assertIn("insufficient", quote["error"])

    def test_success_with_delayed_status_is_not_a_fill(self):
        self.assertIsNone(clob._matched_fill({
            "success": True,
            "orderID": "0x1",
            "status": "delayed",
            "makingAmount": "15",
            "takingAmount": "30",
        }, side="BUY"))

    def test_matched_buy_extracts_actual_fill(self):
        fill = clob._matched_fill({
            "success": True,
            "orderID": "0x1",
            "status": "matched",
            "makingAmount": "15",
            "takingAmount": "30",
        }, side="BUY")
        self.assertEqual(fill["filled_usd"], 15)
        self.assertEqual(fill["filled_shares"], 30)
        self.assertEqual(fill["fill_price"], 0.5)

    def test_fok_buy_uses_requested_usd_not_signed_price_limit(self):
        normalized = clob._normalize_fok_buy_fill({
            "filled_usd": 11.578947,
            "filled_shares": 52.631577,
            "fill_price": 0.22,
        }, requested_usd=10.0)
        self.assertEqual(normalized["filled_usd"], 10.0)
        self.assertAlmostEqual(normalized["fill_price"], 0.19, places=3)

    def test_get_order_matched_schema_is_supported(self):
        fill = clob._matched_fill({
            "status": "ORDER_STATUS_MATCHED",
            "size_matched": "30000000",
            "price": "0.5",
            "associate_trades": ["trade-1"],
        }, side="BUY")
        self.assertEqual(fill["filled_usd"], 15)
        self.assertEqual(fill["filled_shares"], 30)
        self.assertEqual(fill["trade_ids"], ["trade-1"])

    def test_delayed_response_polls_until_matched(self):
        client = Mock()
        client.get_order.return_value = {
            "status": "ORDER_STATUS_MATCHED",
            "size_matched": "30000000",
            "price": "0.5",
        }
        with patch.object(clob.time, "sleep"):
            fill, terminal = clob._confirm_delayed_fill(
                client,
                {"success": True, "status": "delayed", "orderID": "0x1"},
                side="BUY",
                timeout_seconds=1,
            )
        self.assertEqual(fill["filled_usd"], 15)
        self.assertEqual(terminal["status"], "ORDER_STATUS_MATCHED")


if __name__ == "__main__":
    unittest.main()
