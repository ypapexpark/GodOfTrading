"""Bithumb deposit/withdrawal pause event study using public market data.

The event list contains official Bithumb notice publication timestamps.  It
separates emergency pauses (security/network failure) from scheduled network
maintenance.  Publication time is used as the first legally/publicly observable
exchange event; no private information or order endpoint is used.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)
import requests


KST = timezone(timedelta(hours=9))
BITHUMB_API = "https://api.bithumb.com"
BINANCE_API = "https://fapi.binance.com"
BAR_MINUTES = 15
BAR_MS = BAR_MINUTES * 60_000
USER_AGENT = "GodOfTrading-BithumbWalletStudy/1.0"


EVENTS = [
    # Unscheduled network/security events.  Multiple coins on one chain remain
    # one cluster so a 28-token Base outage is not counted as 28 independent
    # observations.
    {
        "id": "sui_20260115", "kind": "emergency_network",
        "published_at": "2026-01-15 00:45:06", "assets": ["SUI"],
        "url": "https://feed.bithumb.com/notice/1651496",
    },
    {
        "id": "stx_20260208", "kind": "emergency_network",
        "published_at": "2026-02-08 11:45:07", "assets": ["STX"],
        "url": "https://feed.bithumb.com/notice/1651927",
    },
    {
        "id": "g_20260210", "kind": "emergency_network",
        "published_at": "2026-02-10 08:54:08", "assets": ["G"],
        "url": "https://feed.bithumb.com/notice/1651945",
    },
    {
        "id": "rei_20260213", "kind": "emergency_network",
        "published_at": "2026-02-13 08:46:44", "assets": ["REI"],
        "url": "https://feed.bithumb.com/notice/1652002",
    },
    {
        "id": "drift_20260402", "kind": "emergency_security",
        "published_at": "2026-04-02 03:07:55", "assets": ["DRIFT"],
        "url": "https://feed.bithumb.com/notice/1652530",
    },
    {
        "id": "mapo_20260521", "kind": "emergency_security",
        "published_at": "2026-05-21 01:55:03", "assets": ["MAPO"],
        "url": "https://feed.bithumb.com/notice/1653331",
    },
    {
        "id": "sui_20260528", "kind": "emergency_network",
        "published_at": "2026-05-28 23:48:38",
        "assets": ["SUI", "DEEP", "WAL", "BLUE", "HAEDAL"],
        "url": "https://feed.bithumb.com/notice/1653451",
    },
    {
        "id": "sui_20260529", "kind": "emergency_network",
        "published_at": "2026-05-29 21:24:26",
        "assets": ["SUI", "DEEP", "WAL", "BLUE", "HAEDAL"],
        "url": "https://feed.bithumb.com/notice/1653477",
    },
    {
        "id": "ip_20260621", "kind": "emergency_network",
        "published_at": "2026-06-21 01:16:14", "assets": ["IP", "ARIAIP"],
        "url": "https://feed.bithumb.com/notice/1653793",
    },
    {
        "id": "taiko_20260622", "kind": "emergency_network",
        "published_at": "2026-06-22 07:41:38", "assets": ["TAIKO"],
        "url": "https://feed.bithumb.com/notice/1653794",
    },
    {
        "id": "base_20260627", "kind": "emergency_network",
        "published_at": "2026-06-27 02:58:38",
        "assets": [
            "AERO", "AVNT", "AWE", "B3", "BRETT", "C", "CARV", "EDGE",
            "FLOCK", "GPS", "HOME", "KAITO", "OPG", "PROMPT", "SIGN",
            "TOSHI", "VIRTUAL", "VVV", "ZORA",
        ],
        "url": "https://feed.bithumb.com/notice/1653890",
    },
    {
        "id": "hbar_20260711", "kind": "emergency_security",
        "published_at": "2026-07-11 17:38:17", "assets": ["HBAR"],
        "url": "https://feed.bithumb.com/notice/1654024",
    },
    {
        "id": "vana_20260714", "kind": "emergency_network",
        "published_at": "2026-07-14 07:20:08", "assets": ["VANA"],
        "url": "https://feed.bithumb.com/notice/1654028",
    },
    # Scheduled notices: the pause itself was already public and typically tied
    # to a foundation upgrade calendar.  These form the non-emergency control.
    {
        "id": "inj_20260403", "kind": "scheduled_upgrade",
        "published_at": "2026-04-03 12:00:00", "assets": ["INJ"],
        "url": "https://feed.bithumb.com/notice/1652535",
    },
    {
        "id": "ada_20260406", "kind": "scheduled_wallet_change",
        "published_at": "2026-04-06 11:00:00", "assets": ["ADA"],
        "url": "https://feed.bithumb.com/notice/1652540",
    },
    {
        "id": "hbar_20260409", "kind": "scheduled_upgrade",
        "published_at": "2026-04-09 19:00:00", "assets": ["HBAR"],
        "url": "https://feed.bithumb.com/notice/1652588",
    },
    {
        "id": "hbar_20260515", "kind": "scheduled_upgrade",
        "published_at": "2026-05-15 18:10:00", "assets": ["HBAR"],
        "url": "https://feed.bithumb.com/notice/1653273",
    },
    {
        "id": "cspr_20260518", "kind": "scheduled_upgrade",
        "published_at": "2026-05-18 18:30:00", "assets": ["CSPR"],
        "url": "https://feed.bithumb.com/notice/1653291",
    },
    {
        "id": "fil_20260520", "kind": "scheduled_upgrade",
        "published_at": "2026-05-20 18:00:00", "assets": ["FIL"],
        "url": "https://feed.bithumb.com/notice/1653330",
    },
    {
        "id": "inj_20260601", "kind": "scheduled_upgrade",
        "published_at": "2026-06-01 18:30:00", "assets": ["INJ"],
        "url": "https://feed.bithumb.com/notice/1653484",
    },
    {
        "id": "iotx_20260602", "kind": "scheduled_upgrade",
        "published_at": "2026-06-02 18:00:00", "assets": ["IOTX"],
        "url": "https://feed.bithumb.com/notice/1653509",
    },
    {
        "id": "atom_20260605", "kind": "scheduled_upgrade",
        "published_at": "2026-06-05 12:00:00", "assets": ["ATOM"],
        "url": "https://feed.bithumb.com/notice/1653546",
    },
    {
        "id": "xion_20260616", "kind": "scheduled_rebrand",
        "published_at": "2026-06-16 18:30:00", "assets": ["XION"],
        "url": "https://feed.bithumb.com/notice/1653734",
    },
    {
        "id": "0g_20260623", "kind": "scheduled_upgrade",
        "published_at": "2026-06-23 19:00:00", "assets": ["0G"],
        "url": "https://feed.bithumb.com/notice/1653841",
    },
    {
        "id": "op_20260702", "kind": "scheduled_upgrade",
        "published_at": "2026-07-02 16:30:00", "assets": ["OP"],
        "url": "https://feed.bithumb.com/notice/1653928",
    },
    {
        "id": "cro_20260709", "kind": "scheduled_upgrade",
        "published_at": "2026-07-09 18:00:00", "assets": ["CRO"],
        "url": "https://feed.bithumb.com/notice/1654013",
    },
    {
        "id": "flr_20260710", "kind": "scheduled_upgrade",
        "published_at": "2026-07-10 11:00:00", "assets": ["FLR"],
        "url": "https://feed.bithumb.com/notice/1654014",
    },
    {
        "id": "near_20260713", "kind": "scheduled_upgrade",
        "published_at": "2026-07-13 18:30:00", "assets": ["NEAR"],
        "url": "https://feed.bithumb.com/notice/1654027",
    },
    {
        "id": "ada_20260714", "kind": "scheduled_upgrade",
        "published_at": "2026-07-14 19:00:00", "assets": ["ADA"],
        "url": "https://feed.bithumb.com/notice/1654035",
    },
]


def _get_json(base: str, path: str, params: dict[str, Any]) -> Any:
    response = requests.get(
        base + path,
        params=params,
        timeout=12,
        headers={"accept": "application/json", "user-agent": USER_AGENT},
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data


def _parse_kst(value: str) -> datetime:
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=KST)


def _fetch_bithumb(asset: str, center: datetime) -> list[dict[str, Any]]:
    to = (center + timedelta(hours=26)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = _get_json(
        BITHUMB_API,
        f"/v1/candles/minutes/{BAR_MINUTES}",
        {"market": f"KRW-{asset}", "to": to, "count": 200},
    )
    clean = []
    for row in rows:
        clean.append({
            "ts": _parse_kst(row["candle_date_time_kst"]),
            "open": float(row["opening_price"]),
            "high": float(row["high_price"]),
            "low": float(row["low_price"]),
            "close": float(row["trade_price"]),
            "qvol": float(row.get("candle_acc_trade_price") or 0),
        })
    return sorted(clean, key=lambda row: row["ts"])


def _fetch_binance(asset: str, center: datetime) -> list[dict[str, Any]]:
    start = int((center - timedelta(hours=26)).timestamp() * 1000)
    end = int((center + timedelta(hours=26)).timestamp() * 1000)
    rows = _get_json(
        BINANCE_API,
        "/fapi/v1/klines",
        {
            "symbol": f"{asset}USDT", "interval": "15m",
            "startTime": start, "endTime": end, "limit": 300,
        },
    )
    return [
        {
            "ts": datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).astimezone(KST),
            "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
            "close": float(row[4]), "qvol": float(row[7]),
        }
        for row in rows
    ]


def _at_or_before(rows: list[dict[str, Any]], target: datetime) -> Optional[dict[str, Any]]:
    chosen = None
    for row in rows:
        if row["ts"] <= target:
            chosen = row
        else:
            break
    return chosen


def _entry_bar(rows: list[dict[str, Any]], event_time: datetime) -> Optional[dict[str, Any]]:
    # Next 15m open avoids pretending we filled before reading the notice.
    target_ms = int(event_time.timestamp() * 1000)
    next_ms = ((target_ms + BAR_MS - 1) // BAR_MS) * BAR_MS
    next_time = datetime.fromtimestamp(next_ms / 1000, timezone.utc).astimezone(KST)
    for row in rows:
        if row["ts"] >= next_time:
            return row
    return None


def _ret(price: float, base: float) -> Optional[float]:
    if price <= 0 or base <= 0:
        return None
    return (price / base - 1) * 100


def _median(values: list[float]) -> Optional[float]:
    clean = [value for value in values if math.isfinite(value)]
    return statistics.median(clean) if clean else None


def _features(rows: list[dict[str, Any]], event_time: datetime) -> Optional[dict[str, Any]]:
    entry = _entry_bar(rows, event_time)
    if not entry:
        return None
    entry_time = entry["ts"]
    p0 = float(entry["open"])
    before_1h = _at_or_before(rows, event_time - timedelta(hours=1))
    before_6h = _at_or_before(rows, event_time - timedelta(hours=6))
    before_24h = _at_or_before(rows, event_time - timedelta(hours=24))
    at_notice = _at_or_before(rows, event_time)
    if not all((before_1h, before_6h, before_24h, at_notice)):
        return None

    prior_hours = []
    for hour in range(1, 25):
        right = event_time - timedelta(hours=hour - 1)
        left = event_time - timedelta(hours=hour)
        prior_hours.append(sum(row["qvol"] for row in rows if left <= row["ts"] < right))
    median_hourly = _median(prior_hours[1:]) or 0.0
    pre_1h_qvol = prior_hours[0]

    result: dict[str, Any] = {
        "entry_time": entry_time.isoformat(),
        "entry": p0,
        "pre_ret_1h_pct": _ret(float(at_notice["close"]), float(before_1h["close"])),
        "pre_ret_6h_pct": _ret(float(at_notice["close"]), float(before_6h["close"])),
        "pre_ret_24h_pct": _ret(float(at_notice["close"]), float(before_24h["close"])),
        "pre_qvol_1h": pre_1h_qvol,
        "pre_qvol_ratio": pre_1h_qvol / median_hourly if median_hourly > 0 else None,
    }
    for hours in (1, 6, 24):
        target = entry_time + timedelta(hours=hours)
        last = _at_or_before(rows, target)
        window = [row for row in rows if entry_time <= row["ts"] < target]
        result[f"post_ret_{hours}h_pct"] = _ret(float(last["close"]), p0) if last else None
        result[f"post_mfe_{hours}h_pct"] = _ret(max(row["high"] for row in window), p0) if window else None
        result[f"post_mae_{hours}h_pct"] = _ret(min(row["low"] for row in window), p0) if window else None
    return result


def _mean_dict(rows: list[dict[str, Any]], keys: list[str]) -> dict[str, Optional[float]]:
    result: dict[str, Optional[float]] = {}
    for key in keys:
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        result[key] = statistics.mean(values) if values else None
    return result


METRIC_KEYS = [
    "pre_ret_1h_pct", "pre_ret_6h_pct", "pre_ret_24h_pct", "pre_qvol_ratio",
    "post_ret_1h_pct", "post_ret_6h_pct", "post_ret_24h_pct",
    "post_mfe_1h_pct", "post_mfe_6h_pct", "post_mfe_24h_pct",
    "post_mae_1h_pct", "post_mae_6h_pct", "post_mae_24h_pct",
    "abnormal_ret_1h_pct", "abnormal_ret_6h_pct", "abnormal_ret_24h_pct",
]


def _event_cluster(event: dict[str, Any]) -> dict[str, Any]:
    event_time = _parse_kst(event["published_at"])
    asset_rows = []
    errors = []
    for asset in event["assets"]:
        try:
            b_features = _features(_fetch_bithumb(asset, event_time), event_time)
            if not b_features:
                raise RuntimeError("insufficient Bithumb candles")
            try:
                x_features = _features(_fetch_binance(asset, event_time), event_time)
            except Exception:
                x_features = None
            row = {"asset": asset, **b_features}
            if x_features:
                for hours in (1, 6, 24):
                    b_ret = b_features.get(f"post_ret_{hours}h_pct")
                    x_ret = x_features.get(f"post_ret_{hours}h_pct")
                    row[f"binance_ret_{hours}h_pct"] = x_ret
                    row[f"abnormal_ret_{hours}h_pct"] = (
                        float(b_ret) - float(x_ret)
                        if b_ret is not None and x_ret is not None else None
                    )
            asset_rows.append(row)
        except Exception as exc:
            errors.append({"asset": asset, "error": str(exc)[:200]})
        time.sleep(0.06)
    return {
        **event,
        "asset_n": len(asset_rows),
        "metrics": _mean_dict(asset_rows, METRIC_KEYS),
        "assets_result": asset_rows,
        "errors": errors,
    }


PRE_KEYS = ["pre_ret_1h_pct", "pre_ret_6h_pct", "pre_ret_24h_pct", "pre_qvol_ratio"]


def _near_other_event(asset: str, control_time: datetime) -> bool:
    """Avoid using another known pause/maintenance window as a normal control."""
    return any(
        asset in event["assets"]
        and abs((_parse_kst(event["published_at"]) - control_time).total_seconds()) < 36 * 3600
        for event in EVENTS
    )


def _percentile_rank(value: float, samples: list[float]) -> Optional[float]:
    clean = [sample for sample in samples if math.isfinite(sample)]
    if not clean:
        return None
    below = sum(sample < value for sample in clean)
    equal = sum(sample == value for sample in clean)
    return (below + 0.5 * equal) / len(clean) * 100


def _control_features(
    asset: str,
    event_time: datetime,
    event_features: dict[str, Any],
) -> Optional[dict[str, Any]]:
    controls = []
    for days in range(1, 31):
        control_time = event_time - timedelta(days=days)
        if _near_other_event(asset, control_time):
            continue
        try:
            features = _features(_fetch_bithumb(asset, control_time), control_time)
            if features:
                controls.append(features)
        except Exception:
            pass
        time.sleep(0.11)
    if not controls:
        return None
    medians = {}
    percentiles = {}
    samples_by_key = {}
    for key in PRE_KEYS:
        samples = [float(row[key]) for row in controls if row.get(key) is not None]
        if not samples or event_features.get(key) is None:
            continue
        samples_by_key[key] = samples
        medians[key] = statistics.median(samples)
        percentiles[key] = _percentile_rank(float(event_features[key]), samples)
    return {
        "n": len(controls),
        "median": medians,
        "percentile": percentiles,
        "samples": samples_by_key,
    }


def _sign_test_p(differences: list[float]) -> Optional[float]:
    nonzero = [value for value in differences if abs(value) > 1e-12]
    n = len(nonzero)
    if not n:
        return None
    positive = sum(value > 0 for value in nonzero)
    tail = min(positive, n - positive)
    probability = sum(math.comb(n, i) for i in range(tail + 1)) / (2 ** n)
    return min(1.0, probability * 2)


def _summary(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for label, subset in (
        ("emergency", [row for row in clusters if row["kind"].startswith("emergency")]),
        ("scheduled", [row for row in clusters if row["kind"].startswith("scheduled")]),
    ):
        metrics = {}
        for key in METRIC_KEYS:
            values = [
                float(row["metrics"][key]) for row in subset
                if row.get("metrics", {}).get(key) is not None
            ]
            metrics[key] = {
                "n": len(values),
                "median": _median(values),
                "mean": statistics.mean(values) if values else None,
                "positive_pct": sum(value > 0 for value in values) / len(values) * 100 if values else None,
            }
        for threshold in (5, 10, 20):
            values = [
                float(row["metrics"]["post_mfe_24h_pct"]) for row in subset
                if row.get("metrics", {}).get("post_mfe_24h_pct") is not None
            ]
            metrics[f"mfe24_ge_{threshold}_pct"] = (
                sum(value >= threshold for value in values) / len(values) * 100 if values else None
            )
        result[label] = {"cluster_n": len(subset), "metrics": metrics}
    return result


def _format(value: Any, suffix: str = "%") -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.2f}{suffix}"


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Bithumb Wallet Pause Event Study",
        "",
        f"- Generated: {result['generated_at']}",
        f"- Emergency clusters: {result['summary']['emergency']['cluster_n']}",
        f"- Scheduled clusters: {result['summary']['scheduled']['cluster_n']}",
        "- Entry assumption: next 15-minute open after public notice",
        "- Emergency multi-asset outages are equal-weighted once per chain event",
        "",
        "## Aggregate",
        "",
        "| cohort | pre 1h | pre volume ratio | post 1h | post 6h | post 24h | 24h MFE | 24h ≥5% | Binance-relative 24h |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label in ("emergency", "scheduled"):
        metrics = result["summary"][label]["metrics"]
        med = lambda key: metrics[key]["median"]
        lines.append(
            f"| {label} | {_format(med('pre_ret_1h_pct'))} | "
            f"{_format(med('pre_qvol_ratio'), 'x')} | {_format(med('post_ret_1h_pct'))} | "
            f"{_format(med('post_ret_6h_pct'))} | {_format(med('post_ret_24h_pct'))} | "
            f"{_format(med('post_mfe_24h_pct'))} | "
            f"{_format(metrics['mfe24_ge_5_pct'])} | {_format(med('abnormal_ret_24h_pct'))} |"
        )
    lines += ["", "## Emergency pre-notice matched controls", ""]
    for key, stats in result["precursor_test"].items():
        unit = " log10(x)" if key == "pre_qvol_log10_ratio" else ("x" if key == "pre_qvol_ratio" else "%")
        lines.append(
            f"- {key}: n={stats['n']}, median(event-control)={_format(stats['median_difference'], unit)}, "
            f"positive={stats['positive_pct']:.1f}%, median event percentile={_format(stats.get('median_event_percentile'))}, "
            f"sign-test p={stats['sign_test_p'] if stats['sign_test_p'] is not None else 'n/a'}"
        )
    lines += [
        "",
        "## Event clusters",
        "",
        "| published KST | kind | assets n | post 1h | post 6h | post 24h | 24h MFE | 24h MAE | abnormal 24h |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["clusters"]:
        m = row["metrics"]
        lines.append(
            f"| {row['published_at']} | {row['kind']} | {row['asset_n']} | "
            f"{_format(m.get('post_ret_1h_pct'))} | {_format(m.get('post_ret_6h_pct'))} | "
            f"{_format(m.get('post_ret_24h_pct'))} | {_format(m.get('post_mfe_24h_pct'))} | "
            f"{_format(m.get('post_mae_24h_pct'))} | {_format(m.get('abnormal_ret_24h_pct'))} |"
        )
    lines += [
        "",
        "## Interpretation guardrails",
        "",
        "- A pause blocks arbitrage inventory transfer; it does not mechanically create buy demand.",
        "- Security-related pauses can be negative fundamental news, unlike scheduled upgrades.",
        "- Buying before an emergency notice is not an implementable rule unless a public precursor is observed in real time.",
        "- Historical wallet status was not available; block lag and wallet_state must be collected prospectively.",
    ]
    return "\n".join(lines) + "\n"


def run() -> dict[str, Any]:
    clusters = []
    for index, event in enumerate(EVENTS, 1):
        cluster = _event_cluster(event)
        clusters.append(cluster)
        print(
            f"[{index}/{len(EVENTS)}] {event['id']} assets={cluster['asset_n']} "
            f"errors={len(cluster['errors'])}",
            flush=True,
        )

    precursor_rows = []
    for cluster in clusters:
        if not cluster["kind"].startswith("emergency") or not cluster["assets_result"]:
            continue
        # One primary/first asset per event avoids correlated ecosystem copies.
        asset_row = cluster["assets_result"][0]
        event_time = _parse_kst(cluster["published_at"])
        control = _control_features(asset_row["asset"], event_time, asset_row)
        if not control:
            continue
        row = {
            "event_id": cluster["id"],
            "asset": asset_row["asset"],
            "control_n": control["n"],
        }
        for key in PRE_KEYS:
            if asset_row.get(key) is not None and control["median"].get(key) is not None:
                row[key] = float(asset_row[key]) - float(control["median"][key])
                row[f"{key}_event_percentile"] = control["percentile"].get(key)
        event_qvol_ratio = asset_row.get("pre_qvol_ratio")
        control_qvol = control["samples"].get("pre_qvol_ratio", [])
        positive_control_qvol = [value for value in control_qvol if value > 0]
        if event_qvol_ratio is not None and float(event_qvol_ratio) > 0 and positive_control_qvol:
            row["pre_qvol_log10_ratio"] = (
                math.log10(float(event_qvol_ratio))
                - statistics.median(math.log10(value) for value in positive_control_qvol)
            )
            row["pre_qvol_log10_ratio_event_percentile"] = control["percentile"].get("pre_qvol_ratio")
        precursor_rows.append(row)
        print(f"[control] {cluster['id']} {asset_row['asset']} n={control['n']}", flush=True)

    precursor_test = {}
    for key in (*PRE_KEYS, "pre_qvol_log10_ratio"):
        differences = [float(row[key]) for row in precursor_rows if row.get(key) is not None]
        percentiles = [
            float(row[f"{key}_event_percentile"])
            for row in precursor_rows
            if row.get(f"{key}_event_percentile") is not None
        ]
        precursor_test[key] = {
            "n": len(differences),
            "median_difference": _median(differences),
            "mean_difference": statistics.mean(differences) if differences else None,
            "positive_pct": sum(value > 0 for value in differences) / len(differences) * 100 if differences else 0.0,
            "median_event_percentile": _median(percentiles),
            "sign_test_p": _sign_test_p(differences),
        }
    return {
        "generated_at": datetime.now(KST).isoformat(),
        "method": {
            "bar_minutes": BAR_MINUTES,
            "entry": "next 15m open after official notice published_at",
            "control": "same asset/time on up to 30 prior days; known +/-36h event windows excluded; median baseline",
            "fees_slippage": "not included; event study, not trading backtest",
        },
        "summary": _summary(clusters),
        "precursor_test": precursor_test,
        "precursor_rows": precursor_rows,
        "clusters": clusters,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-json", default="bithumb_wallet_event_study_latest.json")
    parser.add_argument("--output-md", default="BITHUMB_WALLET_EVENT_STUDY_LATEST.md")
    args = parser.parse_args()
    result = run()
    Path(args.output_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.output_md).write_text(_markdown(result), encoding="utf-8")
    print(json.dumps({"summary": result["summary"], "precursor_test": result["precursor_test"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
