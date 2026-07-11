"""Polymarket CLOB 실주문 어댑터 (초소액 라이브용).

안전장치:
  - POLYMARKET_LIVE_TRADING_ENABLED=true 일 때만 실주문
  - 그 외에는 dry-run 로그만
  - py-clob-client 미설치 시 실주문 불가 (명확한 에러)

환경변수:
  POLYMARKET_PRIVATE_KEY   Polygon EOA private key (0x...)
  POLYMARKET_FUNDER        (선택) proxy/funder 주소
  POLYMARKET_CHAIN_ID      기본 137
  POLYMARKET_CLOB_HOST     기본 https://clob.polymarket.com
  POLYMARKET_LIVE_TRADING_ENABLED  true 일 때만 live
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

CLOB_HOST = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").strip()
CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137") or 137)


def live_enabled() -> bool:
    return os.getenv("POLYMARKET_LIVE_TRADING_ENABLED", "").strip().lower() == "true"


def _private_key() -> str:
    return (os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY") or "").strip()


def client_available() -> tuple[bool, str]:
    try:
        import py_clob_client  # noqa: F401
        return True, "py_clob_client"
    except Exception:
        pass
    try:
        import py_clob_client_v2  # noqa: F401
        return True, "py_clob_client_v2"
    except Exception:
        pass
    return False, "pip install py-clob-client  (또는 py-clob-client-v2)"


def _build_client():
    key = _private_key()
    if not key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY 없음")
    ok, which = client_available()
    if not ok:
        raise RuntimeError(f"CLOB 클라이언트 없음 — {which}")

    funder = (os.getenv("POLYMARKET_FUNDER") or "").strip() or None

    if which == "py_clob_client_v2":
        from py_clob_client_v2 import ClobClient
        client = ClobClient(CLOB_HOST, key=key, chain_id=CHAIN_ID, funder=funder)
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
        except Exception:
            try:
                creds = client.create_or_derive_api_key()
                client.set_api_creds(creds)
            except Exception as e:
                raise RuntimeError(f"CLOB API creds 실패: {e}") from e
        return client, "v2"

    from py_clob_client.client import ClobClient
    kwargs = {"key": key, "chain_id": CHAIN_ID}
    if funder:
        kwargs["funder"] = funder
    client = ClobClient(CLOB_HOST, **kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client, "v1"


def get_usdc_balance_approx() -> float:
    """가능하면 CLOB balance, 실패 시 -1."""
    if not live_enabled() or not _private_key():
        return -1.0
    try:
        client, _ = _build_client()
        # 버전별 API 차이 — best effort
        if hasattr(client, "get_balance_allowance"):
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                bal = client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                return float(bal.get("balance") or bal.get("total") or 0) / 1e6
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

    plan = {
        "ok": False,
        "dry_run": dry_run,
        "token_id": str(token_id),
        "usd_amount": float(usd_amount),
        "price_hint": price_hint,
        "order_id": None,
        "error": "",
        "raw": None,
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
        client, ver = _build_client()
        # FOK market-style buy by USD when available
        if ver == "v2":
            from py_clob_client_v2 import MarketOrderArgs, OrderType
            try:
                from py_clob_client_v2.order_builder.constants import BUY
            except Exception:
                BUY = "BUY"
            mo = MarketOrderArgs(
                token_id=str(token_id),
                amount=float(usd_amount),
                side=BUY,
                order_type=OrderType.FOK,
            )
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

        plan["ok"] = True
        plan["raw"] = resp if isinstance(resp, dict) else {"resp": str(resp)}
        if isinstance(resp, dict):
            plan["order_id"] = resp.get("orderID") or resp.get("id") or resp.get("order_id")
        return plan
    except Exception as e:
        plan["error"] = str(e)[:400]
        return plan


def smoke_test() -> dict[str, Any]:
    ok_pkg, which = client_available()
    return {
        "live_enabled": live_enabled(),
        "private_key_set": bool(_private_key()),
        "client_package": which if ok_pkg else None,
        "client_ok": ok_pkg,
        "hint": (
            "LIVE 실주문: POLYMARKET_LIVE_TRADING_ENABLED=true + "
            "POLYMARKET_PRIVATE_KEY + pip install py-clob-client"
        ),
    }
