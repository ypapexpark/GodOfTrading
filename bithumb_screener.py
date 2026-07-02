"""Bithumb KRW daily screeners."""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (BITHUMB_MA200_ALERT_ENABLED, BITHUMB_MA200_ALERT_HOUR,
                    BITHUMB_MA200_ALERT_TIMES,
                    BITHUMB_MA200_ABOVE_LOOKBACK_DAYS,
                    BITHUMB_MA200_ABOVE_MIN_DAYS,
                    BITHUMB_MA200_CANDLE_COUNT, BITHUMB_MA200_MAX_ROWS_PER_MSG,
                    BITHUMB_MA200_REQUEST_DELAY)

KST = timezone(timedelta(hours=9))
API_BASE = "https://api.bithumb.com/v1"
STATE_FILE = Path(__file__).parent / "bithumb_screener_state.json"


def _get_json(path: str, params: dict | None = None, timeout: int = 10):
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    req = urllib.request.Request(
        f"{API_BASE}{path}{query}",
        headers={"accept": "application/json", "user-agent": "GodOfTrading/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_bithumb_krw_markets() -> list[dict]:
    markets = _get_json("/market/all", {"isDetails": "false"})
    return [m for m in markets if str(m.get("market", "")).startswith("KRW-")]


def fetch_bithumb_daily_candles(market: str, count: int = BITHUMB_MA200_CANDLE_COUNT) -> list[dict]:
    return _get_json("/candles/days", {"market": market, "count": count})


def _sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[:period]) / period


def _ratio(value: float, base: float) -> float:
    return (value / base - 1) * 100 if base > 0 else 0.0


def _volume_ratio(latest_value: float, values: list[float], period: int = 20) -> float:
    past = values[1:period + 1]
    if not past:
        return 0.0
    avg = sum(past) / len(past)
    return latest_value / avg if avg > 0 else 0.0


def _above_regime(closes: list[float], ma_period: int, bb_period: int,
                  lookback: int, min_days: int) -> dict:
    """최근 N개 일봉이 MA200/BB중단 위에서 유지되는지 평가한다.

    빗썸 일봉은 최신순으로 내려온다. 과거 일봉의 MA200까지 계산할 만큼
    데이터가 있으면 각 일자별 이동평균을 쓰고, 거래소가 200개만 반환하는
    경우에는 현재 MA200/BB중단을 기준선으로 삼아 최근 캔들 유지 여부를 본다.
    """
    lookback = max(int(lookback or 1), 1)
    min_days = min(max(int(min_days or lookback), 1), lookback)
    if len(closes) < ma_period or len(closes) < bb_period:
        return {"ok": False, "above_count": 0, "lookback": lookback, "latest_above": False}

    enough_rolling_history = len(closes) >= ma_period + lookback - 1
    anchor_ma = _sma(closes, ma_period)
    anchor_bb = _sma(closes, bb_period)
    above_count = 0
    latest_above = False

    for offset, price in enumerate(closes[:lookback]):
        if enough_rolling_history:
            ma = _sma(closes[offset:offset + ma_period], ma_period)
            bb_mid = _sma(closes[offset:offset + bb_period], bb_period)
        else:
            ma = anchor_ma
            bb_mid = anchor_bb
        is_above = bool(price > ma and price > bb_mid)
        if offset == 0:
            latest_above = is_above
        if is_above:
            above_count += 1

    return {
        "ok": bool(latest_above and above_count >= min_days),
        "above_count": above_count,
        "lookback": lookback,
        "latest_above": latest_above,
    }


def _quality_score(distance_pct: float, bb_mid_distance_pct: float,
                   bb_mid_slope_pct: float, trade_value_ratio20: float,
                   above_count: int, above_lookback: int) -> int:
    """조건 통과 종목 안에서 우선순위를 보기 위한 보조 점수."""
    score = 2  # MA200 상회 + BB 중단 상회는 통과 조건 자체로 이미 충족
    if 0 <= distance_pct <= 35:
        score += 1
    if bb_mid_slope_pct > 0:
        score += 1
    if trade_value_ratio20 >= 1.0:
        score += 1
    if above_count >= above_lookback:
        score += 1
    return score


def _score_label(score: int) -> str:
    if score >= 5:
        return "A"
    if score >= 4:
        return "B"
    return "C"


def _alert_slots() -> list[tuple[int, int]]:
    slots: list[tuple[int, int]] = []
    for raw in BITHUMB_MA200_ALERT_TIMES:
        try:
            hour, minute = str(raw).split(":", 1)
            slots.append((int(hour), int(minute)))
        except Exception:
            continue
    if not slots:
        slots.append((int(BITHUMB_MA200_ALERT_HOUR), 0))
    return sorted(set(slots))


def _current_slot(now: datetime) -> str | None:
    current: str | None = None
    for hour, minute in _alert_slots():
        if (now.hour, now.minute) >= (hour, minute):
            current = f"{hour:02d}:{minute:02d}"
    return current


def screen_bithumb_above_ma200(limit: int | None = None) -> dict:
    """KRW 마켓에서 일봉 종가가 MA200과 BB 중단선 위인 종목을 선별한다."""
    markets = fetch_bithumb_krw_markets()
    if limit:
        markets = markets[:limit]

    passed = []
    skipped = []
    errors = []
    scanned = 0

    for idx, market_info in enumerate(markets):
        market = market_info["market"]
        try:
            required_count = (
                BITHUMB_MA200_CANDLE_COUNT
                + max(int(BITHUMB_MA200_ABOVE_LOOKBACK_DAYS or 1), 1)
                - 1
            )
            candles = fetch_bithumb_daily_candles(market, required_count)
            if len(candles) < BITHUMB_MA200_CANDLE_COUNT:
                skipped.append({
                    "market": market,
                    "name": market_info.get("korean_name", ""),
                    "reason": f"일봉 {len(candles)}개",
                })
                continue

            closes = [float(c["trade_price"]) for c in candles]
            trade_values = [
                float(c.get("candle_acc_trade_price", 0) or 0)
                for c in candles
            ]
            price = closes[0]
            ma200 = _sma(closes, BITHUMB_MA200_CANDLE_COUNT)
            bb_mid = _sma(closes, 20)
            prev_bb_mid = sum(closes[1:21]) / 20 if len(closes) >= 21 else 0.0
            if ma200 <= 0 or bb_mid <= 0:
                continue

            scanned += 1
            regime = _above_regime(
                closes,
                BITHUMB_MA200_CANDLE_COUNT,
                20,
                BITHUMB_MA200_ABOVE_LOOKBACK_DAYS,
                BITHUMB_MA200_ABOVE_MIN_DAYS,
            )
            if regime["ok"]:
                latest = candles[0]
                trade_value = float(latest.get("candle_acc_trade_price", 0) or 0)
                distance_pct = _ratio(price, ma200)
                bb_mid_distance_pct = _ratio(price, bb_mid)
                bb_mid_slope_pct = _ratio(bb_mid, prev_bb_mid)
                trade_value_ratio20 = _volume_ratio(trade_value, trade_values)
                quality_score = _quality_score(
                    distance_pct, bb_mid_distance_pct,
                    bb_mid_slope_pct, trade_value_ratio20,
                    regime["above_count"], regime["lookback"],
                )
                passed.append({
                    "market": market,
                    "coin": market.replace("KRW-", ""),
                    "name": market_info.get("korean_name", ""),
                    "price": price,
                    "ma200": ma200,
                    "bb_mid": bb_mid,
                    "distance_pct": distance_pct,
                    "bb_mid_distance_pct": bb_mid_distance_pct,
                    "bb_mid_slope_pct": bb_mid_slope_pct,
                    "change_rate": float(latest.get("change_rate", 0) or 0) * 100,
                    "trade_value": trade_value,
                    "trade_value_ratio20": trade_value_ratio20,
                    "above_count": regime["above_count"],
                    "above_lookback": regime["lookback"],
                    "quality_score": quality_score,
                    "candle_kst": latest.get("candle_date_time_kst", ""),
                })
        except Exception as e:
            errors.append({
                "market": market,
                "name": market_info.get("korean_name", ""),
                "reason": str(e)[:120],
            })

        if idx < len(markets) - 1 and BITHUMB_MA200_REQUEST_DELAY > 0:
            time.sleep(BITHUMB_MA200_REQUEST_DELAY)

    passed.sort(
        key=lambda x: (
            x["quality_score"],
            x["trade_value"],
            -abs(x["distance_pct"]),
        ),
        reverse=True,
    )
    return {
        "asof": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "total_markets": len(markets),
        "scanned": scanned,
        "passed": passed,
        "skipped": skipped,
        "errors": errors,
    }


def _krw(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:,.6f}"


def _amount(value: float) -> str:
    value = float(value or 0)
    if value >= 1_0000_0000:
        return f"{value / 1_0000_0000:.1f}억"
    if value >= 1_0000:
        return f"{value / 1_0000:.1f}만"
    return f"{value:,.0f}"


def build_bithumb_ma200_messages(result: dict) -> list[str]:
    passed = result["passed"]
    rows_per_msg = min(max(int(BITHUMB_MA200_MAX_ROWS_PER_MSG or 20), 10), 20)
    chunks = [passed[i:i + rows_per_msg] for i in range(0, len(passed), rows_per_msg)] or [[]]
    messages = []

    for chunk_idx, chunk in enumerate(chunks):
        start_no = chunk_idx * rows_per_msg + 1
        total_pages = len(chunks)
        title_suffix = f" ({chunk_idx + 1}/{total_pages})" if total_pages > 1 else ""
        lines = [
            f"🇰🇷 <b>[시장 스크리닝] 빗썸 KRW{title_suffix}</b>",
            (
                "조건: 최신 일봉 MA200/BB중단 위, "
                f"최근 {BITHUMB_MA200_ABOVE_LOOKBACK_DAYS}개 중 "
                f"{BITHUMB_MA200_ABOVE_MIN_DAYS}개 이상 위"
            ),
            f"기준: {result['asof']}",
            f"결과: KRW마켓 {result['total_markets']}개, 계산가능 {result['scanned']}개, 통과 {len(passed)}개",
            "",
        ]

        if not chunk:
            lines.append("오늘은 조건을 동시에 만족한 빗썸 KRW 종목이 없습니다.")
        else:
            for offset, item in enumerate(chunk):
                rank = start_no + offset
                lines.extend([
                    f"{rank:02d}. <b>{item['coin']}</b> {item['name']}  등급 {_score_label(item['quality_score'])}",
                    (
                        f"현재 ₩{_krw(item['price'])}  일변동 {item['change_rate']:+.1f}%  "
                        f"거래대금 {_amount(item['trade_value'])}"
                    ),
                    (
                        f"MA200 {item['distance_pct']:+.1f}%  "
                        f"BB중단 {item['bb_mid_distance_pct']:+.1f}%  "
                        f"상방유지 {item['above_count']}/{item['above_lookback']}  "
                        f"BB기울기 {item['bb_mid_slope_pct']:+.2f}%  "
                        f"거래대금비 {item['trade_value_ratio20']:.1f}x"
                    ),
                    "",
                ])

        if result.get("skipped") or result.get("errors"):
            lines += [
                "",
                f"참고: 200일 미만 {len(result.get('skipped', []))}개, 오류 {len(result.get('errors', []))}개",
            ]
        messages.append("\n".join(lines))

    return messages


def maybe_send_bithumb_ma200_alert(send_func, *, dry_run: bool = False,
                                   force: bool = False, limit: int | None = None) -> dict | None:
    """KST 00:30/06:30 슬롯마다 빗썸 조건검색 결과를 발송한다."""
    if not BITHUMB_MA200_ALERT_ENABLED and not force:
        return None

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    state = _load_state()
    slot = _current_slot(now)
    slot_key = f"{today} {slot}" if slot else ""
    if not force:
        if not slot:
            first_slot = _alert_slots()[0]
            print(f"[빗썸 MA200] {first_slot[0]:02d}:{first_slot[1]:02d} KST 전이라 발송 대기")
            return None
        if state.get("last_ma200_sent_slot_key") == slot_key:
            print(f"[빗썸 MA200] {slot} 슬롯 이미 발송 완료 — {state.get('last_ma200_sent_at', '')}")
            return None

    try:
        result = screen_bithumb_above_ma200(limit=limit)
        messages = build_bithumb_ma200_messages(result)
        if dry_run:
            for msg in messages:
                print(msg)
                print()
        else:
            ok_all = True
            for idx, msg in enumerate(messages, start=1):
                ok = bool(send_func(msg))
                print(f"[빗썸 MA200] 텔레그램 발송 {idx}/{len(messages)}: {'OK' if ok else 'FAIL'}")
                ok_all = ok and ok_all
                time.sleep(0.4)
            if ok_all:
                state["last_ma200_sent_date"] = today
                state["last_ma200_sent_slot"] = slot or "manual"
                state["last_ma200_sent_slot_key"] = slot_key or f"{today} manual"
                state["last_ma200_sent_at"] = now.isoformat()
                state["last_ma200_count"] = len(result["passed"])
                _save_state(state)
                print(f"[빗썸 MA200] 발송 완료 — 상회 {len(result['passed'])}개")
            else:
                print("[빗썸 MA200] 일부 메시지 발송 실패 — 상태 저장하지 않음")
        return result
    except Exception as e:
        print(f"[빗썸 MA200] 스크리너 실패: {e}")
        return None


def _main() -> None:
    parser = argparse.ArgumentParser(description="Bithumb MA200 daily screener")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    maybe_send_bithumb_ma200_alert(
        print,
        dry_run=args.dry_run,
        force=True,
        limit=args.limit,
    )


if __name__ == "__main__":
    _main()
