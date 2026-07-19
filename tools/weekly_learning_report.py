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
MIN_TRADES_FOR_HINT = 30
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


def _close_timestamp(trade: dict, now: datetime | None = None) -> float:
    """진입시각이 아니라 실제 청산시각을 우선 반환한다."""
    close_info = trade.get("close_info") or {}
    api_times = []
    for item in [close_info] + list(close_info.get("parts") or []):
        for key in ("updatedTime", "createdTime"):
            try:
                value = float(item.get(key) or 0)
                if value > 0:
                    api_times.append(value / 1000 if value > 10_000_000_000 else value)
            except Exception:
                pass
    if api_times:
        return max(api_times)

    closed_at = str(trade.get("closed_at") or "").strip()
    if closed_at:
        current = now or datetime.now(KST)
        for fmt in ("%Y-%m-%d %H:%M:%S KST", "%Y-%m-%d %H:%M KST"):
            try:
                return datetime.strptime(closed_at, fmt).replace(tzinfo=KST).timestamp()
            except ValueError:
                pass
        try:
            parsed = datetime.strptime(
                f"{current.year}/{closed_at}", "%Y/%m/%d %H:%M KST"
            ).replace(tzinfo=KST)
            if parsed > current + timedelta(days=1):
                parsed = parsed.replace(year=current.year - 1)
            return parsed.timestamp()
        except ValueError:
            pass

    pm = trade.get("postmortem") or {}
    try:
        if float(pm.get("timestamp") or 0) > 0:
            return float(pm["timestamp"])
    except Exception:
        pass
    return float(trade.get("timestamp") or 0)


def _bybit_api_summary(cutoff: float) -> dict:
    """Bybit 자체 Closed PnL 원장을 읽는다. 실패해도 로컬 리포트는 계속 만든다."""
    try:
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from trader import _ex

        ex = _ex()
        end_ms = int(time.time() * 1000)
        # Bybit는 start~end가 단 1ms라도 7일을 넘으면 10001을 반환한다.
        start_ms = max(int(cutoff * 1000), end_ms - 7 * 86400 * 1000 + 1000)
        params = {
            "category": "linear",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 100,
        }
        rows = []
        cursor = ""
        for _ in range(10):
            if cursor:
                params["cursor"] = cursor
            resp = ex.privateGetV5PositionClosedPnl(params)
            result = resp.get("result") or {}
            rows.extend(result.get("list") or [])
            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                break
        return {
            "ok": True,
            "rows": len(rows),
            "pnl": sum(float(r.get("closedPnl") or 0) for r in rows),
            "open_fee": sum(float(r.get("openFee") or 0) for r in rows),
            "close_fee": sum(float(r.get("closeFee") or 0) for r in rows),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def _binance_api_summary(cutoff: float) -> dict:
    """Binance USD-M Income 원장에서 실현손익·수수료·펀딩을 분리 집계한다."""
    try:
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from binance_trader import _ex

        ex = _ex()
        end_ms = int(time.time() * 1000)
        start_ms = max(int(cutoff * 1000), end_ms - 7 * 86400 * 1000 + 1000)
        rows = []
        seen = set()
        for page in range(1, 11):
            batch = ex.fapiPrivateGetIncome({
                "startTime": start_ms,
                "endTime": end_ms,
                "page": page,
                "limit": 1000,
            })
            if not isinstance(batch, list):
                batch = (batch or {}).get("rows") or []
            for row in batch:
                key = (
                    str(row.get("incomeType") or ""),
                    str(row.get("tranId") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
            if len(batch) < 1000:
                break

        totals = defaultdict(float)
        for row in rows:
            totals[str(row.get("incomeType") or "UNKNOWN")] += float(
                row.get("income") or 0
            )
        realized = totals["REALIZED_PNL"]
        commission = totals["COMMISSION"]
        funding = totals["FUNDING_FEE"]
        trade_pnl = realized + commission
        return {
            "ok": True,
            "rows": len(rows),
            # 로컬 거래 귀속에는 펀딩이 없으므로 원장 일치 비교는 체결손익+수수료로 한다.
            "pnl": trade_pnl,
            "realized_pnl": realized,
            "commission": commission,
            "funding_fee": funding,
            "account_net": trade_pnl + funding,
            "transfer": totals["TRANSFER"],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def _performance(closed: list[dict]) -> dict:
    pnls = [float(t.get("pnl_usd") or 0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "wins": len(wins),
        "losses": len(losses),
        "pnl": sum(pnls),
        "wr": (len(wins) / len(pnls) * 100) if pnls else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": gross_loss / len(losses) if losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
    }


def _venue_block(venue: str, cutoff: float, *, include_api: bool = True) -> dict:
    state = ROOT / ("trade_state_binance.json" if venue == "binance" else "trade_state.json")
    pm_path = ROOT / (
        "trade_postmortem_binance.jsonl" if venue == "binance" else "trade_postmortem.jsonl"
    )
    hist = _load_history(state)
    closed = [
        t for t in hist
        if t.get("status") in ("win", "loss") and _in_window(_close_timestamp(t), cutoff)
    ]
    perf = _performance(closed)

    # stack tags
    tagged = 0
    new_stack_pnl = 0.0
    new_stack_n = 0
    version_counts = Counter()
    for t in closed:
        ctx = t.get("entry_context") or {}
        a = t.get("logic_attribution") or ctx.get("logic_attribution") or {}
        version = (
            t.get("logic_stack_version")
            or ctx.get("logic_stack_version")
            or a.get("stack_version")
        )
        if version:
            version_counts[str(version)] += 1
        if a or version:
            tagged += 1
        if a.get("new_stack_applied") or any(
            "regime" in str(f) for f in (a.get("new_features") or [])
        ):
            new_stack_n += 1
            new_stack_pnl += float(t.get("pnl_usd") or 0)

    pms = [r for r in _load_jsonl(pm_path) if _in_window(r.get("timestamp"), cutoff)]
    cause_ctr = Counter(
        (r.get("primary_hypothesis") or r.get("primary_cause") or {}).get("code") or "unknown"
        for r in pms
    )
    strat_pnl = defaultdict(float)
    strat_n = defaultdict(int)
    for t in closed:
        st = t.get("strategy") or "?"
        strat_pnl[st] += float(t.get("pnl_usd") or 0)
        strat_n[st] += 1

    if include_api and venue == "bybit":
        api = _bybit_api_summary(cutoff)
    elif include_api and venue == "binance":
        api = _binance_api_summary(cutoff)
    else:
        api = {"ok": False}
    ledger_delta = (
        float(api.get("pnl") or 0) - perf["pnl"] if api.get("ok") else None
    )
    api_required = include_api
    ledger_reliable = (
        not api_required
        or (
            bool(api.get("ok"))
            and ledger_delta is not None
            and abs(ledger_delta) <= max(0.25, abs(float(api.get("pnl") or 0)) * 0.005)
        )
    )
    return {
        "venue": venue,
        "closed": len(closed),
        **perf,
        "tagged": tagged,
        "new_stack_n": new_stack_n,
        "new_stack_pnl": new_stack_pnl,
        "version_counts": version_counts,
        "causes": cause_ctr,
        "strat_pnl": dict(strat_pnl),
        "strat_n": dict(strat_n),
        "pm_n": len(pms),
        "api": api,
        "ledger_delta": ledger_delta,
        "ledger_reliable": ledger_reliable,
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
    for b in blocks:
        venue = b["venue"].upper()
        if not b["ledger_reliable"]:
            if b["ledger_delta"] is None:
                tips.append(
                    f"[{venue}] 거래소 원장 검증 실패 — 전략 변경 보류"
                )
            else:
                tips.append(
                    f"[{venue}] 거래소 원장-로컬 손익 차이 ${b['ledger_delta']:+.2f} — "
                    "전략 변경 금지, 체결 귀속부터 복구"
                )
            continue
        cohort_version, cohort_n = (
            b["version_counts"].most_common(1)[0]
            if b["version_counts"] else ("태그없음", 0)
        )
        if cohort_n < MIN_TRADES_FOR_HINT:
            tips.append(
                f"[{venue}] 동일버전 표본 부족 ({cohort_version} {cohort_n}건) — 현 버전 유지"
            )
            continue
        repeated = [(c, n) for c, n in b["causes"].most_common(3) if n >= MIN_CAUSE_REPEAT]
        if repeated:
            code, n = repeated[0]
            tips.append(f"[{venue} 관찰] 원인후보 {code} ×{n} — 체결 경로와 대조 필요")
        if b["strat_pnl"]:
            worst = min(b["strat_pnl"].items(), key=lambda x: x[1])
            if worst[1] < -1.0:
                tips.append(
                    f"[{venue} 관찰] 최악 전략 {worst[0]} pnl={worst[1]:+.2f} — "
                    "동일 버전 표본 확인 후 재평가"
                )

    if not tips:
        tips.append("특이 반복 원인 없음 — 현 스택 관측 유지. config 변경 불필요.")
    return tips[:5]


def build_report(*, include_api: bool = True) -> str:
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    blocks = [
        _venue_block("bybit", cutoff, include_api=include_api),
        _venue_block("binance", cutoff, include_api=include_api),
    ]
    tips = _suggestions(blocks)

    lines = [
        f"📊 GodOfTrading 주간 학습 리포트 ({now})",
        f"기간: 최근 {LOOKBACK_DAYS}일 | 스택 관측 + 포스트모템 (자동 수정 없음)",
        "",
    ]
    for b in blocks:
        if b["venue"] == "bybit" and b["api"].get("ok"):
            lines.append(
                f"[BYBIT 거래소원장] 청산기록 {b['api']['rows']} "
                f"closedPnl={b['api']['pnl']:+.2f} | "
                f"openFee={b['api']['open_fee']:.2f} closeFee={b['api']['close_fee']:.2f}"
            )
        elif b["venue"] == "bybit":
            lines.append(
                f"[BYBIT 거래소원장] 조회 실패 — 로컬 통계만 표시, 전략 제안 보류 "
                f"({b['api'].get('error') or 'API 미사용'})"
            )
        if b["venue"] == "binance" and b["api"].get("ok"):
            lines.append(
                f"[BINANCE 거래소원장] Income {b['api']['rows']}건 "
                f"realized={b['api']['realized_pnl']:+.2f} "
                f"commission={b['api']['commission']:+.2f} | "
                f"tradeNet={b['api']['pnl']:+.2f} "
                f"funding={b['api']['funding_fee']:+.2f} "
                f"accountNet={b['api']['account_net']:+.2f}"
            )
        elif b["venue"] == "binance":
            lines.append(
                f"[BINANCE 거래소원장] 조회 실패 — 로컬 통계만 표시, 전략 제안 보류 "
                f"({b['api'].get('error') or 'API 미사용'})"
            )
        lines.append(
            f"[{b['venue'].upper()} 로컬귀속] 청산 {b['closed']} (W{b['wins']}/L{b['losses']}) "
            f"WR {b['wr']:.0f}% pnl={b['pnl']:+.2f} | "
            f"equity={b['last_equity']} dd={b['drawdown_pct']}"
        )
        pf = "∞" if b["profit_factor"] is None else f"{b['profit_factor']:.2f}"
        lines.append(
            f"  PF {pf} | 평균익 +${b['avg_win']:.2f} / 평균손 -${b['avg_loss']:.2f}"
        )
        if b["ledger_delta"] is not None:
            lines.append(
                f"  원장-로컬 차이 ${b['ledger_delta']:+.2f} | "
                f"귀속 {'정상범위' if b['ledger_reliable'] else '불일치'}"
            )
        lines.append(
            f"  태그진입 {b['tagged']} | 신규스택관여≈{b['new_stack_n']} "
            f"pnl={b['new_stack_pnl']:+.2f} | 포스트모템 {b['pm_n']}건"
        )
        if b["version_counts"]:
            versions = ", ".join(
                f"{version}×{n}" for version, n in b["version_counts"].most_common(3)
            )
            lines.append(f"  로직버전 코호트: {versions}")
        if b["causes"]:
            top = ", ".join(f"{c}×{n}" for c, n in b["causes"].most_common(4))
            lines.append(f"  주요 패인/승인 코드: {top}")
        lines.append("")

    lines.append("💡 검증 판단 (원장 일치 + 동일버전 30건 전에는 구조 변경 없음)")
    for t in tips:
        lines.append(f"  • {t}")
    lines.append("")
    lines.append(
        "변경 기준: 거래소-로컬 원장 일치, 동일 로직버전 청산 30건 이상, "
        "PF·기대값·최대DD 동시 확인"
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
