"""텔레그램 발송 — 매매 전용봇 단일 라우팅."""
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
    """매매 전용봇 — 신호 / 자동매매 / 차단 / 복기 전체."""
    token   = os.getenv("TRADE_BOT_TOKEN")
    chat_id = os.getenv("TRADE_CHAT_ID")
    if not token or not chat_id:
        print("[매매봇] 토큰/chat_id 없음 — .env에 TRADE_BOT_TOKEN, TRADE_CHAT_ID 입력 필요")
        return False
    if "여기에" in token or "여기에" in chat_id:
        print("[매매봇] 토큰 미입력 상태 — 봇파더 토큰 받으면 .env 업데이트")
        return False
    return _post(token, chat_id, text)


def send_review(text: str) -> bool:
    """복기/결산도 같은 매매 전용봇으로 보낸다."""
    return send(text)
