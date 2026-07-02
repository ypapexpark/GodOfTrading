"""Project-level reminder messages.

자동매매 봇이 매일 실행되는 특성을 이용해, 당장 개발하지 않는 항목도
정해진 날짜 이후 한 번 알려줄 수 있게 모아둔 모듈이다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import (
    KIS_API_EARLY_REVIEW_ENABLED,
    KIS_API_REVIEW_REMINDER_DATE,
    KIS_API_REVIEW_REMINDER_ENABLED,
)

KST = timezone(timedelta(hours=9))
STATE_FILE = Path(__file__).parent / "project_reminders_state.json"


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def maybe_send_kis_api_review_reminder(send_func, *, dry_run: bool = False,
                                       force: bool = False) -> bool | None:
    """한국투자증권 KIS Open API 전환 검토 알림을 1회 발송한다."""
    if not KIS_API_REVIEW_REMINDER_ENABLED and not force:
        return None

    state = _load_state()
    if state.get("kis_api_review_sent") and not force:
        return None

    today = datetime.now(KST).date()
    try:
        review_date = datetime.strptime(KIS_API_REVIEW_REMINDER_DATE, "%Y-%m-%d").date()
    except ValueError:
        print("[KIS API 알림] KIS_API_REVIEW_REMINDER_DATE 형식은 YYYY-MM-DD 이어야 합니다.")
        return None

    if today < review_date and not force:
        print(f"[KIS API 알림] {review_date.isoformat()} 이후 발송 예정")
        return None

    msg = "\n".join([
        "🔔 <b>[한투증권 KIS API 전환 검토 알림]</b>",
        "국내주식 KOSPI/KOSDAQ MA200 스크리너가 무료 데이터 소스로 운영 중입니다.",
        "종목 선별 결과가 유용하게 쌓이고 있다면, 이제 정식 시세/종목 데이터 소스로 전환을 검토할 타이밍입니다.",
        "",
        "검토 항목: KIS Open API 앱키/시크릿 발급, 토큰 갱신, 일봉/종목마스터 조회, 호출 제한",
        "포털: https://apiportal.koreainvestment.com/",
    ])

    if dry_run:
        print(msg)
        ok = True
    else:
        ok = bool(send_func(msg))

    if ok and not force:
        state["kis_api_review_sent"] = True
        state["kis_api_review_sent_at"] = datetime.now(KST).isoformat()
        _save_state(state)
        print("[KIS API 알림] 발송 완료")
    elif not ok:
        print("[KIS API 알림] 발송 실패")
    return ok


def maybe_send_kis_api_early_warning(send_func, *, reason: str,
                                     details: str = "",
                                     dry_run: bool = False,
                                     force: bool = False) -> bool | None:
    """무료 국내주식 데이터 소스 이상 시 KIS Open API 조기 전환 알림을 보낸다.

    정기 알림은 2026-07-14 같은 날짜 기반 확인이고, 이 함수는 그 전에
    실제 운영 문제가 감지됐을 때만 1회 발송하는 비상성 알림이다.
    """
    if not KIS_API_EARLY_REVIEW_ENABLED and not force:
        return None

    state = _load_state()
    if state.get("kis_api_early_warning_sent") and not force:
        return None

    lines = [
        "⚠️ <b>[한투증권 KIS API 조기 전환 검토]</b>",
        "국내주식 MA200 스크리너의 무료 데이터 소스에서 이상 징후가 감지됐습니다.",
        "",
        f"감지 사유: <b>{reason}</b>",
    ]
    if details:
        lines.append(f"상세: {details}")
    lines += [
        "",
        "권장: KIS Open API 기반 종목마스터/일봉 조회로 전환 가능성을 먼저 검토하세요.",
        "포털: https://apiportal.koreainvestment.com/",
    ]
    msg = "\n".join(lines)

    if dry_run:
        print(msg)
        ok = True
    else:
        ok = bool(send_func(msg))

    if ok and not force:
        state["kis_api_early_warning_sent"] = True
        state["kis_api_early_warning_sent_at"] = datetime.now(KST).isoformat()
        state["kis_api_early_warning_reason"] = reason
        state["kis_api_early_warning_details"] = details
        _save_state(state)
        print("[KIS API 조기알림] 발송 완료")
    elif not ok:
        print("[KIS API 조기알림] 발송 실패")
    return ok
