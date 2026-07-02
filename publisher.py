"""텔레그램 발송 라우팅.

채널 역할을 코드에서 명확히 고정한다.

1. TRADE_*  → 갓오브트레이딩
   실제 주문이 체결된 뒤의 진입 알림, 상세 진입 분석, 청산 분석,
   일/누적 결산, 빗썸/국내주식 스크리너 결과처럼 "실제 매매 운영 기록"을 보낸다.

2. SIGNAL_* → 바이빗트레이딩파크
   아직 실제 주문이 체결되지 않은 시그널 근거, 황금 진입 발동,
   주문 실패 진단처럼 "판단 근거와 실행 전/실패 이벤트"를 보낸다.

예전 TELEGRAM_*, REVIEW_*, POSITION_ANALYSIS_* 키는 더 이상 사용하지 않는다.
새 라우팅을 추가할 때는 먼저 이 파일의 채널 역할을 갱신한 뒤 env 키를 늘린다.
"""
import os
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_optional_route_reported = set()


def _post(token: str, chat_id: str, text: str) -> bool:
    """Telegram Bot API로 HTML 메시지를 보낸다.

    여기서는 토큰/채팅방 값의 존재 여부를 판단하지 않는다. 어떤 채널로 보낼지는
    `_send_env()`에서 env 키 단위로 결정하고, 이 함수는 전송 성공/실패만 책임진다.
    """
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        result = resp.json()
        if not result.get("ok"):
            print(f"[Telegram] API 오류: {result.get('description', result)}")
        return result.get("ok", False)
    except Exception as e:
        print(f"[Telegram] 전송 실패: {e}")
        return False


def _send_env(token_key: str, chat_key: str, text: str,
              label: str, *, required: bool = True) -> bool:
    """env 키 한 쌍(token/chat_id)을 읽어서 지정 채널로 발송한다.

    required=False인 시그널 채널은 설정이 없어도 매매 봇 전체가 멈추면 안 된다.
    반대로 TRADE_*는 실제 매매 기록 채널이므로 누락 시 반드시 콘솔에 크게 남긴다.
    """
    token   = os.getenv(token_key)
    chat_id = os.getenv(chat_key)
    if not token or not chat_id:
        if required:
            print(f"[{label}] 토큰/chat_id 없음 — .env에 {token_key}, {chat_key} 입력 필요")
        else:
            key = (token_key, chat_key)
            if key not in _optional_route_reported:
                print(f"[{label}] 라우팅 미설정 — 텔레그램 발송 스킵")
                _optional_route_reported.add(key)
        return False
    if "여기에" in token or "여기에" in chat_id:
        print(f"[{label}] 토큰 미입력 상태 — 봇파더 토큰 받으면 .env 업데이트")
        return False
    return _post(token, chat_id, text)


def send_trade(text: str) -> bool:
    """God of Trading 방 — 실제 체결/진입/청산/결산/시장 스크리닝 결과 전용."""
    return _send_env("TRADE_BOT_TOKEN", "TRADE_CHAT_ID", text, "매매내역봇")


def send_position_analysis(text: str) -> bool:
    """실제 포지션 진입/청산 상세 분석도 God of Trading 방으로 보낸다."""
    return send_trade(text)


def send_market_screening(text: str) -> bool:
    """God of Trading 방 — 빗썸/국내주식 종목 서치 결과."""
    return send_trade(text)


def send_bithumb(text: str) -> bool:
    """하위 호환용: 기존 빗썸 스크리너 라우팅 이름."""
    return send_market_screening(text)


def send_signal(text: str) -> bool:
    """바이빗 트레이딩 파크 — 모든 시그널 근거 전용."""
    return _send_env("SIGNAL_BOT_TOKEN", "SIGNAL_CHAT_ID", text,
                     "시그널봇", required=False)


def send(text: str) -> bool:
    """하위 호환용: 실제 매매내역 라우팅."""
    return send_trade(text)


def send_review(text: str) -> bool:
    """복기/결산도 매매내역 라우팅으로 보낸다."""
    return send_trade(text)
