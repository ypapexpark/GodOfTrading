import base64
import hashlib
import hmac
import json
import unittest

import bithumb_wallet_radar as radar


def _decode(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class BithumbWalletRadarTest(unittest.TestCase):
    def test_jwt_is_hs256_and_contains_no_secret(self):
        token = radar.make_jwt("access", "very-secret", now_ms=1234, nonce="nonce-1")
        header_part, payload_part, signature_part = token.split(".")
        self.assertEqual({"alg": "HS256", "typ": "JWT"}, _decode(header_part))
        self.assertEqual(
            {"access_key": "access", "nonce": "nonce-1", "timestamp": 1234},
            _decode(payload_part),
        )
        expected = hmac.new(
            b"very-secret", f"{header_part}.{payload_part}".encode(), hashlib.sha256
        ).digest()
        actual = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
        self.assertEqual(expected, actual)
        self.assertNotIn("very-secret", token)

    def test_notice_classifier_separates_security_scheduled_and_resume(self):
        self.assertEqual(
            "emergency_security",
            radar.classify_notice({"categories": ["입출금"], "title": "ABC 보안 이슈 입출금 중단"}),
        )
        self.assertEqual(
            "scheduled",
            radar.classify_notice({"categories": ["입출금"], "title": "ABC 네트워크 업그레이드 예정"}),
        )
        self.assertEqual(
            "resume",
            radar.classify_notice({"categories": ["입출금"], "title": "ABC 입출금 재개"}),
        )

    def test_market_history_detects_early_turnover_acceleration(self):
        state = radar._default_state()
        start = 1_800_000_000.0
        acc = 1_000_000_000.0
        for minute in range(140):
            # Complete each prior minute by advancing the clock.  The last 15m
            # receives 5x the normal quote turnover while price remains below
            # the 'already vertical' ceiling.
            qvol = 1_000_000.0 * (5 if minute >= 125 else 1)
            acc += qvol
            price = 100.0 * (1 + min(minute, 139) * 0.0002)
            radar.update_market_stats(
                state,
                {"KRW-TEST": {
                    "trade_price": price,
                    "acc_trade_price": acc,
                    "trade_date_kst": "20270101",
                }},
                start + minute * 60,
            )
        features = radar.market_features(state, "KRW-TEST", start + 139 * 60)
        self.assertIsNotNone(features)
        self.assertGreaterEqual(features["qvol_ratio"], 3.0)
        self.assertTrue(radar.qualifies_precursor(features))

    def test_wallet_lag_threshold_is_adaptive_but_never_too_low(self):
        self.assertEqual(10.0, radar.wallet_lag_threshold([0, 1, 2]))
        threshold = radar.wallet_lag_threshold([0] * 19 + [20])
        self.assertGreaterEqual(threshold, 10.0)

    def test_notice_assets_extracts_symbols(self):
        self.assertEqual(
            ["ABC", "XYZ"],
            radar.notice_assets({"title": "에이비씨(ABC), 엑스와이지(XYZ) 입출금 중단"}),
        )


if __name__ == "__main__":
    unittest.main()
