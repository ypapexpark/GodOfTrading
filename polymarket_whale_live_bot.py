#!/usr/bin/env python3
"""Polymarket 고래 카피 — 초소액 LIVE 경로.

paper 봇과 동일 시그널(scan_wallets)을 쓰되:
  - 상태/저널은 *_live_* 파일로 분리
  - 기본은 dry-run (POLYMARKET_LIVE_TRADING_ENABLED 필요)
  - 동시 포지션·단건·일손실 캡
  - 지갑 suspend 로직 공유

사용:
  python3 polymarket_whale_live_bot.py              # dry-run 또는 live(플래그 시)
  python3 polymarket_whale_live_bot.py --smoke
  python3 polymarket_whale_live_bot.py --report-now
"""
from __future__ import annotations

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

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL.*", category=Warning)

from dotenv import load_dotenv

import polymarket_whale_paper_bot as paper
from polymarket_clob_exec import (
    live_enabled,
    place_buy_usd,
    place_sell_shares,
    smoke_test as clob_smoke,
)
from publisher import send_review

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

STATE_FILE = ROOT / "polymarket_whale_live_state.json"
JOURNAL_FILE = ROOT / "polymarket_whale_live_journal.jsonl"

# 2026-07-12: paper 수준 운용 (시드 ~$800 전제)
# - paper: $1000×2%=$20, 동시 한도 없음 (평소 동시 ~4, 피크 ~44)
# - live: 단건 $15 (paper의 75%, 시드 여유) + MAX_OPEN=0 무제한
# - 피크 44×$15≈$660 ≤ $800 시드. 극단 동시 60+ 시 잔고/FOK 주의.
INITIAL_BANKROLL = float(os.getenv("POLYMARKET_LIVE_BANKROLL", "800") or 800)
BET_FRACTION = float(os.getenv("POLYMARKET_LIVE_BET_FRACTION", "0.02") or 0.02)
BET_USD_CAP = float(os.getenv("POLYMARKET_LIVE_BET_USD_CAP", "15") or 15)
# 0 = paper와 동일 무제한
MAX_OPEN = int(os.getenv("POLYMARKET_LIVE_MAX_OPEN", "0") or 0)
MAX_DAILY_LOSS = float(os.getenv("POLYMARKET_LIVE_MAX_DAILY_LOSS", "120") or 120)
COPY_SLIPPAGE = float(os.getenv("POLYMARKET_WHALE_COPY_SLIPPAGE", "0.03") or 0.03)
MIN_NET_USDC = float(os.getenv("POLYMARKET_WHALE_MIN_NET_USDC", "1000") or 1000)
# 텔레그램: paper와 동일 — 건당 즉시 알림 없이 주기 리포트만 (부담 방지)
REPORT_INTERVAL_SECONDS = int(os.getenv("POLYMARKET_LIVE_REPORT_INTERVAL", str(4 * 3600)) or (4 * 3600))


from bot_util import (  # noqa: E402
    KST,
    append_jsonl,
    json_safe as _json_safe,
    now as _now,
    now_kst as _now_kst,
)


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _append(row: dict) -> None:
    append_jsonl(JOURNAL_FILE, row)


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if st.get("last_reset") != _today():
                st["daily_loss"] = 0.0
                st["last_reset"] = _today()
            # 시드 증액(예: 200→800) 시 내부 bankroll 상향 1회 — 단건이 $4로 줄어들던 문제 방지
            seeded = str(st.get("bankroll_env_seed") or "")
            if seeded != str(INITIAL_BANKROLL):
                cur = float(st.get("bankroll") or 0)
                if INITIAL_BANKROLL > cur:
                    st["bankroll"] = INITIAL_BANKROLL
                st["bankroll_env_seed"] = INITIAL_BANKROLL
            return st
        except Exception:
            pass
    # seed wallets from paper config
    paper_state = paper._load_state()
    return {
        "mode": "live" if live_enabled() else "dry_run",
        "wallets": paper_state.get("wallets") or {},
        "open_positions": [],
        "bankroll": INITIAL_BANKROLL,
        "bankroll_env_seed": INITIAL_BANKROLL,
        "daily_loss": 0.0,
        "last_reset": _today(),
        "last_report_time": 0.0,
        "last_scan": {},
        "orders_blocked": 0,
    }


def _ticket_usd(state: dict[str, Any] | None = None) -> float:
    """paper와 같이 고정 티켓: min(시드×비율, cap). state bankroll로 줄이지 않음."""
    return round(min(INITIAL_BANKROLL * BET_FRACTION, BET_USD_CAP), 4)


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(_json_safe(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _token_id_for_outcome(market: dict[str, Any], outcome_index: int) -> str | None:
    raw = market.get("clobTokenIds")
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if ids and 0 <= outcome_index < len(ids):
            return str(ids[outcome_index])
    except Exception:
        return None
    return None


def _risk_ok(state: dict[str, Any], bet: float) -> tuple[bool, str]:
    if float(state.get("daily_loss") or 0) >= MAX_DAILY_LOSS:
        return False, f"일손실 한도 ${MAX_DAILY_LOSS:.0f}"
    # dry-run/shadow 포지션은 실주문 한도에 넣지 않음 (LIVE 전환 직후 막히던 문제)
    open_n = len([
        p for p in state.get("open_positions") or []
        if not p.get("is_shadow") and p.get("live") is True and not p.get("dry_run")
    ])
    # MAX_OPEN<=0 → paper와 동일하게 동시 한도 없음
    if MAX_OPEN > 0 and open_n >= MAX_OPEN:
        return False, f"동시 실포지션 {open_n}>={MAX_OPEN}"
    # 리스크 한도는 시드(INITIAL)와 추적 bankroll 중 큰 쪽 기준
    bank = max(float(state.get("bankroll") or 0), INITIAL_BANKROLL)
    if bet > bank * 0.25:
        return False, "단건이 bankroll 25% 초과"
    return True, ""


def _live_early_exit(
    pos: dict[str, Any],
    state: dict[str, Any],
    *,
    reason: str,
    market: dict[str, Any] | None = None,
) -> bool:
    """고래 축소/플립 시 실매도(또는 dry) 후 로컬 정산."""
    dry = not live_enabled() or bool(pos.get("dry_run")) or bool(pos.get("is_shadow"))
    if market is None:
        market = paper._fetch_market_state(gamma_market_id=pos.get("gamma_market_id"))
    oidx = int(pos.get("outcome_index") or 0)
    price = paper._current_price(market, oidx) if market else None
    if price is None or price <= 0:
        price = float(pos.get("entry_price") or 0.5)
    exit_price = max(min(price * (1 - COPY_SLIPPAGE), 0.999), 0.001)
    entry = max(float(pos.get("entry_price") or 0.5), 1e-6)
    bet = float(pos.get("bet_usd") or _ticket_usd(state))
    shares = bet / entry

    token_id = pos.get("token_id") or (
        _token_id_for_outcome(market, oidx) if market else None
    )
    if not dry and not pos.get("is_shadow"):
        if not token_id:
            _append({
                "event": "order_failed", "error": "exit no token_id",
                "reason": reason, "at": _now_kst(), "title": pos.get("title"),
            })
            return False
        sell = place_sell_shares(
            str(token_id), shares, price_hint=exit_price, dry_run=False,
        )
        if not sell.get("ok"):
            _append({
                "event": "order_failed",
                "error": f"exit:{sell.get('error')}",
                "reason": reason,
                "at": _now_kst(),
                "title": pos.get("title"),
            })
            print(f"  [live-exit-fail] {sell.get('error')}")
            return False

    proceeds = shares * exit_price
    pnl = proceeds - bet
    result = {
        **pos,
        "event": "settled",
        "settled_at": _now_kst(),
        "settled_ts": _now(),
        "won": pnl > 0,
        "pnl_usd": round(pnl, 2),
        "pnl_pct": round(pnl / bet, 4) if bet else 0,
        "exit_price": round(exit_price, 4),
        "settle_reason": reason,
        "early_exit": True,
    }
    _append(result)
    if not pos.get("is_shadow"):
        state["bankroll"] = float(state.get("bankroll") or INITIAL_BANKROLL) + pnl
        if pnl < 0:
            state["daily_loss"] = float(state.get("daily_loss") or 0) + abs(pnl)
    wstate = state.get("wallets", {}).get(pos.get("wallet") or "")
    if isinstance(wstate, dict):
        key = f"{pos.get('condition_id')}:{pos.get('outcome_index')}"
        wstate.setdefault("signaled", {})[key] = False
        wstate["live_n"] = int(wstate.get("live_n") or 0) + 1
        if pnl > 0:
            wstate["live_wins"] = int(wstate.get("live_wins") or 0) + 1
        _update_wallet_status_live(str(pos.get("wallet")), wstate)
    print(f"  [live-exit] {reason} {(pos.get('title') or '')[:36]} pnl={pnl:+.2f}")
    return True


def follow_whale_exits_live(state: dict[str, Any]) -> int:
    """고래 net 축소 → 매도 추종."""
    remaining: list[dict] = []
    closed = 0
    thresh = paper.MIN_NET_USDC * paper.EXIT_NET_FRAC
    for pos in state.get("open_positions") or []:
        wstate = state.get("wallets", {}).get(pos.get("wallet") or "") or {}
        key = f"{pos.get('condition_id')}:{pos.get('outcome_index')}"
        net = float((wstate.get("net_usdc") or {}).get(key, 0.0))
        if net < thresh:
            ok = _live_early_exit(pos, state, reason="whale_exit_reduce")
            if ok:
                closed += 1
            else:
                remaining.append(pos)  # 매도 실패 시 유지 후 재시도
            continue
        remaining.append(pos)
    state["open_positions"] = remaining
    return closed


def open_live_positions(signals: list[dict], state: dict[str, Any]) -> int:
    opened = 0
    # paper 동일: 고정 티켓 (시드×2% cap) — 내부 bankroll 변동으로 단건이 $4로 쪼그라들지 않음
    bet = _ticket_usd(state)
    dry = not live_enabled()

    for sig in signals:
        ok, why = _risk_ok(state, bet)
        if not ok:
            state["orders_blocked"] = int(state.get("orders_blocked") or 0) + 1
            _append({
                "event": "blocked", "reason": why, "wallet": sig.get("wallet"),
                "at": _now_kst(), "title": sig.get("title"),
            })
            print(f"  [live-block] {why}")
            continue

        market = paper._gamma_market_by_condition(sig["condition_id"])
        if not market or market.get("closed"):
            continue

        # 플립: 같은 지갑·마켓 반대 outcome 보유 시 먼저 청산
        still: list[dict] = []
        for pos in state.get("open_positions") or []:
            if (
                pos.get("wallet") == sig.get("wallet")
                and pos.get("condition_id") == sig.get("condition_id")
                and int(pos.get("outcome_index")) != int(sig.get("outcome_index"))
            ):
                _live_early_exit(pos, state, reason="whale_flip_opposite", market=market)
            else:
                still.append(pos)
        state["open_positions"] = still

        if any(
            p.get("wallet") == sig.get("wallet")
            and p.get("condition_id") == sig.get("condition_id")
            and int(p.get("outcome_index")) == int(sig.get("outcome_index"))
            for p in state.get("open_positions") or []
        ):
            continue

        price = paper._current_price(market, sig["outcome_index"])
        if price is None or price <= 0 or price >= 1:
            continue
        entry_price = min(price * (1 + COPY_SLIPPAGE), 0.999)
        is_shadow = state["wallets"].get(sig["wallet"], {}).get("status") == "suspended"
        token_id = _token_id_for_outcome(market, int(sig["outcome_index"]))

        order_res = None
        if not is_shadow:
            if not token_id:
                _append({"event": "blocked", "reason": "no token_id", "at": _now_kst()})
                continue
            order_res = place_buy_usd(
                token_id, bet, price_hint=entry_price, dry_run=dry,
            )
            if not order_res.get("ok"):
                _append({
                    "event": "order_failed",
                    "error": order_res.get("error"),
                    "token_id": token_id,
                    "bet_usd": bet,
                    "at": _now_kst(),
                    "title": market.get("question"),
                })
                print(f"  [order-fail] {order_res.get('error')}")
                continue

        pos = {
            "wallet": sig["wallet"],
            "gamma_market_id": market.get("id"),
            "condition_id": sig["condition_id"],
            "outcome_index": sig["outcome_index"],
            "token_id": token_id,
            "title": market.get("question", sig.get("title")),
            "slug": sig.get("slug"),
            "entry_price": round(entry_price, 4),
            "bet_usd": bet,
            "is_shadow": is_shadow,
            "live": (not dry and not is_shadow),
            "dry_run": dry or is_shadow,
            "order_id": (order_res or {}).get("order_id"),
            "opened_at": _now_kst(),
            "opened_ts": _now(),
        }
        state["open_positions"].append(pos)
        _append({**pos, "event": "opened", "mode": "dry_run" if pos["dry_run"] else "live"})
        opened += 1
        tag = "DRY" if pos["dry_run"] else "LIVE"
        print(f"  [{tag}] open {pos['title'][:40]} ${bet:.2f} @ {entry_price:.3f}")
    return opened


def settle_positions(state: dict[str, Any]) -> int:
    """paper와 동일 정산. live 포지션도 마켓 resolution 기준 paper PnL 추적
    (실제 USDC 잔고 동기화는 2단계에서 강화)."""
    # 임시로 paper settle 재사용 위해 journal 분리 — 로직 복제
    remaining = []
    settled = 0
    for pos in state.get("open_positions", []):
        market = paper._fetch_market_state(gamma_market_id=pos.get("gamma_market_id"))
        if not market:
            remaining.append(pos)
            continue
        winner_idx = paper._resolved_outcome(market)
        if winner_idx is None:
            remaining.append(pos)
            continue
        won = winner_idx == pos["outcome_index"]
        payout = pos["bet_usd"] / pos["entry_price"] if won else 0.0
        pnl = payout - pos["bet_usd"]
        is_shadow = pos.get("is_shadow", False)
        result = {
            **pos,
            "event": "settled",
            "settled_at": _now_kst(),
            "won": won,
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl / pos["bet_usd"], 4) if pos["bet_usd"] else 0,
        }
        _append(result)
        if not is_shadow:
            state["bankroll"] = float(state.get("bankroll") or INITIAL_BANKROLL) + pnl
            if pnl < 0:
                state["daily_loss"] = float(state.get("daily_loss") or 0) + abs(pnl)
        settled += 1
        wstate = state["wallets"].get(pos["wallet"])
        if wstate is not None:
            wstate["live_n"] = wstate.get("live_n", 0) + 1
            wstate["live_wins"] = wstate.get("live_wins", 0) + (1 if won else 0)
            _update_wallet_status_live(pos["wallet"], wstate)
    state["open_positions"] = remaining
    return settled


def _update_wallet_status_live(wallet: str, wstate: dict[str, Any]) -> None:
    """paper._update_wallet_status 와 동일 기준, 저널은 live 파일."""
    n = wstate.get("live_n", 0)
    if n < paper.SUSPEND_MIN_SETTLED:
        return
    z = paper._wallet_z(wstate)
    if z is None:
        return
    if wstate.get("status") == "active" and z <= paper.SUSPEND_Z:
        wstate["status"] = "suspended"
        wstate["suspended_at"] = _now_kst()
        wstate["suspended_reason"] = (
            f"누적 {n}건 z={z:.2f} (기준 {paper.SUSPEND_Z}) — live 추종 자동중단"
        )
        _append({
            "event": "wallet_suspended", "wallet": wallet, "z": round(z, 2),
            "reason": wstate["suspended_reason"], "at": wstate["suspended_at"],
        })
        send_review(
            f"⛔ <b>[Poly Whale LIVE 지갑 중단]</b>\n"
            f"{escape(wallet[:14])}... z={z:.2f}"
        )
    elif wstate.get("status") == "suspended" and z >= paper.REACTIVATE_Z:
        wstate["status"] = "active"
        wstate["reactivated_at"] = _now_kst()
        _append({
            "event": "wallet_reactivated", "wallet": wallet, "z": round(z, 2),
            "at": wstate["reactivated_at"],
        })



def build_report(state: dict[str, Any]) -> str:
    from polymarket_whale_insights import build_insight_comments

    mode = "LIVE" if live_enabled() else "DRY-RUN"
    rows = []
    if JOURNAL_FILE.exists():
        for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    settled = [r for r in rows if r.get("event") == "settled" and not r.get("is_shadow")]
    fails = [r for r in rows if r.get("event") == "order_failed"]
    blocked = [r for r in rows if r.get("event") == "blocked"]
    wins = [r for r in settled if r.get("won")]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    wr = len(wins) / len(settled) if settled else 0.0
    bank = float(state.get("bankroll") or INITIAL_BANKROLL)
    bet = _ticket_usd(state)

    # 지갑별 한 줄 (live journal 기준)
    by_w: dict[str, list] = {}
    for r in settled:
        by_w.setdefault(str(r.get("wallet") or "?"), []).append(r)

    lines = [
        f"🐋 <b>[Polymarket 고래 카피 {mode}]</b> — {datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        f"bankroll ${bank:.2f} (시드 ${INITIAL_BANKROLL:.0f}) | "
        f"일손실 ${float(state.get('daily_loss') or 0):.2f}/${MAX_DAILY_LOSS:.0f}",
        f"정산 {len(settled)} | 승률 {wr:.1%} | PnL ${pnl:+.2f}",
        f"오픈 {len(state.get('open_positions') or [])}/"
        f"{'∞' if MAX_OPEN <= 0 else MAX_OPEN} | "
        f"단건 고정 ~${bet:.2f} (paper 패리티)",
        f"live_flag={live_enabled()} | blocked={state.get('orders_blocked', 0)} | "
        f"order_fail={len(fails)}",
    ]
    if by_w:
        lines.append("")
        lines.append("지갑별 (live 정산):")
        for w, rs in sorted(by_w.items(), key=lambda x: -len(x[1]))[:6]:
            ww = sum(1 for r in rs if r.get("won"))
            pp = sum(float(r.get("pnl_usd") or 0) for r in rs)
            lines.append(
                f"• {escape(w[:12])}... n={len(rs)} WR={ww/len(rs):.0%} PnL ${pp:+.1f}"
            )

    try:
        cfg_whales = paper._load_config().get("whales") or []
    except Exception:
        cfg_whales = []
    comments = build_insight_comments(
        mode=mode,
        state=state,
        settled=settled,
        config_whales=cfg_whales,
        order_failed=fails,
        blocked=blocked,
        bet_fraction=BET_FRACTION,
        max_open=MAX_OPEN,
    )
    lines.append("")
    lines.append("💡 <b>개선·건의 코멘트</b> (자동 제안 · config 자동수정 없음)")
    for c in comments:
        lines.append(f"• {escape(c)}")

    lines += [
        "",
        "※ 모수 추가: 후보 지갑 → paper 관찰 → config whales[] 반영 → live.",
        "※ 건당 TG 없음 · 주기 리포트만 (paper와 동일 운용).",
    ]
    return "\n".join(lines)


def _ensure_wallets_from_config(state: dict[str, Any]) -> None:
    """config 9지갑이 state에 없으면 추가 (last_seen 은 0부터 — 과거 신호 폭주 방지 위해
    paper state 의 last_seen 을 가져와 동기화)."""
    cfg = paper._load_config()
    paper_st = paper._load_state()
    for w in cfg.get("whales") or []:
        addr = w.get("wallet")
        if not addr:
            continue
        if addr not in state["wallets"]:
            src = (paper_st.get("wallets") or {}).get(addr) or {}
            state["wallets"][addr] = {
                "status": src.get("status", "active"),
                "expected_win_rate": w.get("expected_win_rate", 0.5),
                "last_seen_ts": int(src.get("last_seen_ts") or 0),
                "net_usdc": dict(src.get("net_usdc") or {}),
                "signaled": dict(src.get("signaled") or {}),
                "live_wins": int(src.get("live_wins") or 0),
                "live_n": int(src.get("live_n") or 0),
            }


def run_once(report_now: bool = False) -> dict[str, Any]:
    # paper 모듈 상수/설정 정렬
    paper.MIN_NET_USDC = MIN_NET_USDC
    paper.COPY_SLIPPAGE = COPY_SLIPPAGE

    state = _load_state()
    _ensure_wallets_from_config(state)
    state["mode"] = "live" if live_enabled() else "dry_run"

    settled = settle_positions(state)
    signals = paper.scan_wallets(state)
    whale_exits = follow_whale_exits_live(state)
    opened = open_live_positions(signals, state)

    state["last_scan"] = {
        "time": _now_kst(),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "whale_exits": whale_exits,
        "mode": state["mode"],
        "wallets": len(state.get("wallets") or {}),
        "live_flag": live_enabled(),
    }
    # paper와 동일: 건당 TG 없이 주기 리포트만 (기본 4h)
    if report_now or (_now() - float(state.get("last_report_time") or 0) >= REPORT_INTERVAL_SECONDS):
        if send_review(build_report(state)):
            state["last_report_time"] = _now()
    _save_state(state)
    return {
        "mode": state["mode"],
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "open_positions": len(state.get("open_positions") or []),
        "bankroll": state.get("bankroll"),
        "live_enabled": live_enabled(),
        "bet_usd": _ticket_usd(state),
        "bet_fraction": BET_FRACTION,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--report-now", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.smoke:
        info = clob_smoke()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info.get("client_ok") or not live_enabled() else 1

    result = run_once(report_now=args.report_now)
    if args.json:
        print(json.dumps(_json_safe(result), ensure_ascii=False))
    else:
        print(f"[PolymarketWhaleLive] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
