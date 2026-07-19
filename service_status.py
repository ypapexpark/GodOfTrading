"""Small atomic heartbeat files shared by local long-running services."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATUS_DIR = BASE_DIR / ".runtime"


def status_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return STATUS_DIR / f"{safe}.json"


def write_status(name: str, payload: dict) -> dict:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(payload or {})
    data["service"] = name
    data["heartbeat_ts"] = time.time()
    data["pid"] = os.getpid()
    path = status_path(name)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)
    return data


def read_status(name: str) -> dict:
    try:
        payload = json.loads(status_path(name).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def heartbeat_is_fresh(
    name: str,
    max_age_seconds: float,
    *,
    require_ok: bool = True,
) -> bool:
    status = read_status(name)
    age = time.time() - float(status.get("heartbeat_ts") or 0.0)
    if age < 0 or age > max(float(max_age_seconds), 0.0):
        return False
    return bool(status.get("ok", False)) if require_ok else True
