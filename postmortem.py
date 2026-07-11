"""
거래 포스트모템 (post-mortem) — 청산 직후 1건 분석 기록.

무엇인가:
  의료/항공에서 쓰던 말 그대로 "끝난 뒤 부검·회고".
  한 거래가 끝나면 승/패를 감이 아니라 구조화된 원인 후보로 남겨,
  다음 진입(사이즈/차단)과 주간 로직 개선의 재료로 쓴다.

무엇인가가 아닌 것:
  - 확정 진리가 아님 (후보 목록)
  - 코드를 자동으로 갈아엎지 않음 (기록 + 학습 입력)

파일: trade_postmortem.jsonl (+ venue namespace)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from venue_runtime import namespaced_data_path, runtime_venue, venue_label

KST = timezone(timedelta(hours=9))
POSTMORTEM_FILE = namespaced_data_path("trade_postmortem.jsonl")


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _ctx(record: dict) -> dict:
    return record.get("entry_context") or {}


def _attr(record: dict) -> dict:
    ctx = _ctx(record)
    a = record.get("logic_attribution") or ctx.get("logic_attribution") or {}
    return a if isinstance(a, dict) else {}


def _regime(record: dict) -> dict:
    ctx = _ctx(record)
    r = record.get("regime") or ctx.get("regime") or {}
    if isinstance(r, dict):
        return r
    return {}


def _hold_minutes(record: dict) -> float:
    ts = _f(record.get("timestamp"))
    if ts <= 0:
        return 0.0
    return max(0.0, (time.time() - ts) / 60.0)


def _r_multiple(record: dict) -> float | None:
    """실현 R: 손익 / 계획 SL 위험 (대략)."""
    pnl = _f(record.get("pnl_usd"))
    est = _f(record.get("est_sl_loss"))
    if est <= 0:
        entry = _f(record.get("entry_price"))
        sl = _f(record.get("sl"))
        qty = _f(record.get("qty"))
        if entry > 0 and sl > 0 and qty > 0:
            est = abs(entry - sl) * qty
    if est <= 0:
        return None
    return round(pnl / est, 3)


def _loss_cause_candidates(record: dict) -> list[dict]:
    """패배 원인 후보 (확정 아님). code는 집계용."""
    out: list[dict] = []
    direction = str(record.get("direction") or "")
    exit_reason = str(record.get("exit_reason") or "")
    strategy = str(record.get("strategy") or "")
    tf = str(record.get("tf") or "")
    ema = record.get("ema_trend")
    if ema is None:
        ema = _ctx(record).get("ema_trend")
    vol = _f(record.get("vol_ratio") or _ctx(record).get("vol_ratio"))
    bars = int(record.get("bars_ago") or _ctx(record).get("bars_ago") or 0)
    tp1_rr = _f(record.get("tp1_rr") or (_ctx(record).get("rr") or {}).get("tp1"))
    sl_pct = _f(record.get("sl_pct"))
    regime = _regime(record)
    attr = _attr(record)
    r_mult = _r_multiple(record)
    hold = _hold_minutes(record)

    if "SL" in exit_reason or exit_reason == "손실 청산":
        out.append({
            "code": "stop_hit",
            "text": f"{exit_reason} — 가격이 SL 쪽을 먼저 터치 (진입 가정 무효 또는 노이즈)",
            "weight": 1.0,
        })
    if r_mult is not None and r_mult <= -0.9:
        out.append({
            "code": "full_r_loss",
            "text": f"약 {abs(r_mult):.2f}R 전량 손실에 가까움 — 부분익절/보호 전 반대 진행",
            "weight": 0.9,
        })
    if hold < 15 and "SL" in exit_reason:
        out.append({
            "code": "fast_stop",
            "text": f"보유 {hold:.0f}분 내 손절 — 타이밍/노이즈 또는 늦은 진입 가능",
            "weight": 0.7,
        })

    if (direction == "LONG" and ema == -1) or (direction == "SHORT" and ema == 1):
        out.append({
            "code": "ema_against",
            "text": "EMA 큰 흐름과 반대 방향 — 되돌림 압력 가능",
            "weight": 0.8,
        })
    elif ema == 0:
        out.append({
            "code": "ema_neutral",
            "text": "EMA 중립 — 방향성 미확정 구간 가능",
            "weight": 0.5,
        })

    rg = regime.get("regime")
    if rg == "range" and "EMA" in strategy:
        out.append({
            "code": "regime_range_ema",
            "text": "횡보 레짐에서 EMA 추세전략 — 휩쏘/가짜 돌파 전형 패턴",
            "weight": 0.85,
        })
    if rg == "high_vol":
        out.append({
            "code": "regime_high_vol",
            "text": "고변동 레짐 — SL 타이트 시 노이즈 손절 증가",
            "weight": 0.6,
        })
    if rg == "trend":
        tdir = int(regime.get("trend_dir") or 0)
        want = 1 if direction == "LONG" else -1
        if tdir and tdir != want:
            out.append({
                "code": "regime_trend_against",
                "text": "추세 레짐인데 방향이 레짐 기울기와 반대",
                "weight": 0.85,
            })

    if direction == "SHORT":
        out.append({
            "code": "short_structural",
            "text": "SHORT — 계좌 실측상 기대값 불리 구간 (스퀴즈/상승 편향)",
            "weight": 0.55,
        })

    if vol and vol < 1.3:
        out.append({
            "code": "low_volume",
            "text": f"거래량 {vol:.2f}x 약함 — 후속 추진력 부족 가능",
            "weight": 0.55,
        })
    if bars >= 5:
        out.append({
            "code": "stale_signal",
            "text": f"신호 신선도 {bars}봉 전 — 늦은 진입 가능",
            "weight": 0.65,
        })
    if tp1_rr and tp1_rr < 1.0:
        out.append({
            "code": "poor_tp1_rr",
            "text": f"TP1 R:R 1:{tp1_rr:.2f} 낮음 — 작은 흔들림에 불리",
            "weight": 0.5,
        })
    if sl_pct and sl_pct > 8:
        out.append({
            "code": "wide_sl",
            "text": f"SL {sl_pct:.1f}% 넓음 — 단건 손실 비대",
            "weight": 0.7,
        })
    if tf == "15m" and "EMA" not in strategy:
        out.append({
            "code": "noisy_tf",
            "text": "15m 비-EMA 계열 — 노이즈 TF에서 저품질 가능",
            "weight": 0.45,
        })

    feats = attr.get("new_features") or []
    if any("macd_misalign" in str(f) or "macd_soft" in str(f) for f in feats):
        out.append({
            "code": "macd_soft_entry",
            "text": "EMA MACD 미정렬 soft 진입 — 모멘텀 비협조 상태에서 진입",
            "weight": 0.5,
        })

    if not out:
        out.append({
            "code": "unknown",
            "text": "특이 태그 없음 — 시장 역행 또는 표본 부족",
            "weight": 0.3,
        })
    out.sort(key=lambda x: -x["weight"])
    return out[:8]


def _win_cause_candidates(record: dict) -> list[dict]:
    out: list[dict] = []
    direction = str(record.get("direction") or "")
    exit_reason = str(record.get("exit_reason") or "")
    strategy = str(record.get("strategy") or "")
    ema = record.get("ema_trend")
    if ema is None:
        ema = _ctx(record).get("ema_trend")
    vol = _f(record.get("vol_ratio") or _ctx(record).get("vol_ratio"))
    regime = _regime(record)
    r_mult = _r_multiple(record)
    mtf = _f(_ctx(record).get("mtf_boost") or record.get("mtf_boost") or 1.0)
    ema_aligned = bool(_ctx(record).get("ema_aligned") or record.get("ema_aligned"))

    out.append({
        "code": "thesis_worked",
        "text": f"진입 방향 유효 — {exit_reason or '수익 청산'}",
        "weight": 1.0,
    })
    if r_mult is not None and r_mult >= 1.0:
        out.append({
            "code": "good_r_multiple",
            "text": f"약 +{r_mult:.2f}R — 손익비 실현 양호",
            "weight": 0.9,
        })
    elif r_mult is not None and 0 < r_mult < 0.5:
        out.append({
            "code": "shallow_win",
            "text": f"약 +{r_mult:.2f}R 얕은 승리 — 부분익절/조기 보호 가능",
            "weight": 0.7,
        })

    if "부분익절" in exit_reason or "TP" in exit_reason:
        out.append({
            "code": "scale_out_worked",
            "text": "분할/TP 경로로 수익 확정 — 스케일아웃 설계 유효",
            "weight": 0.75,
        })
    if "보호" in exit_reason or "본전" in exit_reason:
        out.append({
            "code": "protect_lock",
            "text": "수익보호/BE 경로 — 큰 되돌림 전 잠금 성공",
            "weight": 0.7,
        })

    rg = regime.get("regime")
    if rg == "trend":
        tdir = int(regime.get("trend_dir") or 0)
        want = 1 if direction == "LONG" else -1
        if tdir == want or tdir == 0:
            out.append({
                "code": "regime_trend_align",
                "text": "추세 레짐 + 순응 방향 — 추세 전략 적합 구간",
                "weight": 0.85,
            })
    if "EMA" in strategy and (ema_aligned or (direction == "LONG" and ema == 1) or (direction == "SHORT" and ema == -1)):
        out.append({
            "code": "ema_aligned",
            "text": "EMA 정렬 눌림목/지속 — 주력 엣지 패턴",
            "weight": 0.8,
        })
    if mtf > 1.0:
        out.append({
            "code": "mtf_aligned",
            "text": f"MTF 부스트 {mtf:.2f}x — 상위봉 정렬 우위",
            "weight": 0.75,
        })
    if vol and vol >= 1.5:
        out.append({
            "code": "volume_support",
            "text": f"거래량 {vol:.2f}x — 후속 추진 동반",
            "weight": 0.55,
        })
    if direction == "LONG":
        out.append({
            "code": "long_bias_env",
            "text": "LONG — 크립토 구조적 상승 편향과 정합 가능",
            "weight": 0.35,
        })

    out.sort(key=lambda x: -x["weight"])
    return out[:8]


def _lessons(record: dict, causes: list[dict], status: str) -> list[str]:
    lessons = []
    codes = {c["code"] for c in causes}
    if status == "loss":
        if "regime_range_ema" in codes:
            lessons.append("횡보에서 EMA 사이즈 추가 축소 또는 스킵 검토")
        if "short_structural" in codes:
            lessons.append("SHORT 게이트/사이즈 유지·강화 관찰")
        if "fast_stop" in codes:
            lessons.append("진입 타이밍·하위TF 정렬 재확인")
        if "stale_signal" in codes:
            lessons.append("신선도 한도 유지 (늦은 진입 금지)")
        if "shallow_win" not in codes and "full_r_loss" in codes:
            lessons.append("TP1 전 추진 실패 — 조기 무효 기준 검토")
        if not lessons:
            lessons.append("동일 심볼·전략군 연패 시 실체결 학습 차단 활용")
    elif status == "win":
        if "regime_trend_align" in codes and "ema_aligned" in codes:
            lessons.append("추세+EMA 정렬 패턴 — 주력 유지")
        if "shallow_win" in codes:
            lessons.append("얕은 승리 다수면 러너/TP 비중 재검토")
        if "scale_out_worked" in codes:
            lessons.append("분할 익절 경로 유효 — 유지")
        if not lessons:
            lessons.append("반복 조건을 logic_attribution과 함께 누적 관찰")
    else:
        lessons.append("본전/보호 청산 — 큰 손실 회피 성공으로 기록")
    return lessons[:4]


def build_trade_postmortem(record: dict) -> dict:
    """청산된 trade_history 레코드 → 포스트모템 dict."""
    status = str(record.get("status") or "")
    if status == "win":
        causes = _win_cause_candidates(record)
        headline = "승리 원인 후보"
    elif status == "loss":
        causes = _loss_cause_candidates(record)
        headline = "패배 원인 후보"
    else:
        causes = [{
            "code": "breakeven",
            "text": "본전 — 수익보호 또는 미세 손익",
            "weight": 1.0,
        }]
        headline = "본전 처리"

    attr = _attr(record)
    regime = _regime(record)
    r_mult = _r_multiple(record)
    primary = causes[0] if causes else {"code": "unknown", "text": "-", "weight": 0}

    pm = {
        "schema": "got_postmortem_v1",
        "venue": record.get("venue") or runtime_venue(),
        "venue_label": venue_label(record.get("venue") or runtime_venue()),
        "time": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "timestamp": time.time(),
        "trade_num": record.get("num"),
        "symbol": record.get("symbol"),
        "direction": record.get("direction"),
        "tf": record.get("tf"),
        "strategy": record.get("strategy"),
        "strategy_family": record.get("strategy_family") or _ctx(record).get("strategy_family"),
        "status": status,
        "pnl_usd": _f(record.get("pnl_usd")),
        "exit_reason": record.get("exit_reason"),
        "entry_price": _f(record.get("entry_price")),
        "exit_price": _f(record.get("exit_price")),
        "sl": _f(record.get("sl")),
        "r_multiple": r_mult,
        "hold_minutes": round(_hold_minutes(record), 1),
        "headline": headline,
        "primary_cause": primary,
        "causes": causes,
        "lessons": _lessons(record, causes, status),
        "regime": {
            "regime": regime.get("regime"),
            "note": regime.get("note"),
            "adx": regime.get("adx"),
            "trend_dir": regime.get("trend_dir"),
        },
        "logic_stack_version": (
            record.get("logic_stack_version")
            or attr.get("stack_version")
            or _ctx(record).get("logic_stack_version")
        ),
        "logic_attribution": {
            "new_stack_applied": attr.get("new_stack_applied"),
            "new_features": attr.get("new_features") or [],
            "summary_ko": attr.get("summary_ko"),
        },
        "summary_ko": (
            f"{'✅승' if status == 'win' else '❌패' if status == 'loss' else '〰본전'} "
            f"{record.get('symbol')} {record.get('direction')} "
            f"pnl={_f(record.get('pnl_usd')):+.2f} "
            f"| {primary.get('text', '')}"
        ),
    }
    return pm


def save_postmortem(pm: dict) -> Path:
    POSTMORTEM_FILE.parent.mkdir(parents=True, exist_ok=True)
    with POSTMORTEM_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pm, ensure_ascii=False) + "\n")
    return POSTMORTEM_FILE


def build_and_save_postmortem(record: dict) -> dict:
    pm = build_trade_postmortem(record)
    path = save_postmortem(pm)
    print(f"  [포스트모템] {pm['summary_ko']}")
    print(f"  [포스트모템] 저장 → {path.name}")
    for i, c in enumerate(pm.get("causes") or [][:3], 1):
        print(f"  [포스트모템] 원인{i}: {c.get('text')}")
    for lesson in pm.get("lessons") or []:
        print(f"  [포스트모템] 교훈: {lesson}")
    return pm


def format_postmortem_telegram(pm: dict) -> str:
    """텔레그램용 짧은 블록."""
    from html import escape
    status = pm.get("status")
    icon = "✅" if status == "win" else "❌" if status == "loss" else "〰"
    lines = [
        f"{icon} <b>포스트모템</b> #{pm.get('trade_num')} {escape(str(pm.get('symbol') or ''))}",
        escape(str(pm.get("summary_ko") or "")),
    ]
    if pm.get("r_multiple") is not None:
        lines.append(f"R배수: <b>{pm['r_multiple']:+.2f}R</b>  |  보유 {pm.get('hold_minutes')}분")
    rg = (pm.get("regime") or {}).get("regime")
    if rg:
        lines.append(f"레짐: {escape(str(rg))} — {escape(str((pm.get('regime') or {}).get('note') or ''))}")
    lines.append("")
    lines.append(f"📌 <b>{escape(str(pm.get('headline') or '원인'))}</b>")
    for c in (pm.get("causes") or [])[:4]:
        lines.append(f"  • {escape(str(c.get('text') or ''))}")
    if pm.get("lessons"):
        lines.append("")
        lines.append("💡 <b>교훈</b>")
        for L in pm["lessons"][:3]:
            lines.append(f"  • {escape(str(L))}")
    la = pm.get("logic_attribution") or {}
    if la.get("summary_ko"):
        lines.append("")
        lines.append(f"🏷 {escape(str(la['summary_ko']))}")
    return "\n".join(lines)
