import unittest

from postmortem import build_trade_postmortem


class PostmortemCausalityTest(unittest.TestCase):
    def test_stop_is_outcome_not_primary_cause(self):
        pm = build_trade_postmortem({
            "num": 1,
            "symbol": "TEST/USDT",
            "direction": "LONG",
            "tf": "1h",
            "strategy": "EMA눌림목",
            "status": "loss",
            "pnl_usd": -1.0,
            "entry_price": 100.0,
            "sl": 99.0,
            "qty": 1.0,
            "exit_reason": "SL 손절",
        })

        self.assertEqual(pm["primary_cause"]["code"], "insufficient_causal_evidence")
        self.assertEqual(pm["causal_evidence"], "unverified")
        self.assertNotEqual(pm["primary_hypothesis"]["code"], "stop_outcome")


if __name__ == "__main__":
    unittest.main()
