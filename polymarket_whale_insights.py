"""Polymarket 고래 카피 리포트용 개선/건의 코멘트 생성.

기록(journal + state)을 보고 자동으로 코멘트를 붙인다.
- 지갑별 승률/표본
- suspend 후보·재활성
- 모수(추적 지갑) 추가 스크리닝 제안
- 사이징·한도·슬리피지 관찰
실제 config 수정은 하지 않음 (사람 승인용 코멘트만).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _wallet_stats(settled: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for r in settled:
        if r.get("is_shadow"):
            continue
        w = str(r.get("wallet") or "?")
        by[w]["n"] += 1
        by[w]["pnl"] += float(r.get("pnl_usd") or 0)
        if r.get("won"):
            by[w]["w"] += 1
    for w, st in by.items():
        st["wr"] = st["w"] / st["n"] if st["n"] else 0.0
    return dict(by)


def build_insight_comments(
    *,
    mode: str,
    state: dict[str, Any],
    settled: list[dict[str, Any]],
    config_whales: list[dict[str, Any]] | None = None,
    order_failed: list[dict[str, Any]] | None = None,
    blocked: list[dict[str, Any]] | None = None,
    bet_fraction: float | None = None,
    max_open: int | None = None,
) -> list[str]:
    """HTML 이스케이프 전 plain 코멘트 리스트."""
    tips: list[str] = []
    config_whales = config_whales or []
    order_failed = order_failed or []
    blocked = blocked or []
    stats = _wallet_stats(settled)
    n = len([r for r in settled if not r.get("is_shadow")])
    wins = sum(1 for r in settled if r.get("won") and not r.get("is_shadow"))
    wr = wins / n if n else 0.0
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled if not r.get("is_shadow"))

    # --- 표본 ---
    if n < 15:
        tips.append(
            f"[관찰] 정산 표본 {n}건 — 모수 변경은 최소 20~30건 후 권장. 지금은 추적·기록 유지."
        )
    elif n < 30:
        tips.append(
            f"[관찰] 정산 {n}건 누적 중. 지갑별 성과 분화 보이면 약한 지갑 suspend 검토."
        )

    # --- 지갑 성과 ---
    strong = []
    weak = []
    for w, st in stats.items():
        if st["n"] < 8:
            continue
        if st["wr"] >= 0.65 and st["pnl"] > 0:
            strong.append((w, st))
        # 승률이 높아도 고가 진입으로 평균손익이 나쁘면 약한 지갑이다.
        if st["pnl"] < 0 and (st["wr"] <= 0.45 or st["pnl"] / st["n"] <= -0.50):
            weak.append((w, st))
    strong.sort(key=lambda x: (-x[1]["wr"], -x[1]["pnl"]))
    weak.sort(key=lambda x: (x[1]["pnl"], x[1]["wr"]))

    if strong:
        w, st = strong[0]
        tips.append(
            f"[유지] 고성과 지갑 {w[:12]}... n={st['n']} WR={st['wr']:.0%} "
            f"PnL=${st['pnl']:+.1f} — 모수 유지·비중 관찰."
        )
    if weak:
        w, st = weak[0]
        tips.append(
            f"[건의] 저성과 지갑 {w[:12]}... n={st['n']} WR={st['wr']:.0%} "
            f"PnL=${st['pnl']:+.1f} — 자동 suspend 기준 미달이면 수동 제외 검토."
        )

    # --- state suspend ---
    wallets = state.get("wallets") or {}
    suspended = [w for w, s in wallets.items() if (s or {}).get("status") == "suspended"]
    active = [w for w, s in wallets.items() if (s or {}).get("status") == "active"]
    if suspended:
        tips.append(
            f"[상태] 중단 지갑 {len(suspended)}명 / 활성 {len(active)}명 — "
            f"그림자추적 회복 시 자동 재개. 장기 미회복 시 config 모수에서 제거 검토."
        )

    # --- 모수 추가 스크리닝 ---
    tips.append(
        "[개선] 승률 좋은 신규 플레이어 발견 시: "
        "PolyBacktest whale_backtest 로 z/n/avg_pnl 확인 → "
        "polymarket_whale_config.json whales[] 에 wallet+expected_win_rate 추가 → "
        "paper 1주 관찰 후 live 모수 반영."
    )
    if len(config_whales) < 12 and n >= 20:
        tips.append(
            f"[건의] 현재 모수 {len(config_whales)}명. "
            f"분기 1회 상위 지갑 재스크리닝(기존 9 + 후보 3~5) 추천."
        )

    # --- live 전용 ---
    if mode.upper() in ("LIVE", "DRY-RUN", "DRY_RUN"):
        if order_failed:
            recent_err = order_failed[-3:]
            tips.append(
                f"[수정] 주문 실패 {len(order_failed)}건 누적 — "
                f"잔고/USDC승인/CLOB/네트워크 점검. 최근: "
                f"{str(recent_err[-1].get('error') or recent_err[-1].get('reason') or '')[:60]}"
            )
        if blocked:
            reasons = defaultdict(int)
            for b in blocked:
                reasons[str(b.get("reason") or "unknown")[:40]] += 1
            top = max(reasons.items(), key=lambda x: x[1])
            tips.append(
                f"[관찰] 진입 차단 {len(blocked)}건 — 최다 사유「{top[0]}」×{top[1]}. "
                f"한도/동시포지션 설정을 로그와 맞춰 볼 것."
            )
        if bet_fraction is not None:
            tips.append(
                f"[설정] live 베팅 비율 {bet_fraction*100:.0f}% 기준. "
                f"실제 단건은 live USD cap을 적용하며 리포트 운영 상태에 표시."
            )
        if max_open is not None:
            open_n = len([
                p for p in (state.get("open_positions") or [])
                if not p.get("is_shadow") and not p.get("dry_run")
            ])
            if max_open > 0 and open_n >= max_open * 0.8:
                tips.append(
                    f"[관찰] 오픈 {open_n}/{max_open} — 한도 근접. "
                    f"신호 많을 때 기회 손실 가능, 한도 상향은 리스크와 트레이드오프."
                )

    # --- 전체 성과 ---
    if n >= 20:
        if wr < 0.5 and pnl < 0:
            tips.append(
                f"[건의] 전체 WR {wr:.0%} PnL ${pnl:+.1f} — "
                f"MIN_NET_USDC·슬리피지·지갑 필터 재검토. 맹목적 모수 확대 금지."
            )
        elif wr >= 0.55 and pnl > 0:
            tips.append(
                f"[긍정] 전체 WR {wr:.0%} PnL ${pnl:+.1f} — "
                f"현 모수 유지 + 후보 지갑은 paper 병행 검증 후 추가."
            )

    # 중복 줄이기
    seen = set()
    uniq = []
    for t in tips:
        head = t[:24]
        if head in seen:
            continue
        seen.add(head)
        uniq.append(t)
    return uniq[:8]


def format_insights_html(comments: list[str]) -> list[str]:
    if not comments:
        return ["💡 <b>코멘트</b>", "• 특이 제안 없음 — 현 추적 유지."]
    lines = ["", "💡 <b>개선·건의 코멘트</b> (자동, 적용은 수동)"]
    for c in comments:
        # escape handled by caller if needed — use plain, caller escapes
        lines.append(f"• {c}")
    return lines
