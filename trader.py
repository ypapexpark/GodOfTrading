"""
자동매매 모듈 — Bybit V5 USDT 영구 선물
리스크: 잔고의 20% / 최대 $10 증거금 / 일손실 $10 한도 / 3연패 4시간 중단
"""
from __future__ import annotations
import os
import json
import time
import math
from html import escape
from pathlib import Path
from datetime import datetime, timezone, timedelta

import ccxt
from dotenv import load_dotenv
from config import (CANDIDATE_LOG_FILE, EXECUTION_JOURNAL_FILE,
                    DRAWDOWN_HARD_STOP_PCT, DRAWDOWN_PAUSE_HOURS,
                    DRAWDOWN_RISK_OFF_PCT, DRAWDOWN_WARN_PCT,
                    MAX_DAILY_LOSS_PCT, ROUND_TRIP_FEE,
                    MIN_QTY_MAP, QTY_STEP_MAP)

load_dotenv(Path(__file__).parent / ".env")

KST        = timezone(timedelta(hours=9))
STATE_FILE = Path(__file__).parent / "trade_state.json"
CANDIDATE_FILE = Path(__file__).parent / CANDIDATE_LOG_FILE
EXECUTION_JOURNAL = Path(__file__).parent / EXECUTION_JOURNAL_FILE

# ─── 리스크 파라미터 (100억 프로젝트 — 공격적 복리 성장) ─────────────────────
TRADE_MARGIN_PCT      = 0.25   # 스윙 기본 비율 (강도별 override 됨)
SCALP_MARGIN_PCT      = 0.13   # 스캘핑 기본 비율
MAX_MARGIN_USD        = 120.0  # 50 → 120: 복리 성장 시 자동 스케일업 허용
MAX_SCALP_MARGIN_USD  = 60.0   # 25 → 60
MIN_BALANCE_USD  = 15.0   # 20 → 15: 잔고가 줄어도 기회 있으면 진입
MAX_DAILY_LOSS   = 30.0   # 20 → 30: 하루 최대 손실 (~33% of $90)
MAX_CONSEC_LOSS  = 3      # 3연패 후 일시중단 유지
PAUSE_HOURS      = 12     # 4 → 12: 손실 구간에서는 재진입보다 냉각이 우선
MAX_LEVERAGE     = 30     # 25 → 30: 황금 진입 시 고레버리지 허용
MAX_CONCURRENT   = 4      # 최대 동시 오픈 포지션 수 (자본 집중 원칙)
MAX_MARGIN_PCT_CAP       = 0.55
MAX_SCALP_MARGIN_PCT_CAP = 0.25

# ─── 트레일링 스톱 파라미터 (ELITE 전용) ─────────────────────────────────────
TRAIL_ATR_MULT    = 1.5   # 현재가에서 SL까지 ATR 거리
TRAIL_ADVANCE_MIN = 0.5   # SL 갱신 최소 이동량 (ATR 단위), 너무 자주 갱신 방지

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
    return {"daily_loss": 0.0, "consec_loss": 0, "pause_until": 0, "last_reset": ""}


def _save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


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
    계좌 전체 드로우다운 방어.
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

    drawdown = max(0.0, (peak - equity) / peak) if peak > 0 else 0.0
    s["drawdown_pct"] = round(drawdown * 100, 2)
    s["max_drawdown_pct"] = max(
        float(s.get("max_drawdown_pct", 0) or 0),
        s["drawdown_pct"],
    )

    if drawdown >= DRAWDOWN_HARD_STOP_PCT:
        until_ts = time.time() + DRAWDOWN_PAUSE_HOURS * 3600
        s["pause_until"] = max(float(s.get("pause_until", 0) or 0), until_ts)
        s["drawdown_status"] = "hard_stop"
        until = datetime.fromtimestamp(s["pause_until"], KST).strftime("%m/%d %H:%M")
        return False, (
            f"계좌 드로우다운 {drawdown*100:.1f}% >= {DRAWDOWN_HARD_STOP_PCT*100:.0f}% "
            f"— {until} KST까지 신규매매 중단"
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
    """후보/차단/체결 이벤트를 JSONL로 저장해 사후 통계 검증에 사용한다."""
    candidate_id = extra.pop("candidate_id", None)
    if not candidate_id:
        ts_ms = int(time.time() * 1000)
        safe_symbol = symbol.replace("/", "").replace(":", "")
        candidate_id = f"{ts_ms}-{safe_symbol}-{tf_key}-{status}"
    row = {
        "candidate_id": candidate_id,
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


def log_execution_journal(trade_num: int | None, event: str = "opened", **payload):
    """실제 체결/청산 이벤트를 JSONL로 저장해 매매 복기와 전략 개선에 사용한다."""
    row = {
        "trade_num": trade_num,
        "event":     event,
        "time":      datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "timestamp": time.time(),
    }
    row.update(_json_safe(payload))
    with EXECUTION_JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
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

    try:
        rows = [
            json.loads(line)
            for line in CANDIDATE_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as e:
        print(f"[후보평가] 로그 읽기 실패: {e}")
        return []

    s = _load_state()
    evaluated = set(s.get("evaluated_candidate_ids", []))
    now = time.time()
    pending = []

    for row in rows[-800:]:
        if row.get("status") not in ("blocked", "order_failed", "opened"):
            continue
        cid = _candidate_key(row)
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
        pending.append((cid, row))
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
    log_trade_candidate(symbol, tf_key, strategy, direction, strength,
                        "blocked", reason, **extra)
    if send_telegram:
        try:
            from publisher import send as tg_send
            coin = symbol.split("/")[0]
            tg_send(
                f"🧭 <b>[자동매매 차단]</b> {coin} {tf_key} {direction}\n"
                f"강도: <b>{strength}</b>\n"
                f"사유: {reason}"
            )
        except Exception as e:
            print(f"[진단] 차단 알림 실패: {e}")


# ─── 서킷브레이커 ────────────────────────────────────────────────────────────

def check_circuit_breaker(balance: float = 0.0) -> tuple[bool, str]:
    """(거래 가능 여부, 이유 메시지) 반환."""
    s = _refresh_daily(_load_state())
    equity = get_usdt_equity() or balance
    guard_ok, guard_reason = _apply_drawdown_guard(s, equity)
    daily_limit = get_daily_loss_limit(balance)

    if not guard_ok:
        _save_state(s)
        return False, guard_reason

    if s.get("consec_loss", 0) >= MAX_CONSEC_LOSS:
        last_loss_ts = _last_closed_loss_ts(s)
        if last_loss_ts > 0:
            recovery_until = last_loss_ts + PAUSE_HOURS * 3600
            if recovery_until > time.time():
                s["pause_until"] = max(float(s.get("pause_until", 0) or 0), recovery_until)

    if float(s.get("pause_until", 0) or 0) > time.time():
        resume = datetime.fromtimestamp(float(s.get("pause_until", 0) or 0), KST).strftime("%H:%M")
        _save_state(s)
        return False, f"3연패 중단 중 — {resume} KST 이후 재개"

    if float(s.get("daily_loss", 0) or 0) >= daily_limit:
        _save_state(s)
        return False, f"일일 손실 한도 도달 — 오늘 손실 ${float(s.get('daily_loss', 0) or 0):.2f} / 한도 ${daily_limit:.2f}"

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
            s["pause_until"] = time.time() + PAUSE_HOURS * 3600
            print(f"[CB] {MAX_CONSEC_LOSS}연패 → {PAUSE_HOURS}시간 거래 중단")
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

    퀀트 원칙: 자본 집중 (Capital Concentration)
    너무 많은 동시 포지션 = 자본 분산 = 복리 효과 감소.
    MAX_CONCURRENT 도달 시 신규 진입 차단.
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
    """TP 비율에 따라 수량 분할. 최소 수량 미달분은 마지막 TP에 합산."""
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

        if qty >= min_q:
            result.append({"qty": qty, "price": tp["price"], "pct": tp["pct"]})
            remaining = round(remaining - qty, 8)

    if remaining > 1e-9 and result:
        final_qty = round(result[-1]["qty"] + remaining, 8)
        if exchange is not None:
            try:
                final_qty = float(exchange.amount_to_precision(fsym, final_qty))
            except Exception:
                pass
        result[-1]["qty"] = final_qty

    return result


# ─── 주문 실행 ───────────────────────────────────────────────────────────────

def execute(symbol: str, direction: str, leverage: int,
            entry_price: float, sl: float, tps: list,
            position_pct: float = TRADE_MARGIN_PCT,
            atr: float = 0.0, is_elite: bool = False,
            max_margin_usd: float | None = None) -> dict:
    """
    실거래 주문 실행.
    tps: [{"price": float, "pct": int}, ...]
    position_pct: 잔고 대비 증거금 비율 (스캘핑=0.10, 스윙=0.20)
    Returns {"ok": bool, "qty": float, "leverage": int, "error": str}
    """
    # 잔고 확인
    balance = get_usdt_balance()
    if balance < MIN_BALANCE_USD:
        msg = f"잔고 부족 ${balance:.2f} < 최소 ${MIN_BALANCE_USD}"
        print(f"[자동매매] {msg}")
        return {"ok": False, "qty": 0, "leverage": leverage, "error": msg}

    # 서킷브레이커
    ok, reason = check_circuit_breaker(balance)
    if not ok:
        print(f"[자동매매] 거래 차단 — {reason}")
        return {"ok": False, "qty": 0, "leverage": leverage, "error": reason}

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
    qty, leverage = calc_qty(symbol, entry_price, leverage, balance, position_pct, max_margin, exchange=ex)
    if qty <= 0:
        return {"ok": False, "qty": 0, "leverage": leverage, "error": "수량 계산 실패"}

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
        entry_order = ex.create_order(
            fsymbol, "market", side, qty,
            params={"category": "linear"}
        )
        entry_id = entry_order.get("id", "")
        print(f"  ✅ 진입 완료 (주문ID: {entry_id})")
        time.sleep(0.5)

        # 3. 손절 (Stop Market, reduceOnly)
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
        print(f"  ✅ 손절 설정: ${sl:,.4f}")
        time.sleep(0.4)

        # 4. TP 지정가 분할 주문
        labels = ["TP1", "TP2", "TP3"]
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
            time.sleep(0.3)

        # 포지션 추적 저장 (TP1 체결 후 손익분기 SL → ELITE는 트레일링 스톱)
        _save_position(symbol, direction, entry_price, qty, sl, atr=atr, is_elite=is_elite)

        print(f"{'='*50}\n")
        return {"ok": True, "qty": qty, "leverage": leverage, "error": ""}

    except Exception as e:
        err = str(e)
        print(f"  ❌ 주문 오류: {err}")
        print(f"{'='*50}\n")
        return {"ok": False, "qty": qty, "leverage": leverage, "error": err}


# ─── 포지션 추적 / 손익분기 SL ────────────────────────────────────────────────

def _save_position(symbol: str, direction: str, entry_price: float,
                   qty: float, sl: float, atr: float = 0.0, is_elite: bool = False):
    """진입 정보 저장. ELITE는 TP1 이후 트레일링 스톱 활성화."""
    s = _load_state()
    s.setdefault("positions", {})[symbol] = {
        "direction":   direction,
        "entry_price": entry_price,
        "initial_qty": qty,
        "sl_price":    sl,
        "be_done":     False,
        "atr":         round(atr, 4),
        "is_elite":    is_elite,
        "trail_sl":    None,    # ELITE TP1 이후 활성화되는 트레일 SL
    }
    _save_state(s)


def _clear_position(symbol: str):
    s = _load_state()
    s.get("positions", {}).pop(symbol, None)
    _save_state(s)


def _update_trail_sl(ex, symbol: str, fsym: str, info: dict,
                     current_price: float, current_qty: float):
    """
    ELITE 포지션 트레일링 스톱 갱신.
    현재가에서 TRAIL_ATR_MULT × ATR 뒤에 SL을 유지하며 방향으로만 이동(래칫).
    """
    from publisher import send as tg_send

    direction    = info["direction"]
    trail_atr    = info.get("atr", 0)
    current_sl   = info.get("trail_sl") or info["sl_price"]
    close_side   = "sell" if direction == "LONG" else "buy"
    tg_dir       = 2 if direction == "LONG" else 1

    if trail_atr <= 0 or current_price <= 0:
        return

    new_sl = (
        current_price - trail_atr * TRAIL_ATR_MULT if direction == "LONG"
        else current_price + trail_atr * TRAIL_ATR_MULT
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
            for p in positions:
                q = abs(float(p.get("contracts", 0) or 0))
                if q > 0:
                    current_qty   = q
                    current_price = float(p.get("markPrice", 0) or 0)
        except Exception as e:
            print(f"[모니터] {symbol} 조회 실패: {e}")
            continue

        # ① 포지션 완전 청산 → PnL 기록
        if current_qty <= 0:
            pnl = 0.0
            try:
                sym_clean = fsym.split("/")[0] + "USDT"
                resp = ex.privateGetV5PositionClosedPnl({
                    "category": "linear", "symbol": sym_clean, "limit": 1
                })
                entries = resp.get("result", {}).get("list", [])
                if entries:
                    pnl = float(entries[0].get("closedPnl", 0))
            except Exception as e:
                print(f"[모니터] {symbol} PnL 조회 실패: {e}")
            record_result(pnl)
            _update_trade_result(symbol, pnl)
            sign = "+" if pnl >= 0 else ""
            print(f"[모니터] {symbol} 청산 완료 — PnL {sign}${pnl:.2f}")
            _clear_position(symbol)
            continue

        # ③ ELITE + TP1 이후 → 트레일링 스톱 래칫
        if info.get("be_done") and info.get("is_elite"):
            _update_trail_sl(ex, symbol, fsym, info, current_price, current_qty)
            continue

        # 이미 BE SL 적용됨 (non-ELITE) → 스킵
        if info.get("be_done"):
            continue

        # ② TP1 체결 감지: 현재 수량이 초기의 85% 미만
        initial_qty = info.get("initial_qty", 0)
        if initial_qty > 0 and current_qty < initial_qty * 0.85:
            direction   = info["direction"]
            entry_price = info["entry_price"]
            close_side  = "sell" if direction == "LONG" else "buy"
            tg_dir      = 2 if direction == "LONG" else 1
            trail_atr   = info.get("atr", 0)
            is_elite    = info.get("is_elite", False)

            try:
                ex.cancel_all_orders(fsym, params={
                    "category": "linear", "orderFilter": "StopOrder"
                })
                time.sleep(0.3)

                # ELITE: 초기 트레일 SL (현재가 기준), 일반: 손익분기
                if is_elite and trail_atr > 0 and current_price > 0:
                    init_sl = (
                        max(entry_price, current_price - trail_atr * TRAIL_ATR_MULT)
                        if direction == "LONG"
                        else min(entry_price, current_price + trail_atr * TRAIL_ATR_MULT)
                    )
                    init_sl = round(init_sl, 4)
                    trail_note = f"트레일링 스톱 시작 ${init_sl:,.4f}"
                else:
                    init_sl    = entry_price
                    trail_note = f"손익분기 보호 ${init_sl:,.4f}"

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

                elite_tag = " 💎 트레일링 모드 진입" if is_elite else ""
                print(f"[모니터] {symbol} TP1 체결 → {trail_note}{elite_tag}")
                tg_send(
                    f"🔄 <b>[TP1 체결{'  💎 트레일링 스톱 시작' if is_elite else ''}]</b> {symbol}\n"
                    f"SL → <b>${init_sl:,.4f}</b>  남은수량 {current_qty}\n"
                    f"{'ELITE: 수익 따라 SL 자동 상향 시작' if is_elite else '손익분기 보호 완료'}"
                )

                s = _load_state()
                if symbol in s.get("positions", {}):
                    s["positions"][symbol]["be_done"]  = True
                    s["positions"][symbol]["sl_price"] = init_sl
                    if is_elite:
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


def _update_trade_result(symbol: str, pnl_usd: float):
    """해당 심볼의 가장 최근 open 거래에 청산 결과 기록."""
    s = _load_state()
    closed_record = None
    for record in reversed(s.get("trade_history", [])):
        if record["symbol"] == symbol and record["status"] == "open":
            record["status"]    = "win" if pnl_usd > 0 else ("loss" if pnl_usd < 0 else "breakeven")
            record["pnl_usd"]   = round(pnl_usd, 4)
            record["closed_at"] = datetime.now(KST).strftime("%m/%d %H:%M KST")
            closed_record = record
            break
    _save_state(s)
    if closed_record:
        log_execution_journal(
            closed_record.get("num"), "closed",
            symbol=closed_record.get("symbol"),
            tf=closed_record.get("tf"),
            strategy=closed_record.get("strategy", ""),
            direction=closed_record.get("direction"),
            status=closed_record.get("status"),
            pnl_usd=closed_record.get("pnl_usd"),
            entry_price=closed_record.get("entry_price"),
            sl=closed_record.get("sl"),
            tps=closed_record.get("tps", []),
            entry_reasons=closed_record.get("entry_reasons", []),
        )


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
                              divergence_count: int = 0) -> str:
    """텔레그램 자동매매 체결 알림 메시지."""
    coin      = symbol.split("/")[0]
    now       = datetime.now(KST).strftime("%m/%d %H:%M KST")
    margin    = round(qty * entry_price / leverage, 2)
    emoji     = "🟢" if direction == "LONG" else "🔴"
    dir_label = "롱 LONG" if direction == "LONG" else "숏 SHORT"
    title_num = f" #{trade_num}" if trade_num else ""

    icons = ["🥇", "🥈", "🥉", "🎯"]
    tp_lines = []
    for i, tp in enumerate(tps[:4]):
        icon = icons[i] if i < len(icons) else "🎯"
        pct = tp.get("pct", 0)
        rr_txt = f"  R:R 1:{tp['rr']}" if tp.get("rr") else ""
        tp_lines.append(
            f"   {icon} TP{i+1} [{pct}%]  <b>{_fmt_price(tp['price'])}</b>{rr_txt}"
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
        quality_bits.append(f"다이버전스 {divergence_count}")
    if confirmed_count:
        quality_bits.append(f"확인 {confirmed_count}")
    if signal_type:
        quality_bits.append(signal_type)

    risk_line = (
        f"예상 SL손실 ~${est_sl_loss:.2f}"
        if est_sl_loss > 0 else "예상 SL손실 계산값 없음"
    )
    if risk_pct > 0:
        risk_line += f"  |  계좌위험 {risk_pct * 100:.2f}%"
    if rr > 0:
        risk_line += f"  |  최대 R:R 1:{rr}"

    lines = [
        f"✅ <b>[매매 체결되었습니다{title_num}] {coin} {dir_label}</b>",
        f"{emoji} 방향: <b>{dir_label}</b>  |  시간: {now}",
    ]
    if meta_bits:
        lines.append(f"전략: {' / '.join(escape(str(bit)) for bit in meta_bits)}")
    if quality_bits:
        lines.append(f"신뢰도: {' | '.join(escape(str(bit)) for bit in quality_bits)}")

    lines += [
        "",
        "🧠 <b>진입 근거</b>",
        *reason_lines,
    ]
    if timing_note:
        lines.append(f"   • 하위봉 타점: {escape(timing_note)}")

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
