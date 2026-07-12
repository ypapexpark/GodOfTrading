import unittest
from unittest.mock import Mock, patch

import polymarket_clob_exec as clob


class PolymarketClobExecutionTest(unittest.TestCase):
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
