"""PolyInsight 모멘텀 paper — 리포트용 개선/졸업 코멘트.

고래 카피와 완전 분리된 전략. config 자동 변경 없음.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

# 실매매 졸업 가드 (수동 승인 전 자동 통과 기준)
GRAD_MIN_SETTLED = 30
GRAD_MIN_WR = 0.55
GRAD_MIN_PNL = 0.0
GRAD_MIN_DAYS = 7


def build_insight_comments(
    *,
    state: dict[str, Any],
    settled: list[dict[str, Any]],
    by_kind: dict[str, dict[str, Any]] | None = None,
    open_n: int = 0,
    max_open: int = 8,
    bankroll: float = 1000.0,
    initial: float = 1000.0,
) -> list[str]:
    tips: list[str] = []
    n = len(settled)
    wins = sum(1 for r in settled if r.get("won"))
    wr = wins / n if n else 0.0
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    first_ts = min((float(r.get("opened_ts") or 0) for r in settled), default=0.0)
    last_ts = max((float(r.get("settled_ts") or r.get("opened_ts") or 0) for r in settled), default=0.0)
    span_days = (last_ts - first_ts) / 86400.0 if first_ts and last_ts else 0.0

    # --- 표본 ---
    if n < 15:
        tips.append(
            f"[관찰] 정산 {n}건 — 모멘텀 edge 판단은 최소 {GRAD_MIN_SETTLED}건·"
            f"{GRAD_MIN_DAYS}일 후. 지금은 paper 축적만."
        )
    elif n < GRAD_MIN_SETTLED:
        tips.append(
            f"[관찰] 정산 {n}/{GRAD_MIN_SETTLED}건 누적 중. "
            f"kind별(momentum_break vs prob_shock) 승률 분화 확인."
        )

    # --- kind 성과 ---
    by_kind = by_kind or {}
    for kind, st in sorted(by_kind.items(), key=lambda x: -x[1].get("n", 0)):
        if st.get("n", 0) < 8:
            continue
        kwr = st["w"] / st["n"] if st["n"] else 0
        if kwr >= 0.6 and st["pnl"] > 0:
            tips.append(
                f"[유지] {kind} n={st['n']} WR={kwr:.0%} PnL=${st['pnl']:+.1f} "
                f"— paper 비중 유지 후보."
            )
        elif kwr <= 0.45 and st["pnl"] < 0:
            tips.append(
                f"[건의] {kind} n={st['n']} WR={kwr:.0%} PnL=${st['pnl']:+.1f} "
                f"— 임계치 강화 또는 해당 kind 중단 검토."
            )

    # --- 오픈/사이징 ---
    if open_n >= max_open * 0.8:
        tips.append(
            f"[관찰] 오픈 {open_n}/{max_open} — 한도 근접. "
            f"신호 폭주 시 기회 손실 가능, 한도 상향은 paper 한도 안에서만."
        )
    if bankroll < initial * 0.85:
        tips.append(
            f"[주의] bankroll ${bankroll:.0f} (시작 ${initial:.0f} 대비 "
            f"{(bankroll/initial-1)*100:.0f}%) — 파라미터 동결·관찰 강화."
        )

    # --- 졸업 게이트 ---
    ready = (
        n >= GRAD_MIN_SETTLED
        and wr >= GRAD_MIN_WR
        and pnl >= GRAD_MIN_PNL
        and span_days >= GRAD_MIN_DAYS
    )
    if ready:
        tips.append(
            f"[졸업후보] 정산 {n}건 WR={wr:.0%} PnL=${pnl:+.1f} "
            f"기간≈{span_days:.0f}일 — 기준 충족. "
            f"실매매는 별도 초소액 LIVE + 고래 카피 계좌 분리 후 수동 승인."
        )
    elif n >= GRAD_MIN_SETTLED:
        reasons = []
        if wr < GRAD_MIN_WR:
            reasons.append(f"WR {wr:.0%}<{GRAD_MIN_WR:.0%}")
        if pnl < GRAD_MIN_PNL:
            reasons.append(f"PnL ${pnl:+.1f}<0")
        if span_days < GRAD_MIN_DAYS:
            reasons.append(f"기간 {span_days:.0f}d<{GRAD_MIN_DAYS}d")
        tips.append(
            f"[미졸업] 표본은 충분하나 조건 미달: {', '.join(reasons) or '기타'}. "
            f"실매매 전환 보류."
        )
    else:
        tips.append(
            f"[원칙] 고래 카피 LIVE와 계좌·한도 분리 유지. "
            f"이 paper가 검증되기 전 실주문 금지."
        )

    # --- 운영 ---
    tips.append(
        "[개선] avoid_chase(극단가)는 진입 안 함 — 고래 카피 필터로만 사용. "
        "momentum_break / prob_shock 만 paper 진입."
    )
    if not tips:
        tips.append("특이 이슈 없음 — 수집·정산 유지.")
    return tips[:10]


def kind_stats(settled: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for r in settled:
        k = str(r.get("kind") or "?")
        by[k]["n"] += 1
        by[k]["pnl"] += float(r.get("pnl_usd") or 0)
        if r.get("won"):
            by[k]["w"] += 1
    return dict(by)
