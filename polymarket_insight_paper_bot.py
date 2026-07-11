#!/usr/bin/env python3
from __future__ import annotations

"""
PolyInsight 모멘텀/쇼크 시그널 — 전용 PAPER 봇 (고래 카피와 계좌·상태 완전 분리).

흐름:
  1. PolyInsight analytics 수집(+스냅샷 history 누적)
  2. momentum_break / prob_shock 만 paper 진입 (extreme_price=avoid 는 스킵)
  3. Gamma resolution 으로 정산
  4. 4시간마다 send_review 로 텔레그램 리포트 (+ 졸업/개선 코멘트)

실주문·지갑 서명 없음. 검증 후 별도 LIVE 모듈로만 승격.
"""

import argparse
import json
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
from polymarket_insight_insights import build_insight_comments, kind_stats

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "polymarket_insight_paper_state.json"
JOURNAL_FILE = ROOT / "polymarket_insight_paper_journal.jsonl"

POLYINSIGHT_ROOT = Path(
    os.getenv("POLYINSIGHT_ROOT", "/Users/ghp/Projects/PolyInsight")
).expanduser()

GAMMA_API = "https://gamma-api.polymarket.com"

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

# 가상 계좌 — 고래 paper($1000) / live($200) 와 숫자만 같아도 파일·봇이 다름
INITIAL_BANKROLL = _env_float("POLYMARKET_INSIGHT_INITIAL_BANKROLL", 1000.0)
BET_FRACTION = _env_float("POLYMARKET_INSIGHT_BET_FRACTION", 0.02)
ENTRY_SLIPPAGE = _env_float("POLYMARKET_INSIGHT_SLIPPAGE", 0.02)
MAX_OPEN = _env_int("POLYMARKET_INSIGHT_MAX_OPEN", 8)
REPORT_INTERVAL_SECONDS = _env_int("POLYMARKET_INSIGHT_REPORT_INTERVAL", 4 * 3600)
# 진입 시 확률이 너무 극단이면 스킵 (추격 금지)
MAX_ENTRY_P = _env_float("POLYMARKET_INSIGHT_MAX_ENTRY_P", 0.80)
MIN_ENTRY_P = _env_float("POLYMARKET_INSIGHT_MIN_ENTRY_P", 0.20)
ENTRY_KINDS = frozenset(
    k.strip()
    for k in os.getenv(
        "POLYMARKET_INSIGHT_ENTRY_KINDS", "momentum_break,prob_shock"
    ).split(",")
    if k.strip()
)

BET_USD = INITIAL_BANKROLL * BET_FRACTION


def _load_state() -> dict[str, Any]:
    data = load_json(STATE_FILE, default=None)
    if isinstance(data, dict):
        return data
    return {
        "bankroll": INITIAL_BANKROLL,
        "open_positions": [],
        "signaled": {},  # signal_id or market_id:kind -> True
        "last_report_time": 0.0,
        "last_scan": {},
        "mode": "PAPER",
        "strategy": "polyinsight_momentum",
        "note": "고래 카피와 분리된 PolyInsight 분석 paper 계좌",
    }


def _save_state(state: dict[str, Any]) -> None:
    save_json(STATE_FILE, state)


def _get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 15) -> Any:
    resp = requests.get(url, params=params or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _fetch_gamma_market(gamma_market_id: str) -> dict[str, Any] | None:
    try:
        return _get_json(f"{GAMMA_API}/markets/{gamma_market_id}")
    except Exception:
        return None


def _fetch_event(event_id: str) -> dict[str, Any] | None:
    try:
        if str(event_id).isdigit():
            return _get_json(f"{GAMMA_API}/events/{event_id}")
        rows = _get_json(f"{GAMMA_API}/events", {"slug": event_id})
        return rows[0] if rows else None
    except Exception:
        return None


def _top_market_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    markets = event.get("markets") or []
    if not markets:
        return None
    return max(markets, key=lambda m: float(m.get("volume", 0) or 0))


def _outcome_index(direction: str, outcomes: list[str]) -> int:
    d = (direction or "YES").upper()
    if d in ("NO", "N", "BEAR"):
        return 1 if len(outcomes) > 1 else 0
    # YES / default → 첫 outcome
    return 0


def _price_at(market: dict[str, Any], outcome_index: int) -> float | None:
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        return float(prices[outcome_index])
    except Exception:
        return None


def _resolved_outcome(market: dict[str, Any]) -> int | None:
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


def run_polyinsight_collect(per_cat: int = 25) -> dict[str, Any]:
    """PolyInsight 사이드 모듈로 스냅샷 누적 + 시그널 생성."""
    if not POLYINSIGHT_ROOT.is_dir():
        return {"ok": False, "error": f"POLYINSIGHT_ROOT missing: {POLYINSIGHT_ROOT}"}

    root_s = str(POLYINSIGHT_ROOT)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)

    try:
        from analytics.collect import collect_open_markets
        from analytics.momentum import generate_signals, dedupe_new
        from analytics.research_db import (
            load_snapshots,
            append_signal,
            read_signals,
        )
        from analytics.bridge_got import export_got_filter
    except Exception as e:
        return {"ok": False, "error": f"import analytics failed: {e}"}

    try:
        collect_stats = collect_open_markets(per_cat=per_cat)
    except Exception as e:
        collect_stats = {"events": 0, "upserted": 0, "error": str(e)}

    snaps = load_snapshots()
    signals = generate_signals(snaps)
    recent = {s.get("signal_id") for s in read_signals(500)}
    fresh = dedupe_new(signals, recent)
    for s in fresh:
        append_signal(s)
    try:
        export_got_filter()
    except Exception:
        pass

    return {
        "ok": True,
        "collect": collect_stats,
        "signals_all": signals,
        "signals_new": fresh,
        "n_markets": len(snaps.get("markets") or {}),
    }


def open_paper_from_signals(
    signals: list[dict[str, Any]],
    state: dict[str, Any],
) -> int:
    opened = 0
    open_ids = {
        str(p.get("event_id") or p.get("market_id") or "")
        for p in state.get("open_positions") or []
    }
    signaled = state.setdefault("signaled", {})

    for sig in signals:
        kind = str(sig.get("kind") or "")
        if kind not in ENTRY_KINDS:
            continue
        if sig.get("use") == "filter_avoid_chase":
            continue

        sid = str(sig.get("signal_id") or "")
        event_id = str(sig.get("market_id") or "")
        if not event_id:
            continue
        if sid and signaled.get(sid):
            continue
        # 같은 이벤트 중복 포지션 방지
        if event_id in open_ids:
            continue
        if any(
            str(k).startswith(f"{event_id}:") for k in signaled
        ) and any(
            str(p.get("event_id")) == event_id
            for p in (state.get("open_positions") or [])
        ):
            continue

        if len(state.get("open_positions") or []) >= MAX_OPEN:
            break

        event = _fetch_event(event_id)
        if not event:
            continue
        market = _top_market_from_event(event)
        if not market or market.get("closed"):
            continue

        try:
            outcomes = json.loads(market.get("outcomes", '["Yes","No"]'))
        except Exception:
            outcomes = ["Yes", "No"]
        direction = str(sig.get("direction") or "YES")
        oidx = _outcome_index(direction, outcomes)
        price = _price_at(market, oidx)
        if price is None or price <= 0.02 or price >= 0.98:
            continue
        # 우리가 사는 쪽 확률이 이미 극단이면 추격 스킵
        if price > MAX_ENTRY_P or price < MIN_ENTRY_P:
            signaled[sid or f"{event_id}:{kind}:skip_extreme"] = True
            continue

        entry_price = min(price * (1 + ENTRY_SLIPPAGE), 0.999)
        pos = {
            "event_id": event_id,
            "gamma_market_id": str(market.get("id") or ""),
            "condition_id": market.get("conditionId") or "",
            "outcome_index": oidx,
            "direction": direction,
            "kind": kind,
            "title": market.get("question") or sig.get("title") or event.get("title"),
            "slug": sig.get("slug") or event.get("slug"),
            "category": sig.get("category"),
            "entry_price": round(entry_price, 4),
            "signal_prob": float(sig.get("probability") or price),
            "bet_usd": BET_USD,
            "signal_id": sid,
            "reasons": sig.get("reasons") or [],
            "opened_at": _now_kst(),
            "opened_ts": _now(),
            "strategy": "polyinsight_momentum",
            "account": "insight_paper",  # 고래와 구별 태그
        }
        if not pos["gamma_market_id"]:
            continue

        state.setdefault("open_positions", []).append(pos)
        open_ids.add(event_id)
        if sid:
            signaled[sid] = True
        signaled[f"{event_id}:{kind}"] = True
        _append_jsonl(JOURNAL_FILE, {**pos, "event": "opened"})
        opened += 1

    return opened


def settle_positions(state: dict[str, Any]) -> int:
    remaining = []
    settled = 0
    for pos in state.get("open_positions") or []:
        mid = pos.get("gamma_market_id")
        if not mid:
            remaining.append(pos)
            continue
        market = _fetch_gamma_market(str(mid))
        if not market:
            remaining.append(pos)
            continue
        winner_idx = _resolved_outcome(market)
        if winner_idx is None:
            remaining.append(pos)
            continue

        won = winner_idx == int(pos.get("outcome_index") or 0)
        bet = float(pos.get("bet_usd") or BET_USD)
        entry = float(pos.get("entry_price") or 0.5)
        payout = (bet / entry) if won and entry > 0 else 0.0
        pnl = payout - bet
        result = {
            **pos,
            "event": "settled",
            "settled_at": _now_kst(),
            "settled_ts": _now(),
            "won": won,
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl / bet, 4) if bet else 0.0,
        }
        _append_jsonl(JOURNAL_FILE, result)
        state["bankroll"] = float(state.get("bankroll") or INITIAL_BANKROLL) + pnl
        settled += 1

    state["open_positions"] = remaining
    return settled


def _fmt_usd(v: float) -> str:
    return f"${v:+.2f}"


def build_report(state: dict[str, Any]) -> str:
    rows = _read_jsonl(JOURNAL_FILE)
    settled = [r for r in rows if r.get("event") == "settled"]
    opened_n = sum(1 for r in rows if r.get("event") == "opened")
    wins = [r for r in settled if r.get("won")]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    wr = len(wins) / len(settled) if settled else 0.0
    by = kind_stats(settled)
    bankroll = float(state.get("bankroll") or INITIAL_BANKROLL)
    open_pos = state.get("open_positions") or []

    lines = [
        f"📊 <b>[PolyInsight 모멘텀 Paper]</b> — "
        f"{datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        "",
        "🏷 계좌: <b>insight_paper</b> (고래 카피 / BTC paper 와 분리)",
        f"가상 잔고: ${bankroll:.2f} (시작 ${INITIAL_BANKROLL:.0f}) · "
        f"단건 ${BET_USD:.0f} ({BET_FRACTION*100:.0f}%)",
        f"누적 정산: {len(settled)}건 | 승률 {wr:.1%} | 누적 PnL {_fmt_usd(pnl)}",
        f"오픈: {len(open_pos)}/{MAX_OPEN} · 누적 진입 기록 {opened_n}건",
    ]

    if by:
        lines.append("")
        lines.append("kind별:")
        for kind, st in sorted(by.items(), key=lambda x: -x[1]["n"]):
            kwr = st["w"] / st["n"] if st["n"] else 0
            lines.append(
                f"• {escape(kind)} n={st['n']} WR={kwr:.0%} PnL={_fmt_usd(st['pnl'])}"
            )

    if open_pos:
        lines.append("")
        lines.append("오픈 포지션:")
        for p in open_pos[:8]:
            lines.append(
                f"• [{escape(str(p.get('kind')))}] "
                f"{escape(str(p.get('direction')))} "
                f"{escape(str(p.get('title') or '')[:36])} "
                f"@ {float(p.get('entry_price') or 0):.0%}"
            )

    recent = settled[-5:]
    if recent:
        lines.append("")
        lines.append("최근 정산:")
        for r in recent:
            lines.append(
                f"• {escape(str(r.get('title') or '')[:40])} — "
                f"{'승' if r.get('won') else '패'} "
                f"{_fmt_usd(float(r.get('pnl_usd') or 0))}"
            )

    comments = build_insight_comments(
        state=state,
        settled=settled,
        by_kind=by,
        open_n=len(open_pos),
        max_open=MAX_OPEN,
        bankroll=bankroll,
        initial=INITIAL_BANKROLL,
    )
    lines.append("")
    lines.append("💡 <b>개선·졸업 코멘트</b> (자동 제안, LIVE 전환은 수동)")
    for c in comments:
        lines.append(f"• {escape(c)}")

    lines += [
        "",
        "※ 실주문 없음 · 슬리피지 가정 반영 · 고래 LIVE 와 한도/지갑 공유 안 함.",
        "※ 검증 후 실매매: 별도 초소액 모듈 + 사람 승인 (이 봇이 LIVE 안 함).",
    ]
    return "\n".join(lines)


def _maybe_send_report(state: dict[str, Any], force: bool = False) -> bool:
    if not force and _now() - float(state.get("last_report_time") or 0.0) < REPORT_INTERVAL_SECONDS:
        return False
    delivered = send_review(build_report(state))
    if delivered:
        state["last_report_time"] = _now()
    return delivered


def run_once(
    report_now: bool = False,
    skip_collect: bool = False,
    per_cat: int = 25,
) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    state = _load_state()

    settled = settle_positions(state)

    signals_all: list[dict[str, Any]] = []
    collect_meta: dict[str, Any] = {"skipped": True}
    if not skip_collect:
        collect_meta = run_polyinsight_collect(per_cat=per_cat)
        if collect_meta.get("ok"):
            # 이번 사이클 전체 후보 중 미진입 것만 시도 (signaled 로 중복 방지)
            signals_all = list(collect_meta.get("signals_all") or [])
        else:
            # 수집 실패 시 기존 signals.jsonl 폴백
            sig_path = POLYINSIGHT_ROOT / "analytics_data" / "signals.jsonl"
            signals_all = _read_jsonl(sig_path, limit=100)
    else:
        sig_path = POLYINSIGHT_ROOT / "analytics_data" / "signals.jsonl"
        signals_all = _read_jsonl(sig_path, limit=100)

    opened = open_paper_from_signals(signals_all, state)

    state["last_scan"] = {
        "time": _now_kst(),
        "collect": collect_meta.get("collect") if collect_meta.get("ok") else collect_meta,
        "signals": len(signals_all),
        "opened": opened,
        "settled": settled,
    }

    reported = _maybe_send_report(state, force=report_now)
    _save_state(state)

    return {
        "strategy": "polyinsight_momentum",
        "account": "insight_paper",
        "signals": len(signals_all),
        "opened": opened,
        "settled": settled,
        "open_positions": len(state.get("open_positions") or []),
        "reported": reported,
        "bankroll": state.get("bankroll"),
        "collect_ok": collect_meta.get("ok"),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report-now", action="store_true")
    p.add_argument("--no-collect", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--per-cat", type=int, default=25)
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    result = run_once(
        report_now=args.report_now,
        skip_collect=args.no_collect,
        per_cat=args.per_cat,
    )
    if args.json:
        print(json.dumps(_json_safe(result), ensure_ascii=False))
    else:
        print(f"[PolymarketInsightPaper] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
