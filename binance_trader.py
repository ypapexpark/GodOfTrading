"""
Binance USD-M futures execution adapter.

This module mirrors the small public surface that main.py needs from trader.py,
but keeps Binance live trading behind an explicit env guard:

  AUTO_TRADE_EXCHANGE=binance
  BINANCE_LIVE_TRADING_ENABLED=true
  BINANCE_API_KEY=...
  BINANCE_API_SECRET=...

Without the live flag, execute() refuses to place orders.
"""
from __future__ import annotations

import math
import os
import time
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import ccxt
from dotenv import load_dotenv

from config import (MIN_FALLBACK_TRADE_MARGIN_USD, MIN_QTY_MAP,
                    MIN_TRADE_MARGIN_USD, QTY_STEP_MAP, ROUND_TRIP_FEE)

load_dotenv(Path(__file__).parent / ".env")

_exchange = None

# ─── API 헬스 (2026-07-11) ──────────────────────────────────────────────────
# -2015 Invalid API-key/IP/permissions 가 수천 회 반복되며 진입이 조용히
# 실패하던 문제. 인증 계열 실패를 sticky unhealthy 로 잡아 신규 진입을 hard-stop
# 하고, 복구 시 한 번 더 알림할 수 있게 alert 플래그를 리셋한다.
_api_health: dict = {
    "healthy": True,
    "last_error": "",
    "fail_count": 0,
    "last_fail_ts": 0.0,
    "last_ok_ts": 0.0,
    "alert_sent": False,
}


def _is_auth_error(err: Exception | str) -> bool:
    msg = str(err)
    needles = (
        "-2015",
        "Invalid API-key",
        "permissions for action",
        "API-key format invalid",
        "Signature for this request is not valid",
        "API key does not exist",
    )
    return any(n in msg for n in needles)


def _mark_api_ok() -> None:
    was_down = not _api_health["healthy"]
    _api_health["healthy"] = True
    _api_health["fail_count"] = 0
    _api_health["last_error"] = ""
    _api_health["last_ok_ts"] = time.time()
    if was_down:
        _api_health["alert_sent"] = False
        print("[Binance API] ✅ 인증 복구 — private 엔드포인트 정상")


def _mark_api_fail(err: Exception | str) -> None:
    if not _is_auth_error(err):
        return
    _api_health["healthy"] = False
    _api_health["fail_count"] = int(_api_health.get("fail_count", 0) or 0) + 1
    _api_health["last_error"] = str(err)[:300]
    _api_health["last_fail_ts"] = time.time()


def is_execution_api_healthy() -> bool:
    return bool(_api_health.get("healthy", True))


def get_execution_api_status() -> dict:
    return dict(_api_health)


def probe_execution_api() -> bool:
    """스캔 시작 시 private 잔고 조회로 헬스 갱신. 실패해도 예외를 밖으로 안 던짐."""
    try:
        bal = _ex().fetch_balance({"type": "future"})
        usdt = bal.get("USDT", {}) or {}
        _ = float(usdt.get("total") or usdt.get("free") or 0)
        _mark_api_ok()
        return True
    except Exception as e:
        _mark_api_fail(e)
        print(f"[Binance API] probe 실패: {e}")
        return False


def maybe_alert_execution_api_down() -> bool:
    """unhealthy 이고 아직 알림 안 보냈으면 텔레그램 1회. True면 이번에 발송."""
    if is_execution_api_healthy() or _api_health.get("alert_sent"):
        return False
    _api_health["alert_sent"] = True
    err = _api_health.get("last_error") or "unknown"
    n = _api_health.get("fail_count", 0)
    msg = (
        "🚨 <b>[Binance API 차단]</b>\n"
        f"private 인증 실패 (count={n})\n"
        f"<code>{err}</code>\n"
        "→ 신규 진입 hard-stop. API 키 Futures 권한 / IP 화이트리스트 / "
        "키 재발급 후 .env 갱신 필요.\n"
        "스캔·시그널은 계속되나 주문/잔고/포지션 동기화 불가."
    )
    print(f"[Binance API] ⛔ 신규진입 중단 — {err}")
    try:
        from publisher import send_signal
        send_signal(msg)
    except Exception as e:
        print(f"[Binance API] 알림 실패: {e}")
    return True


def _live_enabled() -> bool:
    return os.getenv("BINANCE_LIVE_TRADING_ENABLED", "").strip().lower() == "true"


def _ex() -> ccxt.binanceusdm:
    global _exchange
    if _exchange is None:
        _exchange = ccxt.binanceusdm({
            "apiKey": os.getenv("BINANCE_API_KEY", ""),
            "secret": os.getenv("BINANCE_API_SECRET", ""),
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "adjustForTimeDifference": True,
            },
        })
    return _exchange


def _futures_symbol(symbol: str) -> str:
    if ":" in symbol:
        return symbol
    base = symbol.split("/")[0]
    return f"{base}/USDT:USDT"


def _side(direction: str) -> tuple[str, str]:
    entry = "buy" if direction == "LONG" else "sell"
    close = "sell" if direction == "LONG" else "buy"
    return entry, close


def _amount_precision(ex, fsym: str, qty: float) -> float:
    try:
        return float(ex.amount_to_precision(fsym, qty))
    except Exception:
        return float(qty)


def _price_precision(ex, fsym: str, price: float) -> float:
    try:
        return float(ex.price_to_precision(fsym, price))
    except Exception:
        return round(float(price), 6)


def get_usdt_balance() -> float:
    try:
        bal = _ex().fetch_balance({"type": "future"})
        free = float((bal.get("USDT", {}) or {}).get("free", 0) or 0)
        _mark_api_ok()
        return free
    except Exception as e:
        _mark_api_fail(e)
        print(f"[Binance 잔고] 조회 실패: {e}")
        return 0.0


def get_usdt_equity() -> float:
    try:
        bal = _ex().fetch_balance({"type": "future"})
        usdt = bal.get("USDT", {}) or {}
        equity = float(usdt.get("total") or usdt.get("free") or 0)
        _mark_api_ok()
        return equity
    except Exception as e:
        _mark_api_fail(e)
        print(f"[Binance 에쿼티] 조회 실패: {e}")
        return 0.0


def has_open_position(symbol: str) -> bool:
    try:
        fsym = _futures_symbol(symbol)
        positions = _ex().fetch_positions([fsym])
        _mark_api_ok()
        return any(abs(float(p.get("contracts", 0) or 0)) > 0 for p in positions)
    except Exception as e:
        _mark_api_fail(e)
        print(f"[Binance 포지션] 조회 실패: {e}")
        return True


def fetch_all_positions_raw() -> list[dict]:
    try:
        rows = []
        for p in _ex().fetch_positions():
            qty = abs(float(p.get("contracts", 0) or 0))
            if qty <= 0:
                continue
            raw_sym = p.get("symbol", "")
            sym = raw_sym.split(":")[0]
            side = str(p.get("side", "")).lower()
            direction = "LONG" if side == "long" else "SHORT"
            entry = float(p.get("entryPrice", 0) or 0)
            mark = float(p.get("markPrice", 0) or entry or 0)
            lev = float(p.get("leverage", 1) or 1)
            notional = qty * mark
            margin = notional / max(lev, 1)
            rows.append({
                "symbol": sym,
                "fsymbol": raw_sym,
                "venue": "binance",
                "direction": direction,
                "qty": qty,
                "entry_price": entry,
                "mark_price": mark,
                "leverage": lev,
                "unrealized_pnl": float(p.get("unrealizedPnl", 0) or 0),
                "liq_price": float(p.get("liquidationPrice", 0) or 0),
                "margin": margin,
            })
        _mark_api_ok()
        return rows
    except Exception as e:
        _mark_api_fail(e)
        print(f"[Binance 포지션] 전체 조회 실패: {e}")
        return []


def get_portfolio_risk_snapshot(retries: int = 2) -> dict:
    last_error = ""
    positions = []
    ok = False
    for i in range(max(1, int(retries or 1))):
        try:
            # fetch_all_positions_raw 가 예외 대신 [] 를 반환하므로
            # 헬스 플래그로 성공 여부를 판단한다.
            positions = fetch_all_positions_raw()
            if is_execution_api_healthy():
                ok = True
                break
            last_error = _api_health.get("last_error") or "Binance private API unhealthy"
        except Exception as e:
            last_error = str(e)
            _mark_api_fail(e)
            if i + 1 < retries:
                time.sleep(0.4)
    free = get_usdt_balance() if ok else 0.0
    equity = get_usdt_equity() if ok else 0.0
    if not ok and not last_error:
        last_error = _api_health.get("last_error") or "Binance private API unhealthy"
    margin_used = sum(float(p.get("margin", 0) or 0) for p in positions)
    return {
        "ok": ok,
        "reason": "" if ok else last_error,
        "positions": positions,
        "count": len(positions),
        "margin_used": margin_used,
        "long_margin": sum(p["margin"] for p in positions if p["direction"] == "LONG"),
        "short_margin": sum(p["margin"] for p in positions if p["direction"] == "SHORT"),
        "sl_risk": 0.0,
        "free": free,
        "equity": equity if equity > 0 else free + margin_used,
    }


def calc_qty(symbol: str, entry_price: float, leverage: int, balance: float,
             position_pct: float, max_margin: float, exchange=None) -> tuple[float, int]:
    ex = exchange or _ex()
    fsym = _futures_symbol(symbol)
    try:
        ex.load_markets()
    except Exception:
        pass
    margin = min(balance * position_pct, max_margin)
    if margin <= 0 or entry_price <= 0 or leverage <= 0:
        return 0.0, leverage

    step = QTY_STEP_MAP.get(symbol, 0.001)
    min_q = MIN_QTY_MAP.get(symbol, 0.001)
    try:
        market = ex.market(fsym)
        min_q = float((market.get("limits", {}).get("amount", {}) or {}).get("min") or min_q)
    except Exception:
        pass

    qty = math.floor((margin * leverage / entry_price) / step) * step
    qty = _amount_precision(ex, fsym, qty)
    if qty < min_q:
        needed = math.ceil((min_q * entry_price) / max(margin, 1e-9))
        leverage = max(leverage, needed)
        qty = _amount_precision(ex, fsym, min_q)
    return qty, leverage


def _split_tps(total_qty: float, tps: list, symbol: str, exchange) -> list[dict]:
    fsym = _futures_symbol(symbol)
    if not tps:
        return []
    result = []
    remaining = total_qty
    for i, tp in enumerate(tps):
        if i == len(tps) - 1:
            qty = remaining
        else:
            qty = total_qty * float(tp.get("pct", 0) or 0) / 100
        qty = _amount_precision(exchange, fsym, qty)
        if qty > 0:
            result.append({
                "qty": qty,
                "price": _price_precision(exchange, fsym, float(tp["price"])),
                "pct": tp.get("pct", 0),
            })
            remaining = max(remaining - qty, 0.0)
    return result


def place_emergency_sl(symbol: str, direction: str, qty: float, sl_price: float) -> bool:
    if not _live_enabled():
        print("[Binance 긴급SL] live flag disabled")
        return False
    ex = _ex()
    fsym = _futures_symbol(symbol)
    _, close_side = _side(direction)
    try:
        ex.load_markets()
        ex.create_order(
            fsym, "STOP_MARKET", close_side, qty, None,
            params={
                "stopPrice": _price_precision(ex, fsym, sl_price),
                "reduceOnly": True,
                "workingType": "MARK_PRICE",
            },
        )
        print(f"[Binance 긴급SL] {symbol} {direction} SL={sl_price}")
        return True
    except Exception as e:
        print(f"[Binance 긴급SL] 실패: {e}")
        return False


def execute(symbol: str, direction: str, leverage: int,
            entry_price: float, sl: float, tps: list,
            position_pct: float = 0.10,
            atr: float = 0.0, is_elite: bool = False,
            max_margin_usd: float | None = None,
            min_margin_usd: float | None = None,
            allow_pause_override: bool = False,
            pause_override_reason: str = "") -> dict:
    if not _live_enabled():
        return {
            "ok": False, "qty": 0, "leverage": leverage,
            "error": "Binance live trading disabled — set BINANCE_LIVE_TRADING_ENABLED=true",
        }

    if not os.getenv("BINANCE_API_KEY") or not os.getenv("BINANCE_API_SECRET"):
        return {"ok": False, "qty": 0, "leverage": leverage, "error": "Binance API 키 없음"}

    if not is_execution_api_healthy():
        maybe_alert_execution_api_down()
        return {
            "ok": False, "qty": 0, "leverage": leverage,
            "error": (
                "Binance API 인증 실패 hard-stop — "
                f"{_api_health.get('last_error') or 'Invalid API-key/IP/permissions'}"
            ),
        }

    balance = get_usdt_balance()
    if not is_execution_api_healthy():
        maybe_alert_execution_api_down()
        return {
            "ok": False, "qty": 0, "leverage": leverage,
            "error": (
                "Binance 잔고 조회 인증 실패 — "
                f"{_api_health.get('last_error') or 'API auth error'}"
            ),
        }
    min_margin = (
        MIN_TRADE_MARGIN_USD if min_margin_usd is None
        else max(float(min_margin_usd), MIN_FALLBACK_TRADE_MARGIN_USD)
    )
    if balance < min_margin:
        return {
            "ok": False, "qty": 0, "leverage": leverage,
            "error": f"Binance 잔고 부족 ${balance:.2f} < ${min_margin:.2f}",
        }
    if has_open_position(symbol):
        return {
            "ok": False, "qty": 0, "leverage": leverage,
            "error": f"Binance {symbol} 이미 오픈 포지션 있음",
        }

    ex = _ex()
    fsym = _futures_symbol(symbol)
    try:
        ex.load_markets()
    except Exception as e:
        return {"ok": False, "qty": 0, "leverage": leverage, "error": f"시장정보 실패: {e}"}

    max_margin = max_margin_usd if max_margin_usd is not None else max(balance * position_pct, min_margin)
    qty, leverage = calc_qty(symbol, entry_price, leverage, balance, position_pct, max_margin, ex)
    if qty <= 0:
        return {"ok": False, "qty": 0, "leverage": leverage, "error": "Binance 수량 계산 실패"}

    tp_splits = _split_tps(qty, tps, symbol, ex)
    if not tp_splits:
        return {"ok": False, "qty": qty, "leverage": leverage, "error": "Binance TP 분할 실패"}

    side, close_side = _side(direction)
    sl_price = _price_precision(ex, fsym, sl)

    try:
        try:
            ex.set_margin_mode("isolated", fsym)
        except Exception as e:
            if "No need to change margin type" not in str(e):
                print(f"[Binance] margin mode 유지/변경 실패 무시: {e}")
        try:
            ex.set_leverage(leverage, fsym)
        except Exception as e:
            if "No need to change leverage" not in str(e):
                raise
        time.sleep(0.2)

        entry_order = ex.create_order(fsym, "market", side, qty)
        time.sleep(0.2)

        ex.create_order(
            fsym, "STOP_MARKET", close_side, qty, None,
            params={
                "stopPrice": sl_price,
                "reduceOnly": True,
                "workingType": "MARK_PRICE",
            },
        )

        for tp in tp_splits:
            ex.create_order(
                fsym, "limit", close_side, tp["qty"], tp["price"],
                params={"reduceOnly": True, "timeInForce": "GTC"},
            )
            time.sleep(0.15)

        try:
            import trader as bybit_state

            bybit_state._save_position(
                symbol, direction, entry_price, qty, sl,
                atr=atr, is_elite=is_elite, leverage=leverage,
            )
        except Exception as e:
            print(f"[Binance] 로컬 포지션 저장 실패: {e}")

        print(f"[Binance] 진입 완료 {symbol} {direction} qty={qty} lev={leverage}")
        return {
            "ok": True,
            "qty": qty,
            "leverage": leverage,
            "error": "",
            "venue": "binance",
            "order_id": entry_order.get("id", ""),
        }
    except Exception as e:
        return {"ok": False, "qty": qty, "leverage": leverage, "error": f"Binance 주문 오류: {e}"}


def _realized_pnl_since(ex, fsym: str, since_ms: int) -> tuple[float, dict]:
    try:
        trades = ex.fetch_my_trades(fsym, since=since_ms, limit=100)
    except Exception as e:
        print(f"[Binance PnL] 체결 조회 실패: {e}")
        return 0.0, {}
    pnl = 0.0
    commission = 0.0
    for trade in trades:
        info = trade.get("info") or {}
        try:
            pnl += float(info.get("realizedPnl", 0) or 0)
        except Exception:
            pass
        try:
            commission += float(info.get("commission", 0) or 0)
        except Exception:
            pass
    return pnl - commission, {"parts": trades[-10:], "commission": commission}


def _cancel_reduce_stops(ex, fsym: str) -> None:
    """Binance 미체결 reduce-only 스탑 정리 (best-effort)."""
    try:
        open_orders = ex.fetch_open_orders(fsym)
    except Exception:
        open_orders = []
    for o in open_orders:
        try:
            otype = str(o.get("type") or "").lower()
            reduce = bool((o.get("info") or {}).get("reduceOnly") or o.get("reduceOnly"))
            if reduce or "stop" in otype:
                ex.cancel_order(o["id"], fsym)
        except Exception:
            pass


def _set_stop_loss(ex, fsym: str, direction: str, qty: float, stop_price: float) -> float:
    """STOP_MARKET reduce-only SL. 반환: 정밀도 적용된 가격."""
    _, close_side = _side(direction)
    px = _price_precision(ex, fsym, stop_price)
    qty = _amount_precision(ex, fsym, qty)
    if qty <= 0:
        raise ValueError("qty<=0")
    _cancel_reduce_stops(ex, fsym)
    time.sleep(0.2)
    ex.create_order(
        fsym, "STOP_MARKET", close_side, qty, None,
        params={
            "stopPrice": px,
            "reduceOnly": True,
            "workingType": "MARK_PRICE",
        },
    )
    return px


def monitor_positions() -> None:
    """
    Binance 포지션 모니터 (2026-07-11 고도화).
    기존: 청산 감지만 → Bybit과 동일하게 수익보호/TP1 잔량락/트레일 적용.
    """
    try:
        import trader as st
        from publisher import send as tg_send
        from config import ROUND_TRIP_FEE
    except Exception:
        return

    if not is_execution_api_healthy():
        # 한 번 더 probe — 복구되면 모니터 재개
        probe_execution_api()
        if not is_execution_api_healthy():
            return

    state = st._load_state()
    tracked = state.get("positions", {})
    if not tracked:
        return

    # 파라미터는 trader 모듈과 동기 (단일 소스)
    PRE_TP_BE_TRIGGER_R = getattr(st, "PRE_TP_BE_TRIGGER_R", 1.0)
    PRE_TP_BE_LOCK_FRACTION = getattr(st, "PRE_TP_BE_LOCK_FRACTION", 0.55)
    BE_FEE_CUSHION_MULT = getattr(st, "BE_FEE_CUSHION_MULT", 1.2)
    POST_TP1_LOCK_R = getattr(st, "POST_TP1_LOCK_R", 0.35)
    TRAIL_ATR_MULT = getattr(st, "TRAIL_ATR_MULT", 1.5)
    TRAIL_ATR_MULT_STANDARD = getattr(st, "TRAIL_ATR_MULT_STANDARD", 2.0)
    TRAIL_AFTER_TP1_ALL = getattr(st, "TRAIL_AFTER_TP1_ALL", True)
    TRAIL_ADVANCE_MIN = getattr(st, "TRAIL_ADVANCE_MIN", 0.5)
    PROFIT_LOCK_TRIGGER = getattr(st, "PROFIT_LOCK_TRIGGER_MARGIN_ROI_PCT", 10.0)
    PROFIT_LOCK_SL = getattr(st, "PROFIT_LOCK_SL_MARGIN_ROI_PCT", 10.0)

    ex = _ex()
    try:
        ex.load_markets()
    except Exception as e:
        _mark_api_fail(e)
        return

    for symbol, info in list(tracked.items()):
        fsym = _futures_symbol(symbol)
        try:
            positions = ex.fetch_positions([fsym])
            _mark_api_ok()
            live = [p for p in positions if abs(float(p.get("contracts", 0) or 0)) > 0]
            current_qty = sum(abs(float(p.get("contracts", 0) or 0)) for p in live)
            mark = 0.0
            if live:
                mark = float(live[0].get("markPrice") or live[0].get("entryPrice") or 0)
        except Exception as e:
            _mark_api_fail(e)
            print(f"[Binance 모니터] {symbol} 조회 실패: {e}")
            continue

        # ① 청산 완료
        if current_qty <= 0:
            since_ms = int(float(info.get("opened_ts", 0) or 0) * 1000) - 10_000
            pnl, close_info = _realized_pnl_since(ex, fsym, since_ms)
            st.record_result(pnl)
            closed = st._update_trade_result(symbol, pnl, close_info=close_info)
            st._clear_position(symbol)
            if closed:
                try:
                    tg_send(st.build_trade_close_notification(closed))
                except Exception:
                    pass
            print(f"[Binance 모니터] {symbol} 청산 감지 PnL={pnl:.4f}")
            continue

        direction = info.get("direction", "LONG")
        entry_price = float(info.get("entry_price", 0) or 0)
        current_price = mark or entry_price
        leverage = float(info.get("leverage", 1) or 1)
        atr = float(info.get("atr", 0) or 0)
        initial_sl = float(info.get("initial_sl_price") or info.get("sl_price") or 0)
        current_sl = float(info.get("sl_price") or initial_sl or 0)
        risk = abs(entry_price - initial_sl) if entry_price and initial_sl else 0.0
        favorable = (
            current_price - entry_price if direction == "LONG"
            else entry_price - current_price
        )

        # +10% 수익락
        if not info.get("profit_lock_10_done") and entry_price > 0:
            price_move_pct = favorable / entry_price * 100
            margin_roi = price_move_pct * max(leverage, 1.0)
            lock_frac = (PROFIT_LOCK_SL / 100) / max(leverage, 1.0)
            protect_sl = (
                entry_price * (1 + lock_frac) if direction == "LONG"
                else entry_price * (1 - lock_frac)
            )
            improves = (
                (direction == "LONG" and (current_sl <= 0 or protect_sl > current_sl))
                or (direction == "SHORT" and (current_sl <= 0 or protect_sl < current_sl))
            )
            valid = (
                (direction == "LONG" and current_price > protect_sl)
                or (direction == "SHORT" and current_price < protect_sl)
            )
            if margin_roi >= PROFIT_LOCK_TRIGGER and improves and valid:
                try:
                    px = _set_stop_loss(ex, fsym, direction, current_qty, protect_sl)
                    print(f"[Binance +10%락] {symbol} ROI {margin_roi:+.1f}% → SL ${px}")
                    s = st._load_state()
                    if symbol in s.get("positions", {}):
                        s["positions"][symbol]["profit_lock_10_done"] = True
                        s["positions"][symbol]["pre_tp_be_done"] = True
                        s["positions"][symbol]["sl_price"] = px
                    st._save_state(s)
                    info["profit_lock_10_done"] = True
                    info["pre_tp_be_done"] = True
                    info["sl_price"] = px
                    current_sl = px
                except Exception as e:
                    print(f"[Binance +10%락] 실패: {e}")

        # pre-TP 수익보호
        if (
            not info.get("pre_tp_be_done")
            and not info.get("be_done")
            and entry_price > 0
            and risk > 0
            and favorable >= risk * PRE_TP_BE_TRIGGER_R
        ):
            fee_buffer = entry_price * ROUND_TRIP_FEE * BE_FEE_CUSHION_MULT
            lock_distance = max(fee_buffer, risk * PRE_TP_BE_LOCK_FRACTION)
            protect_sl = (
                entry_price + lock_distance if direction == "LONG"
                else entry_price - lock_distance
            )
            valid = (
                (direction == "LONG" and current_price > protect_sl)
                or (direction == "SHORT" and current_price < protect_sl)
            )
            if valid:
                try:
                    px = _set_stop_loss(ex, fsym, direction, current_qty, protect_sl)
                    print(
                        f"[Binance 수익보호] {symbol} {PRE_TP_BE_TRIGGER_R:.1f}R → SL ${px}"
                    )
                    s = st._load_state()
                    if symbol in s.get("positions", {}):
                        s["positions"][symbol]["pre_tp_be_done"] = True
                        s["positions"][symbol]["sl_price"] = px
                    st._save_state(s)
                    info["pre_tp_be_done"] = True
                    info["sl_price"] = px
                    current_sl = px
                except Exception as e:
                    print(f"[Binance 수익보호] 실패: {e}")

        # 트레일 (TP1 이후)
        if info.get("be_done") and atr > 0 and current_price > 0:
            is_elite = bool(info.get("is_elite"))
            if is_elite or TRAIL_AFTER_TP1_ALL:
                mult = TRAIL_ATR_MULT if is_elite else TRAIL_ATR_MULT_STANDARD
                trail_sl = info.get("trail_sl") or current_sl
                new_sl = (
                    current_price - atr * mult if direction == "LONG"
                    else current_price + atr * mult
                )
                advance_ok = (
                    (direction == "LONG" and new_sl > float(trail_sl) + atr * TRAIL_ADVANCE_MIN)
                    or (direction == "SHORT" and new_sl < float(trail_sl) - atr * TRAIL_ADVANCE_MIN)
                )
                if advance_ok:
                    try:
                        px = _set_stop_loss(ex, fsym, direction, current_qty, new_sl)
                        print(f"[Binance 트레일] {symbol} SL → ${px}")
                        s = st._load_state()
                        if symbol in s.get("positions", {}):
                            s["positions"][symbol]["trail_sl"] = px
                            s["positions"][symbol]["sl_price"] = px
                        st._save_state(s)
                        info["trail_sl"] = px
                        info["sl_price"] = px
                    except Exception as e:
                        print(f"[Binance 트레일] 실패: {e}")
            continue

        # TP1 부분익절 감지 → 잔량 +POST_TP1_LOCK_R 보호
        initial_qty = float(info.get("initial_qty", 0) or 0)
        if initial_qty > 0 and current_qty < initial_qty * 0.85 and not info.get("be_done"):
            lock_r = max(0.0, float(POST_TP1_LOCK_R))
            lock_sl = (
                entry_price + risk * lock_r if direction == "LONG"
                else entry_price - risk * lock_r
            ) if risk > 0 else entry_price
            candidates = [entry_price, current_sl, lock_sl]
            init_sl = max(candidates) if direction == "LONG" else min(candidates)
            try:
                px = _set_stop_loss(ex, fsym, direction, current_qty, init_sl)
                print(f"[Binance TP1] {symbol} 잔량 +{lock_r:.2f}R SL ${px}")
                s = st._load_state()
                if symbol in s.get("positions", {}):
                    s["positions"][symbol]["be_done"] = True
                    s["positions"][symbol]["sl_price"] = px
                    s["positions"][symbol]["trail_sl"] = px
                st._save_state(s)
            except Exception as e:
                print(f"[Binance TP1] 실패: {e}")
