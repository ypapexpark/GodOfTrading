"""
Trading venue router.

Default venue remains Bybit. Set `AUTO_TRADE_EXCHANGE=binance` to route live
execution/account/position calls to binance_trader. State and journals are
isolated by venue through venue_runtime.py, so separate LaunchAgents can run
Bybit and Binance side by side without sharing trade_state files.
"""
from __future__ import annotations

import trader as _bybit
from trader import *  # noqa: F401,F403 - re-export existing reporting/state helpers
from venue_runtime import runtime_context, runtime_venue

try:
    import binance_trader as _binance
except Exception:  # pragma: no cover - keeps Bybit path alive if optional adapter breaks
    _binance = None


def active_exchange() -> str:
    return runtime_venue()


def get_runtime_context() -> dict[str, str]:
    return runtime_context()


def _active_adapter():
    if active_exchange() == "binance" and _binance is not None:
        return _binance
    return _bybit


def get_usdt_balance() -> float:
    return _active_adapter().get_usdt_balance()


def get_usdt_equity() -> float:
    return _active_adapter().get_usdt_equity()


def has_open_position(symbol: str) -> bool:
    return _active_adapter().has_open_position(symbol)


def fetch_all_positions_raw() -> list[dict]:
    return _active_adapter().fetch_all_positions_raw()


def get_portfolio_risk_snapshot(retries: int = 2) -> dict:
    return _active_adapter().get_portfolio_risk_snapshot(retries)


def place_emergency_sl(symbol: str, direction: str, qty: float, sl_price: float) -> bool:
    return _active_adapter().place_emergency_sl(symbol, direction, qty, sl_price)


def execute(*args, **kwargs) -> dict:
    return _active_adapter().execute(*args, **kwargs)


def monitor_positions() -> None:
    return _active_adapter().monitor_positions()


def get_open_position_count() -> int:
    adapter = _active_adapter()
    if hasattr(adapter, "get_open_position_count"):
        return adapter.get_open_position_count()
    return len(adapter.fetch_all_positions_raw())


def is_execution_api_healthy() -> bool:
    """Active venue private API health. Bybit defaults True if adapter has no probe."""
    adapter = _active_adapter()
    fn = getattr(adapter, "is_execution_api_healthy", None)
    if callable(fn):
        return bool(fn())
    return True


def probe_execution_api() -> bool:
    adapter = _active_adapter()
    fn = getattr(adapter, "probe_execution_api", None)
    if callable(fn):
        return bool(fn())
    return True


def maybe_alert_execution_api_down() -> bool:
    """Send one-shot alert if venue API is down. Returns True if alert was sent now."""
    adapter = _active_adapter()
    fn = getattr(adapter, "maybe_alert_execution_api_down", None)
    if callable(fn):
        return bool(fn())
    return False


def get_execution_api_status() -> dict:
    adapter = _active_adapter()
    fn = getattr(adapter, "get_execution_api_status", None)
    if callable(fn):
        return dict(fn())
    return {"healthy": True, "venue": active_exchange()}


# Explicitly expose private state helpers imported directly by main.py.
_load_state = _bybit._load_state
_save_state = _bybit._save_state
_append_trade = _bybit._append_trade
_clear_position = _bybit._clear_position
_update_trade_result = _bybit._update_trade_result
