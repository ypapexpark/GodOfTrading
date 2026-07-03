"""Runtime venue helpers for isolated single-venue bot processes.

The scanner is intentionally run as one process per venue.  Environment
variables decide which exchange adapter, market-data feed, and local state
files that process owns.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
_ALIASES = {
    "": "bybit",
    "bybit": "bybit",
    "bybitusdt": "bybit",
    "binance": "binance",
    "binanceusdm": "binance",
    "binance-usdm": "binance",
    "binance_usdm": "binance",
}
_LABELS = {
    "bybit": "Bybit",
    "binance": "Binance",
}


def normalize_venue(raw: str | None) -> str:
    key = str(raw or "").strip().lower().replace(" ", "")
    return _ALIASES.get(key, "bybit")


def runtime_venue() -> str:
    return normalize_venue(os.getenv("AUTO_TRADE_EXCHANGE", "bybit"))


def market_data_venue() -> str:
    raw = os.getenv("GOT_MARKET_DATA_EXCHANGE") or os.getenv("AUTO_TRADE_EXCHANGE")
    return normalize_venue(raw)


def state_namespace() -> str:
    raw = os.getenv("GOT_STATE_NAMESPACE") or os.getenv("AUTO_TRADE_EXCHANGE")
    return normalize_venue(raw)


def venue_label(venue: str | None = None) -> str:
    return _LABELS.get(normalize_venue(venue or runtime_venue()), "Bybit")


def namespaced_data_path(default_name: str, namespace: str | None = None) -> Path:
    """Return a repo-local path, keeping Bybit's legacy filenames unchanged."""
    ns = normalize_venue(namespace or state_namespace())
    path = Path(default_name)
    if ns == "bybit":
        return BASE_DIR / path.name
    return BASE_DIR / f"{path.stem}_{ns}{path.suffix}"


def runtime_context() -> dict[str, str]:
    return {
        "execution_venue": runtime_venue(),
        "market_data_venue": market_data_venue(),
        "state_namespace": state_namespace(),
    }
