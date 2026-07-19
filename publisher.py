"""텔레그램 발송 라우팅.

채널 역할을 코드에서 명확히 고정한다.

1. TRADE_*  → 갓오브트레이딩
   실제 주문이 체결된 뒤의 진입 알림, 상세 진입 분석, 청산 분석,
   일/누적 결산, 국내주식 스크리너 결과처럼 "실제 매매 운영 기록"을 보낸다.

2. SIGNAL_* → 바이빗트레이딩파크
   아직 실제 주문이 체결되지 않은 시그널 근거, 황금 진입 발동,
   주문 실패 진단처럼 "판단 근거와 실행 전/실패 이벤트"를 보낸다.

3. BITHUMB_* → 빗썸 알리미
   빗썸 지갑중단 레이더, 입출금 공지, 전진검증 결과, 빗썸 전용
   스크리너를 보낸다. 미설정이어도 다른 채널로 우회하지 않는다.

예전 TELEGRAM_*, REVIEW_*, POSITION_ANALYSIS_* 키는 더 이상 사용하지 않는다.
새 라우팅을 추가할 때는 먼저 이 파일의 채널 역할을 갱신한 뒤 env 키를 늘린다.
"""
import hashlib
import json
import os
import time
import fcntl
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_optional_route_reported = set()
_backoff_route_reported = set()
_BACKOFF_PATH = Path(
    os.getenv("TELEGRAM_BACKOFF_PATH", "/tmp/godoftrading_telegram_backoff.json")
)
_DEDUPE_PATH = Path(
    os.getenv("TELEGRAM_DEDUPE_PATH", "/tmp/godoftrading_telegram_dedupe.json")
)


def _token_key(token: str) -> str:
    """Persist a stable route key without ever writing the bot token to disk."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]


def _safe_error(exc: Exception, *secrets: str) -> str:
    """requests 예외 URL에 포함될 수 있는 bot token을 로그에서 제거한다."""
    message = str(exc)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<redacted>")
    return message[:500]


def _read_backoffs() -> dict[str, float]:
    try:
        raw = json.loads(_BACKOFF_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): float(v) for k, v in raw.items()}
    except (FileNotFoundError, ValueError, TypeError, OSError):
        pass
    return {}


def _backoff_remaining(token: str) -> int:
    until = _read_backoffs().get(_token_key(token), 0.0)
    return max(0, int(until - time.time() + 0.999))


def _set_backoff(token: str, retry_after: int) -> None:
    """Share Telegram flood-control state across the 3m/5m bot processes."""
    seconds = max(1, int(retry_after))
    _BACKOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{_BACKOFF_PATH}.lock")
    try:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            state = _read_backoffs()
            key = _token_key(token)
            state[key] = max(state.get(key, 0.0), time.time() + seconds)
            tmp_path = Path(f"{_BACKOFF_PATH}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp_path, _BACKOFF_PATH)
    except OSError as exc:
        # Telegram failure handling must never stop the trading loop.
        print(f"[Telegram] 쿨다운 상태 저장 실패: {exc}")


def _claim_dedupe(key: str, ttl_seconds: int) -> bool:
    """프로세스 간 중복 키를 원자적으로 선점한다. True면 이번 호출이 발송 담당."""
    now = time.time()
    ttl = max(1, int(ttl_seconds))
    _DEDUPE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{_DEDUPE_PATH}.lock")
    try:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                raw = json.loads(_DEDUPE_PATH.read_text(encoding="utf-8"))
                state = raw if isinstance(raw, dict) else {}
            except (FileNotFoundError, ValueError, TypeError, OSError):
                state = {}
            state = {
                str(k): float(v) for k, v in state.items()
                if float(v) > now
            }
            if float(state.get(key, 0) or 0) > now:
                return False
            state[key] = now + ttl
            tmp_path = Path(f"{_DEDUPE_PATH}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp_path, _DEDUPE_PATH)
            return True
    except (OSError, ValueError, TypeError) as exc:
        # 중복 억제 장애가 실거래 알림 누락으로 이어지면 안 된다.
        print(f"[Telegram] 중복방지 상태 오류 — 발송 계속: {exc}")
        return True


def _release_dedupe(key: str) -> None:
    """실제 발송 실패 시 다음 스캔이 재시도할 수 있게 선점을 해제한다."""
    lock_path = Path(f"{_DEDUPE_PATH}.lock")
    try:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            raw = json.loads(_DEDUPE_PATH.read_text(encoding="utf-8"))
            state = raw if isinstance(raw, dict) else {}
            state.pop(key, None)
            tmp_path = Path(f"{_DEDUPE_PATH}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp_path, _DEDUPE_PATH)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass


def _post(token: str, chat_id: str, text: str) -> bool:
    """Telegram Bot API로 HTML 메시지를 보낸다.

    여기서는 토큰/채팅방 값의 존재 여부를 판단하지 않는다. 어떤 채널로 보낼지는
    `_send_env()`에서 env 키 단위로 결정하고, 이 함수는 전송 성공/실패만 책임진다.
    """
    remaining = _backoff_remaining(token)
    route_key = _token_key(token)
    if remaining > 0:
        if route_key not in _backoff_route_reported:
            print(f"[Telegram] 429 쿨다운 중 — {remaining}초간 발송 스킵")
            _backoff_route_reported.add(route_key)
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        result = resp.json()
        if not result.get("ok"):
            params = result.get("parameters") or {}
            retry_after = int(params.get("retry_after", 0) or 0)
            if retry_after > 0:
                _set_backoff(token, retry_after)
                print(
                    f"[Telegram] API 429: {retry_after}초 쿨다운 저장 — "
                    "그동안 발송 스킵"
                )
            else:
                print(f"[Telegram] API 오류: {result.get('description', result)}")
        return result.get("ok", False)
    except Exception as e:
        print(f"[Telegram] 전송 실패: {_safe_error(e, token)}")
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
    """God of Trading 방 — 국내주식 등 공용 시장 스크리닝 결과."""
    return send_trade(text)


def send_bithumb(text: str) -> bool:
    """빗썸 알리미 전용 — 다른 매매/시그널 채널로 폴백하지 않는다."""
    return _send_env(
        "BITHUMB_BOT_TOKEN", "BITHUMB_CHAT_ID", text,
        "빗썸알리미봇", required=False,
    )


def send_signal(text: str) -> bool:
    """바이빗 트레이딩 파크 — 모든 시그널 근거 전용."""
    return _send_env("SIGNAL_BOT_TOKEN", "SIGNAL_CHAT_ID", text,
                     "시그널봇", required=False)


def send_signal_once(text: str, *, dedupe_key: str, ttl_seconds: int) -> bool:
    """동일 판단은 TTL 동안 한 번만 발송한다(full/fast 프로세스 공용)."""
    if not _claim_dedupe(dedupe_key, ttl_seconds):
        print("[시그널봇] 동일 캔들·동일 판단 중복 — 발송 스킵")
        return False
    ok = send_signal(text)
    if not ok:
        _release_dedupe(dedupe_key)
    return ok


def send(text: str) -> bool:
    """하위 호환용: 실제 매매내역 라우팅."""
    return send_trade(text)


def send_review(text: str) -> bool:
    """복기/결산도 매매내역 라우팅으로 보낸다."""
    return send_trade(text)
