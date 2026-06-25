"""텔레그램 발송 — 메인봇(신호/매매) + 복기봇(분석 브리핑) 분리."""
import os
import requests


def _post(token: str, chat_id: str, text: str) -> bool:
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


def send(text: str) -> bool:
    """메인봇 — 신호 알림 / 자동매매 결과."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[Telegram] 메인봇 토큰/chat_id 없음")
        return False
    return _post(token, chat_id, text)


def send_review(text: str) -> bool:
    """복기봇 — 거래 복기 / 패인분석 브리핑 전용."""
    token   = os.getenv("REVIEW_BOT_TOKEN")
    chat_id = os.getenv("REVIEW_CHAT_ID")
    if not token or not chat_id:
        print("[복기봇] 토큰/chat_id 미설정 — .env에 REVIEW_BOT_TOKEN, REVIEW_CHAT_ID 입력 필요")
        return False
    if "여기에" in token or "여기에" in chat_id:
        print("[복기봇] 토큰 미입력 상태 — 봇파더 토큰 받으면 .env 업데이트")
        return False
    return _post(token, chat_id, text)
