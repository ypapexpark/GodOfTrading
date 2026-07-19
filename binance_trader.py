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

import os
import math
import time
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import ccxt
from dotenv import load_dotenv
from binance_api_guard import (
    api_backoff_remaining,
    api_backoff_status,
    record_api_error,
)

from config import (BINANCE_MAX_MARGIN_PCT, BINANCE_MAX_MARGIN_USD,
                    BINANCE_ROUND_TRIP_EXECUTION_COST,
                    BINANCE_MAX_TRADE_SL_LOSS_PCT,
                    MIN_FALLBACK_TRADE_MARGIN_USD, MIN_QTY_MAP,
                    MIN_TRADE_MARGIN_USD)

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
    retry_at = record_api_error(err)
    if not _is_auth_error(err) and retry_at <= 0:
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
    remaining = api_backoff_remaining()
    if remaining > 0:
        _api_health["healthy"] = False
        _api_health["last_error"] = f"Binance API rate-limit backoff {remaining:.0f}s"
        _api_health["last_fail_ts"] = time.time()
        return False
    try:
        bal = _ex().fetch_balance({"type": "future"})
        usdt = bal.get("USDT", {}) or {}
        equity = float(usdt.get("total") or usdt.get("free") or 0)
        if equity > 0:
            # venue_runtime이 Binance namespace를 선택하므로 공용 state helper를
            # 재사용해도 trade_state_binance.json만 갱신된다.
            import trader as shared_state
            state = shared_state._load_state()
            shared_state._apply_drawdown_guard(state, equity)
            shared_state._save_state(state)
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
    # 429/418는 모든 로컬 프로세스가 공유 백오프로 자동 복구한다. 매분 새
    # scanner 프로세스가 같은 텔레그램 경고를 반복하지 않게 로그로만 남긴다.
    backoff = api_backoff_status()
    if backoff.get("blocked"):
        print(
            "[Binance API] rate-limit shared backoff — "
            f"{backoff.get('remaining_seconds', 0):.0f}초 후 자동 재개"
        )
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


def _position_leverage(position: dict, fallback: float = 1.0) -> float:
    """Read leverage from CCXT unified or Binance raw position fields."""
    info = position.get("info") or {}
    for value in (position.get("leverage"), info.get("leverage")):
        try:
            parsed = float(value or 0)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed

    # Binance portfolio responses can omit `leverage` while still exposing
    # initialMarginPercentage (0.20 means 5x) and initial margin/notional.
    for value in (
        position.get("initialMarginPercentage"),
        info.get("initialMarginPercentage"),
    ):
        try:
            margin_fraction = float(value or 0)
        except (TypeError, ValueError):
            continue
        if 0 < margin_fraction <= 1:
            return 1.0 / margin_fraction

    notional = abs(float(position.get("notional") or info.get("notional") or 0))
    initial_margin = float(
        position.get("initialMargin")
        or info.get("positionInitialMargin")
        or info.get("initialMargin")
        or 0
    )
    if notional > 0 and initial_margin > 0:
        return notional / initial_margin
    return max(float(fallback or 1.0), 1.0)


def _position_margin(position: dict, notional: float, leverage: float) -> float:
    """Use exchange-reported initial margin, falling back to notional/leverage."""
    info = position.get("info") or {}
    for value in (
        position.get("initialMargin"),
        info.get("positionInitialMargin"),
        info.get("initialMargin"),
    ):
        try:
            parsed = float(value or 0)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return max(float(notional or 0.0), 0.0) / max(float(leverage or 1.0), 1.0)


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
            lev = _position_leverage(p)
            notional = qty * mark
            margin = _position_margin(p, notional, lev)
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
    tracked_sl_risk = 0.0
    try:
        import trader as shared_state
        tracked = (shared_state._load_state().get("positions") or {})
        for info in tracked.values():
            qty = float(info.get("initial_qty", 0) or 0)
            entry = float(info.get("entry_price", 0) or 0)
            stop = float(
                info.get("initial_sl_price") or info.get("sl_price") or 0
            )
            if qty > 0 and entry > 0 and stop > 0:
                tracked_sl_risk += (
                    qty * abs(entry - stop)
                    + qty * entry * BINANCE_ROUND_TRIP_EXECUTION_COST
                )
    except Exception as risk_err:
        print(f"[Binance 포트폴리오] 추적 SL위험 계산 실패: {risk_err}")
    return {
        "ok": ok,
        "reason": "" if ok else last_error,
        "positions": positions,
        "count": len(positions),
        "margin_used": margin_used,
        "long_margin": sum(p["margin"] for p in positions if p["direction"] == "LONG"),
        "short_margin": sum(p["margin"] for p in positions if p["direction"] == "SHORT"),
        "sl_risk": tracked_sl_risk,
        "free": free,
        "equity": equity if equity > 0 else free + margin_used,
    }


def _calc_order_plan(symbol: str, entry_price: float, leverage: int,
                     balance: float, position_pct: float, max_margin: float,
                     exchange=None) -> dict:
    """주어진 증거금 한도 안에서 거래소 정밀도·최소주문을 만족하는 계획을 만든다."""
    ex = exchange or _ex()
    fsym = _futures_symbol(symbol)
    try:
        ex.load_markets()
    except Exception:
        pass
    margin = min(balance * position_pct, max_margin)
    if margin <= 0 or entry_price <= 0 or leverage <= 0:
        return {
            "ok": False, "qty": 0.0, "leverage": leverage,
            "error": "증거금/가격/레버리지 계산값이 0 이하",
        }

    min_q = MIN_QTY_MAP.get(symbol, 0.001)
    min_cost = 0.0
    contract_size = 1.0
    try:
        market = ex.market(fsym)
        min_q = float((market.get("limits", {}).get("amount", {}) or {}).get("min") or min_q)
        min_cost = float((market.get("limits", {}).get("cost", {}) or {}).get("min") or 0)
        contract_size = float(market.get("contractSize") or 1.0)
    except Exception:
        pass

    raw_qty = margin * leverage / (entry_price * max(contract_size, 1e-12))
    qty = _amount_precision(ex, fsym, raw_qty)
    if qty < min_q:
        return {
            "ok": False, "qty": 0.0, "leverage": leverage,
            "error": (
                f"현재 시드 위험한도 내 수량 {qty:g} < 거래소 최소수량 {min_q:g}; "
                "레버리지 자동상향 금지"
            ),
        }

    notional = qty * entry_price * contract_size
    actual_margin = notional / leverage
    if min_cost > 0 and notional + 1e-9 < min_cost:
        return {
            "ok": False, "qty": 0.0, "leverage": leverage,
            "error": (
                f"현재 시드 위험한도 내 주문가치 ${notional:.2f} "
                f"< 거래소 최소주문 ${min_cost:.2f}; 레버리지 자동상향 금지"
            ),
        }
    if actual_margin > max_margin + 0.01:
        return {
            "ok": False, "qty": 0.0, "leverage": leverage,
            "error": (
                f"정밀도 적용 증거금 ${actual_margin:.2f} > 시드 상한 ${max_margin:.2f}"
            ),
        }
    return {
        "ok": True,
        "qty": qty,
        "leverage": leverage,
        "margin_usd": actual_margin,
        "notional_usd": notional,
        "min_qty": min_q,
        "min_notional_usd": min_cost,
    }


def calc_qty(symbol: str, entry_price: float, leverage: int, balance: float,
             position_pct: float, max_margin: float, exchange=None) -> tuple[float, int]:
    """호환용 공개 함수. 최소수량 때문에 레버리지를 올리지 않는다."""
    plan = _calc_order_plan(
        symbol, entry_price, leverage, balance, position_pct, max_margin, exchange
    )
    return float(plan.get("qty") or 0), int(plan.get("leverage") or leverage)


def _seed_sizing_plan(symbol: str, entry_price: float, sl: float, leverage: int,
                      equity: float, free_balance: float, position_pct: float,
                      requested_max_margin: float | None, exchange=None,
                      requested_max_sl_loss: float | None = None) -> dict:
    """실시간 equity 기준으로 증거금·최악 SL손실 상한을 주문 직전에 강제한다."""
    if equity <= 0 or free_balance <= 0:
        return {"ok": False, "error": "실시간 equity/free balance 확인 실패"}
    if entry_price <= 0 or sl <= 0 or leverage <= 0 or position_pct <= 0:
        return {"ok": False, "error": "시드 사이징 입력값 오류"}

    sl_fraction = abs(entry_price - sl) / entry_price
    loss_fraction = sl_fraction + BINANCE_ROUND_TRIP_EXECUTION_COST
    if loss_fraction <= 0:
        return {"ok": False, "error": "SL 위험률 계산 실패"}

    margin_caps = [float(free_balance)]
    if requested_max_margin is not None:
        margin_caps.append(max(float(requested_max_margin), 0.0))
    if BINANCE_MAX_MARGIN_PCT and BINANCE_MAX_MARGIN_PCT > 0:
        margin_caps.append(equity * float(BINANCE_MAX_MARGIN_PCT))
    if BINANCE_MAX_MARGIN_USD and BINANCE_MAX_MARGIN_USD > 0:
        margin_caps.append(float(BINANCE_MAX_MARGIN_USD))

    max_sl_loss = (
        equity * float(BINANCE_MAX_TRADE_SL_LOSS_PCT)
        if BINANCE_MAX_TRADE_SL_LOSS_PCT and BINANCE_MAX_TRADE_SL_LOSS_PCT > 0
        else 0.0
    )
    requested_loss_cap = max(float(requested_max_sl_loss or 0.0), 0.0)
    if requested_loss_cap > 0:
        max_sl_loss = (
            min(max_sl_loss, requested_loss_cap)
            if max_sl_loss > 0 else requested_loss_cap
        )
    if max_sl_loss > 0:
        # 시장가 체결가·수량 정밀도 차이를 흡수할 12% 사전 여유. 실계좌 NEO
        # 사례에서 5% 버퍼로 산정한 뒤 실제 SL위험이 상한을 약 7% 넘어 즉시
        # 왕복청산됐다. 수량을 미리 낮춰 불필요한 수수료 거래를 막는다.
        risk_margin_cap = max_sl_loss / (leverage * loss_fraction) * 0.88
        margin_caps.append(risk_margin_cap)

    safe_margin_cap = max(min(margin_caps), 0.0)
    if safe_margin_cap <= 0:
        return {"ok": False, "error": "현재 시드 기준 주문 가능 증거금이 0"}

    plan = _calc_order_plan(
        symbol,
        entry_price,
        leverage,
        equity,
        position_pct,
        safe_margin_cap,
        exchange,
    )
    if not plan.get("ok"):
        plan.update({
            "seed_equity": equity,
            "free_balance": free_balance,
            "max_margin_usd": safe_margin_cap,
            "max_sl_loss_usd": max_sl_loss,
        })
        return plan

    estimated_loss = float(plan["notional_usd"]) * loss_fraction
    if max_sl_loss > 0 and estimated_loss > max_sl_loss + 1e-6:
        return {
            "ok": False,
            "error": (
                f"정밀도 적용 예상 SL손실 ${estimated_loss:.4f} "
                f"> 현재 시드 상한 ${max_sl_loss:.4f}"
            ),
            "seed_equity": equity,
            "free_balance": free_balance,
            "max_margin_usd": safe_margin_cap,
            "max_sl_loss_usd": max_sl_loss,
        }
    plan.update({
        "seed_equity": equity,
        "free_balance": free_balance,
        "max_margin_usd": safe_margin_cap,
        "max_sl_loss_usd": max_sl_loss,
        "estimated_sl_loss_usd": estimated_loss,
        "requested_position_pct": position_pct,
        "sl_fraction": sl_fraction,
    })
    return plan


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
            pause_override_reason: str = "",
            max_sl_loss_usd: float | None = None,
            position_meta: dict | None = None,
            require_full_protection: bool = False) -> dict:
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

    # Bybit execute()와 동일한 계좌 생존 게이트. Binance 실제 equity를 명시적으로
    # 넘겨 듀얼벤뉴에서 Bybit equity를 잘못 참조하지 않게 한다.
    import trader as shared_state
    equity = get_usdt_equity()
    if equity <= 0 or not is_execution_api_healthy():
        maybe_alert_execution_api_down()
        return {
            "ok": False, "qty": 0, "leverage": leverage,
            "error": "Binance 실시간 equity 확인 실패 — 시드 사이징 hard-stop",
        }
    circuit_ok, circuit_reason = shared_state.check_circuit_breaker(
        balance,
        allow_pause_override=allow_pause_override,
        override_reason=pause_override_reason,
        equity=equity,
    )
    if not circuit_ok:
        return {
            "ok": False, "qty": 0, "leverage": leverage,
            "error": f"Binance 계좌 리스크 차단 — {circuit_reason}",
        }
    if has_open_position(symbol):
        return {
            "ok": False, "qty": 0, "leverage": leverage,
            "error": f"Binance {symbol} 이미 오픈 포지션 있음",
        }

    ex = _ex()
    fsym = _futures_symbol(symbol)
    entry_order_id = ""
    entry_order_link_id = f"got-{uuid.uuid4().hex}"
    try:
        ex.load_markets()
    except Exception as e:
        return {"ok": False, "qty": 0, "leverage": leverage, "error": f"시장정보 실패: {e}"}
    try:
        contract_type = str(
            ((ex.market(fsym).get("info") or {}).get("contractType") or "")
        ).upper()
    except Exception:
        contract_type = ""
    if contract_type and contract_type != "PERPETUAL":
        return {
            "ok": False,
            "qty": 0,
            "leverage": leverage,
            "error": (
                f"Binance 비암호화/특수 계약 제외: {contract_type} "
                "(별도 agreement 대상)"
            ),
        }

    sizing = _seed_sizing_plan(
        symbol,
        entry_price,
        sl,
        leverage,
        equity,
        balance,
        position_pct,
        max_margin_usd,
        ex,
        requested_max_sl_loss=max_sl_loss_usd,
    )
    qty = float(sizing.get("qty") or 0)
    leverage = int(sizing.get("leverage") or leverage)
    if not sizing.get("ok") or qty <= 0:
        return {
            "ok": False,
            "qty": 0,
            "leverage": leverage,
            "error": f"Binance 현재 시드 주문 차단 — {sizing.get('error') or '수량 계산 실패'}",
            "seed_equity": equity,
            "free_balance": balance,
            "max_margin_usd": sizing.get("max_margin_usd", 0),
            "max_sl_loss_usd": sizing.get("max_sl_loss_usd", 0),
        }

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

        try:
            entry_order = ex.create_order(
                fsym,
                "market",
                side,
                qty,
                params={"newClientOrderId": entry_order_link_id},
            )
        except Exception as entry_err:
            # 네트워크 타임아웃은 거래소 접수 후 응답만 유실될 수 있다. 고유 client ID로
            # 실제 접수 여부를 확인해 같은 주문을 다시 내는 대신 기존 주문을 이어서 관리한다.
            entry_order = _recover_entry_order(ex, fsym, entry_order_link_id)
            if not entry_order:
                raise entry_err
            print(
                f"[Binance] 진입 응답 유실 복구: clientOrderId={entry_order_link_id}"
            )
        entry_order_id = str(
            entry_order.get("id")
            or (entry_order.get("info") or {}).get("orderId")
            or ""
        )
        time.sleep(0.2)

        live_position = _live_position_after_entry(ex, fsym)
        if not live_position.get("ok"):
            emergency_ok = _emergency_market_close(
                ex, fsym, close_side, qty, symbol,
                reason=f"진입 직후 실포지션 검증 실패: {live_position.get('error', '')}",
            )
            return {
                "ok": False,
                "qty": qty,
                "leverage": leverage,
                "error": (
                    "Binance 실포지션 검증 실패 — 긴급 청산 완료"
                    if emergency_ok
                    else "Binance 실포지션 검증 및 긴급 청산 실패 — 즉시 수동 확인 필요"
                ),
                "venue": "binance",
                "entry_order_id": entry_order_id,
                "entry_order_link_id": entry_order_link_id,
                "emergency_closed": emergency_ok,
            }

        requested_leverage = leverage
        actual_qty = float(live_position.get("qty") or qty)
        actual_entry = float(live_position.get("entry_price") or entry_price)
        actual_leverage = max(int(float(live_position.get("leverage") or leverage)), 1)
        actual_margin = float(live_position.get("margin_usd") or 0)
        actual_loss = (
            actual_qty * abs(actual_entry - sl)
            + actual_qty * actual_entry * BINANCE_ROUND_TRIP_EXECUTION_COST
        )
        margin_cap = float(sizing.get("max_margin_usd") or 0)
        loss_cap = float(sizing.get("max_sl_loss_usd") or 0)
        over_margin = margin_cap > 0 and actual_margin > margin_cap * 1.02 + 0.01
        # 5% 사전 버퍼 뒤에도 남는 센트 단위 반올림은 허용하되, 계획 위험의
        # 2% + $0.01을 넘으면 기존처럼 즉시 제거한다.
        over_loss = loss_cap > 0 and actual_loss > loss_cap * 1.02 + 0.01
        if over_margin or over_loss:
            reason = (
                f"실체결 위험캡 초과: margin ${actual_margin:.2f}/${margin_cap:.2f}, "
                f"SL ${actual_loss:.2f}/${loss_cap:.2f}"
            )
            emergency_ok = _emergency_market_close(
                ex, fsym, close_side, actual_qty, symbol, reason=reason,
            )
            return {
                "ok": False,
                "qty": actual_qty,
                "leverage": actual_leverage,
                "error": (
                    f"Binance {reason} — 긴급 청산 완료"
                    if emergency_ok else f"Binance {reason} — 긴급 청산 실패"
                ),
                "venue": "binance",
                "entry_order_id": entry_order_id,
                "entry_order_link_id": entry_order_link_id,
                "emergency_closed": emergency_ok,
            }
        if actual_leverage != requested_leverage:
            print(
                f"[Binance] 실레버리지 보정 {requested_leverage}x→{actual_leverage}x | "
                f"실증거금 ${actual_margin:.2f}"
            )
        qty = actual_qty
        entry_price = actual_entry
        leverage = actual_leverage
        tp_splits = _split_tps(qty, tps, symbol, ex)
        if not tp_splits:
            emergency_ok = _emergency_market_close(
                ex, fsym, close_side, qty, symbol,
                reason="실체결 수량 기준 TP 분할 실패",
            )
            return {
                "ok": False, "qty": qty, "leverage": leverage,
                "error": "Binance 실체결 수량 TP 분할 실패 — 긴급 청산 처리",
                "emergency_closed": emergency_ok,
            }

        # 진입 체결 직후 저장한다. 이후 SL/TP API가 실패해도 실제 포지션이 로컬 추적에서
        # 사라지지 않아 다음 모니터 주기에 orphan으로 남지 않는다.
        try:
            shared_state._save_position(
                symbol, direction, entry_price, qty, sl,
                atr=atr, is_elite=is_elite, leverage=leverage,
                entry_order_id=entry_order_id,
                entry_order_link_id=entry_order_link_id,
                position_meta=position_meta,
            )
        except Exception as state_err:
            emergency_ok = _emergency_market_close(
                ex, fsym, close_side, qty, symbol,
                reason=f"로컬 포지션 저장 실패: {state_err}",
            )
            return {
                "ok": False,
                "qty": qty,
                "leverage": leverage,
                "error": (
                    "Binance 로컬 추적 저장 실패 — 긴급 청산 완료"
                    if emergency_ok
                    else "Binance 로컬 추적 저장 및 긴급 청산 실패 — 즉시 수동 확인 필요"
                ),
                "venue": "binance",
                "entry_order_id": entry_order_id,
                "entry_order_link_id": entry_order_link_id,
                "emergency_closed": emergency_ok,
            }

        # 손절은 최대 3회 재시도한다. 모두 실패하면 무방비 포지션을 즉시 reduce-only
        # 시장가로 제거하고, 청산 성공이 확인된 경우에만 로컬 추적을 지운다.
        sl_ok = False
        sl_error = ""
        for sl_attempt in range(3):
            try:
                ex.create_order(
                    fsym, "STOP_MARKET", close_side, qty, None,
                    params={
                        "stopPrice": sl_price,
                        "reduceOnly": True,
                        "workingType": "MARK_PRICE",
                    },
                )
                sl_ok = True
                break
            except Exception as sl_err:
                sl_error = str(sl_err)
                print(f"[Binance] SL 설정 {sl_attempt + 1}/3 실패: {sl_err}")
                if sl_attempt < 2:
                    time.sleep(1.0)

        if not sl_ok:
            emergency_ok = _emergency_market_close(
                ex, fsym, close_side, qty, symbol,
                reason=f"SL 3회 실패: {sl_error}",
            )
            if emergency_ok:
                shared_state._clear_position(symbol)
            return {
                "ok": False,
                "qty": qty,
                "leverage": leverage,
                "error": (
                    "Binance SL 설정 3회 실패 — 긴급 청산 완료"
                    if emergency_ok
                    else "Binance SL 및 긴급 청산 실패 — 추적 유지, 즉시 수동 확인 필요"
                ),
                "venue": "binance",
                "entry_order_id": entry_order_id,
                "entry_order_link_id": entry_order_link_id,
                "emergency_closed": emergency_ok,
            }

        tp_errors = []
        tp_order_ids = []
        for index, tp in enumerate(tp_splits, start=1):
            tp_order = None
            tp_error = ""
            for tp_attempt in range(3):
                try:
                    tp_order = ex.create_order(
                        fsym, "limit", close_side, tp["qty"], tp["price"],
                        params={"reduceOnly": True, "timeInForce": "GTC"},
                    )
                    break
                except Exception as tp_err:
                    tp_error = str(tp_err)
                    print(
                        f"[Binance] TP{index} 설정 {tp_attempt + 1}/3 실패: {tp_err}"
                    )
                    if tp_attempt < 2:
                        time.sleep(0.5)
            if tp_order:
                tp_order_id = str(
                    tp_order.get("id")
                    or (tp_order.get("info") or {}).get("orderId")
                    or ""
                )
                if tp_order_id:
                    tp_order_ids.append(tp_order_id)
            else:
                tp_errors.append(f"TP{index}: {tp_error or '주문 응답 없음'}")
            time.sleep(0.15)

        if tp_errors:
            # 과거 OGN 실계좌에서 TP가 하나도 없는 포지션이 장시간 유지된
            # 사례가 확인됐다. SL만 있는 진입은 계획된 손익비가 아니므로 성공으로
            # 처리하지 않는다. 이미 접수된 부분 TP를 먼저 취소한 뒤 포지션을
            # reduce-only 시장가로 제거한다. 청산이 실패하면 SL과 로컬 추적은
            # 그대로 남겨 무방비 상태를 막는다.
            for tp_order_id in tp_order_ids:
                try:
                    ex.cancel_order(tp_order_id, fsym)
                except Exception as cancel_err:
                    print(
                        f"[Binance] TP 실패 정리 주문 {tp_order_id} 취소 실패: "
                        f"{cancel_err}"
                    )
            emergency_ok = _emergency_market_close(
                ex,
                fsym,
                close_side,
                qty,
                symbol,
                reason=f"TP 보호주문 설정 실패: {' | '.join(tp_errors)}",
            )
            if emergency_ok:
                _cancel_reduce_stops(ex, fsym)
                shared_state._clear_position(symbol)
            return {
                "ok": False,
                "qty": qty,
                "leverage": leverage,
                "error": (
                    "Binance TP 설정 실패 — 긴급 청산 완료"
                    if emergency_ok
                    else "Binance TP 설정 및 긴급 청산 실패 — "
                         "SL/추적 유지, 즉시 수동 확인 필요"
                ),
                "venue": "binance",
                "entry_order_id": entry_order_id,
                "entry_order_link_id": entry_order_link_id,
                "tp_errors": tp_errors,
                "emergency_closed": emergency_ok,
            }

        print(f"[Binance] 진입 완료 {symbol} {direction} qty={qty} lev={leverage}")
        return {
            "ok": True,
            "qty": qty,
            "leverage": leverage,
            "error": "",
            "venue": "binance",
            "order_id": entry_order_id,
            "entry_order_id": entry_order_id,
            "entry_order_link_id": entry_order_link_id,
            "tp_errors": tp_errors,
            "seed_equity": sizing["seed_equity"],
            "free_balance": sizing["free_balance"],
            "margin_usd": sizing["margin_usd"],
            "notional_usd": sizing["notional_usd"],
            "estimated_sl_loss_usd": sizing["estimated_sl_loss_usd"],
            "max_sl_loss_usd": sizing["max_sl_loss_usd"],
            "entry_price": entry_price,
            "requested_leverage": requested_leverage,
            "actual_margin_usd": actual_margin,
        }
    except Exception as e:
        return {
            "ok": False,
            "qty": qty,
            "leverage": leverage,
            "error": f"Binance 주문 오류: {e}",
            "entry_order_id": entry_order_id,
            "entry_order_link_id": entry_order_link_id,
        }


def _recover_entry_order(ex, fsym: str, client_order_id: str) -> dict:
    """응답 유실 시 Binance clientOrderId로 이미 접수된 진입 주문을 복구한다."""
    try:
        market = ex.market(fsym)
        exchange_symbol = market.get("id") or fsym.split(":")[0].replace("/", "")
        raw = ex.fapiPrivateGetOrder({
            "symbol": exchange_symbol,
            "origClientOrderId": client_order_id,
        })
        if raw and raw.get("orderId"):
            return {"id": str(raw["orderId"]), "info": raw}
    except Exception as recover_err:
        print(f"[Binance] clientOrderId 주문 복구 실패: {recover_err}")
        return {}


def _live_position_after_entry(ex, fsym: str, attempts: int = 3) -> dict:
    """Read the exchange position after a market entry.

    Binance may accept the order while applying a different effective leverage
    than the local plan.  The post-trade position is therefore the source of
    truth for margin and local ledger state.
    """
    last_error = ""
    for attempt in range(max(1, attempts)):
        try:
            for position in ex.fetch_positions([fsym]):
                qty = abs(float(position.get("contracts", 0) or 0))
                if qty <= 0:
                    continue
                entry = float(position.get("entryPrice", 0) or 0)
                leverage = _position_leverage(position)
                notional = qty * entry
                return {
                    "ok": True,
                    "qty": qty,
                    "entry_price": entry,
                    "leverage": leverage,
                    "margin_usd": _position_margin(position, notional, leverage),
                }
        except Exception as exc:
            last_error = str(exc)
        if attempt + 1 < attempts:
            time.sleep(0.25)
    return {"ok": False, "error": last_error or "체결 포지션이 조회되지 않음"}


def _emergency_market_close(ex, fsym: str, close_side: str, qty: float,
                            symbol: str, *, reason: str) -> bool:
    """보호 주문/추적 실패 시 무방비 포지션을 best-effort로 즉시 제거한다."""
    print(f"[Binance] {symbol} 긴급 청산 시도 — {reason}")
    try:
        ex.create_order(
            fsym,
            "market",
            close_side,
            _amount_precision(ex, fsym, qty),
            params={"reduceOnly": True},
        )
        print(f"[Binance] {symbol} 긴급 청산 주문 완료")
        return True
    except Exception as emergency_err:
        print(f"[Binance] {symbol} 긴급 청산 실패: {emergency_err}")
        try:
            from publisher import send as tg_send
            tg_send(
                "🚨 <b>[Binance 긴급 확인]</b>\n"
                f"{symbol} 보호 실패 후 시장가 청산도 실패\n"
                f"사유: <code>{str(emergency_err)[:180]}</code>"
            )
        except Exception:
            pass
        return False


def _realized_pnl_since(ex, fsym: str, since_ms: int,
                        entry_order_id: str = "",
                        entry_order_link_id: str = "") -> tuple[float, dict]:
    """진입 이후 체결을 페이지 순회해 실현손익-수수료를 계산한다."""
    trades = []
    seen = set()
    cursor = max(int(since_ms or 0), 0)
    try:
        for _ in range(10):
            page = ex.fetch_my_trades(fsym, since=cursor, limit=1000)
            fresh = []
            for trade in page:
                info = trade.get("info") or {}
                trade_id = str(trade.get("id") or info.get("id") or "")
                stamp = int(trade.get("timestamp") or info.get("time") or 0)
                key = trade_id or f"{info.get('orderId')}:{stamp}:{info.get('qty')}"
                if key in seen or stamp < since_ms:
                    continue
                seen.add(key)
                fresh.append(trade)
            trades.extend(fresh)
            if len(page) < 1000 or not fresh:
                break
            last_stamp = max(
                int(t.get("timestamp") or (t.get("info") or {}).get("time") or 0)
                for t in fresh
            )
            if last_stamp < cursor:
                break
            cursor = last_stamp + 1
    except Exception as e:
        print(f"[Binance PnL] 체결 조회 실패: {e}")
        return 0.0, {
            "error": str(e)[:180],
            "entry_order_id": entry_order_id,
            "entry_order_link_id": entry_order_link_id,
        }
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
    return pnl - commission, {
        "parts": trades[-20:],
        "commission": commission,
        "trade_count": len(trades),
        "entry_order_id": entry_order_id,
        "entry_order_link_id": entry_order_link_id,
    }


def _cancel_reduce_stops(ex, fsym: str) -> None:
    """Binance 미체결 스탑만 정리한다. reduce-only TP 지정가는 보존한다.

    2026-07부터 조건부 주문은 일반 ``openOrders``가 아니라 Algo 주문 API에
    보일 수 있다. 두 원장을 모두 조회하지 않으면 이전 SL을 못 지워 중복
    STOP_MARKET이 누적된다.
    """
    try:
        open_orders = ex.fetch_open_orders(fsym)
    except Exception:
        open_orders = []
    for o in open_orders:
        try:
            if _is_stop_order(o):
                ex.cancel_order(o["id"], fsym)
        except Exception:
            pass
    for order in _fetch_open_algo_stops(ex, fsym):
        _cancel_algo_order(ex, order)


def _is_stop_order(order: dict) -> bool:
    info = order.get("info") or {}
    types = " ".join(
        str(value or "").lower()
        for value in (order.get("type"), info.get("type"), info.get("origType"))
    )
    try:
        has_stop_price = float(info.get("stopPrice") or order.get("stopPrice") or 0) > 0
    except Exception:
        has_stop_price = False
    return "stop" in types or has_stop_price


def _fetch_all_open_algo_stops(ex) -> list[dict]:
    """Fetch active STOPs from Binance's separate Algo ledger."""
    endpoint = getattr(ex, "fapiPrivateGetOpenAlgoOrders", None)
    if not callable(endpoint):
        return []
    try:
        response = endpoint()
    except Exception as e:
        _mark_api_fail(e)
        print(f"[Binance SL] Algo 주문 조회 실패: {e}")
        return []
    rows = response if isinstance(response, list) else (response or {}).get("orders", [])
    result = []
    for order in rows:
        order_type = str(order.get("orderType") or order.get("type") or "").upper()
        status = str(order.get("algoStatus") or order.get("status") or "").upper()
        if (
            "STOP" in order_type
            and status in {"", "NEW", "PARTIALLY_FILLED"}
        ):
            result.append(order)
    return result


def _fetch_open_algo_stops(ex, fsym: str) -> list[dict]:
    """별도 Algo 원장에 있는 해당 심볼의 활성 STOP 주문을 반환한다."""
    wanted = str(fsym).split(":")[0].replace("/", "").upper()
    return [
        order
        for order in _fetch_all_open_algo_stops(ex)
        if str(order.get("symbol") or "").upper() == wanted
    ]


def _cancel_algo_order(ex, order: dict) -> bool:
    endpoint = getattr(ex, "fapiPrivateDeleteAlgoOrder", None)
    algo_id = order.get("algoId")
    if not callable(endpoint) or algo_id in (None, ""):
        return False
    try:
        endpoint({"algoId": algo_id})
        return True
    except Exception as e:
        _mark_api_fail(e)
        print(f"[Binance SL] 구 Algo SL {algo_id} 취소 실패(신규 SL 유지): {e}")
        return False


def cleanup_orphan_protective_orders() -> dict:
    """Cancel stale reduce-only orders and duplicate Algo STOPs safely.

    Manual non-reduce entry orders are never touched. For a live symbol with
    multiple bot STOPs, keep the order closest to both the live quantity and
    the locally tracked stop price.
    """
    if api_backoff_remaining() > 0:
        return {"ok": False, "skipped": "api backoff", "cancelled": 0}
    try:
        import trader as st
        ex = _ex()
        positions = ex.fetch_positions()
        live_qty: dict[str, float] = {}
        for position in positions or []:
            qty = abs(float(position.get("contracts", 0) or 0))
            symbol = _position_symbol(position)
            if qty > 0 and symbol:
                live_qty[symbol] = live_qty.get(symbol, 0.0) + qty
        tracked = (st._load_state().get("positions") or {})
        cancelled = 0

        try:
            normal_orders = ex.fetch_open_orders()
        except Exception:
            normal_orders = []
        for order in normal_orders or []:
            info = order.get("info") or {}
            reduce_only = str(
                order.get("reduceOnly") or info.get("reduceOnly") or ""
            ).lower() in {"true", "1", "yes"}
            if not reduce_only:
                continue
            symbol = str(order.get("symbol") or "").split(":")[0]
            if not symbol:
                raw = str(info.get("symbol") or "").upper()
                symbol = f"{raw[:-4]}/USDT" if raw.endswith("USDT") else ""
            if symbol and live_qty.get(symbol, 0.0) <= 0:
                try:
                    ex.cancel_order(order["id"], _futures_symbol(symbol))
                    cancelled += 1
                except Exception as exc:
                    print(f"[Binance 보호주문] orphan {order.get('id')} 취소 실패: {exc}")

        algo_by_symbol: dict[str, list[dict]] = {}
        for order in _fetch_all_open_algo_stops(ex):
            reduce_only = str(order.get("reduceOnly") or "").lower() in {
                "true", "1", "yes",
            }
            close_position = str(order.get("closePosition") or "").lower() in {
                "true", "1", "yes",
            }
            if not (reduce_only or close_position):
                continue
            raw = str(order.get("symbol") or "").upper()
            symbol = f"{raw[:-4]}/USDT" if raw.endswith("USDT") else ""
            if symbol:
                algo_by_symbol.setdefault(symbol, []).append(order)
        for symbol, orders in algo_by_symbol.items():
            qty = float(live_qty.get(symbol, 0.0) or 0.0)
            if qty <= 0:
                for order in orders:
                    cancelled += int(_cancel_algo_order(ex, order))
                continue
            if len(orders) <= 1:
                continue
            expected_sl = float(
                (tracked.get(symbol) or {}).get("sl_price") or 0.0
            )

            def score(order: dict) -> tuple[float, float]:
                order_qty = float(
                    order.get("quantity") or order.get("origQty") or 0.0
                )
                stop = float(
                    order.get("triggerPrice") or order.get("stopPrice") or 0.0
                )
                qty_error = abs(order_qty - qty) / max(qty, 1e-12)
                stop_error = (
                    abs(stop - expected_sl) / expected_sl
                    if expected_sl > 0 and stop > 0 else 1.0
                )
                return qty_error, stop_error

            keeper = min(orders, key=score)
            for order in orders:
                if order is keeper:
                    continue
                cancelled += int(_cancel_algo_order(ex, order))
        _mark_api_ok()
        if cancelled:
            print(f"[Binance 보호주문] orphan/중복 {cancelled}건 정리")
        return {
            "ok": True,
            "live": len(live_qty),
            "algo_symbols": len(algo_by_symbol),
            "cancelled": cancelled,
        }
    except Exception as exc:
        _mark_api_fail(exc)
        print(f"[Binance 보호주문] 정합성 점검 실패: {exc}")
        return {"ok": False, "error": str(exc)[:240], "cancelled": 0}


def _set_stop_loss(ex, fsym: str, direction: str, qty: float, stop_price: float) -> float:
    """STOP_MARKET reduce-only SL. 반환: 정밀도 적용된 가격."""
    _, close_side = _side(direction)
    px = _price_precision(ex, fsym, stop_price)
    qty = _amount_precision(ex, fsym, qty)
    if qty <= 0:
        raise ValueError("qty<=0")
    try:
        old_stops = [o for o in ex.fetch_open_orders(fsym) if _is_stop_order(o)]
    except Exception:
        old_stops = []
    old_algo_stops = _fetch_open_algo_stops(ex, fsym)
    # 새 SL을 먼저 접수한 뒤 구 SL을 취소한다. 반대 순서는 API 오류 순간 보호가 0이 된다.
    new_order = ex.create_order(
        fsym, "STOP_MARKET", close_side, qty, None,
        params={
            "stopPrice": px,
            "reduceOnly": True,
            "workingType": "MARK_PRICE",
        },
    )
    new_id = str(new_order.get("id") or "")
    for old in old_stops:
        old_id = str(old.get("id") or "")
        if not old_id or old_id == new_id:
            continue
        try:
            ex.cancel_order(old_id, fsym)
        except Exception as cancel_err:
            print(f"[Binance SL] 구 SL {old_id} 취소 실패(신규 SL 유지): {cancel_err}")
    for old in old_algo_stops:
        _cancel_algo_order(ex, old)
    return px


def _position_symbol(position: dict) -> str:
    """Normalize a CCXT/raw Binance position to the local ``BTC/USDT`` form."""
    unified = str(position.get("symbol") or "")
    if unified:
        return unified.split(":")[0]
    raw = str((position.get("info") or {}).get("symbol") or "").upper()
    if raw.endswith("USDT") and len(raw) > 4:
        return f"{raw[:-4]}/USDT"
    return ""


def _fetch_position_snapshot(ex, tracked_symbols: list[str]) -> dict[str, list[dict]]:
    """Fetch the entire USD-M position ledger once and index it locally.

    Older code called ``fetch_positions([symbol])`` once for every tracked
    position. A five-second manager loop must use one account snapshot instead.
    The TypeError fallback keeps compatibility with older CCXT/test adapters.
    """
    try:
        positions = ex.fetch_positions()
    except TypeError:
        positions = ex.fetch_positions(
            [_futures_symbol(symbol) for symbol in tracked_symbols]
        )
    result: dict[str, list[dict]] = {symbol: [] for symbol in tracked_symbols}
    for position in positions or []:
        symbol = _position_symbol(position)
        if symbol in result:
            result[symbol].append(position)
        elif not symbol and len(tracked_symbols) == 1:
            # Minimal adapters sometimes omit both unified and raw symbol.
            result[tracked_symbols[0]].append(position)
    return result


def _cancel_all_protective_orders(ex, fsym: str) -> None:
    """Remove normal TP/SL orders and the separate Binance Algo STOP ledger."""
    try:
        ex.cancel_all_orders(fsym)
    except Exception as exc:
        print(f"[Binance 보호주문] 일반 주문 일괄취소 실패: {exc}")
    _cancel_reduce_stops(ex, fsym)


def monitor_positions() -> dict:
    """
    Binance 포지션 모니터 (2026-07-11 고도화).
    기존: 청산 감지만 → Bybit과 동일하게 수익보호/TP1 잔량락/트레일 적용.
    """
    try:
        import trader as st
        from publisher import send as tg_send
        from config import BINANCE_ROUND_TRIP_EXECUTION_COST
    except Exception:
        return {"ok": False, "tracked": 0, "error": "monitor imports failed"}

    if not is_execution_api_healthy():
        # 한 번 더 probe — 복구되면 모니터 재개
        probe_execution_api()
        if not is_execution_api_healthy():
            return {"ok": False, "tracked": 0, "error": "private API unhealthy"}

    state = st._load_state()
    tracked = state.get("positions", {})
    if not tracked:
        return {"ok": True, "tracked": 0, "live": 0, "closed": 0}

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
        return {"ok": False, "tracked": len(tracked), "error": str(e)[:180]}

    try:
        snapshot = _fetch_position_snapshot(ex, list(tracked))
        _mark_api_ok()
    except Exception as e:
        _mark_api_fail(e)
        print(f"[Binance 모니터] 계좌 포지션 일괄조회 실패: {e}")
        return {"ok": False, "tracked": len(tracked), "error": str(e)[:180]}

    summary = {"ok": True, "tracked": len(tracked), "live": 0, "closed": 0}

    for symbol, info in list(tracked.items()):
        fsym = _futures_symbol(symbol)
        positions = snapshot.get(symbol, [])
        live = [p for p in positions if abs(float(p.get("contracts", 0) or 0)) > 0]
        current_qty = sum(abs(float(p.get("contracts", 0) or 0)) for p in live)
        mark = 0.0
        live_entry = 0.0
        live_leverage = 0.0
        if live:
            summary["live"] += 1
            mark = float(live[0].get("markPrice") or live[0].get("entryPrice") or 0)
            live_entry = float(live[0].get("entryPrice") or 0)
            live_leverage = _position_leverage(live[0])

        # ① 청산 완료
        if current_qty <= 0:
            # 포지션은 닫혔는데 TP/SL가 남으면 다음 진입을 오염시키거나 Algo
            # STOP이 계속 누적된다. 손익 기록 전에 두 주문 원장을 모두 비운다.
            _cancel_all_protective_orders(ex, fsym)
            since_ms = int(float(info.get("opened_ts", 0) or 0) * 1000) - 10_000
            pnl, close_info = _realized_pnl_since(
                ex,
                fsym,
                since_ms,
                entry_order_id=str(info.get("entry_order_id") or ""),
                entry_order_link_id=str(info.get("entry_order_link_id") or ""),
            )
            st.record_result(pnl)
            closed = st._update_trade_result(symbol, pnl, close_info=close_info)
            st._clear_position(symbol)
            if closed:
                try:
                    tg_send(st.build_trade_close_notification(closed))
                except Exception:
                    pass
            print(f"[Binance 모니터] {symbol} 청산 감지 PnL={pnl:.4f}")
            summary["closed"] += 1
            continue

        stored_entry = float(info.get("entry_price", 0) or 0)
        stored_leverage = float(info.get("leverage", 1) or 1)
        entry_changed = live_entry > 0 and abs(live_entry - stored_entry) > max(live_entry * 1e-6, 1e-12)
        leverage_changed = live_leverage > 0 and abs(live_leverage - stored_leverage) > 1e-9
        if entry_changed or leverage_changed:
            latest = st._load_state()
            if symbol in (latest.get("positions") or {}):
                latest["positions"][symbol]["entry_price"] = live_entry or stored_entry
                latest["positions"][symbol]["leverage"] = int(live_leverage or stored_leverage)
                st._save_state(latest)
                info["entry_price"] = live_entry or stored_entry
                info["leverage"] = int(live_leverage or stored_leverage)
                print(
                    f"[Binance 모니터] {symbol} 거래소 메타 동기화 "
                    f"entry {stored_entry:g}→{float(info['entry_price']):g}, "
                    f"lev {stored_leverage:g}x→{float(info['leverage']):g}x"
                )

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

        # D2/D3는 legacy의 "+10% 도달 즉시 +10% SL"을 사용하지 않는다. 최초
        # 구조손절 이후 R 계단, 비용차감 ROI 계단, ATR 간격을 한 후보가격으로
        # 합쳐 정상적인 5m 흔들림을 허용하면서도 이익만 위쪽으로 잠근다.
        if info.get("exit_policy") in {
            "d2_asymmetric", "d3_pullback", "c1_orderflow", "c1v2_asymmetric_scalp",
        }:
            policy_label = {
                "d3_pullback": "D3",
                "c1_orderflow": "C1",
                "c1v2_asymmetric_scalp": "C1v2",
            }.get(info.get("exit_policy"), "D2")
            previous_peak = float(info.get("peak_price") or entry_price)
            peak_price = (
                max(previous_peak, current_price)
                if direction == "LONG" else min(previous_peak, current_price)
            )
            peak_favorable = (
                peak_price - entry_price if direction == "LONG"
                else entry_price - peak_price
            )
            peak_r = peak_favorable / risk if risk > 0 else 0.0
            age_minutes = max(
                (time.time() - float(info.get("opened_ts", 0) or 0)) / 60,
                0.0,
            )
            initial_qty = float(info.get("initial_qty", 0) or 0)
            partial_filled = bool(
                initial_qty > 0 and current_qty < initial_qty * 0.85
            )

            # TP1 체결은 시장가 스냅샷을 놓쳤더라도 가격이 최소 1R에 닿았다는
            # 거래소 체결 증거다. 이를 반영하지 않으면 과거 peak만 보고 잔량 SL
            # 수량을 갱신하지 못해 기존 전체수량 STOP이 남는다.
            if partial_filled and risk > 0:
                implied_tp1_peak = (
                    entry_price + risk
                    if direction == "LONG" else entry_price - risk
                )
                peak_price = (
                    max(peak_price, implied_tp1_peak)
                    if direction == "LONG" else min(peak_price, implied_tp1_peak)
                )
                peak_favorable = (
                    peak_price - entry_price
                    if direction == "LONG" else entry_price - peak_price
                )
                peak_r = max(peak_favorable / risk, 1.0)

            latest = st._load_state()
            if symbol in (latest.get("positions") or {}):
                latest["positions"][symbol]["peak_price"] = peak_price
                latest["positions"][symbol]["peak_r"] = peak_r
                if partial_filled:
                    latest["positions"][symbol]["tp1_detected"] = True
                st._save_state(latest)
            info["peak_price"] = peak_price
            info["peak_r"] = peak_r
            if partial_filled:
                info["tp1_detected"] = True

            max_hold = float(info.get("max_hold_minutes") or 90.0)
            progress_minutes = float(info.get("progress_check_minutes") or 30.0)
            progress_min_r = float(info.get("progress_min_r") or 0.50)
            time_exit_reason = ""
            if max_hold > 0 and age_minutes >= max_hold:
                time_exit_reason = (
                    f"{policy_label} 최대보유 {age_minutes:.0f}분/{max_hold:.0f}분"
                )
            elif (
                progress_minutes > 0
                and age_minutes >= progress_minutes
                and peak_r < progress_min_r
                and not partial_filled
            ):
                time_exit_reason = (
                    f"{policy_label} 무진행 {age_minutes:.0f}분, MFE {peak_r:.2f}R "
                    f"< {progress_min_r:.2f}R"
                )
            if time_exit_reason and not info.get("time_exit_requested"):
                _, close_side = _side(direction)
                closed = _emergency_market_close(
                    ex, fsym, close_side, current_qty, symbol,
                    reason=time_exit_reason,
                )
                if closed:
                    _cancel_all_protective_orders(ex, fsym)
                    latest = st._load_state()
                    if symbol in (latest.get("positions") or {}):
                        latest["positions"][symbol]["time_exit_requested"] = True
                        latest["positions"][symbol]["time_exit_reason"] = time_exit_reason
                        st._save_state(latest)
                    print(
                        f"[Binance {policy_label} 시간청산] "
                        f"{symbol} {time_exit_reason}"
                    )
                continue

            stop_candidates: list[tuple[float, str]] = []
            if risk > 0:
                if peak_r >= 2.0:
                    lock_r = 1.25
                elif peak_r >= 1.5:
                    lock_r = 0.75
                elif peak_r >= 1.0:
                    lock_r = float(info.get("tp1_lock_r") or 0.20)
                else:
                    lock_r = 0.0
                if lock_r > 0:
                    r_stop = (
                        entry_price + risk * lock_r
                        if direction == "LONG"
                        else entry_price - risk * lock_r
                    )
                    stop_candidates.append((r_stop, f"{peak_r:.2f}R→+{lock_r:.2f}R"))

            cost_roi_pct = BINANCE_ROUND_TRIP_EXECUTION_COST * leverage * 100
            peak_net_roi = (
                peak_favorable / entry_price * leverage * 100 - cost_roi_pct
                if entry_price > 0 else 0.0
            )
            if peak_net_roi >= 10.0:
                step = math.floor((peak_net_roi - 10.0) / 5.0)
                lock_net_roi = 3.0 + step * 5.0
                lock_price_fraction = (
                    lock_net_roi / (100 * max(leverage, 1.0))
                    + BINANCE_ROUND_TRIP_EXECUTION_COST
                )
                roi_stop = (
                    entry_price * (1 + lock_price_fraction)
                    if direction == "LONG"
                    else entry_price * (1 - lock_price_fraction)
                )
                stop_candidates.append(
                    (roi_stop, f"순ROI {peak_net_roi:.1f}%→{lock_net_roi:.1f}%")
                )

            trail_activation_r = float(info.get("trail_activation_r") or 2.0)
            if atr > 0 and peak_r >= trail_activation_r:
                trail_mult = float(info.get("trail_atr_mult") or 1.0)
                trail_stop = (
                    peak_price - atr * trail_mult
                    if direction == "LONG"
                    else peak_price + atr * trail_mult
                )
                stop_candidates.append(
                    (trail_stop, f"고점추적 {trail_mult:.2f}ATR")
                )

            if stop_candidates and atr > 0:
                # 어떤 계단도 peak와 최소 0.8ATR 간격을 침범하지 못하게 한다.
                volatility_edge = (
                    peak_price - atr * 0.80
                    if direction == "LONG"
                    else peak_price + atr * 0.80
                )
                adjusted: list[tuple[float, str]] = []
                for candidate, label in stop_candidates:
                    safe_candidate = (
                        min(candidate, volatility_edge)
                        if direction == "LONG"
                        else max(candidate, volatility_edge)
                    )
                    adjusted.append((safe_candidate, label))
                stop_candidates = adjusted

            if stop_candidates:
                protect_sl, protect_reason = (
                    max(stop_candidates, key=lambda item: item[0])
                    if direction == "LONG"
                    else min(stop_candidates, key=lambda item: item[0])
                )
                improves = (
                    direction == "LONG" and protect_sl > current_sl
                    or direction == "SHORT" and protect_sl < current_sl
                )
                valid = (
                    direction == "LONG" and current_price > protect_sl
                    or direction == "SHORT" and current_price < protect_sl
                )
                min_advance = atr * 0.10 if atr > 0 else entry_price * 0.0005
                advanced_enough = abs(protect_sl - current_sl) >= min_advance
                if improves and valid and advanced_enough:
                    try:
                        px = _set_stop_loss(
                            ex, fsym, direction, current_qty, protect_sl
                        )
                        latest = st._load_state()
                        if symbol in (latest.get("positions") or {}):
                            latest["positions"][symbol]["sl_price"] = px
                            latest["positions"][symbol]["trail_sl"] = px
                            latest["positions"][symbol]["pre_tp_be_done"] = peak_r >= 1.0
                            latest["positions"][symbol]["peak_net_roi_pct"] = peak_net_roi
                            if partial_filled:
                                latest["positions"][symbol]["be_done"] = True
                                latest["positions"][symbol]["sl_qty_synced_after_tp1"] = True
                            st._save_state(latest)
                        info["sl_price"] = px
                        current_sl = px
                        print(
                            f"[Binance D2 수익보호] {symbol} {protect_reason} "
                            f"peak={peak_r:.2f}R → SL ${px}"
                        )
                    except Exception as exc:
                        print(f"[Binance D2 수익보호] {symbol} 실패: {exc}")

            # 후보가격이 현 시세보다 위라 이동할 수 없는 경우에도 기존 SL을
            # 현재 잔량 수량으로 다시 접수한다. 가격은 그대로여도 주문 수량 정합성은
            # 반드시 맞아야 한다.
            if partial_filled and not info.get("sl_qty_synced_after_tp1"):
                latest = st._load_state()
                latest_info = (latest.get("positions") or {}).get(symbol, {})
                if not latest_info.get("sl_qty_synced_after_tp1") and current_sl > 0:
                    try:
                        px = _set_stop_loss(
                            ex, fsym, direction, current_qty, current_sl
                        )
                        latest = st._load_state()
                        if symbol in (latest.get("positions") or {}):
                            latest["positions"][symbol]["sl_price"] = px
                            latest["positions"][symbol]["be_done"] = True
                            latest["positions"][symbol]["sl_qty_synced_after_tp1"] = True
                            st._save_state(latest)
                        info["sl_qty_synced_after_tp1"] = True
                        info["be_done"] = True
                        print(
                            f"[Binance {policy_label} TP1] {symbol} "
                            f"잔량 {current_qty:g} 기준 SL 수량 동기화"
                        )
                    except Exception as exc:
                        print(f"[Binance {policy_label} TP1] 잔량 SL 동기화 실패: {exc}")
            continue

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
            fee_buffer = (
                entry_price
                * BINANCE_ROUND_TRIP_EXECUTION_COST
                * BE_FEE_CUSHION_MULT
            )
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

    return summary
