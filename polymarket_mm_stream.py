#!/usr/bin/env python3
"""선정된 Polymarket MM 시장의 공개 WebSocket L2/체결 수집기.

실주문 권한이나 개인키를 사용하지 않는다. market channel의 book, price_change,
best_bid_ask, last_trade_price 이벤트를 받아 paper 엔진용 snapshot과 검증용 raw
JSONL을 만든다.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from websocket import WebSocketTimeoutException, create_connection

from bot_util import append_jsonl, load_json, now_kst
from process_lock import release, try_acquire

ROOT = Path(__file__).parent
BOT_STATE_FILE = ROOT / "polymarket_mm_state.json"
STREAM_STATE_FILE = ROOT / "polymarket_mm_stream_state.json"
EVENT_JOURNAL_FILE = ROOT / "polymarket_mm_stream_events.jsonl"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_TRADES = 10_000
MAX_JOURNAL_BYTES = 200 * 1024 * 1024


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def selected_tokens() -> list[str]:
    state = load_json(BOT_STATE_FILE, default={})
    if not isinstance(state, dict):
        return []
    selected = set(str(value) for value in state.get("selected_conditions") or [])
    tokens = []
    for condition, market in (state.get("markets") or {}).items():
        has_inventory = any(_float(value) > 0 for value in market.get("inventory") or [])
        if str(condition) not in selected and not has_inventory:
            continue
        tokens.extend(str(value) for value in market.get("tokens") or [] if value)
    return list(dict.fromkeys(tokens))


def new_stream_state(previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous if isinstance(previous, dict) else {}
    return {
        "version": 1,
        "connected": False,
        "tokens": [],
        "books": {},
        "trades": list(previous.get("trades") or [])[-MAX_TRADES:],
        "event_count": int(previous.get("event_count") or 0),
        "event_types": dict(previous.get("event_types") or {}),
        "reconnects": int(previous.get("reconnects") or 0),
        "started_at": previous.get("started_at") or now_kst(),
        "started_ts": _float(previous.get("started_ts"), time.time()),
        "heartbeat_ts": time.time(),
        "updated_at": now_kst(),
        "last_error": "",
    }


def _atomic_save(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _rotate_journal_if_needed() -> None:
    try:
        if EVENT_JOURNAL_FILE.stat().st_size < MAX_JOURNAL_BYTES:
            return
    except FileNotFoundError:
        return
    backup = EVENT_JOURNAL_FILE.with_suffix(".jsonl.1")
    if backup.exists():
        backup.unlink()
    EVENT_JOURNAL_FILE.replace(backup)


def _event_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def parse_message(raw: str | bytes) -> list[dict[str, Any]]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw or raw in {"PING", "PONG"}:
        return []
    try:
        return _event_rows(json.loads(raw))
    except (TypeError, json.JSONDecodeError):
        return []


def journal_event(event: dict[str, Any]) -> dict[str, Any]:
    """분석용 핵심은 보존하되 큰 snapshot/설명을 잘라 보관 시간을 늘린다."""
    event_type = str(event.get("event_type") or "unknown")
    if event_type == "book":
        return {
            key: event.get(key) for key in (
                "event_type", "asset_id", "market", "timestamp", "hash"
            )
        } | {
            "bids": list(event.get("bids") or [])[:10],
            "asks": list(event.get("asks") or [])[:10],
        }
    if event_type in {"new_market", "market_resolved"}:
        return {key: event.get(key) for key in (
            "event_type", "id", "market", "condition_id", "question", "timestamp",
            "assets_ids", "clob_token_ids", "outcomes", "winning_asset_id",
            "winning_outcome", "active", "fees_enabled",
        ) if key in event}
    return event


def journal_sample_key(event: dict[str, Any]) -> tuple[str, float]:
    """(sampling key, minimum seconds). 체결은 전부, 고빈도 호가는 표본 저장."""
    event_type = str(event.get("event_type") or "unknown")
    if event_type == "last_trade_price":
        return "", 0.0
    if event_type == "book":
        return f"book:{event.get('asset_id') or event.get('market')}", 2.0
    if event_type == "price_change":
        return f"price_change:{event.get('market')}", 10.0
    if event_type == "best_bid_ask":
        return f"best_bid_ask:{event.get('asset_id') or event.get('market')}", 10.0
    return "", 0.0


def _update_level(book: dict[str, Any], side: str, price: float, size: float) -> None:
    key = "bids" if side == "BUY" else "asks"
    levels = {
        _float(row.get("price")): _float(row.get("size"))
        for row in book.get(key) or []
        if isinstance(row, dict) and _float(row.get("price")) > 0
    }
    if size <= 0:
        levels.pop(price, None)
    else:
        levels[price] = size
    ordered = sorted(levels.items(), reverse=key == "bids")
    book[key] = [{"price": str(p), "size": str(s)} for p, s in ordered]


def apply_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    event_type = str(event.get("event_type") or "unknown")
    state["event_count"] = int(state.get("event_count") or 0) + 1
    counts = defaultdict(int, state.get("event_types") or {})
    counts[event_type] += 1
    state["event_types"] = dict(counts)
    state["last_event_type"] = event_type
    state["last_event_at"] = now_kst()
    state["last_event_ts"] = time.time()

    if event_type == "book":
        token = str(event.get("asset_id") or "")
        if token:
            state.setdefault("books", {})[token] = dict(event)
    elif event_type == "price_change":
        for change in event.get("price_changes") or []:
            if not isinstance(change, dict):
                continue
            token = str(change.get("asset_id") or "")
            book = (state.get("books") or {}).get(token)
            if not isinstance(book, dict):
                continue
            side = str(change.get("side") or "").upper()
            if side in {"BUY", "SELL"}:
                _update_level(book, side, _float(change.get("price")), _float(change.get("size")))
                book["timestamp"] = event.get("timestamp")
                book["hash"] = change.get("hash") or book.get("hash")
    elif event_type == "tick_size_change":
        token = str(event.get("asset_id") or "")
        book = (state.get("books") or {}).get(token)
        if isinstance(book, dict):
            book["tick_size"] = event.get("new_tick_size")
    elif event_type == "last_trade_price":
        token = str(event.get("asset_id") or "")
        timestamp = str(event.get("timestamp") or "")
        trade = {
            "transactionHash": "ws:" + ":".join((
                str(event.get("market") or ""), token, timestamp,
                str(event.get("side") or ""), str(event.get("price") or ""),
                str(event.get("size") or ""),
            )),
            "asset": token,
            "side": str(event.get("side") or "").upper(),
            "price": _float(event.get("price")),
            "size": _float(event.get("size")),
            "timestamp": event.get("timestamp"),
            "proxyWallet": "",
            "source": "websocket",
        }
        trades = list(state.get("trades") or [])
        trades.append(trade)
        state["trades"] = trades[-MAX_TRADES:]


def _save_heartbeat(state: dict[str, Any]) -> None:
    state["heartbeat_ts"] = time.time()
    state["updated_at"] = now_kst()
    _atomic_save(STREAM_STATE_FILE, state)


def run_stream(*, duration_seconds: float = 0.0) -> dict[str, Any]:
    if not try_acquire("polymarket_mm_stream"):
        return {"ok": False, "skipped": "already_running"}
    started = time.monotonic()
    previous = load_json(STREAM_STATE_FILE, default={})
    state = new_stream_state(previous)
    backoff = 1.0
    try:
        while not duration_seconds or time.monotonic() - started < duration_seconds:
            tokens = selected_tokens()
            if not tokens:
                state.update({"connected": False, "tokens": [], "books": {}})
                _save_heartbeat(state)
                time.sleep(1)
                continue
            ws = None
            try:
                ws = create_connection(WS_URL, timeout=5)
                ws.settimeout(1)
                ws.send(json.dumps({
                    "assets_ids": tokens,
                    "type": "market",
                    "custom_feature_enabled": True,
                }))
                state.update({
                    "connected": True,
                    "tokens": tokens,
                    "books": {},
                    "connected_at": now_kst(),
                    "last_error": "",
                })
                _save_heartbeat(state)
                last_save = time.monotonic()
                last_ping = time.monotonic()
                last_journal: dict[str, float] = {}
                backoff = 1.0
                while not duration_seconds or time.monotonic() - started < duration_seconds:
                    current_tokens = selected_tokens()
                    if current_tokens != tokens:
                        break
                    try:
                        raw = ws.recv()
                    except WebSocketTimeoutException:
                        raw = ""
                    for event in parse_message(raw):
                        apply_event(state, event)
                        sample_key, minimum_interval = journal_sample_key(event)
                        now_wall = time.time()
                        if (
                            not sample_key
                            or now_wall - last_journal.get(sample_key, 0.0) >= minimum_interval
                        ):
                            _rotate_journal_if_needed()
                            append_jsonl(EVENT_JOURNAL_FILE, journal_event(event))
                            if sample_key:
                                last_journal[sample_key] = now_wall
                    now_mono = time.monotonic()
                    if now_mono - last_ping >= 10:
                        ws.ping()
                        last_ping = now_mono
                    if now_mono - last_save >= 0.25:
                        _save_heartbeat(state)
                        last_save = now_mono
            except KeyboardInterrupt:
                break
            except Exception as exc:
                state["last_error"] = str(exc)[:500]
                state["reconnects"] = int(state.get("reconnects") or 0) + 1
            finally:
                state["connected"] = False
                _save_heartbeat(state)
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if duration_seconds and time.monotonic() - started >= duration_seconds:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
    finally:
        state["connected"] = False
        _save_heartbeat(state)
        release("polymarket_mm_stream")
    return {
        "ok": True,
        "event_count": int(state.get("event_count") or 0),
        "event_types": state.get("event_types") or {},
        "reconnects": int(state.get("reconnects") or 0),
        "last_error": state.get("last_error") or "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=0.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_stream(duration_seconds=max(args.seconds, 0.0))
    print(json.dumps(result, ensure_ascii=False) if args.json else result)
    return 0 if result.get("ok") or result.get("skipped") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
