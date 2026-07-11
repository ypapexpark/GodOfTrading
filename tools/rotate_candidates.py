#!/usr/bin/env python3
"""trade_candidates*.jsonl 로테이션 (디스크 정리).

본선 봇이 쓰지 않는 시간에 실행 권장.
기본: 최근 keep_lines 줄만 남기고 나머지는 archive/ 로 이동.

  python3 tools/rotate_candidates.py --dry-run
  python3 tools/rotate_candidates.py --keep 5000
"""
from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive"
TARGETS = [
    ROOT / "trade_candidates.jsonl",
    ROOT / "trade_candidates_binance.jsonl",
]


def rotate(path: Path, keep: int, dry: bool) -> None:
    if not path.exists():
        print(f"skip missing {path.name}")
        return
    size_mb = path.stat().st_size / (1024 * 1024)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    n = len(lines)
    if n <= keep:
        print(f"ok {path.name}: {n} lines ({size_mb:.1f}MB) <= keep {keep}")
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE / f"{path.stem}_{stamp}{path.suffix}"
    print(f"{path.name}: {n} lines {size_mb:.1f}MB → archive {n - keep}, keep {keep}")
    if dry:
        print(f"  dry-run: would move full file to {dest.name} then rewrite tail")
        return
    shutil.copy2(path, dest)
    path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    print(f"  archived → {dest}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", type=int, default=8000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    for p in TARGETS:
        rotate(p, args.keep, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
