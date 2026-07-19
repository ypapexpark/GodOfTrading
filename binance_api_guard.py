"""Cross-process Binance rate-limit/temporary-ban coordination."""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path

from service_status import STATUS_DIR, read_status, write_status

STATUS_NAME = "binance_api_backoff"
_BAN_UNTIL_RE = re.compile(r"banned until\s+(\d{10,})", re.IGNORECASE)
_WEIGHT_FILE = STATUS_DIR / "binance_api_weight_bucket.json"
_WEIGHT_LOCK = STATUS_DIR / "binance_api_weight_bucket.lock"
_SHARED_WEIGHT_PER_MINUTE = max(
    300.0,
    float(os.getenv("BINANCE_SHARED_WEIGHT_PER_MINUTE", "1600") or 1600),
)
_SHARED_WEIGHT_BURST = max(
    50.0,
    min(
        float(os.getenv("BINANCE_SHARED_WEIGHT_BURST", "400") or 400),
        _SHARED_WEIGHT_PER_MINUTE,
    ),
)


def record_api_error(error: Exception | str) -> float:
    """Persist a 429/418 backoff and return its absolute epoch seconds."""
    message = str(error)
    lowered = message.lower()
    if not (
        "too many requests" in lowered
        or "way too many requests" in lowered
        or " 429 " in f" {lowered} "
        or " 418 " in f" {lowered} "
        or '"code":-1003' in lowered
    ):
        return 0.0
    match = _BAN_UNTIL_RE.search(message)
    if match:
        retry_at = int(match.group(1)) / 1000.0
    else:
        retry_at = time.time() + 65.0
    previous = read_status(STATUS_NAME)
    retry_at = max(retry_at, float(previous.get("retry_at") or 0.0))
    write_status(
        STATUS_NAME,
        {
            "ok": False,
            "retry_at": retry_at,
            "reason": message[:500],
        },
    )
    return retry_at


def api_backoff_status() -> dict:
    status = read_status(STATUS_NAME)
    retry_at = float(status.get("retry_at") or 0.0)
    remaining = max(retry_at - time.time(), 0.0)
    return {
        **status,
        "retry_at": retry_at,
        "remaining_seconds": remaining,
        "blocked": remaining > 0,
    }


def api_backoff_remaining() -> float:
    return float(api_backoff_status()["remaining_seconds"])


def reserve_api_weight(weight: float) -> None:
    """Cross-process token bucket for all Binance Futures public requests.

    The 1,600/minute budget intentionally leaves headroom for private position,
    balance and order endpoints. A 400-weight burst ceiling prevents hundreds
    of concurrent backfill workers from exhausting the IP limit at once.
    """
    wanted = max(float(weight), 0.0)
    if wanted <= 0:
        return
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    refill_per_second = _SHARED_WEIGHT_PER_MINUTE / 60.0
    while True:
        remaining = api_backoff_remaining()
        if remaining > 0:
            raise RuntimeError(f"Binance shared API backoff {remaining:.1f}s")
        wait_for = 0.05
        with open(_WEIGHT_LOCK, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            now = time.time()
            try:
                state = json.loads(_WEIGHT_FILE.read_text(encoding="utf-8"))
            except Exception:
                state = {}
            previous_ts = float(state.get("updated_at") or now)
            previous_tokens = float(
                state.get("tokens")
                if state.get("tokens") is not None else _SHARED_WEIGHT_BURST
            )
            tokens = min(
                _SHARED_WEIGHT_BURST,
                previous_tokens + max(now - previous_ts, 0.0) * refill_per_second,
            )
            allowed = tokens >= wanted
            if allowed:
                tokens -= wanted
            else:
                wait_for = (wanted - tokens) / refill_per_second
            payload = {
                "updated_at": now,
                "tokens": tokens,
                "rate_per_minute": _SHARED_WEIGHT_PER_MINUTE,
                "burst": _SHARED_WEIGHT_BURST,
                "last_pid": os.getpid(),
            }
            tmp = _WEIGHT_FILE.with_name(
                f".{_WEIGHT_FILE.name}.{os.getpid()}.tmp"
            )
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, _WEIGHT_FILE)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        if allowed:
            return
        time.sleep(max(0.02, min(wait_for, 1.0)))
