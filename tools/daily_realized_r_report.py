#!/usr/bin/env python3
"""Bybit 일일 실현 R / 청산사유 / S1 canary 추이 리포트.

사용:
  python3 tools/daily_realized_r_report.py
  python3 tools/daily_realized_r_report.py --days 3 --telegram
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_kst_day(label: str) -> str | None:
    """'2026-08-03 01:21:40 KST' or '08/03 01:21 KST' → YYYY-MM-DD."""
    if not label:
        return None
    s = str(label).replace(" KST", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt.startswith("%m"):
                dt = dt.replace(year=datetime.now().year)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    return None


def _r_multiple(row: dict) -> float | None:
    pnl = row.get("pnl_usd")
    if pnl is None:
        pnl = row.get("pnl")
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None
    risk = float(row.get("est_sl_loss") or 0)
    if risk <= 0:
        ctx = row.get("entry_context") or {}
        risk = float(ctx.get("est_sl_loss") or 0)
    if risk <= 0:
        return None
    return pnl / risk


def build_report(root: Path, days: int = 1) -> str:
    from scalping_engine import ENGINE_VERSION, STRATEGY, evaluate_live_permission

    state = _load_json(root / "trade_state.json")
    hist = state.get("trade_history") or []
    equity = float(state.get("last_equity") or 0)
    start = float(state.get("equity_start") or 0)
    dd = float(state.get("drawdown_pct") or 0)

    today = datetime.now().date()
    cutoff = today - timedelta(days=max(days - 1, 0))

    # journal for exit reasons (richer)
    journal_closes = []
    jpath = root / "trade_execution_journal.jsonl"
    if jpath.exists():
        with jpath.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("event") != "closed":
                    continue
                day = _parse_kst_day(o.get("time") or o.get("closed_at") or "")
                if not day:
                    continue
                try:
                    d = datetime.strptime(day, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if d < cutoff:
                    continue
                journal_closes.append(o)

    # history window
    window = []
    for t in hist:
        if t.get("status") not in ("win", "loss", "breakeven"):
            continue
        day = _parse_kst_day(t.get("closed_at") or t.get("time") or "")
        if not day:
            continue
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= cutoff:
            window.append(t)

    lines = []
    lines.append(f"📊 Bybit 일일 실현R 리포트 ({today.isoformat()} · {days}d)")
    lines.append(
        f"equity ${equity:.2f}"
        + (f" (start ${start:.2f}, dd {dd:.1f}%)" if start else "")
    )
    lines.append(f"open positions: {len(state.get('positions') or {})}")

    # S1 permission
    perm = evaluate_live_permission(root=root, venue="bybit")
    lines.append(
        f"S1 {ENGINE_VERSION}: {perm.mode} allow={perm.allow} "
        f"closed={perm.closed} — {perm.reason}"
    )

    if not window and not journal_closes:
        lines.append(f"최근 {days}일 청산 없음 (스캔만 동작 중일 수 있음)")
        return "\n".join(lines)

    pnls = [float(t.get("pnl_usd") or 0) for t in window]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr = len(wins) / len(pnls) * 100 if pnls else 0
    lines.append(
        f"청산 {len(pnls)}건 W{len(wins)}/L{len(losses)} WR {wr:.0f}% "
        f"sum ${sum(pnls):+.3f}"
    )
    if wins and losses:
        lines.append(
            f"avgW ${statistics.mean(wins):+.3f} / avgL ${statistics.mean(losses):+.3f} "
            f"PF {sum(wins)/abs(sum(losses)):.2f}"
        )
        be = abs(statistics.mean(losses)) / (
            statistics.mean(wins) + abs(statistics.mean(losses))
        )
        lines.append(f"이 R:R 본전 승률 ≈ {be*100:.0f}% (실제 {wr:.0f}%)")

    rs = [r for r in (_r_multiple(t) for t in window) if r is not None]
    if rs:
        lines.append(f"실현 R: n={len(rs)} mean={statistics.mean(rs):+.2f} sum={sum(rs):+.2f}")

    # by strategy
    by_s: dict[str, list[float]] = defaultdict(list)
    for t in window:
        by_s[str(t.get("strategy") or "?")].append(float(t.get("pnl_usd") or 0))
    lines.append("전략별:")
    for strat, ps in sorted(by_s.items(), key=lambda x: sum(x[1])):
        w = sum(1 for p in ps if p > 0)
        l = sum(1 for p in ps if p < 0)
        lines.append(f"  {strat}: n={len(ps)} W{w}/L{l} pnl ${sum(ps):+.3f}")

    # exit reasons from journal
    if journal_closes:
        exits = Counter(
            (o.get("exit_reason") or o.get("status") or "?") for o in journal_closes
        )
        lines.append("청산사유:")
        for reason, n in exits.most_common(8):
            subset = [
                float(o.get("pnl_usd") or o.get("pnl") or 0)
                for o in journal_closes
                if (o.get("exit_reason") or o.get("status") or "?") == reason
            ]
            lines.append(f"  {reason}: n={n} sum ${sum(subset):+.3f}")

    # S1 cohort on current version in full history
    s1_cur = []
    for t in hist:
        if t.get("strategy") != STRATEGY:
            continue
        if t.get("status") not in ("win", "loss"):
            continue
        ctx = t.get("entry_context") or {}
        ver = t.get("engine_version") or ctx.get("engine_version")
        if ver == ENGINE_VERSION:
            s1_cur.append(t)
    if s1_cur:
        sp = [float(t.get("pnl_usd") or 0) for t in s1_cur]
        sw = sum(1 for p in sp if p > 0)
        sl = sum(1 for p in sp if p < 0)
        lines.append(
            f"S1 현버전 코호트: n={len(sp)} W{sw}/L{sl} sum ${sum(sp):+.3f}"
        )

    lines.append("— Cupsey-Bybit: 초소액·스캔·복리 / 체결은 EV>0일 때만")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--root", type=Path, default=ROOT)
    args = ap.parse_args()
    text = build_report(args.root, days=args.days)
    print(text)
    if args.telegram:
        try:
            from publisher import send_review

            ok = send_review(text)
            print(f"[telegram] {'ok' if ok else 'fail'}")
        except Exception as e:
            print(f"[telegram] error: {e}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
