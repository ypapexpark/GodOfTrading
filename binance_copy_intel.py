#!/usr/bin/env python3
"""Binance Bot Marketplace + Copy Trading 리드 분석/섀도 카피.

공개 Binance 웹 데이터와 공개 시세만 사용한다. 주문 endpoint는 존재하지 않으며
LIVE 승격 코드도 의도적으로 두지 않는다.

주요 흐름:
1) 6시간마다 공개 리드/그리드 전략을 고정 코호트로 스냅샷한다.
2) 30D/90D 성과, 포지션 이력, MDD, PF, 최대 1승 의존도, 마틴게일 징후를 평가한다.
3) 통과 리드의 공개 체결을 60초마다 읽어 현재 시드 기준으로 가상 체결한다.
4) 수수료/감지 지연 슬리피지/최소 주문금액을 반영해 실제 카피 가능성을 기록한다.

이 모듈은 공개 웹용 bapi 경로를 사용한다. 이는 공식 문서화 API가 아니므로 응답
스키마 변경을 정상 장애로 취급하고, 실패 시 이전 스냅샷을 보존한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import time
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Optional

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import requests
from binance_api_guard import (
    api_backoff_remaining,
    record_api_error,
    reserve_api_weight,
)
from dotenv import load_dotenv

from bot_util import append_jsonl, env_float, env_int, load_json, now_kst, save_json
from process_lock import release, try_acquire
from publisher import send_signal


ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

STATE_FILE = ROOT / "binance_copy_intel_state.json"
SNAPSHOT_FILE = ROOT / "binance_copy_intel_snapshots.jsonl"
JOURNAL_FILE = ROOT / "binance_copy_intel_journal.jsonl"
POLICY = "binance_copy_intel_shadow_v1"
LOCK_NAME = "binance_copy_intel"

WEB_BASE = "https://www.binance.com"
COPY_LIST_PATH = "/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list"
COPY_DETAIL_PATH = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail"
COPY_POSITION_HISTORY_PATH = (
    "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position-history"
)
COPY_TRADE_HISTORY_PATH = (
    "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/trade-history"
)
GRID_MARKET_PATH = (
    "/bapi/capital/v1/public/future/common/strategy/landing-page/queryTopStrategy"
)
FUTURES_API = "https://fapi.binance.com"
SPOT_API = "https://api.binance.com"

DISCOVERY_INTERVAL_SECONDS = env_int("BINANCE_COPY_DISCOVERY_SECONDS", 6 * 3600)
REPORT_INTERVAL_SECONDS = env_int("BINANCE_COPY_REPORT_SECONDS", 4 * 3600)
REPORT_RETRY_SECONDS = env_int("BINANCE_COPY_REPORT_RETRY_SECONDS", 5 * 60)
LEADERBOARD_ROWS = env_int("BINANCE_COPY_LEADERBOARD_ROWS", 30)
PRELIMINARY_LIMIT = env_int("BINANCE_COPY_PRELIMINARY_LIMIT", 18)
TRACKED_LEADERS = env_int("BINANCE_COPY_TRACKED_LEADERS", 3)
TRACKED_GRID_BOTS = env_int("BINANCE_COPY_TRACKED_GRID_BOTS", 5)
HISTORY_ROWS = env_int("BINANCE_COPY_HISTORY_ROWS", 100)
TRADE_POLL_ROWS = env_int("BINANCE_COPY_TRADE_POLL_ROWS", 50)

MIN_RUNTIME_DAYS = env_float("BINANCE_COPY_MIN_RUNTIME_DAYS", 90.0)
MAX_MDD_PCT = env_float("BINANCE_COPY_MAX_MDD_PCT", 20.0)
MIN_SHARPE = env_float("BINANCE_COPY_MIN_SHARPE", 1.0)
MIN_CLOSED_POSITIONS = env_int("BINANCE_COPY_MIN_CLOSED_POSITIONS", 60)
MIN_PROFIT_FACTOR = env_float("BINANCE_COPY_MIN_PROFIT_FACTOR", 1.15)
MIN_WINNING_DAY_RATIO = env_float("BINANCE_COPY_MIN_WINNING_DAY_RATIO", 0.55)
MAX_LEVERAGE = env_float("BINANCE_COPY_MAX_LEVERAGE", 5.0)

# 0이면 Binance USD-M 현재 equity를 읽기 전용 조회하고, 실패하면 fallback을 쓴다.
SHADOW_SEED_OVERRIDE = env_float("BINANCE_COPY_SHADOW_SEED_USDT", 0.0)
SHADOW_SEED_FALLBACK = env_float("BINANCE_COPY_SHADOW_SEED_FALLBACK_USDT", 200.0)
PER_LEADER_ALLOCATION_PCT = env_float("BINANCE_COPY_PER_LEADER_PCT", 0.20)
TOTAL_ALLOCATION_CAP_PCT = env_float("BINANCE_COPY_TOTAL_CAP_PCT", 0.60)
SHADOW_LEVERAGE_CAP = env_float("BINANCE_COPY_SHADOW_LEVERAGE_CAP", 5.0)
MIN_COPY_NOTIONAL = env_float("BINANCE_COPY_MIN_NOTIONAL_USDT", 5.0)
TAKER_FEE_RATE = env_float("BINANCE_COPY_TAKER_FEE_RATE", 0.0005)
ONE_WAY_SLIPPAGE = env_float("BINANCE_COPY_ONE_WAY_SLIPPAGE", 0.0003)
SPOT_GRID_ROUND_TRIP_FEE = env_float("BINANCE_GRID_SPOT_ROUND_TRIP_FEE", 0.0020)
FUTURES_GRID_ROUND_TRIP_FEE = env_float("BINANCE_GRID_FUTURES_ROUND_TRIP_FEE", 0.0004)
GRID_PER_BOT_ALLOCATION_PCT = env_float("BINANCE_GRID_PER_BOT_PCT", 0.25)
GRID_MIN_24H_QUOTE_USD = env_float("BINANCE_GRID_MIN_24H_QUOTE_USD", 10_000_000.0)

HTTP_HEADERS = {
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0 (GodOfTrading read-only research)",
    "clienttype": "web",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _api_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
    timeout: float = 15.0,
) -> Any:
    is_futures_api = url.startswith(FUTURES_API)
    if is_futures_api and api_backoff_remaining() > 0:
        raise RuntimeError("Binance shared API backoff active")
    futures_weight = 1
    if is_futures_api and url.endswith("/fapi/v1/ticker/24hr") and not (params or {}).get("symbol"):
        futures_weight = 40
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            if is_futures_api:
                reserve_api_weight(futures_weight)
            response = requests.request(
                method,
                url,
                params=params,
                json=payload,
                headers=HTTP_HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get("success") is False:
                raise RuntimeError(data.get("message") or data.get("code") or "API failure")
            return data
        except Exception as exc:
            if is_futures_api:
                record_api_error(exc)
            last_error = exc
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(f"read-only Binance API failed: {url}: {last_error}")


def _unwrap_data(response: Any) -> Any:
    if isinstance(response, dict) and "data" in response:
        return response.get("data")
    return response


def fetch_leaderboard(
    time_range: str,
    data_type: str,
    *,
    rows: int = LEADERBOARD_ROWS,
    nickname: str = "",
) -> list[dict[str, Any]]:
    response = _api_json(
        "POST",
        WEB_BASE + COPY_LIST_PATH,
        payload={
            "pageNumber": 1,
            "pageSize": max(1, min(int(rows), 100)),
            "timeRange": time_range,
            "dataType": data_type,
            "favoriteOnly": False,
            "hideFull": False,
            "nickname": nickname,
            "order": "DESC",
            "userAsset": 0,
        },
    )
    data = _unwrap_data(response) or {}
    rows_out = data.get("list") if isinstance(data, dict) else []
    return [row for row in (rows_out or []) if isinstance(row, dict)]


def fetch_leader_detail(portfolio_id: str) -> dict[str, Any]:
    response = _api_json(
        "GET",
        WEB_BASE + COPY_DETAIL_PATH,
        params={"portfolioId": str(portfolio_id)},
    )
    data = _unwrap_data(response)
    return data if isinstance(data, dict) else {}


def fetch_position_history(portfolio_id: str, rows: int = HISTORY_ROWS) -> list[dict[str, Any]]:
    response = _api_json(
        "POST",
        WEB_BASE + COPY_POSITION_HISTORY_PATH,
        payload={
            "portfolioId": str(portfolio_id),
            "pageNumber": 1,
            "pageSize": max(1, min(int(rows), 100)),
        },
    )
    data = _unwrap_data(response) or {}
    result = data.get("list") if isinstance(data, dict) else []
    return [row for row in (result or []) if isinstance(row, dict)]


def fetch_trade_history(portfolio_id: str, rows: int = TRADE_POLL_ROWS) -> list[dict[str, Any]]:
    response = _api_json(
        "POST",
        WEB_BASE + COPY_TRADE_HISTORY_PATH,
        payload={
            "portfolioId": str(portfolio_id),
            "pageNumber": 1,
            "pageSize": max(1, min(int(rows), 100)),
        },
    )
    data = _unwrap_data(response) or {}
    result = data.get("list") if isinstance(data, dict) else []
    return [row for row in (result or []) if isinstance(row, dict)]


def fetch_grid_marketplace(strategy_type: int, sort: str, rows: int = 30) -> list[dict[str, Any]]:
    response = _api_json(
        "POST",
        WEB_BASE + GRID_MARKET_PATH,
        payload={
            "strategyType": int(strategy_type),
            "sort": sort,
            "page": 1,
            "rows": max(1, min(int(rows), 100)),
        },
    )
    data = _unwrap_data(response)
    return [row for row in (data or []) if isinstance(row, dict)]


def _resolve_shadow_seed() -> tuple[float, str]:
    if SHADOW_SEED_OVERRIDE > 0:
        return SHADOW_SEED_OVERRIDE, "env_override"
    try:
        # 잔고 조회뿐이며 주문 함수는 호출하지 않는다.
        from binance_trader import get_usdt_equity

        equity = _safe_float(get_usdt_equity())
        if equity > 0:
            return equity, "binance_usdm_equity"
    except Exception as exc:
        print(f"[copy-intel] equity fallback: {exc}")
    return SHADOW_SEED_FALLBACK, "fallback"


def _default_state(seed: Optional[float] = None, seed_source: str = "fallback") -> dict[str, Any]:
    now = time.time()
    starting_seed = _safe_float(seed, SHADOW_SEED_FALLBACK)
    return {
        "policy": POLICY,
        "created_ts": now,
        "created_at": now_kst(),
        "seed_usdt": starting_seed,
        "seed_source": seed_source,
        "cash_usdt": starting_seed,
        "last_discovery_ts": 0.0,
        "last_report_ts": 0.0,
        "last_report_attempt_ts": 0.0,
        "last_report_delivered": False,
        "leaders": {},
        "grid_bots": [],
        "shadow": {
            "baselined": {},
            "seen_fills": {},
            "leader_qty": {},
            "positions": {},
            "closed": [],
            "skipped": [],
        },
        "last_cycle": {},
    }


def _load_state() -> dict[str, Any]:
    raw = load_json(STATE_FILE, {}) or {}
    if not isinstance(raw, dict) or raw.get("policy") != POLICY:
        seed, source = _resolve_shadow_seed()
        default = _default_state(seed, source)
        return default
    # 60초 체결 폴링마다 private balance를 호출하지 않는다. 현재 시드 동기화는
    # 6시간 discovery 때만 수행해, 일시적 API 장애/미실현 PnL로 paper 원금이
    # 매분 출렁이는 일을 막는다.
    old_seed = _safe_float(raw.get("seed_usdt"), SHADOW_SEED_FALLBACK)
    default = _default_state(old_seed, str(raw.get("seed_source") or "saved"))
    default.update(raw)
    for key, fallback in (("leaders", {}), ("grid_bots", []), ("shadow", {})):
        if not isinstance(default.get(key), type(fallback)):
            default[key] = fallback
    shadow = default["shadow"]
    for key in ("baselined", "seen_fills", "leader_qty", "positions"):
        if not isinstance(shadow.get(key), dict):
            shadow[key] = {}
    for key in ("closed", "skipped"):
        if not isinstance(shadow.get(key), list):
            shadow[key] = []
    return default


def _refresh_shadow_seed(state: dict[str, Any]) -> None:
    seed, source = _resolve_shadow_seed()
    # 기존 실제 equity를 알고 있는데 이번 호출만 실패해 fallback으로 떨어졌다면
    # paper 원금을 임의로 바꾸지 않는다.
    if source == "fallback" and state.get("seed_source") == "binance_usdm_equity":
        return
    old_seed = _safe_float(state.get("seed_usdt"), seed)
    if abs(seed - old_seed) <= 0.01:
        state["seed_source"] = source
        return
    state["cash_usdt"] = _safe_float(state.get("cash_usdt"), old_seed) + seed - old_seed
    state["seed_usdt"] = seed
    state["seed_source"] = source
    _journal("shadow_seed_rebased", old_seed=old_seed, new_seed=seed, source=source)


def _journal(event: str, **payload: Any) -> None:
    append_jsonl(
        JOURNAL_FILE,
        {"event": event, "ts": time.time(), "at": now_kst(), **payload},
    )


def _chart_stats(row: dict[str, Any]) -> dict[str, float]:
    values = [
        _safe_float(item.get("value"))
        for item in (row.get("chartItems") or [])
        if isinstance(item, dict)
    ]
    if len(values) < 2:
        return {"winning_days": 0.0, "observed_days": 0.0, "winning_day_ratio": 0.0}
    # 선두의 0 채움 구간은 실제 운용 전이므로 제외한다.
    start = 0
    while start + 1 < len(values) and values[start] == 0 and values[start + 1] == 0:
        start += 1
    active = values[start:]
    changes = [b - a for a, b in zip(active, active[1:])]
    observed = len(changes)
    wins = sum(1 for value in changes if value > 0)
    return {
        "winning_days": float(wins),
        "observed_days": float(observed),
        "winning_day_ratio": wins / observed if observed else 0.0,
    }


def _normalize_leader_row(row: dict[str, Any], time_range: str) -> dict[str, Any]:
    start_ms = _safe_float(row.get("startTime"))
    runtime_days = max(0.0, (time.time() - start_ms / 1000) / 86400) if start_ms else 0.0
    return {
        "portfolio_id": str(row.get("leadPortfolioId") or ""),
        "nickname": str(row.get("nickname") or ""),
        "time_range": time_range,
        "roi_pct": _safe_float(row.get("roi")),
        "pnl_usdt": _safe_float(row.get("pnl")),
        "mdd_pct": _safe_float(row.get("mdd")),
        "win_rate_pct": _safe_float(row.get("winRate")),
        "copier_pnl_usdt": _safe_float(row.get("copierPnl")),
        "sharpe": _safe_float(row.get("sharpRatio")),
        "aum_usdt": _safe_float(row.get("aum")),
        "copy_count": int(_safe_float(row.get("currentCopyCount"))),
        "max_copy_count": int(_safe_float(row.get("maxCopyCount"))),
        "runtime_days": runtime_days,
        "api_trading": bool(row.get("apiKeyTag")),
        **_chart_stats(row),
    }


def analyze_position_history(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [
        row for row in rows
        if str(row.get("status") or "").lower() == "all closed"
        and row.get("closingPnl") is not None
    ]
    pnls = [_safe_float(row.get("closingPnl")) for row in closed]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss else (99.0 if gross_win else 0.0)
    total = sum(pnls)
    largest_win = max(wins) if wins else 0.0
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = abs(statistics.mean(losses)) if losses else 0.0
    loss_win_size_ratio = avg_loss / avg_win if avg_win > 0 else 99.0 if avg_loss else 0.0

    leverages = [_safe_float(row.get("leverage"), 1.0) for row in closed]
    high_lev_ratio = (
        sum(1 for leverage in leverages if leverage > MAX_LEVERAGE) / len(leverages)
        if leverages else 0.0
    )
    durations = []
    symbol_pnl: dict[str, float] = defaultdict(float)
    chronological = sorted(closed, key=lambda row: _safe_float(row.get("opened")))
    loss_size_increases = 0
    loss_followups = 0
    previous: Optional[dict[str, Any]] = None
    for row in chronological:
        opened = _safe_float(row.get("opened"))
        closed_at = _safe_float(row.get("closed"))
        if opened and closed_at and closed_at >= opened:
            durations.append((closed_at - opened) / 3_600_000)
        pnl = _safe_float(row.get("closingPnl"))
        symbol_pnl[str(row.get("symbol") or "?")] += pnl
        notional = abs(_safe_float(row.get("maxOpenInterest")) * _safe_float(row.get("avgCost")))
        if previous and _safe_float(previous.get("pnl")) < 0:
            loss_followups += 1
            if notional > _safe_float(previous.get("notional")) * 1.5:
                loss_size_increases += 1
        previous = {"pnl": pnl, "notional": notional}

    martingale_reasons = []
    if len(closed) >= 20 and len(wins) / len(closed) >= 0.80 and loss_win_size_ratio >= 3.0:
        martingale_reasons.append("high_winrate_large_losses")
    increase_ratio = loss_size_increases / loss_followups if loss_followups else 0.0
    if loss_followups >= 5 and increase_ratio >= 0.40:
        martingale_reasons.append("size_increase_after_loss")
    if high_lev_ratio >= 0.50:
        martingale_reasons.append("persistent_high_leverage")

    positive_symbol_pnl = [value for value in symbol_pnl.values() if value > 0]
    concentration = (
        max(positive_symbol_pnl) / sum(positive_symbol_pnl)
        if positive_symbol_pnl and sum(positive_symbol_pnl) > 0 else 0.0
    )
    return {
        "closed_positions": len(closed),
        "win_rate_pct": len(wins) / len(closed) * 100 if closed else 0.0,
        "net_pnl_usdt": total,
        "profit_factor": pf,
        "largest_win_usdt": largest_win,
        "without_largest_win_usdt": total - largest_win,
        "avg_win_usdt": avg_win,
        "avg_loss_usdt": avg_loss,
        "loss_win_size_ratio": loss_win_size_ratio,
        "max_leverage": max(leverages) if leverages else 0.0,
        "high_leverage_ratio": high_lev_ratio,
        "loss_size_increase_ratio": increase_ratio,
        "symbol_profit_concentration": concentration,
        "median_hold_hours": statistics.median(durations) if durations else 0.0,
        "martingale_suspected": bool(martingale_reasons),
        "martingale_reasons": martingale_reasons,
    }


def score_leader(
    metrics_30d: dict[str, Any],
    metrics_90d: dict[str, Any],
    history: dict[str, Any],
) -> tuple[float, str, list[str]]:
    reasons: list[str] = []
    runtime = _safe_float(metrics_90d.get("runtime_days"))
    if runtime < MIN_RUNTIME_DAYS:
        reasons.append(f"runtime<{MIN_RUNTIME_DAYS:g}d")
    for label, metrics in (("30d", metrics_30d), ("90d", metrics_90d)):
        if _safe_float(metrics.get("pnl_usdt")) <= 0 or _safe_float(metrics.get("roi_pct")) <= 0:
            reasons.append(f"{label}_negative")
        if _safe_float(metrics.get("mdd_pct")) > MAX_MDD_PCT:
            reasons.append(f"{label}_mdd>{MAX_MDD_PCT:g}%")
        if _safe_float(metrics.get("copier_pnl_usdt")) <= 0:
            reasons.append(f"{label}_copier_pnl<=0")
    if _safe_float(metrics_90d.get("sharpe")) < MIN_SHARPE:
        reasons.append(f"sharpe<{MIN_SHARPE:g}")
    if _safe_float(metrics_30d.get("winning_day_ratio")) < MIN_WINNING_DAY_RATIO:
        reasons.append("unstable_winning_days")
    if int(history.get("closed_positions") or 0) < MIN_CLOSED_POSITIONS:
        reasons.append(f"closed<{MIN_CLOSED_POSITIONS}")
    if _safe_float(history.get("profit_factor")) < MIN_PROFIT_FACTOR:
        reasons.append(f"pf<{MIN_PROFIT_FACTOR:g}")
    if _safe_float(history.get("without_largest_win_usdt")) <= 0:
        reasons.append("largest_win_dependency")
    if history.get("martingale_suspected"):
        reasons.append("martingale_suspected")

    score = 0.0
    score += min(18.0, runtime / 180 * 18)
    score += 8.0 if _safe_float(metrics_30d.get("pnl_usdt")) > 0 else 0.0
    score += 8.0 if _safe_float(metrics_90d.get("pnl_usdt")) > 0 else 0.0
    worst_mdd = max(_safe_float(metrics_30d.get("mdd_pct")), _safe_float(metrics_90d.get("mdd_pct")))
    score += max(0.0, 16.0 * (1 - worst_mdd / max(MAX_MDD_PCT, 1)))
    score += min(12.0, max(0.0, _safe_float(metrics_90d.get("sharpe"))) / 4 * 12)
    score += min(10.0, _safe_float(metrics_30d.get("winning_day_ratio")) * 12)
    score += min(12.0, _safe_float(history.get("profit_factor")) / 2 * 12)
    score += min(6.0, int(history.get("closed_positions") or 0) / max(MIN_CLOSED_POSITIONS, 1) * 6)
    score += 5.0 if _safe_float(history.get("without_largest_win_usdt")) > 0 else 0.0
    score += 5.0 if _safe_float(metrics_90d.get("copier_pnl_usdt")) > 0 else 0.0
    score -= 20.0 if history.get("martingale_suspected") else 0.0
    score -= 8.0 if _safe_float(history.get("high_leverage_ratio")) > 0.50 else 0.0
    score -= 5.0 if _safe_float(history.get("symbol_profit_concentration")) > 0.70 else 0.0
    score = max(0.0, min(100.0, score))

    hard_fail_prefixes = (
        "30d_negative", "90d_negative", "30d_mdd", "90d_mdd",
        "30d_copier", "90d_copier", "martingale_suspected",
    )
    if any(reason.startswith(hard_fail_prefixes) for reason in reasons):
        stage = "reject"
    elif reasons:
        stage = "watch"
    else:
        stage = "shadow"
    return round(score, 2), stage, reasons


def _coarse_leader_score(metrics: dict[str, Any]) -> float:
    if metrics["runtime_days"] < 45 or metrics["pnl_usdt"] <= 0:
        return -999.0
    score = 0.0
    score += min(25.0, metrics["runtime_days"] / 180 * 25)
    score += max(0.0, 25 - metrics["mdd_pct"])
    score += min(20.0, max(0.0, metrics["sharpe"]) * 5)
    score += 15.0 if metrics["copier_pnl_usdt"] > 0 else -30.0
    score += min(15.0, metrics["winning_day_ratio"] * 20)
    return score


def _discover_leaders() -> dict[str, dict[str, Any]]:
    cohort: dict[str, dict[str, Any]] = {}
    for data_type in ("SHARP_RATIO", "COPIER_PNL", "PNL"):
        for rank, row in enumerate(fetch_leaderboard("90D", data_type), start=1):
            metrics = _normalize_leader_row(row, "90D")
            portfolio_id = metrics["portfolio_id"]
            if not portfolio_id:
                continue
            entry = cohort.setdefault(portfolio_id, {"metrics_90d": metrics, "source_ranks": {}})
            entry["metrics_90d"] = metrics
            entry["source_ranks"][data_type] = rank

    preliminary = sorted(
        cohort.values(),
        key=lambda item: _coarse_leader_score(item["metrics_90d"]),
        reverse=True,
    )[:PRELIMINARY_LIMIT]

    discovered: dict[str, dict[str, Any]] = {}
    for item in preliminary:
        m90 = item["metrics_90d"]
        portfolio_id = m90["portfolio_id"]
        try:
            search_rows = fetch_leaderboard(
                "30D", "ROI", rows=10, nickname=m90["nickname"]
            )
            matched = next(
                (row for row in search_rows if str(row.get("leadPortfolioId")) == portfolio_id),
                None,
            )
            if not matched:
                raise RuntimeError("30D exact portfolio not found")
            m30 = _normalize_leader_row(matched, "30D")
            detail = fetch_leader_detail(portfolio_id)
            history_rows = fetch_position_history(portfolio_id)
            history = analyze_position_history(history_rows)
            score, stage, reasons = score_leader(m30, m90, history)
            discovered[portfolio_id] = {
                "portfolio_id": portfolio_id,
                "nickname": m90["nickname"],
                "score": score,
                "stage": stage,
                "reasons": reasons,
                "metrics_30d": m30,
                "metrics_90d": m90,
                "history": history,
                "detail": {
                    "margin_balance_usdt": _safe_float(detail.get("marginBalance")),
                    "aum_usdt": _safe_float(detail.get("aumAmount")),
                    "profit_share_pct": _safe_float(detail.get("profitSharingRate")),
                    "position_show": bool(detail.get("positionShow")),
                    "status": str(detail.get("status") or ""),
                    "tags": [str(tag) for tag in (detail.get("tag") or [])],
                    "min_fixed_ratio_usdt": _safe_float(detail.get("fixedRadioMinCopyUsd")),
                    "min_fixed_amount_usdt": _safe_float(detail.get("fixedAmountMinCopyUsd")),
                },
                "source_ranks": item["source_ranks"],
                "discovered_at": now_kst(),
            }
            time.sleep(0.08)
        except Exception as exc:
            print(f"[copy-intel] leader {portfolio_id} detail failed: {exc}")
            _journal("leader_discovery_failed", portfolio_id=portfolio_id, error=str(exc))
    return discovered


def _all_price_maps() -> tuple[dict[str, float], dict[str, float]]:
    futures_rows = _api_json("GET", FUTURES_API + "/fapi/v1/ticker/price")
    try:
        spot_rows = _api_json("GET", SPOT_API + "/api/v3/ticker/price")
    except Exception as exc:
        # 국가별 Spot API 제한이 Futures/Copy 인텔리전스 전체를 멈추게 하지 않는다.
        print(f"[copy-intel] spot ticker unavailable: {exc}")
        spot_rows = []
    futures = {
        str(row.get("symbol")): _safe_float(row.get("price"))
        for row in (futures_rows or []) if isinstance(row, dict)
    }
    spot = {
        str(row.get("symbol")): _safe_float(row.get("price"))
        for row in (spot_rows or []) if isinstance(row, dict)
    }
    return futures, spot


def _all_volume_maps() -> tuple[dict[str, float], dict[str, float]]:
    futures_rows = _api_json("GET", FUTURES_API + "/fapi/v1/ticker/24hr")
    try:
        spot_rows = _api_json("GET", SPOT_API + "/api/v3/ticker/24hr")
    except Exception as exc:
        print(f"[copy-intel] spot volume unavailable: {exc}")
        spot_rows = []
    futures = {
        str(row.get("symbol")): _safe_float(row.get("quoteVolume"))
        for row in (futures_rows or []) if isinstance(row, dict)
    }
    spot = {
        str(row.get("symbol")): _safe_float(row.get("quoteVolume"))
        for row in (spot_rows or []) if isinstance(row, dict)
    }
    return futures, spot


def _grid_spacing_pct(lower: float, upper: float, count: int, grid_type: str, current: float) -> float:
    if lower <= 0 or upper <= lower or count <= 0:
        return 0.0
    if grid_type.upper() == "GEO":
        return ((upper / lower) ** (1 / count) - 1) * 100
    reference = current if current > 0 else (lower + upper) / 2
    return ((upper - lower) / count) / reference * 100


def score_grid_strategy(
    row: dict[str, Any], current_price: float, seed: float,
    quote_volume_24h: Optional[float] = None,
) -> dict[str, Any]:
    params = row.get("strategyParams") or {}
    strategy_type = int(_safe_float(row.get("strategyType"), 1))
    market = "spot" if strategy_type == 1 else "futures"
    lower = _safe_float(params.get("lowerLimit"))
    upper = _safe_float(params.get("upperLimit"))
    count = int(_safe_float(params.get("gridCount")))
    runtime_days = _safe_float(row.get("runningTime")) / 86400
    mdd = _safe_float(row.get("sevenDayMdd"))
    leverage = _safe_float(params.get("leverage"), 1.0) if market == "futures" else 1.0
    spacing = _grid_spacing_pct(lower, upper, count, str(params.get("type") or "ARITH"), current_price)
    fee_pct = (SPOT_GRID_ROUND_TRIP_FEE if market == "spot" else FUTURES_GRID_ROUND_TRIP_FEE) * 100
    net_edge = spacing - fee_pct
    min_investment = _safe_float(row.get("minInvestment"))
    in_range = lower <= current_price <= upper if current_price > 0 else False
    stop_set = bool(params.get("stopLowerLimit") or params.get("stopUpperLimit") or row.get("stopSlPnl"))
    budget = seed * min(TOTAL_ALLOCATION_CAP_PCT, GRID_PER_BOT_ALLOCATION_PCT)

    reasons = []
    if runtime_days < 30:
        reasons.append("runtime<30d")
    if not in_range:
        reasons.append("price_outside_grid")
    if net_edge <= 0:
        reasons.append("grid_edge_after_fee<=0")
    if min_investment > budget:
        reasons.append("min_investment_exceeds_seed_budget")
    if quote_volume_24h is not None and quote_volume_24h < GRID_MIN_24H_QUOTE_USD:
        reasons.append("insufficient_24h_liquidity")
    if not re.fullmatch(r"[A-Z0-9]+(?:USDT|USDC)", str(row.get("symbol") or "")):
        reasons.append("unsupported_or_nonstandard_quote")
    if mdd > MAX_MDD_PCT:
        reasons.append(f"7d_mdd>{MAX_MDD_PCT:g}%")
    if market == "futures" and leverage > MAX_LEVERAGE:
        reasons.append(f"leverage>{MAX_LEVERAGE:g}x")
    if market == "futures" and not stop_set:
        reasons.append("no_stop")

    score = min(20.0, runtime_days / 90 * 20)
    score += 20.0 if in_range else 0.0
    score += min(20.0, max(0.0, net_edge) / 0.5 * 20)
    score += max(0.0, 15.0 * (1 - mdd / max(MAX_MDD_PCT, 1)))
    score += 10.0 if min_investment <= budget else 0.0
    score += min(10.0, math.log10(max(1.0, _safe_float(row.get("matchedCount")))) * 3)
    score += 5.0 if market == "spot" or stop_set else 0.0
    if market == "futures" and leverage > MAX_LEVERAGE:
        score -= 25.0
    return {
        "strategy_id": str(row.get("strategyId") or ""),
        "market": market,
        "symbol": str(row.get("symbol") or ""),
        "score": round(max(0.0, min(100.0, score)), 2),
        "stage": "watch" if not reasons else "reject",
        "reasons": reasons,
        "roi_pct": _safe_float(row.get("roi")),
        "pnl_usdt": _safe_float(row.get("pnl")),
        "runtime_days": runtime_days,
        "mdd_7d_pct": mdd,
        "copy_count": int(_safe_float(row.get("copyCount"))),
        "matched_count": int(_safe_float(row.get("matchedCount"))),
        "current_price": current_price,
        "lower": lower,
        "upper": upper,
        "grid_count": count,
        "grid_type": str(params.get("type") or ""),
        "grid_spacing_pct": spacing,
        "net_grid_edge_pct": net_edge,
        "leverage": leverage,
        "direction": int(_safe_float(row.get("direction"))),
        "min_investment_usdt": min_investment,
        "quote_volume_24h_usd": quote_volume_24h,
        "stop_set": stop_set,
    }


def _discover_grid_bots(seed: float) -> list[dict[str, Any]]:
    futures_prices, spot_prices = _all_price_maps()
    futures_volumes, spot_volumes = _all_volume_maps()
    strategies: dict[tuple[int, str], dict[str, Any]] = {}
    for strategy_type in (1, 2):
        for sort in ("roi", "runningTime", "pnl"):
            try:
                for row in fetch_grid_marketplace(strategy_type, sort, rows=30):
                    key = (strategy_type, str(row.get("strategyId") or ""))
                    strategies[key] = row
            except Exception as exc:
                print(f"[copy-intel] grid type={strategy_type} sort={sort} failed: {exc}")

    scored = []
    for (strategy_type, _), row in strategies.items():
        symbol = str(row.get("symbol") or "")
        prices = spot_prices if strategy_type == 1 else futures_prices
        volumes = spot_volumes if strategy_type == 1 else futures_volumes
        current = _safe_float(prices.get(symbol))
        quote_volume = _safe_float(volumes.get(symbol)) if symbol in volumes else None
        scored.append(score_grid_strategy(row, current, seed, quote_volume))
    scored.sort(key=lambda item: (item["stage"] == "watch", item["score"]), reverse=True)
    # Spot 고득점이 Futures 진단을 전부 밀어내지 않도록 시장별 코호트를 보존한다.
    per_market = max(TRACKED_GRID_BOTS * 2, 10)
    selected = []
    for market in ("spot", "futures"):
        selected.extend([row for row in scored if row["market"] == market][:per_market])
    selected.sort(key=lambda item: (item["stage"] == "watch", item["score"]), reverse=True)
    return selected


def run_discovery(state: dict[str, Any]) -> dict[str, Any]:
    _refresh_shadow_seed(state)
    leaders = _discover_leaders()
    ranked = sorted(leaders.values(), key=lambda item: item["score"], reverse=True)
    # shadow 후보가 너무 적어도 reject를 억지 승격하지 않는다.
    if ranked:
        state["leaders"] = {item["portfolio_id"]: item for item in ranked}
    else:
        _journal("leader_discovery_empty_preserved", previous=len(state.get("leaders") or {}))
        ranked = sorted(
            state.get("leaders", {}).values(),
            key=lambda item: _safe_float(item.get("score")), reverse=True,
        )
    grid_bots = _discover_grid_bots(_safe_float(state.get("seed_usdt")))
    if grid_bots:
        state["grid_bots"] = grid_bots
    else:
        _journal("grid_discovery_empty_preserved", previous=len(state.get("grid_bots") or []))
    state["last_discovery_ts"] = time.time()
    snapshot = {
        "event": "discovery_snapshot",
        "ts": time.time(),
        "at": now_kst(),
        "policy": POLICY,
        "seed_usdt": state.get("seed_usdt"),
        "leaders": ranked,
        "grid_bots": state["grid_bots"],
    }
    append_jsonl(SNAPSHOT_FILE, snapshot)
    return {
        "leaders": len(ranked),
        "shadow_eligible": sum(1 for item in ranked if item["stage"] == "shadow"),
        "grid_bots": len(state["grid_bots"]),
        "grid_watch": sum(1 for item in state["grid_bots"] if item["stage"] == "watch"),
    }


def _fill_id(fill: dict[str, Any]) -> str:
    fields = (
        fill.get("time"), fill.get("symbol"), fill.get("side"), fill.get("price"),
        fill.get("qty"), fill.get("positionSide"), fill.get("fee"), fill.get("realizedProfit"),
    )
    return hashlib.sha256("|".join(str(value) for value in fields).encode("utf-8")).hexdigest()[:24]


def _position_key(portfolio_id: str, symbol: str, position_side: str) -> str:
    return f"{portfolio_id}:{symbol}:{position_side}"


def _is_open_fill(fill: dict[str, Any]) -> bool:
    side = str(fill.get("side") or "").upper()
    position_side = str(fill.get("positionSide") or "").upper()
    return (position_side == "LONG" and side == "BUY") or (
        position_side == "SHORT" and side == "SELL"
    )


def _market_price(symbol: str) -> float:
    response = _api_json(
        "GET", FUTURES_API + "/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=8
    )
    return _safe_float((response or {}).get("price")) if isinstance(response, dict) else 0.0


def _execution_price(market: float, side: str) -> float:
    if side.upper() == "BUY":
        return market * (1 + ONE_WAY_SLIPPAGE)
    return market * (1 - ONE_WAY_SLIPPAGE)


def _tracked_shadow_leaders(state: dict[str, Any]) -> list[dict[str, Any]]:
    eligible = [
        item for item in state.get("leaders", {}).values()
        if isinstance(item, dict) and item.get("stage") == "shadow"
    ]
    eligible.sort(key=lambda item: _safe_float(item.get("score")), reverse=True)
    return eligible[:TRACKED_LEADERS]


def _leader_budget(state: dict[str, Any]) -> float:
    seed = _safe_float(state.get("seed_usdt"))
    per_leader = seed * PER_LEADER_ALLOCATION_PCT
    if TRACKED_LEADERS > 0:
        per_leader = min(per_leader, seed * TOTAL_ALLOCATION_CAP_PCT / TRACKED_LEADERS)
    return max(0.0, per_leader)


def _open_shadow_fill(
    state: dict[str, Any], leader: dict[str, Any], fill: dict[str, Any], market: float
) -> Optional[dict[str, Any]]:
    shadow = state["shadow"]
    portfolio_id = leader["portfolio_id"]
    symbol = str(fill.get("symbol") or "")
    position_side = str(fill.get("positionSide") or "").upper()
    key = _position_key(portfolio_id, symbol, position_side)
    leader_fill_notional = abs(_safe_float(fill.get("quantity")))
    leader_margin = _safe_float((leader.get("detail") or {}).get("margin_balance_usdt"))
    if leader_margin <= 0:
        return None
    leader_fraction = min(5.0, leader_fill_notional / leader_margin)
    budget = _leader_budget(state)
    desired_notional = budget * leader_fraction

    current = shadow["positions"].get(key) or {}
    max_notional = budget * SHADOW_LEVERAGE_CAP
    used_notional = sum(
        _safe_float(position.get("qty")) * _safe_float(position.get("avg_entry"))
        for position in shadow["positions"].values()
        if str(position.get("portfolio_id")) == portfolio_id
    )
    copy_notional = max(0.0, min(desired_notional, max_notional - used_notional))
    if copy_notional < MIN_COPY_NOTIONAL:
        skipped = {
            "portfolio_id": portfolio_id,
            "nickname": leader["nickname"],
            "symbol": symbol,
            "reason": "below_min_notional",
            "copy_notional": copy_notional,
            "leader_fraction": leader_fraction,
            "time": fill.get("time"),
        }
        shadow["skipped"].append(skipped)
        shadow["skipped"] = shadow["skipped"][-500:]
        _journal("shadow_skipped", **skipped)
        return None

    side = str(fill.get("side") or "").upper()
    execution = _execution_price(market, side)
    qty = copy_notional / execution
    old_qty = _safe_float(current.get("qty"))
    new_qty = old_qty + qty
    avg_entry = (
        (_safe_float(current.get("avg_entry")) * old_qty + execution * qty) / new_qty
        if new_qty > 0 else execution
    )
    fee = copy_notional * TAKER_FEE_RATE
    state["cash_usdt"] = _safe_float(state.get("cash_usdt")) - fee
    shadow["positions"][key] = {
        "key": key,
        "portfolio_id": portfolio_id,
        "nickname": leader["nickname"],
        "symbol": symbol,
        "position_side": position_side,
        "qty": new_qty,
        "avg_entry": avg_entry,
        "source_leader_qty": _safe_float(shadow["leader_qty"].get(key)),
        "opened_ts": current.get("opened_ts") or time.time(),
        "entry_fees": _safe_float(current.get("entry_fees")) + fee,
        "score_at_entry": leader.get("score"),
    }
    event = {
        "action": "OPEN" if old_qty == 0 else "ADD",
        "portfolio_id": portfolio_id,
        "nickname": leader["nickname"],
        "symbol": symbol,
        "position_side": position_side,
        "copy_notional": copy_notional,
        "execution_price": execution,
        "market_price": market,
        "fee_usdt": fee,
        "leader_fraction": leader_fraction,
        "fill_delay_seconds": max(0.0, time.time() - _safe_float(fill.get("time")) / 1000),
    }
    _journal("shadow_open", **event)
    return event


def _close_shadow_fill(
    state: dict[str, Any], leader: dict[str, Any], fill: dict[str, Any], market: float,
    leader_qty_before: float,
) -> Optional[dict[str, Any]]:
    shadow = state["shadow"]
    portfolio_id = leader["portfolio_id"]
    symbol = str(fill.get("symbol") or "")
    position_side = str(fill.get("positionSide") or "").upper()
    key = _position_key(portfolio_id, symbol, position_side)
    position = shadow["positions"].get(key)
    if not position or leader_qty_before <= 0:
        return None
    close_fraction = min(1.0, abs(_safe_float(fill.get("qty"))) / leader_qty_before)
    close_qty = min(_safe_float(position.get("qty")), _safe_float(position.get("qty")) * close_fraction)
    if close_qty <= 0:
        return None
    side = str(fill.get("side") or "").upper()
    execution = _execution_price(market, side)
    notional = close_qty * execution
    fee = notional * TAKER_FEE_RATE
    position_qty_before = _safe_float(position.get("qty"))
    entry_fee_alloc = _safe_float(position.get("entry_fees")) * (
        close_qty / position_qty_before if position_qty_before > 0 else 0.0
    )
    entry = _safe_float(position.get("avg_entry"))
    if position_side == "LONG":
        gross = (execution - entry) * close_qty
    else:
        gross = (entry - execution) * close_qty
    net = gross - fee - entry_fee_alloc
    # 진입 수수료는 OPEN 시 이미 cash에서 차감됐다. 여기서는 청산 손익과 청산
    # 수수료만 cash에 반영하고, event net에는 양방향 비용을 모두 표시한다.
    state["cash_usdt"] = _safe_float(state.get("cash_usdt")) + gross - fee
    remaining = position_qty_before - close_qty
    if remaining <= 1e-12:
        shadow["positions"].pop(key, None)
    else:
        position["qty"] = remaining
        position["entry_fees"] = max(0.0, _safe_float(position.get("entry_fees")) - entry_fee_alloc)
        position["source_leader_qty"] = max(0.0, leader_qty_before - abs(_safe_float(fill.get("qty"))))

    event = {
        "action": "CLOSE" if remaining <= 1e-12 else "REDUCE",
        "portfolio_id": portfolio_id,
        "nickname": leader["nickname"],
        "symbol": symbol,
        "position_side": position_side,
        "close_fraction": close_fraction,
        "qty": close_qty,
        "entry_price": entry,
        "execution_price": execution,
        "market_price": market,
        "gross_pnl_usdt": gross,
        "entry_fee_usdt": entry_fee_alloc,
        "fee_usdt": fee,
        "net_pnl_usdt": net,
        "closed_ts": time.time(),
        "fill_delay_seconds": max(0.0, time.time() - _safe_float(fill.get("time")) / 1000),
    }
    shadow["closed"].append(event)
    shadow["closed"] = shadow["closed"][-2000:]
    _journal("shadow_close", **event)
    return event


def process_leader_fills(
    state: dict[str, Any], leader: dict[str, Any], fills: list[dict[str, Any]],
    price_getter=_market_price,
) -> list[dict[str, Any]]:
    shadow = state["shadow"]
    portfolio_id = leader["portfolio_id"]
    seen = list(shadow["seen_fills"].get(portfolio_id) or [])
    seen_set = set(seen)
    fill_pairs = [(_fill_id(fill), fill) for fill in fills]

    if not shadow["baselined"].get(portfolio_id):
        latest_fill_ms = max((_safe_float(fill.get("time")) for _, fill in fill_pairs), default=0.0)
        shadow["seen_fills"][portfolio_id] = [fill_id for fill_id, _ in fill_pairs][-1000:]
        shadow["baselined"][portfolio_id] = {
            "at": now_kst(), "ts": time.time(), "fills": len(fill_pairs),
            "latest_fill_delay_seconds": (
                max(0.0, time.time() - latest_fill_ms / 1000) if latest_fill_ms else None
            ),
        }
        _journal(
            "leader_baselined", portfolio_id=portfolio_id, fills=len(fill_pairs),
            latest_fill_delay_seconds=shadow["baselined"][portfolio_id]["latest_fill_delay_seconds"],
        )
        return []

    unseen = [(fill_id, fill) for fill_id, fill in fill_pairs if fill_id not in seen_set]
    unseen.sort(key=lambda pair: _safe_float(pair[1].get("time")))
    events: list[dict[str, Any]] = []
    prices: dict[str, float] = {}
    for fill_id, fill in unseen:
        symbol = str(fill.get("symbol") or "")
        position_side = str(fill.get("positionSide") or "").upper()
        if not symbol or position_side not in ("LONG", "SHORT"):
            seen.append(fill_id)
            continue
        key = _position_key(portfolio_id, symbol, position_side)
        leader_qty_before = _safe_float(shadow["leader_qty"].get(key))
        qty = abs(_safe_float(fill.get("qty")))
        opening = _is_open_fill(fill)
        if opening:
            shadow["leader_qty"][key] = leader_qty_before + qty
        else:
            shadow["leader_qty"][key] = max(0.0, leader_qty_before - qty)
        if symbol not in prices:
            prices[symbol] = _safe_float(price_getter(symbol))
        market = prices[symbol]
        if market <= 0:
            seen.append(fill_id)
            continue
        event = (
            _open_shadow_fill(state, leader, fill, market)
            if opening else
            _close_shadow_fill(state, leader, fill, market, leader_qty_before)
        )
        if event:
            events.append(event)
        seen.append(fill_id)
    shadow["seen_fills"][portfolio_id] = seen[-1000:]
    return events


def poll_shadow(state: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    tracked = _tracked_shadow_leaders(state)
    for leader in tracked:
        portfolio_id = leader["portfolio_id"]
        try:
            fills = fetch_trade_history(portfolio_id)
            events.extend(process_leader_fills(state, leader, fills))
        except Exception as exc:
            print(f"[copy-intel] fill poll {portfolio_id} failed: {exc}")
            _journal("fill_poll_failed", portfolio_id=portfolio_id, error=str(exc))
        time.sleep(0.05)
    return events


def _shadow_performance(state: dict[str, Any]) -> dict[str, float]:
    closed = state["shadow"].get("closed") or []
    pnls = [_safe_float(row.get("net_pnl_usdt")) for row in closed]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    return {
        "closed": float(len(closed)),
        "net_pnl_usdt": sum(pnls),
        "win_rate_pct": len(wins) / len(closed) * 100 if closed else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (99.0 if wins else 0.0),
    }


def build_report(state: dict[str, Any]) -> str:
    leaders = sorted(
        [item for item in state.get("leaders", {}).values() if isinstance(item, dict)],
        key=lambda item: _safe_float(item.get("score")), reverse=True,
    )
    shadow_candidates = [item for item in leaders if item.get("stage") == "shadow"]
    grids = [item for item in state.get("grid_bots", []) if item.get("stage") == "watch"]
    perf = _shadow_performance(state)
    lines = [
        "🧠 <b>Binance 봇·리드 인텔리전스</b>",
        f"현재 기준시드 <b>${_safe_float(state.get('seed_usdt')):,.2f}</b> "
        f"(<code>{escape(str(state.get('seed_source') or ''))}</code>)",
        f"리드 분석 {len(leaders)} · 섀도 통과 {len(shadow_candidates)} · "
        f"그리드 관찰 {len(grids)}",
    ]
    for item in shadow_candidates[:TRACKED_LEADERS]:
        m30 = item.get("metrics_30d") or {}
        m90 = item.get("metrics_90d") or {}
        hist = item.get("history") or {}
        detail = item.get("detail") or {}
        copy_mins = [
            _safe_float(detail.get(key))
            for key in ("min_fixed_ratio_usdt", "min_fixed_amount_usdt")
            if _safe_float(detail.get(key)) > 0
        ]
        copy_min = min(copy_mins) if copy_mins else 0.0
        min_note = (
            f" · 공식최소 ${copy_min:.0f}{' (시드초과)' if copy_min > _safe_float(state.get('seed_usdt')) else ''}"
            if copy_min else ""
        )
        visibility = "실시간 포지션 비공개·체결이력 관찰" if not detail.get("position_show", True) else "공개 포지션"
        baseline = (state.get("shadow", {}).get("baselined", {}) or {}).get(item["portfolio_id"]) or {}
        delay = _safe_float(baseline.get("latest_fill_delay_seconds"), -1.0)
        delay_note = f" · 최근 공개지연 {delay/60:.0f}분" if delay >= 0 else ""
        lines.append(
            f"✅ <b>{escape(str(item.get('nickname') or '?'))}</b> {item.get('score', 0):.1f}점 · "
            f"30D {m30.get('roi_pct', 0):+.1f}% / 90D {m90.get('roi_pct', 0):+.1f}% · "
            f"MDD {max(_safe_float(m30.get('mdd_pct')), _safe_float(m90.get('mdd_pct'))):.1f}% · "
            f"PF {hist.get('profit_factor', 0):.2f}{min_note} · {visibility}{delay_note}"
        )
    if not shadow_candidates:
        rejected = Counter(
            reason for item in leaders for reason in (item.get("reasons") or [])
        )
        common = ", ".join(f"{reason} {count}" for reason, count in rejected.most_common(4))
        lines.append("⚠️ 자동 통과 리드 없음" + (f" · {escape(common)}" if common else ""))
    for item in grids[:3]:
        lines.append(
            f"🔲 {escape(item['market'])} <b>{escape(item['symbol'])}</b> · "
            f"{item['runtime_days']:.0f}일 · 순그리드폭 {item['net_grid_edge_pct']:.3f}% · "
            f"최소 ${item['min_investment_usdt']:.0f}"
        )
    lines.append(
        f"섀도 체결 {int(perf['closed'])} · 승률 {perf['win_rate_pct']:.1f}% · "
        f"PF {perf['profit_factor']:.2f} · 순손익 <b>${perf['net_pnl_usdt']:+.2f}</b>"
    )
    lines.append(
        f"보유 {len(state['shadow'].get('positions') or {})} · "
        f"최소금액 스킵 {len(state['shadow'].get('skipped') or [])}"
    )
    lines.append("※ 읽기 전용 + PAPER ONLY. 순위 수익률을 실주문에 자동 연결하지 않음.")
    return "\n".join(lines)


def _event_message(events: list[dict[str, Any]]) -> str:
    lines = ["🧪 <b>Binance 리드 섀도 체결</b>"]
    for event in events[:10]:
        if event["action"] in ("OPEN", "ADD"):
            lines.append(
                f"{event['action']} <b>{escape(event['symbol'])} {escape(event['position_side'])}</b> · "
                f"${event['copy_notional']:.2f} @ {event['execution_price']:.8g} · "
                f"{escape(event['nickname'])} · 지연 {event.get('fill_delay_seconds', 0)/60:.0f}분"
            )
        else:
            lines.append(
                f"{event['action']} <b>{escape(event['symbol'])} {escape(event['position_side'])}</b> · "
                f"${event['net_pnl_usdt']:+.2f} · {escape(event['nickname'])} · "
                f"지연 {event.get('fill_delay_seconds', 0)/60:.0f}분"
            )
    lines.append("※ 수수료·슬리피지 반영 PAPER, 실제 주문 아님")
    return "\n".join(lines)


def _maybe_send_periodic_report(
    state: dict[str, Any], now: float, *, report_now: bool = False
) -> bool:
    """Send a due report and keep it due when the network delivery fails."""
    due = now - _safe_float(state.get("last_report_ts")) >= REPORT_INTERVAL_SECONDS
    retry_ready = (
        now - _safe_float(state.get("last_report_attempt_ts"))
        >= REPORT_RETRY_SECONDS
    )
    if not report_now and (not due or not retry_ready):
        return False

    state["last_report_attempt_ts"] = now
    delivered = send_signal(build_report(state))
    state["last_report_delivered"] = delivered
    if delivered:
        state["last_report_ts"] = now
    return delivered


def run_once(*, force_discovery: bool = False, report_now: bool = False, telegram: bool = True) -> dict[str, Any]:
    state = _load_state()
    now = time.time()
    discovery = {}
    due_discovery = now - _safe_float(state.get("last_discovery_ts")) >= DISCOVERY_INTERVAL_SECONDS
    if force_discovery or due_discovery or not state.get("leaders"):
        discovery = run_discovery(state)
    events = poll_shadow(state)
    if telegram and events:
        send_signal(_event_message(events))
    reported = False
    if telegram:
        reported = _maybe_send_periodic_report(
            state, now, report_now=report_now
        )
    state["last_cycle"] = {
        "at": now_kst(),
        "events": len(events),
        "discovery": discovery,
        "reported": reported,
    }
    save_json(STATE_FILE, state)
    return {
        "ok": True,
        "policy": POLICY,
        "seed_usdt": state.get("seed_usdt"),
        "leaders": len(state.get("leaders") or {}),
        "shadow_eligible": len(_tracked_shadow_leaders(state)),
        "grid_bots": len(state.get("grid_bots") or []),
        "shadow_events": len(events),
        "open_positions": len(state["shadow"].get("positions") or {}),
        "discovery": discovery,
        "reported": reported,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover", action="store_true", help="6시간 TTL 무시하고 재수집")
    parser.add_argument("--report-now", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args(argv)
    if not try_acquire(LOCK_NAME):
        return 0
    result: dict[str, Any] = {"ok": True}
    try:
        first = True
        while True:
            started = time.monotonic()
            try:
                result = run_once(
                    force_discovery=bool(args.discover and first),
                    report_now=bool(args.report_now and first),
                    telegram=not args.no_telegram,
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
                print(f"[copy-intel] cycle failed: {exc}", flush=True)
            if args.json and not args.daemon:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            else:
                print(f"[copy-intel] {result}", flush=True)
            if not args.daemon:
                break
            first = False
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, float(args.interval) - elapsed))
    except KeyboardInterrupt:
        result = {"ok": True, "stopped": True}
    finally:
        release(LOCK_NAME)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
