"""Polymarket CLOB 실주문 어댑터 (초소액 라이브용).

2026 CLOB v2 마이그레이션 이후 py-clob-client(v1) 주문은
`invalid order version` 으로 거절된다. **py-clob-client-v2 를 우선** 사용.

안전장치:
  - POLYMARKET_LIVE_TRADING_ENABLED=true 일 때만 실주문
  - 그 외에는 dry-run 로그만

환경변수:
  POLYMARKET_PRIVATE_KEY   Polygon EOA private key (0x...) — MetaMask 등 서명 키
  POLYMARKET_FUNDER        Polymarket 프로필/deposit 지갑 (잔고가 여기 있음)
  POLYMARKET_SIGNATURE_TYPE  0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE, 3=POLY_1271(deposit)
                             웹 입금 계정은 보통 funder=proxyWallet + type=3
  POLYMARKET_CHAIN_ID      기본 137
  POLYMARKET_CLOB_HOST     기본 https://clob.polymarket.com
  POLYMARKET_LIVE_TRADING_ENABLED  true 일 때만 live

주의: MetaMask 주소 ≠ Polymarket 잔고 주소인 경우가 많다.
  서명=EOA, 잔고=proxyWallet(deposit). funder 미설정 시 EOA 잔고 $0 으로 보임.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

CLOB_HOST = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").strip()
CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137") or 137)
_PROXY_CACHE: dict[str, str | None] = {}


def live_enabled() -> bool:
    return os.getenv("POLYMARKET_LIVE_TRADING_ENABLED", "").strip().lower() == "true"


def _private_key() -> str:
    return (os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY") or "").strip()


def _eoa_address() -> str | None:
    key = _private_key()
    if not key:
        return None
    if not key.startswith("0x"):
        key = "0x" + key
    try:
        from eth_account import Account
        return Account.from_key(key).address
    except Exception:
        return None


def resolve_proxy_wallet(eoa: str | None = None) -> str | None:
    """gamma public-profile 로 Polymarket proxyWallet 조회."""
    addr = (eoa or _eoa_address() or "").strip()
    if not addr.startswith("0x"):
        return None
    key = addr.lower()
    if key in _PROXY_CACHE:
        return _PROXY_CACHE[key]
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/public-profile",
            params={"address": addr},
            timeout=15,
        )
        if r.ok:
            proxy = (r.json() or {}).get("proxyWallet")
            if proxy and str(proxy).startswith("0x"):
                _PROXY_CACHE[key] = str(proxy)
                return _PROXY_CACHE[key]
    except Exception:
        pass
    _PROXY_CACHE[key] = None
    return None


def _funder() -> str | None:
    f = (os.getenv("POLYMARKET_FUNDER") or os.getenv("POLYMARKET_FUNDER_ADDRESS") or "").strip()
    if f:
        return f
    # 자동: 웹 계정 proxyWallet (deposit 잔고 위치)
    return resolve_proxy_wallet()


def get_wallet_address() -> str | None:
    """Public wallet that owns collateral/outcome tokens (never returns a key)."""
    return _funder() or _eoa_address()


def _signature_type() -> int | None:
    raw = (os.getenv("POLYMARKET_SIGNATURE_TYPE") or "").strip()
    if raw:
        try:
            return int(raw)
        except Exception:
            return None
    # funder 가 proxy 면 2026 deposit wallet 기본 = POLY_1271 (3)
    if _funder():
        return 3
    return None  # EOA

def client_available() -> tuple[bool, str]:
    """v2 우선 (CLOB 백엔드 2026 마이그레이션 대응)."""
    try:
        import py_clob_client_v2  # noqa: F401
        return True, "py_clob_client_v2"
    except Exception:
        pass
    try:
        import py_clob_client  # noqa: F401
        return True, "py_clob_client"
    except Exception:
        pass
    return False, "pip install py-clob-client-v2"


def _package_version(which: str) -> str:
    try:
        import importlib.metadata as m
        if which == "py_clob_client_v2":
            return m.version("py-clob-client-v2")
        return m.version("py-clob-client")
    except Exception:
        return "?"


def _build_client():
    key = _private_key()
    if not key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY 없음")
    ok, which = client_available()
    if not ok:
        raise RuntimeError(f"CLOB 클라이언트 없음 — {which}")

    funder = _funder()
    sig = _signature_type()

    if which == "py_clob_client_v2":
        from py_clob_client_v2 import ClobClient

        kwargs: dict[str, Any] = {
            "host": CLOB_HOST,
            "chain_id": CHAIN_ID,
            "key": key,
        }
        if funder:
            kwargs["funder"] = funder
        if sig is not None:
            kwargs["signature_type"] = sig
        client = ClobClient(**kwargs)
        try:
            creds = client.create_or_derive_api_key()
            client.set_api_creds(creds)
        except Exception as e:
            raise RuntimeError(f"CLOB API creds 실패 (v2): {e}") from e
        return client, "v2"

    # legacy v1 — 서버가 v2-only 면 invalid order version
    from py_clob_client.client import ClobClient

    kwargs = {"key": key, "chain_id": CHAIN_ID}
    if funder:
        kwargs["funder"] = funder
    if sig is not None:
        kwargs["signature_type"] = sig
    client = ClobClient(CLOB_HOST, **kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client, "v1"


def _matched_fill(resp: Any, *, side: str) -> dict[str, Any] | None:
    """CLOB 응답이 실제 MATCHED 체결일 때만 체결 금액/수량을 반환한다.

    `success=true`와 orderID만 있는 delayed/live 응답은 실제 체결이 아니다.
    이를 성공으로 기록하면 지갑에 없는 유령 포지션이 생긴다.
    """
    if not isinstance(resp, dict):
        return None
    if resp.get("success") is False or str(resp.get("errorMsg") or resp.get("error") or ""):
        return None
    status = str(resp.get("status") or "").strip().lower()
    if status not in {"matched", "order_status_matched"}:
        return None
    # GET /order 응답은 making/taking 대신 size_matched + price를 준다.
    if resp.get("makingAmount") is None or resp.get("takingAmount") is None:
        try:
            size = float(resp.get("size_matched") or 0)
            price = float(resp.get("price") or 0)
        except (TypeError, ValueError):
            return None
        if size >= 10_000:
            size /= 1_000_000.0
        if size <= 0 or price <= 0:
            return None
        return {
            "fill_status": "matched",
            "filled_usd": size * price,
            "filled_shares": size,
            "fill_price": price,
            "transaction_hashes": [],
            "trade_ids": resp.get("associate_trades") or [],
        }
    try:
        making = float(resp.get("makingAmount") or 0)
        taking = float(resp.get("takingAmount") or 0)
    except (TypeError, ValueError):
        return None
    if making <= 0 or taking <= 0:
        return None

    # API 버전에 따라 사람 단위 또는 1e6 fixed-math 문자열이 올 수 있다.
    scale = 1_000_000.0 if max(making, taking) >= 10_000 else 1.0
    making /= scale
    taking /= scale
    if side.upper() == "BUY":
        filled_usd, filled_shares = making, taking
    else:
        filled_shares, filled_usd = making, taking
    if filled_usd <= 0 or filled_shares <= 0:
        return None
    return {
        "fill_status": "matched",
        "filled_usd": filled_usd,
        "filled_shares": filled_shares,
        "fill_price": filled_usd / filled_shares,
        "transaction_hashes": resp.get("transactionsHashes") or [],
        "trade_ids": resp.get("tradeIDs") or [],
    }


def _confirm_delayed_fill(
    client: Any,
    resp: Any,
    *,
    side: str,
    timeout_seconds: float = 8.0,
) -> tuple[dict[str, Any] | None, Any]:
    """V2 delayed 응답을 짧게 폴링해 MATCHED terminal 상태만 승인한다."""
    fill = _matched_fill(resp, side=side)
    if fill:
        return fill, resp
    if not isinstance(resp, dict) or str(resp.get("status") or "").lower() != "delayed":
        return None, resp
    order_id = resp.get("orderID") or resp.get("id") or resp.get("order_id")
    if not order_id or not hasattr(client, "get_order"):
        return None, resp

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    last = resp
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            checked = client.get_order(str(order_id))
        except Exception:
            continue
        if isinstance(checked, dict):
            last = checked
            fill = _matched_fill(checked, side=side)
            if fill:
                return fill, checked
            status = str(checked.get("status") or "").lower()
            if status in {"unmatched", "cancelled", "canceled", "failed", "rejected"}:
                return None, checked
    return None, last


def get_usdc_balance_approx() -> float:
    """가능하면 CLOB balance, 실패 시 -1."""
    if not live_enabled() or not _private_key():
        return -1.0
    try:
        client, ver = _build_client()
        if hasattr(client, "get_balance_allowance"):
            try:
                if ver == "v2":
                    from py_clob_client_v2.clob_types import (
                        AssetType,
                        BalanceAllowanceParams,
                    )
                else:
                    from py_clob_client.clob_types import (
                        AssetType,
                        BalanceAllowanceParams,
                    )
                bal = client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                raw = float(bal.get("balance") or bal.get("total") or 0)
                # 일부 응답은 이미 USDC 단위, 일부는 1e6
                return raw / 1e6 if raw > 1e4 else raw
            except Exception:
                pass
        return -1.0
    except Exception:
        return -1.0


def place_buy_usd(
    token_id: str,
    usd_amount: float,
    *,
    price_hint: float | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """
    outcome 토큰 매수. usd_amount 만큼 (대략) 사용.
    dry_run=True 이거나 LIVE 플래그 off 면 주문 없이 계획만 반환.
    """
    if dry_run is None:
        dry_run = not live_enabled()

    plan: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "token_id": str(token_id),
        "usd_amount": float(usd_amount),
        "price_hint": price_hint,
        "order_id": None,
        "error": "",
        "raw": None,
        "client_ver": None,
    }

    if usd_amount <= 0 or not token_id:
        plan["error"] = "invalid token_id/usd_amount"
        return plan

    if dry_run or not live_enabled():
        plan["ok"] = True
        plan["error"] = ""
        plan["raw"] = {"note": "dry-run — no order submitted"}
        return plan

    if not _private_key():
        plan["error"] = "POLYMARKET_PRIVATE_KEY 미설정"
        return plan

    try:
        # 잔고 사전 점검 — 0이면 주문 스팸 방지 (version 오류와 구분)
        bal = get_usdc_balance_approx()
        plan["usdc_balance_approx"] = bal
        if bal >= 0 and bal < float(usd_amount) * 0.95:
            plan["error"] = (
                f"USDC 부족: balance≈${bal:.2f} < need ${float(usd_amount):.2f} "
                f"(signer에 입금하거나 POLYMARKET_FUNDER=proxy주소 설정)"
            )
            return plan

        client, ver = _build_client()
        plan["client_ver"] = ver

        if ver == "v2":
            from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY

            # user_usdc_balance 가 fee 조정에 쓰임 — best effort
            usdc = 0.0
            try:
                b = get_usdc_balance_approx()
                if b and b > 0:
                    usdc = float(b)
            except Exception:
                pass

            mo_kwargs: dict[str, Any] = {
                "token_id": str(token_id),
                "amount": float(usd_amount),
                "side": BUY,
                "order_type": OrderType.FOK,
            }
            if price_hint and 0 < float(price_hint) < 1:
                mo_kwargs["price"] = float(price_hint)
            if usdc > 0:
                mo_kwargs["user_usdc_balance"] = usdc

            mo = MarketOrderArgs(**mo_kwargs)
            # v2: 버전 불일치 시 자동 재시도
            if hasattr(client, "create_and_post_market_order"):
                resp = client.create_and_post_market_order(mo, order_type=OrderType.FOK)
            else:
                signed = client.create_market_order(mo)
                resp = client.post_order(signed, OrderType.FOK)
        else:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            mo = MarketOrderArgs(
                token_id=str(token_id),
                amount=float(usd_amount),
                side=BUY,
                order_type=OrderType.FOK,
            )
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)

        fill, terminal_resp = _confirm_delayed_fill(client, resp, side="BUY")
        plan["raw"] = terminal_resp if isinstance(terminal_resp, dict) else {"resp": str(terminal_resp)}
        if isinstance(resp, dict):
            err = resp.get("error") or resp.get("errorMsg") or ""
            # 일부 응답은 ok 필드 / success
            if err and "version" in str(err).lower():
                plan["error"] = str(err)[:400]
                plan["ok"] = False
                return plan
            if resp.get("success") is False or (
                resp.get("status") in ("failed", "error", "rejected")
            ):
                plan["error"] = str(err or resp)[:400]
                plan["ok"] = False
                return plan
            plan["order_id"] = (
                resp.get("orderID")
                or resp.get("id")
                or resp.get("order_id")
                or resp.get("orderIds")
            )
        if not fill:
            plan["error"] = (
                f"order not confirmed matched: status={str((terminal_resp or {}).get('status') or 'unknown')}"
                if isinstance(terminal_resp, dict)
                else "order not confirmed matched"
            )
            return plan
        plan.update(fill)
        plan["ok"] = True
        return plan
    except Exception as e:
        msg = str(e)
        if "invalid order version" in msg.lower() or "order_version_mismatch" in msg.lower():
            msg = (
                f"{msg} | fix: pip install -U py-clob-client-v2 "
                f"(v1 py-clob-client is obsolete after CLOB v2 migration)"
            )
        plan["error"] = msg[:400]
        return plan


def place_sell_shares(
    token_id: str,
    shares: float,
    *,
    price_hint: float | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """outcome 토큰 매도 (고래 청산/플립 추종용). amount = shares."""
    if dry_run is None:
        dry_run = not live_enabled()

    plan: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "token_id": str(token_id),
        "shares": float(shares),
        "price_hint": price_hint,
        "order_id": None,
        "error": "",
        "raw": None,
        "client_ver": None,
    }
    if shares <= 0 or not token_id:
        plan["error"] = "invalid token_id/shares"
        return plan
    if dry_run or not live_enabled():
        plan["ok"] = True
        plan["raw"] = {"note": "dry-run sell — no order"}
        return plan
    if not _private_key():
        plan["error"] = "POLYMARKET_PRIVATE_KEY 미설정"
        return plan
    try:
        client, ver = _build_client()
        plan["client_ver"] = ver
        if ver == "v2":
            from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import SELL

            mo_kwargs: dict[str, Any] = {
                "token_id": str(token_id),
                "amount": float(shares),
                "side": SELL,
                "order_type": OrderType.FOK,
            }
            if price_hint and 0 < float(price_hint) < 1:
                mo_kwargs["price"] = float(price_hint)
            mo = MarketOrderArgs(**mo_kwargs)
            if hasattr(client, "create_and_post_market_order"):
                resp = client.create_and_post_market_order(mo, order_type=OrderType.FOK)
            else:
                signed = client.create_market_order(mo)
                resp = client.post_order(signed, OrderType.FOK)
        else:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            mo = MarketOrderArgs(
                token_id=str(token_id),
                amount=float(shares),
                side=SELL,
                order_type=OrderType.FOK,
            )
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)

        fill, terminal_resp = _confirm_delayed_fill(client, resp, side="SELL")
        plan["raw"] = terminal_resp if isinstance(terminal_resp, dict) else {"resp": str(terminal_resp)}
        if isinstance(resp, dict):
            err = resp.get("error") or resp.get("errorMsg") or ""
            if resp.get("success") is False or (
                resp.get("status") in ("failed", "error", "rejected")
            ):
                plan["error"] = str(err or resp)[:400]
                return plan
            plan["order_id"] = (
                resp.get("orderID") or resp.get("id") or resp.get("order_id")
            )
        if not fill:
            plan["error"] = (
                f"order not confirmed matched: status={str((terminal_resp or {}).get('status') or 'unknown')}"
                if isinstance(terminal_resp, dict)
                else "order not confirmed matched"
            )
            return plan
        plan.update(fill)
        plan["ok"] = True
        return plan
    except Exception as e:
        plan["error"] = str(e)[:400]
        return plan


def smoke_test() -> dict[str, Any]:
    ok_pkg, which = client_available()
    eoa = _eoa_address()
    funder = _funder()
    info: dict[str, Any] = {
        "live_enabled": live_enabled(),
        "private_key_set": bool(_private_key()),
        "signer_eoa": eoa,
        "funder": funder,
        "funder_set": bool(funder),
        "funder_is_proxy_not_eoa": bool(
            eoa and funder and eoa.lower() != funder.lower()
        ),
        "signature_type": _signature_type(),
        "client_package": which if ok_pkg else None,
        "client_version": _package_version(which) if ok_pkg else None,
        "client_ok": ok_pkg,
        "prefer": "py-clob-client-v2 (required after 2026 CLOB migration)",
        "hint": (
            "웹 입금 잔고는 MetaMask EOA가 아니라 proxyWallet(funder)에 있음. "
            "POLYMARKET_FUNDER + POLYMARKET_SIGNATURE_TYPE=3 필요."
        ),
    }
    if ok_pkg and live_enabled() and _private_key():
        try:
            client, ver = _build_client()
            info["build_ok"] = True
            info["build_ver"] = ver
            if hasattr(client, "get_version"):
                try:
                    info["exchange_order_version"] = client.get_version()
                except Exception as e:
                    info["exchange_order_version_error"] = str(e)[:120]
            bal = get_usdc_balance_approx()
            info["usdc_balance_approx"] = bal
            if bal is not None and bal >= 0 and bal < 1:
                info["balance_warning"] = (
                    "CLOB USDC≈0 — funder/signature_type 확인. "
                    "웹 UI 잔고와 EOA 온체인 잔고는 다를 수 있음."
                )
        except Exception as e:
            info["build_ok"] = False
            info["build_error"] = str(e)[:300]
    return info
