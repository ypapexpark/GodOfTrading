#!/usr/bin/env python3
from __future__ import annotations

"""
Polymarket 고래 지갑 카피트레이딩 paper bot.

PolyBacktest(/Users/ghp/Projects/PolyBacktest)에서 통계적으로 유의미한 트랙레코드를 가진 것으로
확인된 지갑 9명(polymarket_whale_config.json 스냅샷)의 신규 포지션을 감시해서, 실주문 없이
모의매매로 결과를 누적한다.

Runs a read-only simulation:
  1. 추적 지갑들의 신규 체결(trade)을 data-api.polymarket.com/activity에서 폴링,
  2. 지갑별 마켓당 순포지션이 최소 규모를 새로 넘으면 "카피 신호"로 판정,
  3. 슬리피지를 반영한 가상 진입가로 paper position을 기록,
  4. 마켓 종료 후 Gamma API로 결과를 확인해 정산,
  5. 지갑별 롤링 성과가 백테스트 트랙레코드보다 유의미하게 나빠지면 그 지갑 추종을 자동 중단,
  6. 주기적으로 send_review()로 텔레그램 리포트 전송.

No wallet, API key, signing, or live Polymarket order placement is used.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import requests
from dotenv import load_dotenv

from publisher import send_review

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "polymarket_whale_paper_state.json"
JOURNAL_FILE = ROOT / "polymarket_whale_paper_journal.jsonl"
CONFIG_FILE = ROOT / "polymarket_whale_config.json"

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

load_dotenv(ROOT / ".env")

from bot_util import (  # noqa: E402
    KST,
    append_jsonl as _append_jsonl,
    env_bool as _env_bool,
    env_float as _env_float,
    env_int as _env_int,
    json_safe as _json_safe,
    load_json,
    now as _now,
    now_kst as _now_kst,
    read_jsonl as _read_jsonl,
    save_json,
)

INITIAL_BANKROLL = _env_float("POLYMARKET_WHALE_INITIAL_BANKROLL", 1000.0)
BET_FRACTION = _env_float("POLYMARKET_WHALE_BET_FRACTION", 0.02)
COPY_SLIPPAGE = _env_float("POLYMARKET_WHALE_COPY_SLIPPAGE", 0.03)
MIN_NET_USDC = _env_float("POLYMARKET_WHALE_MIN_NET_USDC", 1000.0)
# 고래 순매수가 이 비율×MIN 미만이면 청산 추종 (줄이거나 손절/플립 후 잔량)
EXIT_NET_FRAC = _env_float("POLYMARKET_WHALE_EXIT_NET_FRAC", 0.35)
# 고래의 뒤늦은 축소/반대 체결을 그대로 따라가면 카피 지연과 왕복 슬리피지가
# 겹친다. 기본값은 진입 신호만 카피하고 시장 결과 확정까지 보유한다.
HOLD_TO_RESOLUTION = _env_bool("POLYMARKET_WHALE_HOLD_TO_RESOLUTION", True)
REPORT_INTERVAL_SECONDS = _env_int("POLYMARKET_WHALE_REPORT_INTERVAL", 4 * 3600)
ACTIVITY_POLL_LIMIT = _env_int("POLYMARKET_WHALE_ACTIVITY_LIMIT", 100)
# activity API는 한 번에 최대 500건을 반환한다. 과거 100건 단일 조회는
# 고빈도 지갑이 3분 사이 수백 건을 거래할 때 중간 체결을 영구히 건너뛰었다.
ACTIVITY_PAGE_SIZE = min(
    max(_env_int("POLYMARKET_WHALE_ACTIVITY_PAGE_SIZE", 500), 1), 500
)
ACTIVITY_MAX_OFFSET = _env_int("POLYMARKET_WHALE_ACTIVITY_MAX_OFFSET", 3000)
WHALE_MAX_OPPOSITE_BUY_RATIO = _env_float(
    "POLYMARKET_WHALE_MAX_OPPOSITE_BUY_RATIO", 0.25
)
WHALE_MM_MIN_MARKETS = _env_int("POLYMARKET_WHALE_MM_MIN_MARKETS", 10)
WHALE_MM_MAX_TWO_SIDED_RATE = _env_float(
    "POLYMARKET_WHALE_MM_MAX_TWO_SIDED_RATE", 0.30
)
SUSPEND_MIN_SETTLED = _env_int("POLYMARKET_WHALE_SUSPEND_MIN_SETTLED", 8)
SUSPEND_Z = _env_float("POLYMARKET_WHALE_SUSPEND_Z", -2.5)
REACTIVATE_Z = _env_float("POLYMARKET_WHALE_REACTIVATE_Z", -1.0)
# 2026-07-13 한 달 paired 분석(CLOB 단일시장 조회, payload 639/639):
# - 단일 고래 621시장: WR 67.95%, PnL -$421.49 (평균 승 +$8.43 / 패 -$20)
# - 2번째 고래 티켓: n=40, ROI +12.57%
# - 3번째 고래 티켓: n=3, ROI +15.75% (소표본)
# - 반대방향 충돌 제외 후 2·3번째만: n=40, 37승, PnL +$130.01, ROI +16.25%
# 거래 빈도와 초저가 대박의 우측 꼬리를 유지하면서 합의가 늘수록 증액한다.
# 2026-07-13 사용자 확정: 1명째 $10, 2명째 +$15, 3명째 +$20 (시장 최대 $45).
CONSENSUS_MIN_WHALES = _env_int("POLYMARKET_WHALE_CONSENSUS_MIN", 1)
CONSENSUS_MAX_WHALES = _env_int("POLYMARKET_WHALE_CONSENSUS_MAX", 3)
CONSENSUS_TTL_SECONDS = _env_int(
    "POLYMARKET_WHALE_CONSENSUS_TTL_SECONDS", 7 * 24 * 3600
)
CONSENSUS_TIER_USD = {
    1: _env_float("POLYMARKET_WHALE_TIER1_USD", 10.0),
    2: _env_float("POLYMARKET_WHALE_TIER2_USD", 15.0),
    3: _env_float("POLYMARKET_WHALE_TIER3_USD", 20.0),
}
# 같은 고래가 기존 방향의 상당 부분 이상을 반대 outcome에 새로 쌓으면 단순
# 노이즈/소액 헤지가 아니라 포지션 역전·복구 시도로 본다. 외부 고래 간 방향
# 충돌은 계속 차단하며, 복구 주문도 기존 시장당 $45 한도를 넘지 않는다.
SAME_WHALE_RECOVERY_ENABLED = _env_bool(
    "POLYMARKET_WHALE_SAME_WHALE_RECOVERY_ENABLED", True
)
SAME_WHALE_RECOVERY_MIN_RATIO = _env_float(
    "POLYMARKET_WHALE_RECOVERY_MIN_OPPOSITE_RATIO", 0.75
)
BET_USD = INITIAL_BANKROLL * BET_FRACTION

# Paper v4는 "화면에 보이는 확률"이 아니라 실제 CLOB 주문장에서 체결 가능한
# 가격만 사용한다. 라이브 주문 정책은 이 파일의 paper 전용 게이트를 호출하지
# 않으므로 변경되지 않는다.
PAPER_SIGNAL_POLICY = "paper_executable_edge_v4"
PAPER_REQUIRE_CLOB_BOOK = _env_bool("POLYMARKET_PAPER_REQUIRE_CLOB_BOOK", True)
PAPER_MAX_SIGNAL_AGE_SECONDS = _env_int(
    "POLYMARKET_PAPER_MAX_SIGNAL_AGE_SECONDS", 15 * 60
)
PAPER_EDGE_FILTER_ENABLED = _env_bool("POLYMARKET_PAPER_EDGE_FILTER_ENABLED", True)
PAPER_MIN_ENTRY_EDGE = _env_float("POLYMARKET_PAPER_MIN_ENTRY_EDGE", 0.05)
PAPER_MAX_ENTRY_PRICE = _env_float("POLYMARKET_PAPER_MAX_ENTRY_PRICE", 0.85)
# 같은 지갑이 반대 결과도 선택 방향의 50% 이상 순매수했다면 방향성 고래가
# 아니라 헤지/마켓메이킹일 가능성이 커 paper 신규 진입만 차단한다.
PAPER_MAX_OPPOSITE_RATIO = _env_float(
    "POLYMARKET_PAPER_MAX_OPPOSITE_RATIO", 0.50
)
# 수수료와 REST 폴링→주문 사이의 짧은 가격 변화를 합친 보수적 비용 버퍼.
PAPER_EXECUTION_BUFFER_PCT = _env_float(
    "POLYMARKET_PAPER_EXECUTION_BUFFER_PCT", 0.015
)
PAPER_MAX_COMMITTED_FRACTION = _env_float(
    "POLYMARKET_PAPER_MAX_COMMITTED_FRACTION", 0.40
)
PAPER_POLICY_MIN_SETTLED = _env_int("POLYMARKET_PAPER_POLICY_MIN_SETTLED", 20)
PAPER_POLICY_SUSPEND_ROI = _env_float(
    "POLYMARKET_PAPER_POLICY_SUSPEND_ROI", -0.10
)


def consensus_bet_usd(consensus_rank: int, scale: float = 1.0) -> float:
    """합의 순번별 증분 사이즈. 라이브 soft-cap은 scale만 낮춘다."""
    base = float(CONSENSUS_TIER_USD.get(consensus_rank, 0.0))
    return round(base * max(float(scale), 0.0), 4)


def _load_config() -> dict[str, Any]:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _load_state() -> dict[str, Any]:
    data = load_json(STATE_FILE, default=None)
    if isinstance(data, dict):
        return data
    config = _load_config()
    return {
        "wallets": {
            w["wallet"]: {
                "status": "active",
                "expected_win_rate": w["expected_win_rate"],
                "last_seen_ts": 0,
                "net_usdc": {},  # "{market_id}:{outcome_index}" -> 현재 관측된 순매수 USDC
                "signaled": {},  # 이미 카피 신호를 낸 포지션 키 (중복 신호 방지)
                "live_wins": 0,  # 라이브 추종 시작 이후 누적 승 (중단 중엔 그림자추적으로 계속 갱신)
                "live_n": 0,     # 라이브 추종 시작 이후 누적 정산 건수
            }
            for w in config["whales"]
        },
        "open_positions": [],
        "bankroll": INITIAL_BANKROLL,
        "last_report_time": 0.0,
        "last_scan": {},
    }


def _save_state(state: dict[str, Any]) -> None:
    save_json(STATE_FILE, state)


def _get_json(url: str, params: dict[str, Any], timeout: int = 15) -> Any:
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _fetch_wallet_activity(wallet: str, since_ts: int) -> list[dict[str, Any]]:
    """마지막 커서 이후 activity를 오래된 순서로 완전 수집한다.

    단일 ``limit=100`` 최신 조회는 활발한 지갑에서 가장 최근 100건만 남기고
    그 앞의 체결을 잃는다. start/end+ASC+offset 페이지를 모두 성공한 경우에만
    반환한다. API offset 상한까지 찬 경우에는 이미 받은 마지막 timestamp부터
    새 chunk를 이어 받아 큰 구간을 중복 재조회하지 않는다.
    """
    start_ts = max(int(since_ts), 0)
    end_ts = max(int(_now()), start_ts)
    cursor = start_ts
    rows_by_fingerprint: dict[str, dict[str, Any]] = {}
    max_chunks = 1000

    for _chunk in range(max_chunks):
        chunk: list[dict[str, Any]] = []
        offset = 0
        while offset <= ACTIVITY_MAX_OFFSET:
            rows = _get_json(
                f"{DATA_API}/activity",
                {
                    "user": wallet,
                    "start": cursor,
                    "end": end_ts,
                    "limit": ACTIVITY_PAGE_SIZE,
                    "offset": offset,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "ASC",
                },
            )
            if not isinstance(rows, list):
                raise ValueError("activity response is not a list")
            chunk.extend(rows)
            for row in rows:
                rows_by_fingerprint[_activity_fingerprint(row)] = row
            if len(rows) < ACTIVITY_PAGE_SIZE:
                return sorted(
                    rows_by_fingerprint.values(),
                    key=lambda row: int(row.get("timestamp") or 0),
                )
            offset += ACTIVITY_PAGE_SIZE

        # 다음 chunk는 마지막으로 받은 초를 포함해 다시 시작하고 fingerprint로
        # 경계 중복을 제거한다. 한 초에 offset 한도 이상이 몰려 진전이 없을 때만
        # 부분 결과로 커서를 전진시키지 않고 안전하게 실패한다.
        next_cursor = max(
            (int(row.get("timestamp") or 0) for row in chunk),
            default=cursor,
        )
        if next_cursor <= cursor:
            raise RuntimeError(
                "activity pagination overflow within one timestamp: "
                f"{cursor}"
            )
        cursor = next_cursor

    raise RuntimeError(f"activity pagination exceeded {max_chunks} chunks")


def _activity_fingerprint(row: dict[str, Any]) -> str:
    """초 단위 timestamp 경계에서 같은 체결을 재처리하지 않는 안정 키."""
    return ":".join(
        str(row.get(key) or "")
        for key in (
            "transactionHash", "timestamp", "type", "side", "asset", "size",
            "usdcSize", "conditionId", "outcomeIndex",
        )
    )


def _wallet_directional_classification(wstate: dict[str, Any]) -> dict[str, Any]:
    markets = wstate.get("market_flow_v2") or {}
    rows = [row for row in markets.values() if isinstance(row, dict)]
    two_sided = 0
    for row in rows:
        buy0 = max(float(row.get("buy_0") or 0), 0.0)
        buy1 = max(float(row.get("buy_1") or 0), 0.0)
        high = max(buy0, buy1)
        ratio = min(buy0, buy1) / high if high > 0 else 0.0
        if ratio >= WHALE_MAX_OPPOSITE_BUY_RATIO:
            two_sided += 1
    rate = two_sided / len(rows) if rows else 0.0
    market_maker_like = (
        len(rows) >= WHALE_MM_MIN_MARKETS
        and rate >= WHALE_MM_MAX_TWO_SIDED_RATE
    )
    result = {
        "markets": len(rows),
        "two_sided_markets": two_sided,
        "two_sided_rate": round(rate, 6),
        "market_maker_like": market_maker_like,
        "updated_at": _now_kst(),
    }
    wstate["classification_v2"] = result
    return result


def _fetch_market_state(condition_id: str | None = None, gamma_market_id: str | None = None) -> dict[str, Any] | None:
    if gamma_market_id:
        try:
            return _get_json(f"{GAMMA_API}/markets/{gamma_market_id}", {})
        except Exception:
            return None
    return None


def _current_price(market: dict[str, Any], outcome_index: int) -> float | None:
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        return float(prices[outcome_index])
    except Exception:
        return None


def _resolved_outcome(market: dict[str, Any]) -> int | None:
    """종료된 마켓이면 승리한 outcome_index 반환, 아니면 None."""
    if not market.get("closed"):
        return None
    try:
        prices = [float(p) for p in json.loads(market.get("outcomePrices", "[]"))]
    except Exception:
        return None
    for idx, p in enumerate(prices):
        if p >= 0.95:
            return idx
    return None


def scan_wallets(
    state: dict[str, Any], *, include_suspended: bool = True,
    block_market_maker_wallets: bool = True,
    repeat_directional_steps: bool = False,
    parallel_fetch: bool = False,
) -> list[dict[str, Any]]:
    """추적 지갑들의 신규 체결을 확인하고, 새로 임계치를 넘은 포지션을 신호로 반환.

    중단(suspended)된 지갑도 스캔은 계속한다 — 실제 카피는 안 하지만 그림자추적으로
    성과가 회복되는지 계속 관찰해서 자동 재활성화 판단에 쓴다 (open_paper_positions에서
    is_shadow로 구분).
    """
    signals = []
    eligible: list[tuple[str, dict[str, Any]]] = []
    for wallet, wstate in state["wallets"].items():
        if not include_suspended and wstate.get("status") != "active":
            wstate.pop("last_error", None)
            wstate["live_scan_skipped_at"] = _now_kst()
            continue
        eligible.append((wallet, wstate))

    activity_by_wallet: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, Exception] = {}
    if parallel_fetch and len(eligible) > 1:
        with ThreadPoolExecutor(max_workers=min(len(eligible), 8)) as pool:
            futures = {
                pool.submit(
                    _fetch_wallet_activity,
                    wallet,
                    int(wstate.get("last_seen_ts") or 0),
                ): wallet
                for wallet, wstate in eligible
            }
            for future in as_completed(futures):
                wallet = futures[future]
                try:
                    activity_by_wallet[wallet] = future.result()
                except Exception as exc:
                    errors[wallet] = exc

    for wallet, wstate in eligible:
        since_ts = int(wstate.get("last_seen_ts") or 0)
        if wallet in errors:
            wstate["last_error"] = str(errors[wallet])
            continue
        try:
            activity = (
                activity_by_wallet.get(wallet, [])
                if parallel_fetch
                else _fetch_wallet_activity(wallet, since_ts)
            )
        except Exception as exc:
            wstate["last_error"] = str(exc)
            continue
        wstate.pop("last_error", None)

        if not activity:
            continue

        activity.sort(key=lambda r: r.get("timestamp", 0))
        seen = set(str(v) for v in wstate.get("activity_seen_v2") or [])
        new_seen: list[str] = []
        candidates: dict[str, dict[str, Any]] = {}
        candidate_levels: dict[str, int] = {}
        market_flow = wstate.setdefault("market_flow_v2", {})
        net_shares = wstate.setdefault("net_shares_v5", {})
        signal_levels = wstate.setdefault("directional_signal_levels_v5", {})
        for r in activity:
            fingerprint = _activity_fingerprint(r)
            trade_ts = int(r.get("timestamp", 0) or 0)
            # 최초 v2 실행에서는 기존 초의 행을 다시 더하지 않는다. 이후에는
            # 같은 초에 늦게 도착한 새 fingerprint만 받아들인다.
            if fingerprint in seen or (
                not seen and trade_ts <= since_ts
            ):
                continue
            new_seen.append(fingerprint)
            trade_ts = int(r.get("timestamp", 0) or 0)
            wstate["last_seen_ts"] = max(
                int(wstate.get("last_seen_ts") or 0), trade_ts
            )
            if r.get("type") != "TRADE":
                continue
            market_id = str(r.get("conditionId") or "")
            outcome_index = r.get("outcomeIndex")
            if market_id == "" or outcome_index is None:
                continue
            try:
                outcome_index = int(outcome_index)
            except (TypeError, ValueError):
                continue
            # Polymarket의 거래 가능한 이진 outcome만 신호로 사용한다.
            # 999는 redeem/집계용 sentinel이라 consensus에 들어가면 정상 방향을
            # 반대 신호로 오인할 수 있다.
            if outcome_index not in {0, 1}:
                continue
            key = f"{market_id}:{outcome_index}"
            sign = 1 if r.get("side") == "BUY" else -1
            usdc = float(r.get("usdcSize") or (float(r.get("size", 0)) * float(r.get("price", 0))))
            net = wstate["net_usdc"].get(key, 0.0) + sign * usdc
            wstate["net_usdc"][key] = net
            shares = max(float(r.get("size") or 0), 0.0)
            net_shares[key] = float(net_shares.get(key) or 0) + sign * shares
            flow = market_flow.setdefault(
                market_id,
                {"buy_0": 0.0, "buy_1": 0.0, "updated_ts": trade_ts},
            )
            if r.get("side") == "BUY":
                flow[f"buy_{outcome_index}"] = (
                    float(flow.get(f"buy_{outcome_index}") or 0) + usdc
                )
            flow["updated_ts"] = trade_ts
            flow["title"] = r.get("title", "")

            # 같은 폴링 배치의 반대 outcome까지 모두 반영한 뒤 방향성을 판정한다.
            if repeat_directional_steps:
                previous_level = int(signal_levels.get(key) or 0)
                reached_level = int(max(net, 0.0) // max(MIN_NET_USDC, 1e-9))
                if reached_level > previous_level:
                    candidates[key] = r
                    candidate_levels[key] = reached_level
            elif net >= MIN_NET_USDC and not wstate["signaled"].get(key):
                candidates[key] = r
                candidate_levels[key] = 1

        if new_seen:
            wstate["activity_seen_v2"] = (list(seen) + new_seen)[-2000:]
        # 오래된 시장 흐름은 지갑 분류 표본을 무한히 키우지 않게 최근 200개만 유지.
        if len(market_flow) > 200:
            keep = sorted(
                market_flow,
                key=lambda k: int((market_flow.get(k) or {}).get("updated_ts") or 0),
                reverse=True,
            )[:200]
            wstate["market_flow_v2"] = market_flow = {
                key: market_flow[key] for key in keep
            }
        classification = _wallet_directional_classification(wstate)

        for key, r in candidates.items():
            market_id = str(r.get("conditionId") or "")
            outcome_index = int(r.get("outcomeIndex") or 0)
            flow = market_flow.get(market_id) or {}
            selected_buy = max(float(flow.get(f"buy_{outcome_index}") or 0), 0.0)
            opposite_buy = max(
                float(flow.get(f"buy_{1 - outcome_index}") or 0), 0.0
            )
            opposite_ratio = opposite_buy / selected_buy if selected_buy > 0 else 0.0
            # 이 시장은 이후에도 반복 신호를 내지 않는다. 양방향/마켓메이커
            # 판정이 난 거래를 나중에 방향성 거래로 오인하는 것을 막는다.
            wstate["signaled"][key] = True
            if repeat_directional_steps:
                signal_levels[key] = int(candidate_levels.get(key) or 1)
            if (
                (
                    block_market_maker_wallets
                    and classification.get("market_maker_like")
                )
                or opposite_ratio >= WHALE_MAX_OPPOSITE_BUY_RATIO
            ):
                wstate["directional_blocks_v2"] = int(
                    wstate.get("directional_blocks_v2") or 0
                ) + 1
                continue
            signals.append({
                "wallet": wallet,
                "gamma_market_id": None,
                "condition_id": market_id,
                "outcome_index": outcome_index,
                "net_usdc": float(wstate["net_usdc"].get(key) or 0),
                "net_shares": float(net_shares.get(key) or 0),
                "signal_level": int(candidate_levels.get(key) or 1),
                "title": r.get("title", ""),
                "slug": r.get("slug", ""),
                "detected_at": _now_kst(),
                "detected_ts": _now(),
                "source_trade_ts": int(r.get("timestamp", 0) or 0),
                "source_trade_price": float(r.get("price") or 0),
                "source_trade_usdc": float(r.get("usdcSize") or 0),
                "opposite_buy_usdc": opposite_buy,
                "opposite_buy_ratio": opposite_ratio,
                "wallet_classification": classification,
                "kind": "enter",
            })
    return signals


def _gamma_market_by_condition(condition_id: str) -> dict[str, Any] | None:
    try:
        rows = _get_json(f"{GAMMA_API}/markets", {"condition_ids": condition_id})
        return rows[0] if rows else None
    except Exception:
        return None


def _token_id_for_outcome(
    market: dict[str, Any], outcome_index: int
) -> str | None:
    raw = market.get("clobTokenIds")
    try:
        token_ids = json.loads(raw) if isinstance(raw, str) else raw
        if token_ids and 0 <= outcome_index < len(token_ids):
            return str(token_ids[outcome_index])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _paper_buy_quote(
    market: dict[str, Any], outcome_index: int, usd_amount: float
) -> dict[str, Any]:
    """현재 ask를 순서대로 소진한 paper 전용 실체결 가능 VWAP.

    Gamma 가격은 체결가가 아니라 라이브 FOK 주문의 최대 허용가격과 동일한
    가격 보호선으로만 쓴다. 그 보호선 안에 주문금액 전부를 받을 ask가 없으면
    paper도 미체결로 기록한다.
    """
    display_price = _current_price(market, outcome_index)
    token_id = _token_id_for_outcome(market, outcome_index)
    if display_price is None or display_price <= 0 or display_price >= 1:
        return {"ok": False, "reason": "invalid_gamma_price"}
    max_price = min(display_price * (1 + COPY_SLIPPAGE), 0.999)
    if not token_id:
        return {"ok": False, "reason": "missing_token_id"}
    if usd_amount <= 0:
        return {"ok": False, "reason": "invalid_bet"}

    try:
        book = _get_json(f"{CLOB_API}/book", {"token_id": token_id})
    except Exception as exc:
        if PAPER_REQUIRE_CLOB_BOOK:
            return {
                "ok": False,
                "reason": "clob_book_unavailable",
                "error": str(exc)[:160],
                "token_id": token_id,
                "gamma_price": display_price,
                "max_price": max_price,
            }
        fallback = min(max_price * (1 + PAPER_EXECUTION_BUFFER_PCT), 0.999)
        return {
            "ok": True,
            "token_id": token_id,
            "gamma_price": display_price,
            "max_price": max_price,
            "best_ask": display_price,
            "book_vwap": max_price,
            "entry_price": fallback,
            "shares": usd_amount / fallback,
            "source": "gamma_fallback",
        }

    levels: list[tuple[float, float]] = []
    for row in (book or {}).get("asks") or []:
        try:
            price = float(row.get("price") or 0)
            size = float(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            levels.append((price, size))
    levels.sort(key=lambda item: item[0])
    if not levels:
        return {
            "ok": False,
            "reason": "empty_ask_book",
            "token_id": token_id,
            "gamma_price": display_price,
            "max_price": max_price,
        }

    remaining = float(usd_amount)
    shares = 0.0
    spent = 0.0
    for price, size in levels:
        if price > max_price + 1e-12:
            break
        level_cost = price * size
        take_cost = min(remaining, level_cost)
        if take_cost <= 0:
            continue
        shares += take_cost / price
        spent += take_cost
        remaining -= take_cost
        if remaining <= 1e-8:
            break

    if remaining > max(1e-6, usd_amount * 1e-6) or shares <= 0:
        return {
            "ok": False,
            "reason": "insufficient_asks_within_price_guard",
            "token_id": token_id,
            "gamma_price": display_price,
            "max_price": max_price,
            "best_ask": levels[0][0],
            "fillable_usd": round(spent, 6),
            "requested_usd": usd_amount,
        }

    book_vwap = spent / shares
    all_in_entry = min(book_vwap * (1 + PAPER_EXECUTION_BUFFER_PCT), 0.999)
    if all_in_entry > max_price + 1e-12:
        return {
            "ok": False,
            "reason": "buffered_price_exceeds_guard",
            "token_id": token_id,
            "gamma_price": display_price,
            "max_price": max_price,
            "best_ask": levels[0][0],
            "book_vwap": book_vwap,
            "entry_price": all_in_entry,
        }
    return {
        "ok": True,
        "token_id": token_id,
        "gamma_price": display_price,
        "max_price": max_price,
        "best_ask": levels[0][0],
        "book_vwap": book_vwap,
        "entry_price": all_in_entry,
        "shares": usd_amount / all_in_entry,
        "source": "clob_ask_vwap",
    }


def _paper_entry_quality(
    state: dict[str, Any], sig: dict[str, Any], entry_price: float
) -> tuple[bool, str, dict[str, float]]:
    """Paper 연구용 신호 품질 게이트. 라이브 주문 로직에는 적용되지 않는다."""
    now_ts = _now()
    source_ts = float(sig.get("source_trade_ts") or sig.get("detected_ts") or now_ts)
    signal_age = max(now_ts - source_ts, 0.0)
    if signal_age > PAPER_MAX_SIGNAL_AGE_SECONDS:
        return False, "stale_signal", {"signal_age_seconds": signal_age}

    wstate = (state.get("wallets") or {}).get(str(sig.get("wallet") or "")) or {}
    expected = float(wstate.get("expected_win_rate") or 0.5)
    if PAPER_EDGE_FILTER_ENABLED:
        max_allowed = min(PAPER_MAX_ENTRY_PRICE, expected - PAPER_MIN_ENTRY_EDGE)
        if max_allowed <= 0 or entry_price > max_allowed:
            return False, "insufficient_price_edge", {
                "entry_price": entry_price,
                "expected_win_rate": expected,
                "max_allowed_price": max_allowed,
                "signal_age_seconds": signal_age,
            }

    condition = str(sig.get("condition_id") or "")
    outcome = int(sig.get("outcome_index") or 0)
    selected_key = f"{condition}:{outcome}"
    nets = wstate.get("net_usdc") or {}
    selected_net = max(float(nets.get(selected_key) or sig.get("net_usdc") or 0), 0.0)
    opposite_net = 0.0
    for key, value in nets.items():
        prefix, _, raw_outcome = str(key).rpartition(":")
        if prefix != condition:
            continue
        try:
            other_outcome = int(raw_outcome)
        except ValueError:
            continue
        if other_outcome != outcome:
            opposite_net += max(float(value or 0), 0.0)
    hedge_ratio = opposite_net / selected_net if selected_net > 0 else 0.0
    if selected_net > 0 and hedge_ratio >= PAPER_MAX_OPPOSITE_RATIO:
        return False, "two_sided_whale_exposure", {
            "selected_net_usdc": selected_net,
            "opposite_net_usdc": opposite_net,
            "opposite_ratio": hedge_ratio,
            "signal_age_seconds": signal_age,
        }
    return True, "", {
        "entry_price": entry_price,
        "expected_win_rate": expected,
        "selected_net_usdc": selected_net,
        "opposite_net_usdc": opposite_net,
        "opposite_ratio": hedge_ratio,
        "signal_age_seconds": signal_age,
    }


def _paper_risk_ok(state: dict[str, Any], bet_usd: float) -> tuple[bool, str]:
    raw_bankroll = state.get("policy_bankroll")
    policy_bankroll = (
        INITIAL_BANKROLL if raw_bankroll is None else float(raw_bankroll)
    )
    committed = sum(
        float(pos.get("bet_usd") or 0)
        for pos in state.get("open_positions") or []
        if pos.get("signal_policy") == PAPER_SIGNAL_POLICY
        and not pos.get("is_shadow")
    )
    if policy_bankroll <= 0:
        return False, "policy_bankroll_depleted"
    if committed + bet_usd > policy_bankroll * PAPER_MAX_COMMITTED_FRACTION:
        return False, "paper_committed_cap"
    return True, ""


def _append_paper_block(
    sig: dict[str, Any], reason: str, **details: Any
) -> None:
    _append_jsonl(JOURNAL_FILE, {
        "event": "paper_entry_blocked",
        "signal_policy": PAPER_SIGNAL_POLICY,
        "reason": reason,
        "wallet": sig.get("wallet"),
        "condition_id": sig.get("condition_id"),
        "outcome_index": sig.get("outcome_index"),
        "title": sig.get("title"),
        "at": _now_kst(),
        **details,
    })


def _ensure_paper_policy_state(state: dict[str, Any]) -> None:
    """오염된 legacy 장부를 보존하면서 v4 검증 성과를 새 원금에서 분리한다."""
    if state.get("paper_policy") == PAPER_SIGNAL_POLICY:
        return
    legacy_bankroll = float(state.get("bankroll") or INITIAL_BANKROLL)
    state["paper_policy"] = PAPER_SIGNAL_POLICY
    state["paper_policy_started_at"] = _now_kst()
    state["paper_policy_started_ts"] = _now()
    state["legacy_bankroll_at_v4"] = legacy_bankroll
    state["policy_bankroll"] = INITIAL_BANKROLL
    for wstate in (state.get("wallets") or {}).values():
        wstate["legacy_status_at_v4"] = wstate.get("status", "active")
        wstate["status"] = "active"
        wstate["policy_n"] = 0
        wstate["policy_wins"] = 0
        wstate["policy_pnl"] = 0.0
        wstate["policy_bet"] = 0.0
    _append_jsonl(JOURNAL_FILE, {
        "event": "paper_policy_started",
        "signal_policy": PAPER_SIGNAL_POLICY,
        "at": state["paper_policy_started_at"],
        "legacy_bankroll": legacy_bankroll,
        "policy_bankroll": INITIAL_BANKROLL,
    })


def _pos_key(pos: dict[str, Any]) -> str:
    return f"{pos.get('condition_id')}:{pos.get('outcome_index')}"


def register_consensus_signal(
    state: dict[str, Any], sig: dict[str, Any]
) -> tuple[int, list[str]]:
    """시장·방향별 고유 고래 합의를 누적하고 현재 합의 순번을 반환."""
    now_ts = _now()
    condition = str(sig.get("condition_id") or "")
    outcome = int(sig.get("outcome_index") or 0)
    key = f"{condition}:{outcome}"
    book = state.setdefault("consensus_candidates", {})
    for old_key, old_record in list(book.items()):
        updated_ts = float(old_record.get("updated_ts") or 0)
        if updated_ts and now_ts - updated_ts > CONSENSUS_TTL_SECONDS:
            book.pop(old_key, None)
    record = book.setdefault(
        key,
        {"wallets": [], "first_seen_at": _now_kst(), "first_seen_ts": now_ts},
    )
    wallets = {str(w) for w in record.get("wallets") or [] if w}
    # 재시작/정책 전환 전 포지션도 이미 확보한 확인 신호로 포함한다.
    for pos in state.get("open_positions") or []:
        if (
            pos.get("condition_id") == condition
            and int(pos.get("outcome_index") or 0) == outcome
        ):
            wallets.update(str(w) for w in pos.get("consensus_wallets") or [] if w)
            if pos.get("wallet"):
                wallets.add(str(pos.get("wallet")))
    if sig.get("wallet"):
        wallets.add(str(sig.get("wallet")))
    record["wallets"] = sorted(wallets)
    record["updated_at"] = _now_kst()
    record["updated_ts"] = now_ts
    record["title"] = sig.get("title") or record.get("title")
    return len(wallets), record["wallets"]


def opposite_consensus_wallets(
    state: dict[str, Any], sig: dict[str, Any]
) -> list[str]:
    """같은 시장의 반대 방향 고래가 이미 관측됐으면 그 지갑 목록을 반환."""
    condition = str(sig.get("condition_id") or "")
    outcome = int(sig.get("outcome_index") or 0)
    wallets: set[str] = set()
    for key, record in (state.get("consensus_candidates") or {}).items():
        prefix, _, raw_outcome = str(key).rpartition(":")
        if prefix != condition:
            continue
        try:
            other_outcome = int(raw_outcome)
        except ValueError:
            continue
        if other_outcome != outcome:
            wallets.update(str(w) for w in record.get("wallets") or [] if w)
    return sorted(wallets)


def same_whale_recovery_context(
    state: dict[str, Any],
    sig: dict[str, Any],
    existing_market: list[dict[str, Any]],
) -> dict[str, Any]:
    """같은 고래가 반대편 순노출을 충분히 키운 복구/역전 신호인지 판정한다."""
    wallet = str(sig.get("wallet") or "")
    condition = str(sig.get("condition_id") or "")
    outcome = int(sig.get("outcome_index") or 0)
    opposite_positions = [
        pos for pos in existing_market
        if str(pos.get("wallet") or "") == wallet
        and int(pos.get("outcome_index") or 0) != outcome
    ]
    wstate = (state.get("wallets") or {}).get(wallet) or {}
    net_usdc = wstate.get("net_usdc") or {}
    new_net = max(float(net_usdc.get(f"{condition}:{outcome}") or 0), 0.0)
    previous_net = max(
        (
            max(
                float(net_usdc.get(
                    f"{condition}:{int(pos.get('outcome_index') or 0)}"
                ) or 0),
                0.0,
            )
            for pos in opposite_positions
        ),
        default=0.0,
    )
    ratio = new_net / previous_net if previous_net > 0 else 0.0
    eligible = (
        SAME_WHALE_RECOVERY_ENABLED
        and bool(opposite_positions)
        and new_net >= MIN_NET_USDC
        and previous_net > 0
        and ratio >= SAME_WHALE_RECOVERY_MIN_RATIO
    )
    return {
        "eligible": eligible,
        "opposite_positions": opposite_positions,
        "new_net_usdc": new_net,
        "previous_net_usdc": previous_net,
        "opposite_to_previous_ratio": ratio,
    }


def same_whale_recovery_bet(
    existing_market: list[dict[str, Any]],
    recovery_context: dict[str, Any],
    max_entry_price: float,
    *,
    scale: float = 1.0,
) -> dict[str, Any]:
    """새 방향 승리 시 기존 같은-고래 손실을 상쇄하는 최소 주문액을 계산한다."""
    positions = recovery_context.get("opposite_positions") or []
    existing_cost = sum(float(pos.get("bet_usd") or 0) for pos in positions)
    existing_shares = sum(float(pos.get("shares_est") or 0) for pos in positions)
    market_committed = sum(float(pos.get("bet_usd") or 0) for pos in existing_market)
    market_cap = sum(float(v) for v in CONSENSUS_TIER_USD.values()) * max(scale, 0.0)
    remaining_cap = max(market_cap - market_committed, 0.0)
    price = min(max(float(max_entry_price), 1e-6), 0.999)
    required = existing_cost * price / (1 - price) if existing_cost > 0 else 0.0
    # 아래로 반올림하면 새 방향이 이겨도 수 센트의 잔여손실이 생기므로 올림한다.
    required = math.ceil(required * 10_000) / 10_000 if required > 0 else 0.0
    total_cost = existing_cost + required
    recovery_win_pnl = required / price - total_cost if required > 0 else 0.0
    original_win_pnl = existing_shares - total_cost
    return {
        "feasible": required > 0 and required <= remaining_cap + 1e-9,
        "bet_usd": required,
        "existing_cost_usd": existing_cost,
        "existing_shares": existing_shares,
        "market_committed_usd": market_committed,
        "market_cap_usd": market_cap,
        "remaining_market_cap_usd": remaining_cap,
        "max_entry_price": price,
        "recovery_win_pnl_est": recovery_win_pnl,
        "original_win_pnl_est": original_win_pnl,
    }


def early_exit_position(
    pos: dict[str, Any],
    state: dict[str, Any],
    *,
    reason: str,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """해소 전 조기 청산 (고래 축소/플립 추종). 시세 매도 가정."""
    if market is None:
        market = _fetch_market_state(gamma_market_id=pos.get("gamma_market_id"))
    price = _current_price(market, int(pos["outcome_index"])) if market else None
    if price is None or price <= 0:
        price = float(pos.get("entry_price") or 0.5)
    exit_price = max(min(price * (1 - COPY_SLIPPAGE), 0.999), 0.001)
    entry = max(float(pos.get("entry_price") or 0.5), 1e-6)
    bet = float(pos.get("bet_usd") or BET_USD)
    shares = bet / entry
    proceeds = shares * exit_price
    pnl = proceeds - bet
    won = pnl > 0
    result = {
        **pos,
        "event": "settled",
        "settled_at": _now_kst(),
        "settled_ts": _now(),
        "won": won,
        "pnl_usd": round(pnl, 2),
        "pnl_pct": round(pnl / bet, 4) if bet else 0.0,
        "exit_price": round(exit_price, 4),
        "settle_reason": reason,
        "early_exit": True,
    }
    _append_jsonl(JOURNAL_FILE, result)
    if not pos.get("is_shadow"):
        state["bankroll"] = float(state.get("bankroll") or INITIAL_BANKROLL) + pnl
    # 재진입 가능하도록 signaled 해제
    wstate = state.get("wallets", {}).get(pos.get("wallet") or "")
    if isinstance(wstate, dict):
        wstate.setdefault("signaled", {})[_pos_key(pos)] = False
    print(
        f"  [paper-exit] {reason} {(pos.get('title') or '')[:36]} "
        f"pnl={pnl:+.2f} @ {exit_price:.3f}"
    )
    return result


def follow_whale_exits(state: dict[str, Any]) -> int:
    """고래가 해당 outcome 순매수를 크게 줄이면 우리도 조기 청산."""
    if HOLD_TO_RESOLUTION:
        return 0

    remaining: list[dict] = []
    closed = 0
    thresh = MIN_NET_USDC * EXIT_NET_FRAC
    for pos in state.get("open_positions") or []:
        wstate = state.get("wallets", {}).get(pos.get("wallet") or "") or {}
        net = float((wstate.get("net_usdc") or {}).get(_pos_key(pos), 0.0))
        # 순매수가 임계 미만 = 고래가 대부분 정리/매도
        if net < thresh:
            early_exit_position(pos, state, reason="whale_exit_reduce")
            closed += 1
            # 지갑 통계 (해소와 동일하게 live_n 갱신은 settle_positions 경로만 — 여기선 paper 간단)
            if not pos.get("is_shadow"):
                ws = state.get("wallets", {}).get(pos.get("wallet") or "")
                if isinstance(ws, dict):
                    ws["live_n"] = int(ws.get("live_n") or 0) + 1
                    if float(pos.get("bet_usd") or 0) and True:
                        # won flag set in early_exit via journal only; recompute from last
                        pass
            continue
        remaining.append(pos)
    state["open_positions"] = remaining
    return closed


def open_paper_positions(signals: list[dict[str, Any]], state: dict[str, Any]) -> int:
    opened = 0
    for sig in signals:
        market = _gamma_market_by_condition(sig["condition_id"])
        if not market or market.get("closed"):
            continue  # 이미 종료됐으면 카피 의미 없음

        existing_market = [
            pos for pos in state.get("open_positions") or []
            if pos.get("condition_id") == sig["condition_id"]
        ]
        consensus_rank, consensus_wallets = register_consensus_signal(state, sig)
        opposite_wallets = opposite_consensus_wallets(state, sig)
        recovery_context = same_whale_recovery_context(state, sig, existing_market)
        foreign_opposite_wallets = [
            wallet for wallet in opposite_wallets
            if str(wallet) != str(sig.get("wallet") or "")
        ]
        same_whale_recovery = bool(recovery_context.get("eligible"))
        if foreign_opposite_wallets or (opposite_wallets and not same_whale_recovery):
            _append_jsonl(JOURNAL_FILE, {
                "event": "consensus_conflict_blocked",
                "reason": (
                    "foreign_whale_opposite_conflict"
                    if foreign_opposite_wallets
                    else "same_whale_recovery_signal_too_weak"
                ),
                "wallet": sig.get("wallet"),
                "condition_id": sig.get("condition_id"),
                "outcome_index": sig.get("outcome_index"),
                "consensus_wallets": consensus_wallets,
                "opposite_wallets": opposite_wallets,
                "foreign_opposite_wallets": foreign_opposite_wallets,
                "recovery_opposite_ratio": recovery_context.get(
                    "opposite_to_previous_ratio"
                ),
                "at": _now_kst(),
                "title": market.get("question"),
            })
            continue
        if consensus_rank > CONSENSUS_MAX_WHALES:
            continue
        same_direction_open = [
            pos for pos in existing_market
            if int(pos.get("outcome_index") or 0) == int(sig["outcome_index"])
        ]
        # 1·2·3번째 고유 고래마다 한 번씩만 증분 진입한다.
        target_tickets = consensus_rank
        if len(same_direction_open) >= target_tickets:
            continue

        same_wallet_market = [
            pos for pos in state.get("open_positions") or []
            if pos.get("wallet") == sig["wallet"]
            and pos.get("condition_id") == sig["condition_id"]
        ]
        if HOLD_TO_RESOLUTION and same_wallet_market:
            if any(
                int(pos.get("outcome_index") or 0) == int(sig["outcome_index"])
                for pos in same_wallet_market
            ):
                continue
            if not same_whale_recovery:
                continue

        if not HOLD_TO_RESOLUTION:
            # 같은 지갑·같은 마켓 반대 outcome 보유 중이면 먼저 청산 (플립 추종)
            still_open: list[dict] = []
            for pos in state.get("open_positions") or []:
                if (
                    pos.get("wallet") == sig["wallet"]
                    and pos.get("condition_id") == sig["condition_id"]
                    and int(pos.get("outcome_index")) != int(sig["outcome_index"])
                ):
                    early_exit_position(
                        pos, state, reason="whale_flip_opposite", market=market,
                    )
                else:
                    still_open.append(pos)
            state["open_positions"] = still_open

        # 이미 같은 키 오픈이면 스킵
        if any(
            p.get("wallet") == sig["wallet"]
            and p.get("condition_id") == sig["condition_id"]
            and int(p.get("outcome_index")) == int(sig["outcome_index"])
            for p in state.get("open_positions") or []
        ):
            continue

        bet_usd = consensus_bet_usd(consensus_rank)
        if bet_usd <= 0:
            continue
        gamma_price = _current_price(market, int(sig["outcome_index"]))
        if gamma_price is None or gamma_price <= 0 or gamma_price >= 1:
            continue
        recovery_sizing: dict[str, Any] = {}
        if same_whale_recovery:
            max_entry_price = min(gamma_price * (1 + COPY_SLIPPAGE), 0.999)
            recovery_sizing = same_whale_recovery_bet(
                existing_market,
                recovery_context,
                max_entry_price,
            )
            if not recovery_sizing.get("feasible"):
                _append_paper_block(
                    sig,
                    "same_whale_recovery_exceeds_market_cap",
                    recovery_required_usd=recovery_sizing.get("bet_usd"),
                    remaining_market_cap_usd=recovery_sizing.get(
                        "remaining_market_cap_usd"
                    ),
                    recovery_opposite_ratio=recovery_context.get(
                        "opposite_to_previous_ratio"
                    ),
                )
                continue
            bet_usd = float(recovery_sizing["bet_usd"])
        is_shadow = state["wallets"].get(sig["wallet"], {}).get("status") == "suspended"
        if not is_shadow:
            risk_ok, risk_reason = _paper_risk_ok(state, bet_usd)
            if not risk_ok:
                _append_paper_block(
                    sig, risk_reason, bet_usd=bet_usd,
                    policy_bankroll=state.get("policy_bankroll"),
                )
                continue

        quote = _paper_buy_quote(market, int(sig["outcome_index"]), bet_usd)
        if not quote.get("ok"):
            _append_paper_block(
                sig,
                str(quote.get("reason") or "paper_quote_failed"),
                **{k: v for k, v in quote.items() if k not in {"ok", "reason"}},
            )
            continue
        entry_price = float(quote["entry_price"])
        quality_ok, quality_reason, quality = _paper_entry_quality(
            state, sig, entry_price
        )
        if same_whale_recovery and quality_reason == "two_sided_whale_exposure":
            quality_ok = True
            quality_reason = ""
        if not quality_ok:
            _append_paper_block(
                sig,
                quality_reason,
                token_id=quote.get("token_id"),
                gamma_price=quote.get("gamma_price"),
                best_ask=quote.get("best_ask"),
                book_vwap=quote.get("book_vwap"),
                **quality,
            )
            continue

        pos = {
            "wallet": sig["wallet"],
            "gamma_market_id": market.get("id"),
            "condition_id": sig["condition_id"],
            "outcome_index": sig["outcome_index"],
            "token_id": quote.get("token_id"),
            "title": market.get("question", sig["title"]),
            "slug": sig["slug"],
            "entry_price": round(entry_price, 6),
            "quote_entry_price": round(float(quote.get("max_price") or 0), 6),
            "gamma_price": round(float(quote.get("gamma_price") or 0), 6),
            "book_best_ask": round(float(quote.get("best_ask") or 0), 6),
            "book_vwap": round(float(quote.get("book_vwap") or 0), 6),
            "execution_source": quote.get("source"),
            "execution_buffer_pct": PAPER_EXECUTION_BUFFER_PCT,
            "bet_usd": bet_usd,
            "shares_est": round(float(quote.get("shares") or 0), 6),
            "consensus_rank": consensus_rank,
            "consensus_wallets": consensus_wallets,
            "signal_policy": PAPER_SIGNAL_POLICY,
            "position_role": (
                "same_whale_recovery_hedge" if same_whale_recovery else "primary_copy"
            ),
            "recovery_opposite_ratio": (
                round(float(recovery_context.get("opposite_to_previous_ratio") or 0), 6)
                if same_whale_recovery else None
            ),
            "recovery_existing_cost_usd": recovery_sizing.get("existing_cost_usd"),
            "recovery_market_cap_usd": recovery_sizing.get("market_cap_usd"),
            "recovery_win_pnl_est": recovery_sizing.get("recovery_win_pnl_est"),
            "signal_age_seconds": round(
                float(quality.get("signal_age_seconds") or 0), 3
            ),
            "expected_win_rate": round(
                float(quality.get("expected_win_rate") or 0), 6
            ),
            "opposite_ratio": round(
                float(quality.get("opposite_ratio") or 0), 6
            ),
            "exit_policy": (
                "hold_to_resolution_v1" if HOLD_TO_RESOLUTION else "follow_whale_exit_v1"
            ),
            "is_shadow": is_shadow,  # True면 중단된 지갑의 회복 관찰용, 실제 잔고에 반영 안 함
            "opened_at": _now_kst(),
            "opened_ts": _now(),
        }
        state["open_positions"].append(pos)
        _append_jsonl(JOURNAL_FILE, {**pos, "event": "opened"})
        opened += 1
    return opened


def settle_positions(state: dict[str, Any]) -> int:
    remaining = []
    settled = 0
    for pos in state.get("open_positions", []):
        market = _fetch_market_state(gamma_market_id=pos["gamma_market_id"])
        if not market:
            remaining.append(pos)
            continue
        winner_idx = _resolved_outcome(market)
        if winner_idx is None:
            remaining.append(pos)
            continue

        won = winner_idx == pos["outcome_index"]
        bet_usd = float(pos.get("bet_usd") or 0)
        shares = float(pos.get("shares_est") or 0)
        if shares <= 0:
            shares = bet_usd / max(float(pos.get("entry_price") or 0), 1e-9)
        payout = shares if won else 0.0
        pnl = payout - bet_usd
        is_shadow = pos.get("is_shadow", False)
        result = {
            **pos,
            "event": "settled",
            "settled_at": _now_kst(),
            "won": won,
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl / bet_usd, 4) if bet_usd else 0.0,
        }
        _append_jsonl(JOURNAL_FILE, result)
        if not is_shadow:
            state["bankroll"] = state.get("bankroll", INITIAL_BANKROLL) + pnl
            if pos.get("signal_policy") == PAPER_SIGNAL_POLICY:
                raw_policy_bankroll = state.get("policy_bankroll")
                policy_bankroll = (
                    INITIAL_BANKROLL
                    if raw_policy_bankroll is None
                    else float(raw_policy_bankroll)
                )
                state["policy_bankroll"] = policy_bankroll + pnl
        settled += 1

        wstate = state["wallets"].get(pos["wallet"])
        if wstate is not None:
            if pos.get("signal_policy") == PAPER_SIGNAL_POLICY:
                wstate["policy_n"] = int(wstate.get("policy_n") or 0) + 1
                wstate["policy_wins"] = int(wstate.get("policy_wins") or 0) + (
                    1 if won else 0
                )
                wstate["policy_pnl"] = float(wstate.get("policy_pnl") or 0) + pnl
                wstate["policy_bet"] = float(wstate.get("policy_bet") or 0) + bet_usd
                _update_paper_policy_status(pos["wallet"], wstate)
            else:
                wstate["live_n"] = int(wstate.get("live_n") or 0) + 1
                wstate["live_wins"] = int(wstate.get("live_wins") or 0) + (
                    1 if won else 0
                )
                if state.get("paper_policy") != PAPER_SIGNAL_POLICY:
                    _update_wallet_status(pos["wallet"], wstate)

    state["open_positions"] = remaining
    return settled


def _wallet_z(wstate: dict[str, Any]) -> float | None:
    n = wstate.get("live_n", 0)
    if n <= 0:
        return None
    expected = wstate.get("expected_win_rate", 0.5)
    wins = wstate.get("live_wins", 0)
    se = math.sqrt(n * expected * (1 - expected))
    if se <= 0:
        return None
    return (wins - n * expected) / se


def _paper_policy_roi(wstate: dict[str, Any]) -> float:
    bet = float(wstate.get("policy_bet") or 0)
    return float(wstate.get("policy_pnl") or 0) / bet if bet > 0 else 0.0


def _update_paper_policy_status(wallet: str, wstate: dict[str, Any]) -> None:
    """v4는 승률이 아니라 실제 체결가 기준 ROI로 고래를 중단/재개한다."""
    n = int(wstate.get("policy_n") or 0)
    if n < PAPER_POLICY_MIN_SETTLED:
        return
    roi = _paper_policy_roi(wstate)
    status = str(wstate.get("status") or "active")
    if status == "active" and roi <= PAPER_POLICY_SUSPEND_ROI:
        wstate["status"] = "suspended"
        wstate["suspended_at"] = _now_kst()
        wstate["suspended_reason"] = (
            f"paper v4 실제호가 기준 {n}건 ROI {roi:+.1%} "
            f"(중단 기준 {PAPER_POLICY_SUSPEND_ROI:+.1%})"
        )
        _append_jsonl(JOURNAL_FILE, {
            "event": "wallet_suspended",
            "signal_policy": PAPER_SIGNAL_POLICY,
            "wallet": wallet,
            "roi": round(roi, 6),
            "reason": wstate["suspended_reason"],
            "at": wstate["suspended_at"],
        })
        send_review(
            f"⛔ <b>[Polymarket Paper v4 고래 자동중단]</b>\n"
            f"지갑 {escape(wallet[:14])}...\n{escape(wstate['suspended_reason'])}"
        )
    elif status == "suspended" and roi >= 0:
        wstate["status"] = "active"
        wstate["reactivated_at"] = _now_kst()
        _append_jsonl(JOURNAL_FILE, {
            "event": "wallet_reactivated",
            "signal_policy": PAPER_SIGNAL_POLICY,
            "wallet": wallet,
            "roi": round(roi, 6),
            "at": wstate["reactivated_at"],
        })
        send_review(
            f"✅ <b>[Polymarket Paper v4 고래 자동재개]</b>\n"
            f"지갑 {escape(wallet[:14])}... {n}건 ROI {roi:+.1%}로 회복"
        )


def _update_wallet_status(wallet: str, wstate: dict[str, Any]) -> None:
    """누적(라이브 추종 시작 이후) z-score로 중단/재활성화 판단.
    고정 크기 최근 N건 창 대신 누적치를 쓰는 이유: 표본이 작을 때(초반) 우연한 몇 건의
    불운으로 성급히 중단되는 걸 막고, 표본이 쌓일수록 판단이 통계적으로 견고해지게 하기 위함.
    중단 기준(SUSPEND_Z)과 재활성화 기준(REACTIVATE_Z)에 히스테리시스를 둬서 경계값 근처에서
    상태가 계속 왔다갔다(flapping)하는 것도 방지한다."""
    n = wstate.get("live_n", 0)
    if n < SUSPEND_MIN_SETTLED:
        return
    z = _wallet_z(wstate)
    if z is None:
        return

    if wstate["status"] == "active" and z <= SUSPEND_Z:
        wstate["status"] = "suspended"
        wstate["suspended_at"] = _now_kst()
        wstate["suspended_reason"] = (
            f"누적 {n}건 z={z:.2f} (기준 {SUSPEND_Z}) — 백테스트 기대승률 대비 유의미하게 저조. "
            f"중단 중에도 그림자추적은 계속하며, z가 {REACTIVATE_Z} 이상으로 회복되면 자동 재활성화됨."
        )
        _append_jsonl(JOURNAL_FILE, {
            "event": "wallet_suspended", "wallet": wallet, "z": round(z, 2),
            "reason": wstate["suspended_reason"], "at": wstate["suspended_at"],
        })
        send_review(
            f"⛔ <b>[Polymarket 고래 추종 자동중단]</b>\n"
            f"지갑 {escape(wallet[:14])}...\n{escape(wstate['suspended_reason'])}"
        )
    elif wstate["status"] == "suspended" and z >= REACTIVATE_Z:
        wstate["status"] = "active"
        wstate["reactivated_at"] = _now_kst()
        _append_jsonl(JOURNAL_FILE, {
            "event": "wallet_reactivated", "wallet": wallet, "z": round(z, 2),
            "at": wstate["reactivated_at"],
        })
        send_review(
            f"✅ <b>[Polymarket 고래 추종 자동재개]</b>\n"
            f"지갑 {escape(wallet[:14])}... 누적 {n}건 z={z:.2f}로 회복돼 카피 재개."
        )


def _fmt_usd(v: float) -> str:
    return f"${v:+.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v*100:+.2f}%"


def build_report(state: dict[str, Any]) -> str:
    from polymarket_whale_insights import build_insight_comments

    rows = _read_jsonl(JOURNAL_FILE)
    settled_all = [r for r in rows if r.get("event") == "settled"]
    policy_settled_all = [
        r for r in settled_all if r.get("signal_policy") == PAPER_SIGNAL_POLICY
    ]
    settled = [r for r in policy_settled_all if not r.get("is_shadow")]
    shadow = [r for r in policy_settled_all if r.get("is_shadow")]
    legacy_settled = [
        r for r in settled_all
        if r.get("signal_policy") != PAPER_SIGNAL_POLICY and not r.get("is_shadow")
    ]
    wins = [r for r in settled if r.get("won")]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    legacy_pnl = sum(float(r.get("pnl_usd") or 0) for r in legacy_settled)
    win_rate = len(wins) / len(settled) if settled else 0.0
    largest_win = max(
        (float(r.get("pnl_usd") or 0) for r in settled),
        default=0.0,
    )
    blocked = [
        r for r in rows
        if r.get("event") == "paper_entry_blocked"
        and r.get("signal_policy") == PAPER_SIGNAL_POLICY
    ]
    blocked_by_reason: dict[str, int] = {}
    for row in blocked:
        reason = str(row.get("reason") or "unknown")
        blocked_by_reason[reason] = blocked_by_reason.get(reason, 0) + 1
    policy_open = [
        p for p in state.get("open_positions") or []
        if p.get("signal_policy") == PAPER_SIGNAL_POLICY
    ]
    legacy_open = [
        p for p in state.get("open_positions") or []
        if p.get("signal_policy") != PAPER_SIGNAL_POLICY
    ]

    active = [w for w, s in state["wallets"].items() if s["status"] == "active"]
    suspended = [w for w, s in state["wallets"].items() if s["status"] == "suspended"]
    raw_policy_bankroll = state.get("policy_bankroll")
    policy_bankroll = (
        INITIAL_BANKROLL if raw_policy_bankroll is None else float(raw_policy_bankroll)
    )

    lines = [
        f"🐋 <b>[Polymarket 고래 카피 Paper v4]</b> — {datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        "",
        f"v4 검증 잔고: ${policy_bankroll:.2f} "
        f"(독립 시작 ${INITIAL_BANKROLL:.0f})",
        f"v4 정산: {len(settled)}건 | 승률 {win_rate:.1%} | PnL {_fmt_usd(pnl)}",
        f"최대 단일 수익 {_fmt_usd(largest_win)} | 해당 1건 제외 {_fmt_usd(pnl - largest_win)}",
        f"v4 오픈 {len(policy_open)}개 | 진입차단 {len(blocked)}건"
        + (f" | 그림자 정산 {len(shadow)}건" if shadow else ""),
        "청산 정책: "
        + ("최초 진입 후 시장 결과까지 보유" if HOLD_TO_RESOLUTION else "고래 축소·플립 추종"),
        f"진입 정책: 1명째 ${consensus_bet_usd(1):.2f} | "
        f"2명째 +${consensus_bet_usd(2):.2f} | "
        f"3명째 +${consensus_bet_usd(3):.2f} | 시장 최대 "
        f"${sum(CONSENSUS_TIER_USD.values()):.2f} | 동일방향 반복·다른 고래 반대 차단 | "
        f"같은 고래 반대노출 {SAME_WHALE_RECOVERY_MIN_RATIO:.0%}+ 복구헤지 허용",
        f"체결 모델: 실제 CLOB ask VWAP + 비용버퍼 {PAPER_EXECUTION_BUFFER_PCT:.1%} | "
        f"표시가 대비 최대 {COPY_SLIPPAGE:.1%} 이내만 체결",
        f"품질 필터: 기대승률 대비 {PAPER_MIN_ENTRY_EDGE:.0%}p 엣지 | "
        f"최대가격 {PAPER_MAX_ENTRY_PRICE:.2f} | 신호 {PAPER_MAX_SIGNAL_AGE_SECONDS // 60}분 이내 | "
        f"반대노출 {PAPER_MAX_OPPOSITE_RATIO:.0%} 미만",
        f"추적 지갑: 활성 {len(active)}명 / 중단(회복관찰중) {len(suspended)}명",
        "",
        f"레거시 참고(성과판정 제외): 정산 {len(legacy_settled)}건 | "
        f"PnL {_fmt_usd(legacy_pnl)} | 오픈 {len(legacy_open)}개",
    ]
    if blocked_by_reason:
        labels = ", ".join(
            f"{reason} {count}" for reason, count in sorted(blocked_by_reason.items())
        )
        lines.append(f"v4 차단 사유: {escape(labels)}")
    if suspended:
        lines.append("")
        lines.append("⛔ 중단된 지갑 (그림자추적으로 회복 시 자동재개):")
        for w in suspended:
            ws = state["wallets"][w]
            lines.append(
                f"• {escape(w[:14])}... v4 {int(ws.get('policy_n') or 0)}건 "
                f"ROI {_paper_policy_roi(ws):+.1%} "
                f"— {escape(ws.get('suspended_reason',''))}"
            )

    recent = settled[-5:]
    if recent:
        lines.append("")
        lines.append("최근 정산(실카피):")
        for r in recent:
            lines.append(
                f"• {escape(str(r.get('title'))[:40])} — "
                f"{'승' if r.get('won') else '패'} {_fmt_usd(float(r.get('pnl_usd') or 0))}"
            )

    try:
        cfg_whales = _load_config().get("whales") or []
    except Exception:
        cfg_whales = []
    comments = build_insight_comments(
        mode="PAPER",
        state=state,
        settled=policy_settled_all,
        config_whales=cfg_whales,
        bet_fraction=BET_FRACTION,
    )
    lines.append("")
    lines.append("💡 <b>개선·건의 코멘트</b> (자동 제안, 모수 변경은 수동)")
    for c in comments:
        lines.append(f"• {escape(c)}")

    lines += [
        "",
        "※ 실주문 없음. 표시가격 가상체결을 폐기하고 실제 주문장 체결 가능 물량만 반영합니다.",
    ]
    return "\n".join(lines)


def _maybe_send_report(state: dict[str, Any], force: bool = False) -> bool:
    if not force and _now() - float(state.get("last_report_time") or 0.0) < REPORT_INTERVAL_SECONDS:
        return False
    delivered = send_review(build_report(state))
    if delivered:
        state["last_report_time"] = _now()
    return delivered


def run_once(report_now: bool = False) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    state = _load_state()
    _ensure_paper_policy_state(state)
    policy_name = (
        "hold_to_resolution_v1" if HOLD_TO_RESOLUTION else "follow_whale_exit_v1"
    )
    if state.get("exit_policy") != policy_name:
        state["exit_policy"] = policy_name
        state["exit_policy_started_at"] = _now_kst()

    settled = settle_positions(state)
    signals = scan_wallets(state)
    # 고래 축소 청산 추종 → 그다음 신규/플립 진입
    exited = follow_whale_exits(state)
    opened = open_paper_positions(signals, state)

    state["last_scan"] = {
        "time": _now_kst(),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "whale_exits": exited,
        "exit_policy": policy_name,
        "signal_policy": PAPER_SIGNAL_POLICY,
        "consensus_candidates": len(state.get("consensus_candidates") or {}),
    }

    reported = _maybe_send_report(state, force=report_now)
    _save_state(state)

    return {
        "signals": len(signals), "opened": opened, "settled": settled,
        "open_positions": len(state["open_positions"]), "reported": reported,
        "bankroll": state.get("bankroll"),
        "policy_bankroll": state.get("policy_bankroll"),
        "signal_policy": PAPER_SIGNAL_POLICY,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-now", action="store_true", help="Send a Telegram report now")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    result = run_once(report_now=args.report_now)
    if args.json:
        print(json.dumps(_json_safe(result), ensure_ascii=False))
    else:
        print(f"[PolymarketWhalePaper] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
