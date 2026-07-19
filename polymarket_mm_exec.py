"""Polymarket market-making 전용 post-only 주문 어댑터.

고래 카피의 FOK 주문과 완전히 분리한다. 실주문은 아래 두 조건이 모두 참일 때만
가능하다.

* POLYMARKET_LIVE_TRADING_ENABLED=true
* POLYMARKET_MM_LIVE_ENABLED=true

호가는 GTD + post-only로 제출하며, 이 모듈이 만든 주문 ID만 취소한다.
"""
from __future__ import annotations

import os
import time
from typing import Any

import polymarket_clob_exec as clob


def mm_live_enabled() -> bool:
    return (
        clob.live_enabled()
        and os.getenv("POLYMARKET_MM_LIVE_ENABLED", "").strip().lower() == "true"
    )


def _result_ok(response: Any) -> bool:
    return bool(
        isinstance(response, dict)
        and response.get("success") is not False
        and not str(response.get("error") or response.get("errorMsg") or "")
        and response.get("orderID")
        and str(response.get("status") or "").lower() in {"live", "matched", "delayed"}
    )


def post_quotes(quotes: list[dict[str, Any]], *, lifetime_seconds: int = 900,
                dry_run: bool | None = None) -> list[dict[str, Any]]:
    """여러 outcome BUY/SELL 호가를 하나의 post-only batch로 제출한다."""
    if dry_run is None:
        dry_run = not mm_live_enabled()
    plans = [{
        "ok": False,
        "dry_run": bool(dry_run),
        "token_id": str(row.get("token_id") or ""),
        "price": float(row.get("price") or 0),
        "size": float(row.get("size") or 0),
        "side": str(row.get("side") or "BUY").upper(),
        "order_id": None,
        "status": "",
        "error": "",
    } for row in quotes]
    for plan in plans:
        if (
            not plan["token_id"] or not (0 < plan["price"] < 1)
            or plan["size"] <= 0 or plan["side"] not in {"BUY", "SELL"}
        ):
            plan["error"] = "invalid quote"
    if any(plan["error"] for plan in plans):
        return plans
    if dry_run or not mm_live_enabled():
        for plan in plans:
            plan.update({"ok": True, "status": "paper", "error": ""})
        return plans

    try:
        client, version = clob._build_client()
        if version != "v2":
            raise RuntimeError("market making requires py-clob-client-v2")
        from py_clob_client_v2 import (
            OrderArgs, OrderType, PartialCreateOrderOptions, PostOrdersV2Args,
        )
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        # GTD는 60초 security threshold가 추가로 필요하다.
        expiration = int(time.time()) + 60 + max(int(lifetime_seconds), 10)
        signed = []
        for plan, source in zip(plans, quotes):
            options = PartialCreateOrderOptions(
                tick_size=str(source.get("tick_size") or "0.01"),
                neg_risk=bool(source.get("neg_risk", False)),
            )
            order = client.create_order(
                OrderArgs(
                    token_id=plan["token_id"],
                    price=plan["price"],
                    size=plan["size"],
                    side=BUY if plan["side"] == "BUY" else SELL,
                    expiration=expiration,
                ),
                options=options,
            )
            signed.append(PostOrdersV2Args(order=order, orderType=OrderType.GTD))
        responses = client.post_orders(signed, post_only=True)
        if not isinstance(responses, list):
            responses = [responses]
        for index, plan in enumerate(plans):
            response = responses[index] if index < len(responses) else {}
            plan["ok"] = _result_ok(response)
            plan["order_id"] = response.get("orderID") if isinstance(response, dict) else None
            plan["status"] = str(response.get("status") or "") if isinstance(response, dict) else ""
            plan["error"] = str(
                response.get("error") or response.get("errorMsg") or ""
            )[:300] if isinstance(response, dict) else str(response)[:300]
        return plans
    except Exception as exc:
        for plan in plans:
            plan["error"] = str(exc)[:400]
        return plans


def post_buy_quotes(quotes: list[dict[str, Any]], *, lifetime_seconds: int = 900,
                    dry_run: bool | None = None) -> list[dict[str, Any]]:
    """기존 호출부 호환용 BUY wrapper."""
    return post_quotes(
        [{**row, "side": "BUY"} for row in quotes],
        lifetime_seconds=lifetime_seconds,
        dry_run=dry_run,
    )


def cancel_mm_orders(order_ids: list[str], *, dry_run: bool | None = None) -> dict[str, Any]:
    """전달된 MM 주문만 취소한다. 계정 전체 cancel-all은 사용하지 않는다."""
    unique = list(dict.fromkeys(str(value) for value in order_ids if value))
    if dry_run is None:
        dry_run = not mm_live_enabled()
    if not unique:
        return {"ok": True, "dry_run": bool(dry_run), "canceled": []}
    if dry_run or not mm_live_enabled():
        return {"ok": True, "dry_run": True, "canceled": unique}
    try:
        client, version = clob._build_client()
        if version != "v2":
            raise RuntimeError("market making requires py-clob-client-v2")
        response = client.cancel_orders(unique)
        canceled = response.get("canceled") or [] if isinstance(response, dict) else []
        return {
            "ok": len(canceled) == len(unique),
            "dry_run": False,
            "canceled": canceled,
            "not_canceled": response.get("not_canceled") or {} if isinstance(response, dict) else {},
        }
    except Exception as exc:
        return {"ok": False, "dry_run": False, "canceled": [], "error": str(exc)[:400]}


def get_mm_order(order_id: str) -> dict[str, Any]:
    if not mm_live_enabled() or not order_id:
        return {"ok": False, "error": "MM live disabled"}
    try:
        client, version = clob._build_client()
        if version != "v2":
            raise RuntimeError("market making requires py-clob-client-v2")
        row = client.get_order(str(order_id))
        return {"ok": isinstance(row, dict), "order": row}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:400]}
