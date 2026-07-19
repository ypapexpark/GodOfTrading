#!/usr/bin/env python3
"""Binance C1 v2 breakout-retest order-flow challenger.

The rejected v1 entered on a single order-flow burst and produced negative
forward expectancy in both PAPER and LIVE.  V2 is long-only and requires a
completed 5m breakout, live retest, persistent 30-second taker/depth agreement,
BTC market alignment, and relative strength.  PAPER must graduate before the
existing protected micro-live path can be armed.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import signal
import statistics
import threading
import time
from typing import Any, Optional

os.environ.setdefault("AUTO_TRADE_EXCHANGE", "binance")
os.environ.setdefault("GOT_MARKET_DATA_EXCHANGE", "binance")
os.environ.setdefault("GOT_STATE_NAMESPACE", "binance")

import requests
import websocket
from dotenv import load_dotenv

from binance_divergence_engine import D2Plan
from bot_util import append_jsonl, load_json, now_kst, read_jsonl
from config import (
    BINANCE_C1_ACCOUNT_RISK_PCT,
    BINANCE_C1_AUTO_PROMOTE_ENABLED,
    BINANCE_C1_ENGINE_ENABLED,
    BINANCE_C1_LEVERAGE,
    BINANCE_C1_LIVE_ENABLED,
    BINANCE_C1_MAX_MARGIN_PCT,
    BINANCE_C1_MIN_MARGIN_USD,
    BINANCE_C1_TOP_N,
    BINANCE_C1_TRAIL_ACTIVATION_R,
    BINANCE_ROUND_TRIP_EXECUTION_COST,
)
from fetcher import fetch_ohlcv
from process_lock import release, try_acquire
from publisher import send_signal
from service_status import write_status


ROOT = Path(__file__).parent
STATE_FILE = ROOT / "binance_orderflow_challenger_state.json"
JOURNAL_FILE = ROOT / "binance_orderflow_challenger_journal.jsonl"
TRADE_STATE_FILE = ROOT / "trade_state_binance.json"
POLICY = "2026-07-19-c1v2-breakout-retest-persistent-flow"
STRATEGY = "C1V2_BREAKOUT_RETEST_PERSISTENT_FLOW"
LEGACY_STRATEGY = "C1_ORDERFLOW_TREND_CONTINUATION"
SERVICE_NAME = "binance_orderflow_challenger"
WS_BASE = "wss://fstream.binance.com/stream?streams="
BINANCE_FAPI = "https://fapi.binance.com"

FLOW_WINDOW_SECONDS = 20.0
CONTEXT_REFRESH_SECONDS = 45.0
UNIVERSE_REFRESH_SECONDS = 30 * 60.0
EVALUATION_SECONDS = 2.0
SIGNAL_COOLDOWN_SECONDS = 30 * 60.0
MAX_LIVE_ATTEMPTS_PER_HOUR = 4
PERSISTENCE_LOOKBACK_SECONDS = 30.0
PERSISTENCE_SAMPLE_SECONDS = 8.0
MIN_PERSISTENT_SAMPLES = 3
MIN_FLOW_IMBALANCE = 0.20
MIN_EACH_FLOW_IMBALANCE = 0.10
MIN_BOOK_IMBALANCE = 0.08
MAX_SPREAD_PCT = 0.06
MAX_RETEST_EXTENSION_ATR = 0.25
MIN_RETEST_EXTENSION_ATR = -0.08
MAX_ANCHOR_DISTANCE_ATR = 0.50
MIN_RELATIVE_STRENGTH_PCT = 0.15
BREAKOUT_LOOKBACK_BARS = 12
BREAKOUT_MAX_AGE_BARS = 2
GLOBAL_SIGNAL_GAP_SECONDS = 120.0
MAX_SIGNALS_PER_5M_BUCKET = 2
V2_MAX_HOLD_MINUTES = 45
V2_PROGRESS_CHECK_MINUTES = 15
V2_PROGRESS_MIN_R = 0.25

PAPER_INITIAL_BANKROLL = 1_000.0
PAPER_RISK_PCT = 0.0025
PAPER_NOTIONAL_CAP_PCT = 0.15
PAPER_MAX_OPEN = 3
PAPER_TP1_R = 1.2
PAPER_TP1_SIZE = 0.40
PAPER_TP2_R = 2.2
GRADUATION_MIN_CLOSED = 50
GRADUATION_MIN_PF = 1.20
REPORT_INTERVAL_SECONDS = 4 * 3600.0
REPORT_RETRY_SECONDS = 5 * 60.0

_stop = threading.Event()


def _safe(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _default_state() -> dict[str, Any]:
    now = time.time()
    return {
        "policy": POLICY,
        "started_ts": now,
        "started_at": now_kst(),
        "bankroll": PAPER_INITIAL_BANKROLL,
        "initial_bankroll": PAPER_INITIAL_BANKROLL,
        "positions": {},
        "last_signal_ts": {},
        "last_signal_key": {},
        "last_portfolio_signal_ts": 0.0,
        "signal_bucket_counts": {},
        "live_attempt_ts": [],
        "graduation_gate_status": "collecting",
        "graduation_gate_notified_at": "",
        "period": {
            "signals": 0,
            "paper_opened": 0,
            "paper_closed": 0,
            "live_attempts": 0,
            "live_opened": 0,
            "context_blocks": 0,
            "flow_blocks": 0,
            "market_blocks": 0,
            "persistence_blocks": 0,
            "retest_blocks": 0,
            "correlation_blocks": 0,
            "cooldown_blocks": 0,
        },
        "last_report_time": 0.0,
        "last_report_attempt_time": 0.0,
        "last_report_delivered": False,
    }


def _load_state() -> dict[str, Any]:
    default = _default_state()
    raw = load_json(STATE_FILE, {}) or {}
    if not isinstance(raw, dict) or raw.get("policy") != POLICY:
        if isinstance(raw, dict) and raw.get("policy"):
            _journal(
                "policy_retired",
                retired_policy=raw.get("policy"),
                retired_paper_positions=len(raw.get("positions") or {}),
                retired_bankroll=_safe(raw.get("bankroll")),
            )
        return default
    default.update(raw)
    for key in (
        "positions", "last_signal_ts", "last_signal_key",
        "signal_bucket_counts", "period",
    ):
        if not isinstance(default.get(key), dict):
            default[key] = _default_state()[key]
    if not isinstance(default.get("live_attempt_ts"), list):
        default["live_attempt_ts"] = []
    return default


def _save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _journal(event: str, **payload: Any) -> None:
    append_jsonl(
        JOURNAL_FILE,
        {"event": event, "policy": POLICY, "ts": time.time(), "at": now_kst(), **payload},
    )


def _bump(state: dict[str, Any], key: str, amount: int = 1) -> None:
    period = state.setdefault("period", {})
    period[key] = int(period.get(key) or 0) + amount


@dataclass(frozen=True)
class FlowSnapshot:
    symbol: str
    price: float
    bid: float
    ask: float
    spread_pct: float
    buy_quote: float
    sell_quote: float
    flow_imbalance: float
    book_imbalance: float
    trade_quote: float
    age_seconds: float


class OrderFlowBook:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trades: dict[str, deque[tuple[float, float, float]]] = defaultdict(deque)
        self._quotes: dict[str, dict[str, float]] = {}
        self._depth: dict[str, tuple[float, float, float]] = {}
        self.messages = 0
        self.last_message_ts = 0.0

    def on_payload(self, payload: dict[str, Any]) -> None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        event = str(data.get("e") or "")
        raw_symbol = str(data.get("s") or "").upper()
        if not raw_symbol:
            return
        now = time.time()
        with self._lock:
            if event in {"aggTrade", "trade"}:
                price = _safe(data.get("p"))
                qty = _safe(data.get("q"))
                quote = price * qty
                if quote > 0:
                    if bool(data.get("m")):
                        self._trades[raw_symbol].append((now, 0.0, quote))
                    else:
                        self._trades[raw_symbol].append((now, quote, 0.0))
            elif event == "bookTicker":
                self._quotes[raw_symbol] = {
                    "bid": _safe(data.get("b")),
                    "ask": _safe(data.get("a")),
                    "bid_qty": _safe(data.get("B")),
                    "ask_qty": _safe(data.get("A")),
                    "ts": now,
                }
            elif event == "depthUpdate":
                bids = data.get("b") or data.get("bids") or []
                asks = data.get("a") or data.get("asks") or []
                bid_qty = sum(_safe(row[1]) for row in bids[:5] if len(row) >= 2)
                ask_qty = sum(_safe(row[1]) for row in asks[:5] if len(row) >= 2)
                self._depth[raw_symbol] = (bid_qty, ask_qty, now)
            self.messages += 1
            self.last_message_ts = now

    def snapshot(self, raw_symbol: str, now: Optional[float] = None) -> Optional[FlowSnapshot]:
        now = float(now or time.time())
        raw_symbol = raw_symbol.upper()
        with self._lock:
            quote = dict(self._quotes.get(raw_symbol) or {})
            depth = self._depth.get(raw_symbol)
            trades = self._trades.get(raw_symbol)
            if trades is None:
                return None
            cutoff = now - FLOW_WINDOW_SECONDS
            while trades and trades[0][0] < cutoff:
                trades.popleft()
            buy_quote = sum(row[1] for row in trades)
            sell_quote = sum(row[2] for row in trades)
        bid = _safe(quote.get("bid"))
        ask = _safe(quote.get("ask"))
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / 2
        trade_total = buy_quote + sell_quote
        flow_imbalance = (buy_quote - sell_quote) / trade_total if trade_total > 0 else 0.0
        if depth:
            bid_qty, ask_qty, depth_ts = depth
        else:
            bid_qty = _safe(quote.get("bid_qty"))
            ask_qty = _safe(quote.get("ask_qty"))
            depth_ts = _safe(quote.get("ts"))
        depth_total = bid_qty + ask_qty
        book_imbalance = (bid_qty - ask_qty) / depth_total if depth_total > 0 else 0.0
        freshest = min(_safe(quote.get("ts")), depth_ts or _safe(quote.get("ts")))
        return FlowSnapshot(
            symbol=raw_symbol,
            price=mid,
            bid=bid,
            ask=ask,
            spread_pct=(ask - bid) / mid * 100,
            buy_quote=buy_quote,
            sell_quote=sell_quote,
            flow_imbalance=flow_imbalance,
            book_imbalance=book_imbalance,
            trade_quote=trade_total,
            age_seconds=max(now - freshest, 0.0),
        )


def _atr(frame, length: int = 14) -> float:
    prev = frame["close"].shift(1)
    tr = (frame["high"] - frame["low"]).to_frame("a")
    tr["b"] = (frame["high"] - prev).abs()
    tr["c"] = (frame["low"] - prev).abs()
    series = tr.max(axis=1).rolling(length, min_periods=length).mean()
    return _safe(series.iloc[-1]) if len(series) else 0.0


def _rolling_vwap(frame, length: int = 20) -> float:
    window = frame.tail(length)
    volume = window["volume"].sum()
    if volume <= 0:
        return 0.0
    typical = (window["high"] + window["low"] + window["close"]) / 3
    return _safe((typical * window["volume"]).sum() / volume)


def _market_context() -> Optional[dict[str, Any]]:
    """BTC regime used as a common market gate, independent of BTC breakout."""
    try:
        one_hour = fetch_ohlcv("BTC/USDT", "1h", 80, cache_only=True)
        five = fetch_ohlcv("BTC/USDT", "5m", 40, cache_only=True)
    except Exception:
        return None
    if len(one_hour) < 55 or len(five) < 8:
        return None
    ema20 = one_hour["close"].ewm(span=20, adjust=False).mean()
    ema50 = one_hour["close"].ewm(span=50, adjust=False).mean()
    close_1h = _safe(one_hour["close"].iloc[-1])
    close_5m = _safe(five["close"].iloc[-1])
    base_15m = _safe(five["close"].iloc[-4])
    return {
        "trend_long": bool(ema20.iloc[-1] > ema50.iloc[-1] and close_1h > ema20.iloc[-1]),
        "return_15m_pct": (close_5m / base_15m - 1) * 100 if base_15m > 0 else -99.0,
        "signal_bar": str(five.index[-1]),
    }


def _context(symbol: str) -> Optional[dict[str, Any]]:
    try:
        one_hour = fetch_ohlcv(symbol, "1h", 80, cache_only=True)
        five = fetch_ohlcv(symbol, "5m", 80, cache_only=True)
    except Exception:
        return None
    if len(one_hour) < 55 or len(five) < 35:
        return None
    ema20 = one_hour["close"].ewm(span=20, adjust=False).mean()
    ema50 = one_hour["close"].ewm(span=50, adjust=False).mean()
    ema9 = five["close"].ewm(span=9, adjust=False).mean()
    atr5 = _atr(five)
    if atr5 <= 0:
        return None
    last_1h = _safe(one_hour["close"].iloc[-1])
    last_5m = _safe(five["close"].iloc[-1])
    trend = ""
    if ema20.iloc[-1] > ema50.iloc[-1] and last_1h > ema20.iloc[-1]:
        trend = "LONG"

    breakout: dict[str, Any] = {}
    latest_index = len(five) - 1
    first_index = max(BREAKOUT_LOOKBACK_BARS, latest_index - BREAKOUT_MAX_AGE_BARS)
    for index in range(first_index, latest_index + 1):
        prior = five.iloc[index - BREAKOUT_LOOKBACK_BARS:index]
        if len(prior) < BREAKOUT_LOOKBACK_BARS:
            continue
        level = _safe(prior["high"].max())
        row = five.iloc[index]
        bar_range = _safe(row["high"] - row["low"])
        close_location = (
            (_safe(row["close"]) - _safe(row["low"])) / bar_range
            if bar_range > 0 else 0.0
        )
        prior_median_volume = _safe(prior["volume"].median())
        volume_ratio = _safe(row["volume"]) / max(prior_median_volume, 1e-12)
        if (
            _safe(row["close"]) >= level + atr5 * 0.03
            and close_location >= 0.65
            and volume_ratio >= 1.20
        ):
            breakout = {
                "breakout_valid": True,
                "breakout_level": level,
                "breakout_bar": str(five.index[index]),
                "breakout_age_bars": latest_index - index,
                "breakout_volume_ratio": volume_ratio,
                "breakout_close_location": close_location,
            }

    base_15m = _safe(five["close"].iloc[-4])
    return_15m_pct = (last_5m / base_15m - 1) * 100 if base_15m > 0 else -99.0
    signal_bar = str(five.index[-1])
    return {
        "direction": trend,
        "last_5m": last_5m,
        "ema9": _safe(ema9.iloc[-1]),
        "ema9_prev": _safe(ema9.iloc[-2]),
        "vwap20": _rolling_vwap(five, 20),
        "atr5": atr5,
        "atr_pct": atr5 / last_5m * 100 if last_5m > 0 else 99.0,
        "recent_high": _safe(five["high"].iloc[-6:].max()),
        "recent_low": _safe(five["low"].iloc[-6:].min()),
        "volume_ratio": (
            _safe(five["volume"].iloc[-1])
            / max(_safe(five["volume"].iloc[-21:-1].median()), 1e-12)
        ),
        "signal_bar": signal_bar,
        "return_15m_pct": return_15m_pct,
        **breakout,
    }


def summarize_persistence(
    samples: list[tuple[float, float, float, float]],
    now: float,
) -> dict[str, float]:
    recent = [row for row in samples if now - row[0] <= PERSISTENCE_LOOKBACK_SECONDS]
    flows = [row[1] for row in recent]
    books = [row[2] for row in recent]
    quotes = [row[3] for row in recent]
    return {
        "samples": float(len(recent)),
        "avg_flow": statistics.mean(flows) if flows else 0.0,
        "min_flow": min(flows) if flows else 0.0,
        "avg_book": statistics.mean(books) if books else 0.0,
        "positive_book_samples": float(sum(value >= 0.03 for value in books)),
        "min_trade_quote": min(quotes) if quotes else 0.0,
    }


def build_plan(
    symbol: str,
    flow: FlowSnapshot,
    context: dict[str, Any],
    *,
    min_flow_quote: float,
    persistence: Optional[dict[str, float]] = None,
    market: Optional[dict[str, Any]] = None,
) -> tuple[Optional[D2Plan], str]:
    direction = str(context.get("direction") or "")
    if direction != "LONG":
        return None, "1h_trend"
    market = market or {}
    if not market.get("trend_long") or _safe(market.get("return_15m_pct"), -99.0) < -0.10:
        return None, "btc_market"
    if not context.get("breakout_valid"):
        return None, "no_breakout"
    if flow.age_seconds > 3.0 or flow.spread_pct > MAX_SPREAD_PCT:
        return None, "stale_or_spread"
    if flow.trade_quote < min_flow_quote:
        return None, "flow_liquidity"
    if flow.flow_imbalance < MIN_EACH_FLOW_IMBALANCE or flow.book_imbalance < 0.03:
        return None, "current_flow"
    atr = _safe(context.get("atr5"))
    ema9 = _safe(context.get("ema9"))
    vwap20 = _safe(context.get("vwap20"))
    breakout_level = _safe(context.get("breakout_level"))
    if atr <= 0 or ema9 <= 0 or vwap20 <= 0 or breakout_level <= 0:
        return None, "5m_context"

    retest_extension = (flow.price - breakout_level) / atr
    anchor_distance = min(abs(flow.price - ema9), abs(flow.price - vwap20)) / atr
    if (
        retest_extension < MIN_RETEST_EXTENSION_ATR
        or retest_extension > MAX_RETEST_EXTENSION_ATR
        or anchor_distance > MAX_ANCHOR_DISTANCE_ATR
        or flow.bid < breakout_level - atr * 0.08
    ):
        return None, "retest"

    btc_return = _safe(market.get("return_15m_pct"))
    relative_strength = _safe(context.get("return_15m_pct")) - btc_return
    is_btc = symbol.split("/")[0].upper() == "BTC"
    if (not is_btc and relative_strength < MIN_RELATIVE_STRENGTH_PCT) or (
        is_btc and _safe(context.get("return_15m_pct")) < 0.10
    ):
        return None, "relative_strength"

    persistence = persistence or {}
    sample_count = int(persistence.get("samples") or 0)
    if (
        sample_count < MIN_PERSISTENT_SAMPLES
        or _safe(persistence.get("avg_flow")) < MIN_FLOW_IMBALANCE
        or _safe(persistence.get("min_flow")) < MIN_EACH_FLOW_IMBALANCE
        or _safe(persistence.get("avg_book")) < MIN_BOOK_IMBALANCE
        or int(persistence.get("positive_book_samples") or 0) < 2
        or _safe(persistence.get("min_trade_quote")) < min_flow_quote
    ):
        return None, "persistence"
    if _safe(context.get("atr_pct"), 99.0) > 1.85:
        return None, "volatility"

    entry = flow.ask
    side = 1.0
    minimum_risk = entry * max(BINANCE_ROUND_TRIP_EXECUTION_COST * 3.0, 0.0030)
    structural_risk = entry - (breakout_level - atr * 0.25)
    risk = max(atr * 0.55, minimum_risk, structural_risk)
    if risk / entry > 0.012:
        return None, "stop_too_wide"
    stop = entry - side * risk
    tp1 = entry + side * risk * PAPER_TP1_R
    tp2 = entry + side * risk * PAPER_TP2_R
    weighted_r = PAPER_TP1_R * PAPER_TP1_SIZE + PAPER_TP2_R * (1 - PAPER_TP1_SIZE)
    cash_cost = entry * BINANCE_ROUND_TRIP_EXECUTION_COST
    net_gain = max(weighted_r * risk - cash_cost, 0.0)
    net_loss = risk + cash_cost
    required_wr = net_loss / (net_loss + net_gain) if net_gain > 0 else 1.0
    signal_bucket = int(time.time() // 300)
    plan = D2Plan(
        eligible=True,
        reason="completed 5m breakout retest + persistent flow + BTC relative strength",
        direction=direction,
        divergence_kind="orderflow",
        indicator_votes=("1h_trend", "5m_breakout_retest", "persistent_taker_flow", "btc_relative_strength"),
        score=round(
            70 + _safe(persistence.get("avg_flow")) * 20
            + _safe(persistence.get("avg_book")) * 10
            + min(relative_strength, 1.0) * 5,
            2,
        ),
        entry=entry,
        stop=stop,
        tps=(
            {"price": tp1, "pct": int(PAPER_TP1_SIZE * 100), "rr": PAPER_TP1_R},
            {"price": tp2, "pct": int((1 - PAPER_TP1_SIZE) * 100), "rr": PAPER_TP2_R},
        ),
        atr=atr,
        atr_pct=round(atr / entry * 100, 5),
        stop_atr=round(risk / atr, 5),
        stop_pct=round(risk / entry * 100, 5),
        volume_ratio_5m=round(_safe(context.get("volume_ratio")), 5),
        required_win_rate=round(required_wr, 6),
        weighted_reward_r=round(weighted_r, 4),
        signal_bar=f"{context.get('breakout_bar')}:{signal_bucket}:LONG",
        context_votes={"1h": 1, "5m": 1, "btc": 1},
        metrics={
            "flow_imbalance": flow.flow_imbalance,
            "book_imbalance": flow.book_imbalance,
            "trade_quote_20s": flow.trade_quote,
            "spread_pct": flow.spread_pct,
            "persistent_samples": sample_count,
            "persistent_avg_flow": _safe(persistence.get("avg_flow")),
            "persistent_min_flow": _safe(persistence.get("min_flow")),
            "persistent_avg_book": _safe(persistence.get("avg_book")),
            "breakout_level": breakout_level,
            "breakout_age_bars": _safe(context.get("breakout_age_bars")),
            "breakout_volume_ratio": _safe(context.get("breakout_volume_ratio")),
            "retest_extension_atr": retest_extension,
            "anchor_distance_atr": anchor_distance,
            "relative_strength_15m_pct": relative_strength,
            "btc_return_15m_pct": btc_return,
            "max_hold_minutes": V2_MAX_HOLD_MINUTES,
            "progress_check_minutes": V2_PROGRESS_CHECK_MINUTES,
            "progress_min_r": V2_PROGRESS_MIN_R,
            "trail_activation_r": BINANCE_C1_TRAIL_ACTIVATION_R,
            "trail_atr_mult": 0.80,
        },
        signal_tier="C1V2",
        setup_timeframe="5m",
    )
    return plan, "ok"


def _paper_open(state: dict[str, Any], symbol: str, plan: D2Plan, now: float) -> bool:
    if any(pos.get("symbol") == symbol for pos in state["positions"].values()):
        return False
    if len(state["positions"]) >= PAPER_MAX_OPEN:
        return False
    bankroll = max(_safe(state.get("bankroll")), 0.0)
    risk_fraction = abs(plan.entry - plan.stop) / plan.entry
    loss_fraction = risk_fraction + BINANCE_ROUND_TRIP_EXECUTION_COST
    notional = min(
        bankroll * PAPER_NOTIONAL_CAP_PCT,
        bankroll * PAPER_RISK_PCT / max(loss_fraction, 1e-9),
    )
    if notional < 5.0:
        return False
    position_id = f"{symbol}-{int(now * 1000)}"
    state["positions"][position_id] = {
        "id": position_id,
        "symbol": symbol,
        "direction": plan.direction,
        "entry_ts": now,
        "entry_at": now_kst(),
        "entry": plan.entry,
        "stop": plan.stop,
        "tp1": _safe(plan.tps[0]["price"]),
        "tp2": _safe(plan.tps[1]["price"]),
        "risk": abs(plan.entry - plan.stop),
        "notional": notional,
        "remaining": 1.0,
        "gross_usd": 0.0,
        "fees_usd": notional * BINANCE_ROUND_TRIP_EXECUTION_COST / 2,
        "tp1_done": False,
        "max_r": 0.0,
        "signal_bar": plan.signal_bar,
        "plan": plan.to_dict(),
    }
    _bump(state, "paper_opened")
    _journal("paper_open", **state["positions"][position_id])
    return True


def _paper_close_fraction(position: dict[str, Any], fraction: float, price: float) -> None:
    fraction = min(max(fraction, 0.0), _safe(position.get("remaining")))
    if fraction <= 0:
        return
    side = 1.0 if position["direction"] == "LONG" else -1.0
    notional = _safe(position["notional"])
    entry = _safe(position["entry"])
    position["gross_usd"] += notional * fraction * side * (price / entry - 1)
    position["fees_usd"] += notional * fraction * BINANCE_ROUND_TRIP_EXECUTION_COST / 2
    position["remaining"] = max(_safe(position["remaining"]) - fraction, 0.0)


def _paper_settle(
    state: dict[str, Any], position_id: str, price: float, reason: str, now: float,
) -> dict[str, Any]:
    position = state["positions"][position_id]
    _paper_close_fraction(position, _safe(position["remaining"]), price)
    net = _safe(position["gross_usd"]) - _safe(position["fees_usd"])
    net_pct = net / max(_safe(position["notional"]), 1e-9) * 100
    state["bankroll"] = _safe(state.get("bankroll")) + net
    settled = {
        **position,
        "exit_ts": now,
        "exit_at": now_kst(),
        "exit_price": price,
        "exit_reason": reason,
        "net_usd": net,
        "net_pct": net_pct,
        "bankroll_after": state["bankroll"],
    }
    state["positions"].pop(position_id, None)
    _bump(state, "paper_closed")
    _journal("paper_close", **settled)
    return settled


def _manage_paper(
    state: dict[str, Any], feed: OrderFlowBook, raw_by_symbol: dict[str, str], now: float,
) -> list[dict[str, Any]]:
    settled: list[dict[str, Any]] = []
    for position_id in list(state["positions"]):
        position = state["positions"].get(position_id)
        if not position:
            continue
        flow = feed.snapshot(raw_by_symbol.get(position["symbol"], ""), now)
        if not flow:
            continue
        direction = position["direction"]
        exit_price = flow.bid if direction == "LONG" else flow.ask
        side = 1.0 if direction == "LONG" else -1.0
        risk = max(_safe(position["risk"]), 1e-12)
        favorable = side * (exit_price - _safe(position["entry"]))
        position["max_r"] = max(_safe(position.get("max_r")), favorable / risk)
        stop_hit = exit_price <= _safe(position["stop"]) if direction == "LONG" else exit_price >= _safe(position["stop"])
        if stop_hit:
            settled.append(_paper_settle(state, position_id, exit_price, "stop", now))
            continue
        tp1_hit = exit_price >= _safe(position["tp1"]) if direction == "LONG" else exit_price <= _safe(position["tp1"])
        if not position["tp1_done"] and tp1_hit:
            _paper_close_fraction(position, PAPER_TP1_SIZE, _safe(position["tp1"]))
            position["tp1_done"] = True
            cost_lock = _safe(position["entry"]) * BINANCE_ROUND_TRIP_EXECUTION_COST
            position["stop"] = (
                _safe(position["entry"]) + cost_lock
                if direction == "LONG" else _safe(position["entry"]) - cost_lock
            )
            _journal("paper_partial", symbol=position["symbol"], level="tp1", price=position["tp1"])
        tp2_hit = exit_price >= _safe(position["tp2"]) if direction == "LONG" else exit_price <= _safe(position["tp2"])
        if tp2_hit:
            settled.append(_paper_settle(state, position_id, _safe(position["tp2"]), "tp2", now))
            continue
        age_minutes = (now - _safe(position["entry_ts"])) / 60
        if age_minutes >= V2_MAX_HOLD_MINUTES:
            settled.append(_paper_settle(state, position_id, exit_price, "timeout", now))
        elif (
            age_minutes >= V2_PROGRESS_CHECK_MINUTES
            and not position["tp1_done"]
            and _safe(position["max_r"]) < V2_PROGRESS_MIN_R
        ):
            settled.append(_paper_settle(state, position_id, exit_price, "no_progress", now))
    return settled


def _strategy_performance(strategy: str) -> dict[str, float]:
    payload = load_json(TRADE_STATE_FILE, {}) or {}
    rows = [
        row for row in (payload.get("trade_history") or [])
        if row.get("strategy") == strategy and row.get("status") in {"win", "loss"}
    ]
    pnls = [_safe(row.get("pnl_usd")) for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    positions = payload.get("positions") or {}
    open_count = sum(1 for row in positions.values() if row.get("strategy") == strategy)
    return _perf(pnls, open_count=open_count)


def _perf(pnls: list[float], *, open_count: int = 0) -> dict[str, float]:
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    equity = peak = max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n": float(len(pnls)),
        "wins": float(len(wins)),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0.0,
        "net": sum(pnls),
        "avg": statistics.mean(pnls) if pnls else 0.0,
        "pf": sum(wins) / abs(sum(losses)) if losses else (99.0 if wins else 0.0),
        "max_dd": max_dd,
        "open": float(open_count),
    }


def _paper_performance(state: dict[str, Any]) -> dict[str, float]:
    rows = _paper_rows()
    return _perf([_safe(row.get("net_usd")) for row in rows], open_count=len(state["positions"]))


def _paper_rows() -> list[dict[str, Any]]:
    return [
        row for row in read_jsonl(JOURNAL_FILE)
        if row.get("event") == "paper_close" and row.get("policy") == POLICY
    ]


def _graduation(state: dict[str, Any]) -> tuple[bool, list[str]]:
    """Conservative forward-only gate for C1 v2 micro-live activation."""
    rows = _paper_rows()
    pnls = [_safe(row.get("net_usd")) for row in rows]
    perf = _perf(pnls, open_count=len(state.get("positions") or {}))
    reasons: list[str] = []
    if len(pnls) < GRADUATION_MIN_CLOSED:
        reasons.append(f"closed {len(pnls)}/{GRADUATION_MIN_CLOSED}")
    if perf["pf"] < GRADUATION_MIN_PF:
        reasons.append(f"PF {perf['pf']:.2f}/{GRADUATION_MIN_PF:.2f}")
    if perf["net"] <= 0:
        reasons.append(f"net ${perf['net']:+.2f}")
    if len(pnls) >= 25 and sum(pnls[-25:]) <= 0:
        reasons.append(f"last25 ${sum(pnls[-25:]):+.2f}")
    if pnls:
        without_best = sum(pnls) - max(pnls)
        if without_best <= 0:
            reasons.append(f"ex-best ${without_best:+.2f}")
    max_dd_limit = _safe(state.get("initial_bankroll"), PAPER_INITIAL_BANKROLL) * 0.08
    if perf["max_dd"] > max_dd_limit:
        reasons.append(f"DD ${perf['max_dd']:.2f}/${max_dd_limit:.2f}")
    return not reasons, reasons


def _live_permission(state: dict[str, Any], *, armed: bool) -> tuple[bool, str]:
    if not BINANCE_C1_ENGINE_ENABLED:
        return False, "engine_disabled"
    if not armed:
        return False, "runtime_not_armed"
    if BINANCE_C1_LIVE_ENABLED:
        return True, "manual_live"
    graduated, reasons = _graduation(state)
    if BINANCE_C1_AUTO_PROMOTE_ENABLED and graduated:
        return True, "paper_graduated"
    return False, ", ".join(reasons) if reasons else "live_disabled"


def _maybe_notify_graduation_transition(state: dict[str, Any]) -> bool:
    """Notify only when PAPER graduates; live permission remains automatic."""
    paper = _paper_performance(state)
    closed = int(paper["n"])
    graduated, reasons = _graduation(state)
    next_status = (
        "collecting"
        if closed < GRADUATION_MIN_CLOSED
        else ("graduated" if graduated else "locked")
    )
    previous = str(state.get("graduation_gate_status") or "collecting")
    if next_status == previous:
        return False
    if next_status != "graduated":
        state["graduation_gate_status"] = next_status
        return False
    title = "✅ <b>C1 v2 승격 완료</b>"
    action = (
        "사용자 확인 없이 다음 C1 v2 적합 신호부터 계좌위험 "
        f"{BINANCE_C1_ACCOUNT_RISK_PCT*100:.2f}% micro-LIVE 주문을 자동 시도합니다."
    )
    reason_text = "; ".join(reasons[:5]) if reasons else "모든 졸업 기준 통과"
    message = "\n".join([
        title,
        f"PAPER n={closed} · 승률 {paper['win_rate']:.1f}% · "
        f"PF {paper['pf']:.2f} · 순익 ${paper['net']:+.2f} · DD ${paper['max_dd']:.2f}",
        f"판정: {escape(reason_text)}",
        action,
        "※ 맥북과 C1 서비스가 실행 중이어야 자동 감시·주문이 계속됩니다.",
    ])
    if not send_signal(message):
        return False
    state["graduation_gate_status"] = next_status
    state["graduation_gate_notified_at"] = now_kst()
    _journal(
        "graduation_gate_transition",
        previous=previous,
        current=next_status,
        closed=closed,
        performance=paper,
        reasons=reasons,
    )
    return True


def _verdict(perf: dict[str, float]) -> str:
    n = int(perf["n"])
    if n < GRADUATION_MIN_CLOSED:
        return f"v2 표본 수집 중 {n}/{GRADUATION_MIN_CLOSED}"
    if perf["net"] <= 0 or perf["pf"] < GRADUATION_MIN_PF:
        return "⚠️ 비용후 음의 기대 관찰 — 확대 금지"
    return "✅ v2 PAPER 졸업 기준 후보"


def build_report(state: dict[str, Any]) -> str:
    paper = _paper_performance(state)
    live = _strategy_performance(STRATEGY)
    legacy = _strategy_performance(LEGACY_STRATEGY)
    d2 = _strategy_performance("D2_DIVERGENCE_VOLUME_ASYMMETRIC")
    d3 = _strategy_performance("D3_4H_MA200_VOLUME_PULLBACK_LONG")
    period = state.get("period") or {}
    graduated, graduation_reasons = _graduation(state)
    live_active, live_reason = _live_permission(state, armed=True)
    return "\n".join([
        "📊 <b>Binance 4시간 전략 검증 리포트</b>",
        f"C1 v2 실시간 신호 {int(period.get('signals') or 0)} · "
        f"PAPER 진입/청산 {int(period.get('paper_opened') or 0)}/{int(period.get('paper_closed') or 0)} · "
        f"LIVE 시도/체결 {int(period.get('live_attempts') or 0)}/{int(period.get('live_opened') or 0)}",
        f"C1 v2 PAPER n={int(paper['n'])} 승률 {paper['win_rate']:.1f}% · "
        f"PF {paper['pf']:.2f} · 누적 ${paper['net']:+.2f} · DD ${paper['max_dd']:.2f} · 보유 {int(paper['open'])}",
        f"C1 v2 LIVE n={int(live['n'])} 승률 {live['win_rate']:.1f}% · "
        f"PF {live['pf']:.2f} · 누적 ${live['net']:+.2f} · 보유 {int(live['open'])}",
        f"판정: <b>{escape(_verdict(paper))}</b>",
        f"LIVE 게이트 {'OPEN' if live_active else 'LOCKED'} · "
        f"{escape('졸업 완료' if graduated else '; '.join(graduation_reasons[:4]) or live_reason)}",
        f"폐기 C1 v1 LIVE n={int(legacy['n'])} PF {legacy['pf']:.2f} ${legacy['net']:+.2f}",
        f"비교 D2(신규 LIVE 중지) n={int(d2['n'])} PF {d2['pf']:.2f} ${d2['net']:+.2f} · "
        f"D3(LIVE 유지) n={int(d3['n'])} PF {d3['pf']:.2f} ${d3['net']:+.2f}",
        f"차단 집계 구조 {int(period.get('context_blocks') or 0)} · "
        f"시장 {int(period.get('market_blocks') or 0)} · "
        f"눌림 {int(period.get('retest_blocks') or 0)} · "
        f"지속수급 {int(period.get('persistence_blocks') or 0)} · "
        f"주문흐름 {int(period.get('flow_blocks') or 0)} · "
        f"상관/집중 {int(period.get('correlation_blocks') or 0)} · "
        f"재진입 {int(period.get('cooldown_blocks') or 0)}",
        f"가상시드 ${_safe(state.get('bankroll')):,.2f} · 수수료/슬리피 반영 · "
        f"졸업 후 micro-LIVE 계좌위험 {BINANCE_C1_ACCOUNT_RISK_PCT*100:.2f}%",
    ])


def _maybe_report(state: dict[str, Any], now: float, *, force: bool = False) -> bool:
    due = now - _safe(state.get("last_report_time")) >= REPORT_INTERVAL_SECONDS
    retry = now - _safe(state.get("last_report_attempt_time")) >= REPORT_RETRY_SECONDS
    if not force and (not due or not retry):
        return False
    state["last_report_attempt_time"] = now
    delivered = send_signal(build_report(state))
    state["last_report_delivered"] = delivered
    if delivered:
        state["last_report_time"] = now
        for key in list((state.get("period") or {}).keys()):
            state["period"][key] = 0
    return delivered


def _canonical_raw(row: dict[str, Any]) -> tuple[str, str]:
    canonical = str(row.get("symbol") or "")
    raw = canonical.split(":")[0].replace("/", "").upper()
    return canonical, raw


def _universe() -> tuple[list[str], dict[str, str], dict[str, float]]:
    # CCXT load_markets+fetch_tickers가 일부 시작에서 1분 이상 멈춰 WebSocket이
    # 뜨지 않는 현상이 있었다. 공식 USD-M 두 REST 응답을 명시적 timeout으로 받아
    # TRADING/USDT/PERPETUAL만 고른다. 30분에 한 번이므로 API 부담도 작다.
    info_response = requests.get(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo", timeout=12)
    info_response.raise_for_status()
    ticker_response = requests.get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", timeout=12)
    ticker_response.raise_for_status()
    active = {
        str(row.get("symbol") or "")
        for row in (info_response.json().get("symbols") or [])
        if row.get("status") == "TRADING"
        and row.get("contractType") == "PERPETUAL"
        and row.get("quoteAsset") == "USDT"
    }
    rows: list[dict[str, Any]] = []
    for ticker in ticker_response.json() or []:
        raw = str(ticker.get("symbol") or "").upper()
        if raw not in active or not raw.endswith("USDT"):
            continue
        last = _safe(ticker.get("lastPrice"))
        quote_volume = _safe(ticker.get("quoteVolume"))
        if last <= 0 or quote_volume <= 0:
            continue
        rows.append({
            "symbol": f"{raw[:-4]}/USDT",
            "raw": raw,
            "volume_usd": quote_volume,
        })
    rows.sort(key=lambda row: _safe(row.get("volume_usd")), reverse=True)
    rows = rows[: max(BINANCE_C1_TOP_N, 1)]
    raw_symbols: list[str] = []
    canonical_by_raw: dict[str, str] = {}
    qvol_by_raw: dict[str, float] = {}
    for row in rows:
        canonical = str(row.get("symbol") or "")
        raw = str(row.get("raw") or "").upper()
        if not canonical or not raw or not raw.endswith("USDT"):
            continue
        raw_symbols.append(raw)
        canonical_by_raw[raw] = canonical
        qvol_by_raw[raw] = _safe(row.get("volume_usd"))
    return raw_symbols, canonical_by_raw, qvol_by_raw


def _stream_url(raw_symbols: list[str]) -> str:
    streams: list[str] = []
    for raw in raw_symbols:
        name = raw.lower()
        # 2026-07-19 production fstream accepts aggTrade subscriptions but did
        # not emit them in a live probe.  The raw trade stream is active and
        # carries the same buyer-maker flag required for signed taker flow.
        streams.extend((f"{name}@trade", f"{name}@bookTicker", f"{name}@depth5@100ms"))
    return WS_BASE + "/".join(streams)


def _live_open_count(symbol: str = "") -> int:
    payload = load_json(TRADE_STATE_FILE, {}) or {}
    return sum(
        1 for key, row in (payload.get("positions") or {}).items()
        if row.get("strategy") == STRATEGY and (not symbol or key == symbol)
    )


def _attempt_live(
    state: dict[str, Any], symbol: str, plan: D2Plan, now: float, *, armed: bool,
) -> None:
    allowed, permission_reason = _live_permission(state, armed=armed)
    if not allowed:
        return
    recent = [ts for ts in state.get("live_attempt_ts", []) if now - _safe(ts) < 3600]
    state["live_attempt_ts"] = recent
    if len(recent) >= MAX_LIVE_ATTEMPTS_PER_HOUR:
        _journal("live_blocked", symbol=symbol, reason="hourly_attempt_cap")
        return
    before = _live_open_count(symbol)
    state["live_attempt_ts"].append(now)
    _bump(state, "live_attempts")
    try:
        import main as main_module

        main_module.AUTO_TRADE = True
        main_module._try_binance_d2_trade(
            symbol,
            plan,
            engine_code="C1V2",
            strategy=STRATEGY,
            engine_version=POLICY,
            engine_state_key="binance_c1_orderflow_engine",
            live_enabled=True,
            leverage_config=BINANCE_C1_LEVERAGE,
            max_margin_pct=BINANCE_C1_MAX_MARGIN_PCT,
            min_margin_usd=BINANCE_C1_MIN_MARGIN_USD,
            max_hold_minutes=V2_MAX_HOLD_MINUTES,
            progress_check_minutes=V2_PROGRESS_CHECK_MINUTES,
            progress_min_r=V2_PROGRESS_MIN_R,
            trail_activation_r=BINANCE_C1_TRAIL_ACTIVATION_R,
            is_divergence=False,
            strategy_family="C1 v2 Breakout Retest Persistent Flow",
            core_strategy="completed 5m breakout retest + persistent flow + BTC relative strength",
            strategy_mode="binance_c1v2_breakout_retest_live",
            exit_policy="c1v2_asymmetric_scalp",
            account_risk_pct_override=BINANCE_C1_ACCOUNT_RISK_PCT,
        )
    except Exception as exc:
        _journal("live_error", symbol=symbol, error=str(exc)[:300])
        print(f"[C1] live attempt failed {symbol}: {exc}")
        return
    after = _live_open_count(symbol)
    if after > before:
        _bump(state, "live_opened")
        _journal(
            "live_opened", symbol=symbol, signal_bar=plan.signal_bar,
            permission_reason=permission_reason,
        )


def _handle_signal(
    state: dict[str, Any], symbol: str, plan: D2Plan, now: float, *, live_armed: bool,
) -> None:
    key = plan.signal_bar
    if state["last_signal_key"].get(symbol) == key:
        _bump(state, "cooldown_blocks")
        return
    if now - _safe(state["last_signal_ts"].get(symbol)) < SIGNAL_COOLDOWN_SECONDS:
        _bump(state, "cooldown_blocks")
        return
    if now - _safe(state.get("last_portfolio_signal_ts")) < GLOBAL_SIGNAL_GAP_SECONDS:
        _bump(state, "correlation_blocks")
        return
    bucket = str(int(now // 300))
    bucket_counts = state.setdefault("signal_bucket_counts", {})
    for old_bucket in list(bucket_counts):
        if old_bucket != bucket:
            bucket_counts.pop(old_bucket, None)
    if int(bucket_counts.get(bucket) or 0) >= MAX_SIGNALS_PER_5M_BUCKET:
        _bump(state, "correlation_blocks")
        return
    if len(state["positions"]) >= PAPER_MAX_OPEN:
        _bump(state, "correlation_blocks")
        return
    if not _paper_open(state, symbol, plan, now):
        _bump(state, "cooldown_blocks")
        return
    state["last_signal_key"][symbol] = key
    state["last_signal_ts"][symbol] = now
    state["last_portfolio_signal_ts"] = now
    bucket_counts[bucket] = int(bucket_counts.get(bucket) or 0) + 1
    _bump(state, "signals")
    _journal("signal", symbol=symbol, plan=plan.to_dict())
    _save_state(state)
    _attempt_live(state, symbol, plan, now, armed=live_armed)


class Runner:
    def __init__(self, *, live: bool, report_now: bool) -> None:
        self.live = live
        self.report_now = report_now
        self.feed = OrderFlowBook()
        self.state = _load_state()
        self.raw_symbols: list[str] = []
        self.canonical_by_raw: dict[str, str] = {}
        self.qvol_by_raw: dict[str, float] = {}
        self.contexts: dict[str, dict[str, Any]] = {}
        self.market_context: dict[str, Any] = {}
        self.flow_samples: dict[str, deque[tuple[float, float, float, float]]] = defaultdict(deque)
        self.last_context_refresh = 0.0
        self.last_save = 0.0
        self.connected = False
        self.last_error = ""

    def refresh_universe(self) -> None:
        raw, canonical, qvol = _universe()
        if not raw:
            raise RuntimeError("empty Binance C1 universe")
        self.raw_symbols = raw
        self.canonical_by_raw = canonical
        self.qvol_by_raw = qvol

    def refresh_contexts(self, now: float) -> None:
        refreshed: dict[str, dict[str, Any]] = {}
        market = _market_context()
        self.market_context = market or {}
        for raw in self.raw_symbols:
            symbol = self.canonical_by_raw.get(raw, "")
            context = _context(symbol) if symbol else None
            if context:
                refreshed[raw] = context
        self.contexts = refreshed
        self.last_context_refresh = now

    def record_flow(self, raw: str, flow: FlowSnapshot, now: float) -> dict[str, float]:
        samples = self.flow_samples[raw]
        cutoff = now - PERSISTENCE_LOOKBACK_SECONDS
        while samples and samples[0][0] < cutoff:
            samples.popleft()
        if not samples or now - samples[-1][0] >= PERSISTENCE_SAMPLE_SECONDS:
            samples.append((now, flow.flow_imbalance, flow.book_imbalance, flow.trade_quote))
        return summarize_persistence(list(samples), now)

    def on_open(self, _ws) -> None:
        self.connected = True
        self.last_error = ""
        print(f"[C1] WebSocket connected: {len(self.raw_symbols)} symbols / {len(self.raw_symbols)*3} streams")

    def on_message(self, _ws, message: str) -> None:
        try:
            payload = json.loads(message)
            if isinstance(payload, dict):
                self.feed.on_payload(payload)
        except Exception as exc:
            self.last_error = str(exc)[:200]

    def on_error(self, _ws, error: Any) -> None:
        self.last_error = str(error)[:300]
        print(f"[C1] WebSocket error: {self.last_error}")

    def on_close(self, _ws, status: Any, message: Any) -> None:
        self.connected = False
        print(f"[C1] WebSocket closed: {status} {message}")

    def evaluator(self) -> None:
        if self.report_now:
            _maybe_report(self.state, time.time(), force=True)
            _save_state(self.state)
            self.report_now = False
        while not _stop.wait(EVALUATION_SECONDS):
            now = time.time()
            try:
                if now - self.last_context_refresh >= CONTEXT_REFRESH_SECONDS:
                    self.refresh_contexts(now)
                raw_by_symbol = {value: key for key, value in self.canonical_by_raw.items()}
                settled = _manage_paper(self.state, self.feed, raw_by_symbol, now)
                for trade in settled:
                    print(
                        f"[C1 PAPER] {trade['symbol']} {trade['exit_reason']} "
                        f"{trade['net_pct']:+.3f}% ${trade['net_usd']:+.3f}"
                    )
                _maybe_notify_graduation_transition(self.state)
                for raw in self.raw_symbols:
                    context = self.contexts.get(raw)
                    if not context:
                        _bump(self.state, "context_blocks")
                        continue
                    flow = self.feed.snapshot(raw, now)
                    if not flow:
                        _bump(self.state, "flow_blocks")
                        continue
                    persistence = self.record_flow(raw, flow, now)
                    qvol = self.qvol_by_raw.get(raw, 0.0)
                    average_20s = qvol / (24 * 60 * 60) * FLOW_WINDOW_SECONDS
                    min_flow = max(10_000.0, min(average_20s * 0.05, 250_000.0))
                    symbol = self.canonical_by_raw[raw]
                    plan, reason = build_plan(
                        symbol,
                        flow,
                        context,
                        min_flow_quote=min_flow,
                        persistence=persistence,
                        market=self.market_context,
                    )
                    if plan:
                        _handle_signal(
                            self.state, symbol, plan, now, live_armed=self.live,
                        )
                    elif reason in {
                        "1h_trend", "5m_context", "no_breakout", "volatility",
                        "stop_too_wide",
                    }:
                        _bump(self.state, "context_blocks")
                    elif reason in {"btc_market", "relative_strength"}:
                        _bump(self.state, "market_blocks")
                    elif reason == "retest":
                        _bump(self.state, "retest_blocks")
                    elif reason == "persistence":
                        _bump(self.state, "persistence_blocks")
                    else:
                        _bump(self.state, "flow_blocks")
                _maybe_report(self.state, now)
                if now - self.last_save >= 5.0:
                    _save_state(self.state)
                    self.last_save = now
                graduated, graduation_reasons = _graduation(self.state)
                live_active, live_reason = _live_permission(self.state, armed=self.live)
                write_status(
                    SERVICE_NAME,
                    {
                        "ok": self.connected and self.feed.last_message_ts > now - 10,
                        "connected": self.connected,
                        "policy": POLICY,
                        "strategy": STRATEGY,
                        "live_armed": self.live,
                        "live_enabled": live_active,
                        "live_gate_reason": live_reason,
                        "paper_graduated": graduated,
                        "graduation_gate_status": self.state.get("graduation_gate_status"),
                        "graduation_gate_notified_at": self.state.get("graduation_gate_notified_at"),
                        "graduation_reasons": graduation_reasons,
                        "paper_performance": _paper_performance(self.state),
                        "symbols": len(self.raw_symbols),
                        "contexts": len(self.contexts),
                        "btc_market": self.market_context,
                        "persistent_symbols": sum(
                            len(samples) >= MIN_PERSISTENT_SAMPLES
                            for samples in self.flow_samples.values()
                        ),
                        "messages": self.feed.messages,
                        "message_age_seconds": round(max(now - self.feed.last_message_ts, 0.0), 3) if self.feed.last_message_ts else None,
                        "paper_positions": len(self.state["positions"]),
                        "last_error": self.last_error,
                    },
                )
            except Exception as exc:
                self.last_error = str(exc)[:300]
                write_status(SERVICE_NAME, {"ok": False, "phase": "evaluator", "error": self.last_error})
                print(f"[C1] evaluator error: {exc}")

    def run(self) -> int:
        if not BINANCE_C1_ENGINE_ENABLED:
            print("[C1] disabled by config")
            return 0
        if not self.live:
            print("[C1 v2] PAPER-only runtime; live graduation is not armed")
        evaluator_thread = threading.Thread(target=self.evaluator, name="c1-evaluator", daemon=True)
        evaluator_thread.start()
        while not _stop.is_set():
            try:
                self.refresh_universe()
                self.refresh_contexts(time.time())
                app = websocket.WebSocketApp(
                    _stream_url(self.raw_symbols),
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                )
                timer = threading.Timer(UNIVERSE_REFRESH_SECONDS, app.close)
                timer.daemon = True
                timer.start()
                app.run_forever(ping_interval=20, ping_timeout=10)
                timer.cancel()
            except Exception as exc:
                self.last_error = str(exc)[:300]
                print(f"[C1] stream cycle failed: {exc}")
            if not _stop.wait(3.0):
                continue
        _save_state(self.state)
        write_status(SERVICE_NAME, {"ok": False, "stopped": True})
        return 0


def _request_stop(*_args) -> None:
    _stop.set()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--live", action="store_true", help="enable protected micro-live mirror")
    parser.add_argument("--report-now", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if not try_acquire(SERVICE_NAME):
        return 0
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    try:
        return Runner(live=args.live, report_now=args.report_now).run()
    finally:
        release(SERVICE_NAME)


if __name__ == "__main__":
    raise SystemExit(main())
