#!/usr/bin/env python3
from __future__ import annotations

"""
PolyInsight 모멘텀 — LIVE 스켈레톤 (초안, 기본 비활성).

■ 이게 뭔가요?
  paper 봇이 검증(졸업)된 뒤에 실주문을 넣을 **자리만 잡아 둔 파일**입니다.
  지금은 주문 코드가 연결되어 있지 않고, 기본값으로 LIVE가 꺼져 있습니다.
  "나중에 실매매 붙일 때 여기다 채우면 된다"는 설계도 역할입니다.

■ paper 와 관계
  - paper: polymarket_insight_paper_bot.py  (지금 돌아가는 것)
  - live : 이 파일                         (졸업 후에만 의미 있음)
  - 고래 카피 LIVE(polymarket_whale_live_bot.py) 와 지갑·한도·state 를 공유하지 않음

■ 졸업 게이트 (paper 저널 기준, insights 와 동일)
  정산 ≥30 · WR≥55% · PnL>0 · ≥7일

■ 켜는 방법 (졸업 후, 사람 승인 후에만)
  1. paper 리포트에 [졸업후보] 가 뜬 것을 확인
  2. .env 에 POLYMARKET_INSIGHT_LIVE=true  (기본 false)
  3. 별도 초소액 bankroll / 전용 funder 설정
  4. 이 파일의 TODO 구간에 CLOB 주문 연결 (whale live 패턴 참고)
  5. LaunchAgent 는 paper 와 별도로 등록

실행:
  python3 polymarket_insight_live_bot.py --status
  python3 polymarket_insight_live_bot.py --json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from polymarket_insight_insights import (
    GRAD_MIN_DAYS,
    GRAD_MIN_PNL,
    GRAD_MIN_SETTLED,
    GRAD_MIN_WR,
)

ROOT = Path(__file__).parent
PAPER_JOURNAL = ROOT / "polymarket_insight_paper_journal.jsonl"
LIVE_STATE = ROOT / "polymarket_insight_live_state.json"  # 생성 예약 (아직 미사용)
LIVE_JOURNAL = ROOT / "polymarket_insight_live_journal.jsonl"

load_dotenv(ROOT / ".env")

from bot_util import (  # noqa: E402
    KST,
    env_bool as _env_bool,
    read_jsonl as _read_jsonl,
)


def paper_graduation() -> dict[str, Any]:
    """paper 저널로 졸업 여부만 계산. 실주문과 무관."""
    settled = [r for r in _read_jsonl(PAPER_JOURNAL) if r.get("event") == "settled"]
    n = len(settled)
    wins = sum(1 for r in settled if r.get("won"))
    wr = wins / n if n else 0.0
    pnl = sum(float(r.get("pnl_usd") or 0) for r in settled)
    first_ts = min((float(r.get("opened_ts") or 0) for r in settled), default=0.0)
    last_ts = max(
        (float(r.get("settled_ts") or r.get("opened_ts") or 0) for r in settled),
        default=0.0,
    )
    span_days = (last_ts - first_ts) / 86400.0 if first_ts and last_ts else 0.0
    ready = (
        n >= GRAD_MIN_SETTLED
        and wr >= GRAD_MIN_WR
        and pnl >= GRAD_MIN_PNL
        and span_days >= GRAD_MIN_DAYS
    )
    return {
        "ready": ready,
        "settled_n": n,
        "win_rate": round(wr, 4),
        "pnl_usd": round(pnl, 2),
        "span_days": round(span_days, 2),
        "need": {
            "min_settled": GRAD_MIN_SETTLED,
            "min_wr": GRAD_MIN_WR,
            "min_pnl": GRAD_MIN_PNL,
            "min_days": GRAD_MIN_DAYS,
        },
    }


def status() -> dict[str, Any]:
    live_flag = _env_bool("POLYMARKET_INSIGHT_LIVE", False)
    grad = paper_graduation()
    return {
        "module": "polymarket_insight_live_bot",
        "role": "SKELETON_ONLY",
        "live_env_flag": live_flag,
        "orders_implemented": False,  # TODO: CLOB 연결 후 True
        "account_tag": "insight_live",
        "separate_from_whale": True,
        "paper_graduation": grad,
        "would_allow_live": bool(live_flag and grad["ready"]),
        "message": _human_message(live_flag, grad),
        "checked_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }


def _human_message(live_flag: bool, grad: dict[str, Any]) -> str:
    if not grad["ready"]:
        return (
            f"아직 졸업 전 — paper 정산 {grad['settled_n']}/{grad['need']['min_settled']}건, "
            f"WR {grad['win_rate']:.0%}, PnL ${grad['pnl_usd']:+.1f}, "
            f"{grad['span_days']:.1f}일. paper 봇만 계속 돌리세요."
        )
    if not live_flag:
        return (
            "paper 졸업 조건 충족. 그래도 LIVE 플래그가 꺼져 있음 "
            "(POLYMARKET_INSIGHT_LIVE=true 필요). 주문 구현도 아직 없음."
        )
    return (
        "플래그+졸업 통과 — 하지만 이 스켈레톤에는 실주문 코드가 없음. "
        "whale live / clob_exec 패턴으로 TODO 구간을 채운 뒤에만 사용."
    )


def run_once() -> dict[str, Any]:
    """주기 실행용 자리. 현재는 상태 확인만 하고 주문 0건."""
    st = status()
    # --- TODO (졸업 후 구현) ---
    # 1. paper 와 동일 시그널 소스 (PolyInsight analytics)
    # 2. 리스크: 초소액 bankroll, max_open, daily loss, 고래 지갑과 funder 분리
    # 3. polymarket_clob_exec 로 지정 outcome buy
    # 4. live journal/state 에 기록 + 4h 리포트
    # 5. LIVE=false 또는 graduation 실패 시 즉시 return
    if not st["would_allow_live"] or not st["orders_implemented"]:
        st["opened"] = 0
        st["settled"] = 0
        return st

    # 여기에만 실주문 루프를 추가할 것
    raise NotImplementedError("insight live orders not implemented — skeleton only")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true", help="졸업/LIVE 상태만 출력")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = status() if args.status else run_once()
    if args.json or args.status:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[InsightLiveSkeleton] {result.get('message')}")
        print(json.dumps({k: result[k] for k in (
            "live_env_flag", "orders_implemented", "would_allow_live", "paper_graduation"
        ) if k in result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
