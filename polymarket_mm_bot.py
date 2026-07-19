#!/usr/bin/env python3
"""Polymarket maker-mirror V2 소액 market-making 봇.

기본 모드는 실제 주문이 없는 queue-aware paper simulation이다. 관측된 maker형
고래가 활발한 시장을 후보 점수에 반영하고, pUSD split으로 만든 YES/NO 재고를
바탕으로 양쪽 토큰의 BUY/SELL 호가를 함께 제공한다. 체결 뒤에는 재고 skew,
complete-set merge, 비용 비교형 재균형으로 방향 노출을 줄인다.

실주문은 ``POLYMARKET_MM_LIVE_ENABLED=true``만으로도 켜지지 않는다. 충분한 paper
표본으로 state의 promotion gate가 통과하고 기존 Polymarket live 플래그도 켜져 있어야
한다. 현재 구현의 기본 운용은 paper-only다.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import html
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

from bot_util import append_jsonl, env_float, env_int, load_json, now_kst, save_json
from polymarket_mm_exec import mm_live_enabled
from process_lock import release, try_acquire

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

STATE_FILE = ROOT / "polymarket_mm_state.json"
JOURNAL_FILE = ROOT / "polymarket_mm_journal.jsonl"
STREAM_STATE_FILE = ROOT / "polymarket_mm_stream_state.json"
DISCOVERY_SNAPSHOT_FILE = ROOT / "polymarket_mm_candidates.json"
WHALE_PAPER_STATE_FILE = ROOT / "polymarket_whale_paper_state.json"

PAPER_INITIAL_CASH = env_float("POLYMARKET_MM_PAPER_CASH", 100.0)
STRATEGY_VERSION = 2
# $100 안팎의 시드로 10개 시장에 얇게 퍼지면 네 방향 호가와 재고를 감당할 수 없다.
# discovery는 전체 시장을 보되 최소 5주 재고 기준 실제 호가는 기본 5개에 집중한다.
MAX_MARKETS = env_int("POLYMARKET_MM_MAX_MARKETS", 5)
CANDIDATE_POOL_SIZE = max(MAX_MARKETS, env_int("POLYMARKET_MM_CANDIDATE_POOL", MAX_MARKETS * 3))
UNIVERSE_LIMIT = min(max(env_int("POLYMARKET_MM_UNIVERSE_LIMIT", 10_000), 100), 20_000)
DISCOVERY_WORKER_PAUSE = env_float("POLYMARKET_MM_DISCOVERY_PAUSE", 15.0)
DISCOVERY_STALE_SECONDS = env_float("POLYMARKET_MM_DISCOVERY_STALE", 5 * 60.0)
DAEMON_INTERVAL = env_float("POLYMARKET_MM_DAEMON_INTERVAL", 1.0)
STREAM_STALE_SECONDS = env_float("POLYMARKET_MM_STREAM_STALE_SECONDS", 5.0)
REPORT_INTERVAL_SECONDS = env_int("POLYMARKET_MM_REPORT_INTERVAL", 30 * 60)
MIN_LIQUIDITY = env_float("POLYMARKET_MM_MIN_LIQUIDITY", 5_000.0)
MIN_VOLUME_24H = env_float("POLYMARKET_MM_MIN_VOLUME_24H", 2_000.0)
MIN_HOURS_TO_END = env_float("POLYMARKET_MM_MIN_HOURS_TO_END", 12.0)
MAX_HOURS_TO_END = env_float("POLYMARKET_MM_MAX_HOURS_TO_END", 30 * 24.0)
MIN_PROBABILITY = env_float("POLYMARKET_MM_MIN_PROBABILITY", 0.10)
MAX_PROBABILITY = env_float("POLYMARKET_MM_MAX_PROBABILITY", 0.90)
MIN_BOOK_SPREAD = env_float("POLYMARKET_MM_MIN_BOOK_SPREAD", 0.01)
MAX_BOOK_SPREAD = env_float("POLYMARKET_MM_MAX_BOOK_SPREAD", 0.08)
MAX_ONE_HOUR_MOVE = env_float("POLYMARKET_MM_MAX_ONE_HOUR_MOVE", 0.04)
MAX_ONE_DAY_MOVE = env_float("POLYMARKET_MM_MAX_ONE_DAY_MOVE", 0.12)

QUOTE_SHARES = env_float("POLYMARKET_MM_QUOTE_SHARES", 5.0)
TARGET_INVENTORY_SHARES = env_float(
    "POLYMARKET_MM_TARGET_INVENTORY_SHARES", QUOTE_SHARES
)
MAX_PAIR_COST = env_float("POLYMARKET_MM_MAX_PAIR_COST", 0.98)
MIN_LOCKED_PAIR_EDGE = env_float("POLYMARKET_MM_MIN_PAIR_EDGE", 0.02)
MAX_UNMATCHED_USD = env_float("POLYMARKET_MM_MAX_UNMATCHED_USD", 8.0)
MAX_TOTAL_COMMITTED = env_float("POLYMARKET_MM_MAX_TOTAL_COMMITTED", 50.0)
MAX_INVENTORY_AGE = env_int("POLYMARKET_MM_MAX_INVENTORY_AGE", 15 * 60)
MAX_ADVERSE_MOVE = env_float("POLYMARKET_MM_MAX_ADVERSE_MOVE", 0.05)
TAKER_FEE_RATE = env_float("POLYMARKET_MM_TAKER_FEE_RATE", 0.05)
QUOTE_LIFETIME_SECONDS = env_int("POLYMARKET_MM_QUOTE_LIFETIME", 15 * 60)
REQUOTE_TICKS = max(env_int("POLYMARKET_MM_REQUOTE_TICKS", 2), 1)
BASE_HALF_SPREAD = env_float("POLYMARKET_MM_BASE_HALF_SPREAD", 0.01)
INVENTORY_SKEW_PER_SHARE = env_float("POLYMARKET_MM_INVENTORY_SKEW", 0.0015)
FLOW_SKEW_MAX = env_float("POLYMARKET_MM_FLOW_SKEW_MAX", 0.015)
TOXIC_FLOW_THRESHOLD = env_float("POLYMARKET_MM_TOXIC_FLOW_THRESHOLD", 0.72)
FLOW_DECAY_SECONDS = env_float("POLYMARKET_MM_FLOW_DECAY_SECONDS", 90.0)
PAPER_QUEUE_FRACTION = env_float("POLYMARKET_MM_PAPER_QUEUE_FRACTION", 1.0)

PROMOTION_MIN_DAYS = env_int("POLYMARKET_MM_PROMOTION_MIN_DAYS", 7)
PROMOTION_MIN_FILLS = env_int("POLYMARKET_MM_PROMOTION_MIN_FILLS", 100)
PROMOTION_MIN_PAIRS = env_int("POLYMARKET_MM_PROMOTION_MIN_PAIRS", 30)
PROMOTION_MIN_REALIZED = env_float("POLYMARKET_MM_PROMOTION_MIN_REALIZED", 2.0)
PROMOTION_MAX_DRAWDOWN = env_float("POLYMARKET_MM_PROMOTION_MAX_DRAWDOWN", 0.05)
PROMOTION_MIN_SELL_FILLS = env_int("POLYMARKET_MM_PROMOTION_MIN_SELL_FILLS", 20)
PROMOTION_MAX_REBALANCE_LOSS_SHARE = env_float(
    "POLYMARKET_MM_PROMOTION_MAX_REBALANCE_LOSS_SHARE", 0.40
)


def _get_json(url: str, params: dict[str, Any] | None = None,
              timeout: int = 15) -> Any:
    response = requests.get(url, params=params or {}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _post_json(url: str, payload: Any, timeout: int = 20) -> Any:
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _atomic_save_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    save_json(temporary, payload)
    os.replace(temporary, path)


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


def _jsonish(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value if value is not None else default


def _floor_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    steps = math.floor((value + 1e-12) / tick)
    decimals = max(0, int(round(-math.log10(tick))) + 1)
    return round(steps * tick, decimals)


def _ceil_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    steps = math.ceil((value - 1e-12) / tick)
    decimals = max(0, int(round(-math.log10(tick))) + 1)
    return round(steps * tick, decimals)


def _sorted_levels(book: dict[str, Any], side: str) -> list[tuple[float, float]]:
    levels = []
    for row in book.get(side) or []:
        price = _float(row.get("price"))
        size = _float(row.get("size"))
        if 0 < price < 1 and size > 0:
            levels.append((price, size))
    return sorted(levels, key=lambda item: item[0], reverse=side == "bids")


def analyze_book(book: dict[str, Any]) -> dict[str, Any]:
    bids = _sorted_levels(book, "bids")
    asks = _sorted_levels(book, "asks")
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    tick = _float(book.get("tick_size"), 0.01)
    return {
        "token_id": str(book.get("asset_id") or ""),
        "condition_id": str(book.get("market") or ""),
        "tick_size": tick,
        "min_order_size": _float(book.get("min_order_size"), 5.0),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": best_ask - best_bid if best_bid is not None and best_ask is not None else None,
        "mid": (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None,
        "bids": bids,
        "asks": asks,
    }


def _queue_at(book: dict[str, Any], price: float, side: str = "BUY") -> float:
    levels = book.get("bids" if str(side).upper() == "BUY" else "asks") or []
    return sum(size for level_price, size in levels if abs(level_price - price) < 1e-9)


def _top_size(book: dict[str, Any], side: str) -> float:
    levels = book.get("bids" if str(side).upper() == "BUY" else "asks") or []
    return _float(levels[0][1]) if levels else 0.0


def _microprice(book: dict[str, Any]) -> float:
    """Top-of-book imbalance를 반영한 단기 공정가."""
    bid = _float(book.get("best_bid"))
    ask = _float(book.get("best_ask"))
    bid_size = _top_size(book, "BUY")
    ask_size = _top_size(book, "SELL")
    if not (0 < bid < ask < 1) or bid_size + ask_size <= 0:
        return _float(book.get("mid"), (bid + ask) / 2)
    return (ask * bid_size + bid * ask_size) / (bid_size + ask_size)


def _market_end_hours(row: dict[str, Any], now_ts: float | None = None) -> float:
    raw = row.get("endDate") or row.get("end_date")
    if not raw:
        return 999999.0
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return (parsed.timestamp() - (now_ts or time.time())) / 3600
    except Exception:
        return 999999.0


def _market_start_hours(row: dict[str, Any], now_ts: float | None = None) -> float | None:
    raw = row.get("gameStartTime") or row.get("eventStartTime") or row.get("startDate")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return (parsed.timestamp() - (now_ts or time.time())) / 3600
    except Exception:
        return None


def _market_tokens(row: dict[str, Any]) -> list[str]:
    return [str(v) for v in (_jsonish(row.get("clobTokenIds"), []) or [])][:2]


def _market_prices(row: dict[str, Any]) -> list[float]:
    return [_float(v) for v in (_jsonish(row.get("outcomePrices"), []) or [])][:2]


def cheap_market_filter(row: dict[str, Any]) -> tuple[bool, str]:
    prices = _market_prices(row)
    tokens = _market_tokens(row)
    end_hours = _market_end_hours(row)
    if not row.get("acceptingOrders") or not row.get("enableOrderBook"):
        return False, "orders_disabled"
    if len(tokens) != 2 or len(prices) != 2:
        return False, "not_binary"
    if not (MIN_PROBABILITY <= prices[0] <= MAX_PROBABILITY):
        return False, "extreme_probability"
    if _float(row.get("liquidityNum") or row.get("liquidity")) < MIN_LIQUIDITY:
        return False, "low_liquidity"
    if _float(row.get("volume24hr")) < MIN_VOLUME_24H:
        return False, "low_volume"
    if not (MIN_HOURS_TO_END <= end_hours <= MAX_HOURS_TO_END):
        return False, "bad_time_to_resolution"
    if abs(_float(row.get("oneHourPriceChange"))) > MAX_ONE_HOUR_MOVE:
        return False, "toxic_hourly_move"
    if abs(_float(row.get("oneDayPriceChange"))) > MAX_ONE_DAY_MOVE:
        return False, "toxic_daily_move"
    start_hours = _market_start_hours(row)
    is_sports = bool(
        row.get("sportsMarketType") or row.get("gameStartTime") or row.get("gameId")
    )
    if is_sports and start_hours is not None and start_hours <= 0:
        return False, "live_sports"
    return True, ""


def fetch_books_batch(tokens: list[str]) -> dict[str, dict[str, Any]]:
    """공식 POST /books 배치 API로 최대 500개 토큰씩 가져온다."""
    unique = list(dict.fromkeys(str(token) for token in tokens if token))
    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(unique), 500):
        chunk = unique[start:start + 500]
        rows = _post_json(f"{CLOB_API}/books", [
            {"token_id": token} for token in chunk
        ])
        if not isinstance(rows, list):
            continue
        for row in rows:
            analyzed = analyze_book(row)
            token = str(analyzed.get("token_id") or "")
            if token:
                result[token] = analyzed
    return result


def fetch_market_books(tokens: list[str]) -> list[dict[str, Any]]:
    by_token = fetch_books_batch(tokens)
    return [by_token[str(token)] for token in tokens if str(token) in by_token]


def maker_market_heat() -> dict[str, dict[str, Any]]:
    """고래 파이프라인이 MM형으로 분류한 지갑의 양방향 시장 활동."""
    payload = load_json(WHALE_PAPER_STATE_FILE, default={})
    wallets = payload.get("wallets") if isinstance(payload, dict) else {}
    heat: dict[str, dict[str, Any]] = {}
    for wallet, wstate in (wallets or {}).items():
        classification = wstate.get("classification_v2") or {}
        if not classification.get("market_maker_like"):
            continue
        for condition, flow in (wstate.get("market_flow_v2") or {}).items():
            buy0 = max(_float(flow.get("buy_0")), 0.0)
            buy1 = max(_float(flow.get("buy_1")), 0.0)
            if min(buy0, buy1) <= 0:
                continue
            row = heat.setdefault(str(condition), {
                "wallets": set(), "two_sided_usdc": 0.0,
                "total_usdc": 0.0, "updated_ts": 0.0,
            })
            row["wallets"].add(str(wallet))
            row["two_sided_usdc"] += 2 * min(buy0, buy1)
            row["total_usdc"] += buy0 + buy1
            row["updated_ts"] = max(_float(row.get("updated_ts")), _float(flow.get("updated_ts")))
    return {
        condition: {
            **row,
            "wallets": sorted(row["wallets"]),
            "balance_ratio": (
                _float(row.get("two_sided_usdc")) / max(_float(row.get("total_usdc")), 1e-9)
            ),
        }
        for condition, row in heat.items()
    }


def deep_market_candidate(
    row: dict[str, Any], books: list[dict[str, Any]],
    maker_heat: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if len(books) != 2 or any(book.get("mid") is None for book in books):
        return None
    # 최소 1틱을 개선해도 maker로 남으려면 현재 스프레드가 적어도 2틱이어야 한다.
    if any(
        _float(book.get("spread"))
        < max(MIN_BOOK_SPREAD, 2 * _float(book.get("tick_size"), 0.01)) - 1e-12
        for book in books
    ):
        return None
    if any(_float(book.get("spread")) > MAX_BOOK_SPREAD for book in books):
        return None
    if any(_float(book.get("min_order_size"), 999) > QUOTE_SHARES for book in books):
        return None
    mid_sum = _float(books[0].get("mid")) + _float(books[1].get("mid"))
    if not (0.97 <= mid_sum <= 1.03):
        return None
    reward_daily = sum(
        _float(reward.get("rewardsDailyRate")) for reward in row.get("clobRewards") or []
    )
    reward_min_size = _float(row.get("rewardsMinSize"))
    reward_eligible = reward_daily > 0 and reward_min_size <= QUOTE_SHARES
    spread_score = min(_float(books[0].get("spread")), _float(books[1].get("spread")))
    condition_id = str(row.get("conditionId") or "")
    observed = maker_heat or {}
    maker_wallet_count = len(observed.get("wallets") or [])
    maker_flow = _float(observed.get("two_sided_usdc"))
    maker_bonus = min(math.log1p(maker_flow) / 8, 1.5) + min(maker_wallet_count, 3) * 0.25
    # 넓은 spread를 무한정 보상하지 않는다. 5c를 넘는 폭은 대개 독성/정체 위험이다.
    usable_spread = min(spread_score, 0.05)
    score = (
        4 * usable_spread
        + math.log1p(_float(row.get("volume24hr"))) / 10
        + math.log1p(_float(row.get("liquidityNum") or row.get("liquidity"))) / 20
        + (math.log1p(reward_daily) / 20 if reward_eligible else 0.0)
        + maker_bonus
        - 5 * abs(_float(row.get("oneHourPriceChange")))
        - 2 * abs(_float(row.get("oneDayPriceChange")))
    )
    return {
        "condition_id": condition_id,
        "gamma_market_id": str(row.get("id") or ""),
        "title": str(row.get("question") or ""),
        "end_date": row.get("endDate"),
        "end_hours": _market_end_hours(row),
        "tokens": _market_tokens(row),
        "outcomes": _jsonish(row.get("outcomes"), ["Yes", "No"]),
        "neg_risk": bool(row.get("negRisk")),
        "liquidity": _float(row.get("liquidityNum") or row.get("liquidity")),
        "volume_24h": _float(row.get("volume24hr")),
        "rewards_min_size": reward_min_size,
        "rewards_max_spread": _float(row.get("rewardsMaxSpread")),
        "rewards_daily_rate": reward_daily,
        "reward_eligible_at_quote_size": reward_eligible,
        "fees_enabled": bool(row.get("feesEnabled")),
        "sports_market_type": row.get("sportsMarketType"),
        "game_start_time": row.get("gameStartTime") or row.get("eventStartTime"),
        "maker_wallet_count": maker_wallet_count,
        "maker_wallets": list(observed.get("wallets") or []),
        "maker_flow_usdc": maker_flow,
        "maker_balance_ratio": _float(observed.get("balance_ratio")),
        "tick_sizes": [_float(book.get("tick_size"), 0.01) for book in books],
        "min_order_sizes": [_float(book.get("min_order_size"), 5.0) for book in books],
        "books": books,
        "score": score,
        "selected_at": now_kst(),
    }


def fetch_market_universe() -> tuple[list[dict[str, Any]], bool]:
    """유동성 기준을 넘는 미종료 시장 전체를 안정적인 keyset으로 순회한다."""
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    cursor = ""
    has_more = False
    viable_exhausted = False
    while len(rows) < UNIVERSE_LIMIT:
        params: dict[str, Any] = {
            "closed": "false", "limit": min(100, UNIVERSE_LIMIT - len(rows)),
            "liquidity_num_min": MIN_LIQUIDITY,
            "order": "volume24hr", "ascending": "false",
        }
        if cursor:
            params["after_cursor"] = cursor
        payload = _get_json(f"{GAMMA_API}/markets/keyset", params)
        page = payload.get("markets") if isinstance(payload, dict) else []
        if not isinstance(page, list) or not page:
            break
        for row in page:
            market_id = str(row.get("id") or row.get("conditionId") or "")
            if not market_id or market_id in seen_ids:
                continue
            seen_ids.add(market_id)
            rows.append(row)
        next_cursor = str(payload.get("next_cursor") or "")
        has_more = bool(next_cursor)
        # volume24hr 내림차순이므로 마지막 행이 기준 아래면 이후 페이지는 전부
        # 거래량 필터에서 탈락한다. '모든 활성시장' 숫자보다 모든 경제적 후보를
        # 빠짐없이 훑는 것이 목적이므로 여기서 안전하게 중단한다.
        if page and _float(page[-1].get("volume24hr")) < MIN_VOLUME_24H:
            viable_exhausted = True
            break
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return rows, bool(has_more and len(rows) >= UNIVERSE_LIMIT and not viable_exhausted)


def discover_markets() -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows, truncated = fetch_market_universe()
    maker_heat_by_condition = maker_market_heat()
    reasons: dict[str, int] = defaultdict(int)
    eligible_rows = []
    for row in rows:
        ok, reason = cheap_market_filter(row)
        if not ok:
            reasons[reason] += 1
            continue
        eligible_rows.append(row)

    token_ids = [token for row in eligible_rows for token in _market_tokens(row)]
    try:
        books_by_token = fetch_books_batch(token_ids)
    except Exception:
        books_by_token = {}
        reasons["book_batch_error"] += len(eligible_rows)

    candidates = []
    for row in eligible_rows:
        tokens = _market_tokens(row)
        books = [books_by_token[token] for token in tokens if token in books_by_token]
        if len(books) != 2:
            reasons["book_error"] += 1
            continue
        candidate = deep_market_candidate(
            row, books, maker_heat_by_condition.get(str(row.get("conditionId") or "")),
        )
        if candidate is None:
            reasons["book_quality"] += 1
            continue
        candidates.append(candidate)
    candidates.sort(key=lambda item: item["score"], reverse=True)
    reasons["_universe_scanned"] = len(rows)
    reasons["_metadata_eligible"] = len(eligible_rows)
    reasons["_books_received"] = len(books_by_token)
    reasons["_universe_truncated"] = int(truncated)
    return candidates[:CANDIDATE_POOL_SIZE], dict(reasons)


def write_discovery_snapshot() -> dict[str, Any]:
    """느린 전체시장 탐색 결과를 main 1초 루프와 분리해 저장한다."""
    if not try_acquire("polymarket_mm_discovery"):
        return {"ok": False, "skipped": "already_running"}
    started = time.monotonic()
    try:
        try:
            candidates, rejects = discover_markets()
            slim_candidates = [
                {key: value for key, value in candidate.items() if key != "books"}
                for candidate in candidates
            ]
            payload = {
                "ok": True,
                "generated_at": now_kst(),
                "generated_ts": time.time(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "candidates": slim_candidates,
                "rejections": rejects,
            }
            _atomic_save_json(DISCOVERY_SNAPSHOT_FILE, payload)
            return payload
        except Exception as exc:
            payload = {
                "ok": False,
                "generated_at": now_kst(),
                "generated_ts": time.time(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "error": str(exc)[:500],
            }
            # 정상 후보 snapshot은 일시 장애 한 번으로 덮지 않는다.
            return payload
    finally:
        release("polymarket_mm_discovery")


def apply_discovery_snapshot(state: dict[str, Any]) -> tuple[bool, str]:
    snapshot = load_json(DISCOVERY_SNAPSHOT_FILE, default={})
    if not isinstance(snapshot, dict) or not snapshot.get("ok"):
        return False, str(snapshot.get("error") or "discovery snapshot unavailable")[:300]
    generated_ts = _float(snapshot.get("generated_ts"))
    if generated_ts <= _float(state.get("last_discovery_ts")):
        lag = max(time.time() - generated_ts, 0.0) if generated_ts else 999999.0
        return False, "" if lag <= DISCOVERY_STALE_SECONDS else f"discovery stale {lag:.1f}s"
    candidates = snapshot.get("candidates") if isinstance(snapshot.get("candidates"), list) else []
    rejects = snapshot.get("rejections") if isinstance(snapshot.get("rejections"), dict) else {}
    pool_by_condition = {
        str(candidate.get("condition_id") or ""): candidate
        for candidate in candidates if candidate.get("condition_id")
    }
    stable_candidates = candidates[:max(MAX_MARKETS * 2, MAX_MARKETS)]
    stable_ids = {
        str(candidate.get("condition_id") or "") for candidate in stable_candidates
    } | {
        str(candidate.get("condition_id") or "")
        for candidate in candidates if _int(candidate.get("maker_wallet_count")) > 0
    }
    # queue 보존을 위해 상위 2배 안정권 또는 관측 MM 고래 시장만 기존 선택을 유지한다.
    chosen = []
    chosen_ids = set()
    for condition in state.get("selected_conditions") or []:
        candidate = pool_by_condition.get(str(condition))
        if candidate and str(condition) in stable_ids and len(chosen) < MAX_MARKETS:
            chosen.append(candidate)
            chosen_ids.add(str(condition))
    maker_cohort = sorted(
        (candidate for candidate in candidates if _int(candidate.get("maker_wallet_count")) > 0),
        key=lambda item: (
            _int(item.get("maker_wallet_count")), _float(item.get("maker_flow_usdc"))
        ),
        reverse=True,
    )[:1]
    for candidate in maker_cohort:
        condition = str(candidate.get("condition_id") or "")
        if condition and condition not in chosen_ids and len(chosen) < MAX_MARKETS:
            chosen.append(candidate)
            chosen_ids.add(condition)
    # 고래 MM 시장이 없을 때도 표본 확보를 위해 거래량 상위 cohort 하나를 유지한다.
    volume_cohort = sorted(
        candidates, key=lambda item: _float(item.get("volume_24h")), reverse=True
    )[:1]
    for candidate in volume_cohort:
        condition = str(candidate.get("condition_id") or "")
        if condition and condition not in chosen_ids and len(chosen) < MAX_MARKETS:
            chosen.append(candidate)
            chosen_ids.add(condition)
    for candidate in candidates:
        condition = str(candidate.get("condition_id") or "")
        if condition and condition not in chosen_ids and len(chosen) < MAX_MARKETS:
            chosen.append(candidate)
            chosen_ids.add(condition)
    previous = set(str(value) for value in state.get("selected_conditions") or [])
    _sync_selected_markets(state, chosen)
    state["last_selection_changes"] = len(previous.symmetric_difference(chosen_ids))
    state["last_discovery_ts"] = generated_ts
    state["last_discovery_at"] = snapshot.get("generated_at") or now_kst()
    state["last_discovery_duration_seconds"] = _float(snapshot.get("duration_seconds"))
    state["discovery_rejections"] = rejects
    state["last_discovery_candidates"] = len(candidates)
    state["last_discovery_universe"] = _int(rejects.get("_universe_scanned"))
    state["last_discovery_metadata_eligible"] = _int(rejects.get("_metadata_eligible"))
    state["last_discovery_truncated"] = bool(rejects.get("_universe_truncated"))
    return True, ""


def _new_state(initial_cash: float | None = None) -> dict[str, Any]:
    seed = PAPER_INITIAL_CASH if initial_cash is None else max(_float(initial_cash), 0.0)
    return {
        "version": STRATEGY_VERSION,
        "strategy_revision": 4,
        "strategy": "maker_mirror_v2",
        "mode": "paper",
        "started_at": now_kst(),
        "started_ts": time.time(),
        "cash": seed,
        "initial_cash": seed,
        "markets": {},
        "orders": {},
        "seen_trades": [],
        "trade_bootstrap_complete": False,
        "fills": 0,
        "buy_fills": 0,
        "sell_fills": 0,
        "maker_buy_notional": 0.0,
        "maker_sell_notional": 0.0,
        "maker_spread_pnl": 0.0,
        "rebalance_pnl": 0.0,
        "rebalance_count": 0,
        "split_operations": 0,
        "split_shares": 0.0,
        "paired_shares": 0.0,
        "pair_cycles": 0,
        "realized_pnl": 0.0,
        "exit_pnl": 0.0,
        "equity_curve": [seed],
        "peak_equity": seed,
        "max_drawdown": 0.0,
        "cycles": 0,
        "last_discovery_ts": 0.0,
        "last_scan": {},
        "promotion": {"approved": False, "reasons": ["paper not started"]},
    }


def _migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    if _int(state.get("version")) >= STRATEGY_VERSION:
        if _int(state.get("strategy_revision")) < 2:
            for market in (state.get("markets") or {}).values():
                market["target_inventory"] = TARGET_INVENTORY_SHARES
            state["strategy_revision"] = 2
        if _int(state.get("strategy_revision")) < 3:
            # revision 2의 10주→5주 목표 축소 merge는 거래 성과가 아니다.
            if _int(state.get("fills")) == 0:
                state["pair_cycles"] = 0
                state["paired_shares"] = 0.0
            state["strategy_revision"] = 3
        if _int(state.get("strategy_revision")) < 4:
            if _int(state.get("fills")) == 0:
                state["pair_cycles"] = 0
                state["paired_shares"] = 0.0
            state["strategy_revision"] = 4
        state.setdefault("trade_bootstrap_complete", False)
        for key, default in (
            ("buy_fills", 0), ("sell_fills", 0),
            ("maker_buy_notional", 0.0), ("maker_sell_notional", 0.0),
            ("maker_spread_pnl", 0.0), ("rebalance_pnl", 0.0),
            ("rebalance_count", 0), ("split_operations", 0), ("split_shares", 0.0),
        ):
            state.setdefault(key, default)
        return state

    # V1은 BUY-only 전략이라 V2와 성과를 섞으면 검증 지표가 무의미하다. 이전 결과를
    # state 안에 보존하고, 당시 liquidation equity를 새 전략의 실제 시작 시드로 쓴다.
    starting_equity = _float(state.get("equity"), _float(state.get("cash"), PAPER_INITIAL_CASH))
    migrated = _new_state(starting_equity)
    migrated["legacy_v1_summary"] = {
        "archived_at": now_kst(),
        "started_at": state.get("started_at"),
        "cash": _float(state.get("cash")),
        "equity": _float(state.get("equity")),
        "realized_pnl": _float(state.get("realized_pnl")),
        "fills": _int(state.get("fills")),
        "pair_cycles": _int(state.get("pair_cycles")),
        "paired_shares": _float(state.get("paired_shares")),
        "exit_pnl": _float(state.get("exit_pnl")),
        "max_drawdown": _float(state.get("max_drawdown")),
        "orders": len(state.get("orders") or {}),
        "inventory_shares": sum(
            sum(_float(value) for value in market.get("inventory") or [])
            for market in (state.get("markets") or {}).values()
        ),
    }
    append_jsonl(JOURNAL_FILE, {
        "event": "strategy_migrated", "at": now_kst(),
        "from_version": _int(state.get("version"), 1),
        "to_version": STRATEGY_VERSION,
        "starting_equity": round(starting_equity, 6),
        "legacy": migrated["legacy_v1_summary"],
    })
    return migrated


def _load_state() -> dict[str, Any]:
    state = load_json(STATE_FILE, default=None)
    return _migrate_state(state) if isinstance(state, dict) else _new_state()


def _market_state(state: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    condition = candidate["condition_id"]
    market = state.setdefault("markets", {}).setdefault(condition, {
        "condition_id": condition,
        "gamma_market_id": candidate.get("gamma_market_id"),
        "title": candidate.get("title"),
        "tokens": candidate.get("tokens"),
        "outcomes": candidate.get("outcomes"),
        "neg_risk": candidate.get("neg_risk", False),
        "inventory": [0.0, 0.0],
        "inventory_cost": [0.0, 0.0],
        "inventory_since_ts": [0.0, 0.0],
        "fills": 0,
        "buy_fills": 0,
        "sell_fills": 0,
        "maker_buy_notional": 0.0,
        "maker_sell_notional": 0.0,
        "maker_spread_pnl": 0.0,
        "rebalance_pnl": 0.0,
        "split_shares": 0.0,
        "target_inventory": TARGET_INVENTORY_SHARES,
        "flow": [{"buy": 0.0, "sell": 0.0, "updated_ts": 0.0} for _ in range(2)],
        "pairs": 0.0,
        "realized_pnl": 0.0,
        "exit_pnl": 0.0,
        "active": True,
    })
    for key in (
        "gamma_market_id", "title", "tokens", "outcomes", "neg_risk",
        "end_date", "end_hours", "liquidity", "volume_24h", "rewards_min_size",
        "rewards_max_spread", "rewards_daily_rate", "reward_eligible_at_quote_size",
        "fees_enabled", "tick_sizes", "min_order_sizes", "score", "sports_market_type",
        "game_start_time", "maker_wallet_count", "maker_wallets", "maker_flow_usdc",
        "maker_balance_ratio",
    ):
        if key in candidate:
            market[key] = candidate[key]
    market["active"] = True
    market.setdefault("target_inventory", TARGET_INVENTORY_SHARES)
    market.setdefault("flow", [{"buy": 0.0, "sell": 0.0, "updated_ts": 0.0} for _ in range(2)])
    for key, default in (
        ("buy_fills", 0), ("sell_fills", 0),
        ("maker_buy_notional", 0.0), ("maker_sell_notional", 0.0),
        ("maker_spread_pnl", 0.0), ("rebalance_pnl", 0.0), ("split_shares", 0.0),
    ):
        market.setdefault(key, default)
    market["last_selected_at"] = now_kst()
    return market


def _sync_selected_markets(state: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    selected_order = [candidate["condition_id"] for candidate in candidates]
    selected = set(selected_order)
    for market in (state.get("markets") or {}).values():
        market["active"] = market.get("condition_id") in selected
    for candidate in candidates:
        _market_state(state, candidate)
    state["selected_conditions"] = selected_order


def _total_inventory_cost(state: dict[str, Any]) -> float:
    return sum(
        sum(_float(v) for v in market.get("inventory_cost") or [])
        for market in (state.get("markets") or {}).values()
    )


def _inventory_excess(market: dict[str, Any]) -> tuple[int | None, float]:
    inventory = [_float(v) for v in market.get("inventory") or [0, 0]]
    if inventory[0] > inventory[1] + 1e-9:
        return 0, inventory[0] - inventory[1]
    if inventory[1] > inventory[0] + 1e-9:
        return 1, inventory[1] - inventory[0]
    return None, 0.0


def _avg_cost(market: dict[str, Any], outcome: int) -> float:
    shares = _float((market.get("inventory") or [0, 0])[outcome])
    cost = _float((market.get("inventory_cost") or [0, 0])[outcome])
    return cost / shares if shares > 0 else 0.0


def _flow_signal(market: dict[str, Any], outcome: int) -> tuple[float, float]:
    rows = market.get("flow") or []
    row = rows[outcome] if len(rows) > outcome and isinstance(rows[outcome], dict) else {}
    age = max(time.time() - _float(row.get("updated_ts")), 0.0)
    decay = math.exp(-age / max(FLOW_DECAY_SECONDS, 1.0))
    buy = _float(row.get("buy")) * decay
    sell = _float(row.get("sell")) * decay
    total = buy + sell
    return ((buy - sell) / total if total > 1e-9 else 0.0), total


def fair_prices(market: dict[str, Any], books: list[dict[str, Any]]) -> list[float]:
    """보완 토큰 일관성, order imbalance, 최근 체결 흐름, 재고를 합친 공정가."""
    raw0 = _microprice(books[0])
    raw1 = _microprice(books[1])
    base0 = min(max((raw0 + (1.0 - raw1)) / 2, 0.02), 0.98)
    inventory = [_float(v) for v in market.get("inventory") or [0, 0]]
    target = _float(market.get("target_inventory"), TARGET_INVENTORY_SHARES)
    flow0, _ = _flow_signal(market, 0)
    flow1, _ = _flow_signal(market, 1)
    directional_flow = (flow0 - flow1) / 2
    flow_skew = max(min(directional_flow * FLOW_SKEW_MAX, FLOW_SKEW_MAX), -FLOW_SKEW_MAX)
    inventory_skew = max(min(
        -(inventory[0] - inventory[1]) * INVENTORY_SKEW_PER_SHARE,
        BASE_HALF_SPREAD * 2,
    ), -BASE_HALF_SPREAD * 2)
    # target은 가격 skew가 아닌 split 재고 보충에 사용한다. 상대 재고 차이만 방향 위험이다.
    del target
    fair0 = min(max(base0 + flow_skew + inventory_skew, 0.02), 0.98)
    return [fair0, 1.0 - fair0]


def quote_targets(market: dict[str, Any], books: list[dict[str, Any]],
                  *, cash: float, total_committed: float) -> list[dict[str, Any]]:
    """Split 재고 기반 YES/NO BUY+SELL maker 호가를 만든다."""
    if len(books) != 2:
        return []
    fair = fair_prices(market, books)
    bid_targets: list[float] = []
    ask_targets: list[float] = []
    for outcome, book in enumerate(books):
        bid = _float(book.get("best_bid"))
        ask = _float(book.get("best_ask"))
        tick = _float(book.get("tick_size"), 0.01)
        if not (0 < bid < ask < 1) or ask - bid < 2 * tick - 1e-12:
            return []
        half = max(BASE_HALF_SPREAD, tick, (ask - bid) * 0.30)
        bid_price = min(bid + tick, fair[outcome] - half, ask - tick)
        ask_price = max(ask - tick, fair[outcome] + half, bid + tick)
        bid_targets.append(_floor_tick(bid_price, tick))
        ask_targets.append(_ceil_tick(ask_price, tick))

    # 두 BUY가 모두 체결되면 $1 미만, split으로 받은 두 토큰을 모두 팔면 $1 초과가
    # 되도록 완성세트 경제성을 주문 전에 보장한다.
    if sum(bid_targets) > MAX_PAIR_COST:
        overflow = sum(bid_targets) - MAX_PAIR_COST
        for outcome in (0, 1):
            tick = _float(books[outcome].get("tick_size"), 0.01)
            bid_targets[outcome] = _floor_tick(bid_targets[outcome] - overflow / 2, tick)
    minimum_ask_sum = 1.0 + MIN_LOCKED_PAIR_EDGE
    if sum(ask_targets) < minimum_ask_sum:
        shortage = minimum_ask_sum - sum(ask_targets)
        for outcome in (0, 1):
            tick = _float(books[outcome].get("tick_size"), 0.01)
            ask_targets[outcome] = _ceil_tick(ask_targets[outcome] + shortage / 2, tick)

    result = []
    inventory = [_float(v) for v in market.get("inventory") or [0, 0]]
    excess_side, excess_shares = _inventory_excess(market)
    remaining_budget = min(max(cash, 0.0), max(MAX_TOTAL_COMMITTED - total_committed, 0.0))
    for outcome in (0, 1):
        min_size = _float(books[outcome].get("min_order_size"), 5.0)
        size = max(QUOTE_SHARES, min_size)
        flow, flow_volume = _flow_signal(market, outcome)
        common = {
            "condition_id": market["condition_id"],
            "token_id": str((market.get("tokens") or [])[outcome]),
            "outcome_index": outcome,
            "tick_size": _float(books[outcome].get("tick_size"), 0.01),
            "neg_risk": bool(market.get("neg_risk")),
            "fair_price": round(fair[outcome], 6),
            "flow_imbalance": round(flow, 6),
        }
        # 공격적 SELL 흐름을 받는 BUY, 공격적 BUY 흐름에 파는 SELL은 독성 구간에서 쉰다.
        suppress_buy = (
            (flow_volume > 0 and flow <= -TOXIC_FLOW_THRESHOLD)
            or (outcome == excess_side and excess_shares >= QUOTE_SHARES - 1e-9)
        )
        if not suppress_buy:
            price = bid_targets[outcome]
            affordable = min(remaining_budget / price if price > 0 else 0.0, size)
            if (
                0.02 <= price <= 0.98 and affordable + 1e-9 >= min_size
                and price * affordable <= MAX_UNMATCHED_USD + 1e-9
            ):
                buy_size = round(affordable, 6)
                result.append({
                    **common, "side": "BUY", "price": price, "size": buy_size,
                    "queue_ahead": _queue_at(books[outcome], price, "BUY") * PAPER_QUEUE_FRACTION,
                })
                remaining_budget -= price * buy_size
        if not (flow_volume > 0 and flow >= TOXIC_FLOW_THRESHOLD):
            price = ask_targets[outcome]
            sell_size = min(size, inventory[outcome])
            if 0.02 <= price <= 0.98 and sell_size + 1e-9 >= min_size:
                result.append({
                    **common, "side": "SELL", "price": price,
                    "size": round(sell_size, 6),
                    "queue_ahead": _queue_at(books[outcome], price, "SELL") * PAPER_QUEUE_FRACTION,
                })
    buy_rows = [row for row in result if row["side"] == "BUY"]
    if excess_side is None and len(buy_rows) != 2:
        # 균형 상태에서 한 결과만 살 수 있으면 market making이 아니라 방향 베팅이다.
        result = [row for row in result if row["side"] != "BUY"]
    return result


def _order_key(condition: str, outcome: int, side: str = "BUY") -> str:
    return f"{condition}:{outcome}:{str(side).upper()}"


def _replace_paper_quotes(state: dict[str, Any], market: dict[str, Any],
                          targets: list[dict[str, Any]]) -> tuple[int, int]:
    condition = market["condition_id"]
    target_by_key = {
        (_int(row["outcome_index"]), str(row.get("side") or "BUY").upper()): row
        for row in targets
    }
    canceled = 0
    placed = 0
    orders = state.setdefault("orders", {})
    for outcome in (0, 1):
        for side in ("BUY", "SELL"):
            key = _order_key(condition, outcome, side)
            current = orders.get(key)
            target = target_by_key.get((outcome, side))
            tick = _float((target or current or {}).get("tick_size"), 0.01)
            same = bool(
                current and target
                and abs(_float(current.get("price")) - _float(target.get("price")))
                    < REQUOTE_TICKS * tick - 1e-12
                and abs(_float(current.get("size")) - _float(target.get("size"))) < 1e-6
                and time.time() - _float(current.get("placed_ts")) < QUOTE_LIFETIME_SECONDS
            )
            if same:
                continue
            if current:
                append_jsonl(JOURNAL_FILE, {
                    **current, "event": "paper_quote_canceled", "at": now_kst(),
                    "reason": "requote" if target else "risk_or_inventory",
                })
                orders.pop(key, None)
                canceled += 1
            if target:
                order = {
                    **target,
                    "remaining": target["size"],
                    "placed_ts": time.time(),
                    "placed_at": now_kst(),
                    "mode": "paper",
                }
                orders[key] = order
                append_jsonl(JOURNAL_FILE, {**order, "event": "paper_quote_placed"})
                placed += 1
    return placed, canceled


def _trade_fingerprint(row: dict[str, Any]) -> str:
    raw = "|".join(str(row.get(key) or "") for key in (
        "transactionHash", "asset", "side", "price", "size", "timestamp", "proxyWallet"
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def fetch_market_trades(condition: str) -> list[dict[str, Any]]:
    rows = _get_json(f"{DATA_API}/trades", {
        "market": condition, "limit": 100, "takerOnly": "true",
    })
    return rows if isinstance(rows, list) else []


def load_stream_market_data(
    relevant: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any]]:
    """WebSocket 수집기가 만든 최신 L2 snapshot과 체결을 읽는다."""
    snapshot = load_json(STREAM_STATE_FILE, default={})
    if not isinstance(snapshot, dict):
        snapshot = {}
    heartbeat_ts = _float(snapshot.get("heartbeat_ts"))
    lag = max(time.time() - heartbeat_ts, 0.0) if heartbeat_ts else 999999.0
    fresh = bool(snapshot.get("connected")) and lag <= STREAM_STALE_SECONDS
    raw_books = snapshot.get("books") if isinstance(snapshot.get("books"), dict) else {}
    books_by_condition: dict[str, list[dict[str, Any]]] = {}
    if fresh:
        for market in relevant:
            tokens = [str(value) for value in market.get("tokens") or []]
            prepared = []
            for outcome, token in enumerate(tokens):
                raw = raw_books.get(token)
                if not isinstance(raw, dict):
                    continue
                enriched = dict(raw)
                enriched["asset_id"] = token
                enriched["market"] = market["condition_id"]
                ticks = market.get("tick_sizes") or [0.01, 0.01]
                minimums = market.get("min_order_sizes") or [5.0, 5.0]
                enriched.setdefault("tick_size", ticks[outcome] if len(ticks) > outcome else 0.01)
                enriched.setdefault(
                    "min_order_size", minimums[outcome] if len(minimums) > outcome else 5.0
                )
                prepared.append(analyze_book(enriched))
            if len(prepared) == 2:
                books_by_condition[market["condition_id"]] = prepared
    trades = snapshot.get("trades") if fresh and isinstance(snapshot.get("trades"), list) else []
    stats = {
        "connected": bool(snapshot.get("connected")),
        "fresh": fresh,
        "lag_ms": round(lag * 1000, 1) if lag < 999999 else None,
        "event_count": _int(snapshot.get("event_count")),
        "trade_events": _int((snapshot.get("event_types") or {}).get("last_trade_price")),
        "subscribed_tokens": len(snapshot.get("tokens") or []),
        "books": len(books_by_condition),
    }
    return books_by_condition, trades, stats


def fetch_rest_market_data(
    markets: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[str]]:
    """WebSocket 초기 연결/장애 때만 쓰는 batch REST fallback."""
    tokens = [str(token) for market in markets for token in market.get("tokens") or []]
    books_by_token: dict[str, dict[str, Any]] = {}
    errors = []
    if tokens:
        try:
            books_by_token = fetch_books_batch(tokens)
        except Exception as exc:
            errors.append(f"batch_books:{str(exc)[:120]}")
    books_by_condition: dict[str, list[dict[str, Any]]] = {}
    trades = []
    for market in markets:
        market_tokens = [str(value) for value in market.get("tokens") or []]
        books = [books_by_token[token] for token in market_tokens if token in books_by_token]
        if len(books) == 2:
            books_by_condition[market["condition_id"]] = books
        if market.get("active"):
            try:
                trades.extend(fetch_market_trades(market["condition_id"]))
            except Exception as exc:
                errors.append(f"{market['condition_id'][:10]}:{str(exc)[:100]}")
    return books_by_condition, trades, errors


def ensure_split_inventory(
    state: dict[str, Any], market: dict[str, Any], books: list[dict[str, Any]],
) -> float:
    """Paper pUSD를 YES/NO 완성세트로 split해 양쪽 SELL 재고를 만든다."""
    if not market.get("active") or len(books) != 2:
        return 0.0
    inventory = [_float(value) for value in market.get("inventory") or [0, 0]]
    target = max(_float(market.get("target_inventory"), TARGET_INVENTORY_SHARES), 0.0)
    needed = max(target - min(inventory), 0.0)
    committed = _total_inventory_cost(state) + _reserved_order_usd(state)
    affordable = min(
        needed,
        _float(state.get("cash")),
        max(MAX_TOTAL_COMMITTED - committed, 0.0),
    )
    min_size = max(_float(book.get("min_order_size"), QUOTE_SHARES) for book in books)
    if affordable + 1e-9 < min_size:
        return 0.0
    shares = round(affordable, 6)
    fair = fair_prices(market, books)
    state["cash"] = _float(state.get("cash")) - shares
    for outcome in (0, 1):
        market["inventory"][outcome] = inventory[outcome] + shares
        market["inventory_cost"][outcome] = (
            _float(market["inventory_cost"][outcome]) + shares * fair[outcome]
        )
    market["split_shares"] = _float(market.get("split_shares")) + shares
    state["split_shares"] = _float(state.get("split_shares")) + shares
    state["split_operations"] = _int(state.get("split_operations")) + 1
    append_jsonl(JOURNAL_FILE, {
        "event": "paper_inventory_split", "at": now_kst(),
        "condition_id": market["condition_id"], "title": market.get("title"),
        "shares": shares, "collateral_usd": shares,
        "cost_allocation": [round(shares * fair[0], 6), round(shares * fair[1], 6)],
    })
    return shares


def _record_market_flow(market: dict[str, Any], trade: dict[str, Any]) -> None:
    token = str(trade.get("asset") or "")
    try:
        outcome = [str(value) for value in market.get("tokens") or []].index(token)
    except ValueError:
        return
    rows = market.setdefault(
        "flow", [{"buy": 0.0, "sell": 0.0, "updated_ts": 0.0} for _ in range(2)]
    )
    while len(rows) < 2:
        rows.append({"buy": 0.0, "sell": 0.0, "updated_ts": 0.0})
    row = rows[outcome]
    now_ts = time.time()
    age = max(now_ts - _float(row.get("updated_ts")), 0.0)
    decay = math.exp(-age / max(FLOW_DECAY_SECONDS, 1.0))
    row["buy"] = _float(row.get("buy")) * decay
    row["sell"] = _float(row.get("sell")) * decay
    side = str(trade.get("side") or "").lower()
    if side in {"buy", "sell"}:
        row[side] += max(_float(trade.get("size")), 0.0)
    row["updated_ts"] = now_ts


def _refresh_inventory_age(market: dict[str, Any]) -> None:
    side, shares = _inventory_excess(market)
    ages = market.setdefault("inventory_since_ts", [0.0, 0.0])
    if side is None or shares <= 1e-9:
        market["inventory_since_ts"] = [0.0, 0.0]
        return
    other = 1 - side
    ages[other] = 0.0
    if not _float(ages[side]):
        ages[side] = time.time()


def _apply_buy_fill(state: dict[str, Any], order: dict[str, Any],
                    shares: float, trade: dict[str, Any]) -> float:
    market = state["markets"][order["condition_id"]]
    outcome = _int(order["outcome_index"])
    price = _float(order["price"])
    affordable = _float(state.get("cash")) / price if price > 0 else 0.0
    shares = min(shares, _float(order.get("remaining")), affordable)
    if shares <= 1e-9:
        return 0.0
    cost = shares * price
    state["cash"] = _float(state.get("cash")) - cost
    market["inventory"][outcome] = _float(market["inventory"][outcome]) + shares
    market["inventory_cost"][outcome] = _float(market["inventory_cost"][outcome]) + cost
    _refresh_inventory_age(market)
    market["fills"] = _int(market.get("fills")) + 1
    market["buy_fills"] = _int(market.get("buy_fills")) + 1
    market["maker_buy_notional"] = _float(market.get("maker_buy_notional")) + cost
    state["fills"] = _int(state.get("fills")) + 1
    state["buy_fills"] = _int(state.get("buy_fills")) + 1
    state["maker_buy_notional"] = _float(state.get("maker_buy_notional")) + cost
    order["remaining"] = max(_float(order.get("remaining")) - shares, 0.0)
    append_jsonl(JOURNAL_FILE, {
        "event": "paper_maker_fill",
        "at": now_kst(),
        "condition_id": order["condition_id"],
        "token_id": order["token_id"],
        "outcome_index": outcome,
        "side": "BUY",
        "title": market.get("title"),
        "price": price,
        "shares": round(shares, 6),
        "cost_usd": round(cost, 6),
        "trade_price": _float(trade.get("price")),
        "trade_size": _float(trade.get("size")),
        "queue_model": "price_time_queue_v1",
    })
    return shares


def _apply_sell_fill(state: dict[str, Any], order: dict[str, Any],
                     shares: float, trade: dict[str, Any]) -> float:
    market = state["markets"][order["condition_id"]]
    outcome = _int(order["outcome_index"])
    price = _float(order["price"])
    inventory = _float(market["inventory"][outcome])
    shares = min(shares, _float(order.get("remaining")), inventory)
    if shares <= 1e-9:
        return 0.0
    avg = _avg_cost(market, outcome)
    proceeds = shares * price
    cost = shares * avg
    pnl = proceeds - cost
    state["cash"] = _float(state.get("cash")) + proceeds
    market["inventory"][outcome] = max(inventory - shares, 0.0)
    market["inventory_cost"][outcome] = max(
        _float(market["inventory_cost"][outcome]) - cost, 0.0
    )
    if market["inventory"][outcome] <= 1e-9:
        market["inventory_since_ts"][outcome] = 0.0
    _refresh_inventory_age(market)
    market["fills"] = _int(market.get("fills")) + 1
    market["sell_fills"] = _int(market.get("sell_fills")) + 1
    market["maker_sell_notional"] = _float(market.get("maker_sell_notional")) + proceeds
    market["maker_spread_pnl"] = _float(market.get("maker_spread_pnl")) + pnl
    market["realized_pnl"] = _float(market.get("realized_pnl")) + pnl
    state["fills"] = _int(state.get("fills")) + 1
    state["sell_fills"] = _int(state.get("sell_fills")) + 1
    state["maker_sell_notional"] = _float(state.get("maker_sell_notional")) + proceeds
    state["maker_spread_pnl"] = _float(state.get("maker_spread_pnl")) + pnl
    state["realized_pnl"] = _float(state.get("realized_pnl")) + pnl
    order["remaining"] = max(_float(order.get("remaining")) - shares, 0.0)
    append_jsonl(JOURNAL_FILE, {
        "event": "paper_maker_fill", "at": now_kst(),
        "condition_id": order["condition_id"], "token_id": order["token_id"],
        "outcome_index": outcome, "side": "SELL", "title": market.get("title"),
        "price": price, "shares": round(shares, 6),
        "proceeds_usd": round(proceeds, 6), "cost_basis_usd": round(cost, 6),
        "pnl_usd": round(pnl, 6), "trade_price": _float(trade.get("price")),
        "trade_size": _float(trade.get("size")), "queue_model": "price_time_queue_v2",
    })
    return shares


def process_paper_trades(state: dict[str, Any], trades: Iterable[dict[str, Any]]) -> int:
    """공개 taker trade로 BUY/SELL maker 주문의 queue-aware 체결을 모사한다."""
    trades = list(trades)
    if not state.get("trade_bootstrap_complete"):
        fingerprints = [_trade_fingerprint(trade) for trade in trades]
        state["seen_trades"] = list(dict.fromkeys(fingerprints))[-20000:]
        state["trade_bootstrap_complete"] = True
        for market in (state.get("markets") or {}).values():
            market["flow"] = [
                {"buy": 0.0, "sell": 0.0, "updated_ts": 0.0} for _ in range(2)
            ]
        append_jsonl(JOURNAL_FILE, {
            "event": "paper_trade_baseline", "at": now_kst(),
            "trades_seeded": len(fingerprints),
        })
        return 0
    seen = set(str(value) for value in state.get("seen_trades") or [])
    new_seen = []
    fills = 0
    token_orders = {
        (str(order.get("token_id")), str(order.get("side") or "BUY").upper()): order
        for order in (state.get("orders") or {}).values()
        if _float(order.get("remaining")) > 0
    }
    for trade in sorted(trades, key=lambda row: _int(row.get("timestamp"))):
        fingerprint = _trade_fingerprint(trade)
        if fingerprint in seen:
            continue
        new_seen.append(fingerprint)
        token = str(trade.get("asset") or "")
        for market in (state.get("markets") or {}).values():
            if token in {str(value) for value in market.get("tokens") or []}:
                _record_market_flow(market, trade)
                break
        taker_side = str(trade.get("side") or "").upper()
        maker_side = "BUY" if taker_side == "SELL" else "SELL" if taker_side == "BUY" else ""
        order = token_orders.get((token, maker_side))
        if not order:
            continue
        trade_price = _float(trade.get("price"))
        trade_size = _float(trade.get("size"))
        crosses = (
            trade_price <= _float(order.get("price")) + 1e-12
            if maker_side == "BUY"
            else trade_price >= _float(order.get("price")) - 1e-12
        )
        if not crosses or trade_size <= 0:
            continue
        queue = max(_float(order.get("queue_ahead")), 0.0)
        queue_consumed = min(queue, trade_size)
        order["queue_ahead"] = queue - queue_consumed
        fillable = trade_size - queue_consumed
        if fillable <= 0:
            continue
        applied = (
            _apply_buy_fill(state, order, fillable, trade)
            if maker_side == "BUY"
            else _apply_sell_fill(state, order, fillable, trade)
        )
        if applied > 0:
            fills += 1
    if new_seen:
        state["seen_trades"] = (list(seen) + new_seen)[-20000:]
    for key, order in list((state.get("orders") or {}).items()):
        if _float(order.get("remaining")) <= 1e-9:
            state["orders"].pop(key, None)
    return fills


def merge_complete_sets(
    state: dict[str, Any], market: dict[str, Any], *, keep_shares: float = 0.0,
    count_performance: bool = True,
) -> float:
    inventory = market.get("inventory") or [0.0, 0.0]
    pair_shares = max(
        min(_float(inventory[0]), _float(inventory[1])) - max(keep_shares, 0.0), 0.0
    )
    if pair_shares <= 1e-9:
        return 0.0
    avg0 = _avg_cost(market, 0)
    avg1 = _avg_cost(market, 1)
    pair_cost = pair_shares * (avg0 + avg1)
    payout = pair_shares
    pnl = payout - pair_cost
    for outcome, avg in ((0, avg0), (1, avg1)):
        market["inventory"][outcome] = max(_float(inventory[outcome]) - pair_shares, 0.0)
        market["inventory_cost"][outcome] = max(
            _float(market["inventory_cost"][outcome]) - pair_shares * avg, 0.0
        )
        if market["inventory"][outcome] <= 1e-9:
            market["inventory_since_ts"][outcome] = 0.0
    state["cash"] = _float(state.get("cash")) + payout
    state["realized_pnl"] = _float(state.get("realized_pnl")) + pnl
    if count_performance:
        state["paired_shares"] = _float(state.get("paired_shares")) + pair_shares
        state["pair_cycles"] = _int(state.get("pair_cycles")) + 1
    market["pairs"] = _float(market.get("pairs")) + pair_shares
    market["realized_pnl"] = _float(market.get("realized_pnl")) + pnl
    _refresh_inventory_age(market)
    append_jsonl(JOURNAL_FILE, {
        "event": "paper_pair_merged",
        "at": now_kst(),
        "condition_id": market["condition_id"],
        "title": market.get("title"),
        "shares": round(pair_shares, 6),
        "pair_cost": round(pair_cost, 6),
        "payout": round(payout, 6),
        "pnl_usd": round(pnl, 6),
        "performance_cycle": bool(count_performance),
    })
    return pair_shares


def _best_bid(book: dict[str, Any]) -> float:
    return _float(book.get("best_bid"))


def _taker_fee(market: dict[str, Any], shares: float, price: float) -> float:
    if not market.get("fees_enabled"):
        return 0.0
    return max(shares, 0.0) * TAKER_FEE_RATE * max(price, 0.0) * max(1.0 - price, 0.0)


def exit_stale_inventory(state: dict[str, Any], market: dict[str, Any],
                         books: list[dict[str, Any]]) -> float:
    """편측 재고를 direct SELL과 complement BUY+merge 중 더 나은 방법으로 중립화."""
    side, shares = _inventory_excess(market)
    if side is None or shares <= 1e-9:
        return 0.0
    since = _float((market.get("inventory_since_ts") or [0, 0])[side])
    age = max(time.time() - since, 0.0) if since else 0.0
    avg = _avg_cost(market, side)
    bid = _best_bid(books[side])
    adverse = avg - bid
    complement = 1 - side
    complement_ask = _float(books[complement].get("best_ask"))
    complement_fee = _taker_fee(market, shares, complement_ask)
    locked_pair_pnl = shares * (1.0 - avg - complement_ask) - complement_fee
    flow, flow_volume = _flow_signal(market, side)
    toxic = flow_volume > 0 and flow <= -TOXIC_FLOW_THRESHOLD
    if (
        locked_pair_pnl <= 0
        and age < MAX_INVENTORY_AGE
        and adverse < MAX_ADVERSE_MOVE
        and not toxic
    ):
        return 0.0

    direct_fee = _taker_fee(market, shares, bid)
    direct_effective = bid - direct_fee / max(shares, 1e-9)
    complement_effective = 1.0 - complement_ask - complement_fee / max(shares, 1e-9)
    can_buy_complement = _float(state.get("cash")) + 1e-9 >= shares * complement_ask + complement_fee
    if complement_ask > 0 and can_buy_complement and complement_effective > direct_effective:
        hedge_cost = shares * complement_ask + complement_fee
        state["cash"] = _float(state.get("cash")) - hedge_cost
        market["inventory"][complement] = _float(market["inventory"][complement]) + shares
        market["inventory_cost"][complement] = (
            _float(market["inventory_cost"][complement]) + hedge_cost
        )
        _refresh_inventory_age(market)
        before = _float(state.get("realized_pnl"))
        merged = merge_complete_sets(
            state, market,
            keep_shares=_float(market.get("target_inventory"), TARGET_INVENTORY_SHARES),
        )
        pnl = _float(state.get("realized_pnl")) - before
        state["rebalance_pnl"] = _float(state.get("rebalance_pnl")) + pnl
        state["rebalance_count"] = _int(state.get("rebalance_count")) + 1
        market["rebalance_pnl"] = _float(market.get("rebalance_pnl")) + pnl
        append_jsonl(JOURNAL_FILE, {
            "event": "paper_inventory_rebalance", "at": now_kst(),
            "condition_id": market["condition_id"], "title": market.get("title"),
            "method": "complement_buy", "outcome_index": side,
            "shares": round(shares, 6), "avg_cost": round(avg, 6),
            "hedge_outcome": complement, "hedge_price": round(complement_ask, 6),
            "fee_usd": round(complement_fee, 6), "merged_shares": round(merged, 6),
            "pnl_usd": round(pnl, 6),
            "reason": "locked_pair" if locked_pair_pnl > 0 else "risk_rebalance",
        })
        return pnl if abs(pnl) > 1e-12 else 1e-12

    exit_price = max(bid, 0.001)
    proceeds = shares * exit_price - direct_fee
    cost = shares * avg
    pnl = proceeds - cost
    state["cash"] = _float(state.get("cash")) + proceeds
    market["inventory"][side] = max(_float(market["inventory"][side]) - shares, 0.0)
    market["inventory_cost"][side] = max(
        _float(market["inventory_cost"][side]) - cost, 0.0
    )
    if market["inventory"][side] <= 1e-9:
        market["inventory_since_ts"][side] = 0.0
    state["realized_pnl"] = _float(state.get("realized_pnl")) + pnl
    state["exit_pnl"] = _float(state.get("exit_pnl")) + pnl
    market["realized_pnl"] = _float(market.get("realized_pnl")) + pnl
    market["exit_pnl"] = _float(market.get("exit_pnl")) + pnl
    market["rebalance_pnl"] = _float(market.get("rebalance_pnl")) + pnl
    state["rebalance_pnl"] = _float(state.get("rebalance_pnl")) + pnl
    state["rebalance_count"] = _int(state.get("rebalance_count")) + 1
    _refresh_inventory_age(market)
    append_jsonl(JOURNAL_FILE, {
        "event": "paper_inventory_exit",
        "at": now_kst(),
        "condition_id": market["condition_id"],
        "title": market.get("title"),
        "outcome_index": side,
        "shares": round(shares, 6),
        "avg_cost": round(avg, 6),
        "exit_price": round(exit_price, 6),
        "fee_usd": round(direct_fee, 6),
        "inventory_age_seconds": round(age, 3),
        "pnl_usd": round(pnl, 6),
        "reason": (
            "adverse_move" if adverse >= MAX_ADVERSE_MOVE
            else "toxic_flow" if toxic else "inventory_timeout"
        ),
        "method": "direct_sell",
    })
    return pnl


def mark_equity(state: dict[str, Any], books_by_condition: dict[str, list[dict[str, Any]]]) -> float:
    value = _float(state.get("cash"))
    for condition, market in (state.get("markets") or {}).items():
        books = books_by_condition.get(condition) or []
        inventory = [_float(value) for value in market.get("inventory") or [0, 0]]
        complete_sets = min(inventory)
        value += complete_sets  # YES+NO는 거래 없이 merge하면 정확히 $1.
        for outcome in (0, 1):
            excess = max(inventory[outcome] - complete_sets, 0.0)
            if excess <= 0:
                continue
            mark = (
                _best_bid(books[outcome])
                if len(books) > outcome else _avg_cost(market, outcome)
            )
            value += excess * mark
    state["equity"] = value
    peak = max(_float(state.get("peak_equity"), value), value)
    state["peak_equity"] = peak
    drawdown = max(peak - value, 0.0)
    state["max_drawdown"] = max(_float(state.get("max_drawdown")), drawdown)
    curve = list(state.get("equity_curve") or [])
    curve.append(round(value, 6))
    state["equity_curve"] = curve[-5000:]
    return value


def promotion_status(state: dict[str, Any]) -> dict[str, Any]:
    elapsed_days = max((time.time() - _float(state.get("started_ts"))) / 86400, 0.0)
    equity = _float(state.get("equity"), _float(state.get("cash")))
    initial = max(_float(state.get("initial_cash"), PAPER_INITIAL_CASH), 1e-9)
    drawdown_fraction = _float(state.get("max_drawdown")) / initial
    reasons = []
    if elapsed_days < PROMOTION_MIN_DAYS:
        reasons.append(f"관찰 {elapsed_days:.1f}일 < {PROMOTION_MIN_DAYS}일")
    if _int(state.get("fills")) < PROMOTION_MIN_FILLS:
        reasons.append(f"maker fill {_int(state.get('fills'))} < {PROMOTION_MIN_FILLS}")
    if _int(state.get("sell_fills")) < PROMOTION_MIN_SELL_FILLS:
        reasons.append(
            f"maker sell fill {_int(state.get('sell_fills'))} < {PROMOTION_MIN_SELL_FILLS}"
        )
    if _int(state.get("pair_cycles")) < PROMOTION_MIN_PAIRS:
        reasons.append(f"pair cycle {_int(state.get('pair_cycles'))} < {PROMOTION_MIN_PAIRS}")
    if _float(state.get("realized_pnl")) < PROMOTION_MIN_REALIZED:
        reasons.append(
            f"실현손익 ${_float(state.get('realized_pnl')):+.2f} < ${PROMOTION_MIN_REALIZED:.2f}"
        )
    if drawdown_fraction > PROMOTION_MAX_DRAWDOWN:
        reasons.append(
            f"최대낙폭 {drawdown_fraction:.1%} > {PROMOTION_MAX_DRAWDOWN:.1%}"
        )
    maker_profit = max(_float(state.get("maker_spread_pnl")), 0.0)
    rebalance_loss = max(-_float(state.get("rebalance_pnl")), 0.0)
    rebalance_loss_share = rebalance_loss / max(maker_profit, 1e-9)
    if rebalance_loss > 0 and rebalance_loss_share > PROMOTION_MAX_REBALANCE_LOSS_SHARE:
        reasons.append(
            f"재균형손실/메이커이익 {rebalance_loss_share:.0%} > "
            f"{PROMOTION_MAX_REBALANCE_LOSS_SHARE:.0%}"
        )
    approved = not reasons and equity > initial
    return {
        "approved": approved,
        "paper_only": not approved,
        "elapsed_days": elapsed_days,
        "fills": _int(state.get("fills")),
        "buy_fills": _int(state.get("buy_fills")),
        "sell_fills": _int(state.get("sell_fills")),
        "pair_cycles": _int(state.get("pair_cycles")),
        "maker_spread_pnl": _float(state.get("maker_spread_pnl")),
        "rebalance_pnl": _float(state.get("rebalance_pnl")),
        "rebalance_loss_share": rebalance_loss_share,
        "realized_pnl": _float(state.get("realized_pnl")),
        "equity": equity,
        "drawdown_fraction": drawdown_fraction,
        "reasons": reasons,
        "updated_at": now_kst(),
    }


def _reserved_order_usd(state: dict[str, Any]) -> float:
    return sum(
        _float(order.get("price")) * _float(order.get("remaining"))
        for order in (state.get("orders") or {}).values()
        if str(order.get("side") or "BUY").upper() == "BUY"
    )


def _cancel_inactive_quotes(state: dict[str, Any]) -> int:
    active = set(state.get("selected_conditions") or [])
    canceled = 0
    for key, order in list((state.get("orders") or {}).items()):
        if str(order.get("condition_id")) in active:
            continue
        append_jsonl(JOURNAL_FILE, {
            **order, "event": "paper_quote_canceled", "at": now_kst(),
            "reason": "market_deselected",
        })
        state["orders"].pop(key, None)
        canceled += 1
    return canceled


def settle_resolved_market(state: dict[str, Any], market: dict[str, Any]) -> bool:
    if not any(_float(v) > 0 for v in market.get("inventory") or []):
        return False
    try:
        row = _get_json(f"{GAMMA_API}/markets/{market['gamma_market_id']}", {})
    except Exception:
        return False
    if not row.get("closed"):
        return False
    prices = _market_prices(row)
    winner = next((index for index, price in enumerate(prices) if price >= 0.95), None)
    if winner is None:
        return False
    inventory = [_float(v) for v in market.get("inventory") or [0, 0]]
    costs = [_float(v) for v in market.get("inventory_cost") or [0, 0]]
    payout = inventory[winner]
    pnl = payout - sum(costs)
    state["cash"] = _float(state.get("cash")) + payout
    state["realized_pnl"] = _float(state.get("realized_pnl")) + pnl
    market["realized_pnl"] = _float(market.get("realized_pnl")) + pnl
    market["inventory"] = [0.0, 0.0]
    market["inventory_cost"] = [0.0, 0.0]
    market["inventory_since_ts"] = [0.0, 0.0]
    market["active"] = False
    for outcome in (0, 1):
        for side in ("BUY", "SELL"):
            state.get("orders", {}).pop(
                _order_key(market["condition_id"], outcome, side), None
            )
    append_jsonl(JOURNAL_FILE, {
        "event": "paper_market_resolved", "at": now_kst(),
        "condition_id": market["condition_id"], "title": market.get("title"),
        "winner": winner, "payout": round(payout, 6),
        "cost": round(sum(costs), 6), "pnl_usd": round(pnl, 6),
    })
    return True


def build_report(state: dict[str, Any]) -> str:
    promotion = state.get("promotion") or {}
    active = [
        market for market in (state.get("markets") or {}).values()
        if market.get("active")
    ]
    inventory_cost = _total_inventory_cost(state)
    stream = (state.get("last_scan") or {}).get("stream") or {}
    lines = [
        f"🏦 <b>[Polymarket Maker Mirror V2 PAPER]</b> — {datetime.now().strftime('%m/%d %H:%M')}",
        f"• Equity ${_float(state.get('equity')):.2f} | "
        f"실현 ${_float(state.get('realized_pnl')):+.2f} | "
        f"재고원가 ${inventory_cost:.2f}",
        f"• maker fill {_int(state.get('fills'))} "
        f"(BUY {_int(state.get('buy_fills'))}/SELL {_int(state.get('sell_fills'))}) | "
        f"pair {_int(state.get('pair_cycles'))}회/{_float(state.get('paired_shares')):.1f}주 | "
        f"split {_float(state.get('split_shares')):.1f}주",
        f"• maker PnL ${_float(state.get('maker_spread_pnl')):+.2f} | "
        f"rebalance ${_float(state.get('rebalance_pnl')):+.2f} "
        f"({_int(state.get('rebalance_count'))}회)",
        f"• 선택시장 {len(active)} | resting paper quotes {len(state.get('orders') or {})}",
        f"• WS event {_int(stream.get('event_count')):,} | trade {_int(stream.get('trade_events')):,} | "
        f"lag {_float(stream.get('lag_ms')):.0f}ms",
        f"• 누적 quote {_int(state.get('quote_placements')):,} / cancel "
        f"{_int(state.get('quote_cancellations')):,}",
        f"• 경제성탐색 {_int(state.get('last_discovery_universe'))} | "
        f"판단주기 {DAEMON_INTERVAL:.1f}s | feed {state.get('last_scan', {}).get('data_source', '-')}",
        f"• LIVE 승급: {'통과' if promotion.get('approved') else '대기'}",
    ]
    for reason in (promotion.get("reasons") or [])[:4]:
        lines.append(f"  - {html.escape(str(reason))}")
    for market in active[:3]:
        inventory = market.get("inventory") or [0, 0]
        lines.append(
            f"• {html.escape(str(market.get('title') or '')[:60])} | "
            f"inv {inventory[0]:.1f}/{inventory[1]:.1f} | "
            f"PnL ${_float(market.get('realized_pnl')):+.2f} | "
            f"MM고래 {_int(market.get('maker_wallet_count'))}"
        )
    return "\n".join(lines)


def build_event_alert(state: dict[str, Any], *, fills: int, merges: int,
                      exits: int, resolved: int, splits: int = 0) -> str:
    lines = [
        f"⚡ <b>[Polymarket Maker Mirror V2 이벤트]</b> — {datetime.now().strftime('%m/%d %H:%M:%S')}",
        f"• maker fill {fills} | split {splits} | merge {merges} | "
        f"재균형 {exits} | 정산 {resolved}",
        f"• Equity ${_float(state.get('equity')):.2f} | "
        f"실현 ${_float(state.get('realized_pnl')):+.2f} | "
        f"누적 BUY/SELL {_int(state.get('buy_fills'))}/{_int(state.get('sell_fills'))}",
    ]
    inventory_markets = [
        market for market in (state.get("markets") or {}).values()
        if any(_float(value) > 0 for value in market.get("inventory") or [])
    ]
    for market in inventory_markets[:3]:
        inventory = market.get("inventory") or [0, 0]
        lines.append(
            f"• {html.escape(str(market.get('title') or '')[:55])} | "
            f"inv {inventory[0]:.2f}/{inventory[1]:.2f}"
        )
    return "\n".join(lines)


def run_once(*, report_now: bool = False) -> dict[str, Any]:
    lock_name = "polymarket_mm"
    if not try_acquire(lock_name):
        return {"ok": False, "skipped": "already_running"}
    try:
        state = _load_state()
        discovery_due, discovery_error = apply_discovery_snapshot(state)
        if discovery_error:
            state["last_discovery_error"] = discovery_error

        canceled = _cancel_inactive_quotes(state)
        relevant = [
            market for market in (state.get("markets") or {}).values()
            if market.get("active") or any(_float(v) > 0 for v in market.get("inventory") or [])
        ]
        books_by_condition, all_trades, stream_stats = load_stream_market_data(relevant)
        missing = [
            market for market in relevant
            if market["condition_id"] not in books_by_condition
        ]
        scan_errors = []
        data_source = "websocket"
        if missing:
            rest_books, rest_trades, rest_errors = fetch_rest_market_data(missing)
            books_by_condition.update(rest_books)
            all_trades.extend(rest_trades)
            scan_errors.extend(rest_errors)
            data_source = "websocket+rest" if stream_stats["fresh"] else "rest_fallback"

        fills = process_paper_trades(state, all_trades)
        merges = 0
        exits = 0
        resolved = 0
        for market in relevant:
            condition = market["condition_id"]
            books = books_by_condition.get(condition)
            if not books:
                continue
            keep_inventory = (
                _float(market.get("target_inventory"), TARGET_INVENTORY_SHARES)
                if market.get("active") else 0.0
            )
            if merge_complete_sets(
                state, market, keep_shares=keep_inventory,
                count_performance=bool(market.get("active")),
            ) != 0:
                merges += 1
            if exit_stale_inventory(state, market, books) != 0:
                exits += 1
            if not market.get("active") and settle_resolved_market(state, market):
                resolved += 1

        placed = 0
        splits = 0
        for market in relevant:
            if not market.get("active"):
                continue
            books = books_by_condition.get(market["condition_id"])
            if not books:
                continue
            if ensure_split_inventory(state, market, books) > 0:
                splits += 1
            current_reserved = sum(
                _float(order.get("price")) * _float(order.get("remaining"))
                for order in (state.get("orders") or {}).values()
                if order.get("condition_id") == market["condition_id"]
                and str(order.get("side") or "BUY").upper() == "BUY"
            )
            committed = (
                _total_inventory_cost(state) + _reserved_order_usd(state)
                - current_reserved
            )
            targets = quote_targets(
                market, books, cash=_float(state.get("cash")), total_committed=committed
            )
            new_placed, new_canceled = _replace_paper_quotes(state, market, targets)
            placed += new_placed
            canceled += new_canceled

        state["quote_placements"] = _int(state.get("quote_placements")) + placed
        state["quote_cancellations"] = _int(state.get("quote_cancellations")) + canceled

        equity = mark_equity(state, books_by_condition)
        state["cycles"] = _int(state.get("cycles")) + 1
        state["promotion"] = promotion_status(state)
        # Paper 검증을 통과하기 전에는 MM live flag가 설정돼도 실제 제출하지 않는다.
        live_requested = mm_live_enabled()
        state["mode"] = "live_ready_manual_review" if (
            live_requested and state["promotion"]["approved"]
        ) else "paper"
        state["live_execution_started"] = False
        state["live_execution_block_reason"] = (
            "manual review required after paper promotion"
            if live_requested and state["promotion"]["approved"] else
            "; ".join(state["promotion"].get("reasons") or ["MM live flag disabled"])
        )
        state["last_scan"] = {
            "at": now_kst(), "selected_markets": len(state.get("selected_conditions") or []),
            "books": len(books_by_condition), "trades_seen": len(all_trades),
            "fills": fills, "buy_fills_total": _int(state.get("buy_fills")),
            "sell_fills_total": _int(state.get("sell_fills")),
            "splits": splits, "merges": merges, "inventory_exits": exits,
            "resolved": resolved, "quotes_placed": placed, "quotes_canceled": canceled,
            "resting_quotes": len(state.get("orders") or {}), "equity": equity,
            "discovery_due": discovery_due, "discovery_error": discovery_error,
            "scan_errors": scan_errors[:5], "mode": state["mode"],
            "data_source": data_source, "stream": stream_stats,
            "cycles": state["cycles"],
        }
        report_due = report_now or (
            time.time() - _float(state.get("last_report_ts")) >= REPORT_INTERVAL_SECONDS
        )
        if report_due:
            try:
                from publisher import send_review
                if send_review(build_report(state)):
                    state["last_report_ts"] = time.time()
            except Exception:
                pass
        _atomic_save_json(STATE_FILE, state)
        if fills or splits or merges or exits or resolved:
            try:
                from publisher import send_review
                send_review(build_event_alert(
                    state, fills=fills, splits=splits, merges=merges,
                    exits=exits, resolved=resolved
                ))
            except Exception:
                pass
        return {"ok": True, **state["last_scan"], "promotion": state["promotion"]}
    finally:
        release(lock_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--report-now", action="store_true")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--write-discovery", action="store_true")
    parser.add_argument("--discovery-daemon", action="store_true")
    args = parser.parse_args(argv)
    if args.discovery_daemon:
        while True:
            result = write_discovery_snapshot()
            if args.json:
                print(json.dumps({
                    "ok": result.get("ok"),
                    "generated_at": result.get("generated_at"),
                    "duration_seconds": result.get("duration_seconds"),
                    "candidate_count": len(result.get("candidates") or []),
                    "rejections": result.get("rejections"),
                    "error": result.get("error", ""),
                }, ensure_ascii=False), flush=True)
            time.sleep(max(DISCOVERY_WORKER_PAUSE, 1.0))
    if args.daemon:
        while True:
            started = time.monotonic()
            result: dict[str, Any] = {}
            try:
                result = run_once(report_now=args.report_now)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                print(json.dumps({"ok": False, "error": str(exc)[:500]}), flush=True)
            target_interval = (
                DAEMON_INTERVAL
                if isinstance(result, dict) and result.get("data_source") == "websocket"
                else max(DAEMON_INTERVAL, 5.0)
            )
            delay = max(target_interval - (time.monotonic() - started), 0.05)
            time.sleep(delay)
    if args.write_discovery:
        result = write_discovery_snapshot()
    elif args.discover_only:
        candidates, rejects = discover_markets()
        result = {"ok": True, "candidates": candidates, "rejections": rejects}
    else:
        result = run_once(report_now=args.report_now)
    print(json.dumps(result, ensure_ascii=False, default=str) if args.json else result)
    return 0 if result.get("ok") or result.get("skipped") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
