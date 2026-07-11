#!/usr/bin/env python3
"""Hyperliquid 고래 지갑 paper 카피 봇 (골격).

폴리 고래 봇과 같은 패턴:
  1) config의 whales/seed 지갑 userFills 폴링
  2) 큰 신규 체결 → paper 포지션 (고정 노셔널, 레버 캡)
  3) 마크 가격으로 미실현 추적 / 청산 감지 시 정산
  4) 텔레그램 리포트

실주문 없음. 지갑 리스트는 tools/hl_whale_screen.py 로 채운다.

  python3 hyperliquid_whale_paper_bot.py
  python3 hyperliquid_whale_paper_bot.py --report-now
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL.*", category=Warning)

import requests
from dotenv import load_dotenv

from publisher import send_review

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

CONFIG_FILE = ROOT / "hyperliquid_whale_config.json"
STATE_FILE = ROOT / "hyperliquid_whale_paper_state.json"
JOURNAL_FILE = ROOT / "hyperliquid_whale_paper_journal.jsonl"
API = "https://api.hyperliquid.xyz/info"

from bot_util import (  # noqa: E402
    KST,
    append_jsonl,
    json_safe as _json_safe,
    load_json,
    now as _now,
    now_kst as _now_kst,
    save_json,
)


def _load_config() -> dict:
    data = load_json(CONFIG_FILE, default=None)
    if isinstance(data, dict):
        return data
    return {"params": {}, "whales": [], "seed_wallets": []}

def _params(cfg: dict) -> dict:
    p = cfg.get("params") or {}
    return {
        # 2026-07-12: 5000→3000. Open 레그의 ~72%가 $5k 미만이지만 $2k는
        # 분할/시드 노이즈 비중이 큼. $3k는 7d 후보 ~1.25×, 품질 타협점.
        "min_fill_notional_usd": float(p.get("min_fill_notional_usd", 3000)),
        "copy_notional_usd": float(p.get("copy_notional_usd", 25)),
        "max_leverage_copy": float(p.get("max_leverage_copy", 5)),
        "max_open_positions": int(p.get("max_open_positions", 8)),
        "slippage_bps": float(p.get("slippage_bps", 15)),
        "report_interval_seconds": int(p.get("report_interval_seconds", 4 * 3600)),
        "max_hold_hours": float(p.get("max_hold_hours", 48)),
        "min_whale_flat_age_sec": float(p.get("min_whale_flat_age_sec", 180)),
    }


def _wallets(cfg: dict) -> list[str]:
    out = []
    # env override/add: HL_WHALE_WALLETS=0xabc,0xdef
    import os
    env_w = os.getenv("HL_WHALE_WALLETS", "").strip()
    if env_w:
        for part in env_w.split(","):
            a = part.strip().lower()
            if a.startswith("0x"):
                out.append(a)
    for w in cfg.get("seed_wallets") or []:
        if isinstance(w, str) and w.startswith("0x"):
            out.append(w.lower())
    for w in cfg.get("whales") or []:
        addr = (w.get("wallet") if isinstance(w, dict) else w) or ""
        if str(addr).startswith("0x"):
            out.append(str(addr).lower())
    return list(dict.fromkeys(out))


# 정산 표본 마일스톤 — 도달 시 1회 텔레그램 리마인드 (까먹지 않게)
# 20: 1차 판단 가능 / 30: 모수·min_fill 재평가 / 50: 라이브 검토 후보
SAMPLE_MILESTONES = (20, 30, 50)


def _load_state(cfg: dict) -> dict:
    data = load_json(STATE_FILE, default=None)
    if isinstance(data, dict):
        data.setdefault("sample_milestones_sent", {})
        return data
    return {
        "wallets": {
            w: {"last_fill_time": 0, "status": "active", "copied_keys": {}}
            for w in _wallets(cfg)
        },
        "open_positions": [],
        "bankroll": 1000.0,
        "last_report_time": 0.0,
        "last_scan": {},
        "sample_milestones_sent": {},
    }


def _save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


def _append(row: dict) -> None:
    append_jsonl(JOURNAL_FILE, row)


def _post(body: dict, retries: int = 3, timeout: int = 25) -> Any:
    """HL info API — 간헐적 네트워크 실패 재시도."""
    last: Exception | None = None
    for i in range(max(1, retries)):
        try:
            r = requests.post(API, json=body, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i + 1 < retries:
                time.sleep(0.35 * (i + 1))
    assert last is not None
    raise last


def _coin_dex(coin: str) -> str:
    """'xyz:ORCL' → 'xyz', 'BTC' → '' (메인 perp dex)."""
    c = str(coin or "")
    if ":" in c:
        return c.split(":", 1)[0].strip()
    return ""


def _coins_match(a: str, b: str) -> bool:
    """심볼 매칭 (정확 일치 또는 dex 접두 정규화)."""
    if not a or not b:
        return False
    if a == b:
        return True
    # xyz:ORCL vs ORCL (드묾)
    al, bl = a.split(":")[-1], b.split(":")[-1]
    if al == bl and (":" in a or ":" in b):
        # 같은 base 여도 dex 다르면 다른 마켓 — 한쪽만 prefix 있을 때만 허용
        return (":" in a) != (":" in b)
    return False


def _user_fills(addr: str) -> list[dict]:
    data = _post({"type": "userFills", "user": addr})
    return data if isinstance(data, list) else []


def _perp_dex_names() -> list[str]:
    """HIP-3 등 추가 perp dex 이름 목록."""
    try:
        data = _post({"type": "perpDexs"})
        if not isinstance(data, list):
            return []
        out = []
        for d in data:
            if isinstance(d, dict) and d.get("name"):
                out.append(str(d["name"]))
        return out
    except Exception:
        return ["xyz"]  # 폴백: 주식 perp 등


def _mids() -> dict[str, float]:
    """coin -> mid. 메인 + 모든 perp dex allMids 병합."""
    out: dict[str, float] = {}
    try:
        data = _post({"type": "allMids"})
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    out[str(k)] = float(v)
                except Exception:
                    pass
    except Exception:
        pass
    for dex in _perp_dex_names():
        try:
            data = _post({"type": "allMids", "dex": dex})
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        out[str(k)] = float(v)
                    except Exception:
                        pass
        except Exception:
            continue
    return out


def _clearinghouse(addr: str, dex: str = "") -> dict | None:
    """포지션 조회. dex='' 메인, 'xyz' 등 HIP-3.

    실패 시 None (빈 dict 과 구분 — 실패를 whale_flat 로 오인 금지).
    """
    body: dict[str, Any] = {"type": "clearinghouseState", "user": addr}
    if dex:
        body["dex"] = dex
    try:
        data = _post(body)
        if isinstance(data, dict) and (
            "assetPositions" in data or "marginSummary" in data
        ):
            return data
        return None
    except Exception:
        return None


def _position_szi(ch: dict, coin: str) -> float | None:
    """clearinghouse 에서 coin 포지션 szi. 없으면 0. 조회 불가면 None 아님 — 호출측에서 ch None 처리."""
    for ap in ch.get("assetPositions") or []:
        if not isinstance(ap, dict):
            continue
        pos_inner = ap.get("position") if isinstance(ap.get("position"), dict) else ap
        c = str(pos_inner.get("coin") or "")
        if not _coins_match(c, coin) and c != coin:
            continue
        try:
            return float(pos_inner.get("szi") or 0)
        except Exception:
            return 0.0
    return 0.0

def _fill_direction(f: dict) -> str:
    """HL side: B=buy/bid, A=sell/ask. dir 필드가 있으면 우선."""
    d = str(f.get("dir") or "").lower()
    if "long" in d or d in ("open long", "close short"):
        # Open Long / Close Short → 우리는 long 쪽 노출 증가/숏청산 = LONG 카피 신호로 Open Long만
        if "open long" in d:
            return "LONG"
        if "open short" in d:
            return "SHORT"
        # close * 는 진입 카피 대상 아님
        if "close" in d:
            return "CLOSE"
    side_raw = str(f.get("side") or "").lower()
    if side_raw in ("b", "buy"):
        return "LONG"
    return "SHORT"


def scan_signals(state: dict, cfg: dict) -> list[dict]:
    p = _params(cfg)
    signals = []
    for wallet in _wallets(cfg):
        wstate = state["wallets"].setdefault(
            wallet, {"last_fill_time": 0, "status": "active", "copied_keys": {}, "seeded": False}
        )
        if wstate.get("status") == "suspended":
            continue
        try:
            fills = _user_fills(wallet)
            wstate.pop("last_error", None)
        except Exception as e:
            wstate["last_error"] = str(e)[:200]
            continue
        fills = sorted(fills, key=lambda f: int(f.get("time") or 0))
        if not fills:
            continue

        # 콜드스타트: 과거 전량 카피 금지 — 커서만 최신 체결로 시드
        last_t = int(wstate.get("last_fill_time") or 0)
        if not wstate.get("seeded") and last_t <= 0:
            newest = max(int(f.get("time") or 0) for f in fills)
            wstate["last_fill_time"] = newest
            wstate["seeded"] = True
            wstate["seeded_at"] = _now_kst()
            continue

        wstate["seeded"] = True
        for f in fills:
            t = int(f.get("time") or 0)
            if t <= last_t:
                continue
            try:
                px = float(f.get("px") or 0)
                sz = float(f.get("sz") or 0)
                notional = abs(px * sz)
            except Exception:
                continue
            wstate["last_fill_time"] = max(int(wstate.get("last_fill_time") or 0), t)
            if notional < p["min_fill_notional_usd"]:
                continue
            coin = str(f.get("coin") or "")
            if not coin or coin.startswith("@"):
                continue
            direction = _fill_direction(f)
            if direction == "CLOSE":
                continue  # 청산 체결은 별도 settle 로직이 처리
            key = f"{wallet}:{coin}:{t}:{direction}"
            if wstate.setdefault("copied_keys", {}).get(key):
                continue
            wstate["copied_keys"][key] = True
            if len(wstate["copied_keys"]) > 500:
                items = list(wstate["copied_keys"].items())[-200:]
                wstate["copied_keys"] = dict(items)
            signals.append({
                "wallet": wallet,
                "coin": coin,
                "direction": direction,
                "px": px,
                "sz": sz,
                "notional": notional,
                "fill_time": t,
                "key": key,
                "dir_raw": f.get("dir"),
            })
    return signals


def open_paper(signals: list[dict], state: dict, cfg: dict) -> int:
    p = _params(cfg)
    mids = _mids()
    opened = 0
    for sig in signals:
        open_n = len(state.get("open_positions") or [])
        if open_n >= p["max_open_positions"]:
            break
        # one position per coin
        if any(x.get("coin") == sig["coin"] for x in state.get("open_positions") or []):
            continue
        mid = float(mids.get(sig["coin"]) or sig["px"] or 0)
        if mid <= 0:
            continue
        slip = p["slippage_bps"] / 10000.0
        entry = mid * (1 + slip) if sig["direction"] == "LONG" else mid * (1 - slip)
        notional = p["copy_notional_usd"]
        pos = {
            "wallet": sig["wallet"],
            "coin": sig["coin"],
            "direction": sig["direction"],
            "entry_price": round(entry, 6),
            "notional_usd": notional,
            "qty": round(notional / entry, 8),
            "max_leverage": p["max_leverage_copy"],
            "whale_notional": round(sig["notional"], 2),
            "opened_at": _now_kst(),
            "opened_ts": _now(),
            "source_fill_time": sig["fill_time"],
        }
        state.setdefault("open_positions", []).append(pos)
        _append({**pos, "event": "opened"})
        opened += 1
        print(
            f"  [HL-paper] {sig['direction']} {sig['coin']} ${notional:.0f} "
            f"(whale ${sig['notional']:.0f}) via {sig['wallet'][:10]}..."
        )
    return opened


def mark_and_settle(state: dict, cfg: dict) -> int:
    """마크 업데이트 + 정산.

    청산 트리거:
      1) 소스 지갑 clearinghouse(해당 dex) 에 같은 방향 포지션 없음 (고래 flat)
      2) max_hold_hours 초과

    중요: xyz:ORCL 등 HIP-3 는 dex='xyz' clearinghouse 를 봐야 함.
    메인 CH 만 보면 항상 flat 오판 → pnl≈0 즉시 정산 버그.
    CH 조회 실패 시에는 정산하지 않고 유지.
    """
    p = _params(cfg)
    max_hold = float(p.get("max_hold_hours") or 48) * 3600
    # 오픈 직후 너무 이른 flat 판정 방지 (API 지연·부분체결)
    min_flat_age = float(p.get("min_whale_flat_age_sec") or 180)
    settled = 0
    remaining = []
    mids = _mids()
    # (wallet, dex) -> clearinghouse | None(실패)
    ch_cache: dict[tuple[str, str], dict | None] = {}

    for pos in state.get("open_positions") or []:
        coin = pos["coin"]
        dex = _coin_dex(coin)
        mid = float(mids.get(coin) or pos.get("mark") or pos["entry_price"])
        entry = float(pos["entry_price"])
        qty = float(pos["qty"])
        if pos["direction"] == "LONG":
            upnl = (mid - entry) * qty
        else:
            upnl = (entry - mid) * qty
        pos["mark"] = mid
        pos["unrealized_pnl"] = round(upnl, 4)
        pos["dex"] = dex or "main"

        closed = False
        reason = ""
        age = _now() - float(pos.get("opened_ts") or 0)

        # 1) max hold
        if age >= max_hold:
            closed = True
            reason = f"max_hold_{p.get('max_hold_hours')}h"

        # 2) whale flat — 올바른 dex CH 필수
        if not closed and age >= min_flat_age:
            w = str(pos["wallet"])
            cache_key = (w, dex)
            if cache_key not in ch_cache:
                ch_cache[cache_key] = _clearinghouse(w, dex=dex)
            ch = ch_cache[cache_key]
            if ch is None:
                # 네트워크/API 실패 → 유지 (false whale_flat 금지)
                remaining.append(pos)
                continue
            szi = _position_szi(ch, coin)
            if szi is None:
                szi = 0.0
            still = False
            if abs(szi) > 1e-12:
                if pos["direction"] == "LONG" and szi > 0:
                    still = True
                elif pos["direction"] == "SHORT" and szi < 0:
                    still = True
                # 반대 방향만 남음 = 우리 카피 방향은 flat
            if not still:
                closed = True
                reason = "whale_flat"

        if closed:
            pnl = float(pos.get("unrealized_pnl") or 0)
            row = {
                **pos,
                "event": "settled",
                "settled_at": _now_kst(),
                "settled_ts": _now(),
                "pnl_usd": round(pnl, 4),
                "exit_price": mid,
                "settle_reason": reason,
            }
            _append(row)
            state["bankroll"] = float(state.get("bankroll") or 1000) + pnl
            settled += 1
            print(f"  [HL-paper] settle {pos['direction']} {coin} pnl={pnl:+.2f} ({reason})")
        else:
            remaining.append(pos)
    state["open_positions"] = remaining
    return settled

def build_report(state: dict, cfg: dict) -> str:
    p = _params(cfg)
    rows = []
    if JOURNAL_FILE.exists():
        for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    settled = [r for r in rows if r.get("event") == "settled"]
    opened = [r for r in rows if r.get("event") == "opened"]
    wins = [r for r in settled if float(r.get("pnl_usd") or 0) > 0]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    wr = len(wins) / len(settled) if settled else 0.0
    wallets_n = len(_wallets(cfg))

    # wallet pnl breakdown
    by_w: dict[str, float] = {}
    for r in settled:
        w = str(r.get("wallet") or "?")[:12]
        by_w[w] = by_w.get(w, 0.0) + float(r.get("pnl_usd") or 0)

    lines = [
        f"🌊 <b>[Hyperliquid 고래 Paper 카피]</b> — "
        f"{datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        "",
        "🏷 계좌: <b>hl_whale_paper</b> (Poly/Bybit 과 분리)",
        f"bankroll ${float(state.get('bankroll') or 1000):.2f} | "
        f"정산 {len(settled)}건 | 승(PnL&gt;0) {wr:.0%} | 누적 PnL ${pnl:+.2f}",
        f"오픈 {len(state.get('open_positions') or [])}/{p['max_open_positions']} | "
        f"카피 ${p['copy_notional_usd']:.0f}/건 | min fill ${p['min_fill_notional_usd']:.0f} | "
        f"max hold {p['max_hold_hours']:.0f}h",
        f"추적 지갑 {wallets_n}개 | 누적 진입 기록 {len(opened)}건",
    ]
    if state.get("open_positions"):
        lines.append("")
        lines.append("오픈:")
        for pos in (state.get("open_positions") or [])[:6]:
            lines.append(
                f"• {pos.get('direction')} {escape(str(pos.get('coin')))} "
                f"uPnL ${float(pos.get('unrealized_pnl') or 0):+.2f} "
                f"via {escape(str(pos.get('wallet') or '')[:10])}..."
            )
    if settled:
        lines.append("")
        lines.append("최근 정산:")
        for r in settled[-5:]:
            lines.append(
                f"• {escape(str(r.get('direction')))} {escape(str(r.get('coin')))} "
                f"${float(r.get('pnl_usd') or 0):+.2f} "
                f"({escape(str(r.get('settle_reason') or ''))})"
            )
    if by_w:
        top = sorted(by_w.items(), key=lambda x: -x[1])[:3]
        weak = sorted(by_w.items(), key=lambda x: x[1])[:2]
        lines.append("")
        lines.append("💡 개선 코멘트")
        if len(settled) < 20:
            lines.append(
                f"• [관찰] 정산 {len(settled)}/20건 — 1차 마일스톤 전. "
                f"도달 시 별도 TG 리마인드가 1회 갑니다."
            )
        elif len(settled) < 30:
            lines.append(
                f"• [관찰] 정산 {len(settled)}/30건 — 재평가 마일스톤 대기 중."
            )
        if top and top[0][1] > 0:
            lines.append(
                f"• [유지] 고성과 지갑 {escape(top[0][0])}... PnL ${top[0][1]:+.2f}"
            )
        if weak and weak[0][1] < 0 and len(settled) >= 10:
            lines.append(
                f"• [건의] 저성과 {escape(weak[0][0])}... PnL ${weak[0][1]:+.2f} — "
                f"제외 검토 (config whales[])."
            )
        lines.append(
            "• [개선] 분기 1회 tools/hl_whale_screen.py --from-leaderboard 로 모수 갱신."
        )
    lines += [
        "",
        "※ 실주문 없음 · 콜드스타트 시 과거 체결 미카피 · 폴리 고래와 계좌 분리.",
    ]
    return "\n".join(lines)


def _read_journal_settled() -> list[dict]:
    rows: list[dict] = []
    if not JOURNAL_FILE.exists():
        return rows
    for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("event") == "settled":
            rows.append(r)
    return rows


def build_sample_milestone_report(state: dict, cfg: dict, milestone: int) -> str:
    """표본 충분 마일스톤 전용 리포트 (4h 정기 리포트와 별도 1회 발송)."""
    p = _params(cfg)
    settled = _read_journal_settled()
    n = len(settled)
    pnls = [float(r.get("pnl_usd") or 0) for r in settled]
    wins = sum(1 for x in pnls if x > 0)
    losses = sum(1 for x in pnls if x <= 0)
    wr = wins / n if n else 0.0
    total = sum(pnls)
    avg = total / n if n else 0.0
    avg_w = (sum(x for x in pnls if x > 0) / wins) if wins else 0.0
    avg_l = (sum(x for x in pnls if x <= 0) / losses) if losses else 0.0
    by_reason: dict[str, int] = {}
    by_coin: dict[str, float] = {}
    for r in settled:
        reason = str(r.get("settle_reason") or "?")[:32]
        by_reason[reason] = by_reason.get(reason, 0) + 1
        coin = str(r.get("coin") or "?")
        by_coin[coin] = by_coin.get(coin, 0.0) + float(r.get("pnl_usd") or 0)

    if milestone <= 20:
        verdict = (
            "1차 표본 도달 — min_fill·지갑 모수 큰 변경 전 성과 방향만 확인하세요."
        )
    elif milestone <= 30:
        verdict = (
            "재평가 구간 — $3k min_fill / 12지갑 유지 vs 조정 여부를 결정해도 됩니다."
        )
    else:
        verdict = (
            "라이브 검토 후보 구간 — paper EV·DD·지갑별 편차 보고 "
            "초소액 LIVE 여부를 논의해도 됩니다 (자동 전환 없음)."
        )

    lines = [
        f"📊 <b>[HL Paper 표본 마일스톤 n≥{milestone}]</b> — "
        f"{datetime.now(KST).strftime('%m/%d %H:%M KST')}",
        "",
        "⏰ 까먹지 말라고 보내는 <b>1회성</b> 리마인드입니다.",
        f"정산 <b>{n}</b>건 도달 (목표 게이트 {milestone})",
        "",
        f"bankroll ${float(state.get('bankroll') or 1000):.2f} | "
        f"WR {wr:.0%} ({wins}W/{losses}L) | 누적 PnL <b>${total:+.2f}</b>",
        f"건당 기댓값 ${avg:+.3f} | avgW ${avg_w:+.2f} | avgL ${avg_l:+.2f}",
        f"설정: min fill ${p['min_fill_notional_usd']:.0f} | "
        f"카피 ${p['copy_notional_usd']:.0f} | 지갑 {len(_wallets(cfg))}개",
        "",
        f"📌 {escape(verdict)}",
    ]
    if by_reason:
        lines.append("")
        lines.append("정산 사유:")
        for reason, cnt in sorted(by_reason.items(), key=lambda x: -x[1])[:6]:
            lines.append(f"• {escape(reason)} ×{cnt}")
    if by_coin:
        top = sorted(by_coin.items(), key=lambda x: -x[1])[:5]
        weak = sorted(by_coin.items(), key=lambda x: x[1])[:3]
        lines.append("")
        lines.append("코인별 PnL (상위):")
        for c, v in top:
            lines.append(f"• {escape(c)} ${v:+.2f}")
        if weak and weak[0][1] < 0:
            lines.append("코인별 PnL (하위):")
            for c, v in weak:
                lines.append(f"• {escape(c)} ${v:+.2f}")
    lines += [
        "",
        "다음 액션 예:",
        "• 텔레에 「HL paper 리뷰」라고 하면 같이 판단",
        "• 모수 갱신: tools/hl_whale_screen.py --from-leaderboard",
        "• LIVE 전환은 사람 승인 후에만 (이 알림이 자동 켜지 않음)",
        "",
        "※ paper only · 같은 마일스톤은 다시 안 보냄",
    ]
    return "\n".join(lines)


def _maybe_send_sample_milestones(state: dict, cfg: dict) -> list[int]:
    """정산 건수가 마일스톤에 도달하면 1회씩 TG 발송. 발송된 milestone 리스트 반환."""
    settled = _read_journal_settled()
    n = len(settled)
    sent_map = state.setdefault("sample_milestones_sent", {})
    fired: list[int] = []
    for m in SAMPLE_MILESTONES:
        key = f"n{m}"
        if n < m:
            continue
        if sent_map.get(key):
            continue
        msg = build_sample_milestone_report(state, cfg, m)
        if send_review(msg):
            sent_map[key] = {
                "sent_at": _now_kst(),
                "settled_n": n,
                "bankroll": state.get("bankroll"),
            }
            fired.append(m)
            print(f"  [HL-paper] sample milestone n>={m} report sent (settled={n})")
        else:
            print(f"  [HL-paper] sample milestone n>={m} TG 실패 — 다음 주기에 재시도")
            break  # 실패 시 순서 유지하며 재시도
    return fired


def run_once(report_now: bool = False) -> dict:
    cfg = _load_config()
    wallets = _wallets(cfg)
    state = _load_state(cfg)
    # ensure new wallets
    for w in wallets:
        state["wallets"].setdefault(w, {"last_fill_time": 0, "status": "active", "copied_keys": {}})

    if not wallets:
        result = {
            "ok": False,
            "error": "no wallets — fill hyperliquid_whale_config.json seed_wallets or whales",
            "wallets": 0,
        }
        print(f"[HLWhalePaper] {result}")
        return result

    settled = mark_and_settle(state, cfg)
    signals = scan_signals(state, cfg)
    opened = open_paper(signals, state, cfg)
    # 정산 누적 후 표본 마일스톤 (20/30/50) 1회 리마인드
    milestones = _maybe_send_sample_milestones(state, cfg)
    n_settled = len(_read_journal_settled())
    state["last_scan"] = {
        "time": _now_kst(),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "wallets": len(wallets),
        "settled_total": n_settled,
        "milestones_fired": milestones,
    }
    interval = float(_params(cfg).get("report_interval_seconds") or 4 * 3600)
    reported = False
    if report_now or (_now() - float(state.get("last_report_time") or 0) >= interval):
        if send_review(build_report(state, cfg)):
            state["last_report_time"] = _now()
            reported = True
    _save_state(state)
    return {
        "ok": True,
        "account": "hl_whale_paper",
        "wallets": len(wallets),
        "signals": len(signals),
        "opened": opened,
        "settled": settled,
        "settled_total": n_settled,
        "open_positions": len(state.get("open_positions") or []),
        "bankroll": state.get("bankroll"),
        "reported": reported,
        "milestones_fired": milestones,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report-now", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = run_once(report_now=args.report_now)
    if args.json:
        print(json.dumps(_json_safe(result), ensure_ascii=False))
    else:
        print(f"[HLWhalePaper] {result}")
    return 0 if result.get("ok", True) or result.get("wallets") == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
