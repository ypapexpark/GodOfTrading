#!/usr/bin/env python3
"""고래 포지션 증감 추종의 paired backtest.

동일한 최초 진입 표본에서 다음 정책을 비교한다.
  hold: 최초 $20 진입 후 만기 보유
  add_only: 고래 추가매수만 비례 추종
  reduce_only: 고래 축소만 비례 추종
  full_resize: 추가·축소 모두 비례 추종
  full_capped_1.1x/1.25x/1.5x/2x: 비례추종의 최대 수량 제한
  full_capped_2x: full_resize를 최초 수량 2배로 제한

반대 outcome 플립은 기존 별도 분석에서 손실이 확인됐으므로 이번 비교에서 제외한다.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import polymarket_whale_paper_bot as paper  # noqa: E402

START_TS = int(datetime.fromisoformat("2026-06-01T00:00:00+00:00").timestamp())
# 분석 도중 새 체결이 들어와 offset 페이지가 밀리지 않도록 스냅샷 종료시각 고정.
END_TS = 1_783_894_000
BET = 20.0
MIN_NET = 1000.0
SLIPPAGE = 0.03
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def _fetch_wallet(wallet: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    window_seconds = 2 * 24 * 3600
    window_start = START_TS
    while window_start < END_TS:
        window_end = min(window_start + window_seconds, END_TS)
        for offset in range(0, 10_000, 500):
            response = requests.get(
                f"{DATA_API}/activity",
                params={
                    "user": wallet,
                    "limit": 500,
                    "offset": offset,
                    "start": window_start,
                    "end": window_end,
                    "sortDirection": "ASC",
                },
                timeout=30,
            )
            if response.status_code == 400 and offset > 0:
                break
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list) or not page:
                break
            rows.extend(r for r in page if r.get("type") == "TRADE")
            if len(page) < 500:
                break
        window_start = window_end
    unique: dict[tuple, dict] = {}
    for row in rows:
        key = (
            row.get("transactionHash"), row.get("asset"), row.get("side"),
            row.get("timestamp"), row.get("size"), row.get("price"),
        )
        unique[key] = row
    return sorted(unique.values(), key=lambda r: int(r.get("timestamp") or 0))


def _market(condition_id: str) -> dict[str, Any] | None:
    for attempt in range(4):
        try:
            response = requests.get(
                f"{CLOB_API}/markets/{condition_id}",
                timeout=25,
            )
            response.raise_for_status()
            row = response.json()
            if str(row.get("condition_id") or "").lower() != condition_id.lower():
                raise ValueError("CLOB condition mismatch")
            tokens = row.get("tokens") or []
            winner = next(
                (idx for idx, token in enumerate(tokens) if token.get("winner") is True),
                None,
            )
            return {
                "closed": bool(row.get("closed")),
                "outcomePrices": json.dumps(
                    [float(token.get("price") or 0) for token in tokens]
                ),
                "winner_index": winner,
            }
        except Exception:
            pass
        if attempt < 3:
            time.sleep(0.35 * (attempt + 1))
    return None


def _trade_price(row: dict, buying: bool) -> float:
    raw = float(row.get("price") or 0)
    return min(raw + SLIPPAGE, 0.999) if buying else max(raw - SLIPPAGE, 0.001)


def _new_policy(entry_price: float) -> dict[str, float]:
    shares = BET / entry_price
    return {
        "shares": shares,
        "cash": -BET,
        "peak": BET,
        "entry_shares": shares,
    }


def _apply(policy: dict[str, float], target: float, price: float) -> None:
    delta = target - policy["shares"]
    if abs(delta) < 1e-12:
        return
    policy["cash"] -= delta * price
    policy["shares"] = target
    policy["peak"] = max(policy["peak"], max(-policy["cash"], 0.0))


def _simulate_wallet(wallet: str, rows: list[dict]) -> list[dict[str, Any]]:
    net_usdc: dict[tuple[str, int], float] = defaultdict(float)
    net_shares: dict[tuple[str, int], float] = defaultdict(float)
    chosen_condition: set[str] = set()
    positions: dict[tuple[str, int], dict[str, Any]] = {}

    for row in rows:
        condition = str(row.get("conditionId") or "")
        outcome = row.get("outcomeIndex")
        if not condition or outcome is None:
            continue
        key = (condition, int(outcome))
        side = str(row.get("side") or "").upper()
        sign = 1.0 if side == "BUY" else -1.0
        size = float(row.get("size") or 0)
        usdc = float(row.get("usdcSize") or size * float(row.get("price") or 0))
        net_usdc[key] += sign * usdc
        net_shares[key] = max(net_shares[key] + sign * size, 0.0)

        pos = positions.get(key)
        if pos is None:
            if condition in chosen_condition or net_usdc[key] < MIN_NET or net_shares[key] <= 0:
                continue
            entry_price = _trade_price(row, True)
            base = _new_policy(entry_price)
            positions[key] = {
                "wallet": wallet,
                "condition_id": condition,
                "outcome_index": int(outcome),
                "title": row.get("title") or "",
                "entry_ts": int(row.get("timestamp") or 0),
                "scale": base["shares"] / net_shares[key],
                "hold": dict(base),
                "add_only": dict(base),
                "reduce_only": dict(base),
                "full_resize": dict(base),
                "full_capped_1_1x": dict(base),
                "full_capped_1_25x": dict(base),
                "full_capped_1_5x": dict(base),
                "full_capped_2x": dict(base),
            }
            chosen_condition.add(condition)
            continue

        scale = float(pos["scale"])
        target = max(net_shares[key] * scale, 0.0)
        price = _trade_price(row, side == "BUY")
        if side == "BUY":
            _apply(pos["add_only"], max(pos["add_only"]["shares"], target), price)
        else:
            _apply(pos["reduce_only"], min(pos["reduce_only"]["shares"], target), price)
        _apply(pos["full_resize"], target, price)
        for name, multiple in [
            ("full_capped_1_1x", 1.1),
            ("full_capped_1_25x", 1.25),
            ("full_capped_1_5x", 1.5),
            ("full_capped_2x", 2.0),
        ]:
            capped = min(target, pos[name]["entry_shares"] * multiple)
            _apply(pos[name], capped, price)
    return list(positions.values())


def _summary(trades: list[dict], policy: str) -> dict[str, float]:
    pnl = [float(t[policy]["pnl"]) for t in trades]
    peak = [float(t[policy]["peak"]) for t in trades]
    return {
        "n": len(trades),
        "pnl": sum(pnl),
        "avg_pnl": statistics.mean(pnl) if pnl else 0.0,
        "median_pnl": statistics.median(pnl) if pnl else 0.0,
        "wins": sum(v > 0 for v in pnl),
        "capital": sum(peak),
        "avg_peak": statistics.mean(peak) if peak else 0.0,
        "max_peak": max(peak, default=0.0),
        "roi": sum(pnl) / sum(peak) if sum(peak) else 0.0,
    }


def main() -> int:
    wallets = [w["wallet"] for w in paper._load_config().get("whales") or []]
    activities: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = {pool.submit(_fetch_wallet, wallet): wallet for wallet in wallets}
        for future in as_completed(futures):
            activities[futures[future]] = future.result()

    positions: list[dict] = []
    for wallet, rows in activities.items():
        positions.extend(_simulate_wallet(wallet, rows))

    markets: dict[str, dict | None] = {}
    conditions = sorted({p["condition_id"] for p in positions})
    # CLOB 단일 condition endpoint로 조회해 Gamma 목록 필터의 빈 응답 누락을 피한다.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_market, condition): condition for condition in conditions}
        for future in as_completed(futures):
            markets[futures[future]] = future.result()

    resolved: list[dict] = []
    policies = [
        "hold", "add_only", "reduce_only", "full_resize",
        "full_capped_1_1x", "full_capped_1_25x", "full_capped_1_5x",
        "full_capped_2x",
    ]
    for pos in positions:
        market = markets.get(pos["condition_id"]) or {}
        winner = market.get("winner_index")
        if winner is None:
            winner = paper._resolved_outcome(market)
        if winner is None:
            continue
        won = winner == pos["outcome_index"]
        for name in policies:
            state = pos[name]
            payout = state["shares"] if won else 0.0
            state["pnl"] = state["cash"] + payout
        resolved.append(pos)

    summaries = {name: _summary(resolved, name) for name in policies}
    hold_pnl = [t["hold"]["pnl"] for t in resolved]
    paired = {}
    for name in policies[1:]:
        diff = [t[name]["pnl"] - t["hold"]["pnl"] for t in resolved]
        paired[name] = {
            "delta_pnl": sum(diff),
            "avg_delta": statistics.mean(diff) if diff else 0.0,
            "median_delta": statistics.median(diff) if diff else 0.0,
            "better": sum(v > 0.005 for v in diff),
            "worse": sum(v < -0.005 for v in diff),
            "unchanged": sum(abs(v) <= 0.005 for v in diff),
        }
    top_hold = max(hold_pnl, default=0.0)
    sensitivity = {
        name: sum(t[name]["pnl"] for t in resolved) - max(
            (t[name]["pnl"] for t in resolved), default=0.0
        )
        for name in policies
    }
    same_direction: dict[tuple[str, int], list[dict]] = defaultdict(list)
    by_market_outcomes: dict[str, set[int]] = defaultdict(set)
    for trade in resolved:
        key = (trade["condition_id"], int(trade["outcome_index"]))
        same_direction[key].append(trade)
        by_market_outcomes[trade["condition_id"]].add(int(trade["outcome_index"]))

    single_groups = [rs for rs in same_direction.values() if len({r["wallet"] for r in rs}) == 1]
    consensus_groups = [rs for rs in same_direction.values() if len({r["wallet"] for r in rs}) >= 2]

    def _consensus_summary(groups: list[list[dict]]) -> dict[str, float]:
        first_pnl = []
        all_pnl = []
        marginal_pnl = []
        all_ticket_pnl = []
        entry_prices = []
        tickets = 0
        extra_tickets = 0
        for group in groups:
            ordered = sorted(group, key=lambda r: int(r["entry_ts"]))
            first = float(ordered[0]["hold"]["pnl"])
            rest = sum(float(r["hold"]["pnl"]) for r in ordered[1:])
            ticket_pnl = [float(r["hold"]["pnl"]) for r in ordered]
            first_pnl.append(first)
            marginal_pnl.append(rest)
            all_pnl.append(first + rest)
            all_ticket_pnl.extend(ticket_pnl)
            entry_prices.extend(
                BET / max(float(r["hold"]["entry_shares"]), 1e-9)
                for r in ordered
            )
            tickets += len(ordered)
            extra_tickets += max(len(ordered) - 1, 0)
        best_extra = max(marginal_pnl, default=0.0)
        return {
            "groups": len(groups),
            "winning_groups": sum(v > 0 for v in first_pnl),
            "win_rate": sum(v > 0 for v in first_pnl) / len(groups) if groups else 0.0,
            "tickets": tickets,
            "first_ticket_pnl": sum(first_pnl),
            "follow_all_pnl": sum(all_pnl),
            "extra_tickets": extra_tickets,
            "extra_ticket_pnl": sum(marginal_pnl),
            "extra_ticket_roi": (
                sum(marginal_pnl) / (BET * extra_tickets) if extra_tickets else 0.0
            ),
            "best_group_extra_pnl": best_extra,
            "extra_pnl_without_best_group": sum(marginal_pnl) - best_extra,
            "gross_profit": sum(v for v in all_ticket_pnl if v > 0),
            "gross_loss": sum(v for v in all_ticket_pnl if v < 0),
            "avg_win_pnl": statistics.mean([v for v in all_ticket_pnl if v > 0]) if any(v > 0 for v in all_ticket_pnl) else 0.0,
            "avg_loss_pnl": statistics.mean([v for v in all_ticket_pnl if v < 0]) if any(v < 0 for v in all_ticket_pnl) else 0.0,
            "avg_entry_price": statistics.mean(entry_prices) if entry_prices else 0.0,
        }

    exact_counts: dict[str, list[list[dict]]] = {
        "one": [], "two": [], "three": [], "four_plus": [],
    }
    for group in same_direction.values():
        count = len({r["wallet"] for r in group})
        bucket = "one" if count == 1 else "two" if count == 2 else "three" if count == 3 else "four_plus"
        exact_counts[bucket].append(group)

    marginal_by_ticket: dict[str, dict[str, float]] = {}
    for ordinal in range(1, 6):
        values = []
        for group in same_direction.values():
            ordered = sorted(group, key=lambda r: int(r["entry_ts"]))
            if len(ordered) >= ordinal:
                values.append(float(ordered[ordinal - 1]["hold"]["pnl"]))
        marginal_by_ticket[str(ordinal)] = {
            "n": len(values),
            "wins": sum(v > 0 for v in values),
            "win_rate": sum(v > 0 for v in values) / len(values) if values else 0.0,
            "pnl": sum(values),
            "roi": sum(values) / (BET * len(values)) if values else 0.0,
            "avg_pnl": statistics.mean(values) if values else 0.0,
        }

    def _skip_first_take_second_third(groups: list[list[dict]]) -> dict[str, float]:
        values: list[float] = []
        for group in groups:
            ordered = sorted(group, key=lambda r: int(r["entry_ts"]))
            values.extend(float(r["hold"]["pnl"]) for r in ordered[1:3])
        return {
            "tickets": len(values),
            "wins": sum(v > 0 for v in values),
            "pnl": sum(values),
            "roi": sum(values) / (BET * len(values)) if values else 0.0,
        }

    production_groups = [
        group for group in same_direction.values()
        if len({r["wallet"] for r in group}) >= 2
    ]
    conflict_free_groups = [
        group for group in production_groups
        if len(by_market_outcomes[group[0]["condition_id"]]) == 1
    ]

    consensus = {
        "single_whale_same_direction": _consensus_summary(single_groups),
        "two_plus_whales_same_direction": _consensus_summary(consensus_groups),
        "production_skip_first_trade_second_and_third": (
            _skip_first_take_second_third(production_groups)
        ),
        "production_conflict_free": (
            _skip_first_take_second_third(conflict_free_groups)
        ),
        "exact_unique_whale_count": {
            key: _consensus_summary(groups) for key, groups in exact_counts.items()
        },
        "marginal_ticket_by_arrival_order": marginal_by_ticket,
        "markets_with_opposite_direction_whales": sum(
            len(outcomes) >= 2 for outcomes in by_market_outcomes.values()
        ),
    }
    print(json.dumps({
        "start_ts": START_TS,
        "end_ts": END_TS,
        "wallet_activity_rows": {w: len(r) for w, r in activities.items()},
        "candidate_positions": len(positions),
        "resolved_market_payloads": sum(m is not None for m in markets.values()),
        "market_payload_misses": sum(m is None for m in markets.values()),
        "resolved": len(resolved),
        "summaries": summaries,
        "paired_vs_hold": paired,
        "pnl_without_each_policy_best_trade": sensitivity,
        "hold_best_trade": top_hold,
        "whale_consensus": consensus,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
