#!/usr/bin/env python3
"""Polymarket 전 지갑 탐색 -> 카피 가능성 검증 -> paper/live 승급 파이프라인.

이 모듈은 고래의 표시 PnL이 아니라, 우리가 늦게 taker로 진입했을 때의 결과를
검증한다. discovery 실행은 후보와 watchlist만 갱신하며 주문을 내지 않는다.
radar 실행 역시 실제 CLOB 호가로 모의 체결만 기록한다. live 봇은 이 파이프라인이
``live_approved``로 승급한 지갑만 별도로 읽는다.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import time
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

from bot_util import append_jsonl, env_float, env_int, load_json, now_kst, save_json
from process_lock import release, try_acquire

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

STATE_FILE = ROOT / "polymarket_wallet_pipeline_state.json"
WATCHLIST_FILE = ROOT / "polymarket_wallet_watchlist.json"
RADAR_STATE_FILE = ROOT / "polymarket_wallet_radar_state.json"
RADAR_JOURNAL_FILE = ROOT / "polymarket_wallet_radar_journal.jsonl"
LEGACY_CONFIG_FILE = ROOT / "polymarket_whale_config.json"

LEADERBOARD_CATEGORIES = (
    "OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE", "MENTIONS",
    "WEATHER", "ECONOMICS", "TECH", "FINANCE",
)
LEADERBOARD_PERIODS = ("WEEK", "MONTH")
LEADERBOARD_ORDERS = ("PNL", "VOL")

DISCOVERY_GLOBAL_TRADE_LIMIT = min(
    env_int("POLYMARKET_DISCOVERY_GLOBAL_TRADE_LIMIT", 2500), 10000
)
DISCOVERY_MAX_PROFILES = env_int("POLYMARKET_DISCOVERY_MAX_PROFILES", 30)
PROFILE_TRADE_LIMIT = min(
    env_int("POLYMARKET_DISCOVERY_PROFILE_TRADE_LIMIT", 1000), 10000
)
PROFILE_WINDOW_DAYS = env_int("POLYMARKET_DISCOVERY_PROFILE_DAYS", 30)
MIN_DIRECTIONAL_USD = env_float("POLYMARKET_DISCOVERY_MIN_DIRECTIONAL_USD", 250.0)
MAX_DIRECTIONAL_OPPOSITE_RATIO = env_float(
    "POLYMARKET_DISCOVERY_MAX_OPPOSITE_RATIO", 0.15
)
MAX_PROFILE_TWO_SIDED_RATE = env_float(
    "POLYMARKET_DISCOVERY_MAX_TWO_SIDED_RATE", 0.15
)
HARD_REJECT_TWO_SIDED_RATE = env_float(
    "POLYMARKET_DISCOVERY_REJECT_TWO_SIDED_RATE", 0.30
)
MAX_MAKER_REBATE_USD = env_float(
    "POLYMARKET_DISCOVERY_MAX_MAKER_REBATE_USD", 25.0
)

# 역사 재생 시 체결 지연/슬리피지와 taker 비용을 각각 보수적으로 더한다.
BACKTEST_LATENCY_SLIPPAGE = env_float(
    "POLYMARKET_DISCOVERY_BACKTEST_SLIPPAGE", 0.03
)
BACKTEST_EXECUTION_COST = env_float(
    "POLYMARKET_DISCOVERY_BACKTEST_COST", 0.02
)
BACKTEST_MAX_ENTRY_PRICE = env_float(
    "POLYMARKET_DISCOVERY_BACKTEST_MAX_ENTRY", 0.85
)
BACKTEST_MIN_SETTLED = env_int("POLYMARKET_DISCOVERY_MIN_BACKTEST_SETTLED", 15)
BACKTEST_MIN_ROI = env_float("POLYMARKET_DISCOVERY_MIN_BACKTEST_ROI", 0.03)
BACKTEST_MIN_BOOTSTRAP_P5 = env_float(
    "POLYMARKET_DISCOVERY_MIN_BOOTSTRAP_P5", -0.02
)

PAPER_MAX_WALLETS = env_int("POLYMARKET_DISCOVERY_MAX_PAPER_WALLETS", 20)
LIVE_MAX_WALLETS = env_int("POLYMARKET_DISCOVERY_MAX_LIVE_WALLETS", 3)
FORWARD_MIN_SETTLED = env_int("POLYMARKET_DISCOVERY_FORWARD_MIN_SETTLED", 30)
FORWARD_MIN_ROI = env_float("POLYMARKET_DISCOVERY_FORWARD_MIN_ROI", 0.05)
FORWARD_MIN_BOOTSTRAP_P5 = env_float(
    "POLYMARKET_DISCOVERY_FORWARD_MIN_BOOTSTRAP_P5", 0.0
)
FORWARD_MAX_DRAWDOWN_UNITS = env_float(
    "POLYMARKET_DISCOVERY_FORWARD_MAX_DRAWDOWN", 6.0
)
LIVE_DEMOTE_MIN_SETTLED = env_int(
    "POLYMARKET_DISCOVERY_LIVE_DEMOTE_MIN_SETTLED", 20
)
LIVE_DEMOTE_ROI = env_float("POLYMARKET_DISCOVERY_LIVE_DEMOTE_ROI", -0.03)

# 실제 거래가 전혀 없는 영구 paper 상태를 피하되 과거 대박 한 건에 속지 않도록,
# 충분히 크고 분산된 비용후 백테스트만 절반 사이즈 live canary로 허용한다.
CANARY_MIN_SETTLED = env_int("POLYMARKET_DISCOVERY_CANARY_MIN_SETTLED", 50)
CANARY_MIN_ROI = env_float("POLYMARKET_DISCOVERY_CANARY_MIN_ROI", 0.20)
CANARY_MIN_BOOTSTRAP_P5 = env_float(
    "POLYMARKET_DISCOVERY_CANARY_MIN_BOOTSTRAP_P5", 0.10
)
CANARY_MAX_DRAWDOWN_UNITS = env_float(
    "POLYMARKET_DISCOVERY_CANARY_MAX_DRAWDOWN", 5.0
)
CANARY_MAX_LARGEST_WIN_SHARE = env_float(
    "POLYMARKET_DISCOVERY_CANARY_MAX_LARGEST_WIN_SHARE", 0.25
)
CANARY_EARLY_STOP_SETTLED = env_int(
    "POLYMARKET_DISCOVERY_CANARY_EARLY_STOP_SETTLED", 8
)
CANARY_EARLY_STOP_ROI = env_float(
    "POLYMARKET_DISCOVERY_CANARY_EARLY_STOP_ROI", -0.05
)
CANARY_LIVE_RISK_MULT = env_float(
    "POLYMARKET_DISCOVERY_CANARY_LIVE_RISK_MULT", 0.50
)

RADAR_BET_USD = env_float("POLYMARKET_RADAR_BET_USD", 10.0)
RADAR_MAX_SIGNAL_AGE = env_int("POLYMARKET_RADAR_MAX_SIGNAL_AGE", 120)
RADAR_MIN_EDGE = env_float("POLYMARKET_RADAR_MIN_ENTRY_EDGE", 0.05)
RADAR_MAX_ENTRY = env_float("POLYMARKET_RADAR_MAX_ENTRY_PRICE", 0.85)


def _get_json(url: str, params: dict[str, Any] | None = None,
              timeout: int = 20) -> Any:
    response = requests.get(url, params=params or {}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _wallet_of(row: dict[str, Any]) -> str:
    return str(
        row.get("proxyWallet") or row.get("proxy_wallet")
        or row.get("wallet") or row.get("address") or ""
    ).lower()


def _condition_of(row: dict[str, Any]) -> str:
    return str(
        row.get("conditionId") or row.get("condition_id")
        or row.get("market") or ""
    )


def _trade_usd(row: dict[str, Any]) -> float:
    direct = _float(row.get("usdcSize"))
    if direct > 0:
        return direct
    return max(_float(row.get("size")), 0.0) * max(_float(row.get("price")), 0.0)


def _outcome_index(row: dict[str, Any]) -> int | None:
    raw = row.get("outcomeIndex")
    if raw is None:
        raw = row.get("outcome_index")
    index = _int(raw, -1)
    return index if index in {0, 1} else None


def _timestamp(row: dict[str, Any]) -> int:
    return _int(row.get("timestamp") or row.get("createdAt") or row.get("time"))


def _leaderboard_rows(category: str, period: str, order: str) -> list[dict[str, Any]]:
    rows = _get_json(
        f"{DATA_API}/v1/leaderboard",
        {
            "category": category,
            "timePeriod": period,
            "orderBy": order,
            "limit": 50,
            "offset": 0,
        },
    )
    return rows if isinstance(rows, list) else []


def _global_trade_rows() -> list[dict[str, Any]]:
    rows = _get_json(
        f"{DATA_API}/trades",
        {"limit": DISCOVERY_GLOBAL_TRADE_LIMIT, "takerOnly": "true"},
    )
    return rows if isinstance(rows, list) else []


def discover_candidates() -> dict[str, dict[str, Any]]:
    """공개 leaderboard와 최신 taker feed를 합쳐 활성 후보를 만든다."""
    candidates: dict[str, dict[str, Any]] = {}

    def touch(wallet: str) -> dict[str, Any]:
        return candidates.setdefault(wallet, {
            "wallet": wallet,
            "leaderboard": [],
            "recent_taker_trades": 0,
            "recent_taker_usd": 0.0,
            "last_activity_ts": 0,
        })

    jobs = [
        (category, period, order)
        for category in LEADERBOARD_CATEGORIES
        for period in LEADERBOARD_PERIODS
        for order in LEADERBOARD_ORDERS
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_leaderboard_rows, *job): job for job in jobs
        }
        for future in as_completed(futures):
            category, period, order = futures[future]
            try:
                rows = future.result()
            except Exception:
                continue
            for rank, row in enumerate(rows, start=1):
                wallet = _wallet_of(row)
                if not wallet:
                    continue
                entry = touch(wallet)
                entry["leaderboard"].append({
                    "category": category,
                    "period": period,
                    "order": order,
                    "rank": _int(row.get("rank"), rank),
                    "pnl": _float(row.get("pnl")),
                    "volume": _float(row.get("vol") or row.get("volume")),
                    "user_name": str(row.get("userName") or row.get("name") or ""),
                })

    try:
        global_rows = _global_trade_rows()
    except Exception:
        global_rows = []
    for row in global_rows:
        wallet = _wallet_of(row)
        if not wallet:
            continue
        entry = touch(wallet)
        entry["recent_taker_trades"] += 1
        entry["recent_taker_usd"] += _trade_usd(row)
        entry["last_activity_ts"] = max(entry["last_activity_ts"], _timestamp(row))

    for entry in candidates.values():
        boards = entry["leaderboard"]
        entry["best_rank"] = min((r["rank"] for r in boards), default=9999)
        entry["week_pnl"] = max(
            (r["pnl"] for r in boards if r["period"] == "WEEK"), default=0.0
        )
        entry["month_pnl"] = max(
            (r["pnl"] for r in boards if r["period"] == "MONTH"), default=0.0
        )
        entry["leaderboard_appearances"] = len(boards)
        positive_efficiencies = [
            min(max(r["pnl"] / r["volume"], 0.0), 1.0)
            for r in boards if r["pnl"] > 0 and r["volume"] >= 100
        ]
        entry["leader_efficiency"] = max(positive_efficiencies, default=0.0)
        positive_pnl = max(entry["week_pnl"], entry["month_pnl"], 0.0)
        entry["discovery_score"] = (
            math.log1p(max(entry["recent_taker_usd"], 0.0))
            + 1.5 * math.log1p(len(boards))
            + max(0.0, 5.0 - math.log10(max(entry["best_rank"], 1)))
            + (2.0 if entry["week_pnl"] > 0 else 0.0)
            + (2.0 if entry["month_pnl"] > 0 else 0.0)
        )
        # 순수 거래량 상위는 maker가 장악하기 쉽다. 실제 카피 후보 탐색 순서는
        # 손익/거래량 효율과 여러 카테고리에서의 재현성을 더 크게 본다.
        entry["copyability_priority"] = (
            6.0 * entry["leader_efficiency"]
            + 1.5 * math.log1p(len(boards))
            + 0.7 * math.log1p(positive_pnl)
            - 0.35 * math.log1p(max(
                (r["volume"] for r in boards), default=0.0
            ))
            + (1.0 if entry["last_activity_ts"] else 0.0)
        )
    return candidates


def fetch_wallet_trades(wallet: str, *, taker_only: bool = False) -> list[dict[str, Any]]:
    rows = _get_json(
        f"{DATA_API}/trades",
        {
            "user": wallet,
            "limit": PROFILE_TRADE_LIMIT,
            "takerOnly": str(bool(taker_only)).lower(),
        },
    )
    return rows if isinstance(rows, list) else []


def _material_two_sided(buys: list[float]) -> bool:
    largest = max(buys) if buys else 0.0
    smallest = min(buys) if buys else 0.0
    return largest >= MIN_DIRECTIONAL_USD and smallest / largest >= 0.15


def profile_trade_rows(all_rows: Iterable[dict[str, Any]],
                       taker_rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """지갑 거래에서 방향성/양방향 재고/추정 taker 비중을 계산한다."""
    cutoff = int(time.time()) - PROFILE_WINDOW_DAYS * 86400
    rows = [r for r in all_rows if _timestamp(r) >= cutoff]
    takers = [r for r in taker_rows if _timestamp(r) >= cutoff]
    net_flow: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    buy_flow: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    trade_sizes: list[float] = []
    latest = 0
    for row in rows:
        condition = _condition_of(row)
        outcome = _outcome_index(row)
        if not condition or outcome is None:
            continue
        usd = _trade_usd(row)
        trade_sizes.append(usd)
        latest = max(latest, _timestamp(row))
        sign = 1.0 if str(row.get("side") or "BUY").upper() == "BUY" else -1.0
        net_flow[condition][outcome] += sign * usd
        if sign > 0:
            buy_flow[condition][outcome] += usd

    material_conditions = [
        condition for condition, values in buy_flow.items()
        if max(values) >= MIN_DIRECTIONAL_USD
    ]
    two_sided = sum(
        _material_two_sided(buy_flow[condition]) for condition in material_conditions
    )
    directional = 0
    for condition in material_conditions:
        positive = [max(v, 0.0) for v in net_flow[condition]]
        largest = max(positive)
        smallest = min(positive)
        if largest >= MIN_DIRECTIONAL_USD and smallest / max(largest, 1e-9) < MAX_DIRECTIONAL_OPPOSITE_RATIO:
            directional += 1
    all_usd = sum(_trade_usd(r) for r in rows)
    taker_usd = sum(_trade_usd(r) for r in takers)
    return {
        "trade_count_30d": len(rows),
        "market_count_30d": len(net_flow),
        "material_market_count": len(material_conditions),
        "directional_market_count": directional,
        "two_sided_markets": two_sided,
        "two_sided_rate": two_sided / len(material_conditions) if material_conditions else 0.0,
        "median_trade_usd": statistics.median(trade_sizes) if trade_sizes else 0.0,
        "all_trade_usd": all_usd,
        "taker_trade_usd": taker_usd,
        "estimated_taker_share": min(taker_usd / all_usd, 1.0) if all_usd > 0 else 0.0,
        "last_activity_ts": latest,
        "active_age_hours": max((time.time() - latest) / 3600, 0.0) if latest else 99999.0,
    }


def fetch_recent_maker_rebates(wallet: str, days: int = 3) -> float:
    total = 0.0
    today = datetime.now(timezone.utc).date()
    for back in range(1, days + 1):
        date = (today - timedelta(days=back)).isoformat()
        try:
            payload = _get_json(
                f"{CLOB_API}/rebates/current",
                {"maker_address": wallet, "date": date},
            )
        except Exception:
            continue
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if not isinstance(row, dict):
                continue
            total += _float(
                row.get("rebated_fees_usdc") or row.get("rebate")
                or row.get("rebateAmount") or row.get("amount")
            )
    return total


def build_wallet_profile(wallet: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    all_rows = fetch_wallet_trades(wallet, taker_only=False)
    taker_rows = fetch_wallet_trades(wallet, taker_only=True)
    profile = profile_trade_rows(all_rows, taker_rows)
    promising = (
        profile["trade_count_30d"] >= 20
        and profile["material_market_count"] >= 5
        and profile["two_sided_rate"] <= HARD_REJECT_TWO_SIDED_RATE
    )
    profile["maker_rebate_3d"] = fetch_recent_maker_rebates(wallet) if promising else 0.0
    profile["market_maker_like"] = bool(
        profile["two_sided_rate"] > HARD_REJECT_TWO_SIDED_RATE
        or profile["maker_rebate_3d"] > MAX_MAKER_REBATE_USD
    )
    return profile, all_rows


def _resolved_winner(condition_id: str, cache: dict[str, Any]) -> int | None:
    cached = cache.get(condition_id)
    if (
        isinstance(cached, dict)
        and "winner" in cached
        and (
            cached.get("winner") in {0, 1}
            or time.time() - _float(cached.get("checked_ts")) < 3600
        )
    ):
        value = cached.get("winner")
        return int(value) if value in {0, 1} else None
    try:
        market = _get_json(f"{CLOB_API}/markets/{condition_id}", {})
    except Exception:
        cache[condition_id] = {
            "winner": None, "checked_at": now_kst(), "checked_ts": time.time()
        }
        return None
    winner = None
    if bool(market.get("closed") or market.get("active") is False):
        tokens = market.get("tokens") or []
        for index, token in enumerate(tokens[:2]):
            if bool(token.get("winner")):
                winner = index
                break
    cache[condition_id] = {
        "winner": winner, "checked_at": now_kst(), "checked_ts": time.time()
    }
    return winner


def simulate_condition(rows: Iterable[dict[str, Any]], winner: int | None) -> dict[str, Any] | None:
    """한 시장의 고래 흐름을 시간순으로 재생해 $1 카피 결과를 반환한다."""
    ordered = sorted(rows, key=_timestamp)
    net_usd = [0.0, 0.0]
    net_shares = [0.0, 0.0]
    gross_buy = [0.0, 0.0]
    position: dict[str, Any] | None = None

    for row in ordered:
        outcome = _outcome_index(row)
        if outcome is None:
            continue
        side = str(row.get("side") or "BUY").upper()
        sign = 1.0 if side == "BUY" else -1.0
        usd = _trade_usd(row)
        shares = max(_float(row.get("size")), 0.0)
        price = _float(row.get("price"))
        net_usd[outcome] += sign * usd
        net_shares[outcome] += sign * shares
        if sign > 0:
            gross_buy[outcome] += usd

        just_opened = False
        if position is None:
            selected = 0 if net_usd[0] >= net_usd[1] else 1
            opposite = 1 - selected
            opposite_ratio = gross_buy[opposite] / max(gross_buy[selected], 1e-9)
            if (
                net_usd[selected] >= MIN_DIRECTIONAL_USD
                and opposite_ratio < MAX_DIRECTIONAL_OPPOSITE_RATIO
                and price > 0
            ):
                selected_price = price if outcome == selected else 1.0 - price
                entry = selected_price * (
                    1 + BACKTEST_LATENCY_SLIPPAGE + BACKTEST_EXECUTION_COST
                )
                if 0 < entry <= BACKTEST_MAX_ENTRY_PRICE:
                    position = {
                        "outcome": selected,
                        "entry_price": min(entry, 0.999),
                        "source_price": selected_price,
                        "entry_ts": _timestamp(row),
                        "whale_shares_at_entry": max(net_shares[selected], 0.0),
                        "exit_price": None,
                        "exit_reason": "resolution",
                    }
                    just_opened = True

        if position is not None and not just_opened:
            selected = int(position["outcome"])
            opposite = 1 - selected
            opposite_ratio = gross_buy[opposite] / max(gross_buy[selected], 1e-9)
            entry_shares = max(_float(position["whale_shares_at_entry"]), 1e-9)
            reduced = max(net_shares[selected], 0.0) <= entry_shares * 0.50
            if opposite_ratio >= 0.25 or reduced:
                selected_price = price if outcome == selected else 1.0 - price
                position["exit_price"] = max(
                    min(selected_price * (1 - BACKTEST_LATENCY_SLIPPAGE - BACKTEST_EXECUTION_COST), 0.999),
                    0.001,
                )
                position["exit_reason"] = "opposite_flow" if opposite_ratio >= 0.25 else "whale_reduction"
                break

    if position is None:
        return None
    entry = _float(position["entry_price"])
    if position.get("exit_price") is not None:
        payout = _float(position["exit_price"]) / max(entry, 1e-9)
        resolved = True
        won = payout > 1.0
    elif winner in {0, 1}:
        payout = (1.0 / max(entry, 1e-9)) if winner == position["outcome"] else 0.0
        resolved = True
        won = winner == position["outcome"]
    else:
        payout = 0.0
        resolved = False
        won = False
    return {
        **position,
        "resolved": resolved,
        "won": won,
        "pnl_unit": payout - 1.0 if resolved else None,
    }


def _bootstrap_p5(samples: list[float], *, seed_key: str = "") -> float:
    if not samples:
        return 0.0
    seed = int(hashlib.sha256(seed_key.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    means = [
        statistics.fmean(rng.choice(samples) for _ in samples)
        for _ in range(1000)
    ]
    means.sort()
    return means[max(int(len(means) * 0.05) - 1, 0)]


def summarize_samples(samples: list[float], wins: list[bool] | None = None,
                      *, seed_key: str = "") -> dict[str, Any]:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in samples:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    positives = sorted((v for v in samples if v > 0), reverse=True)
    positive_sum = sum(positives)
    return {
        "settled": len(samples),
        "roi": statistics.fmean(samples) if samples else 0.0,
        "win_rate": (
            sum(bool(v) for v in wins) / len(wins)
            if wins else sum(v > 0 for v in samples) / len(samples) if samples else 0.0
        ),
        "bootstrap_p5": _bootstrap_p5(samples, seed_key=seed_key),
        "max_drawdown_units": max_drawdown,
        "largest_win_share": positives[0] / positive_sum if positive_sum > 0 else 0.0,
        "pnl_units": sum(samples),
    }


def backtest_wallet(wallet: str, rows: list[dict[str, Any]],
                    market_cache: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        condition = _condition_of(row)
        if condition and _outcome_index(row) is not None:
            grouped[condition].append(row)

    # 신호조차 없던 모든 거래 시장을 조회하지 않는다. 방향성 임계치를 넘은
    # 시장만 resolution 조회 대상으로 줄여 공개 API 부하와 실행 시간을 제한한다.
    provisional: dict[str, dict[str, Any]] = {}
    conditions = []
    for condition, condition_rows in grouped.items():
        result = simulate_condition(condition_rows, None)
        if result is None:
            continue
        provisional[condition] = result
        if not result.get("resolved"):
            conditions.append(condition)
    winners: dict[str, int | None] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_resolved_winner, condition, market_cache): condition
            for condition in conditions
        }
        for future in as_completed(futures):
            condition = futures[future]
            try:
                winners[condition] = future.result()
            except Exception:
                winners[condition] = None

    results = []
    for condition, initial in provisional.items():
        result = (
            initial if initial.get("resolved")
            else simulate_condition(grouped[condition], winners.get(condition))
        )
        if result:
            results.append({"condition_id": condition, **result})
    settled = [r for r in results if r.get("resolved")]
    samples = [_float(r.get("pnl_unit")) for r in settled]
    summary = summarize_samples(
        samples, [bool(r.get("won")) for r in settled], seed_key=wallet
    )
    summary.update({
        "signals": len(results),
        "unresolved": len(results) - len(settled),
        "early_exit_count": sum(r.get("exit_reason") != "resolution" for r in settled),
        "model": "directional_inventory_copy_v1_5pct_cost",
    })
    return summary


def candidate_stage(profile: dict[str, Any], backtest: dict[str, Any],
                    forward: dict[str, Any], previous: str = "") -> tuple[str, list[str]]:
    reasons: list[str] = []
    if profile.get("market_maker_like"):
        reasons.append("마켓메이커형 양방향 재고 또는 maker rebate")
        return "rejected", reasons
    last_activity_ts = _float(profile.get("last_activity_ts"))
    active_age_hours = (
        max((time.time() - last_activity_ts) / 3600, 0.0)
        if last_activity_ts > 0
        else _float(profile.get("active_age_hours"), 99999.0)
    )
    if active_age_hours > 48:
        reasons.append("최근 48시간 거래 없음")
        return "screened", reasons
    if profile.get("two_sided_rate", 1.0) > MAX_PROFILE_TWO_SIDED_RATE:
        reasons.append("양방향 시장 비율이 paper 기준 초과")
        return "screened", reasons
    if profile.get("directional_market_count", 0) < 5:
        reasons.append("방향성 시장 표본 부족")
        return "screened", reasons
    if backtest.get("settled", 0) < BACKTEST_MIN_SETTLED:
        reasons.append("비용 반영 백테스트 정산 표본 부족")
        return "screened", reasons
    if backtest.get("roi", 0.0) < BACKTEST_MIN_ROI:
        reasons.append("비용 반영 백테스트 ROI 미달")
        return "rejected", reasons
    if backtest.get("bootstrap_p5", -1.0) < BACKTEST_MIN_BOOTSTRAP_P5:
        reasons.append("백테스트 부트스트랩 하단 미달")
        return "screened", reasons
    if backtest.get("largest_win_share", 1.0) > 0.65:
        reasons.append("백테스트 수익이 단일 대박에 과도하게 집중")
        return "screened", reasons

    forward_n = _int(forward.get("settled"))
    forward_roi = _float(forward.get("roi"))
    if previous == "live_approved":
        severe_decay = (
            forward_n >= LIVE_DEMOTE_MIN_SETTLED
            and (
                forward_roi <= LIVE_DEMOTE_ROI
                or _float(forward.get("bootstrap_p5")) < -0.05
                or _float(forward.get("max_drawdown_units"))
                > FORWARD_MAX_DRAWDOWN_UNITS * 1.25
            )
        )
        if severe_decay:
            reasons.append("실거래 승인 후 전진 paper 성과 악화로 자동 강등")
            return "suspended", reasons
        reasons.append("승인 유지: 자동강등 기준 이내")
        return "live_approved", reasons
    if (
        forward_n >= FORWARD_MIN_SETTLED
        and forward_roi >= FORWARD_MIN_ROI
        and _float(forward.get("bootstrap_p5")) > FORWARD_MIN_BOOTSTRAP_P5
        and _float(forward.get("max_drawdown_units")) <= FORWARD_MAX_DRAWDOWN_UNITS
        and _float(forward.get("largest_win_share")) <= 0.50
    ):
        reasons.append("실제호가 전진검증 통과")
        return "live_approved", reasons

    exceptional_backtest = (
        _int(backtest.get("settled")) >= CANARY_MIN_SETTLED
        and _float(backtest.get("roi")) >= CANARY_MIN_ROI
        and _float(backtest.get("bootstrap_p5")) >= CANARY_MIN_BOOTSTRAP_P5
        and _float(backtest.get("max_drawdown_units")) <= CANARY_MAX_DRAWDOWN_UNITS
        and _float(backtest.get("largest_win_share"), 1.0)
        <= CANARY_MAX_LARGEST_WIN_SHARE
        and _float(profile.get("estimated_taker_share")) >= 0.80
        and _float(profile.get("two_sided_rate")) <= 0.05
    )
    if previous == "live_canary" and forward_n >= CANARY_EARLY_STOP_SETTLED:
        canary_decay = (
            forward_roi <= CANARY_EARLY_STOP_ROI
            or _float(forward.get("bootstrap_p5")) < -0.10
        )
        if canary_decay:
            reasons.append("live canary 전진성과 조기중단 기준 도달")
            return "suspended", reasons
    if exceptional_backtest:
        reasons.append(
            "분산된 강한 비용후 백테스트 통과 — 절반위험 live canary"
        )
        return "live_canary", reasons
    reasons.append("과거 검증 통과, 실제호가 전진 paper 관찰 중")
    return "paper", reasons


def _forward_stats(radar: dict[str, Any], wallet: str) -> dict[str, Any]:
    wstate = ((radar.get("wallets") or {}).get(wallet) or {})
    samples = [_float(v) for v in wstate.get("pnl_samples") or []]
    return summarize_samples(samples, seed_key=f"forward:{wallet}")


def build_watchlist(candidates: dict[str, dict[str, Any]],
                    radar: dict[str, Any]) -> dict[str, Any]:
    paper_rows = []
    live_rows = []
    blocked_live_rows = []
    all_rows = []
    for wallet, item in candidates.items():
        forward = _forward_stats(radar, wallet)
        previous = str(item.get("stage") or "")
        stage, reasons = candidate_stage(
            item.get("profile") or {}, item.get("backtest") or {}, forward, previous
        )
        item["stage"] = stage
        item["stage_reasons"] = reasons
        item["forward"] = forward
        profile = item.get("profile") or {}
        backtest = item.get("backtest") or {}
        score = (
            4.0 * _float(backtest.get("roi"))
            + 2.0 * _float(backtest.get("bootstrap_p5"))
            + 3.0 * _float(forward.get("roi"))
            + math.log1p(_int(forward.get("settled")))
            - _float(profile.get("two_sided_rate"))
        )
        row = {
            "wallet": wallet,
            "stage": stage,
            "score": round(score, 8),
            "expected_win_rate": round(
                max(_float(backtest.get("win_rate")), 0.50), 6
            ),
            "two_sided_rate": round(_float(profile.get("two_sided_rate")), 6),
            "maker_rebate_3d": round(_float(profile.get("maker_rebate_3d")), 6),
            "backtest": backtest,
            "forward": forward,
            "reasons": reasons,
            "live_risk_mult": (
                round(min(max(CANARY_LIVE_RISK_MULT, 0.10), 0.50), 4)
                if stage == "live_canary" else 1.0
            ),
        }
        all_rows.append(row)
        if stage == "paper":
            paper_rows.append(row)
        elif stage in {"live_approved", "live_canary"}:
            live_rows.append(row)
        if (
            bool((item.get("discovery") or {}).get("legacy_live"))
            and stage not in {"live_approved", "live_canary"}
        ):
            blocked_live_rows.append(row)
    paper_rows.sort(key=lambda row: row["score"], reverse=True)
    live_rows.sort(key=lambda row: row["score"], reverse=True)
    return {
        "version": 1,
        "generated_at": now_kst(),
        "policy": "copyable_wallet_promotion_v1",
        "paper": paper_rows[:PAPER_MAX_WALLETS],
        "live_approved": live_rows[:LIVE_MAX_WALLETS],
        "blocked_live": blocked_live_rows,
        "counts": {
            "all": len(all_rows),
            "paper": min(len(paper_rows), PAPER_MAX_WALLETS),
            "live_approved": min(len(live_rows), LIVE_MAX_WALLETS),
            "live_canary": sum(r["stage"] == "live_canary" for r in all_rows),
            "rejected": sum(r["stage"] == "rejected" for r in all_rows),
            "screened": sum(r["stage"] == "screened" for r in all_rows),
            "suspended": sum(r["stage"] == "suspended" for r in all_rows),
            "blocked_legacy_live": len(blocked_live_rows),
        },
    }


def _seed_legacy_live_candidates(
    discovered: dict[str, dict[str, Any]],
    existing: dict[str, dict[str, Any]] | None = None,
) -> None:
    """기존 live 9지갑도 신규 후보와 동일한 검증에서 예외로 두지 않는다."""
    config = load_json(LEGACY_CONFIG_FILE, default={}) or {}
    for row in config.get("whales") or []:
        wallet = str(row.get("wallet") or "").lower()
        if not wallet:
            continue
        entry = discovered.setdefault(wallet, {
            "wallet": wallet,
            "leaderboard": [],
            "recent_taker_trades": 0,
            "recent_taker_usd": 0.0,
            "last_activity_ts": 0,
            "best_rank": 9999,
            "week_pnl": 0.0,
            "month_pnl": 0.0,
            "leaderboard_appearances": 0,
            "leader_efficiency": 0.0,
            "discovery_score": 0.0,
            "copyability_priority": 0.0,
        })
        entry["legacy_live"] = True
        entry["legacy_expected_win_rate"] = _float(row.get("expected_win_rate"), 0.5)
        previous = (existing or {}).get(wallet) or {}
        # 최초 검증 또는 24시간 정기 재검증 때만 기존 9개를 우선한다. 매 주기
        # 9칸을 독점하던 구조 때문에 신규 고래 프로파일링이 지나치게 느렸다.
        needs_priority = (
            not previous.get("profile")
            or time.time() - _float(previous.get("last_profiled_ts")) >= 24 * 3600
        )
        if needs_priority:
            entry["copyability_priority"] = max(
                _float(entry.get("copyability_priority")), 1_000_000.0
            )
            entry["discovery_score"] = max(
                _float(entry.get("discovery_score")), 1_000_000.0
            )


def _watchlist_signature(watchlist: dict[str, Any]) -> str:
    payload = {
        key: [
            {
                "wallet": row.get("wallet"),
                "stage": row.get("stage"),
                "live_risk_mult": row.get("live_risk_mult"),
            }
            for row in watchlist.get(key) or []
        ]
        for key in ("paper", "live_approved", "blocked_live")
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _notify_watchlist_change(watchlist: dict[str, Any], state: dict[str, Any]) -> bool:
    signature = _watchlist_signature(watchlist)
    if signature == state.get("last_watchlist_notification_signature"):
        return False
    paper_rows = watchlist.get("paper") or []
    live_rows = watchlist.get("live_approved") or []
    blocked_rows = watchlist.get("blocked_live") or []
    counts = watchlist.get("counts") or {}
    lines = [
        "🔎 <b>[Polymarket 지갑 선별 변경]</b>",
        f"검증 후보 {counts.get('all', 0)} · paper {len(paper_rows)} · "
        f"live 승인 {len(live_rows)}(canary {counts.get('live_canary', 0)}) · "
        f"기존 live 차단 {len(blocked_rows)} · "
        f"maker/실패 {counts.get('rejected', 0)}",
    ]
    for label, rows in (("PAPER", paper_rows[:3]), ("LIVE", live_rows[:3])):
        for row in rows:
            backtest = row.get("backtest") or {}
            forward = row.get("forward") or {}
            row_label = (
                "LIVE-CANARY" if row.get("stage") == "live_canary" else label
            )
            lines.append(
                f"• {row_label} {str(row.get('wallet') or '')[:12]}… "
                f"BT {int(backtest.get('settled') or 0)}건 "
                f"{_float(backtest.get('roi')):+.1%} / "
                f"FWD {int(forward.get('settled') or 0)}건 "
                f"{_float(forward.get('roi')):+.1%}"
            )
    for row in blocked_rows[:3]:
        lines.append(
            f"• BLOCK {str(row.get('wallet') or '')[:12]}… "
            f"{str((row.get('reasons') or ['검증 실패'])[0])}"
        )
    lines.append(
        "※ 기본 승급은 실제호가 전진검증 30건, 분산된 초강력 비용후 "
        "백테스트만 50% 위험 canary로 먼저 검증합니다."
    )
    try:
        from publisher import send_review
        sent = bool(send_review("\n".join(lines)))
    except Exception:
        sent = False
    if sent:
        state["last_watchlist_notification_signature"] = signature
        state["last_watchlist_notification_at"] = now_kst()
    return sent


def run_discovery(max_profiles: int | None = None) -> dict[str, Any]:
    lock_name = "polymarket_wallet_discovery"
    if not try_acquire(lock_name):
        return {"ok": False, "skipped": "already_running"}
    try:
        state = load_json(STATE_FILE, default={}) or {}
        existing = state.get("candidates") or {}
        discovered = discover_candidates()
        if not discovered:
            return {
                "ok": False,
                "error": "public discovery sources unavailable or empty",
                "preserved_existing_state": True,
            }
        _seed_legacy_live_candidates(discovered, existing)
        now_ts = time.time()
        eligible = [
            row for row in discovered.values()
            if now_ts - _float((existing.get(row["wallet"]) or {}).get("last_profiled_ts"))
            >= 2 * 3600
        ]
        # 거래량/활동성, 손익 효율, 주간 PnL의 세 큐를 번갈아 뽑아 maker 상위권만
        # 계속 검사하는 편향을 피한다.
        queues = [
            sorted(eligible, key=lambda row: row.get("copyability_priority", 0), reverse=True),
            sorted(eligible, key=lambda row: row.get("week_pnl", 0), reverse=True),
            sorted(eligible, key=lambda row: row.get("discovery_score", 0), reverse=True),
        ]
        limit = DISCOVERY_MAX_PROFILES if max_profiles is None else max(max_profiles, 0)
        selected = []
        selected_wallets: set[str] = set()
        cursors = [0] * len(queues)
        while len(selected) < limit and any(
            cursors[index] < len(queue) for index, queue in enumerate(queues)
        ):
            for index, queue in enumerate(queues):
                while (
                    cursors[index] < len(queue)
                    and queue[cursors[index]]["wallet"] in selected_wallets
                ):
                    cursors[index] += 1
                if cursors[index] < len(queue):
                    row = queue[cursors[index]]
                    cursors[index] += 1
                    selected.append(row)
                    selected_wallets.add(row["wallet"])
                    if len(selected) >= limit:
                        break
        market_cache = state.get("market_cache") or {}
        for discovery in selected:
            wallet = discovery["wallet"]
            item = dict(existing.get(wallet) or {})
            item.update({
                "wallet": wallet,
                "discovery": discovery,
                "last_profiled_at": now_kst(),
                "last_profiled_ts": now_ts,
            })
            try:
                profile, rows = build_wallet_profile(wallet)
                item["profile"] = profile
                if not profile.get("market_maker_like") and profile.get("directional_market_count", 0) >= 5:
                    item["backtest"] = backtest_wallet(wallet, rows, market_cache)
                else:
                    item["backtest"] = item.get("backtest") or {}
                item.pop("error", None)
            except Exception as exc:
                item["error"] = str(exc)[:300]
            existing[wallet] = item

        radar = load_json(RADAR_STATE_FILE, default={}) or {}
        watchlist = build_watchlist(existing, radar)
        state.update({
            "version": 1,
            "updated_at": now_kst(),
            "discovered_wallets": len(discovered),
            "profiled_this_run": len(selected),
            "candidates": existing,
            "market_cache": market_cache,
        })
        notification_sent = _notify_watchlist_change(watchlist, state)
        save_json(STATE_FILE, state)
        save_json(WATCHLIST_FILE, watchlist)
        return {
            "ok": True,
            "discovered_wallets": len(discovered),
            "profiled": len(selected),
            "watchlist": watchlist["counts"],
            "notification_sent": notification_sent,
        }
    finally:
        release(lock_name)


def _new_radar_wallet(row: dict[str, Any]) -> dict[str, Any]:
    """과거 체결 재생 없이 watchlist 등록 이후 거래부터 관찰한다."""
    return {
        "status": "active",
        "discovery_managed": True,
        "expected_win_rate": _float(row.get("expected_win_rate"), 0.5),
        "last_seen_ts": int(time.time()),
        "net_usdc": {},
        "net_shares_v5": {},
        "signaled": {},
        "directional_signal_levels_v5": {},
        "market_flow_v2": {},
        "activity_seen_v2": [],
        "pnl_samples": [],
        "settled": 0,
        "wins": 0,
        "pnl_usd": 0.0,
        "bet_usd": 0.0,
    }


def _sync_radar_wallets(state: dict[str, Any], watchlist: dict[str, Any]) -> int:
    tracked = {
        str(row.get("wallet") or "").lower(): row
        for row in (watchlist.get("paper") or []) + (watchlist.get("live_approved") or [])
        if row.get("wallet")
    }
    wallets = state.setdefault("wallets", {})
    added = 0
    for wallet, row in tracked.items():
        if wallet not in wallets:
            wallets[wallet] = _new_radar_wallet(row)
            added += 1
        wallets[wallet]["status"] = "active"
        wallets[wallet]["expected_win_rate"] = _float(
            row.get("expected_win_rate"), wallets[wallet].get("expected_win_rate", 0.5)
        )
    for wallet, wstate in wallets.items():
        if wallet not in tracked:
            wstate["status"] = "retired"
    return added


def _radar_record_result(state: dict[str, Any], position: dict[str, Any],
                         pnl: float, *, won: bool, reason: str,
                         exit_price: float | None = None) -> None:
    wallet = str(position.get("wallet") or "")
    wstate = (state.get("wallets") or {}).get(wallet)
    if wstate is None:
        return
    bet = max(_float(position.get("bet_usd")), 1e-9)
    unit = pnl / bet
    samples = list(wstate.get("pnl_samples") or [])
    samples.append(round(unit, 8))
    wstate["pnl_samples"] = samples[-500:]
    wstate["settled"] = _int(wstate.get("settled")) + 1
    wstate["wins"] = _int(wstate.get("wins")) + (1 if won else 0)
    wstate["pnl_usd"] = _float(wstate.get("pnl_usd")) + pnl
    wstate["bet_usd"] = _float(wstate.get("bet_usd")) + bet
    append_jsonl(RADAR_JOURNAL_FILE, {
        **position,
        "event": "radar_settled",
        "settled_at": now_kst(),
        "reason": reason,
        "won": won,
        "exit_price": exit_price,
        "pnl_usd": round(pnl, 6),
        "pnl_unit": round(unit, 8),
    })


def _sell_quote(token_id: str, shares: float) -> dict[str, Any]:
    if not token_id or shares <= 0:
        return {"ok": False, "reason": "invalid_sell"}
    try:
        book = _get_json(f"{CLOB_API}/book", {"token_id": token_id})
    except Exception as exc:
        return {"ok": False, "reason": "book_unavailable", "error": str(exc)[:160]}
    levels: list[tuple[float, float]] = []
    for row in (book or {}).get("bids") or []:
        price = _float(row.get("price"))
        size = _float(row.get("size"))
        if price > 0 and size > 0:
            levels.append((price, size))
    levels.sort(key=lambda item: item[0], reverse=True)
    remaining = shares
    proceeds = 0.0
    filled = 0.0
    for price, size in levels:
        take = min(remaining, size)
        proceeds += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-8:
            break
    if remaining > max(shares * 1e-6, 1e-6) or filled <= 0:
        return {"ok": False, "reason": "insufficient_bids", "fillable_shares": filled}
    raw_price = proceeds / filled
    exit_price = max(raw_price * (1 - BACKTEST_EXECUTION_COST), 0.001)
    return {"ok": True, "exit_price": exit_price, "book_vwap": raw_price}


def _radar_exit_reason(position: dict[str, Any], wstate: dict[str, Any]) -> str:
    condition = str(position.get("condition_id") or "")
    outcome = _int(position.get("outcome_index"), -1)
    flow = (wstate.get("market_flow_v2") or {}).get(condition) or {}
    selected_buy = max(_float(flow.get(f"buy_{outcome}")), 0.0)
    opposite_buy = max(_float(flow.get(f"buy_{1 - outcome}")), 0.0)
    if selected_buy > 0 and opposite_buy / selected_buy >= 0.25:
        return "opposite_flow"
    key = f"{condition}:{outcome}"
    current_shares = max(_float((wstate.get("net_shares_v5") or {}).get(key)), 0.0)
    initial_shares = max(_float(position.get("whale_shares_at_entry")), 1e-9)
    if current_shares <= initial_shares * 0.50:
        return "whale_reduction"
    return ""


def _radar_settle_and_exit(state: dict[str, Any], *, check_resolution: bool) -> tuple[int, int]:
    import polymarket_whale_paper_bot as paper

    remaining = []
    settled = 0
    exited = 0
    for position in state.get("open_positions") or []:
        wallet = str(position.get("wallet") or "")
        wstate = (state.get("wallets") or {}).get(wallet) or {}
        exit_reason = _radar_exit_reason(position, wstate)
        if exit_reason:
            quote = _sell_quote(str(position.get("token_id") or ""), _float(position.get("shares")))
            if quote.get("ok"):
                exit_price = _float(quote.get("exit_price"))
                pnl = _float(position.get("shares")) * exit_price - _float(position.get("bet_usd"))
                _radar_record_result(
                    state, position, pnl, won=pnl > 0, reason=exit_reason,
                    exit_price=exit_price,
                )
                exited += 1
                continue

        if not check_resolution:
            remaining.append(position)
            continue
        market = paper._fetch_market_state(
            gamma_market_id=str(position.get("gamma_market_id") or "")
        )
        winner = paper._resolved_outcome(market) if market else None
        if winner is None:
            remaining.append(position)
            continue
        won = winner == _int(position.get("outcome_index"), -1)
        payout = _float(position.get("shares")) if won else 0.0
        pnl = payout - _float(position.get("bet_usd"))
        _radar_record_result(state, position, pnl, won=won, reason="resolution")
        settled += 1
    state["open_positions"] = remaining
    return settled, exited


def _radar_open_signals(state: dict[str, Any], signals: list[dict[str, Any]]) -> tuple[int, int]:
    import polymarket_whale_paper_bot as paper

    opened = 0
    blocked = 0
    open_keys = {
        (str(p.get("wallet")), str(p.get("condition_id")), _int(p.get("outcome_index")))
        for p in state.get("open_positions") or []
    }
    for signal in signals:
        wallet = str(signal.get("wallet") or "")
        condition = str(signal.get("condition_id") or "")
        outcome = _int(signal.get("outcome_index"), -1)
        key = (wallet, condition, outcome)
        if key in open_keys:
            continue
        age = max(time.time() - _float(signal.get("source_trade_ts"), time.time()), 0.0)
        if age > RADAR_MAX_SIGNAL_AGE:
            blocked += 1
            continue
        market = paper._gamma_market_by_condition(condition)
        if not market:
            blocked += 1
            continue
        quote = paper._paper_buy_quote(market, outcome, RADAR_BET_USD)
        if not quote.get("ok"):
            blocked += 1
            continue
        entry = _float(quote.get("entry_price"))
        expected = _float(
            ((state.get("wallets") or {}).get(wallet) or {}).get("expected_win_rate"),
            0.5,
        )
        max_entry = min(RADAR_MAX_ENTRY, expected - RADAR_MIN_EDGE)
        source_price = _float(signal.get("source_trade_price"))
        drift_ok = source_price <= 0 or _float(quote.get("book_vwap")) <= source_price * 1.03
        if entry <= 0 or entry > max_entry or not drift_ok:
            blocked += 1
            continue
        gamma_id = str(market.get("id") or "")
        token_id = str(quote.get("token_id") or "")
        if not gamma_id or not token_id:
            blocked += 1
            continue
        position = {
            "wallet": wallet,
            "condition_id": condition,
            "gamma_market_id": gamma_id,
            "token_id": token_id,
            "outcome_index": outcome,
            "title": signal.get("title") or market.get("question") or "",
            "signal_level": _int(signal.get("signal_level"), 1),
            "source_trade_ts": _int(signal.get("source_trade_ts")),
            "source_trade_price": source_price,
            "signal_age_seconds": round(age, 3),
            "entry_price": entry,
            "bet_usd": RADAR_BET_USD,
            "shares": RADAR_BET_USD / entry,
            "whale_shares_at_entry": _float(signal.get("net_shares")),
            "expected_win_rate": expected,
            "opened_at": now_kst(),
            "opened_ts": time.time(),
            "execution_source": "clob_ask_vwap",
        }
        state.setdefault("open_positions", []).append(position)
        append_jsonl(RADAR_JOURNAL_FILE, {**position, "event": "radar_opened"})
        open_keys.add(key)
        opened += 1
    return opened, blocked


def run_radar() -> dict[str, Any]:
    """승급 후보를 15초 주기로 실제 호가 기반 paper 추적한다."""
    lock_name = "polymarket_wallet_radar"
    if not try_acquire(lock_name):
        return {"ok": False, "skipped": "already_running"}
    try:
        import polymarket_whale_paper_bot as paper

        watchlist = load_json(WATCHLIST_FILE, default={}) or {}
        state = load_json(RADAR_STATE_FILE, default={}) or {
            "version": 1,
            "wallets": {},
            "open_positions": [],
            "last_resolution_check_ts": 0,
        }
        added = _sync_radar_wallets(state, watchlist)
        paper.MIN_NET_USDC = MIN_DIRECTIONAL_USD
        paper.COPY_SLIPPAGE = BACKTEST_LATENCY_SLIPPAGE
        signals = paper.scan_wallets(
            state,
            include_suspended=False,
            block_market_maker_wallets=False,
            repeat_directional_steps=True,
            parallel_fetch=True,
        )
        resolution_due = time.time() - _float(state.get("last_resolution_check_ts")) >= 60
        settled, exited = _radar_settle_and_exit(state, check_resolution=resolution_due)
        if resolution_due:
            state["last_resolution_check_ts"] = time.time()
        opened, blocked = _radar_open_signals(state, signals)
        state["last_scan"] = {
            "at": now_kst(),
            "tracked_wallets": sum(
                w.get("status") == "active" for w in (state.get("wallets") or {}).values()
            ),
            "added": added,
            "signals": len(signals),
            "opened": opened,
            "blocked": blocked,
            "settled": settled,
            "early_exited": exited,
            "open_positions": len(state.get("open_positions") or []),
        }
        save_json(RADAR_STATE_FILE, state)
        return {"ok": True, **state["last_scan"]}
    finally:
        release(lock_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radar", action="store_true", help="forward paper radar 1회")
    parser.add_argument("--max-profiles", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_radar() if args.radar else run_discovery(args.max_profiles)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        print(f"[PolymarketWalletPipeline] {result}")
    return 0 if result.get("ok") or result.get("skipped") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
