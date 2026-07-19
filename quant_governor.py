"""Evidence-based live strategy governor.

This module deliberately does not generate trading signals.  It answers the
more important production question: does this venue have enough net live
evidence to risk capital on the currently approved strategy family?

The governor uses only trades that were actually closed before the candidate
entry.  Candidate/backtest outcomes are kept out of the live cohort so a noisy
research signal cannot silently promote itself.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from venue_runtime import normalize_venue


ROOT = Path(__file__).resolve().parent
DEFAULT_MIN_CLOSED = 20
DEFAULT_MIN_PROFIT_FACTOR = 1.15
DEFAULT_PROBATION_RISK_MULT = 0.50
DEFAULT_WEAK_VERSION_RISK_MULT = 0.35


@dataclass(frozen=True)
class CohortMetrics:
    closed: int = 0
    wins: int = 0
    losses: int = 0
    pnl_usd: float = 0.0
    expectancy_usd: float = 0.0
    win_rate: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    payoff_ratio: float = 0.0
    profit_factor: float | None = None
    max_drawdown_usd: float = 0.0
    mean_r: float | None = None
    conservative_mean_r: float | None = None


@dataclass(frozen=True)
class QuantDecision:
    allow: bool
    mode: str
    risk_mult: float
    reason: str
    venue: str
    strategy: str
    direction: str
    timeframe: str
    cohort: CohortMetrics
    version_closed: int = 0
    version_pnl_usd: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def state_path(venue: str, root: Path = ROOT) -> Path:
    name = "trade_state_binance.json" if normalize_venue(venue) == "binance" else "trade_state.json"
    return root / name


def load_closed_history(venue: str, root: Path = ROOT) -> list[dict]:
    path = state_path(venue, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [
        row for row in (payload.get("trade_history") or [])
        if row.get("status") in {"win", "loss"}
    ]


def _trade_r(row: dict) -> float | None:
    try:
        pnl = float(row.get("pnl_usd") or 0)
        risk = abs(float(row.get("est_sl_loss") or 0))
    except Exception:
        return None
    if risk <= 0:
        return None
    return pnl / risk


def cohort_metrics(rows: Iterable[dict]) -> CohortMetrics:
    trades = list(rows)
    pnls = [float(row.get("pnl_usd") or 0) for row in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    rs = [value for value in (_trade_r(row) for row in trades) if value is not None]
    mean_r = statistics.fmean(rs) if rs else None
    conservative_r = None
    if rs:
        # One-sided ~80% lower confidence estimate.  This is intentionally a
        # diagnostic rather than a promotion condition because crypto returns
        # are not Gaussian and the live sample is still small.
        stderr = statistics.stdev(rs) / math.sqrt(len(rs)) if len(rs) > 1 else 0.0
        conservative_r = mean_r - 1.2816 * stderr

    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    return CohortMetrics(
        closed=len(trades),
        wins=len(wins),
        losses=len(losses),
        pnl_usd=round(sum(pnls), 8),
        expectancy_usd=round(sum(pnls) / len(pnls), 8) if pnls else 0.0,
        win_rate=len(wins) / len(pnls) if pnls else 0.0,
        avg_win_usd=round(avg_win, 8),
        avg_loss_usd=round(avg_loss, 8),
        payoff_ratio=round(avg_win / avg_loss, 8) if avg_loss > 0 else 0.0,
        profit_factor=round(gross_win / gross_loss, 8) if gross_loss > 0 else None,
        max_drawdown_usd=round(max_dd, 8),
        mean_r=round(mean_r, 8) if mean_r is not None else None,
        conservative_mean_r=(
            round(conservative_r, 8) if conservative_r is not None else None
        ),
    )


def approved_cohort(
    history: Iterable[dict], approved_strategies: Sequence[str]
) -> list[dict]:
    approved = set(approved_strategies)
    return [
        row for row in history
        if row.get("strategy") in approved and row.get("direction") == "LONG"
    ]


def evaluate_live_candidate(
    *,
    venue: str,
    strategy: str,
    direction: str,
    timeframe: str,
    approved_strategies: Sequence[str],
    logic_stack_version: str = "",
    root: Path = ROOT,
    min_closed: int = DEFAULT_MIN_CLOSED,
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR,
    binance_canary_enabled: bool = False,
    binance_canary_risk_mult: float = 0.10,
    binance_canary_early_review: int = 8,
) -> QuantDecision:
    """Return a fail-closed live decision for a candidate.

    The strategy family is evaluated separately per venue.  A strategy that
    works on Bybit is not assumed to work on Binance because fills, product
    universe, fee tier, leverage constraints and historical sizing differ.
    """
    venue = normalize_venue(venue)
    direction = str(direction or "").upper()
    approved = set(approved_strategies)
    history = load_closed_history(venue, root)
    cohort_rows = approved_cohort(history, approved_strategies)
    metrics = cohort_metrics(cohort_rows)

    version_rows = [
        row for row in cohort_rows
        if logic_stack_version
        and (
            row.get("logic_stack_version")
            or (row.get("entry_context") or {}).get("logic_stack_version")
        ) == logic_stack_version
    ]
    version_metrics = cohort_metrics(version_rows)

    base = dict(
        venue=venue,
        strategy=strategy,
        direction=direction,
        timeframe=timeframe,
        cohort=metrics,
        version_closed=version_metrics.closed,
        version_pnl_usd=version_metrics.pnl_usd,
    )
    if strategy not in approved or direction != "LONG":
        return QuantDecision(
            allow=False,
            mode="shadow",
            risk_mult=0.0,
            reason="승인된 EMA-LONG 챔피언 코호트가 아님",
            **base,
        )

    # 사용자가 승인한 Binance 초소액 실전 OOS 수집 경로. 과거 Binance 손실을
    # 무시해 정상 사이즈를 주는 것이 아니라 현재 로직 버전만 별도 canary로 본다.
    # 후보 MFE/MAE나 백테스트는 이 판정에 들어오지 않고 실제 청산만 집계된다.
    if venue == "binance" and binance_canary_enabled and logic_stack_version:
        version_pf = (
            version_metrics.profit_factor
            if version_metrics.profit_factor is not None
            else (float("inf") if version_metrics.pnl_usd > 0 else 0.0)
        )
        early_n = max(1, min(int(binance_canary_early_review), min_closed))
        if version_metrics.closed < early_n:
            return QuantDecision(
                allow=True,
                mode="canary",
                risk_mult=max(0.0, min(float(binance_canary_risk_mult), 0.10)),
                reason=(
                    f"binance v5 초소액 실전 OOS {version_metrics.closed}/{early_n}건 "
                    f"(정식 {min_closed}건), risk×"
                    f"{max(0.0, min(float(binance_canary_risk_mult), 0.10)):.2f}"
                ),
                **base,
            )
        if version_metrics.closed < min_closed:
            if (
                version_metrics.pnl_usd <= 0
                or version_metrics.expectancy_usd <= 0
                or version_pf < 1.0
            ):
                return QuantDecision(
                    allow=False,
                    mode="shadow",
                    risk_mult=0.0,
                    reason=(
                        f"binance canary 조기중단: n={version_metrics.closed}, "
                        f"PF={version_pf:.2f}, E=${version_metrics.expectancy_usd:+.3f}"
                    ),
                    **base,
                )
            return QuantDecision(
                allow=True,
                mode="canary",
                risk_mult=max(0.0, min(float(binance_canary_risk_mult), 0.10)),
                reason=(
                    f"binance canary 조기평가 통과: n={version_metrics.closed}, "
                    f"PF={version_pf:.2f}, E=${version_metrics.expectancy_usd:+.3f}, "
                    f"risk×{max(0.0, min(float(binance_canary_risk_mult), 0.10)):.2f}"
                ),
                **base,
            )
        if (
            version_metrics.pnl_usd <= 0
            or version_metrics.expectancy_usd <= 0
            or version_pf < min_profit_factor
        ):
            return QuantDecision(
                allow=False,
                mode="shadow",
                risk_mult=0.0,
                reason=(
                    f"binance 현 버전 정식평가 실패: n={version_metrics.closed}, "
                    f"PF={version_pf:.2f}, E=${version_metrics.expectancy_usd:+.3f}"
                ),
                **base,
            )
        promoted_risk = 0.50 if (
            version_metrics.closed >= 40
            and version_pf >= 1.25
            and (version_metrics.conservative_mean_r or 0.0) > 0
        ) else 0.25
        return QuantDecision(
            allow=True,
            mode="champion" if promoted_risk >= 0.50 else "probation",
            risk_mult=promoted_risk,
            reason=(
                f"binance 현 버전 실전 승격: n={version_metrics.closed}, "
                f"PF={version_pf:.2f}, E=${version_metrics.expectancy_usd:+.3f}, "
                f"risk×{promoted_risk:.2f}"
            ),
            **base,
        )

    pf = metrics.profit_factor or 0.0
    if metrics.closed < min_closed:
        return QuantDecision(
            allow=False,
            mode="shadow",
            risk_mult=0.0,
            reason=(
                f"{venue} 챔피언 표본 {metrics.closed}/{min_closed}, "
                f"PF {pf:.2f}, pnl ${metrics.pnl_usd:+.2f} — 실거래 승격 전"
            ),
            **base,
        )
    if metrics.pnl_usd <= 0 or metrics.expectancy_usd <= 0 or pf < min_profit_factor:
        return QuantDecision(
            allow=False,
            mode="shadow",
            risk_mult=0.0,
            reason=(
                f"{venue} 비용후 기대값 미달: n={metrics.closed}, "
                f"PF={pf:.2f}(<{min_profit_factor:.2f}), "
                f"E=${metrics.expectancy_usd:+.3f}"
            ),
            **base,
        )

    # 과거 버전의 좋은 성과가 현재 버전의 충분히 쌓인 음의 표본을 가리면 안 된다.
    # 현 버전이 최소 표본에 도달한 뒤에는 aggregate cohort와 별도로 같은 승격
    # 기준을 다시 통과해야 한다.
    if logic_stack_version and version_metrics.closed >= min_closed:
        version_pf = version_metrics.profit_factor or 0.0
        if (
            version_metrics.pnl_usd <= 0
            or version_metrics.expectancy_usd <= 0
            or version_pf < min_profit_factor
        ):
            return QuantDecision(
                allow=False,
                mode="shadow",
                risk_mult=0.0,
                reason=(
                    f"{venue} 현 버전 비용후 기대값 미달: "
                    f"n={version_metrics.closed}, PF={version_pf:.2f}, "
                    f"E=${version_metrics.expectancy_usd:+.3f}"
                ),
                **base,
            )

    risk_mult = 1.0
    mode = "champion"
    reason = (
        f"{venue} 챔피언 유지: n={metrics.closed}, PF={pf:.2f}, "
        f"E=${metrics.expectancy_usd:+.3f}"
    )
    if version_metrics.closed < min_closed:
        mode = "probation"
        risk_mult = DEFAULT_PROBATION_RISK_MULT
        if version_metrics.closed and version_metrics.pnl_usd < 0:
            risk_mult = DEFAULT_WEAK_VERSION_RISK_MULT
        reason += (
            f" | 현 버전 {version_metrics.closed}/{min_closed}건 "
            f"pnl=${version_metrics.pnl_usd:+.2f}, risk×{risk_mult:.2f}"
        )
    elif logic_stack_version:
        version_pf = version_metrics.profit_factor or 0.0
        reason += (
            f" | 현 버전 통과 n={version_metrics.closed}, "
            f"PF={version_pf:.2f}, E=${version_metrics.expectancy_usd:+.3f}"
        )

    return QuantDecision(
        allow=True,
        mode=mode,
        risk_mult=risk_mult,
        reason=reason,
        **base,
    )
