#!/usr/bin/env python3
from __future__ import annotations

"""
Polymarket BTC Up/Down paper bot.

Runs a read-only simulation:
  1. discovers the current BTC 5m/15m Polymarket market,
  2. estimates a fair probability from public Bybit BTC 1m data,
  3. simulates a FOK taker buy when net edge clears the configured buffer,
  4. settles simulated positions after Polymarket publishes resolution data,
  5. sends a periodic Telegram report through the existing send_review route.

No wallet, API key, signing, or live Polymarket order placement is used.
"""

import argparse
import json
import math
import os
import statistics
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

import ccxt
from dotenv import load_dotenv

from polymarket_benchmark import _fetch_event, discover_markets
from publisher import send_review


ROOT = Path(__file__).parent
STATE_FILE = ROOT / "polymarket_paper_state.json"
JOURNAL_FILE = ROOT / "polymarket_paper_journal.jsonl"
CANDIDATE_FILE = ROOT / "polymarket_paper_candidates.jsonl"

POLY_CRYPTO_TAKER_FEE_RATE = 0.07

load_dotenv(ROOT / ".env")

from bot_util import (  # noqa: E402
    KST,
    append_jsonl as _append_jsonl,
    env_float as _env_float,
    env_int as _env_int,
    json_safe as _json_safe,
    load_json,
    now as _now,
    now_kst as _now_kst,
    read_jsonl as _read_jsonl,
    save_json,
)

PAPER_ORDER_USD = _env_float("POLYMARKET_PAPER_ORDER_USD", 100.0)
MIN_EDGE = _env_float("POLYMARKET_PAPER_MIN_EDGE", 0.025)
MIN_TIME_REMAINING = _env_int("POLYMARKET_PAPER_MIN_TIME_REMAINING", 20)
MAX_TIME_REMAINING = _env_int("POLYMARKET_PAPER_MAX_TIME_REMAINING", 260)
MAX_OPEN_POSITIONS = _env_int("POLYMARKET_PAPER_MAX_OPEN_POSITIONS", 4)
REPORT_INTERVAL_SECONDS = _env_int("POLYMARKET_PAPER_REPORT_INTERVAL", 4 * 3600)
RESOLUTION_DELAY_SECONDS = _env_int("POLYMARKET_PAPER_RESOLUTION_DELAY", 45)
RECURRENCE = os.getenv("POLYMARKET_PAPER_RECURRENCE", "5m").strip() or "5m"
MILESTONE_REPORT_AT = (
    os.getenv("POLYMARKET_PAPER_MILESTONE_REPORT_AT", "2026-07-10 06:00").strip()
)


def _parse_ts(value: str | None) -> float:
    if not value:
        return 0.0
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _parse_kst_minute(value: str) -> float:
    if not value:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=KST).timestamp()
        except Exception:
            continue
    return 0.0


def _load_state() -> dict[str, Any]:
    data = load_json(STATE_FILE, default=None)
    if isinstance(data, dict):
        return data
    return {
        "open_positions": [],
        "traded_keys": {},
        "last_report_time": 0.0,
        "last_scan": {},
    }


def _save_state(state: dict[str, Any]) -> None:
    save_json(STATE_FILE, state)


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _fetch_bybit_btc_context() -> dict[str, float]:
    ex = ccxt.bybit({"options": {"defaultType": "future"}, "enableRateLimit": True})
    raw = ex.fetch_ohlcv("BTC/USDT:USDT", timeframe="1m", limit=80)
    closes = [float(row[4]) for row in raw if row and float(row[4]) > 0]
    if len(closes) < 20:
        raise RuntimeError("Bybit BTC 1m 데이터 부족")

    returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    tail = returns[-45:] if len(returns) >= 45 else returns
    sigma_1m = statistics.pstdev(tail) if len(tail) >= 2 else 0.0008
    sigma_1m = max(sigma_1m, 0.00015)
    momentum_3m = math.log(closes[-1] / closes[-4]) if len(closes) >= 4 else 0.0
    return {
        "price": closes[-1],
        "sigma_1m": sigma_1m,
        "momentum_3m": momentum_3m,
    }


def _estimate_up_probability(current_price: float, price_to_beat: float,
                             sigma_1m: float, seconds_remaining: float) -> float:
    minutes = max(seconds_remaining / 60.0, 0.25)
    sigma_price = current_price * sigma_1m * math.sqrt(minutes)
    if sigma_price <= 0:
        return 0.5
    z = (current_price - price_to_beat) / sigma_price
    return min(max(_normal_cdf(z), 0.005), 0.995)


def _fee_usd(shares: float, price: float) -> float:
    return shares * POLY_CRYPTO_TAKER_FEE_RATE * price * (1.0 - price)


def _fee_per_share(price: float) -> float:
    return POLY_CRYPTO_TAKER_FEE_RATE * price * (1.0 - price)


def _fill_for_order(token: dict[str, Any], order_usd: float) -> dict[str, Any] | None:
    fills = token.get("fills") or []
    if not fills:
        return None
    return min(fills, key=lambda f: abs(float(f.get("requested_usd", 0) or 0) - order_usd))


def _best_paper_candidate(rows: list[dict[str, Any]],
                          btc_ctx: dict[str, float],
                          state: dict[str, Any]) -> dict[str, Any] | None:
    now_ts = _now()
    open_count = len(state.get("open_positions", []))
    if open_count >= MAX_OPEN_POSITIONS:
        return None

    best: dict[str, Any] | None = None
    for row in rows:
        if not row.get("found") or row.get("closed") or not row.get("accepting_orders"):
            continue
        start_ts = _parse_ts(row.get("start_time"))
        end_ts = _parse_ts(row.get("end_date"))
        if not (start_ts <= now_ts < end_ts):
            continue

        seconds_remaining = end_ts - now_ts
        if seconds_remaining < MIN_TIME_REMAINING or seconds_remaining > MAX_TIME_REMAINING:
            continue

        metadata = row.get("event_metadata") or {}
        price_to_beat = float(metadata.get("priceToBeat") or 0.0)
        if price_to_beat <= 0:
            continue

        up_prob = _estimate_up_probability(
            btc_ctx["price"], price_to_beat, btc_ctx["sigma_1m"], seconds_remaining
        )

        for token in row.get("tokens", []):
            if token.get("error"):
                continue
            outcome = str(token.get("outcome") or "")
            if outcome not in {"Up", "Down"}:
                continue
            key = f"{row['slug']}:fair_value_bybit:{outcome}"
            if key in state.get("traded_keys", {}):
                continue

            fill = _fill_for_order(token, PAPER_ORDER_USD)
            if not fill:
                continue
            unfilled = float(fill.get("unfilled_usd") or 0.0)
            if unfilled > PAPER_ORDER_USD * 0.01:
                continue
            shares = float(fill.get("shares") or 0.0)
            avg_price = float(fill.get("avg_price") or 0.0)
            filled_usd = float(fill.get("filled_usd") or 0.0)
            if shares <= 0 or avg_price <= 0 or filled_usd <= 0:
                continue

            model_prob = up_prob if outcome == "Up" else (1.0 - up_prob)
            fee_per_share = _fee_per_share(avg_price)
            edge_per_share = model_prob - avg_price - fee_per_share
            fee = _fee_usd(shares, avg_price)
            total_cost = filled_usd + fee
            expected_value = model_prob * shares - total_cost
            expected_roi = expected_value / total_cost if total_cost > 0 else 0.0

            candidate = {
                "candidate_id": f"{int(now_ts * 1000)}-{row['slug']}-{outcome}",
                "time": _now_kst(),
                "timestamp": now_ts,
                "slug": row["slug"],
                "title": row.get("title"),
                "strategy": "fair_value_bybit",
                "outcome": outcome,
                "token_id": token.get("asset_id"),
                "condition_id": row.get("condition_id"),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "seconds_remaining": round(seconds_remaining, 1),
                "price_to_beat": price_to_beat,
                "bybit_btc": btc_ctx["price"],
                "sigma_1m": btc_ctx["sigma_1m"],
                "model_up_probability": up_prob,
                "model_probability": model_prob,
                "best_bid": token.get("best_bid"),
                "best_ask": token.get("best_ask"),
                "avg_price": avg_price,
                "shares": shares,
                "filled_usd": filled_usd,
                "fee_usd": fee,
                "total_cost": total_cost,
                "edge_per_share": edge_per_share,
                "expected_value_usd": expected_value,
                "expected_roi": expected_roi,
                "min_edge": MIN_EDGE,
                "status": "TRADE" if edge_per_share >= MIN_EDGE and expected_value > 0 else "SKIP",
            }
            if best is None or candidate["edge_per_share"] > best["edge_per_share"]:
                best = candidate

    return best


def _open_paper_position(candidate: dict[str, Any], state: dict[str, Any]) -> bool:
    if candidate.get("status") != "TRADE":
        return False
    key = f"{candidate['slug']}:{candidate['strategy']}:{candidate['outcome']}"
    if key in state.get("traded_keys", {}):
        return False

    position = {
        "position_id": candidate["candidate_id"],
        "opened_at": candidate["time"],
        "opened_ts": candidate["timestamp"],
        **candidate,
    }
    state.setdefault("open_positions", []).append(position)
    state.setdefault("traded_keys", {})[key] = candidate["timestamp"]
    _append_jsonl(JOURNAL_FILE, {"event": "opened", **position})
    return True


def _winner_from_event(event: dict[str, Any]) -> str | None:
    metadata = event.get("eventMetadata") or {}
    final_price = metadata.get("finalPrice")
    price_to_beat = metadata.get("priceToBeat")
    if final_price is not None and price_to_beat is not None:
        try:
            return "Up" if float(final_price) >= float(price_to_beat) else "Down"
        except Exception:
            pass

    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")
    try:
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        prices = json.loads(prices) if isinstance(prices, str) else prices
        numeric = [float(x) for x in prices]
        if outcomes and numeric and max(numeric) >= 0.99:
            return str(outcomes[numeric.index(max(numeric))])
    except Exception:
        return None
    return None


def _resolve_positions(state: dict[str, Any]) -> int:
    now_ts = _now()
    remaining = []
    resolved = 0

    for pos in state.get("open_positions", []):
        end_ts = float(pos.get("end_ts") or 0.0)
        if now_ts < end_ts + RESOLUTION_DELAY_SECONDS:
            remaining.append(pos)
            continue

        try:
            event = _fetch_event(str(pos.get("slug")))
        except Exception as exc:
            pos["last_resolution_error"] = str(exc)
            remaining.append(pos)
            continue

        if not event:
            remaining.append(pos)
            continue

        winner = _winner_from_event(event)
        if not winner:
            remaining.append(pos)
            continue

        shares = float(pos.get("shares") or 0.0)
        total_cost = float(pos.get("total_cost") or 0.0)
        payout = shares if pos.get("outcome") == winner else 0.0
        pnl = payout - total_cost
        result = {
            **pos,
            "event": "resolved",
            "resolved_at": _now_kst(),
            "resolved_ts": now_ts,
            "winner": winner,
            "payout_usd": payout,
            "pnl_usd": pnl,
            "roi": pnl / total_cost if total_cost > 0 else 0.0,
            "result": "win" if pnl > 0 else "loss",
        }
        _append_jsonl(JOURNAL_FILE, result)
        resolved += 1

    state["open_positions"] = remaining
    return resolved


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [r for r in rows if r.get("event") == "resolved"]
    wins = [r for r in closed if r.get("result") == "win"]
    stake = sum(float(r.get("total_cost") or 0.0) for r in closed)
    pnl = sum(float(r.get("pnl_usd") or 0.0) for r in closed)
    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "stake": stake,
        "pnl": pnl,
        "roi": pnl / stake if stake > 0 else 0.0,
    }


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _fmt_usd(value: float) -> str:
    return f"${value:+.2f}"


def build_report(state: dict[str, Any]) -> str:
    rows = _read_jsonl(JOURNAL_FILE)
    now_ts = _now()
    since_24h = [r for r in rows if float(r.get("resolved_ts") or 0.0) >= now_ts - 86400]
    since_4h = [r for r in rows if float(r.get("resolved_ts") or 0.0) >= now_ts - 4 * 3600]
    all_stats = _stats(rows)
    day_stats = _stats(since_24h)
    interval_stats = _stats(since_4h)

    last_scan = state.get("last_scan") or {}
    open_positions = state.get("open_positions") or []
    recent = [r for r in rows if r.get("event") == "resolved"][-5:]

    lines = [
        f"📊 <b>[Polymarket Paper 4시간 리포트]</b> — {datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        "",
        f"설정: {escape(RECURRENCE)} / paper ${PAPER_ORDER_USD:.0f} / 최소 순엣지 {MIN_EDGE:.2%}p",
        f"최근 스캔: {escape(str(last_scan.get('slug', '-')))} | "
        f"best edge {float(last_scan.get('edge_per_share') or 0.0):+.3f} | "
        f"{escape(str(last_scan.get('status', '-')))}",
        "",
        "📌 <b>최근 4시간</b>",
        f"거래 {interval_stats['trades']}회 | 승률 {interval_stats['win_rate']*100:.1f}% | "
        f"PnL {_fmt_usd(interval_stats['pnl'])} | ROI {_fmt_pct(interval_stats['roi'])}",
        "📅 <b>최근 24시간</b>",
        f"거래 {day_stats['trades']}회 | 승률 {day_stats['win_rate']*100:.1f}% | "
        f"PnL {_fmt_usd(day_stats['pnl'])} | ROI {_fmt_pct(day_stats['roi'])}",
        "🧾 <b>전체 누적</b>",
        f"거래 {all_stats['trades']}회 | 승률 {all_stats['win_rate']*100:.1f}% | "
        f"PnL {_fmt_usd(all_stats['pnl'])} | ROI {_fmt_pct(all_stats['roi'])}",
        "",
        f"대기 중 포지션: {len(open_positions)}개",
    ]

    if recent:
        lines.append("")
        lines.append("최근 정산:")
        for r in recent:
            lines.append(
                f"• {escape(str(r.get('slug')))} {escape(str(r.get('outcome')))} "
                f"{escape(str(r.get('result')))} {_fmt_usd(float(r.get('pnl_usd') or 0.0))} "
                f"edge {float(r.get('edge_per_share') or 0.0):+.3f}"
            )

    lines += [
        "",
        "※ 실주문 없음. Bybit 가격 기반 fair-value paper 테스트라 Chainlink 해소 피드 괴리까지 성과에 반영됩니다.",
    ]
    return "\n".join(lines)


def _maybe_send_report(state: dict[str, Any], force: bool = False) -> bool:
    if not force and _now() - float(state.get("last_report_time") or 0.0) < REPORT_INTERVAL_SECONDS:
        return False
    msg = build_report(state)
    delivered = send_review(msg)
    if delivered:
        state["last_report_time"] = _now()
    return delivered


def _maybe_send_milestone_report(state: dict[str, Any]) -> bool:
    target_ts = _parse_kst_minute(MILESTONE_REPORT_AT)
    if target_ts <= 0 or _now() < target_ts:
        return False
    key = f"polymarket_paper_milestone:{MILESTONE_REPORT_AT}"
    sent = state.setdefault("milestone_reports_sent", {})
    if sent.get(key):
        return False

    msg = build_report(state).replace(
        "[Polymarket Paper 4시간 리포트]",
        "[Polymarket Paper 1주일 결과 보고서]",
        1,
    )
    delivered = send_review(msg)
    if delivered:
        sent[key] = _now()
    return delivered


def run_once(report_now: bool = False) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    state = _load_state()
    resolved = _resolve_positions(state)

    btc_ctx = _fetch_bybit_btc_context()
    rows = discover_markets(
        recurrence=RECURRENCE if RECURRENCE in {"5m", "15m"} else "5m",
        lookback=0,
        ahead=2,
        order_sizes=(PAPER_ORDER_USD,),
        depth_width=0.05,
    )
    candidate = _best_paper_candidate(rows, btc_ctx, state)
    opened = False
    if candidate:
        state["last_scan"] = {
            "time": candidate["time"],
            "slug": candidate["slug"],
            "status": candidate["status"],
            "outcome": candidate["outcome"],
            "edge_per_share": candidate["edge_per_share"],
            "model_probability": candidate["model_probability"],
            "avg_price": candidate["avg_price"],
            "seconds_remaining": candidate["seconds_remaining"],
        }
        _append_jsonl(CANDIDATE_FILE, candidate)
        opened = _open_paper_position(candidate, state)
    else:
        state["last_scan"] = {
            "time": _now_kst(),
            "slug": "-",
            "status": "NO_CANDIDATE",
            "edge_per_share": 0.0,
        }

    milestone_sent = _maybe_send_milestone_report(state)
    report_sent = _maybe_send_report(state, force=report_now)
    _save_state(state)
    return {
        "resolved": resolved,
        "opened": opened,
        "report_sent": report_sent,
        "milestone_sent": milestone_sent,
        "candidate": candidate,
        "open_positions": len(state.get("open_positions", [])),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-now", action="store_true", help="Send a Telegram report now")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        result = run_once(report_now=args.report_now)
    except Exception as exc:
        result = {"error": str(exc), "time": _now_kst()}
        _append_jsonl(CANDIDATE_FILE, {"status": "ERROR", **result})
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[PolymarketPaper] 오류: {exc}")
        return 1

    if args.json:
        print(json.dumps(_json_safe(result), ensure_ascii=False, indent=2))
    else:
        cand = result.get("candidate") or {}
        print(
            f"[PolymarketPaper] resolved={result['resolved']} opened={result['opened']} "
            f"open={result['open_positions']} status={cand.get('status', 'NO_CANDIDATE')} "
            f"edge={float(cand.get('edge_per_share') or 0.0):+.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
