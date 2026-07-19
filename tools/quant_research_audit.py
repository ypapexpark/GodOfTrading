#!/usr/bin/env python3
"""Build a reproducible, read-only quant promotion audit.

The output is evidence for a decision; it never changes live configuration or
places orders.  Run it after every strategy/version change and from the weekly
learning job before promoting a challenger.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import (
    AUTO_TRADE_STRATEGY_WHITELIST,
    BINANCE_CANARY_EARLY_REVIEW_CLOSED,
    BINANCE_CANARY_LIVE_ENABLED,
    BINANCE_CANARY_RISK_MULT,
    LOGIC_STACK_VERSION,
)
from quant_governor import evaluate_live_candidate, load_closed_history


KST = timezone(timedelta(hours=9))


def _state_summary(venue: str) -> dict:
    name = "trade_state_binance.json" if venue == "binance" else "trade_state.json"
    path = ROOT / name
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    history = state.get("trade_history") or []
    return {
        "equity": state.get("last_equity"),
        "drawdown_pct": state.get("drawdown_pct"),
        "tracked_positions": sorted((state.get("positions") or {}).keys()),
        "history_open": [
            row.get("symbol") for row in history if row.get("status") == "open"
        ],
        "ledger_orphans": sum(
            1 for row in history if row.get("status") == "ledger_orphan"
        ),
        "closed_total": len(load_closed_history(venue)),
    }


def build_audit() -> dict:
    approved = tuple(sorted(AUTO_TRADE_STRATEGY_WHITELIST))
    venues = {}
    representative = approved[0] if approved else ""
    for venue in ("bybit", "binance"):
        decision = evaluate_live_candidate(
            venue=venue,
            strategy=representative,
            direction="LONG",
            timeframe="portfolio",
            approved_strategies=approved,
            logic_stack_version=LOGIC_STACK_VERSION,
            binance_canary_enabled=BINANCE_CANARY_LIVE_ENABLED,
            binance_canary_risk_mult=BINANCE_CANARY_RISK_MULT,
            binance_canary_early_review=BINANCE_CANARY_EARLY_REVIEW_CLOSED,
        )
        venues[venue] = {
            **_state_summary(venue),
            "champion_decision": decision.to_dict(),
        }
    return {
        "schema": "got_quant_research_audit_v1",
        "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "logic_stack_version": LOGIC_STACK_VERSION,
        "approved_live_family": {
            "direction": "LONG",
            "strategies": list(approved),
        },
        "promotion_policy": {
            "venue_isolation": True,
            "minimum_closed": 20,
            "minimum_profit_factor": 1.15,
            "positive_net_expectancy_required": True,
            "new_version_probation_risk_mult": 0.50,
            "binance_canary_live_enabled": BINANCE_CANARY_LIVE_ENABLED,
            "binance_canary_risk_mult": BINANCE_CANARY_RISK_MULT,
            "binance_canary_early_review_closed": BINANCE_CANARY_EARLY_REVIEW_CLOSED,
            "candidate_or_backtest_data_can_self_promote": False,
            "live_config_auto_mutation": False,
        },
        "venues": venues,
    }


def _text_report(audit: dict) -> str:
    lines = [
        f"GodOfTrading Quant Audit — {audit['generated_at']}",
        f"stack: {audit['logic_stack_version']}",
        "",
    ]
    for venue, block in audit["venues"].items():
        d = block["champion_decision"]
        c = d["cohort"]
        pf = "∞" if c["profit_factor"] is None else f"{c['profit_factor']:.2f}"
        lines.extend([
            f"[{venue.upper()}] {d['mode']} allow={d['allow']} risk×{d['risk_mult']:.2f}",
            f"  n={c['closed']} WR={c['win_rate']:.1%} PF={pf} "
            f"pnl=${c['pnl_usd']:+.2f} E=${c['expectancy_usd']:+.3f} "
            f"maxDD=${c['max_drawdown_usd']:.2f}",
            f"  version n={d['version_closed']} pnl=${d['version_pnl_usd']:+.2f}",
            f"  decision: {d['reason']}",
            f"  tracked={len(block['tracked_positions'])} history_open={len(block['history_open'])} "
            f"ledger_orphans={block['ledger_orphans']}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json", type=Path,
        default=ROOT / "quant_research_audit_latest.json",
    )
    parser.add_argument(
        "--text", type=Path,
        default=ROOT / "quant_research_audit_latest.txt",
    )
    args = parser.parse_args()
    audit = build_audit()
    args.json.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = _text_report(audit)
    args.text.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
