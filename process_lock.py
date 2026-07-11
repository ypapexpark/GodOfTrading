"""프로세스 단일 실행 잠금 (LaunchAgent 겹침 방지).

macOS LaunchAgent 의 StartInterval 은 "이전 실행 종료 후 N초"가 아니라
"N초마다 기동 시도"라서, 스캔이 주기보다 길면 main.py 가 여러 개 겹친다.

같은 벤뉴·같은 모드의 두 번째 인스턴스는 즉시 종료(exit 0)한다.
다른 벤뉴(Bybit/Binance)나 모드(full/fast)는 서로 막지 않는다.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional, TextIO

BASE_DIR = Path(__file__).parent
LOCK_DIR = BASE_DIR / ".locks"

# 프로세스 수명 동안 FD 유지 (close 시 flock 해제)
_held: dict[str, TextIO] = {}


def lock_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return LOCK_DIR / f"{safe}.lock"


def try_acquire(name: str) -> bool:
    """비차단 exclusive lock. 성공 시 True, 이미 점유 중이면 False."""
    if name in _held:
        return True
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = lock_path(name)
    f = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        # 점유자 정보 (디버그용)
        holder = ""
        try:
            f.seek(0)
            holder = (f.read() or "").strip().splitlines()[:1]
            holder = holder[0] if holder else "?"
        except Exception:
            holder = "?"
        f.close()
        print(
            f"[lock] skip — 이미 실행 중: {name} "
            f"(holder_pid≈{holder}, path={path.name})"
        )
        return False

    try:
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}\n")
        f.flush()
    except Exception:
        pass
    _held[name] = f
    return True


def release(name: str) -> None:
    f = _held.pop(name, None)
    if f is None:
        return
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        f.close()
    except Exception:
        pass


def main_run_lock_name(*, venue: str, fast: bool, special: str = "") -> str:
    """main.py 인스턴스 키.

    examples:
      main_bybit_full
      main_bybit_fast
      main_binance_full
      main_bithumb_only
    """
    if special:
        return f"main_{venue}_{special}"
    mode = "fast" if fast else "full"
    return f"main_{venue}_{mode}"


def holder_pid(name: str) -> Optional[int]:
    path = lock_path(name)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip().splitlines()
        return int(raw[0]) if raw else None
    except Exception:
        return None
