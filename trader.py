"""
자동매매 모듈 — Bybit V5 USDT 영구 선물
리스크: 강도별 계좌 위험률 / 일손실 한도 / 드로우다운 하드스톱
"""
from __future__ import annotations
import os
import json
import time
import math
import uuid
import hashlib
import warnings
from collections import deque
from html import escape
from pathlib import Path
from datetime import datetime, timezone, timedelta

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import ccxt
from dotenv import load_dotenv
from config import (CANDIDATE_LOG_FILE, EXECUTION_JOURNAL_FILE,
                    DRAWDOWN_HARD_STOP_PCT, DRAWDOWN_PAUSE_HOURS,
                    DRAWDOWN_HARD_STOP_BLOCK_NEW_TRADES,
                    DRAWDOWN_RISK_OFF_PCT, DRAWDOWN_WARN_PCT,
                    MAX_DAILY_LOSS_PCT, MIN_FALLBACK_TRADE_MARGIN_USD,
                    MIN_TRADE_MARGIN_USD, ROUND_TRIP_FEE,
                    MIN_QTY_MAP, QTY_STEP_MAP)
from strategy_catalog import classify_strategy, format_profile
from venue_runtime import namespaced_data_path, runtime_venue, venue_label

load_dotenv(Path(__file__).parent / ".env")

KST        = timezone(timedelta(hours=9))
VENUE      = runtime_venue()
STATE_FILE = namespaced_data_path("trade_state.json")
CANDIDATE_FILE = namespaced_data_path(CANDIDATE_LOG_FILE)
EXECUTION_JOURNAL = namespaced_data_path(EXECUTION_JOURNAL_FILE)

TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_CANDIDATE_EVENT_KEYS: set[tuple[str, str, str]] | None = None


def _candidate_log_tail(max_lines: int = 50_000) -> list[dict]:
    """Read only the useful tail of a potentially very large JSONL ledger."""
    if not CANDIDATE_FILE.exists():
        return []
    rows: list[dict] = []
    try:
        with CANDIDATE_FILE.open("r", encoding="utf-8") as handle:
            lines = deque(handle, maxlen=max_lines)
        for line in lines:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        print(f"[후보로그] tail 읽기 실패: {exc}")
    return rows


def _load_candidate_event_keys() -> set[tuple[str, str, str]]:
    global _CANDIDATE_EVENT_KEYS
    if _CANDIDATE_EVENT_KEYS is None:
        _CANDIDATE_EVENT_KEYS = {
            (
                str(row.get("candidate_id") or ""),
                str(row.get("status") or ""),
                str(row.get("reason") or ""),
            )
            for row in _candidate_log_tail(8_000)
            if row.get("candidate_id")
        }
    return _CANDIDATE_EVENT_KEYS

# ─── 리스크 파라미터 (100억 프로젝트 — 공격적 복리 성장) ─────────────────────
TRADE_MARGIN_PCT      = 0.25   # 스윙 기본 비율 (강도별 override 됨)
SCALP_MARGIN_PCT      = 0.13   # 스캘핑 기본 비율
MAX_MARGIN_USD        = 120.0  # 50 → 120: 복리 성장 시 자동 스케일업 허용
MAX_SCALP_MARGIN_USD  = 120.0  # 계좌 60달러 구간에서도 전액 투입 여지 확보
MIN_BALANCE_USD  = max(20.0, MIN_TRADE_MARGIN_USD)
MAX_DAILY_LOSS   = 10.0   # 동적 일손실 한도의 상한/대형계좌 하한. 소액 계좌는 MAX_DAILY_LOSS_PCT 우선.
MAX_CONSEC_LOSS  = 3      # 학습/리포트용 연패 기준. 연패만으로 매매를 중단하지 않는다.
PAUSE_HOURS      = 0      # 연패 타임락 비활성화
MAX_LEVERAGE     = 30     # 25 → 30: 황금 진입 시 고레버리지 허용
MAX_CONCURRENT   = 4      # legacy 호환값. 신규 진입 차단은 포트폴리오 증거금 엔진(main.py)이 담당한다.
MAX_MARGIN_PCT_CAP       = 1.00
MAX_SCALP_MARGIN_PCT_CAP = 1.00

# ─── 트레일링 스톱 파라미터 (ELITE 전용) ─────────────────────────────────────
TRAIL_ATR_MULT    = 1.5   # ELITE: 현재가에서 SL까지 ATR 거리
TRAIL_ATR_MULT_STANDARD = 2.0  # 2026-07-11: 일반(비-ELITE) TP1 이후 러너 트레일
TRAIL_ADVANCE_MIN = 0.5   # SL 갱신 최소 이동량 (ATR 단위), 너무 자주 갱신 방지
# 2026-07-11 R:R 개선: 0.8R에서 너무 일찍 잠그면 노이즈에 걸린 뒤 TP1을 못 먹는
# 케이스가 있고, 잔량 BE 청산이 승리 금액을 깎음. 1.0R 도달 시 0.55R 잠금.
PRE_TP_BE_TRIGGER_R = 1.0
BE_FEE_CUSHION_MULT = 1.2 # 수수료까지 감안한 소폭 이익 보호
PRE_TP_BE_LOCK_FRACTION = 0.55
# TP1 부분익절 후 잔량을 순수 BE가 아니라 +0.35R에 잠가 얕은 승리 금액 개선
# (실측: "부분익절 후 잔량 보호청산" 다수 → 잔량이 BE에서 죽으면 전체 기대값 악화)
POST_TP1_LOCK_R = 0.35
# 일반 포지션도 TP1 이후 트레일 허용 (기존 ELITE only → 러너 기회 확대)
TRAIL_AFTER_TP1_ALL = True
PROFIT_LOCK_TRIGGER_MARGIN_ROI_PCT = 10.0 # 레버리지 포함 +10% 수익권 진입 시
PROFIT_LOCK_SL_MARGIN_ROI_PCT = 10.0      # SL도 증거금 ROI +10% 부근으로 이동

MIN_QTY  = MIN_QTY_MAP
QTY_STEP = QTY_STEP_MAP


# ─── Bybit 연결 ──────────────────────────────────────────────────────────────

def _ex() -> ccxt.bybit:
    return ccxt.bybit({
        "apiKey":  os.getenv("BYBIT_API_KEY", ""),
        "secret":  os.getenv("BYBIT_API_SECRET", ""),
        "options": {"defaultType": "linear"},
        "enableRateLimit": True,
    })


def _futures_symbol(symbol: str) -> str:
    """ccxt USDT 영구선물 심볼 변환. SOL/USDT → SOL/USDT:USDT"""
    if ":" not in symbol:
        base = symbol.split("/")[0]
        return f"{base}/USDT:USDT"
    return symbol


# ─── 상태 파일 (서킷브레이커 추적) ──────────────────────────────────────────

def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "venue": VENUE,
        "daily_loss": 0.0,
        "consec_loss": 0,
        "pause_until": 0,
        "last_reset": "",
    }


def _save_state(s: dict):
    s.setdefault("venue", VENUE)
    # 프로세스가 중단돼도 반쪽 JSON이 남지 않게 같은 디렉터리에서 원자 교체한다.
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _refresh_daily(s: dict) -> dict:
    if s.get("last_reset") != _today_kst():
        s["daily_loss"] = 0.0
        s["last_reset"] = _today_kst()
    return s


def get_daily_loss_limit(balance: float = 0.0) -> float:
    """
    복리 운용용 일손실 한도.
    소액 구간은 계좌 기준 일손실 한도를 우선해 생존성을 확보하고,
    계좌가 커지면 고정 $30 한도에 묶이지 않게 비율 기준으로 확장한다.
    """
    if balance <= 0:
        return MAX_DAILY_LOSS
    pct_limit = balance * MAX_DAILY_LOSS_PCT
    if balance < 500:
        return round(min(MAX_DAILY_LOSS, pct_limit), 2)
    return round(max(MAX_DAILY_LOSS, pct_limit), 2)


def _env_float(name: str) -> float:
    raw = os.getenv(name, "").strip()
    if not raw or "여기에" in raw:
        return 0.0
    try:
        return float(raw)
    except Exception:
        return 0.0


def _last_closed_loss_ts(s: dict) -> float:
    for trade in reversed(s.get("trade_history", [])):
        if trade.get("status") == "loss":
            return float(trade.get("timestamp", 0) or 0)
    return float(s.get("last_loss_ts", 0) or 0)


def _apply_drawdown_guard(s: dict, equity: float) -> tuple[bool, str]:
    """
    계좌 전체 드로우다운 고정시간 방어.

    실행 기준점 대비 DD가 hard-stop 한도에 닿으면 최초 시각부터 정확히
    DRAWDOWN_PAUSE_HOURS 동안만 신규진입을 막는다. 반복 검사로 종료시각을
    연장하지 않으며, 시간이 지나면 손실률과 무관하게 현재 equity로 실행
    기준점을 재설정하고 자동 재개한다. 전체 최고 equity와 역대 최대 DD는
    별도 기록으로 보존한다.

    .env ACCOUNT_START_BALANCE가 있으면 기존 손실률까지 반영하고,
    없으면 봇이 관측한 최고 equity 기준으로 이후 손실을 막는다.
    """
    if equity <= 0:
        return True, "ok"

    configured_start = _env_float("ACCOUNT_START_BALANCE")
    stored_start = float(s.get("equity_start", 0) or 0)
    start = configured_start or stored_start or equity
    s["equity_start"] = round(start, 4)

    peak = max(float(s.get("equity_peak", 0) or 0), start, equity)
    s["equity_peak"] = round(peak, 4)
    s["last_equity"] = round(equity, 4)

    all_time_drawdown = max(0.0, (peak - equity) / peak) if peak > 0 else 0.0
    s["all_time_drawdown_pct"] = round(all_time_drawdown * 100, 2)

    # equity_peak는 보고용 역대 최고점으로 보존한다. 시간제 재개 후 즉시 다시
    # 18% 하드스톱에 걸리지 않도록 실행 회차의 기준점은 별도로 관리한다.
    guard_peak = float(s.get("drawdown_guard_peak", 0) or 0)
    if guard_peak <= 0:
        guard_peak = peak
    guard_peak = max(guard_peak, equity)
    s["drawdown_guard_peak"] = round(guard_peak, 4)
    drawdown = (
        max(0.0, (guard_peak - equity) / guard_peak)
        if guard_peak > 0 else 0.0
    )
    s["drawdown_pct"] = round(drawdown * 100, 2)
    s["max_drawdown_pct"] = max(
        float(s.get("max_drawdown_pct", 0) or 0),
        s["all_time_drawdown_pct"],
    )

    now_ts = time.time()
    started_ts = float(s.get("hard_stop_started_ts", 0) or 0)
    until_ts = float(
        s.get("hard_stop_until", 0)
        or s.get("pause_until", 0)
        or 0
    )
    hard_stop_latched = started_ts > 0 or s.get("drawdown_status") == "hard_stop"

    if hard_stop_latched and DRAWDOWN_HARD_STOP_BLOCK_NEW_TRADES:
        if started_ts <= 0:
            # 구버전에서 이미 멈춘 상태를 마이그레이션한다. 기존 종료시각이
            # 있으면 역산하고, 없으면 현재를 최초 발동시각으로 사용한다.
            started_ts = (
                until_ts - DRAWDOWN_PAUSE_HOURS * 3600
                if until_ts > now_ts else now_ts
            )
        if until_ts <= 0:
            until_ts = started_ts + DRAWDOWN_PAUSE_HOURS * 3600
        s["hard_stop_started_ts"] = started_ts
        s["hard_stop_until"] = until_ts
        s["pause_until"] = until_ts
        if now_ts < until_ts:
            s["drawdown_status"] = "hard_stop"
            until = datetime.fromtimestamp(until_ts, KST).strftime("%m/%d %H:%M")
            remaining_minutes = max(1, math.ceil((until_ts - now_ts) / 60))
            return False, (
                f"계좌 드로우다운 시간제 하드스톱 — {until} KST 자동재개 "
                f"(약 {remaining_minutes}분 남음, 종료시각 고정)"
            )

        # 고정시간 만료: 현재 손실률과 무관하게 무조건 같은 크기로 재개한다.
        previous_guard_peak = guard_peak
        previous_drawdown_pct = round(drawdown * 100, 2)
        s.setdefault("hard_stop_resumes", []).append({
            "timestamp": now_ts,
            "equity": round(equity, 4),
            "previous_guard_peak": round(previous_guard_peak, 4),
            "previous_drawdown_pct": previous_drawdown_pct,
            "pause_hours": DRAWDOWN_PAUSE_HOURS,
        })
        s["hard_stop_resume_count"] = int(
            s.get("hard_stop_resume_count", 0) or 0
        ) + 1
        s["last_hard_stop_resume_ts"] = now_ts
        s["last_hard_stop_resume_equity"] = round(equity, 4)
        s["last_hard_stop_drawdown_pct"] = previous_drawdown_pct
        s["drawdown_guard_peak"] = round(equity, 4)
        s["drawdown_pct"] = 0.0
        s["drawdown_status"] = "normal"
        s["hard_stop_started_ts"] = 0
        s["hard_stop_until"] = 0
        s["pause_until"] = 0
        return True, (
            f"계좌 드로우다운 4시간 하드스톱 만료 — equity ${equity:.2f} "
            "기준으로 자동재개"
        )

    if drawdown >= DRAWDOWN_HARD_STOP_PCT:
        if not DRAWDOWN_HARD_STOP_BLOCK_NEW_TRADES:
            s["pause_until"] = 0
            s["drawdown_status"] = "hard_stop_recovery"
            return True, (
                f"계좌 드로우다운 {drawdown*100:.1f}% >= {DRAWDOWN_HARD_STOP_PCT*100:.0f}% "
                f"— 신규매매 중단 대신 회복모드로 축소 운용"
            )
        started_ts = now_ts
        until_ts = started_ts + DRAWDOWN_PAUSE_HOURS * 3600
        s["hard_stop_started_ts"] = started_ts
        s["hard_stop_until"] = until_ts
        s["pause_until"] = until_ts
        s["last_hard_stop_trigger_ts"] = started_ts
        s["last_hard_stop_trigger_equity"] = round(equity, 4)
        s["last_hard_stop_trigger_drawdown_pct"] = round(drawdown * 100, 2)
        s["drawdown_status"] = "hard_stop"
        until = datetime.fromtimestamp(until_ts, KST).strftime("%m/%d %H:%M")
        return False, (
            f"계좌 드로우다운 {drawdown*100:.1f}% >= {DRAWDOWN_HARD_STOP_PCT*100:.0f}% "
            f"— {until} KST 자동재개 (고정 {DRAWDOWN_PAUSE_HOURS}시간)"
        )

    if drawdown >= DRAWDOWN_RISK_OFF_PCT:
        s["drawdown_status"] = "risk_off"
    elif drawdown >= DRAWDOWN_WARN_PCT:
        s["drawdown_status"] = "warning"
    else:
        s["drawdown_status"] = "normal"
    return True, "ok"


def get_margin_cap(balance: float, scalp: bool = False) -> float:
    """계좌 성장에 따라 증거금 한도도 같이 커지게 한다."""
    pct_cap = MAX_SCALP_MARGIN_PCT_CAP if scalp else MAX_MARGIN_PCT_CAP
    floor   = MAX_SCALP_MARGIN_USD if scalp else MAX_MARGIN_USD
    return round(max(floor, balance * pct_cap), 2)


def position_pct_for_risk(balance: float, leverage: int, entry_price: float,
                          sl_price: float, risk_pct: float,
                          max_position_pct: float) -> tuple[float, float]:
    """
    목표 계좌 위험률을 만족하는 증거금 비율 계산.
    Returns: (position_pct, estimated_sl_loss_usd)
    """
    if balance <= 0 or leverage <= 0 or entry_price <= 0 or sl_price <= 0:
        return 0.0, 0.0

    sl_pct = abs(entry_price - sl_price) / entry_price
    if sl_pct <= 0:
        return 0.0, 0.0

    target_loss = balance * risk_pct
    loss_pct_with_fees = sl_pct + ROUND_TRIP_FEE
    raw_margin  = target_loss / (leverage * loss_pct_with_fees)
    pct         = min(raw_margin / balance, max_position_pct)
    pct         = max(pct, 0.0)
    est_loss    = balance * pct * leverage * loss_pct_with_fees
    return round(pct, 4), round(est_loss, 4)


def _json_safe(value):
    """numpy/pandas 값이 섞여도 상태/저널 JSON 저장이 실패하지 않게 변환한다."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return str(value)


def log_trade_candidate(symbol: str, tf_key: str, strategy: str,
                        direction: str, strength: str, status: str,
                        reason: str = "", **extra):
    """후보/차단/체결 이벤트를 JSONL로 저장해 사후 통계 검증에 사용한다.

    같은 거래소·심볼·판단봉·전략 신호를 매 3~5분 스캔마다 새 후보로 만들면
    사후평가 표본이 중복으로 부풀고 오래된 후보가 tail에서 밀려난다. v6부터
    candidate_id는 판단봉 버킷에 고정하고 동일 상태/사유 이벤트는 한 번만 쓴다.
    """
    if "strategy_family" not in extra or "core_strategy" not in extra:
        signal_type = str(extra.get("signal_type", "") or "")
        is_divergence = bool(extra.get(
            "is_divergence",
            signal_type in {"bullish", "bearish", "hidden_bullish", "hidden_bearish"},
        ))
        profile = classify_strategy(
            strategy, signal_type, is_divergence, direction,
            {"reasons": [reason] if reason else []},
            bool(extra.get("asymmetric_mode", False)),
        )
        extra.setdefault("strategy_family", profile["family_label"])
        extra.setdefault("core_strategy", profile["strategy_label"])
        extra.setdefault("strategy_mode", profile["family_key"])
    candidate_id = extra.pop("candidate_id", None)
    if not candidate_id:
        now = time.time()
        bucket_seconds = TF_SECONDS.get(tf_key, 300)
        signal_bucket = int(now // bucket_seconds) * bucket_seconds
        identity = "|".join((
            VENUE,
            symbol,
            tf_key,
            strategy,
            direction,
            str(extra.get("signal_type", "") or ""),
            str(signal_bucket),
        ))
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
        safe_symbol = symbol.replace("/", "").replace(":", "")
        candidate_id = f"{signal_bucket}-{safe_symbol}-{tf_key}-{digest}"
    event_key = (str(candidate_id), str(status), str(reason))
    event_keys = _load_candidate_event_keys()
    if event_key in event_keys:
        return candidate_id
    row = {
        "candidate_id": candidate_id,
        "venue":     VENUE,
        "time":      datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "timestamp": time.time(),
        "symbol":    symbol,
        "tf":        tf_key,
        "strategy":  strategy,
        "direction": direction,
        "strength":  strength,
        "status":    status,
        "reason":    reason,
    }
    row.update(_json_safe(extra))
    with CANDIDATE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    event_keys.add(event_key)
    # 한 프로세스가 매우 오래 살아도 메모리가 무한정 커지지 않게 한다. 일반 실행은
    # one-shot이어서 거의 도달하지 않지만 안전한 상한을 둔다.
    if len(event_keys) > 30_000:
        _CANDIDATE_EVENT_KEYS.clear()
        _CANDIDATE_EVENT_KEYS.update(list(event_keys)[-12_000:])
    return candidate_id


def log_execution_journal(trade_num: int | None, event: str = "opened", **payload):
    """실제 체결/청산 이벤트를 JSONL로 저장해 매매 복기와 전략 개선에 사용한다."""
    row = {
        "trade_num": trade_num,
        "event":     event,
        "venue":     VENUE,
        "time":      datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "timestamp": time.time(),
    }
    row.update(_json_safe(payload))
    with EXECUTION_JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


EVAL_HORIZONS = (5, 10, 20)


def _candidate_key(row: dict) -> str:
    if row.get("candidate_id"):
        return row["candidate_id"]
    return (
        f"{int(row.get('timestamp', 0) * 1000)}-"
        f"{row.get('symbol','').replace('/','')}-{row.get('tf','')}-"
        f"{row.get('status','')}-{row.get('reason','')[:24]}"
    )


def evaluate_trade_candidates(max_items: int = 80) -> list[str]:
    """
    후보 신호 사후 평가.
    각 후보 이후 5/10/20봉의 MFE/MAE를 기록해 다음 전략 수정의 근거로 쓴다.
    """
    if not CANDIDATE_FILE.exists():
        return []

    rows = _candidate_log_tail(50_000)
    if not rows:
        return []

    s = _load_state()
    evaluated = set(s.get("evaluated_candidate_ids", []))
    now = time.time()
    pending = []
    pending_ids: set[str] = set()
    source_by_id: dict[str, dict] = {}
    source_priority = {"blocked": 1, "order_failed": 2, "opened": 3}

    # 한 판단봉 후보가 여러 게이트 상태를 거쳤다면 실제 주문에 가장 가까운 이벤트를
    # 대표로 평가한다(opened > order_failed > blocked). 같은 후보를 여러 표본으로
    # 세는 누수를 막는다.
    for row in rows:
        if row.get("status") not in ("blocked", "order_failed", "opened"):
            continue
        cid = _candidate_key(row)
        prior = source_by_id.get(cid)
        if prior is None or source_priority.get(row.get("status"), 0) >= source_priority.get(prior.get("status"), 0):
            source_by_id[cid] = row

    for cid, row in source_by_id.items():
        if cid in evaluated:
            continue
        tf = row.get("tf")
        price = float(row.get("price") or 0)
        ts = float(row.get("timestamp") or 0)
        if not tf or price <= 0 or ts <= 0:
            evaluated.add(cid)
            continue
        if now - ts < TF_SECONDS.get(tf, 3600) * max(EVAL_HORIZONS):
            continue
        if cid in pending_ids:
            continue
        pending.append((cid, row))
        pending_ids.add(cid)
        if len(pending) >= max_items:
            break

    if not pending:
        if evaluated:
            s["evaluated_candidate_ids"] = list(evaluated)[-2000:]
            _save_state(s)
        return []

    from fetcher import fetch_ohlcv

    notes = []
    cache = {}
    for cid, row in pending:
        symbol = row["symbol"]
        tf = row["tf"]
        key = (symbol, tf)
        try:
            if key not in cache:
                cache[key] = fetch_ohlcv(symbol, tf, 240)
            df = cache[key]
            ts = float(row["timestamp"])
            price = float(row["price"])
            direction = row.get("direction", "")

            idx = None
            for i, t in enumerate(df.index):
                if t.timestamp() >= ts:
                    idx = i
                    break
            if idx is None:
                evaluated.add(cid)
                continue

            result = {}
            latest_close = float(df["close"].iloc[-1])
            if latest_close > 0:
                if direction == "LONG":
                    now_move = (latest_close - price) / price * 100
                else:
                    now_move = (price - latest_close) / price * 100
                result["now"] = {
                    "price": round(latest_close, 8),
                    "move_pct": round(now_move, 3),
                    "edge_score": round(now_move, 3),
                }
            for h in EVAL_HORIZONS:
                future = df.iloc[idx + 1: idx + 1 + h]
                if len(future) < h:
                    continue
                hi = float(future["high"].max())
                lo = float(future["low"].min())
                if direction == "LONG":
                    mfe = (hi - price) / price * 100
                    mae = (lo - price) / price * 100
                else:
                    mfe = (price - lo) / price * 100
                    mae = (price - hi) / price * 100
                result[str(h)] = {
                    "mfe_pct": round(mfe, 3),
                    "mae_pct": round(mae, 3),
                    "edge_score": round(mfe + mae, 3),
                }

            if result:
                log_trade_candidate(
                    symbol, tf, row.get("strategy", ""), direction,
                    row.get("strength", ""), "evaluated",
                    reason="후보 사후평가",
                    candidate_id=cid,
                    source_status=row.get("status", ""),
                    source_reason=row.get("reason", ""),
                    price=price,
                    eval=result,
                )
                notes.append(f"{symbol} {tf} {direction} {row.get('strategy','')} 평가 완료")
            evaluated.add(cid)
        except Exception as e:
            print(f"[후보평가] {row.get('symbol')} {row.get('tf')} 실패: {e}")

    s["evaluated_candidate_ids"] = list(evaluated)[-2000:]
    _save_state(s)
    return notes


def notify_trade_block(symbol: str, tf_key: str, direction: str,
                       strength: str, reason: str,
                       strategy: str = "자동매매",
                       send_telegram: bool = False, **extra):
    """콘솔 + 후보 로그 + 선택적 텔레그램 진단."""
    msg = f"  [{strategy}] {symbol} {tf_key} {direction} {strength} → {reason}"
    print(msg)
    profile = classify_strategy(
        strategy, str(extra.get("signal_type", "") or ""),
        bool(extra.get("is_divergence", False)), direction,
        {"reasons": [reason] if reason else []},
        bool(extra.get("asymmetric_mode", False)),
    )
    extra.setdefault("strategy_family", profile["family_label"])
    extra.setdefault("core_strategy", profile["strategy_label"])
    extra.setdefault("strategy_mode", profile["family_key"])
    log_trade_candidate(symbol, tf_key, strategy, direction, strength,
                        "blocked", reason, **extra)
    if send_telegram:
        try:
            from publisher import send_signal as tg_send
            coin = symbol.split("/")[0]
            tg_send(
                f"🧭 <b>[자동매매 차단]</b> {coin} {tf_key} {direction}\n"
                f"전략군: <b>{escape(format_profile(profile))}</b>\n"
                f"강도: <b>{escape(str(strength))}</b>\n"
                f"사유: {escape(str(reason))}"
            )
        except Exception as e:
            print(f"[진단] 차단 알림 실패: {e}")


# ─── 서킷브레이커 ────────────────────────────────────────────────────────────

def check_circuit_breaker(balance: float = 0.0,
                          allow_pause_override: bool = False,
                          override_reason: str = "",
                          equity: float | None = None) -> tuple[bool, str]:
    """(거래 가능 여부, 이유 메시지) 반환."""
    s = _refresh_daily(_load_state())
    equity_now = get_usdt_equity() or balance if equity is None else float(equity)
    guard_ok, guard_reason = _apply_drawdown_guard(s, equity_now)
    daily_limit = get_daily_loss_limit(balance)

    if not guard_ok:
        _save_state(s)
        return False, guard_reason

    if float(s.get("daily_loss", 0) or 0) >= daily_limit:
        if allow_pause_override:
            reason = override_reason or "고기대수익 기회"
            _save_state(s)
            return True, (
                f"일일 손실 한도 초과이나 {reason} 예외 — "
                f"오늘 손실 ${float(s.get('daily_loss', 0) or 0):.2f} / 한도 ${daily_limit:.2f}"
            )
        _save_state(s)
        return False, (
            f"일일 손실 한도 도달 — 오늘 손실 "
            f"${float(s.get('daily_loss', 0) or 0):.2f} / 한도 ${daily_limit:.2f}"
        )

    # 연패는 학습/복기 신호로만 사용한다. 과거 버전의 3연패 pause가 남아 있으면 제거한다.
    if s.get("drawdown_status") != "hard_stop":
        s["pause_until"] = 0

    _save_state(s)
    return True, "ok"


def record_result(pnl_usd: float):
    """거래 결과 기록. pnl_usd 음수 = 손실."""
    s = _refresh_daily(_load_state())
    if pnl_usd < 0:
        s["daily_loss"] = round(s.get("daily_loss", 0) + abs(pnl_usd), 4)
        s["consec_loss"] = s.get("consec_loss", 0) + 1
        s["last_loss_ts"] = time.time()
        if s["consec_loss"] >= MAX_CONSEC_LOSS:
            print(f"[학습] {s['consec_loss']}연패 기록 — 매매 중단 없이 패인 분석에 반영")
    else:
        s["consec_loss"] = 0
    _save_state(s)


# ─── 잔고 / 포지션 조회 ──────────────────────────────────────────────────────

def get_usdt_balance() -> float:
    try:
        # Bybit UTA(통합계좌) 기준 조회
        bal = _ex().fetch_balance({"type": "unified", "accountType": "UNIFIED"})
        return float(bal.get("USDT", {}).get("free", 0))
    except Exception as e:
        print(f"[잔고] 조회 실패: {e}")
        return 0.0


def get_usdt_equity() -> float:
    try:
        bal = _ex().fetch_balance({"type": "unified", "accountType": "UNIFIED"})
        usdt = bal.get("USDT", {}) or {}
        total = usdt.get("total")
        if total is None:
            total = usdt.get("free", 0)
        return float(total or 0)
    except Exception as e:
        print(f"[에쿼티] 조회 실패: {e}")
        return 0.0


def has_open_position(symbol: str) -> bool:
    """해당 코인에 롱/숏 포지션이 하나라도 있으면 True. 조회 실패 시 안전하게 차단."""
    try:
        fsym = _futures_symbol(symbol)
        positions = _ex().fetch_positions([fsym], params={"category": "linear"})
        for p in positions:
            if abs(float(p.get("contracts", 0) or 0)) > 0:
                return True
    except Exception as e:
        print(f"[포지션] 조회 실패: {e}")
        return True  # 조회 실패 시 진입 차단 (안전 우선)
    return False


def get_open_position_count() -> int:
    """
    현재 오픈된 전체 포지션 수 반환.

    legacy 진단용 함수.
    신규 진입 차단은 더 이상 개수 기반으로 하지 않고, main.py의
    포트폴리오 증거금/SL위험 게이트에서 판단한다.
    """
    try:
        positions = _ex().fetch_positions(params={"category": "linear"})
        return sum(1 for p in positions if abs(float(p.get("contracts", 0) or 0)) > 0)
    except Exception as e:
        print(f"[포지션수] 조회 실패: {e}")
        return MAX_CONCURRENT   # 실패 시 최대값으로 간주 (안전 우선)


def fetch_all_positions_raw() -> list[dict]:
    """
    Bybit 전체 오픈 포지션 상세 반환 (orphan 감지 + 긴급 SL 설정용).
    Returns: [{"symbol": "BTC/USDT", "direction", "qty", "entry_price",
               "mark_price", "leverage", "unrealized_pnl", "margin", "liq_price"}, ...]
    """
    try:
        positions = _ex().fetch_positions(params={"category": "linear"})
        result = []
        for p in positions:
            qty = abs(float(p.get("contracts", 0) or 0))
            if qty <= 0:
                continue
            raw_sym = p.get("symbol", "")   # "SOL/USDT:USDT"
            sym = raw_sym.split(":")[0]      # "SOL/USDT"
            result.append({
                "symbol":          sym,
                "fsymbol":         raw_sym,
                "direction":       "LONG" if p.get("side") == "long" else "SHORT",
                "qty":             qty,
                "entry_price":     float(p.get("entryPrice", 0)      or 0),
                "mark_price":      float(p.get("markPrice", 0)        or 0),
                "leverage":        float(p.get("leverage", 1)         or 1),
                "unrealized_pnl":  float(p.get("unrealizedPnl", 0)   or 0),
                "liq_price":       float(p.get("liquidationPrice", 0) or 0),
                "margin":          float((p.get("info") or {}).get("positionIM", 0) or 0),
            })
        return result
    except Exception as e:
        print(f"[포지션] 전체 조회 실패: {e}")
        return []


def get_portfolio_risk_snapshot(retries: int = 2) -> dict:
    """
    신규 진입 허용 판단용 포트폴리오 스냅샷.

    고정 동시 포지션 개수 대신 실제 사용 증거금과 로컬 SL 기준 총 손절 위험을 본다.
    조회 실패 시에도 사용 가능한 free/equity 정보를 최대한 반환해, 단순 API 오류가
    곧바로 "포지션 개수 한도" 차단으로 이어지지 않게 한다.
    """
    def _num(value, default: float = 0.0) -> float:
        try:
            return float(value or default)
        except Exception:
            return default

    def _position_rows(raw_positions: list[dict]) -> list[dict]:
        state_positions = (_load_state().get("positions", {}) or {})
        rows: list[dict] = []
        for p in raw_positions:
            qty = abs(_num(p.get("contracts")))
            if qty <= 0:
                continue

            raw_sym = p.get("symbol", "")
            sym = raw_sym.split(":")[0]
            side = str(p.get("side", "")).lower()
            direction = "LONG" if side == "long" else "SHORT"
            entry_price = _num(p.get("entryPrice"))
            mark_price = _num(p.get("markPrice"), entry_price)
            leverage = max(_num(p.get("leverage"), 1.0), 1.0)
            notional = qty * (mark_price or entry_price)

            info = p.get("info") or {}
            margin = _num(info.get("positionIM"))
            if margin <= 0:
                margin = _num(info.get("positionBalance"))
            if margin <= 0 and notional > 0:
                margin = notional / leverage

            tracked = state_positions.get(sym, {}) or {}
            sl_price = _num(tracked.get("sl_price"))
            sl_risk = 0.0
            if sl_price > 0 and entry_price > 0 and qty > 0:
                if direction == "LONG":
                    adverse_move = max(entry_price - sl_price, 0.0)
                else:
                    adverse_move = max(sl_price - entry_price, 0.0)
                sl_risk = adverse_move * qty + notional * ROUND_TRIP_FEE

            rows.append({
                "symbol": sym,
                "direction": direction,
                "qty": qty,
                "entry_price": entry_price,
                "mark_price": mark_price,
                "leverage": leverage,
                "notional": notional,
                "margin": margin,
                "sl_risk": sl_risk,
                "unrealized_pnl": _num(p.get("unrealizedPnl")),
                "liq_price": _num(p.get("liquidationPrice")),
            })
        return rows

    attempts = max(1, int(retries or 1))
    last_error = ""
    rows: list[dict] = []
    ok = False
    for i in range(attempts):
        try:
            rows = _position_rows(_ex().fetch_positions(params={"category": "linear"}))
            ok = True
            break
        except Exception as e:
            last_error = str(e)
            print(f"[포트폴리오] 포지션 스냅샷 조회 실패({i + 1}/{attempts}): {e}")
            if i + 1 < attempts:
                time.sleep(0.4)

    free = get_usdt_balance()
    equity = get_usdt_equity()
    margin_used = sum(float(p.get("margin", 0) or 0) for p in rows)
    if not ok and equity > 0 and free >= 0:
        margin_used = max(equity - free, 0.0)
    if equity <= 0:
        equity = free + margin_used

    return {
        "ok": ok,
        "reason": "" if ok else last_error,
        "positions": rows,
        "count": len(rows),
        "margin_used": margin_used,
        "long_margin": sum(p["margin"] for p in rows if p["direction"] == "LONG"),
        "short_margin": sum(p["margin"] for p in rows if p["direction"] == "SHORT"),
        "sl_risk": sum(p["sl_risk"] for p in rows),
        "free": free,
        "equity": equity,
    }


def place_emergency_sl(symbol: str, direction: str, qty: float, sl_price: float) -> bool:
    """
    미추적 포지션(orphan)에 긴급 손절가 설정.
    기존 StopOrder를 모두 취소 후 새 SL 배치.
    """
    fsym      = _futures_symbol(symbol)
    close_side = "sell" if direction == "LONG" else "buy"
    tg_dir     = 2 if direction == "LONG" else 1
    ex = _ex()
    try:
        ex.load_markets()
        ex.cancel_all_orders(fsym, params={"category": "linear", "orderFilter": "StopOrder"})
        time.sleep(0.3)
        ex.create_order(
            fsym, "market", close_side, qty,
            params={
                "category":         "linear",
                "stopOrderType":    "StopLoss",
                "triggerPrice":     str(round(sl_price, 4)),
                "triggerDirection": tg_dir,
                "reduceOnly":       True,
            }
        )
        print(f"[긴급SL] {symbol} {direction}  SL=${sl_price:,.4f}  qty={qty}")
        return True
    except Exception as e:
        print(f"[긴급SL] {symbol} 실패: {e}")
        return False


# ─── 수량 계산 ───────────────────────────────────────────────────────────────

def calc_qty(symbol: str, entry_price: float,
             leverage: int, balance: float,
             position_pct: float = TRADE_MARGIN_PCT,
             max_margin: float = MAX_MARGIN_USD,
             exchange=None) -> tuple[float, int]:
    """
    포지션 수량 및 실제 레버리지 계산.
    최소 수량 미달 시 레버리지 자동 상향 (MAX_LEVERAGE 한도).
    Returns (qty, leverage). qty=0 이면 거래 불가.
    """
    margin   = min(balance * position_pct, max_margin)
    if margin <= 0 or entry_price <= 0 or leverage <= 0:
        print(f"[수량] {symbol} 수량 계산 불가 — margin={margin:.4f}, price={entry_price}, lev={leverage}")
        return 0.0, leverage
    fsym     = _futures_symbol(symbol)
    step     = QTY_STEP.get(symbol, 0.001)
    min_q    = MIN_QTY.get(symbol, 0.001)

    if exchange is not None:
        try:
            market = exchange.market(fsym)
            min_q = float((market.get("limits", {}).get("amount", {}) or {}).get("min") or min_q)
        except Exception:
            pass

    pos_val  = margin * leverage
    qty      = round(math.floor(pos_val / entry_price / step) * step, 8)

    if qty < min_q:
        req_lev = math.ceil((min_q * entry_price) / margin)
        if req_lev > MAX_LEVERAGE:
            print(f"[수량] {symbol} 최소수량 불가 — 필요 레버리지 {req_lev}x > 한도 {MAX_LEVERAGE}x")
            return 0.0, leverage
        print(f"[수량] 최소수량 충족 위해 레버리지 {leverage}x → {req_lev}x 조정")
        leverage = req_lev
        qty      = min_q

    if exchange is not None:
        try:
            qty = float(exchange.amount_to_precision(fsym, qty))
        except Exception:
            pass

    return qty, leverage


def _split_tps(total_qty: float, tps: list, symbol: str, exchange=None) -> list:
    """TP 비율에 따라 수량 분할. 최소수량 탓에 분할이 안 되면 가장 가까운 TP에 배치."""
    fsym  = _futures_symbol(symbol)
    step  = QTY_STEP.get(symbol, 0.001)
    min_q = MIN_QTY.get(symbol, 0.001)
    if exchange is not None:
        try:
            market = exchange.market(fsym)
            min_q = float((market.get("limits", {}).get("amount", {}) or {}).get("min") or min_q)
        except Exception:
            pass
    result    = []
    remaining = total_qty

    if total_qty < min_q * 2:
        qty = total_qty
        if exchange is not None:
            try:
                qty = float(exchange.amount_to_precision(fsym, qty))
            except Exception:
                pass
        if qty >= min_q and tps:
            return [{"qty": qty, "price": tps[0]["price"], "pct": 100}]

    for i, tp in enumerate(tps):
        if i == len(tps) - 1:
            qty = remaining
        else:
            qty = round(math.floor(total_qty * tp["pct"] / 100 / step) * step, 8)
        if exchange is not None:
            try:
                qty = float(exchange.amount_to_precision(fsym, qty))
            except Exception:
                pass

        if i == len(tps) - 1 and not result and qty >= min_q:
            return [{"qty": qty, "price": tps[0]["price"], "pct": 100}]

        if qty >= min_q:
            result.append({"qty": qty, "price": tp["price"], "pct": tp["pct"]})
            remaining = round(remaining - qty, 8)

    if remaining > 1e-9 and result:
        # 최소수량 때문에 앞쪽 분할이 생략된 경우, 잔량은 가장 가까운 TP에 더한다.
        final_qty = round(result[0]["qty"] + remaining, 8)
        if exchange is not None:
            try:
                final_qty = float(exchange.amount_to_precision(fsym, final_qty))
            except Exception:
                pass
        result[0]["qty"] = final_qty

    return result


# ─── 주문 실행 ───────────────────────────────────────────────────────────────

def _planned_stop_loss_usd(qty: float, entry_price: float, sl: float,
                           round_trip_cost: float = ROUND_TRIP_FEE) -> float:
    """Worst planned cash loss at SL, including conservative execution cost."""
    if qty <= 0 or entry_price <= 0 or sl <= 0:
        return 0.0
    return (
        float(qty) * abs(float(entry_price) - float(sl))
        + float(qty) * float(entry_price) * max(float(round_trip_cost), 0.0)
    )


def _live_position_after_entry(ex, fsym: str, attempts: int = 3) -> dict:
    """Read the actual Bybit position after a market entry."""
    last_error = ""
    for attempt in range(max(1, attempts)):
        try:
            for position in ex.fetch_positions(
                [fsym], params={"category": "linear"}
            ):
                qty = abs(float(position.get("contracts", 0) or 0))
                if qty <= 0:
                    continue
                entry = float(position.get("entryPrice", 0) or 0)
                leverage = max(float(position.get("leverage", 1) or 1), 1.0)
                return {
                    "ok": True,
                    "qty": qty,
                    "entry_price": entry,
                    "leverage": leverage,
                    "margin_usd": qty * entry / leverage if entry > 0 else 0.0,
                }
        except Exception as exc:
            last_error = str(exc)
        if attempt + 1 < attempts:
            time.sleep(0.25)
    return {"ok": False, "error": last_error or "체결 포지션이 조회되지 않음"}


def _emergency_market_close(ex, fsym: str, close_side: str, qty: float,
                            symbol: str, reason: str) -> bool:
    """Best-effort reduce-only close when a protected entry is incomplete."""
    print(f"  ❌ {symbol} 보호 실패 — 긴급 청산: {reason}")
    try:
        ex.create_order(
            fsym, "market", close_side, qty,
            params={"category": "linear", "reduceOnly": True},
        )
        print(f"  ✅ {symbol} 긴급 청산 주문 접수")
        return True
    except Exception as exc:
        print(f"  ❌ {symbol} 긴급 청산 실패: {exc}")
        return False


def execute(symbol: str, direction: str, leverage: int,
            entry_price: float, sl: float, tps: list,
            position_pct: float = TRADE_MARGIN_PCT,
            atr: float = 0.0, is_elite: bool = False,
            max_margin_usd: float | None = None,
            min_margin_usd: float | None = None,
            allow_pause_override: bool = False,
            pause_override_reason: str = "",
            max_sl_loss_usd: float | None = None,
            position_meta: dict | None = None,
            require_full_protection: bool = False) -> dict:
    """
    실거래 주문 실행.
    tps: [{"price": float, "pct": int}, ...]
    position_pct: 잔고 대비 증거금 비율 (스캘핑=0.10, 스윙=0.20)
    Returns {"ok": bool, "qty": float, "leverage": int, "error": str}
    """
    # 잔고 확인
    balance = get_usdt_balance()
    min_required_margin = (
        MIN_TRADE_MARGIN_USD if min_margin_usd is None
        else max(float(min_margin_usd), MIN_FALLBACK_TRADE_MARGIN_USD)
    )
    min_required_balance = MIN_BALANCE_USD if min_margin_usd is None else min_required_margin
    if balance < min_required_balance:
        msg = f"잔고 부족 ${balance:.2f} < 최소 ${min_required_balance:.2f}"
        print(f"[자동매매] {msg}")
        return {"ok": False, "qty": 0, "leverage": leverage, "error": msg}

    # 서킷브레이커
    ok, reason = check_circuit_breaker(
        balance,
        allow_pause_override=allow_pause_override,
        override_reason=pause_override_reason,
    )
    if not ok:
        print(f"[자동매매] 거래 차단 — {reason}")
        return {"ok": False, "qty": 0, "leverage": leverage, "error": reason}
    if reason != "ok":
        print(f"[자동매매] 서킷 예외 — {reason}")

    # 중복 포지션 확인
    if has_open_position(symbol):
        msg = f"{symbol} 이미 오픈 포지션 있음 — 스킵"
        print(f"[자동매매] {msg}")
        return {"ok": False, "qty": 0, "leverage": leverage, "error": msg}

    # ccxt는 USDT 영구선물에 SOL/USDT:USDT 형식 필요
    fsymbol = _futures_symbol(symbol)
    ex = _ex()
    try:
        ex.load_markets()
    except Exception as e:
        msg = f"시장 정보 조회 실패: {e}"
        print(f"[자동매매] {msg}")
        return {"ok": False, "qty": 0, "leverage": leverage, "error": msg}

    # 수량 계산
    if max_margin_usd is None:
        max_margin = get_margin_cap(balance, scalp=(position_pct <= SCALP_MARGIN_PCT))
    else:
        max_margin = max_margin_usd
    planned_margin = min(balance * position_pct, max_margin)
    if planned_margin + 1e-9 < min_required_margin:
        msg = (
            f"예정 증거금 ${planned_margin:.2f} < 최소 실행 증거금 "
            f"${min_required_margin:.2f}"
        )
        print(f"[자동매매] {msg}")
        return {"ok": False, "qty": 0, "leverage": leverage, "error": msg}
    qty, leverage = calc_qty(symbol, entry_price, leverage, balance, position_pct, max_margin, exchange=ex)
    if qty <= 0:
        return {"ok": False, "qty": 0, "leverage": leverage, "error": "수량 계산 실패"}

    planned_loss = _planned_stop_loss_usd(qty, entry_price, sl)
    loss_cap = max(float(max_sl_loss_usd or 0.0), 0.0)
    if loss_cap > 0 and planned_loss > loss_cap + 1e-6:
        msg = (
            f"정밀도 적용 예상 SL손실 ${planned_loss:.4f} > "
            f"현재 시드 상한 ${loss_cap:.4f}; 최소수량 확대 금지"
        )
        print(f"[자동매매] {msg}")
        return {
            "ok": False, "qty": 0, "leverage": leverage, "error": msg,
            "seed_equity": get_usdt_equity(), "free_balance": balance,
            "max_sl_loss_usd": loss_cap,
        }

    tp_splits = _split_tps(qty, tps, symbol, exchange=ex)
    if not tp_splits:
        return {"ok": False, "qty": 0, "leverage": leverage, "error": "TP 분할 실패"}

    side       = "buy"  if direction == "LONG"  else "sell"
    close_side = "sell" if direction == "LONG"  else "buy"
    tg_dir     = 2 if direction == "LONG" else 1

    print(f"\n{'='*50}")
    print(f"  🚀 자동매매 실행: {direction} {qty} {symbol}  {leverage}x")
    print(f"  잔고: ${balance:.2f}  |  증거금: ~${qty * entry_price / leverage:.2f}")
    print(f"  진입≈${entry_price:,.4f}  |  손절: ${sl:,.4f}")
    print(f"{'='*50}")

    entry_live = False
    actual_qty = qty
    actual_entry = entry_price
    actual_margin = qty * entry_price / max(leverage, 1)
    actual_loss = planned_loss
    entry_id = ""
    entry_order_link_id = ""
    try:
        # 1. 레버리지 설정
        try:
            ex.set_leverage(leverage, fsymbol)
        except Exception as e:
            err = str(e)
            if "110043" not in err and "not modified" not in err.lower():
                raise
        time.sleep(0.4)

        # 2. 시장가 진입
        entry_order_link_id = f"got-{uuid.uuid4().hex}"
        entry_order = ex.create_order(
            fsymbol, "market", side, qty,
            params={"category": "linear", "orderLinkId": entry_order_link_id}
        )
        entry_id = entry_order.get("id", "")
        entry_live = True
        print(f"  ✅ 진입 완료 (주문ID: {entry_id})")
        time.sleep(0.5)

        live_position = _live_position_after_entry(ex, fsymbol)
        if live_position.get("ok"):
            actual_qty = float(live_position.get("qty") or qty)
            actual_entry = float(live_position.get("entry_price") or entry_price)
            leverage = max(int(float(live_position.get("leverage") or leverage)), 1)
            actual_margin = float(
                live_position.get("margin_usd")
                or actual_qty * actual_entry / leverage
            )
            actual_loss = _planned_stop_loss_usd(actual_qty, actual_entry, sl)
        elif require_full_protection:
            emergency_ok = _emergency_market_close(
                ex, fsymbol, close_side, qty, symbol,
                f"진입 직후 실포지션 검증 실패: {live_position.get('error', '')}",
            )
            return {
                "ok": False, "qty": qty, "leverage": leverage,
                "error": (
                    "실포지션 검증 실패 — 긴급 청산 완료"
                    if emergency_ok else
                    "실포지션 검증 및 긴급 청산 실패 — 즉시 수동 확인 필요"
                ),
                "entry_order_id": entry_id,
                "entry_order_link_id": entry_order_link_id,
                "emergency_closed": emergency_ok,
            }

        over_margin = max_margin > 0 and actual_margin > max_margin * 1.02 + 0.01
        over_loss = loss_cap > 0 and actual_loss > loss_cap * 1.005 + 1e-6
        if over_margin or over_loss:
            reason = (
                f"실체결 위험캡 초과: margin ${actual_margin:.2f}/${max_margin:.2f}, "
                f"SL ${actual_loss:.4f}/${loss_cap:.4f}"
            )
            emergency_ok = _emergency_market_close(
                ex, fsymbol, close_side, actual_qty, symbol, reason,
            )
            return {
                "ok": False, "qty": actual_qty, "leverage": leverage,
                "error": (
                    f"{reason} — 긴급 청산 완료"
                    if emergency_ok else f"{reason} — 긴급 청산 실패"
                ),
                "entry_order_id": entry_id,
                "entry_order_link_id": entry_order_link_id,
                "emergency_closed": emergency_ok,
            }

        qty = actual_qty
        entry_price = actual_entry
        tp_splits = _split_tps(qty, tps, symbol, exchange=ex)
        if not tp_splits:
            emergency_ok = _emergency_market_close(
                ex, fsymbol, close_side, qty, symbol,
                "실체결 수량 기준 TP 분할 실패",
            )
            return {
                "ok": False, "qty": qty, "leverage": leverage,
                "error": "실체결 수량 TP 분할 실패 — 긴급 청산 처리",
                "emergency_closed": emergency_ok,
            }

        # 진입 직후 즉시 포지션 추적 저장 — SL 실패해도 orphan으로 감지되게 함
        _save_position(
            symbol, direction, entry_price, qty, sl,
            atr=atr, is_elite=is_elite, leverage=leverage,
            entry_order_id=entry_id,
            entry_order_link_id=entry_order_link_id,
            position_meta=position_meta,
        )

        # 3. 손절 (Stop Market, reduceOnly) — 최대 3회 재시도, 실패 시 긴급 시장가 청산
        sl_ok = False
        for sl_attempt in range(3):
            try:
                ex.create_order(
                    fsymbol, "market", close_side, qty,
                    params={
                        "category":         "linear",
                        "stopOrderType":    "StopLoss",
                        "triggerPrice":     str(round(sl, 4)),
                        "triggerDirection": tg_dir,
                        "reduceOnly":       True,
                    }
                )
                sl_ok = True
                break
            except Exception as sl_err:
                print(f"  ⚠️ SL 설정 시도 {sl_attempt+1}/3 실패: {sl_err}")
                if sl_attempt < 2:
                    time.sleep(1.0)

        if not sl_ok:
            # SL 설정 완전 실패 → 즉시 시장가 청산으로 무방비 포지션 제거
            print(f"  ❌ SL 설정 3회 모두 실패 — {symbol} 긴급 시장가 청산 시도")
            try:
                from publisher import send as tg_send_emergency
                tg_send_emergency(
                    f"⚠️ <b>SL 설정 실패 — 긴급 청산</b>\n"
                    f"심볼: {symbol} {direction}\n"
                    f"SL ${sl:,.4f} 설정 3회 실패 → 시장가 청산 실행"
                )
            except Exception:
                pass
            emergency_ok = _emergency_market_close(
                ex, fsymbol, close_side, qty, symbol, "SL 설정 3회 실패",
            )
            if emergency_ok:
                _clear_position(symbol)
            return {
                "ok": False, "qty": qty, "leverage": leverage,
                "error": (
                    "SL 설정 3회 실패 — 긴급 청산 완료"
                    if emergency_ok else
                    "SL 및 긴급 청산 실패 — 추적 유지, 즉시 수동 확인 필요"
                ),
                "emergency_closed": emergency_ok,
            }

        print(f"  ✅ 손절 설정: ${sl:,.4f}")
        time.sleep(0.4)

        # 4. TP 지정가 분할 주문
        labels = ["TP1", "TP2", "TP3"]
        tp_errors = []
        for i, tp_item in enumerate(tp_splits):
            try:
                ex.create_order(
                    fsymbol, "limit", close_side,
                    tp_item["qty"], round(tp_item["price"], 4),
                    params={"category": "linear", "reduceOnly": True}
                )
                print(f"  ✅ {labels[i]}: ${tp_item['price']:,.4f}  수량:{tp_item['qty']}")
            except Exception as tp_err:
                # 110017: 포지션이 이미 청산됨 (SL/TP 선 체결) — 정상 케이스
                if "110017" in str(tp_err):
                    print(f"  ℹ️ {labels[i]}: 포지션 이미 청산됨 (스킵)")
                else:
                    print(f"  ⚠️ {labels[i]} 설정 실패: {tp_err}")
                    tp_errors.append(f"{labels[i]}: {tp_err}")
            time.sleep(0.3)

        if require_full_protection and tp_errors:
            emergency_ok = _emergency_market_close(
                ex, fsymbol, close_side, qty, symbol,
                f"TP 보호주문 설정 실패: {' | '.join(tp_errors)}",
            )
            if emergency_ok:
                try:
                    ex.cancel_all_orders(fsymbol, params={"category": "linear"})
                except Exception as cancel_err:
                    print(f"  ⚠️ {symbol} 긴급청산 후 잔여주문 취소 실패: {cancel_err}")
                _clear_position(symbol)
            return {
                "ok": False, "qty": qty, "leverage": leverage,
                "error": (
                    "TP 설정 실패 — 긴급 청산 완료"
                    if emergency_ok else
                    "TP 설정 및 긴급 청산 실패 — SL/추적 유지, 즉시 수동 확인 필요"
                ),
                "entry_order_id": entry_id,
                "entry_order_link_id": entry_order_link_id,
                "tp_errors": tp_errors,
                "emergency_closed": emergency_ok,
            }

        # 포지션 추적 저장은 SL 설정 전에 이미 완료됨
        print(f"{'='*50}\n")
        return {
            "ok": True,
            "qty": qty,
            "leverage": leverage,
            "error": "",
            "entry_order_id": entry_id,
            "entry_order_link_id": entry_order_link_id,
            "entry_price": entry_price,
            "seed_equity": get_usdt_equity(),
            "free_balance": balance,
            "margin_usd": actual_margin,
            "notional_usd": qty * entry_price,
            "estimated_sl_loss_usd": actual_loss,
            "max_sl_loss_usd": loss_cap,
        }

    except Exception as e:
        err = str(e)
        print(f"  ❌ 주문 오류: {err}")
        emergency_ok = False
        if require_full_protection and entry_live:
            emergency_ok = _emergency_market_close(
                ex, fsymbol, close_side, actual_qty, symbol,
                f"보호주문 구성 중 예외: {err}",
            )
            if emergency_ok:
                _clear_position(symbol)
        print(f"{'='*50}\n")
        return {
            "ok": False, "qty": qty, "leverage": leverage,
            "error": f"{err} — 긴급 청산 완료" if emergency_ok else err,
            "entry_order_id": entry_id,
            "entry_order_link_id": entry_order_link_id,
            "emergency_closed": emergency_ok,
        }


# ─── 포지션 추적 / 손익분기 SL ────────────────────────────────────────────────

def _save_position(symbol: str, direction: str, entry_price: float,
                   qty: float, sl: float, atr: float = 0.0,
                   is_elite: bool = False, leverage: int = 1,
                   entry_order_id: str = "", entry_order_link_id: str = "",
                   position_meta: dict | None = None):
    """진입 정보 저장. ELITE는 TP1 이후 트레일링 스톱 활성화."""
    s = _load_state()
    record = {
        "venue":       VENUE,
        "direction":   direction,
        "entry_price": entry_price,
        "initial_qty": qty,
        "opened_ts":   time.time(),
        "sl_price":    sl,
        "initial_sl_price": sl,
        "be_done":     False,
        "pre_tp_be_done": False,
        "profit_lock_10_done": False,
        "atr":         round(atr, 4),
        "leverage":    int(leverage or 1),
        "entry_order_id": entry_order_id,
        "entry_order_link_id": entry_order_link_id,
        "is_elite":    is_elite,
        "trail_sl":    None,    # ELITE TP1 이후 활성화되는 트레일 SL
    }
    safe_meta = _json_safe(position_meta or {})
    for key in (
        "engine_version", "strategy", "signal_bar", "max_hold_minutes",
        "exit_policy", "divergence_kind", "signal_tier", "setup_timeframe",
        "tp1_lock_r",
        "trail_atr_mult", "trail_activation_r",
        "progress_check_minutes", "progress_min_r",
    ):
        if key in safe_meta:
            record[key] = safe_meta[key]
    s.setdefault("positions", {})[symbol] = record
    _save_state(s)


def _clear_position(symbol: str):
    s = _load_state()
    s.get("positions", {}).pop(symbol, None)
    _save_state(s)


def reconcile_stale_open_history(actual_symbols: set[str] | list[str]) -> list[dict]:
    """Quarantine local ``open`` rows that are neither tracked nor live.

    Older full/fast processes could both observe one close and consume separate
    open rows for the same symbol.  Those duplicate rows must not count as live
    risk or as wins/losses.  We preserve them as ledger anomalies instead of
    inventing a PnL that cannot be attributed safely.
    """
    live = {str(symbol) for symbol in (actual_symbols or [])}
    s = _load_state()
    tracked = set((s.get("positions") or {}).keys())
    quarantined: list[dict] = []
    now_label = datetime.now(KST).strftime("%m/%d %H:%M KST")
    for record in s.get("trade_history", []) or []:
        symbol = str(record.get("symbol") or "")
        if record.get("status") != "open" or symbol in tracked or symbol in live:
            continue
        snapshot = {
            "num": record.get("num"),
            "symbol": symbol,
            "opened_at": record.get("time"),
            "entry_order_id": record.get("entry_order_id", ""),
            "entry_order_link_id": record.get("entry_order_link_id", ""),
            "reason": "local_open_without_tracked_or_exchange_position",
            "detected_at": now_label,
        }
        record["status"] = "ledger_orphan"
        record["closed_at"] = now_label
        record["ledger_reconciliation"] = snapshot
        quarantined.append(snapshot)
    if quarantined:
        s.setdefault("ledger_anomalies", []).extend(quarantined)
        _save_state(s)
        print(f"[원장] 미추적·미보유 open 기록 {len(quarantined)}건 격리")
    return quarantined


def _closed_pnl_entries_for_trade(ex, fsym: str, symbol: str, direction: str,
                                  opened_ts: float, limit: int = 20) -> list[dict]:
    """Bybit 부분익절/잔량청산 내역을 한 거래 단위로 모은다."""
    try:
        sym_clean = fsym.split("/")[0] + "USDT"
        resp = ex.privateGetV5PositionClosedPnl({
            "category": "linear", "symbol": sym_clean, "limit": limit
        })
        entries = resp.get("result", {}).get("list", []) or []
    except Exception as e:
        print(f"[모니터] {symbol} PnL 조회 실패: {e}")
        return []

    close_side = "Buy" if direction == "SHORT" else "Sell"
    opened_ms = int(max(0.0, opened_ts) * 1000) - 10_000
    matched = []
    for item in entries:
        try:
            created_ms = int(float(item.get("createdTime", 0) or 0))
        except Exception:
            created_ms = 0
        if created_ms < opened_ms:
            continue
        if str(item.get("side", "")).lower() != close_side.lower():
            continue
        matched.append(item)
    return sorted(matched, key=lambda x: int(float(x.get("updatedTime", 0) or 0)))


def _aggregate_closed_pnl(entries: list[dict]) -> tuple[float, dict]:
    """부분 청산 여러 건을 하나의 거래 결과로 합산한다."""
    if not entries:
        return 0.0, {}

    total_pnl = 0.0
    total_qty = 0.0
    total_exit_value = 0.0
    total_open_fee = 0.0
    total_close_fee = 0.0
    for item in entries:
        total_pnl += float(item.get("closedPnl", 0) or 0)
        qty = float(item.get("closedSize", item.get("qty", 0)) or 0)
        total_qty += qty
        total_exit_value += float(item.get("cumExitValue", 0) or 0)
        total_open_fee += float(item.get("openFee", 0) or 0)
        total_close_fee += float(item.get("closeFee", 0) or 0)

    avg_exit = total_exit_value / total_qty if total_qty > 0 else 0.0
    latest = entries[-1]
    aggregate = dict(latest)
    aggregate.update({
        "orderType": "Aggregate" if len(entries) > 1 else latest.get("orderType", ""),
        "closedPnl": str(total_pnl),
        "qty": str(total_qty),
        "closedSize": str(total_qty),
        "avgExitPrice": str(avg_exit or latest.get("avgExitPrice", "")),
        "openFee": str(total_open_fee),
        "closeFee": str(total_close_fee),
        "parts": entries,
    })
    return total_pnl, aggregate


def _update_trail_sl(ex, symbol: str, fsym: str, info: dict,
                     current_price: float, current_qty: float,
                     atr_mult: float | None = None):
    """
    트레일링 스톱 갱신.
    현재가에서 atr_mult × ATR 뒤에 SL을 유지하며 방향으로만 이동(래칫).
    ELITE는 타이트(1.5ATR), 일반 러너는 넓게(2.0ATR) — 2026-07-11.
    """
    from publisher import send as tg_send

    direction    = info["direction"]
    trail_atr    = info.get("atr", 0)
    current_sl   = info.get("trail_sl") or info["sl_price"]
    close_side   = "sell" if direction == "LONG" else "buy"
    tg_dir       = 2 if direction == "LONG" else 1
    mult = float(atr_mult if atr_mult is not None else TRAIL_ATR_MULT)

    if trail_atr <= 0 or current_price <= 0:
        return

    new_sl = (
        current_price - trail_atr * mult if direction == "LONG"
        else current_price + trail_atr * mult
    )
    new_sl = round(new_sl, 4)

    # 방향으로만 이동 + 최소 이동량 체크 (래칫)
    if direction == "LONG":
        if new_sl <= current_sl + trail_atr * TRAIL_ADVANCE_MIN:
            return
    else:
        if new_sl >= current_sl - trail_atr * TRAIL_ADVANCE_MIN:
            return

    try:
        ex.cancel_all_orders(fsym, params={"category": "linear", "orderFilter": "StopOrder"})
        time.sleep(0.3)
        ex.create_order(
            fsym, "market", close_side, current_qty,
            params={
                "category":         "linear",
                "stopOrderType":    "StopLoss",
                "triggerPrice":     str(new_sl),
                "triggerDirection": tg_dir,
                "reduceOnly":       True,
            }
        )
        move = "↑" if direction == "LONG" else "↓"
        print(f"[트레일] {symbol} SL {current_sl:,.4f} → {new_sl:,.4f} {move}  (현재가 ${current_price:,.4f})")

        s = _load_state()
        if symbol in s.get("positions", {}):
            s["positions"][symbol]["trail_sl"] = new_sl
            s["positions"][symbol]["sl_price"] = new_sl
        _save_state(s)

        tg_send(
            f"📈 <b>[트레일링 스톱 {move}]</b> {symbol}\n"
            f"SL {current_sl:,.4f} → <b>{new_sl:,.4f}</b>\n"
            f"현재가 ${current_price:,.4f}  |  수익 보호 강화 중"
        )
    except Exception as e:
        print(f"[트레일] {symbol} SL 갱신 실패: {e}")


def monitor_positions():
    """
    스캔마다 호출.
    ① 포지션 청산 감지 → PnL 기록
    ② TP1 체결 감지 → SL 손익분기 이동
    ③ ELITE + be_done → 트레일링 스톱 래칫 갱신
    """
    from publisher import send as tg_send

    s = _load_state()
    tracked = s.get("positions", {})
    if not tracked:
        return

    try:
        ex = _ex()
        ex.load_markets()
    except Exception:
        return

    for symbol, info in list(tracked.items()):
        fsym = _futures_symbol(symbol)

        # 현재 포지션 수량 + 현재가 조회
        try:
            positions    = ex.fetch_positions([fsym], params={"category": "linear"})
            current_qty  = 0.0
            current_price = 0.0
            current_leverage = float(info.get("leverage", 1) or 1)
            for p in positions:
                q = abs(float(p.get("contracts", 0) or 0))
                if q > 0:
                    current_qty   = q
                    current_price = float(p.get("markPrice", 0) or 0)
                    current_leverage = max(float(p.get("leverage", current_leverage) or current_leverage), 1.0)
        except Exception as e:
            print(f"[모니터] {symbol} 조회 실패: {e}")
            continue

        # H1: leverage=None 포지션을 거래소 조회값으로 보정 (수익보호 SL 정상 작동을 위해)
        stored_leverage = info.get("leverage")
        if current_qty > 0 and current_leverage > 1 and (not stored_leverage or int(stored_leverage) < 2):
            try:
                s2 = _load_state()
                if symbol in s2.get("positions", {}):
                    s2["positions"][symbol]["leverage"] = int(current_leverage)
                    _save_state(s2)
                    info["leverage"] = int(current_leverage)
                    print(f"[모니터] {symbol} leverage None → {int(current_leverage)}x 보정")
            except Exception:
                pass

        # ① 포지션 완전 청산 → PnL 기록
        if current_qty <= 0:
            open_record = next(
                (
                    r for r in reversed(s.get("trade_history", []))
                    if r.get("symbol") == symbol and r.get("status") == "open"
                ),
                {},
            )
            opened_ts = float(
                info.get("opened_ts")
                or open_record.get("timestamp", 0)
                or 0
            )
            close_entries = _closed_pnl_entries_for_trade(
                ex, fsym, symbol, info.get("direction", ""), opened_ts
            )
            pnl, close_info = _aggregate_closed_pnl(close_entries)
            record_result(pnl)
            closed_record = _update_trade_result(symbol, pnl, close_info=close_info)
            sign = "+" if pnl >= 0 else ""
            print(f"[모니터] {symbol} 청산 완료 — PnL {sign}${pnl:.2f}")
            if closed_record:
                tg_send(build_trade_close_notification(closed_record))
            _clear_position(symbol)
            continue

        # S1 is a short-horizon engine.  If the expected move has not appeared
        # inside its holding window, exit instead of turning a scalp into an
        # unplanned swing.  A failed exit leaves the SL and local tracking intact.
        max_hold_minutes = float(info.get("max_hold_minutes") or 0.0)
        opened_ts = float(info.get("opened_ts") or 0.0)
        age_seconds = time.time() - opened_ts if opened_ts > 0 else 0.0
        last_time_exit = float(info.get("time_exit_requested_ts") or 0.0)
        if (
            max_hold_minutes > 0
            and age_seconds >= max_hold_minutes * 60
            and (not last_time_exit or time.time() - last_time_exit >= 120)
        ):
            direction = info["direction"]
            close_side = "sell" if direction == "LONG" else "buy"
            try:
                ex.create_order(
                    fsym, "market", close_side, current_qty,
                    params={"category": "linear", "reduceOnly": True},
                )
                time.sleep(0.2)
                try:
                    ex.cancel_all_orders(fsym, params={"category": "linear"})
                except Exception as cancel_err:
                    print(f"[시간청산] {symbol} 잔여주문 취소 실패: {cancel_err}")
                s2 = _load_state()
                if symbol in s2.get("positions", {}):
                    s2["positions"][symbol]["time_exit_requested_ts"] = time.time()
                    _save_state(s2)
                print(
                    f"[시간청산] {symbol} {direction} "
                    f"{age_seconds/60:.0f}분 >= {max_hold_minutes:.0f}분"
                )
                tg_send(
                    f"⏱️ <b>[S1 시간청산]</b> {symbol} {direction}\n"
                    f"보유 {age_seconds/60:.0f}분 — 계획된 "
                    f"{max_hold_minutes:.0f}분 한도 도달, reduce-only 청산"
                )
                continue
            except Exception as time_exit_err:
                print(f"[시간청산] {symbol} 실패 — SL 유지: {time_exit_err}")

        # 레버리지 포함 수익률이 +10%를 넘으면 SL도 +10% 이익권으로 끌어올린다.
        if not info.get("profit_lock_10_done"):
            direction = info["direction"]
            entry_price = float(info.get("entry_price", 0) or 0)
            current_sl = float(info.get("sl_price") or info.get("initial_sl_price") or 0)
            favorable = (
                current_price - entry_price if direction == "LONG"
                else entry_price - current_price
            )
            price_move_pct = favorable / entry_price * 100 if entry_price > 0 else 0.0
            margin_roi_pct = price_move_pct * max(current_leverage, 1.0)
            lock_frac = (PROFIT_LOCK_SL_MARGIN_ROI_PCT / 100) / max(current_leverage, 1.0)
            protect_sl = (
                entry_price * (1 + lock_frac) if direction == "LONG"
                else entry_price * (1 - lock_frac)
            )
            improves_sl = (
                (direction == "LONG" and (current_sl <= 0 or protect_sl > current_sl))
                or (direction == "SHORT" and (current_sl <= 0 or protect_sl < current_sl))
            )
            valid_trigger = (
                (direction == "LONG" and current_price > protect_sl)
                or (direction == "SHORT" and current_price < protect_sl)
            )
            if (
                margin_roi_pct >= PROFIT_LOCK_TRIGGER_MARGIN_ROI_PCT
                and improves_sl
                and valid_trigger
            ):
                close_side = "sell" if direction == "LONG" else "buy"
                tg_dir = 2 if direction == "LONG" else 1
                try:
                    try:
                        protect_sl = float(ex.price_to_precision(fsym, protect_sl))
                    except Exception:
                        protect_sl = round(protect_sl, 4)
                    ex.cancel_all_orders(fsym, params={
                        "category": "linear", "orderFilter": "StopOrder"
                    })
                    time.sleep(0.3)
                    ex.create_order(
                        fsym, "market", close_side, current_qty,
                        params={
                            "category":         "linear",
                            "stopOrderType":    "StopLoss",
                            "triggerPrice":     str(protect_sl),
                            "triggerDirection": tg_dir,
                            "reduceOnly":       True,
                        }
                    )
                    print(
                        f"[+10%락] {symbol} {direction} 증거금ROI {margin_roi_pct:+.1f}% "
                        f"→ SL ${protect_sl:,.4f}"
                    )
                    tg_send(
                        f"🛡️ <b>[+10% 수익락 SL 이동]</b> {symbol} {direction}\n"
                        f"증거금ROI <b>{margin_roi_pct:+.1f}%</b> 도달 → "
                        f"SL <b>${protect_sl:,.4f}</b>\n"
                        f"수익권을 손실 거래로 돌려보내지 않도록 이익 보호"
                    )
                    s = _load_state()
                    if symbol in s.get("positions", {}):
                        s["positions"][symbol]["profit_lock_10_done"] = True
                        s["positions"][symbol]["pre_tp_be_done"] = True
                        s["positions"][symbol]["sl_price"] = protect_sl
                    _save_state(s)
                    info["profit_lock_10_done"] = True
                    info["pre_tp_be_done"] = True
                    info["sl_price"] = protect_sl
                except Exception as e:
                    print(f"[+10%락] {symbol} SL 이동 실패: {e}")

        # TP1 전이라도 충분히 수익권이면 손실 거래로 되돌아가지 않게 SL을 당긴다.
        if not info.get("pre_tp_be_done") and not info.get("be_done"):
            direction = info["direction"]
            entry_price = float(info.get("entry_price", 0) or 0)
            initial_sl = float(info.get("initial_sl_price") or info.get("sl_price") or 0)
            risk = abs(entry_price - initial_sl)
            favorable = (
                current_price - entry_price if direction == "LONG"
                else entry_price - current_price
            )
            if entry_price > 0 and risk > 0 and favorable >= risk * PRE_TP_BE_TRIGGER_R:
                close_side = "sell" if direction == "LONG" else "buy"
                tg_dir = 2 if direction == "LONG" else 1
                fee_buffer = entry_price * ROUND_TRIP_FEE * BE_FEE_CUSHION_MULT
                lock_distance = max(fee_buffer, risk * PRE_TP_BE_LOCK_FRACTION)
                protect_sl = (
                    entry_price + lock_distance if direction == "LONG"
                    else entry_price - lock_distance
                )
                valid_trigger = (
                    (direction == "LONG" and current_price > protect_sl) or
                    (direction == "SHORT" and current_price < protect_sl)
                )
                if valid_trigger:
                    try:
                        try:
                            protect_sl = float(ex.price_to_precision(fsym, protect_sl))
                        except Exception:
                            protect_sl = round(protect_sl, 4)
                        ex.cancel_all_orders(fsym, params={
                            "category": "linear", "orderFilter": "StopOrder"
                        })
                        time.sleep(0.3)
                        ex.create_order(
                            fsym, "market", close_side, current_qty,
                            params={
                                "category":         "linear",
                                "stopOrderType":    "StopLoss",
                                "triggerPrice":     str(protect_sl),
                                "triggerDirection": tg_dir,
                                "reduceOnly":       True,
                            }
                        )
                        print(
                            f"[수익보호] {symbol} {direction} {PRE_TP_BE_TRIGGER_R:.1f}R 도달 "
                            f"→ SL ${protect_sl:,.4f}"
                        )
                        tg_send(
                            f"🛡️ <b>[수익보호 SL 이동]</b> {symbol} {direction}\n"
                            f"{PRE_TP_BE_TRIGGER_R:.1f}R 도달 → SL <b>${protect_sl:,.4f}</b>\n"
                            f"TP1 전 되돌림에도 손실 방어"
                        )
                        s = _load_state()
                        if symbol in s.get("positions", {}):
                            s["positions"][symbol]["pre_tp_be_done"] = True
                            s["positions"][symbol]["sl_price"] = protect_sl
                        _save_state(s)
                    except Exception as e:
                        print(f"[수익보호] {symbol} SL 이동 실패: {e}")

        # ③ TP1 이후 트레일링 — ELITE 타이트 / 일반도 러너 허용(2026-07-11)
        if info.get("be_done"):
            is_elite = info.get("is_elite", False)
            if is_elite or TRAIL_AFTER_TP1_ALL:
                mult = TRAIL_ATR_MULT if is_elite else TRAIL_ATR_MULT_STANDARD
                _update_trail_sl(
                    ex, symbol, fsym, info, current_price, current_qty,
                    atr_mult=mult,
                )
            continue

        # ② TP1 체결 감지: 현재 수량이 초기의 85% 미만
        initial_qty = info.get("initial_qty", 0)
        if initial_qty > 0 and current_qty < initial_qty * 0.85:
            direction   = info["direction"]
            entry_price = float(info["entry_price"])
            close_side  = "sell" if direction == "LONG" else "buy"
            tg_dir      = 2 if direction == "LONG" else 1
            trail_atr   = float(info.get("atr", 0) or 0)
            is_elite    = info.get("is_elite", False)
            initial_sl  = float(info.get("initial_sl_price") or info.get("sl_price") or entry_price)
            risk = abs(entry_price - initial_sl)

            try:
                ex.cancel_all_orders(fsym, params={
                    "category": "linear", "orderFilter": "StopOrder"
                })
                time.sleep(0.3)

                current_sl = float(info.get("sl_price") or entry_price)

                # 잔량 SL: 순수 BE 대신 +POST_TP1_LOCK_R 잠금 (얕은 승리 개선)
                lock_r = max(0.0, float(POST_TP1_LOCK_R))
                lock_sl = (
                    entry_price + risk * lock_r if direction == "LONG"
                    else entry_price - risk * lock_r
                ) if risk > 0 else entry_price

                # ELITE: 초기 트레일 SL (현재가 기준). 이미 유리한 SL은 절대 낮추지 않음.
                if is_elite and trail_atr > 0 and current_price > 0:
                    trail_candidate = (
                        current_price - trail_atr * TRAIL_ATR_MULT
                        if direction == "LONG"
                        else current_price + trail_atr * TRAIL_ATR_MULT
                    )
                    candidates = [entry_price, current_sl, lock_sl, trail_candidate]
                    init_sl = max(candidates) if direction == "LONG" else min(candidates)
                    init_sl = round(init_sl, 4)
                    trail_note = f"트레일링 스톱 시작 ${init_sl:,.4f}"
                else:
                    candidates = [entry_price, current_sl, lock_sl]
                    init_sl = max(candidates) if direction == "LONG" else min(candidates)
                    init_sl = round(init_sl, 4)
                    trail_note = f"잔량 +{lock_r:.2f}R 보호 ${init_sl:,.4f}"

                ex.create_order(
                    fsym, "market", close_side, current_qty,
                    params={
                        "category":         "linear",
                        "stopOrderType":    "StopLoss",
                        "triggerPrice":     str(round(init_sl, 4)),
                        "triggerDirection": tg_dir,
                        "reduceOnly":       True,
                    }
                )

                elite_tag = " 💎 트레일링 모드 진입" if is_elite else " 🏃 러너 트레일 대기"
                print(f"[모니터] {symbol} TP1 체결 → {trail_note}{elite_tag}")
                tg_send(
                    f"🔄 <b>[TP1 체결{'  💎 트레일' if is_elite else '  러너 보호'}]</b> {symbol}\n"
                    f"SL → <b>${init_sl:,.4f}</b>  남은수량 {current_qty}\n"
                    f"{'ELITE: 수익 따라 SL 자동 상향' if is_elite else f'잔량 +{lock_r:.2f}R 잠금 후 트레일'}"
                )

                s = _load_state()
                if symbol in s.get("positions", {}):
                    s["positions"][symbol]["be_done"]  = True
                    s["positions"][symbol]["sl_price"] = init_sl
                    s["positions"][symbol]["trail_sl"] = init_sl
                _save_state(s)

            except Exception as e:
                print(f"[모니터] {symbol} TP1 처리 실패: {e}")


# ─── 거래 이력 관리 ──────────────────────────────────────────────────────────

def _append_trade(symbol: str, direction: str, tf_key: str, strength: str,
                  leverage: int, qty: float, entry_price: float,
                  sl: float, margin: float, **extra) -> int:
    """신규 진입 거래를 이력에 추가. 전체 누적 번호 반환."""
    s = _load_state()
    history = s.setdefault("trade_history", [])
    num = s.get("trade_counter", 0) + 1
    s["trade_counter"] = num
    record = {
        "num":           num,
        "venue":         VENUE,
        "time":          datetime.now(KST).strftime("%m/%d %H:%M KST"),
        "timestamp":     time.time(),
        "symbol":        symbol,
        "direction":     direction,
        "tf":            tf_key,
        "strength":      strength,
        "leverage":      leverage,
        "qty":           qty,
        "entry_price":   entry_price,
        "sl":            sl,
        "margin":        round(margin, 2),
        "status":        "open",
        "pnl_usd":       0.0,
        "closed_at":     None,
        "pyramid_count": 0,       # 불타기 추가 횟수 (최대 2회)
        "pyramid_adds":  [],      # 각 불타기 진입 기록 [{price, margin, time}]
    }
    record.update(_json_safe(extra))
    history.append(record)
    _save_state(s)
    return num


def _first_float(data: dict, keys: list[str]) -> float:
    for key in keys:
        try:
            value = data.get(key)
            if value not in (None, ""):
                return float(value)
        except Exception:
            continue
    return 0.0


def _near_price(a: float, b: float, tolerance_pct: float = 0.25) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / b * 100 <= tolerance_pct


def _infer_exit_reason(record: dict, exit_price: float, pnl_usd: float) -> str:
    close_info = record.get("close_info", {}) or {}
    parts = close_info.get("parts", []) if isinstance(close_info, dict) else []
    if parts:
        has_limit = any(str(p.get("orderType", "")).lower() == "limit" for p in parts)
        has_market = any(str(p.get("orderType", "")).lower() == "market" for p in parts)
        if has_limit and has_market:
            return "TP 부분익절 후 잔량 보호청산"
        if has_limit and pnl_usd > 0:
            return "분할 TP 수익실현"

    if pnl_usd < 0:
        if _near_price(exit_price, float(record.get("sl", 0) or 0), 0.35):
            return "SL 손절"
        return "손실 청산"
    if abs(pnl_usd) < 0.01:
        return "본전/수익보호 청산"

    for i, tp in enumerate(record.get("tps", []) or [], start=1):
        try:
            if _near_price(exit_price, float(tp.get("price", 0) or 0), 0.35):
                return f"TP{i} 수익실현"
        except Exception:
            continue
    return "수익 청산"


def _update_trade_result(symbol: str, pnl_usd: float, close_info: dict | None = None) -> dict | None:
    """해당 심볼의 가장 최근 open 거래에 청산 결과 기록."""
    s = _load_state()
    closed_record = None
    close_info = close_info or {}
    exit_price = _first_float(close_info, [
        "avgExitPrice", "exitPrice", "closedPrice", "price", "markPrice",
    ])
    for record in reversed(s.get("trade_history", [])):
        if record["symbol"] == symbol and record["status"] == "open":
            record["status"]    = "win" if pnl_usd > 0 else ("loss" if pnl_usd < 0 else "breakeven")
            record["pnl_usd"]   = round(pnl_usd, 4)
            record["closed_at"] = datetime.now(KST).strftime("%m/%d %H:%M KST")
            if exit_price > 0:
                record["exit_price"] = exit_price
            if close_info:
                record["close_info"] = _json_safe(close_info)
            record["exit_reason"] = _infer_exit_reason(record, exit_price, pnl_usd)
            # 청산 직후 포스트모템 (승/패 원인 후보 + 레짐/로직 태그)
            try:
                from postmortem import build_and_save_postmortem
                pm = build_and_save_postmortem(record)
                record["postmortem"] = pm
            except Exception as e:
                print(f"  [포스트모템] 생성 실패: {e}")
                pm = None
            closed_record = record
            break
    _save_state(s)
    if closed_record:
        log_execution_journal(
            closed_record.get("num"), "closed",
            symbol=closed_record.get("symbol"),
            tf=closed_record.get("tf"),
            strategy=closed_record.get("strategy", ""),
            strategy_family=closed_record.get("strategy_family", ""),
            core_strategy=closed_record.get("core_strategy", ""),
            strategy_mode=closed_record.get("strategy_mode", ""),
            asymmetric_mode=closed_record.get("asymmetric_mode", False),
            direction=closed_record.get("direction"),
            status=closed_record.get("status"),
            pnl_usd=closed_record.get("pnl_usd"),
            entry_price=closed_record.get("entry_price"),
            sl=closed_record.get("sl"),
            tps=closed_record.get("tps", []),
            exit_price=closed_record.get("exit_price", 0),
            exit_reason=closed_record.get("exit_reason", ""),
            entry_reasons=closed_record.get("entry_reasons", []),
            postmortem=closed_record.get("postmortem"),
            logic_stack_version=closed_record.get("logic_stack_version")
                or (closed_record.get("entry_context") or {}).get("logic_stack_version"),
            logic_attribution=closed_record.get("logic_attribution")
                or (closed_record.get("entry_context") or {}).get("logic_attribution"),
        )
    return closed_record


def get_recent_trades(hours: int = 6) -> list:
    """최근 N시간 내 거래 이력 반환."""
    cutoff = time.time() - hours * 3600
    return [t for t in _load_state().get("trade_history", [])
            if t.get("timestamp", 0) >= cutoff]


def get_today_trades() -> list:
    """오늘(KST 자정 이후) 거래 이력 반환."""
    today_str = _today_kst()
    history = _load_state().get("trade_history", [])
    result = []
    for t in history:
        ts = t.get("timestamp", 0)
        trade_day = datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d") if ts else ""
        if trade_day == today_str:
            result.append(t)
    return result


def get_cumulative_stats() -> dict:
    """
    전체 누적 거래 통계 반환.
    Returns: {
      total, wins, losses, open_cnt,
      total_pnl, avg_win, avg_loss, profit_factor,
      win_rate, max_win, max_loss,
      best_trade, worst_trade,
      max_consec_win, max_consec_loss,
      cur_consec_win, cur_consec_loss,
    }
    """
    history = _load_state().get("trade_history", [])
    closed  = [t for t in history if t["status"] in ("win", "loss", "breakeven")]
    wins    = [t for t in closed if t["status"] == "win"]
    losses  = [t for t in closed if t["status"] == "loss"]
    opens   = [t for t in history if t["status"] == "open"]

    total_pnl  = round(sum(t["pnl_usd"] for t in closed), 2)
    avg_win    = round(sum(t["pnl_usd"] for t in wins)   / max(len(wins), 1), 2)
    avg_loss   = round(sum(t["pnl_usd"] for t in losses) / max(len(losses), 1), 2)
    pf_denom   = abs(avg_loss) * max(len(losses), 1)
    pf_numer   = avg_win      * max(len(wins), 1)
    profit_factor = round(pf_numer / pf_denom, 2) if pf_denom > 0 else 0.0

    best_trade  = max(closed, key=lambda t: t["pnl_usd"]) if closed else None
    worst_trade = min(closed, key=lambda t: t["pnl_usd"]) if closed else None

    # 연속 승/패 계산
    max_cw = max_cl = cur_cw = cur_cl = 0
    running_cw = running_cl = 0
    for t in closed:
        if t["status"] == "win":
            running_cw += 1
            running_cl  = 0
        elif t["status"] == "loss":
            running_cl += 1
            running_cw  = 0
        else:
            running_cw = running_cl = 0
        max_cw = max(max_cw, running_cw)
        max_cl = max(max_cl, running_cl)
    cur_cw = running_cw
    cur_cl = running_cl

    wr = round(len(wins) / max(len(closed), 1) * 100, 1)

    return {
        "total":          len(history),
        "closed":         len(closed),
        "wins":           len(wins),
        "losses":         len(losses),
        "open_cnt":       len(opens),
        "total_pnl":      total_pnl,
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "profit_factor":  profit_factor,
        "win_rate":       wr,
        "best_trade":     best_trade,
        "worst_trade":    worst_trade,
        "max_consec_win":  max_cw,
        "max_consec_loss": max_cl,
        "cur_consec_win":  cur_cw,
        "cur_consec_loss": cur_cl,
    }


def add_trade_context(trade_num: int, **ctx):
    """거래 번호에 분석용 컨텍스트 추가 (ema_trend, confirmed_count, vol_ratio 등)."""
    s = _load_state()
    for record in s.get("trade_history", []):
        if record["num"] == trade_num:
            record.update(_json_safe(ctx))
            break
    _save_state(s)


def _directional_move_pct(entry_price: float, target_price: float, direction: str) -> float:
    if entry_price <= 0 or target_price <= 0:
        return 0.0
    if direction == "LONG":
        return (target_price - entry_price) / entry_price * 100
    return (entry_price - target_price) / entry_price * 100


def _fmt_signed_usd(value: float) -> str:
    return f"+${value:.2f}" if value >= 0 else f"-${abs(value):.2f}"


def _trade_expectation(symbol: str, direction: str, leverage: int,
                       qty: float, entry_price: float, sl: float,
                       tps: list, est_sl_loss: float = 0.0) -> dict:
    """진입 당시 사용자가 이해할 수 있는 기대수익/위험 시나리오."""
    tp_details = []
    weighted_net_pct = 0.0
    weighted_gross_pct = 0.0
    total_expected_usd = 0.0
    fee_pct = ROUND_TRIP_FEE * 100

    for i, tp in enumerate(tps or [], start=1):
        price = float(tp.get("price", 0) or 0)
        pct_weight = float(tp.get("pct", 0) or 0) / 100
        gross_pct = _directional_move_pct(entry_price, price, direction)
        net_pct = gross_pct - fee_pct
        tp_qty = qty * pct_weight
        expected_usd = tp_qty * entry_price * (net_pct / 100)
        weighted_gross_pct += gross_pct * pct_weight
        weighted_net_pct += net_pct * pct_weight
        total_expected_usd += expected_usd
        tp_details.append({
            "idx": i,
            "price": price,
            "pct": int(tp.get("pct", 0) or 0),
            "gross_pct": gross_pct,
            "net_pct": net_pct,
            "margin_roi_pct": net_pct * leverage,
            "expected_usd": expected_usd,
            "rr": tp.get("rr", 0),
        })

    sl_pct_calc = abs(_directional_move_pct(entry_price, sl, direction))
    if est_sl_loss <= 0 and qty > 0 and entry_price > 0:
        est_sl_loss = qty * entry_price * ((sl_pct_calc / 100) + ROUND_TRIP_FEE)

    return {
        "tp_details": tp_details,
        "weighted_gross_pct": weighted_gross_pct,
        "weighted_net_pct": weighted_net_pct,
        "weighted_margin_roi_pct": weighted_net_pct * leverage,
        "expected_usd": total_expected_usd,
        "sl_pct": sl_pct_calc,
        "est_sl_loss": est_sl_loss,
    }


def _strategy_plain_summary(strategy: str, signal_type: str,
                            is_divergence: bool, direction: str) -> str:
    dir_word = "상승" if direction == "LONG" else "하락"
    profile = classify_strategy(strategy, signal_type, is_divergence, direction)
    label = profile.get("strategy_label", "")
    if profile.get("family_key") == "btc_macro_short":
        return "BTC 월봉/주봉/일봉 하락 우위에서 롱은 배제하고 큰 추세 방향 숏만 노린 전용 진입"
    if profile.get("family_key") == "btc_sync":
        if "평균회귀" in label:
            return f"BTC 베타 대비 과도한 가격 이탈이 되돌기 시작한 구간에서 단기 {dir_word} 평균회귀를 노린 진입"
        return f"BTC 대비 가격 괴리와 거래량 확장을 이용해 단기 {dir_word} 모멘텀을 별도 전략으로 포착한 진입"
    if label == "거래량 급등 추세":
        return f"거래량 급등과 구조 돌파가 동시에 발생해 단기 {dir_word} 추세 가속을 노린 진입"
    if label == "BB 중단 내림롱":
        return "주봉+3일봉이 BB 중단 위에서 유지되는 강세 종목의 눌림롱 진입"
    if label == "구조 돌파":
        return f"명확한 지지/저항 구조 돌파로 {dir_word} 추세 가속에 합류한 진입"
    if label == "역추세 반전":
        return f"상위 흐름과 반대지만 고확신 반전 근거가 겹친 {dir_word} 반전 시도"
    if is_divergence:
        if signal_type.startswith("hidden_"):
            return f"히든 다이버전스로 기존 {dir_word} 추세 지속/재개를 노린 진입"
        return f"일반 다이버전스로 단기 {dir_word} 반전 가능성을 노린 진입"
    if "EMA" in strategy:
        return f"EMA 추세 방향 눌림/되돌림 이후 {dir_word} 재개를 노린 진입"
    if "마이크로" in strategy:
        return f"최근 미세 구조 돌파로 단기 {dir_word} 추세 합류를 노린 진입"
    if "BB" in strategy:
        return f"볼린저밴드 구조와 변동성 확장을 이용한 {dir_word} 추세 진입"
    if "RSI" in strategy:
        return f"RSI 극단값 이후 단기 {dir_word} 반전을 노린 진입"
    return f"{strategy or '자동매매'} 조건 충족으로 {dir_word} 방향 기대"


def _indicator_line(entry_context: dict | None) -> str:
    snap = (entry_context or {}).get("indicator_snapshot", {}) or {}
    if not snap:
        return ""
    order = ["rsi", "cci", "macd", "obv", "srsi", "vol", "cvd"]
    labels = {
        "rsi": "RSI", "cci": "CCI", "macd": "MACD", "obv": "OBV",
        "srsi": "SRSI", "vol": "VOL", "cvd": "CVD",
    }
    bits = []
    for key in order:
        item = snap.get(key, {}) or {}
        ok = "OK" if item.get("ok") else "NO"
        value = item.get("value")
        suffix = f"({value})" if value not in (None, "") else ""
        bits.append(f"{labels[key]} {ok}{suffix}")
    return " / ".join(bits)


def _holding_time(record: dict) -> str:
    ts = float(record.get("timestamp", 0) or 0)
    if ts <= 0:
        return "-"
    seconds = max(0, int(time.time() - ts))
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}분"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}시간 {rem}분"


def _loss_diagnostics(record: dict) -> list[str]:
    """청산 후 사용자가 이해할 수 있는 실패 원인 후보."""
    reasons = []
    direction = record.get("direction", "")
    ema = record.get("ema_trend")
    vol = float(record.get("vol_ratio", 0) or 0)
    bars = int(record.get("bars_ago", 0) or 0)
    tp1_rr = float(record.get("tp1_rr", 0) or 0)
    best_rr = float(record.get("best_rr", 0) or 0)
    strategy = record.get("strategy", "")
    exit_reason = record.get("exit_reason", "손실 청산")

    reasons.append(f"{exit_reason}: 진입 가정이 TP1 도달 전에 무효화됨")

    if (direction == "LONG" and ema == -1) or (direction == "SHORT" and ema == 1):
        reasons.append("EMA 큰 흐름과 반대 방향 진입이라 되돌림 압력이 강했을 가능성")
    elif ema == 0:
        reasons.append("EMA 중립 구간이라 방향성이 충분히 확정되지 않았을 가능성")

    if vol and vol < 1.5:
        reasons.append(f"진입 당시 거래량 {vol:.1f}x로 후속 추세를 밀어줄 힘이 약했을 가능성")
    elif vol and vol >= 3.0:
        reasons.append(f"거래량은 {vol:.1f}x로 충분했지만, 돌파 후 추격 매수/매도가 이어지지 않음")

    if bars >= 8:
        reasons.append(f"신호가 {bars}봉 전 기준이라 실제 진입 시점에는 신선도가 떨어졌을 가능성")

    if tp1_rr and tp1_rr < 1.0:
        reasons.append(f"TP1 R:R이 1:{tp1_rr:.1f}로 낮아, 작은 흔들림에도 손익비가 불리했음")
    elif best_rr and best_rr < 1.5:
        reasons.append(f"최대 R:R이 1:{best_rr:.1f}로 낮아, 진입 대비 기대 보상이 제한적이었음")

    if record.get("fast_exit"):
        reasons.append(f"{strategy}는 15분봉 빠른 확정 전략인데, TP1 전 추진력이 부족했음")

    if len(reasons) == 1:
        reasons.append("기록상 조건 문제는 크지 않아 시장 노이즈/급반전 가능성이 큼")
    return reasons[:5]


def build_trade_close_notification(record: dict) -> str:
    """실제 청산 후 결과와 실패/성공 원인을 설명하는 텔레그램 메시지."""
    status = record.get("status", "")
    symbol = record.get("symbol", "")
    coin = symbol.split("/")[0] if symbol else ""
    direction = record.get("direction", "")
    pnl = float(record.get("pnl_usd", 0) or 0)
    entry_price = float(record.get("entry_price", 0) or 0)
    exit_price = float(record.get("exit_price", 0) or 0)
    sl = float(record.get("sl", 0) or 0)
    leverage = int(record.get("leverage", 1) or 1)
    qty = float(record.get("qty", 0) or 0)
    tps = record.get("tps", []) or []
    ctx = record.get("entry_context", {}) or {}
    profile = ctx.get("strategy_profile") or classify_strategy(
        record.get("strategy", ""),
        record.get("signal_type", ""),
        bool(record.get("is_divergence", True)),
        direction,
        ctx,
        bool(record.get("asymmetric_mode", False)),
    )
    expectation = _trade_expectation(
        symbol, direction, leverage, qty, entry_price, sl, tps,
        float(record.get("est_sl_loss", 0) or 0),
    )
    exit_move = _directional_move_pct(entry_price, exit_price, direction) if exit_price else 0.0
    pnl_label = "이익" if status == "win" else "손실" if status == "loss" else "본전"
    header_icon = "✅" if status == "win" else "❌" if status == "loss" else "〰"
    venue = venue_label(record.get("venue", VENUE))

    lines = [
        f"{header_icon} <b>[{venue} 매매 종료 #{record.get('num')}] {coin} {direction} {pnl_label}</b>",
        f"결과: <b>{_fmt_signed_usd(pnl)}</b>  |  사유: {escape(record.get('exit_reason', '청산'))}",
        f"보유시간: {_holding_time(record)}  |  전략: {escape(str(record.get('strategy', '')))} / {record.get('tf','')}",
        f"전략군: <b>{escape(format_profile(profile))}</b>",
        "",
        "📌 <b>계획 대비 결과</b>",
        f"   진입 {_fmt_price(entry_price)} → 청산 {_fmt_price(exit_price) if exit_price else '가격 확인불가'}"
        + (f"  ({exit_move:+.2f}%)" if exit_price else ""),
        f"   원래 기대: 전체 목표 가중 <b>{expectation['weighted_net_pct']:+.2f}%</b>"
        f" / 약 <b>{_fmt_signed_usd(expectation['expected_usd'])}</b>",
        f"   허용 손실: SL {_fmt_price(sl)}"
        f" / 약 <b>-${expectation['est_sl_loss']:.2f}</b>",
    ]

    if tps:
        tp1 = tps[0]
        tp1_pct = _directional_move_pct(entry_price, float(tp1.get("price", 0) or 0), direction)
        lines.append(
            f"   1차 목표: {_fmt_price(tp1.get('price', 0))} ({tp1_pct:+.2f}%)"
        )

    lines += [
        "",
        "🧠 <b>진입 당시 판단</b>",
        f"   {_strategy_plain_summary(record.get('strategy',''), record.get('signal_type',''), bool(record.get('is_divergence', True)), direction)}",
    ]
    for reason in (record.get("entry_reasons") or ctx.get("reasons") or [])[:4]:
        lines.append(f"   • {escape(str(reason))}")

    pm = record.get("postmortem")
    if isinstance(pm, dict) and pm.get("causes"):
        lines += ["", f"🔎 <b>{escape(str(pm.get('headline') or '포스트모템'))}</b>"]
        if pm.get("r_multiple") is not None:
            lines.append(
                f"   R배수: <b>{pm['r_multiple']:+.2f}R</b>  |  보유 {pm.get('hold_minutes')}분"
            )
        for c in (pm.get("causes") or [])[:4]:
            lines.append(f"   • {escape(str(c.get('text') or ''))}")
        if pm.get("lessons"):
            lines.append("")
            lines.append("💡 <b>교훈</b>")
            for lesson in pm["lessons"][:3]:
                lines.append(f"   • {escape(str(lesson))}")
        la = pm.get("logic_attribution") or {}
        if la.get("summary_ko"):
            lines.append("")
            lines.append(f"🏷 {escape(str(la['summary_ko']))}")
    elif status == "loss":
        lines += ["", "🔎 <b>실패 원인 추정</b>"]
        for reason in _loss_diagnostics(record):
            lines.append(f"   • {escape(reason)}")
    elif status == "win":
        lines += [
            "",
            "🔎 <b>성공 원인</b>",
            "   • 진입 가정이 유효했고 가격이 목표 방향으로 먼저 이동함",
            "   • 다음 복기에서는 어떤 조건이 반복 가능한 승리 패턴인지 기록",
        ]
    else:
        lines += [
            "",
            "🔎 <b>본전 처리</b>",
            "   • 수익보호 또는 손익분기 방어가 작동해 큰 손실을 피한 거래",
        ]

    lines.append("")
    lines.append("🧾 trade_history + journal + <b>trade_postmortem.jsonl</b> 저장")
    return "\n".join(lines)


def build_trade_close_summary(record: dict, analysis_sent: bool = False) -> str:
    """결산/매매내역 방에 남기는 짧은 청산 결과."""
    status = record.get("status", "")
    icon = "✅" if status == "win" else "❌" if status == "loss" else "〰"
    label = "이익" if status == "win" else "손실" if status == "loss" else "본전"
    symbol = record.get("symbol", "")
    coin = symbol.split("/")[0] if symbol else ""
    entry_price = float(record.get("entry_price", 0) or 0)
    exit_price = float(record.get("exit_price", 0) or 0)
    pnl = float(record.get("pnl_usd", 0) or 0)
    move_pct = _directional_move_pct(entry_price, exit_price, record.get("direction", ""))
    ctx = record.get("entry_context", {}) or {}
    profile = ctx.get("strategy_profile") or classify_strategy(
        record.get("strategy", ""),
        record.get("signal_type", ""),
        bool(record.get("is_divergence", True)),
        record.get("direction", ""),
        ctx,
        bool(record.get("asymmetric_mode", False)),
    )
    venue = venue_label(record.get("venue", VENUE))
    lines = [
        f"{icon} <b>[{venue} 매매 종료 #{record.get('num')}] {coin} {record.get('direction','')} {label}</b>",
        f"결과: <b>{_fmt_signed_usd(pnl)}</b>  |  사유: {escape(record.get('exit_reason', '청산'))}",
        f"전략: {escape(str(record.get('strategy', '')))} / {record.get('tf','')}  |  "
        f"전략군: <b>{escape(format_profile(profile))}</b>",
        f"진입 {_fmt_price(entry_price)} → 청산 {_fmt_price(exit_price) if exit_price else '가격 확인불가'}"
        + (f"  ({move_pct:+.2f}%)" if exit_price else ""),
        "📌 상세 청산 분석도 갓오브트레이딩으로 발송" if analysis_sent
        else "📌 갓오브트레이딩 매매봇 미설정 — .env에 TRADE_BOT_TOKEN / TRADE_CHAT_ID 필요",
    ]
    return "\n".join(lines)


def build_trade_notification(symbol: str, direction: str, leverage: int,
                              qty: float, entry_price: float,
                              sl: float, tps: list, balance: float,
                              tf_key: str = "", strength: str = "",
                              strategy: str = "", trade_num: int | None = None,
                              reasons: list[str] | None = None,
                              timing_note: str = "", rr: float = 0.0,
                              risk_pct: float = 0.0,
                              est_sl_loss: float = 0.0,
                              sl_pct: float = 0.0,
                              signal_type: str = "",
                              confirmed_count: int = 0,
                              divergence_count: int = 0,
                              is_divergence: bool = True,
                              entry_context: dict | None = None) -> str:
    """텔레그램 자동매매 체결 알림 메시지."""
    coin      = symbol.split("/")[0]
    now       = datetime.now(KST).strftime("%m/%d %H:%M KST")
    margin    = round(qty * entry_price / leverage, 2)
    emoji     = "🟢" if direction == "LONG" else "🔴"
    dir_label = "롱 LONG" if direction == "LONG" else "숏 SHORT"
    title_num = f" #{trade_num}" if trade_num else ""
    expectation = _trade_expectation(
        symbol, direction, leverage, qty, entry_price, sl, tps, est_sl_loss
    )
    if sl_pct <= 0:
        sl_pct = expectation["sl_pct"]

    icons = ["🥇", "🥈", "🥉", "🎯"]
    tp_lines = []
    for detail in expectation["tp_details"][:4]:
        icon = icons[detail["idx"] - 1] if detail["idx"] - 1 < len(icons) else "🎯"
        rr_txt = f"  R:R 1:{detail['rr']}" if detail.get("rr") else ""
        tp_lines.append(
            f"   {icon} TP{detail['idx']} [{detail['pct']}%]  "
            f"<b>{_fmt_price(detail['price'])}</b>"
            f"  목표 {detail['gross_pct']:+.2f}% / 순 {detail['net_pct']:+.2f}%"
            f" / 예상 {_fmt_signed_usd(detail['expected_usd'])}{rr_txt}"
        )
    if not tp_lines:
        tp_lines.append("   TP 미설정")

    reason_lines = []
    for reason in (reasons or [])[:6]:
        if reason:
            reason_lines.append(f"   • {escape(str(reason))}")
    if not reason_lines:
        reason_lines.append("   • 자동매매 조건 충족")

    meta_bits = [bit for bit in [tf_key, strategy, strength] if bit]
    quality_bits = []
    if divergence_count:
        label = "다이버전스" if is_divergence else "보조조건"
        quality_bits.append(f"{label} {divergence_count}")
    if confirmed_count:
        quality_bits.append(f"확인 {confirmed_count}")
    if signal_type:
        quality_bits.append(signal_type)

    strategy_summary = _strategy_plain_summary(
        strategy, signal_type, is_divergence, direction
    )
    indicator_line = _indicator_line(entry_context)
    profile = (entry_context or {}).get("strategy_profile") or classify_strategy(
        strategy, signal_type, is_divergence, direction, entry_context,
        bool((entry_context or {}).get("asymmetric_mode", False)),
    )

    risk_line = (
        f"예상 SL손실 ~${expectation['est_sl_loss']:.2f}"
        if expectation["est_sl_loss"] > 0 else "예상 SL손실 계산값 없음"
    )
    if risk_pct > 0:
        risk_line += f"  |  계좌위험 {risk_pct * 100:.2f}%"
    if rr > 0:
        risk_line += f"  |  최대 R:R 1:{rr}"

    lines = [
        f"✅ <b>[{venue_label()} 매매 체결되었습니다{title_num}] {coin} {dir_label}</b>",
        f"{emoji} 방향: <b>{dir_label}</b>  |  시간: {now}",
    ]
    if meta_bits:
        lines.append(f"전략: {' / '.join(escape(str(bit)) for bit in meta_bits)}")
    lines.append(f"전략군: <b>{escape(format_profile(profile))}</b>")
    if quality_bits:
        lines.append(f"신뢰도: {' | '.join(escape(str(bit)) for bit in quality_bits)}")

    lines += [
        "",
        "🧠 <b>진입 근거</b>",
        f"   • {escape(strategy_summary)}",
        *reason_lines,
    ]
    if timing_note:
        lines.append(f"   • 하위봉 타점: {escape(timing_note)}")
    if indicator_line:
        lines.append(f"   • 지표 스냅샷: {escape(indicator_line)}")

    lines += [
        "",
        "📈 <b>기대 수익 / 리스크</b>",
        f"   목표 전체 체결 시: <b>{expectation['weighted_net_pct']:+.2f}%</b>"
        f" / 약 <b>{_fmt_signed_usd(expectation['expected_usd'])}</b>",
        f"   증거금 기준 기대 ROI: <b>{expectation['weighted_margin_roi_pct']:+.1f}%</b>",
        f"   실패 기준: SL 도달 시 <b>-{sl_pct:.2f}%</b>"
        f" / 약 <b>-${expectation['est_sl_loss']:.2f}</b>",
        "   판단: TP1 전 0.8R 도달 시 수익보호 SL 이동, TP1 체결 후 손익분기/트레일 보호",
    ]

    lines += [
        "",
        "📌 <b>가격 계획</b>",
        f"   💵 진입가: <b>{_fmt_price(entry_price)}</b>",
        f"   🛑 손절가: <b>{_fmt_price(sl)}</b>"
        + (f"  (-{sl_pct:.2f}%)" if sl_pct > 0 else ""),
        *tp_lines,
        "",
        "💼 <b>포지션</b>",
        f"   레버리지 <b>{leverage}x</b>  |  수량 {qty}",
        f"   증거금 ~${margin:.2f}  |  잔고 ${balance:.2f}",
        f"   {escape(risk_line)}",
        "",
        "🧾 이 체결 근거는 trade_history + execution_journal에 저장됨",
    ]
    return "\n".join(lines)


def _fmt_price(price: float) -> str:
    """가격대에 맞춰 알림용 소수점을 조정한다."""
    p = abs(float(price))
    if p >= 1000:
        digits = 2
    elif p >= 100:
        digits = 3
    elif p >= 1:
        digits = 4
    elif p >= 0.01:
        digits = 6
    else:
        digits = 8
    return f"${float(price):,.{digits}f}"


# ─── 불타기(Pyramid) 지원 ────────────────────────────────────────────────────

def get_open_positions_detail() -> list[dict]:
    """
    오픈 포지션 상세 반환.
    불타기 조건 확인용 — entry_price, direction, sl, atr, pyramid_count 포함.
    """
    return [
        t for t in _load_state().get("trade_history", [])
        if t.get("status") == "open"
    ]


def can_pyramid(symbol: str, tf_key: str) -> tuple[bool, str]:
    """
    특정 심볼+TF의 오픈 포지션이 불타기 가능한지 확인.
    반환: (가능여부, 이유)
    """
    positions = get_open_positions_detail()
    for p in positions:
        if p["symbol"] == symbol and p["tf"] == tf_key:
            count = p.get("pyramid_count", 0)
            if count >= 2:
                return False, f"불타기 최대 2회 도달 ({count}/2)"
            return True, f"불타기 {count+1}회차 가능"
    return False, "오픈 포지션 없음"


def add_pyramid_entry(symbol: str, tf_key: str,
                      add_price: float, add_margin: float, add_qty: float) -> bool:
    """
    오픈 포지션에 불타기 진입 기록 추가.
    pyramid_count 증가 + pyramid_adds 리스트 업데이트.
    반환: 성공 여부
    """
    s = _load_state()
    for record in reversed(s.get("trade_history", [])):
        if record["symbol"] == symbol and record["tf"] == tf_key and record["status"] == "open":
            record["pyramid_count"] = record.get("pyramid_count", 0) + 1
            record.setdefault("pyramid_adds", []).append({
                "level":  record["pyramid_count"],
                "price":  round(add_price, 4),
                "margin": round(add_margin, 2),
                "qty":    add_qty,
                "time":   datetime.now(KST).strftime("%m/%d %H:%M KST"),
            })
            # 가중평균 진입가 업데이트
            orig_margin = record.get("margin", 0)
            total_margin = orig_margin + sum(a["margin"] for a in record["pyramid_adds"])
            orig_price = record["entry_price"]
            record["avg_entry"] = round(
                (orig_price * orig_margin + add_price * add_margin) / max(orig_margin + add_margin, 1e-9),
                4
            )
            _save_state(s)
            return True
    return False


def build_pyramid_notification(symbol: str, direction: str, tf_key: str,
                                level: int, entry_price: float,
                                add_margin: float, profit_atr: float,
                                balance: float) -> str:
    """불타기 진입 텔레그램 알림 메시지."""
    coin  = symbol.split("/")[0]
    now   = datetime.now(KST).strftime("%m/%d %H:%M KST")
    emoji = "🔺" if direction == "LONG" else "🔻"
    lvl_icon = ["", "🥈", "🥉"][min(level, 2)]

    return (
        f"🔥 <b>[불타기 {level}회] {coin} {direction}</b>  {now}\n"
        f"\n"
        f"{lvl_icon} 추가진입가: ≈${entry_price:,.2f}\n"
        f"{emoji} 현재 수익: +{profit_atr:.1f} ATR 진행 중\n"
        f"💼 추가 증거금: ~${add_margin:.1f}  |  잔고: ${balance:.2f}\n"
        f"📐 규칙: 1회 +1.5ATR / 2회 +3.0ATR 도달 시 진입\n"
        f"⚠️ 기존 SL 유지 — 전체 포지션 손익분기 이상 확보 후 트레일링"
    )
