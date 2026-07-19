"""Attach public Binance futures-positioning data to recent pump events.

This research tool reads the event set produced by ``binance_pump_study.py``
and compares the last fully closed one-hour derivatives bucket before the
first +5% daily breakout with up to three same-symbol/same-hour non-pump days.
It never reads credentials or places orders.
"""
from __future__ import annotations

import argparse
import gzip
import itertools
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests


DAY_MS = 86_400_000
HOUR_MS = 3_600_000
BAR_MS = 900_000
BASE_URL = "https://fapi.binance.com"
USER_AGENT = "GodOfTrading-PumpDerivativesStudy/1.0"

SERIES = {
    "oi_value": ("openInterestHist", "sumOpenInterestValue"),
    "taker_buy_sell_ratio": ("takerlongshortRatio", "buySellRatio"),
    "global_long_short_ratio": ("globalLongShortAccountRatio", "longShortRatio"),
    "top_long_short_ratio": ("topLongShortAccountRatio", "longShortRatio"),
}

FEATURES = [
    "oi_value_change_1h_pct",
    "oi_value_change_6h_pct",
    "oi_value_change_24h_pct",
    "taker_buy_sell_ratio",
    "global_long_short_ratio",
    "top_long_short_ratio",
    "top_minus_global_ratio",
]


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _market_id(symbol: str) -> str:
    return symbol.split("/")[0] + "USDT"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_gzip(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _save_gzip(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))


def _request_series(
    market_id: str,
    endpoint: str,
    value_field: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, float]]:
    cursor = start_ms
    rows: list[dict[str, float]] = []
    seen = set()
    while cursor < end_ms:
        # Binance rejects an over-wide statistics window even when `limit`
        # would truncate it, so page explicitly at 500 hourly buckets.
        page_end = min(end_ms, cursor + 500 * HOUR_MS)
        response = requests.get(
            f"{BASE_URL}/futures/data/{endpoint}",
            params={
                "symbol": market_id,
                "period": "1h",
                "startTime": cursor,
                "endTime": page_end - 1,
                "limit": 500,
            },
            headers={"accept": "application/json", "user-agent": USER_AGENT},
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError(
                f"HTTP {response.status_code} {endpoint}: {response.text[:300]}"
            )
        payload = response.json()
        if isinstance(payload, dict):
            raise RuntimeError(str(payload)[:300])
        if not payload:
            break
        for item in payload:
            timestamp = int(item.get("timestamp") or 0)
            if timestamp in seen or not (start_ms <= timestamp < end_ms):
                continue
            try:
                value = float(item[value_field])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append({"timestamp": timestamp, "value": value})
            seen.add(timestamp)
        last_timestamp = int(payload[-1].get("timestamp") or 0)
        if last_timestamp <= cursor:
            break
        cursor = last_timestamp + 1
        time.sleep(0.04)
    return sorted(rows, key=lambda row: row["timestamp"])


def _cached_series(
    cache_root: Path,
    symbol: str,
    label: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, float]]:
    path = cache_root / label / f"{_safe_symbol(symbol)}.json.gz"
    if path.exists():
        try:
            cached = _load_gzip(path)
            if (
                cached.get("start_ms", 0) <= start_ms
                and cached.get("end_ms", 0) >= end_ms
            ):
                return cached.get("rows", [])
        except Exception:
            pass
    endpoint, field = SERIES[label]
    rows = _request_series(_market_id(symbol), endpoint, field, start_ms, end_ms)
    _save_gzip(path, {
        "symbol": symbol,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "rows": rows,
    })
    return rows


def _at_or_before(rows: list[dict[str, float]], timestamp: int) -> Optional[float]:
    chosen = None
    for row in rows:
        if int(row["timestamp"]) <= timestamp:
            chosen = float(row["value"])
        else:
            break
    return chosen


def _pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous <= 0:
        return None
    return (current / previous - 1) * 100


def _snapshot(series: dict[str, list[dict[str, float]]], anchor_ms: int) -> dict[str, float]:
    # Use only the last fully closed hourly bucket to avoid future leakage from
    # a partially observed hour.
    closed_bucket_ms = (anchor_ms // HOUR_MS) * HOUR_MS - HOUR_MS
    oi_now = _at_or_before(series["oi_value"], closed_bucket_ms)
    result: dict[str, float] = {}
    for hours in (1, 6, 24):
        value = _pct_change(
            oi_now,
            _at_or_before(series["oi_value"], closed_bucket_ms - hours * HOUR_MS),
        )
        if value is not None:
            result[f"oi_value_change_{hours}h_pct"] = value
    for label in (
        "taker_buy_sell_ratio",
        "global_long_short_ratio",
        "top_long_short_ratio",
    ):
        value = _at_or_before(series[label], closed_bucket_ms)
        if value is not None:
            result[label] = value
    if "top_long_short_ratio" in result and "global_long_short_ratio" in result:
        result["top_minus_global_ratio"] = (
            result["top_long_short_ratio"] - result["global_long_short_ratio"]
        )
    return result


def _onset_anchor(cache_15m: Path, symbol: str, day_ms: int) -> Optional[int]:
    path = cache_15m / f"{_safe_symbol(symbol)}.json.gz"
    if not path.exists():
        return None
    payload = _load_gzip(path)
    day_rows = [
        row for row in payload.get("rows", [])
        if day_ms <= int(row[0]) < day_ms + DAY_MS
    ]
    if not day_rows:
        return None
    day_open = float(day_rows[0][1])
    onset = next((row for row in day_rows if float(row[4]) >= day_open * 1.05), None)
    if onset is None:
        onset = next((row for row in day_rows if float(row[2]) >= day_open * 1.05), None)
    return int(onset[0]) - BAR_MS if onset else None


def _median(values: list[float]) -> Optional[float]:
    clean = [value for value in values if math.isfinite(value)]
    return statistics.median(clean) if clean else None


def _quartile(values: list[float], fraction: float) -> Optional[float]:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    position = (len(clean) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(clean) - 1)
    weight = position - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def _sign_test_p(values: list[float]) -> Optional[float]:
    nonzero = [value for value in values if abs(value) > 1e-12]
    if not nonzero:
        return None
    n = len(nonzero)
    positives = sum(value > 0 for value in nonzero)
    tail = min(positives, n - positives)
    probability = sum(math.comb(n, index) for index in range(tail + 1)) / 2 ** n
    return min(1.0, probability * 2)


def _summary(event_rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for feature in FEATURES:
        event_values = [
            float(row["event"][feature])
            for row in event_rows if row["event"].get(feature) is not None
        ]
        control_values = [
            float(row["control_median"][feature])
            for row in event_rows if row["control_median"].get(feature) is not None
        ]
        differences = [
            float(row["event"][feature]) - float(row["control_median"][feature])
            for row in event_rows
            if row["event"].get(feature) is not None
            and row["control_median"].get(feature) is not None
        ]
        result[feature] = {
            "event_n": len(event_values),
            "event_median": _median(event_values),
            "event_p25": _quartile(event_values, 0.25),
            "event_p75": _quartile(event_values, 0.75),
            "control_n": len(control_values),
            "control_median": _median(control_values),
            "matched_n": len(differences),
            "median_event_minus_control": _median(differences),
            "event_above_control_pct": (
                sum(value > 0 for value in differences) / len(differences) * 100
                if differences else None
            ),
            "sign_test_p": _sign_test_p(differences),
        }
    return result


def _pnl_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["net_pct"]) for row in rows]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    return {
        "n": len(values),
        "win_rate_pct": len(wins) / len(values) * 100 if values else 0.0,
        "avg_net_pct": statistics.mean(values) if values else 0.0,
        "sum_net_pct": sum(values),
        "profit_factor": (
            sum(wins) / abs(sum(losses))
            if losses else (99.0 if wins else 0.0)
        ),
    }


def _candidate_gate_backtest(
    source: dict[str, Any],
    series_by_symbol: dict[str, dict[str, list[dict[str, float]]]],
) -> dict[str, Any]:
    best = source.get("best_rule") or {}
    split_ms = int(source["study"]["split_ms"])
    rows = []
    for candidate in (best.get("train_all") or []) + (best.get("test_all") or []):
        symbol = candidate["symbol"]
        if symbol not in series_by_symbol:
            continue
        features = _snapshot(series_by_symbol[symbol], int(candidate["signal_ts"]))
        if not all(features.get(key) is not None for key in (
            "oi_value_change_6h_pct", "oi_value_change_24h_pct", "taker_buy_sell_ratio"
        )):
            continue
        rows.append({
            "symbol": symbol,
            "signal_ts": int(candidate["signal_ts"]),
            "split": "train" if int(candidate["signal_ts"]) < split_ms else "test",
            "net_pct": float(candidate["net_pct"]),
            **features,
        })

    rules = []
    # Thresholds are intentionally coarse and economically interpretable.
    # The winner is selected on train only and reported on untouched OOS.
    for oi_6h, oi_24h, taker in itertools.product(
        (0.0, 0.5, 2.0),
        (0.0, 3.0, 8.0),
        (1.0, 1.03, 1.08),
    ):
        matched = [
            row for row in rows
            if float(row["oi_value_change_6h_pct"]) >= oi_6h
            and float(row["oi_value_change_24h_pct"]) >= oi_24h
            and float(row["taker_buy_sell_ratio"]) >= taker
        ]
        train = [row for row in matched if row["split"] == "train"]
        test = [row for row in matched if row["split"] == "test"]
        train_metrics = _pnl_metrics(train)
        if train_metrics["n"] < 8:
            continue
        rules.append({
            "rule": {
                "oi_6h_min_pct": oi_6h,
                "oi_24h_min_pct": oi_24h,
                "taker_buy_sell_min": taker,
            },
            "train": train_metrics,
            "test": _pnl_metrics(test),
            "train_rows": train,
            "test_rows": test,
            "score": (
                train_metrics["avg_net_pct"] * math.sqrt(train_metrics["n"])
                + min(train_metrics["profit_factor"], 4.0) * 0.15
            ),
        })
    rules.sort(key=lambda item: item["score"], reverse=True)
    return {
        "base_train": _pnl_metrics([row for row in rows if row["split"] == "train"]),
        "base_test": _pnl_metrics([row for row in rows if row["split"] == "test"]),
        "candidate_rows_n": len(rows),
        "rules_tested": len(rules),
        "best_train_selected_rule": rules[0] if rules else None,
        "top_rules": rules[:5],
    }


def _format(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):+.4f}"


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Binance Pump Derivatives Precursor Study",
        "",
        f"- Generated UTC: {result['generated_at_utc']}",
        f"- Eligible pump events: {result['eligible_event_n']}",
        f"- Events with derivatives + matched controls: {result['matched_event_n']}",
        "- Anchor: last fully closed 1h bucket before the bar preceding first +5% daily breakout",
        "- Controls: same symbol/time on up to 3 prior non-pump days",
        "",
        "| feature | event n | event median | control median | matched difference | event > control | sign p |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for feature, stats in result["summary"].items():
        above = stats["event_above_control_pct"]
        lines.append(
            f"| {feature} | {stats['event_n']} | {_format(stats['event_median'])} | "
            f"{_format(stats['control_median'])} | {_format(stats['median_event_minus_control'])} | "
            f"{_format(above)}% | {_format(stats['sign_test_p'])} |"
        )
    lines += [
        "",
        "## Guardrails",
        "",
        "- These are public Binance derivatives aggregates, not blockchain exchange-wallet flows.",
        "- The +5% onset definition means even the prior bucket can reflect an already-started move.",
        "- A common precursor is not automatically profitable after latency, fees, slippage, and false positives.",
    ]
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = _load_json(Path(args.source_json).resolve())
    events = source["eligible_events"]
    pump_days: dict[str, set[int]] = {}
    for event in events:
        pump_days.setdefault(event["symbol"], set()).add(int(event["day_ms"]))

    study_start = int(source["study"]["start_ms"])
    study_end = int(source["study"]["end_ms_exclusive"])
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # The statistics endpoints expose only the latest month.  Stay inside the
    # documented rolling window; earliest study events may therefore be absent.
    fetch_start = max(study_start - DAY_MS, now_ms - 29 * DAY_MS)
    cache_root = Path(args.derivatives_cache).resolve()
    cache_15m = Path(args.market_cache).resolve() / "m15_taker_v2"
    candidate_symbols = {
        row["symbol"]
        for row in (
            (source.get("best_rule") or {}).get("train_all") or []
        ) + (
            (source.get("best_rule") or {}).get("test_all") or []
        )
    }
    study_symbols = sorted(set(pump_days) | candidate_symbols)
    series_by_symbol: dict[str, dict[str, list[dict[str, float]]]] = {}
    failures = []
    for index, symbol in enumerate(study_symbols, 1):
        try:
            series_by_symbol[symbol] = {
                label: _cached_series(
                    cache_root, symbol, label, fetch_start, study_end
                )
                for label in SERIES
            }
        except Exception as exc:
            failures.append({"symbol": symbol, "error": str(exc)[:300]})
        print(f"[derivatives] {index}/{len(study_symbols)} {symbol}", flush=True)
        time.sleep(0.06)

    event_rows = []
    for event in events:
        symbol = event["symbol"]
        if symbol not in series_by_symbol:
            continue
        anchor = _onset_anchor(cache_15m, symbol, int(event["day_ms"]))
        if anchor is None:
            continue
        event_snapshot = _snapshot(series_by_symbol[symbol], anchor)
        controls = []
        for days_back in range(1, 8):
            control_day = int(event["day_ms"]) - days_back * DAY_MS
            if control_day in pump_days[symbol]:
                continue
            control = _snapshot(series_by_symbol[symbol], anchor - days_back * DAY_MS)
            if control:
                controls.append(control)
            if len(controls) >= 3:
                break
        control_median = {
            feature: _median([
                float(control[feature])
                for control in controls if control.get(feature) is not None
            ])
            for feature in FEATURES
        }
        if event_snapshot and any(value is not None for value in control_median.values()):
            event_rows.append({
                "symbol": symbol,
                "date_utc": event["date_utc"],
                "anchor_utc": datetime.fromtimestamp(anchor / 1000, timezone.utc).isoformat(),
                "event": event_snapshot,
                "control_n": len(controls),
                "control_median": control_median,
            })
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": {
            "source": str(Path(args.source_json).resolve()),
            "bar": "1h public Binance futures aggregates",
            "point_in_time": "last fully closed hourly bucket before pre-onset 15m bar",
            "control": "up to 3 same-symbol/same-time prior non-pump days",
            "fetch_start_utc": datetime.fromtimestamp(fetch_start / 1000, timezone.utc).isoformat(),
        },
        "eligible_event_n": len(events),
        "symbol_n": len(study_symbols),
        "matched_event_n": len(event_rows),
        "failures": failures,
        "summary": _summary(event_rows),
        "candidate_gate_backtest": _candidate_gate_backtest(source, series_by_symbol),
        "events": event_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-json", default="binance_pump_study_latest.json")
    parser.add_argument("--market-cache", default="/tmp/got_binance_pump_study")
    parser.add_argument("--derivatives-cache", default="/tmp/got_binance_pump_derivatives")
    parser.add_argument("--output-json", default="binance_pump_derivatives_latest.json")
    parser.add_argument("--output-md", default="BINANCE_PUMP_DERIVATIVES_LATEST.md")
    args = parser.parse_args()
    result = run(args)
    Path(args.output_json).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path(args.output_md).write_text(_markdown(result), encoding="utf-8")
    print(json.dumps({
        "matched_event_n": result["matched_event_n"],
        "failure_n": len(result["failures"]),
        "summary": result["summary"],
        "candidate_gate_backtest": result["candidate_gate_backtest"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
