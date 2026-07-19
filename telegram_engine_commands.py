#!/usr/bin/env python3
"""Read-only Telegram command console for GodOfTrading engines.

The service polls the existing trade and signal bots.  It never accepts order
commands; only configured chat IDs may read local engine state and statistics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import statistics
import threading
import time
from typing import Any, Callable, Optional

import requests
from dotenv import load_dotenv

from bot_util import load_json, read_jsonl
from config import (
    BINANCE_C1_ACCOUNT_RISK_PCT,
    BINANCE_C1_AUTO_PROMOTE_ENABLED,
    BINANCE_C1_ENGINE_ENABLED,
    BINANCE_C1_LIVE_ENABLED,
    BINANCE_C1_TOP_N,
    BINANCE_D2_ENGINE_ENABLED,
    BINANCE_D2_LIVE_ENABLED,
    BINANCE_MA200_PULLBACK_ENGINE_ENABLED,
    BINANCE_MA200_PULLBACK_LIVE_ENABLED,
)
from process_lock import release, try_acquire
from service_status import read_status, write_status


ROOT = Path(__file__).parent
STATE_FILE = ROOT / "telegram_engine_command_state.json"
BINANCE_TRADE_STATE = ROOT / "trade_state_binance.json"
BYBIT_TRADE_STATE = ROOT / "trade_state.json"
C1_STATE = ROOT / "binance_orderflow_challenger_state.json"
C1_JOURNAL = ROOT / "binance_orderflow_challenger_journal.jsonl"
PUMP_STATE = ROOT / "binance_pump_paper_state.json"
PUMP_JOURNAL = ROOT / "binance_pump_paper_journal.jsonl"
COPY_STATE = ROOT / "binance_copy_intel_state.json"
HL_WHALE_STATE = ROOT / "hyperliquid_whale_paper_state.json"
HL_WHALE_JOURNAL = ROOT / "hyperliquid_whale_paper_journal.jsonl"
HL_WHALE_POLICY = "2026-07-19-hl-position-delta-taker-v2"
HL_WHALE_LEGACY_POLICY = "2026-07-11-hl-fill-copy-v1"
SERVICE_NAME = "telegram_engine_commands"
KST = timezone(timedelta(hours=9))
MAX_MESSAGE = 3900

C1_V2_STRATEGY = "C1V2_BREAKOUT_RETEST_PERSISTENT_FLOW"
C1_V1_STRATEGY = "C1_ORDERFLOW_TREND_CONTINUATION"
C1_V2_POLICY = "2026-07-19-c1v2-breakout-retest-persistent-flow"
# Compatibility alias for callers that mean the current C1 strategy.
C1_STRATEGY = C1_V2_STRATEGY
D2_STRATEGY = "D2_DIVERGENCE_VOLUME_ASYMMETRIC"
D3_STRATEGY = "D3_4H_MA200_VOLUME_PULLBACK_LONG"
KNOWN_BINANCE = {C1_V2_STRATEGY, C1_V1_STRATEGY, D2_STRATEGY, D3_STRATEGY}

_stop = threading.Event()
_state_lock = threading.Lock()


@dataclass
class Route:
    name: str
    token: str
    allowed_chats: set[str]
    username: str = ""
    ok: bool = False
    last_error: str = ""
    last_update_ts: float = 0.0

    @property
    def key(self) -> str:
        digest = hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:12]
        return f"{self.name}:{digest}"


def _safe(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _now_text() -> str:
    return datetime.now(KST).strftime("%m/%d %H:%M KST")


def _load_offsets() -> dict[str, int]:
    payload = load_json(STATE_FILE, {}) or {}
    raw = payload.get("offsets") if isinstance(payload, dict) else {}
    return {str(k): int(v) for k, v in (raw or {}).items()}


def _save_offsets(offsets: dict[str, int]) -> None:
    payload = {"offsets": offsets, "updated_ts": time.time(), "updated_at": _now_text()}
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _telegram(route: Route, method: str, payload: Optional[dict[str, Any]] = None,
              *, timeout: float = 15.0) -> dict[str, Any]:
    response = requests.post(
        f"https://api.telegram.org/bot{route.token}/{method}",
        json=payload or {},
        timeout=timeout,
    )
    data = response.json()
    if not response.ok or not data.get("ok"):
        raise RuntimeError(str(data.get("description") or response.text)[:300])
    return data


def _split_message(text: str) -> list[str]:
    if len(text) <= MAX_MESSAGE:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= MAX_MESSAGE:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = line
    if current:
        chunks.append(current)
    return chunks


def _send(route: Route, chat_id: str, text: str) -> None:
    for chunk in _split_message(text):
        _telegram(
            route,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )


def _closed_rows(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = load_json(path, {}) or {}
    rows = [
        row for row in (state.get("trade_history") or [])
        if row.get("status") in {"win", "loss", "breakeven"}
    ]
    return state, rows


def _performance(rows: list[dict[str, Any]], *, reference_equity: float = 0.0,
                 open_count: int = 0) -> dict[str, float]:
    pnls = [_safe(row.get("pnl_usd")) for row in rows]
    wins = [value for value in pnls if value > 1e-9]
    losses = [value for value in pnls if value < -1e-9]
    breakeven = len(pnls) - len(wins) - len(losses)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    risk_values = []
    for row, pnl in zip(rows, pnls):
        risk = _safe(
            row.get("est_sl_loss")
            or (row.get("entry_context") or {}).get("est_sl_loss")
        )
        if risk > 0:
            risk_values.append(pnl / risk)
    return {
        "n": float(len(rows)),
        "wins": float(len(wins)),
        "losses": float(len(losses)),
        "be": float(breakeven),
        "win_rate": len(wins) / len(rows) * 100 if rows else 0.0,
        "net": sum(pnls),
        "pf": gross_win / gross_loss if gross_loss > 0 else (99.0 if wins else 0.0),
        "contribution_pct": sum(pnls) / reference_equity * 100 if reference_equity > 0 else 0.0,
        "avg_r": statistics.mean(risk_values) if risk_values else 0.0,
        "open": float(open_count),
    }


def _strategy_perf(path: Path, strategy: Optional[str] = None,
                   *, exclude: Optional[set[str]] = None) -> dict[str, float]:
    state, rows = _closed_rows(path)
    if strategy is not None:
        rows = [row for row in rows if row.get("strategy") == strategy]
    if exclude:
        rows = [row for row in rows if row.get("strategy") not in exclude]
    positions = state.get("positions") or {}
    if strategy is not None:
        open_count = sum(1 for row in positions.values() if row.get("strategy") == strategy)
    elif exclude:
        open_count = sum(1 for row in positions.values() if row.get("strategy") not in exclude)
    else:
        open_count = len(positions)
    return _performance(
        rows,
        reference_equity=_safe(state.get("equity_start")),
        open_count=open_count,
    )


def _journal_perf(path: Path, event: str, state_path: Path,
                  position_key: str = "positions",
                  policy: Optional[str] = None) -> dict[str, float]:
    rows = [
        row for row in read_jsonl(path)
        if row.get("event") == event and (policy is None or row.get("policy") == policy)
    ]
    state = load_json(state_path, {}) or {}
    positions = state.get(position_key) or {}
    if policy is not None and state.get("policy") != policy:
        positions = {}
    initial = _safe(state.get("initial_bankroll") or state.get("seed_usdt"))
    synthetic = [{"pnl_usd": _safe(row.get("net_usd"))} for row in rows]
    return _performance(synthetic, reference_equity=initial, open_count=len(positions))


def _hyperliquid_snapshot() -> dict[str, Any]:
    state = load_json(HL_WHALE_STATE, {}) or {}
    all_settled = [
        row for row in read_jsonl(HL_WHALE_JOURNAL)
        if row.get("event") == "settled"
    ]
    settled = [
        row for row in all_settled
        if str(row.get("policy") or HL_WHALE_LEGACY_POLICY) == HL_WHALE_POLICY
    ]
    legacy_settled = [
        row for row in all_settled
        if str(row.get("policy") or HL_WHALE_LEGACY_POLICY) == HL_WHALE_LEGACY_POLICY
    ]
    synthetic = [{"pnl_usd": _safe(row.get("pnl_usd"))} for row in settled]
    legacy_synthetic = [
        {"pnl_usd": _safe(row.get("pnl_usd"))} for row in legacy_settled
    ]
    all_positions = state.get("open_positions") or []
    positions = [
        row for row in all_positions
        if str(row.get("policy") or HL_WHALE_LEGACY_POLICY) == HL_WHALE_POLICY
    ]
    legacy_positions = [row for row in all_positions if row not in positions]
    perf = _performance(synthetic, reference_equity=1000.0, open_count=len(positions))
    legacy_perf = _performance(
        legacy_synthetic, reference_equity=1000.0, open_count=len(legacy_positions)
    )
    equity = peak = max_dd = 0.0
    for row in synthetic:
        equity += _safe(row.get("pnl_usd"))
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    by_wallet: dict[str, dict[str, float]] = {}
    by_reason: dict[str, int] = {}
    for row in settled:
        wallet = str(row.get("wallet") or "unknown")[:12]
        bucket = by_wallet.setdefault(wallet, {"pnl": 0.0, "n": 0.0, "wins": 0.0})
        pnl = _safe(row.get("pnl_usd"))
        bucket["pnl"] += pnl
        bucket["n"] += 1
        bucket["wins"] += float(pnl > 0)
        reason = str(row.get("settle_reason") or "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    unrealized = sum(_safe(row.get("unrealized_pnl")) for row in positions)
    policy_bankroll = _safe(
        (state.get("policy_bankrolls") or {}).get(HL_WHALE_POLICY), 1000.0
    )
    return {
        "state": state,
        "settled": settled,
        "positions": positions,
        "legacy_positions": legacy_positions,
        "perf": perf,
        "legacy_perf": legacy_perf,
        "max_dd": max_dd,
        "unrealized": unrealized,
        "policy_bankroll": policy_bankroll,
        "estimated_equity": policy_bankroll + unrealized,
        "by_wallet": by_wallet,
        "by_reason": by_reason,
    }


def _fmt_perf(label: str, perf: dict[str, float]) -> str:
    net = perf["net"]
    net_text = f"{'+' if net >= 0 else '-'}${abs(net):.2f}"
    return (
        f"<b>{escape(label)}</b>  {int(perf['wins'])}W/{int(perf['losses'])}L/{int(perf['be'])}BE "
        f"(n={int(perf['n'])}, {perf['win_rate']:.1f}%)\n"
        f"  PF {perf['pf']:.2f} · 순익 {net_text} · "
        f"계좌기준 {perf['contribution_pct']:+.2f}% · 평균 {perf['avg_r']:+.2f}R "
        f"· 보유 {int(perf['open'])}"
    )


def build_engines(argument: str = "") -> str:
    argument = argument.lower().strip()
    if argument in {"hyperliquid", "whale", "whales"}:
        argument = "hl"
    details = {
        "c1": (
            "<b>C1 v2 · 돌파-눌림 지속수급 Challenger</b>\n"
            f"상위 {BINANCE_C1_TOP_N}종목·완료 5m 돌파/눌림·30초 수급 지속·"
            "BTC 정렬/상대강도·LONG 전용\n"
            f"신규 LIVE {'ON' if BINANCE_C1_LIVE_ENABLED else 'LOCKED'} · "
            f"PAPER 50건/PF≥1.20/순익 양수 졸업 후 자동승격 "
            f"{'ON' if BINANCE_C1_AUTO_PROMOTE_ENABLED else 'OFF'} · "
            f"승격 후 계좌위험 {BINANCE_C1_ACCOUNT_RISK_PCT*100:.2f}%\n"
            "C1 v1은 비용후 음의 기대값으로 폐기"
        ),
        "d2": (
            "<b>D2 · 다중봉 다이버전스</b>\n"
            "RSI/Stoch/CCI/MACD 3-of-4 + 거래량 + 5m 실행확인\n"
            f"후보스캔 {'ON' if BINANCE_D2_ENGINE_ENABLED else 'OFF'} · "
            f"신규 LIVE {'ON' if BINANCE_D2_LIVE_ENABLED else 'OFF'} · 기존 포지션 관리 유지"
        ),
        "d3": (
            "<b>D3 · 4h MA200 돌파 후 눌림롱</b>\n"
            "거래량 돌파 후 MA200/볼린저 중단 되돌림·15m/5m 반등\n"
            f"엔진 {'ON' if BINANCE_MA200_PULLBACK_ENGINE_ENABLED else 'OFF'} · "
            f"LIVE {'ON' if BINANCE_MA200_PULLBACK_LIVE_ENABLED else 'OFF'}"
        ),
        "bybit": (
            "<b>Bybit · 기존 복합 실매매</b>\n"
            "EMA 눌림/돌파·RSI2·BTC Sync·다이버전스 전략 복합\n"
            "전략별 성과는 /results, 현재 보유는 /positions에서 확인"
        ),
        "paper": (
            "<b>PAPER 엔진</b>\n"
            "C1 동시 미러·Binance 급등 레이더·Copy Intel shadow\n"
            "실주문 없이 수수료/슬리피를 반영해 forward 성과 축적"
        ),
        "hl": (
            "<b>Hyperliquid · 고래 순포지션 PAPER v2</b>\n"
            "12개 지갑의 taker open을 30초마다 집계하고 실제 순포지션·계좌대비 확신도를 재확인\n"
            "PAPER 전용 · 자동 LIVE 전환 없음 · 결과는 /hyperliquid"
        ),
    }
    if argument in details:
        return f"⚙️ <b>엔진 상세</b> · {_now_text()}\n\n{details[argument]}"
    return "\n\n".join([
        f"⚙️ <b>GodOfTrading 엔진 맵</b> · {_now_text()}",
        details["c1"], details["d2"], details["d3"], details["bybit"], details["paper"], details["hl"],
        "상세: <code>/engines c1</code> · <code>/engines d2</code> · "
        "<code>/engines d3</code> · <code>/engines bybit</code> · <code>/engines hl</code>",
    ])


def build_results(argument: str = "") -> str:
    argument = argument.lower().strip()
    perfs = {
        "c1v2": ("Binance C1 v2 LIVE", _strategy_perf(BINANCE_TRADE_STATE, C1_V2_STRATEGY)),
        "c1v1": ("Binance C1 v1 LIVE (폐기)", _strategy_perf(BINANCE_TRADE_STATE, C1_V1_STRATEGY)),
        "d2": ("Binance D2 LIVE", _strategy_perf(BINANCE_TRADE_STATE, D2_STRATEGY)),
        "d3": ("Binance D3 LIVE", _strategy_perf(BINANCE_TRADE_STATE, D3_STRATEGY)),
        "binance_other": ("Binance 기타 LIVE", _strategy_perf(BINANCE_TRADE_STATE, exclude=KNOWN_BINANCE)),
        "bybit": ("Bybit 전체 LIVE", _strategy_perf(BYBIT_TRADE_STATE)),
    }
    lines = [f"📊 <b>실매매 엔진 결과</b> · {_now_text()}"]
    if argument == "c1":
        for key in ("c1v2", "c1v1"):
            label, perf = perfs[key]
            lines.extend(["", _fmt_perf(label, perf)])
    elif argument in perfs:
        label, perf = perfs[argument]
        lines.extend(["", _fmt_perf(label, perf)])
    else:
        for key in ("c1v2", "c1v1", "d2", "d3", "binance_other", "bybit"):
            label, perf = perfs[key]
            lines.extend(["", _fmt_perf(label, perf)])
    lines.extend([
        "",
        "※ 계좌기준% = 해당 전략 누적 순손익 / 엔진 기준 equity.",
        "PAPER 결과는 <code>/paper</code>, 보유 포지션은 <code>/positions</code>",
    ])
    return "\n".join(lines)


def build_paper(_argument: str = "") -> str:
    c1_v2 = _journal_perf(
        C1_JOURNAL, "paper_close", C1_STATE, policy=C1_V2_POLICY,
    )
    c1_v1 = _journal_perf(
        C1_JOURNAL, "paper_close", C1_STATE,
        policy="2026-07-19-c1-orderflow-v1",
    )
    pump = _journal_perf(PUMP_JOURNAL, "paper_settled", PUMP_STATE)
    copy_state = load_json(COPY_STATE, {}) or {}
    shadow = copy_state.get("shadow") or {}
    copy_rows = [{"pnl_usd": _safe(row.get("net_usd"))} for row in (shadow.get("closed") or [])]
    copy_perf = _performance(
        copy_rows,
        reference_equity=_safe(copy_state.get("seed_usdt")),
        open_count=len(shadow.get("positions") or {}),
    )
    return "\n\n".join([
        f"🧪 <b>PAPER / SHADOW 결과</b> · {_now_text()}",
        _fmt_perf("C1 v2 돌파-눌림 PAPER", c1_v2),
        _fmt_perf("C1 v1 주문흐름 PAPER (폐기)", c1_v1),
        _fmt_perf("Binance 급등 레이더 PAPER", pump),
        _fmt_perf("Binance Copy Intel SHADOW", copy_perf),
        "※ 수수료·슬리피 반영 forward 기록. LIVE 손익과 분리.",
    ])


def build_hyperliquid(_argument: str = "") -> str:
    snap = _hyperliquid_snapshot()
    state = snap["state"]
    perf = snap["perf"]
    positions = snap["positions"]
    legacy_positions = snap["legacy_positions"]
    legacy = snap["legacy_perf"]
    last_scan = state.get("last_scan") or {}
    n = int(perf["n"])
    if n < 20:
        verdict = f"표본 수집 중 {n}/20"
    elif perf["net"] < 0 or perf["pf"] < 1.0:
        verdict = "⚠️ 음의 기대값 — LIVE 전환 금지"
    elif perf["pf"] >= 1.20 and perf["net"] > 0:
        verdict = "긍정 후보 — 지갑별 안정성 추가검증 필요"
    else:
        verdict = "경계 구간 — PAPER 유지"
    lines = [
        f"🌊 <b>Hyperliquid 고래 PAPER v2 결과</b> · {_now_text()}",
        "",
        f"정산 {int(perf['wins'])}W/{int(perf['losses'])}L/{int(perf['be'])}BE "
        f"(n={n}, 승률 {perf['win_rate']:.1f}%)",
        f"PF {perf['pf']:.2f} · 누적 ${perf['net']:+.2f} · "
        f"건당 ${perf['net']/n:+.3f}" if n else "PF 0.00 · 누적 $+0.00 · 건당 $+0.000",
        f"v2 bankroll ${snap['policy_bankroll']:.2f} · "
        f"최대DD ${snap['max_dd']:.2f}",
        f"오픈 {len(positions)}건 · 미실현 ${snap['unrealized']:+.2f} · "
        f"추정 equity ${snap['estimated_equity']:.2f}",
        f"추적 지갑 {int((last_scan.get('wallets') or len(state.get('wallets') or {})))}개 · "
        f"최근 스캔 {escape(str(last_scan.get('time') or '기록 없음'))}",
        f"판정: <b>{verdict}</b>",
        "",
        f"<b>폐기 v1 기준선</b> {int(legacy['wins'])}W/{int(legacy['losses'])}L/"
        f"{int(legacy['be'])}BE (n={int(legacy['n'])}) · PF {legacy['pf']:.2f} · "
        f"${legacy['net']:+.2f} · 잔여오픈 {len(legacy_positions)}",
    ]
    if positions:
        lines.extend(["", "<b>현재 오픈</b>"])
        for row in positions[:6]:
            lines.append(
                f"• {escape(str(row.get('direction') or ''))} "
                f"{escape(str(row.get('coin') or ''))} · "
                f"uPnL ${_safe(row.get('unrealized_pnl')):+.2f}"
            )
    by_wallet = snap["by_wallet"]
    if by_wallet:
        ranked = sorted(by_wallet.items(), key=lambda item: item[1]["pnl"], reverse=True)
        lines.extend(["", "<b>지갑별 실현 PnL</b>"])
        for wallet, data in ranked[:3]:
            lines.append(
                f"• {escape(wallet)}… n={int(data['n'])} · ${data['pnl']:+.2f}"
            )
        if len(ranked) > 3:
            weakest_wallet, weakest = ranked[-1]
            lines.append(
                f"• 최하 {escape(weakest_wallet)}… n={int(weakest['n'])} · "
                f"${weakest['pnl']:+.2f}"
            )
    reasons = snap["by_reason"]
    if reasons:
        reason_text = " · ".join(
            f"{escape(reason)} {count}건"
            for reason, count in sorted(reasons.items(), key=lambda item: -item[1])
        )
        lines.extend(["", f"정산 사유: {reason_text}"])
    lines.extend([
        "",
        "※ 30초 주기 PAPER 전용이며 Hyperliquid 실주문은 하지 않습니다.",
    ])
    return "\n".join(lines)


def _engine_label(strategy: str) -> str:
    return {
        C1_V2_STRATEGY: "C1v2",
        C1_V1_STRATEGY: "C1v1(폐기)",
        D2_STRATEGY: "D2",
        D3_STRATEGY: "D3",
    }.get(strategy, strategy or "Legacy")


def build_positions(_argument: str = "") -> str:
    lines = [f"📌 <b>현재 실포지션</b> · {_now_text()}"]
    total = 0
    now = time.time()
    for venue, path in (("BIN", BINANCE_TRADE_STATE), ("BYB", BYBIT_TRADE_STATE)):
        state = load_json(path, {}) or {}
        positions = state.get("positions") or {}
        lines.append(f"\n<b>{venue}</b> 보유 {len(positions)}")
        for symbol, row in list(positions.items())[:20]:
            total += 1
            age = max((now - _safe(row.get("opened_ts"))) / 60, 0.0)
            direction = "LONG" if row.get("direction") == "LONG" else "SHORT"
            lines.append(
                f"• <b>{escape(str(symbol))}</b> {direction} · "
                f"{escape(_engine_label(str(row.get('strategy') or '')))} · "
                f"진입 {_safe(row.get('entry_price')):.8g} · {age:.0f}분"
            )
        if len(positions) > 20:
            lines.append(f"… 외 {len(positions)-20}건")
    if total == 0:
        lines.append("\n현재 추적 중인 실포지션이 없습니다.")
    lines.append("\n※ 실시간 평가손익이 아닌 진입가/보유시간 조회입니다.")
    return "\n".join(lines)


def _status_line(label: str, service: str, max_age: float) -> str:
    status = read_status(service)
    heartbeat = _safe(status.get("heartbeat_ts"))
    age = time.time() - heartbeat if heartbeat else 999999.0
    healthy = bool(status.get("ok")) and 0 <= age <= max_age
    suffix = f"{age:.0f}초 전" if heartbeat else "heartbeat 없음"
    icon = "🟢" if healthy else "🔴"
    return f"{icon} {label} · {suffix}"


def build_status(_argument: str = "") -> str:
    c1 = read_status("binance_orderflow_challenger")
    command = read_status(SERVICE_NAME)
    lines = [
        f"🩺 <b>자동매매 시스템 상태</b> · {_now_text()}",
        "",
        _status_line("Binance 시세 수집", "binance_market_collector", 45),
        _status_line("Binance 포지션 관리", "binance_position_manager", 20),
        _status_line("C1 주문흐름", "binance_orderflow_challenger", 20),
        _status_line("Telegram 명령", SERVICE_NAME, 45),
        "",
        f"C1 v2 연결 {bool(c1.get('connected'))} · LIVE-ARMED {bool(c1.get('live_armed'))} · "
        f"LIVE-ACTIVE {bool(c1.get('live_enabled'))} · "
        f"감시 {int(c1.get('symbols') or 0)}종목 · 메시지지연 {_safe(c1.get('message_age_seconds')):.3f}초",
        f"C1 PAPER 졸업 {bool(c1.get('paper_graduated'))} · "
        f"보유 {int(c1.get('paper_positions') or 0)} · 처리메시지 {int(c1.get('messages') or 0):,}",
        f"C1 LIVE 게이트: {escape(str(c1.get('live_gate_reason') or '상태 대기'))}",
        f"명령 라우트 {int(command.get('routes_ok') or 0)}/{int(command.get('routes') or 0)}",
    ]
    return "\n".join(lines)


def build_help(_argument: str = "") -> str:
    return "\n".join([
        "🤖 <b>GodOfTrading 조회 명령</b>",
        "",
        "<code>/engines</code> — 운영 중인 엔진 설명",
        "<code>/engines c1</code> — 특정 엔진 상세",
        "<code>/results</code> — 실매매 엔진별 승패·PF·순익",
        "<code>/results c1</code> — 특정 엔진 성과",
        "<code>/paper</code> — PAPER/SHADOW 결과",
        "<code>/hyperliquid</code> — HL 고래추종 PAPER 상세",
        "<code>/positions</code> — 현재 실포지션",
        "<code>/status</code> — 수집·관리·실시간 서비스 상태",
        "<code>/help</code> — 이 도움말",
        "",
        "※ 조회 전용이며 텔레그램 명령으로 주문을 낼 수 없습니다.",
    ])


COMMANDS: dict[str, Callable[[str], str]] = {
    "engine": build_engines,
    "engines": build_engines,
    "result": build_results,
    "results": build_results,
    "paper": build_paper,
    "hyperliquid": build_hyperliquid,
    "hl": build_hyperliquid,
    "whales": build_hyperliquid,
    "positions": build_positions,
    "position": build_positions,
    "status": build_status,
    "help": build_help,
    "start": build_help,
}


def dispatch(text: str) -> Optional[str]:
    raw = str(text or "").strip()
    if not raw.startswith("/"):
        return None
    parts = raw.split(maxsplit=1)
    command = parts[0][1:].split("@", 1)[0].lower()
    argument = parts[1] if len(parts) > 1 else ""
    builder = COMMANDS.get(command)
    if not builder:
        return build_help()
    return builder(argument)


def _allowed_extra() -> set[str]:
    raw = os.getenv("TELEGRAM_COMMAND_ALLOWED_CHAT_IDS", "")
    return {part.strip() for part in raw.replace(";", ",").split(",") if part.strip()}


def _routes() -> list[Route]:
    extras = _allowed_extra()
    merged: dict[str, Route] = {}
    for name, token_key, chat_key in (
        ("trade", "TRADE_BOT_TOKEN", "TRADE_CHAT_ID"),
        ("signal", "SIGNAL_BOT_TOKEN", "SIGNAL_CHAT_ID"),
    ):
        token = os.getenv(token_key, "").strip()
        chat = os.getenv(chat_key, "").strip()
        if not token or "여기에" in token:
            continue
        if token in merged:
            if chat:
                merged[token].allowed_chats.add(chat)
            merged[token].allowed_chats.update(extras)
            merged[token].name += f"+{name}"
        else:
            allowed = set(extras)
            if chat:
                allowed.add(chat)
            merged[token] = Route(name=name, token=token, allowed_chats=allowed)
    return list(merged.values())


def _register(route: Route) -> None:
    me = _telegram(route, "getMe")
    route.username = str((me.get("result") or {}).get("username") or "")
    commands = [
        {"command": "engines", "description": "운영 중인 매매엔진 설명"},
        {"command": "results", "description": "엔진별 실매매 승패·순익"},
        {"command": "paper", "description": "PAPER/SHADOW 검증 결과"},
        {"command": "hyperliquid", "description": "HL 고래추종 PAPER 결과"},
        {"command": "positions", "description": "현재 실포지션"},
        {"command": "status", "description": "실시간 서비스 상태"},
        {"command": "help", "description": "명령어 도움말"},
    ]
    _telegram(route, "setMyCommands", {"commands": commands})


def _prime_offset(route: Route, offsets: dict[str, int]) -> int:
    if route.key in offsets:
        return int(offsets[route.key])
    data = _telegram(route, "getUpdates", {"offset": -1, "limit": 1, "timeout": 0})
    updates = data.get("result") or []
    offset = int(updates[-1]["update_id"]) + 1 if updates else 0
    with _state_lock:
        offsets[route.key] = offset
        _save_offsets(offsets)
    return offset


def _handle_update(route: Route, update: dict[str, Any]) -> None:
    message = update.get("message") or update.get("channel_post") or {}
    text = str(message.get("text") or "")
    chat_id = str((message.get("chat") or {}).get("id") or "")
    if not text.startswith("/") or not chat_id:
        return
    if chat_id not in route.allowed_chats:
        print(f"[telegram-commands] unauthorized chat ignored: {chat_id}")
        return
    response = dispatch(text)
    if response:
        _send(route, chat_id, response)


def _poll(route: Route, offsets: dict[str, int]) -> None:
    try:
        _register(route)
        offset = _prime_offset(route, offsets)
        route.ok = True
        print(
            f"[telegram-commands] @{route.username or route.name} ready "
            f"allowed_chats={len(route.allowed_chats)}"
        )
    except Exception as exc:
        route.last_error = str(exc)[:300]
        print(f"[telegram-commands] route init failed {route.name}: {exc}")
        return
    while not _stop.is_set():
        try:
            data = _telegram(
                route,
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": ["message", "channel_post"],
                },
                timeout=35,
            )
            for update in data.get("result") or []:
                offset = max(offset, int(update.get("update_id") or 0) + 1)
                try:
                    _handle_update(route, update)
                except Exception as exc:
                    route.last_error = str(exc)[:300]
                    print(f"[telegram-commands] update failed {route.name}: {exc}")
                with _state_lock:
                    offsets[route.key] = offset
                    _save_offsets(offsets)
            route.ok = True
            route.last_update_ts = time.time()
            route.last_error = ""
        except Exception as exc:
            route.ok = False
            route.last_error = str(exc)[:300]
            print(f"[telegram-commands] poll failed {route.name}: {exc}")
            _stop.wait(3.0)


def _request_stop(*_args) -> None:
    _stop.set()


def run() -> int:
    load_dotenv(ROOT / ".env")
    if not try_acquire(SERVICE_NAME):
        return 0
    routes = _routes()
    if not routes:
        print("[telegram-commands] no configured Telegram routes")
        release(SERVICE_NAME)
        return 1
    offsets = _load_offsets()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    threads = [
        threading.Thread(target=_poll, args=(route, offsets), name=f"tg-{route.name}", daemon=True)
        for route in routes
    ]
    for thread in threads:
        thread.start()
    try:
        while not _stop.wait(5.0):
            write_status(
                SERVICE_NAME,
                {
                    "ok": any(route.ok for route in routes),
                    "routes": len(routes),
                    "routes_ok": sum(route.ok for route in routes),
                    "bots": [
                        {
                            "name": route.name,
                            "username": route.username,
                            "ok": route.ok,
                            "allowed_chats": len(route.allowed_chats),
                            "last_update_age": (
                                round(time.time() - route.last_update_ts, 1)
                                if route.last_update_ts else None
                            ),
                            "error": route.last_error,
                        }
                        for route in routes
                    ],
                },
            )
    finally:
        write_status(SERVICE_NAME, {"ok": False, "stopped": True})
        release(SERVICE_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
