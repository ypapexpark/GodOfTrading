import unittest

from process_lock import main_run_lock_name


class ProcessLockNameTest(unittest.TestCase):
    def test_live_execution_lock_is_shared_by_full_and_fast(self):
        full = main_run_lock_name(venue="bybit", fast=False, auto_trade=True)
        fast = main_run_lock_name(venue="bybit", fast=True, auto_trade=True)
        self.assertEqual(full, fast)
        self.assertEqual(full, "main_bybit_live_execution")

    def test_read_only_scanners_keep_separate_locks(self):
        self.assertNotEqual(
            main_run_lock_name(venue="bybit", fast=False),
            main_run_lock_name(venue="bybit", fast=True),
        )


if __name__ == "__main__":
    unittest.main()
