"""KOSPI/KOSDAQ daily MA200 screeners.

이 모듈은 갓오브트레이딩의 "하루 한 번 종목 선별" 역할만 담당한다.
실시간 매매 엔진과 분리해서 두면, 국내주식 데이터 소스를 나중에
한국투자증권 KIS Open API로 바꿔도 메인 매매 로직을 건드릴 필요가 적다.
"""
from __future__ import annotations

import argparse
import json
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import (
    KIS_API_EARLY_ERROR_RATE,
    KIS_API_EARLY_FAILURE_THRESHOLD,
    KIS_API_EARLY_MIN_ERRORS,
    KIS_API_EARLY_MIN_SCAN_RATIO,
    KRX_MA200_ALERT_ENABLED,
    KRX_MA200_ALERT_HOUR,
    KRX_MA200_ALERT_TIMES,
    KRX_MA200_ABOVE_LOOKBACK_DAYS,
    KRX_MA200_ABOVE_MIN_DAYS,
    KRX_MA200_CANDLE_COUNT,
    KRX_MA200_LOOKBACK_DAYS,
    KRX_MA200_MAX_RESULTS,
    KRX_MA200_MAX_ROWS_PER_MSG,
    KRX_MA200_REQUEST_DELAY,
)
from project_reminders import maybe_send_kis_api_early_warning

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

KST = timezone(timedelta(hours=9))
STATE_FILE = Path(__file__).parent / "krx_screener_state.json"
RUNNING_STALE_SECONDS = 60 * 60 * 2


def _should_watch_data_health(force: bool, dry_run: bool, limit: int | None) -> bool:
    """실제 자동 전체 스캔에서만 데이터 품질 알림을 평가한다."""
    return (not force) and (not dry_run) and limit is None


def _load_state() -> dict[str, Any]:
    """스크리너 발송/실행 상태를 읽는다.

    상태 파일이 깨져도 봇 전체가 죽지 않게 빈 dict로 복구한다. 하루 한 번
    알림은 편의 기능이므로, 상태 파일 문제 때문에 자동매매까지 멈추면 안 된다.
    """
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _mark_running(state: dict[str, Any], now: datetime) -> bool:
    """중복 전체 스캔을 막기 위한 간단한 실행 락.

    LaunchAgent가 5분 주기로 봇을 깨우기 때문에, 국내주식 전체 조회가 길어질 때
    새 프로세스가 같은 작업을 다시 시작하지 않게 막는다. 이전 실행이 2시간 넘게
    끝나지 않았다면 비정상 종료로 보고 락을 회수한다.
    """
    running_since = state.get("krx_ma200_running_since")
    if running_since:
        try:
            started = datetime.fromisoformat(running_since)
            elapsed = (now - started).total_seconds()
            if elapsed < RUNNING_STALE_SECONDS:
                print(f"[국내주식 MA200] 이미 실행 중 — {running_since}")
                return False
        except Exception:
            pass

    state["krx_ma200_running_since"] = now.isoformat()
    _save_state(state)
    return True


def _clear_running(state: dict[str, Any]) -> None:
    state.pop("krx_ma200_running_since", None)
    _save_state(state)


def _fdr():
    """FinanceDataReader를 지연 import한다.

    텔레그램/바이빗 자동매매만 돌릴 때 국내주식 패키지 문제로 전체 봇이
    시작 실패하지 않도록 import 시점을 스크리너 실행 직전으로 늦춘다.
    """
    try:
        import FinanceDataReader as fdr
    except ImportError as exc:
        raise RuntimeError(
            "FinanceDataReader가 설치되어 있지 않습니다. "
            "python3 -m pip install -r requirements.txt 실행이 필요합니다."
        ) from exc
    return fdr


def fetch_krx_listing() -> list[dict[str, Any]]:
    """KRX 상장 종목 목록 중 KOSPI/KOSDAQ 보통 종목 후보를 가져온다."""
    fdr = _fdr()
    listing = fdr.StockListing("KRX")
    rows: list[dict[str, Any]] = []

    for _, row in listing.iterrows():
        code = str(row.get("Code", "")).zfill(6)
        market = str(row.get("Market", "")).upper()
        name = str(row.get("Name", "")).strip()
        if market not in {"KOSPI", "KOSDAQ"}:
            continue
        if len(code) != 6 or not code.isdigit() or not name:
            continue
        rows.append({
            "code": code,
            "name": name,
            "market": market,
            "marcap": _safe_float(row.get("Marcap", 0)),
            "amount": _safe_float(row.get("Amount", 0)),
            "listing_close": _safe_float(row.get("Close", 0)),
        })

    rows.sort(key=lambda x: (x["market"], x["code"]))
    return rows


def fetch_stock_daily(code: str, start_date: str, end_date: str):
    """단일 종목의 일봉을 가져온다.

    FinanceDataReader는 종목별 조회 API를 사용하므로 전체 스캔은 가볍지 않다.
    다만 하루 1회 종목 선별 MVP에는 충분하고, 향후 KIS API로 대체할 수 있게
    함수 경계를 분리해 둔다.
    """
    fdr = _fdr()
    df = fdr.DataReader(code, start_date, end_date)
    if df is None or df.empty:
        return df
    return df.sort_index()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _price(value: float) -> str:
    return f"{float(value):,.0f}"


def _amount(value: float) -> str:
    value = float(value or 0)
    if value >= 1_0000_0000_0000:
        return f"{value / 1_0000_0000_0000:.1f}조"
    if value >= 1_0000_0000:
        return f"{value / 1_0000_0000:.1f}억"
    if value >= 1_0000:
        return f"{value / 1_0000:.1f}만"
    return f"{value:,.0f}"


def _sma_latest(values, period: int) -> float:
    if len(values) < period:
        return 0.0
    return float(values.tail(period).mean())


def _ratio(value: float, base: float) -> float:
    return (value / base - 1) * 100 if base > 0 else 0.0


def _volume_ratio(latest_volume: float, volumes, period: int = 20) -> float:
    if len(volumes) < period + 1:
        return 0.0
    past = volumes.iloc[-period - 1:-1].astype(float)
    avg = float(past.mean())
    return latest_volume / avg if avg > 0 else 0.0


def _above_regime_latest(close, ma_period: int, bb_period: int,
                         lookback: int, min_days: int) -> dict[str, Any]:
    """최근 N개 일봉이 MA200/BB중단 위에서 유지되는지 평가한다.

    KRX 일봉은 오래된 순서로 정렬되어 있다. 충분한 과거 데이터가 있으면
    각 일자별 MA200/BB중단을 다시 계산하고, 부족하면 현재 기준선을 기준으로
    최근 캔들 유지 여부를 평가한다.
    """
    lookback = max(int(lookback or 1), 1)
    min_days = min(max(int(min_days or lookback), 1), lookback)
    if len(close) < ma_period or len(close) < bb_period:
        return {"ok": False, "above_count": 0, "lookback": lookback, "latest_above": False}

    enough_rolling_history = len(close) >= ma_period + lookback - 1
    anchor_ma = _sma_latest(close, ma_period)
    anchor_bb = _sma_latest(close, bb_period)
    above_count = 0
    latest_above = False

    for offset in range(min(lookback, len(close))):
        end = len(close) - offset
        historical = close.iloc[:end]
        price = float(historical.iloc[-1])
        if enough_rolling_history:
            ma = _sma_latest(historical, ma_period)
            bb_mid = _sma_latest(historical, bb_period)
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
                   bb_mid_slope_pct: float, volume_ratio20: float,
                   trade_value: float, above_count: int,
                   above_lookback: int) -> int:
    """조건 통과 종목의 우선순위를 보기 위한 보조 점수."""
    score = 2  # MA200 상회 + BB 중단 상회는 통과 조건 자체로 이미 충족
    if 0 <= distance_pct <= 35:
        score += 1
    if bb_mid_slope_pct > 0:
        score += 1
    if volume_ratio20 >= 1.0 or trade_value >= 50_0000_0000:
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
    for raw in KRX_MA200_ALERT_TIMES:
        try:
            hour, minute = str(raw).split(":", 1)
            slots.append((int(hour), int(minute)))
        except Exception:
            continue
    if not slots:
        slots.append((int(KRX_MA200_ALERT_HOUR), 0))
    return sorted(set(slots))


def _current_slot(now: datetime) -> str | None:
    current: str | None = None
    for hour, minute in _alert_slots():
        if (now.hour, now.minute) >= (hour, minute):
            current = f"{hour:02d}:{minute:02d}"
    return current


def _krx_health_warning(result: dict[str, Any]) -> tuple[str, str] | None:
    """국내주식 무료 데이터 소스의 품질 저하 여부를 판단한다.

    스킵은 신규상장/거래정지 등 정상적인 사유도 섞일 수 있어 단독으로는
    너무 민감하다. 그래서 명확한 오류율과 MA200 계산 가능 비율을 함께 본다.
    """
    total = int(result.get("total_stocks", 0) or 0)
    scanned = int(result.get("scanned", 0) or 0)
    errors = len(result.get("errors", []) or [])
    if total <= 0:
        return (
            "국내주식 종목 목록 조회 실패",
            "KOSPI/KOSDAQ 종목 수가 0개로 수집됐습니다.",
        )

    error_rate = errors / total
    scan_ratio = scanned / total
    details = (
        f"전체 {total}개, MA200 계산 가능 {scanned}개({scan_ratio*100:.1f}%), "
        f"오류 {errors}개({error_rate*100:.1f}%)"
    )

    if errors >= KIS_API_EARLY_MIN_ERRORS and error_rate >= KIS_API_EARLY_ERROR_RATE:
        return ("무료 데이터 오류율 과다", details)
    if scan_ratio < KIS_API_EARLY_MIN_SCAN_RATIO:
        return ("MA200 계산 가능 종목 비율 저하", details)
    return None


def _record_success_health(state: dict[str, Any], result: dict[str, Any]) -> None:
    """정상 스캔 결과와 데이터 품질 요약을 상태 파일에 남긴다."""
    total = int(result.get("total_stocks", 0) or 0)
    scanned = int(result.get("scanned", 0) or 0)
    errors = len(result.get("errors", []) or [])
    state["krx_ma200_failure_count"] = 0
    state["last_krx_ma200_health"] = {
        "checked_at": datetime.now(KST).isoformat(),
        "total_stocks": total,
        "scanned": scanned,
        "errors": errors,
        "scan_ratio": round(scanned / total, 4) if total else 0,
        "error_rate": round(errors / total, 4) if total else 0,
    }
    _save_state(state)


def _record_failure_health(state: dict[str, Any], exc: Exception) -> tuple[int, str]:
    """전체 스캔 실패를 누적 기록하고 현재 연속 실패 횟수를 반환한다."""
    count = int(state.get("krx_ma200_failure_count", 0) or 0) + 1
    reason = str(exc)[:180]
    state["krx_ma200_failure_count"] = count
    state["last_krx_ma200_failure_at"] = datetime.now(KST).isoformat()
    state["last_krx_ma200_failure_reason"] = reason
    _save_state(state)
    return count, reason


def screen_krx_above_ma200(limit: int | None = None) -> dict[str, Any]:
    """KOSPI/KOSDAQ에서 최신 종가가 MA200과 BB 중단선 위인 종목을 선별한다."""
    end = datetime.now(KST).date()
    start = end - timedelta(days=KRX_MA200_LOOKBACK_DAYS)
    start_text = start.strftime("%Y-%m-%d")
    end_text = end.strftime("%Y-%m-%d")

    listing = fetch_krx_listing()
    if limit:
        listing = listing[:limit]

    passed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    scanned = 0

    for idx, item in enumerate(listing):
        code = item["code"]
        name = item["name"]
        try:
            df = fetch_stock_daily(code, start_text, end_text)
            if df is None or len(df) < KRX_MA200_CANDLE_COUNT:
                skipped.append({
                    "code": code,
                    "name": name,
                    "reason": f"일봉 {0 if df is None else len(df)}개",
                })
                continue
            if "Close" not in df.columns:
                skipped.append({"code": code, "name": name, "reason": "Close 컬럼 없음"})
                continue

            close = df["Close"].dropna().astype(float)
            if len(close) < KRX_MA200_CANDLE_COUNT:
                skipped.append({"code": code, "name": name, "reason": f"종가 {len(close)}개"})
                continue

            latest_close = float(close.iloc[-1])
            ma200 = _sma_latest(close, KRX_MA200_CANDLE_COUNT)
            bb_mid = _sma_latest(close, 20)
            prev_bb_mid = float(close.iloc[-21:-1].mean()) if len(close) >= 21 else 0.0
            if latest_close <= 0 or ma200 <= 0 or bb_mid <= 0:
                skipped.append({"code": code, "name": name, "reason": "가격/MA200/BB중단 0"})
                continue

            scanned += 1
            regime = _above_regime_latest(
                close,
                KRX_MA200_CANDLE_COUNT,
                20,
                KRX_MA200_ABOVE_LOOKBACK_DAYS,
                KRX_MA200_ABOVE_MIN_DAYS,
            )
            if regime["ok"]:
                change = _safe_float(df["Change"].iloc[-1] * 100 if "Change" in df.columns else 0)
                volume = _safe_float(df["Volume"].iloc[-1] if "Volume" in df.columns else 0)
                volume_ratio20 = _volume_ratio(
                    volume,
                    df["Volume"].dropna().astype(float) if "Volume" in df.columns else close * 0,
                )
                trade_value = item["amount"] or latest_close * volume
                distance_pct = (latest_close / ma200 - 1) * 100
                bb_mid_distance_pct = _ratio(latest_close, bb_mid)
                bb_mid_slope_pct = _ratio(bb_mid, prev_bb_mid)
                quality_score = _quality_score(
                    distance_pct, bb_mid_distance_pct,
                    bb_mid_slope_pct, volume_ratio20, trade_value,
                    regime["above_count"], regime["lookback"],
                )
                passed.append({
                    "code": code,
                    "name": name,
                    "market": item["market"],
                    "price": latest_close,
                    "ma200": ma200,
                    "bb_mid": bb_mid,
                    "distance_pct": distance_pct,
                    "bb_mid_distance_pct": bb_mid_distance_pct,
                    "bb_mid_slope_pct": bb_mid_slope_pct,
                    "change_pct": change,
                    "volume": volume,
                    "volume_ratio20": volume_ratio20,
                    "above_count": regime["above_count"],
                    "above_lookback": regime["lookback"],
                    "marcap": item["marcap"],
                    "amount": trade_value,
                    "quality_score": quality_score,
                    "date": close.index[-1].strftime("%Y-%m-%d"),
                })
        except Exception as exc:
            errors.append({"code": code, "name": name, "reason": str(exc)[:140]})

        if idx < len(listing) - 1 and KRX_MA200_REQUEST_DELAY > 0:
            time.sleep(KRX_MA200_REQUEST_DELAY)

    # 거리율이 너무 큰 종목만 위에 몰리면 거래대금이 작은 종목이 과대표시될 수 있어
    # 거래대금을 2차 기준으로 둔다. 실제 매매 후보로 볼 때 유동성이 중요하기 때문이다.
    passed.sort(
        key=lambda x: (
            x["quality_score"],
            x["amount"],
            -abs(x["distance_pct"]),
        ),
        reverse=True,
    )
    return {
        "asof": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "start": start_text,
        "end": end_text,
        "total_stocks": len(listing),
        "scanned": scanned,
        "passed": passed,
        "skipped": skipped,
        "errors": errors,
    }


def build_krx_ma200_messages(result: dict[str, Any]) -> list[str]:
    """Telegram 제한 길이를 고려해 MA200 상회 종목 결과를 여러 메시지로 나눈다."""
    passed = result["passed"]
    max_results = max(int(KRX_MA200_MAX_RESULTS or 180), 20)
    rows_per_msg = min(max(int(KRX_MA200_MAX_ROWS_PER_MSG or 18), 10), 18)
    visible = passed[:max_results]
    chunks = [visible[i:i + rows_per_msg] for i in range(0, len(visible), rows_per_msg)] or [[]]
    messages: list[str] = []

    for chunk_idx, chunk in enumerate(chunks):
        start_no = chunk_idx * rows_per_msg + 1
        total_pages = len(chunks)
        title_suffix = f" ({chunk_idx + 1}/{total_pages})" if total_pages > 1 else ""
        lines = [
            f"🇰🇷 <b>[시장 스크리닝] KOSPI/KOSDAQ{title_suffix}</b>",
            (
                "조건: 최신 일봉 MA200/BB중단 위, "
                f"최근 {KRX_MA200_ABOVE_LOOKBACK_DAYS}개 중 "
                f"{KRX_MA200_ABOVE_MIN_DAYS}개 이상 위"
            ),
            f"기준: {result['asof']}",
            f"결과: 전체 {result['total_stocks']}개, 계산가능 {result['scanned']}개, 통과 {len(passed)}개",
        ]
        if len(passed) > len(visible):
            lines.append(f"표시: 상위 {len(visible)}개 (등급/거래대금 우선)")
        lines.append("")

        if not chunk:
            lines.append("오늘은 조건을 동시에 만족한 KOSPI/KOSDAQ 종목이 없습니다.")
        else:
            for offset, item in enumerate(chunk):
                rank = start_no + offset
                lines.extend([
                    f"{rank:02d}. <b>{item['code']} {item['name']}</b> {item['market']}  등급 {_score_label(item['quality_score'])}",
                    (
                        f"현재 ₩{_price(item['price'])}  일변동 {item['change_pct']:+.1f}%  "
                        f"거래대금 {_amount(item['amount'])}"
                    ),
                    (
                        f"MA200 {item['distance_pct']:+.1f}%  "
                        f"BB중단 {item['bb_mid_distance_pct']:+.1f}%  "
                        f"상방유지 {item['above_count']}/{item['above_lookback']}  "
                        f"BB기울기 {item['bb_mid_slope_pct']:+.2f}%  "
                        f"거래량 {item['volume_ratio20']:.1f}x"
                    ),
                    "",
                ])

        if result.get("skipped") or result.get("errors"):
            lines += [
                "",
                f"참고: 200일 미만/부족 {len(result.get('skipped', []))}개, 오류 {len(result.get('errors', []))}개",
            ]
        messages.append("\n".join(lines))

    return messages


def maybe_send_krx_ma200_alert(send_func, *, dry_run: bool = False,
                               force: bool = False,
                               limit: int | None = None) -> dict[str, Any] | None:
    """KST 00:30/06:30 슬롯마다 국내주식 조건검색 결과를 발송한다."""
    if not KRX_MA200_ALERT_ENABLED and not force:
        return None

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    state = _load_state()
    watch_health = _should_watch_data_health(force, dry_run, limit)
    slot = _current_slot(now)
    slot_key = f"{today} {slot}" if slot else ""
    if not force:
        if not slot:
            first_slot = _alert_slots()[0]
            print(f"[국내주식 MA200] {first_slot[0]:02d}:{first_slot[1]:02d} KST 전이라 발송 대기")
            return None
        if state.get("last_krx_ma200_sent_slot_key") == slot_key:
            print(f"[국내주식 MA200] {slot} 슬롯 이미 발송 완료 — {state.get('last_krx_ma200_sent_at', '')}")
            return None
        if not _mark_running(state, now):
            return None
    else:
        state.pop("krx_ma200_running_since", None)

    try:
        result = screen_krx_above_ma200(limit=limit)
        if watch_health:
            _record_success_health(state, result)
            health_warning = _krx_health_warning(result)
            if health_warning:
                reason, details = health_warning
                maybe_send_kis_api_early_warning(
                    send_func,
                    reason=reason,
                    details=details,
                )
        messages = build_krx_ma200_messages(result)
        if dry_run:
            for msg in messages:
                print(msg)
                print()
        else:
            ok_all = True
            for idx, msg in enumerate(messages, start=1):
                ok = bool(send_func(msg))
                print(f"[국내주식 MA200] 텔레그램 발송 {idx}/{len(messages)}: {'OK' if ok else 'FAIL'}")
                ok_all = ok and ok_all
                time.sleep(0.4)
            if ok_all:
                state["last_krx_ma200_sent_date"] = today
                state["last_krx_ma200_sent_slot"] = slot or "manual"
                state["last_krx_ma200_sent_slot_key"] = slot_key or f"{today} manual"
                state["last_krx_ma200_sent_at"] = now.isoformat()
                state["last_krx_ma200_count"] = len(result["passed"])
                _save_state(state)
                print(f"[국내주식 MA200] 발송 완료 — 상회 {len(result['passed'])}개")
            else:
                print("[국내주식 MA200] 일부 메시지 발송 실패 — 상태 저장하지 않음")
        return result
    except Exception as exc:
        print(f"[국내주식 MA200] 스크리너 실패: {exc}")
        if watch_health:
            failure_count, failure_reason = _record_failure_health(state, exc)
            if failure_count >= KIS_API_EARLY_FAILURE_THRESHOLD:
                maybe_send_kis_api_early_warning(
                    send_func,
                    reason=f"국내주식 무료 데이터 전체 스캔 {failure_count}회 연속 실패",
                    details=f"최근 오류: {failure_reason}",
                )
        return None
    finally:
        if not force:
            latest_state = _load_state()
            _clear_running(latest_state)
        elif state.get("krx_ma200_running_since"):
            _clear_running(state)


def _main() -> None:
    parser = argparse.ArgumentParser(description="KOSPI/KOSDAQ MA200 daily screener")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    maybe_send_krx_ma200_alert(
        print,
        dry_run=args.dry_run,
        force=True,
        limit=args.limit,
    )


if __name__ == "__main__":
    _main()
