#!/usr/bin/env python3
"""주간 학습 리포트 — 제안만, config 자동 수정 없음.

LaunchAgent: com.godoftrading.weekly-report (월 09:00 KST)
수동: python3 tools/weekly_learning_report.py [--telegram]
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))

LOOKBACK_DAYS = 7
MIN_TRADES_FOR_HINT = 5
MIN_CAUSE_REPEAT = 3


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("trade_history") or []
    except Exception:
        return []


def _in_window(ts: float, cutoff: float) -> bool:
    try:
        return float(ts or 0) >= cutoff
    except Exception:
        return False


def _venue_block(venue: str, cutoff: float) -> dict:
    state = ROOT / ("trade_state_binance.json" if venue == "binance" else "trade_state.json")
    pm_path = ROOT / (
        "trade_postmortem_binance.jsonl" if venue == "binance" else "trade_postmortem.jsonl"
    )
    hist = _load_history(state)
    closed = [
        t for t in hist
        if t.get("status") in ("win", "loss") and _in_window(t.get("timestamp"), cutoff)
    ]
    wins = [t for t in closed if t.get("status") == "win"]
    losses = [t for t in closed if t.get("status") == "loss"]
    pnl = sum(float(t.get("pnl_usd") or 0) for t in closed)

    # stack tags
    tagged = 0
    new_stack_pnl = 0.0
    new_stack_n = 0
    for t in closed:
        ctx = t.get("entry_context") or {}
        a = t.get("logic_attribution") or ctx.get("logic_attribution") or {}
        if a or t.get("logic_stack_version") or ctx.get("logic_stack_version"):
            tagged += 1
        if a.get("new_stack_applied") or any(
            "regime" in str(f) for f in (a.get("new_features") or [])
        ):
            new_stack_n += 1
            new_stack_pnl += float(t.get("pnl_usd") or 0)

    pms = [r for r in _load_jsonl(pm_path) if _in_window(r.get("timestamp"), cutoff)]
    cause_ctr = Counter(
        (r.get("primary_cause") or {}).get("code") or "unknown" for r in pms
    )
    strat_pnl = defaultdict(float)
    strat_n = defaultdict(int)
    for t in closed:
        st = t.get("strategy") or "?"
        strat_pnl[st] += float(t.get("pnl_usd") or 0)
        strat_n[st] += 1

    return {
        "venue": venue,
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "pnl": pnl,
        "wr": (len(wins) / len(closed) * 100) if closed else 0.0,
        "tagged": tagged,
        "new_stack_n": new_stack_n,
        "new_stack_pnl": new_stack_pnl,
        "causes": cause_ctr,
        "strat_pnl": dict(strat_pnl),
        "strat_n": dict(strat_n),
        "pm_n": len(pms),
        "equity": (_load_history.__wrapped__ if False else None),
        "last_equity": (
            json.loads(state.read_text()).get("last_equity") if state.exists() else None
        ),
        "drawdown_pct": (
            json.loads(state.read_text()).get("drawdown_pct") if state.exists() else None
        ),
    }


def _suggestions(blocks: list[dict]) -> list[str]:
    tips = []
    total_closed = sum(b["closed"] for b in blocks)
    if total_closed < MIN_TRADES_FOR_HINT:
        tips.append(
            f"표본 부족 (최근 7일 청산 {total_closed}건 < {MIN_TRADES_FOR_HINT}). "
            f"기록만 유지, 구조 변경 보류."
        )
        return tips

    # merge causes
    cause = Counter()
    for b in blocks:
        cause.update(b["causes"])
    for code, n in cause.most_common(5):
        if n >= MIN_CAUSE_REPEAT:
            if code == "regime_range_ema":
                tips.append(
                    f"[제안] range+EMA 패인 {n}회 — REGIME_RANGE_EMA_RISK_MULT 추가 하향 검토 "
                    f"(자동적용 안 함, 승인 필요)"
                )
            elif code == "short_structural":
                tips.append(
                    f"[제안] SHORT 구조 패인 {n}회 — SHORT_GLOBAL_RISK_MULT 유지/강화 관찰"
                )
            elif code == "fast_stop":
                tips.append(
                    f"[제안] 빠른 손절 {n}회 — 하위TF 타이밍/진입 지연 점검"
                )
            elif code == "shallow_win":
                tips.append(
                    f"[제안] 얕은 승리 {n}회 — 러너/TP 비중 재확인 (이미 조정됨, 관측 유지)"
                )
            else:
                tips.append(f"[관찰] primary_cause={code} ×{n}")

    # strategy worst
    merged = defaultdict(float)
    for b in blocks:
        for st, p in b["strat_pnl"].items():
            merged[st] += p
    if merged:
        worst = min(merged.items(), key=lambda x: x[1])
        if worst[1] < -1.0:
            tips.append(
                f"[관찰] 최악 전략 {worst[0]} 주간 pnl={worst[1]:+.2f} — "
                f"화이트리스트/사이즈 재평가 후보"
            )

    if not tips:
        tips.append("특이 반복 원인 없음 — 현 스택 관측 유지. config 변경 불필요.")
    # only one structural suggestion
    structural = [t for t in tips if t.startswith("[제안]")]
    if len(structural) > 1:
        tips = [structural[0]] + [t for t in tips if not t.startswith("[제안]")][:2]
    return tips[:5]


def build_report() -> str:
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    blocks = [_venue_block("bybit", cutoff), _venue_block("binance", cutoff)]
    tips = _suggestions(blocks)

    lines = [
        f"📊 GodOfTrading 주간 학습 리포트 ({now})",
        f"기간: 최근 {LOOKBACK_DAYS}일 | 스택 관측 + 포스트모템 (자동 수정 없음)",
        "",
    ]
    for b in blocks:
        lines.append(
            f"[{b['venue'].upper()}] 청산 {b['closed']} (W{b['wins']}/L{b['losses']}) "
            f"WR {b['wr']:.0f}% pnl={b['pnl']:+.2f} | "
            f"equity={b['last_equity']} dd={b['drawdown_pct']}"
        )
        lines.append(
            f"  태그진입 {b['tagged']} | 신규스택관여≈{b['new_stack_n']} "
            f"pnl={b['new_stack_pnl']:+.2f} | 포스트모템 {b['pm_n']}건"
        )
        if b["causes"]:
            top = ", ".join(f"{c}×{n}" for c, n in b["causes"].most_common(4))
            lines.append(f"  주요 패인/승인 코드: {top}")
        lines.append("")

    lines.append("💡 제안 (적용은 사람 승인 후)")
    for t in tips:
        lines.append(f"  • {t}")
    lines.append("")
    lines.append(
        "명령 예: 「주간 제안 1번 적용해」 / 「포스트모템 리포트」 / "
        "python3 tools/postmortem_report.py"
    )
    lines.append(
        "고수 지표: 신규 오실레이터 자동 추가 안 함. "
        "원칙(레짐·리스크·R:R)만 검증 후 편입 (TRADING_PRINCIPLES.md)."
    )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--telegram", action="store_true", help="send_review 채널로 발송")
    ap.add_argument("--print-only", action="store_true")
    args = ap.parse_args()
    text = build_report()
    print(text)

    out = ROOT / "weekly_learning_report_latest.txt"
    out.write_text(text, encoding="utf-8")
    print(f"\n[saved] {out}")

    if args.telegram:
        try:
            import sys
            sys.path.insert(0, str(ROOT))
            from publisher import send_review
            # HTML-safe plain
            ok = send_review(f"<pre>{text.replace('<','&lt;')}</pre>")
            print("[telegram]", "ok" if ok else "fail")
        except Exception as e:
            print("[telegram] error:", e)


if __name__ == "__main__":
    main()
