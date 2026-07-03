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
        return float((bal.get("USDT", {}) or {}).get("free", 0) or 0)
    except Exception as e:
        print(f"[Binance 잔고] 조회 실패: {e}")
        return 0.0


def get_usdt_equity() -> float:
    try:
        bal = _ex().fetch_balance({"type": "future"})
        usdt = bal.get("USDT", {}) or {}
        return float(usdt.get("total") or usdt.get("free") or 0)
    except Exception as e:
        print(f"[Binance 에쿼티] 조회 실패: {e}")
        return 0.0


def has_open_position(symbol: str) -> bool:
    try:
        fsym = _futures_symbol(symbol)
        positions = _ex().fetch_positions([fsym])
        return any(abs(float(p.get("contracts", 0) or 0)) > 0 for p in positions)
    except Exception as e:
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
        return rows
    except Exception as e:
        print(f"[Binance 포지션] 전체 조회 실패: {e}")
        return []


def get_portfolio_risk_snapshot(retries: int = 2) -> dict:
    last_error = ""
    positions = []
    ok = False
    for i in range(max(1, int(retries or 1))):
        try:
            positions = fetch_all_positions_raw()
            ok = True
            break
        except Exception as e:
            last_error = str(e)
            if i + 1 < retries:
                time.sleep(0.4)
    free = get_usdt_balance()
    equity = get_usdt_equity()
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

    balance = get_usdt_balance()
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


def monitor_positions() -> None:
    """Basic Binance close detection for positions tracked in trader.py state."""
    try:
        import trader as bybit_state
        from publisher import send as tg_send
    except Exception:
        return

    state = bybit_state._load_state()
    tracked = state.get("positions", {})
    if not tracked:
        return
    ex = _ex()
    try:
        ex.load_markets()
    except Exception:
        return

    for symbol, info in list(tracked.items()):
        fsym = _futures_symbol(symbol)
        try:
            positions = ex.fetch_positions([fsym])
            current_qty = sum(abs(float(p.get("contracts", 0) or 0)) for p in positions)
        except Exception as e:
            print(f"[Binance 모니터] {symbol} 조회 실패: {e}")
            continue
        if current_qty > 0:
            continue

        since_ms = int(float(info.get("opened_ts", 0) or 0) * 1000) - 10_000
        pnl, close_info = _realized_pnl_since(ex, fsym, since_ms)
        bybit_state.record_result(pnl)
        closed = bybit_state._update_trade_result(symbol, pnl, close_info=close_info)
        bybit_state._clear_position(symbol)
        if closed:
            try:
                tg_send(bybit_state.build_trade_close_notification(closed))
            except Exception:
                pass
        print(f"[Binance 모니터] {symbol} 청산 감지 PnL={pnl:.4f}")
