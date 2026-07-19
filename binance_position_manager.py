#!/usr/bin/env python3
"""Always-on Binance position manager, independent from universe scans."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

# These must be selected before trader/venue_runtime are imported.
os.environ.setdefault("AUTO_TRADE_EXCHANGE", "binance")
os.environ.setdefault("GOT_MARKET_DATA_EXCHANGE", "binance")
os.environ.setdefault("GOT_STATE_NAMESPACE", "binance")

from binance_trader import (
    cleanup_orphan_protective_orders,
    is_execution_api_healthy,
    maybe_alert_execution_api_down,
    monitor_positions,
    probe_execution_api,
)
from binance_api_guard import api_backoff_remaining
from process_lock import acquire_wait, release, try_acquire
from service_status import write_status

SERVICE_NAME = "binance_position_manager"
INTERVAL_SECONDS = max(
    2.0,
    float(os.getenv("BINANCE_POSITION_MANAGER_INTERVAL_SECONDS", "5") or 5),
)
PROBE_INTERVAL_SECONDS = max(
    15.0,
    float(os.getenv("BINANCE_POSITION_MANAGER_PROBE_SECONDS", "60") or 60),
)
_stop = threading.Event()


def _request_stop(*_args) -> None:
    _stop.set()


def run() -> int:
    if not try_acquire(SERVICE_NAME):
        return 0
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    last_probe = 0.0
    last_reconcile = 0.0
    backoff_until = 0.0
    cycles = 0
    while not _stop.is_set():
        started = time.time()
        try:
            shared_backoff = api_backoff_remaining()
            if shared_backoff > 0:
                write_status(
                    SERVICE_NAME,
                    {
                        "ok": False,
                        "api_backoff": True,
                        "backoff_seconds": round(shared_backoff, 1),
                        "cycles": cycles,
                        "interval_seconds": INTERVAL_SECONDS,
                    },
                )
                _stop.wait(min(shared_backoff, 30.0))
                continue
            if started < backoff_until:
                write_status(
                    SERVICE_NAME,
                    {
                        "ok": False,
                        "backoff": True,
                        "backoff_seconds": round(backoff_until - started, 1),
                        "cycles": cycles,
                        "interval_seconds": INTERVAL_SECONDS,
                    },
                )
                _stop.wait(min(INTERVAL_SECONDS, backoff_until - started))
                continue
            if started - last_probe >= PROBE_INTERVAL_SECONDS:
                probe_execution_api()
                last_probe = started
            if not is_execution_api_healthy():
                maybe_alert_execution_api_down()
                summary = {
                    "ok": False,
                    "error": "private API unhealthy",
                    "tracked": 0,
                }
            else:
                if not acquire_wait("binance_account_state_cycle", timeout=2.0):
                    summary = {
                        "ok": True,
                        "skipped": "entry/state update in progress",
                        "tracked": 0,
                    }
                else:
                    try:
                        summary = monitor_positions()
                        error_text = str(
                            (summary or {}).get("error") or ""
                        ).lower()
                        if "too many requests" in error_text or "429" in error_text:
                            backoff_until = time.time() + 60.0
                        if started - last_reconcile >= PROBE_INTERVAL_SECONDS:
                            # Orphan/live-ledger reconciliation belongs to the
                            # account manager, not the market scanner.
                            try:
                                from main import _reconcile_orphan_positions
                                _reconcile_orphan_positions()
                                cleanup_orphan_protective_orders()
                            except Exception as reconcile_exc:
                                print(
                                    "[Binance position manager] reconcile error: "
                                    f"{reconcile_exc}"
                                )
                            last_reconcile = started
                    finally:
                        release("binance_account_state_cycle")
            cycles += 1
            write_status(
                SERVICE_NAME,
                {
                    **(summary or {}),
                    "cycles": cycles,
                    "interval_seconds": INTERVAL_SECONDS,
                    "cycle_seconds": round(time.time() - started, 3),
                },
            )
        except Exception as exc:
            write_status(
                SERVICE_NAME,
                {
                    "ok": False,
                    "cycles": cycles,
                    "interval_seconds": INTERVAL_SECONDS,
                    "error": str(exc)[:300],
                },
            )
            print(f"[Binance position manager] cycle error: {exc}")
        wait_for = max(0.1, INTERVAL_SECONDS - (time.time() - started))
        _stop.wait(wait_for)
    write_status(SERVICE_NAME, {"ok": False, "stopped": True, "cycles": cycles})
    return 0


if __name__ == "__main__":
    sys.exit(run())
