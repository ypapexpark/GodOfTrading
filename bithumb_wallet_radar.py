"""Bithumb wallet-pause precursor radar and prospective event recorder.

This process never submits an order.  It records signals that are observable in
real time so the wallet-pause hypothesis can be tested without look-ahead:

* public Bithumb notices,
* all KRW tickers and forming one-minute price/turnover history,
* authenticated read-only wallet/block status when BITHUMB_* keys exist.

The historical event study found large intraday excursions after some emergency
pauses, but negative median close-to-close returns.  Therefore a wallet pause is
an event to measure, not an automatic live buy trigger.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import statistics
import time
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Optional

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)
import requests
from dotenv import load_dotenv

from bot_util import append_jsonl, load_json, now_kst, read_jsonl, save_json
from process_lock import release, try_acquire
from publisher import send_bithumb


ROOT = Path(__file__).parent
API_BASE = "https://api.bithumb.com"
STATE_FILE = ROOT / "bithumb_wallet_radar_state.json"
JOURNAL_FILE = ROOT / "bithumb_wallet_radar_journal.jsonl"
LOCK_NAME = "bithumb_wallet_radar"
POLICY = "bithumb_wallet_precursor_forward_v1"
KST = timezone(timedelta(hours=9))

MARKET_TTL_SECONDS = 6 * 3600
REPORT_INTERVAL_SECONDS = 4 * 3600
NOTICE_COUNT = 20
TICKER_BATCH = 80
HISTORY_MINUTES = 180
BASELINE_MAX_SAMPLES = 2_016  # one sample/minute for up to two weeks
EVENT_TTL_SECONDS = 25 * 3600

# A price/turnover move is supporting evidence, not a stand-alone wallet-pause
# prediction.  The upper return bound avoids labelling an already vertical move
# as a useful pre-buy observation.
MIN_15M_QVOL_KRW = 10_000_000.0
MIN_15M_VOLUME_RATIO = 3.0
MIN_PRECURSOR_RET_15M_PCT = -5.0
MAX_PRECURSOR_RET_15M_PCT = 8.0

load_dotenv(ROOT / ".env")


def _default_state() -> dict[str, Any]:
    return {
        "policy": POLICY,
        "started_at": now_kst(),
        "markets": [],
        "markets_updated_ts": 0.0,
        "market_stats": {},
        "wallets": {},
        "block_baselines": {},
        "notices_seen": [],
        "notice_bootstrapped": False,
        "open_events": {},
        "last_alert_ts": {},
        "last_wallet_baseline_minute": 0,
        "last_report_time": 0.0,
        "last_report_delivered": False,
        "auth_status": "unknown",
        "last_scan": {},
    }


def _load_state() -> dict[str, Any]:
    default = _default_state()
    raw = load_json(STATE_FILE, {}) or {}
    if not isinstance(raw, dict) or raw.get("policy") != POLICY:
        return default
    default.update(raw)
    for key in (
        "market_stats", "wallets", "block_baselines", "open_events",
        "last_alert_ts", "last_scan",
    ):
        if not isinstance(default.get(key), dict):
            default[key] = {}
    for key in ("markets", "notices_seen"):
        if not isinstance(default.get(key), list):
            default[key] = []
    return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_jwt(access_key: str, secret_key: str, *, now_ms: Optional[int] = None,
             nonce: Optional[str] = None) -> str:
    """Create the HS256 token required by Bithumb read-only private APIs."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "access_key": access_key,
        "nonce": nonce or str(uuid.uuid4()),
        "timestamp": int(now_ms if now_ms is not None else time.time() * 1000),
    }
    encoded = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in (header, payload)
    )
    signature = hmac.new(
        secret_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64url(signature)}"


def _public_get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            response = requests.get(
                API_BASE + path,
                params=params,
                timeout=10,
                headers={"accept": "application/json", "user-agent": "GodOfTrading-BithumbRadar/1.0"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.3)
    raise RuntimeError(f"Bithumb public API failed: {path}: {last_error}")


def _fetch_wallet_status() -> tuple[Optional[list[dict[str, Any]]], str]:
    access_key = os.getenv("BITHUMB_ACCESS_KEY", "").strip()
    secret_key = os.getenv("BITHUMB_SECRET_KEY", "").strip()
    if not access_key or not secret_key or "여기에" in access_key or "여기에" in secret_key:
        return None, "missing_readonly_keys"
    token = make_jwt(access_key, secret_key)
    try:
        response = requests.get(
            API_BASE + "/v1/status/wallet",
            headers={"Authorization": f"Bearer {token}", "accept": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError("wallet response was not a list")
        return rows, "ok"
    except Exception as exc:
        # Never include the token/key in state or logs.
        return None, f"error:{type(exc).__name__}:{str(exc)[:160]}"


def _active_markets(state: dict[str, Any], now: float) -> list[str]:
    cached = [str(x) for x in state.get("markets") or []]
    if cached and now - _safe_float(state.get("markets_updated_ts")) < MARKET_TTL_SECONDS:
        return cached
    rows = _public_get("/v1/market/all", {"isDetails": "true"})
    markets = sorted(
        str(row["market"])
        for row in rows
        if str(row.get("market") or "").startswith("KRW-")
        and str(row.get("market_warning") or "NONE") != "CAUTION"
    )
    state["markets"] = markets
    state["markets_updated_ts"] = now
    return markets


def _fetch_tickers(markets: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index in range(0, len(markets), TICKER_BATCH):
        batch = markets[index:index + TICKER_BATCH]
        rows = _public_get("/v1/ticker", {"markets": ",".join(batch)})
        for row in rows:
            market = str(row.get("market") or "")
            if market:
                result[market] = row
        if index + TICKER_BATCH < len(markets):
            time.sleep(0.06)
    return result


def _finish_minute(stats: dict[str, Any]) -> None:
    if not stats.get("minute_start") or not stats.get("minute_open"):
        return
    rows = stats.setdefault("minutes", [])
    rows.append([
        int(stats["minute_start"]),
        _safe_float(stats["minute_open"]),
        _safe_float(stats["minute_high"]),
        _safe_float(stats["minute_low"]),
        _safe_float(stats["minute_close"]),
        max(0.0, _safe_float(stats["minute_qvol"])),
    ])
    stats["minutes"] = rows[-HISTORY_MINUTES:]


def update_market_stats(state: dict[str, Any], tickers: dict[str, dict[str, Any]],
                        now: float) -> None:
    """Build compact forming 1m bars from all-market ticker snapshots."""
    minute_start = int(now // 60 * 60)
    all_stats = state.setdefault("market_stats", {})
    for market, row in tickers.items():
        price = _safe_float(row.get("trade_price"))
        acc = _safe_float(row.get("acc_trade_price"))
        trade_date = str(row.get("trade_date_kst") or "")
        if price <= 0:
            continue
        stats = all_stats.setdefault(market, {"minutes": []})
        previous_acc = _safe_float(stats.get("last_acc"))
        previous_date = str(stats.get("last_trade_date") or "")
        qvol_delta = 0.0
        if previous_acc > 0 and previous_date == trade_date and acc >= previous_acc:
            qvol_delta = acc - previous_acc

        if int(stats.get("minute_start") or 0) != minute_start:
            _finish_minute(stats)
            stats.update({
                "minute_start": minute_start,
                "minute_open": price,
                "minute_high": price,
                "minute_low": price,
                "minute_close": price,
                "minute_qvol": 0.0,
            })
        stats["minute_high"] = max(_safe_float(stats.get("minute_high"), price), price)
        low = _safe_float(stats.get("minute_low"), price)
        stats["minute_low"] = min(low if low > 0 else price, price)
        stats["minute_close"] = price
        stats["minute_qvol"] = _safe_float(stats.get("minute_qvol")) + max(0.0, qvol_delta)
        stats["last_acc"] = acc
        stats["last_trade_date"] = trade_date
        stats["last_price"] = price
        stats["last_ts"] = now


def _price_at_or_before(rows: list[list[float]], target: float) -> Optional[float]:
    chosen: Optional[float] = None
    for row in rows:
        if float(row[0]) <= target:
            chosen = float(row[4])
        else:
            break
    return chosen


def market_features(state: dict[str, Any], market: str, now: float) -> Optional[dict[str, float]]:
    stats = (state.get("market_stats") or {}).get(market) or {}
    rows = [list(row) for row in stats.get("minutes") or []]
    if stats.get("minute_start"):
        rows.append([
            int(stats["minute_start"]), _safe_float(stats.get("minute_open")),
            _safe_float(stats.get("minute_high")), _safe_float(stats.get("minute_low")),
            _safe_float(stats.get("minute_close")), _safe_float(stats.get("minute_qvol")),
        ])
    if len(rows) < 35:
        return None
    current = _safe_float(stats.get("last_price"))
    p15 = _price_at_or_before(rows, now - 15 * 60)
    p60 = _price_at_or_before(rows, now - 60 * 60)
    if current <= 0 or not p15:
        return None
    qvol15 = sum(float(row[5]) for row in rows if float(row[0]) >= now - 15 * 60)
    prior_windows = []
    for window in range(1, 9):
        right = now - window * 15 * 60
        left = right - 15 * 60
        value = sum(float(row[5]) for row in rows if left <= float(row[0]) < right)
        if value > 0:
            prior_windows.append(value)
    baseline = statistics.median(prior_windows) if prior_windows else 0.0
    return {
        "price": current,
        "ret_15m_pct": (current / p15 - 1) * 100,
        "ret_60m_pct": (current / p60 - 1) * 100 if p60 else 0.0,
        "qvol_15m_krw": qvol15,
        "qvol_ratio": qvol15 / baseline if baseline > 0 else 0.0,
        "history_minutes": float(len(rows)),
    }


def qualifies_precursor(features: Optional[dict[str, float]]) -> bool:
    if not features:
        return False
    return (
        features["qvol_15m_krw"] >= MIN_15M_QVOL_KRW
        and features["qvol_ratio"] >= MIN_15M_VOLUME_RATIO
        and MIN_PRECURSOR_RET_15M_PCT <= features["ret_15m_pct"] <= MAX_PRECURSOR_RET_15M_PCT
    )


def wallet_lag_threshold(samples: list[Any]) -> float:
    clean = sorted(max(0.0, _safe_float(value)) for value in samples)
    if len(clean) < 20:
        return 10.0
    median = statistics.median(clean)
    p95 = clean[min(len(clean) - 1, int((len(clean) - 1) * 0.95))]
    return max(10.0, p95 + 5.0, median * 4.0 + 2.0)


def _wallet_key(row: dict[str, Any]) -> str:
    return f"{str(row.get('currency') or '').upper()}:{str(row.get('net_type') or '').upper()}"


def _wallet_is_abnormal(row: dict[str, Any], threshold: float) -> bool:
    block_state = str(row.get("block_state") or "").lower()
    elapsed = _safe_float(row.get("block_elapsed_minutes"))
    return block_state not in ("", "normal") or elapsed >= threshold


def classify_notice(notice: dict[str, Any]) -> str:
    title = str(notice.get("title") or "")
    categories = " ".join(str(x) for x in notice.get("categories") or [])
    text = f"{categories} {title}".lower()
    if "입출금" not in text and "입금" not in text and "출금" not in text:
        return "other"
    if "재개" in text:
        return "resume"
    if any(word in text for word in ("해킹", "보안", "취약", "공격", "탈취", "이상")):
        return "emergency_security"
    if any(word in text for word in ("업그레이드", "하드포크", "마이그레이션", "정기", "예정")):
        return "scheduled"
    if any(word in text for word in ("중단", "중지", "지연", "장애", "점검")):
        return "emergency_network"
    return "wallet_notice"


def notice_assets(notice: dict[str, Any]) -> list[str]:
    title = str(notice.get("title") or "")
    return sorted(set(re.findall(r"\(([A-Z0-9]{1,15})\)", title)))


def _journal(event: str, **payload: Any) -> None:
    append_jsonl(JOURNAL_FILE, {"time": now_kst(), "ts": time.time(), "event": event, **payload})


def _event_id(cohort: str, asset: str, now: float) -> str:
    return f"{cohort}:{asset}:{int(now // 300 * 300)}"


def _start_forward_event(state: dict[str, Any], cohort: str, asset: str,
                         features: Optional[dict[str, float]], now: float,
                         context: dict[str, Any]) -> bool:
    if not features or features.get("price", 0) <= 0:
        return False
    event_id = _event_id(cohort, asset, now)
    if event_id in state["open_events"]:
        return False
    event = {
        "id": event_id,
        "cohort": cohort,
        "asset": asset,
        "market": f"KRW-{asset}",
        "entry_ts": now,
        "entry_time": now_kst(),
        "entry_price": features["price"],
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
        "checkpoints": {},
        "features": features,
        "context": context,
    }
    state["open_events"][event_id] = event
    _journal("forward_event_opened", **event)
    return True


def _update_forward_events(state: dict[str, Any], tickers: dict[str, dict[str, Any]],
                           now: float) -> list[dict[str, Any]]:
    completed = []
    for event_id, event in list(state.get("open_events", {}).items()):
        ticker = tickers.get(str(event.get("market"))) or {}
        price = _safe_float(ticker.get("trade_price"))
        entry = _safe_float(event.get("entry_price"))
        if price <= 0 or entry <= 0:
            if now - _safe_float(event.get("entry_ts")) > EVENT_TTL_SECONDS:
                state["open_events"].pop(event_id, None)
            continue
        ret = (price / entry - 1) * 100
        event["mfe_pct"] = max(_safe_float(event.get("mfe_pct")), ret)
        event["mae_pct"] = min(_safe_float(event.get("mae_pct")), ret)
        age = now - _safe_float(event.get("entry_ts"))
        for hours in (1, 6, 24):
            key = f"ret_{hours}h_pct"
            if age >= hours * 3600 and key not in event["checkpoints"]:
                event["checkpoints"][key] = ret
                _journal("forward_event_checkpoint", id=event_id, cohort=event["cohort"],
                         asset=event["asset"], hours=hours, return_pct=ret,
                         mfe_pct=event["mfe_pct"], mae_pct=event["mae_pct"])
        if age >= 24 * 3600:
            event["exit_ts"] = now
            event["exit_price"] = price
            event["return_24h_pct"] = ret
            _journal("forward_event_closed", **event)
            completed.append(event)
            state["open_events"].pop(event_id, None)
    return completed


def _can_alert(state: dict[str, Any], key: str, now: float, cooldown: float = 3600) -> bool:
    if now - _safe_float(state.get("last_alert_ts", {}).get(key)) < cooldown:
        return False
    state.setdefault("last_alert_ts", {})[key] = now
    return True


def _analyze_wallets(state: dict[str, Any], rows: list[dict[str, Any]],
                     tickers: dict[str, dict[str, Any]], now: float,
                     telegram: bool) -> dict[str, int]:
    counts = {"wallet_rows": len(rows), "lag_watch": 0, "strict_precursors": 0, "pauses": 0}
    baseline_minute = int(now // 60)
    sample_baseline = baseline_minute != int(state.get("last_wallet_baseline_minute") or 0)
    latest = state.setdefault("wallets", {})
    baselines = state.setdefault("block_baselines", {})

    for row in rows:
        key = _wallet_key(row)
        asset = str(row.get("currency") or "").upper()
        if not asset:
            continue
        previous = latest.get(key) or {}
        samples = baselines.setdefault(key, [])
        threshold = wallet_lag_threshold(samples)
        abnormal = _wallet_is_abnormal(row, threshold)
        previous_streak = int(previous.get("lag_streak") or 0)
        lag_streak = previous_streak + 1 if abnormal else 0
        wallet_state = str(row.get("wallet_state") or "unknown").lower()
        previous_state = str(previous.get("wallet_state") or "unknown").lower()
        elapsed = _safe_float(row.get("block_elapsed_minutes"))

        if sample_baseline and wallet_state == "working" and not abnormal:
            samples.append(elapsed)
            baselines[key] = samples[-BASELINE_MAX_SAMPLES:]

        snapshot = {
            "currency": asset,
            "net_type": str(row.get("net_type") or ""),
            "network_name": str(row.get("network_name") or ""),
            "wallet_state": wallet_state,
            "block_state": str(row.get("block_state") or ""),
            "block_height": row.get("block_height"),
            "block_updated_at": row.get("block_updated_at"),
            "block_elapsed_minutes": elapsed,
            "lag_threshold_minutes": threshold,
            "lag_streak": lag_streak,
            "updated_ts": now,
        }
        latest[key] = snapshot
        features = market_features(state, f"KRW-{asset}", now)

        if previous and wallet_state != previous_state:
            _journal("wallet_state_changed", key=key, previous=previous_state,
                     current=wallet_state, snapshot=snapshot, market_features=features or {})
            if wallet_state in ("paused", "withdraw_only", "deposit_only"):
                counts["pauses"] += 1
                _start_forward_event(state, "wallet_transition", asset, features, now, snapshot)
                alert_key = f"state:{key}:{wallet_state}"
                if telegram and _can_alert(state, alert_key, now, 3 * 3600):
                    send_bithumb(
                        "⛔ <b>[빗썸 지갑 상태 변화 · 관찰]</b>\n"
                        f"{escape(asset)} / {escape(snapshot['network_name'])}\n"
                        f"{escape(previous_state)} → <b>{escape(wallet_state)}</b>\n"
                        f"블록 갱신 지연 {elapsed:.0f}분\n"
                        "실매수 아님 · 1/6/24시간 성과 전진기록 시작"
                    )

        if wallet_state == "working" and abnormal and lag_streak >= 2:
            counts["lag_watch"] += 1
            strict = qualifies_precursor(features)
            if strict:
                counts["strict_precursors"] += 1
                opened = _start_forward_event(
                    state, "strict_precursor", asset, features, now,
                    {"wallet": snapshot, "reason": "block_lag_plus_market_flow"},
                )
                alert_key = f"precursor:{key}"
                if opened and telegram and _can_alert(state, alert_key, now, 2 * 3600):
                    send_bithumb(
                        "🟠 <b>[빗썸 지갑중단 사전후보 · PAPER]</b>\n"
                        f"{escape(asset)} / {escape(snapshot['network_name'])}\n"
                        f"블록 갱신 지연 {elapsed:.0f}분 (기준 {threshold:.0f}분)\n"
                        f"15분 {features['ret_15m_pct']:+.2f}% · 거래대금 "
                        f"{features['qvol_ratio']:.1f}배\n"
                        "실주문 없음 · 공지 전 신호인지 전진검증"
                    )
            elif lag_streak == 2:
                _journal("block_lag_watch", key=key, snapshot=snapshot,
                         market_features=features or {}, strict=False)

    if sample_baseline:
        state["last_wallet_baseline_minute"] = baseline_minute
    return counts


def _process_notices(state: dict[str, Any], notices: list[dict[str, Any]],
                     tickers: dict[str, dict[str, Any]], now: float,
                     telegram: bool) -> dict[str, int]:
    seen = set(str(x) for x in state.get("notices_seen") or [])
    bootstrap = not bool(state.get("notice_bootstrapped"))
    new_wallet_notices = 0
    for notice in reversed(notices):
        notice_id = str(notice.get("pc_url") or f"{notice.get('published_at')}:{notice.get('title')}")
        if notice_id in seen:
            continue
        seen.add(notice_id)
        kind = classify_notice(notice)
        assets = notice_assets(notice)
        _journal("notice_observed", notice_id=notice_id, kind=kind, assets=assets, notice=notice)
        if kind not in ("other", "resume"):
            new_wallet_notices += 1
            for asset in assets:
                features = market_features(state, f"KRW-{asset}", now)
                _start_forward_event(state, f"notice_{kind}", asset, features, now, notice)
            if not bootstrap and telegram and _can_alert(state, f"notice:{notice_id}", now, 24 * 3600):
                asset_text = ", ".join(assets) if assets else "종목 본문 확인 필요"
                send_bithumb(
                    "📢 <b>[빗썸 입출금 공지 · 관찰]</b>\n"
                    f"분류: {escape(kind)}\n"
                    f"대상: {escape(asset_text)}\n"
                    f"{escape(str(notice.get('title') or ''))}\n"
                    "공지 직후 자동매수 아님 · 원인별 성과 분리 기록"
                )
    state["notices_seen"] = list(seen)[-500:]
    state["notice_bootstrapped"] = True
    return {"new_wallet_notices": new_wallet_notices}


def _forward_summary() -> dict[str, Any]:
    rows = [row for row in read_jsonl(JOURNAL_FILE) if row.get("event") == "forward_event_closed"]
    by_cohort: dict[str, list[float]] = {}
    for row in rows:
        cohort = str(row.get("cohort") or "unknown")
        value = _safe_float(row.get("return_24h_pct"))
        by_cohort.setdefault(cohort, []).append(value)
    result = {}
    for cohort, values in by_cohort.items():
        result[cohort] = {
            "n": len(values),
            "median_24h_pct": statistics.median(values),
            "mean_24h_pct": statistics.mean(values),
            "win_rate_pct": sum(value > 0 for value in values) / len(values) * 100,
        }
    return result


def build_report(state: dict[str, Any]) -> str:
    scan = state.get("last_scan") or {}
    summary = _forward_summary()
    lines = [
        "🔬 <b>[빗썸 지갑중단 레이더 · PAPER]</b>",
        f"정책: {escape(POLICY)}",
        f"KRW 시장 {int(scan.get('ticker_count') or 0)}개 · 지갑행 {int(scan.get('wallet_rows') or 0)}개",
        f"블록지연 관찰 {int(scan.get('lag_watch') or 0)} · 엄격후보 {int(scan.get('strict_precursors') or 0)}",
        f"열린 전진사건 {len(state.get('open_events') or {})}개",
        f"지갑 API: {escape(str(state.get('auth_status') or 'unknown'))}",
    ]
    if summary:
        lines.append("\n<b>완료된 24시간 표본</b>")
        for cohort, stats in sorted(summary.items()):
            lines.append(
                f"• {escape(cohort)} n={stats['n']} · 중앙 {stats['median_24h_pct']:+.2f}% · "
                f"승률 {stats['win_rate_pct']:.0f}%"
            )
    else:
        lines.append("\n아직 완료된 24시간 전진표본이 없습니다.")
    if state.get("auth_status") == "missing_readonly_keys":
        lines.append(
            "\n⚠️ BITHUMB_ACCESS_KEY / BITHUMB_SECRET_KEY가 없어 "
            "공지·시세만 수집 중입니다. 읽기 전용 키가 있어야 공지 전 블록지연을 관찰합니다."
        )
    lines.append("\n실주문 없음 · 중단 자체는 매수 신호로 사용하지 않음")
    return "\n".join(lines)


def run_once(*, report_now: bool = False, telegram: bool = True) -> dict[str, Any]:
    now = time.time()
    state = _load_state()
    markets = _active_markets(state, now)
    tickers = _fetch_tickers(markets)
    update_market_stats(state, tickers, now)
    completed = _update_forward_events(state, tickers, now)

    notices = _public_get("/v1/notices", {"count": NOTICE_COUNT})
    notice_counts = _process_notices(state, notices, tickers, now, telegram)
    wallet_rows, auth_status = _fetch_wallet_status()
    state["auth_status"] = auth_status
    wallet_counts = {"wallet_rows": 0, "lag_watch": 0, "strict_precursors": 0, "pauses": 0}
    if wallet_rows is not None:
        wallet_counts = _analyze_wallets(state, wallet_rows, tickers, now, telegram)

    state["last_scan"] = {
        "time": now_kst(),
        "ticker_count": len(tickers),
        **wallet_counts,
        **notice_counts,
        "completed_events": len(completed),
        "open_events": len(state.get("open_events") or {}),
    }
    due = now - _safe_float(state.get("last_report_time")) >= REPORT_INTERVAL_SECONDS
    reported = False
    if telegram and (report_now or due):
        reported = send_bithumb(build_report(state))
        state["last_report_time"] = now
        state["last_report_delivered"] = reported
    save_json(STATE_FILE, state)
    return {"ok": True, "account": "bithumb_wallet_radar", **state["last_scan"],
            "auth_status": auth_status, "reported": reported}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--report-now", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not try_acquire(LOCK_NAME):
        return 0
    result: dict[str, Any] = {"ok": True}
    try:
        first = True
        while True:
            started = time.monotonic()
            try:
                result = run_once(
                    report_now=bool(args.report_now and first),
                    telegram=not args.no_telegram,
                )
            except Exception as exc:
                result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                print(f"[bithumb-wallet-radar] cycle failed: {result['error']}", flush=True)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if not args.daemon:
                break
            first = False
            time.sleep(max(1.0, float(args.interval) - (time.monotonic() - started)))
    except KeyboardInterrupt:
        result = {"ok": True, "stopped": True}
    finally:
        release(LOCK_NAME)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
