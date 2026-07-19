# Binance Bot · Copy Trading Intelligence

`binance_copy_intel.py`는 Binance 공개 Bot Marketplace와 Futures Copy Trading
리드 포트폴리오를 분석하고, 통과한 리드의 공개 체결을 현재 시드 기준으로
섀도 카피한다. **주문 API와 LIVE 승격 경로는 없다.**

## 실행 구조

- 60초: 통과 리드 최대 3명의 최신 공개 체결 확인
- 6시간: 30D/90D 리드 성과, 포지션 이력, 현물·선물 그리드 전략 재수집
- 4시간: 시그널 텔레그램 채널에 요약 보고
- 최초 관찰: 이미 열린 리드 포지션은 따라가지 않고 체결 ID만 baseline 처리
- 이후 체결: 리드 계좌 대비 체결비율을 현재 Binance USD-M equity에 맞춰 가상 체결
- 공개 체결의 원시 시각과 감지 시각 차이를 기록해 외부 카피의 실제 지연을 검증

## 리드 승격 조건

- 운용 90일 이상
- 30D/90D ROI와 PNL 양수, 카피어 PNL 양수
- MDD 20% 이하, Sharpe 1 이상
- 최근 30일 수익일 비율 55% 이상
- 청산 포지션 60건 이상, PF 1.15 이상
- 최대 수익 1건 제거 후에도 누적손익 양수
- 고승률·대형손실 또는 손실 후 사이즈 증가 등 마틴게일 징후 없음

조건을 통과해도 `shadow`일 뿐 자동 실거래로 전환하지 않는다.

## 현재 시드와 체결비용

`.env`에 이미 있는 Binance API 키로 USD-M equity를 **읽기 전용** 조회한다.
조회 실패 시 마지막 정상 시드를 유지한다. 리드별 20%, 전체 60%, 가상 레버리지
최대 5배가 기본값이며 Binance 최소 주문금액, taker 0.05%, 편도 슬리피지 0.03%를
반영한다.

선택 설정값:

```dotenv
# 0이면 실제 Binance USD-M equity 읽기 전용 사용
BINANCE_COPY_SHADOW_SEED_USDT=0
BINANCE_COPY_PER_LEADER_PCT=0.20
BINANCE_COPY_TOTAL_CAP_PCT=0.60
BINANCE_COPY_SHADOW_LEVERAGE_CAP=5
BINANCE_COPY_MIN_NOTIONAL_USDT=5
BINANCE_COPY_DISCOVERY_SECONDS=21600
BINANCE_COPY_REPORT_SECONDS=14400
BINANCE_COPY_TRACKED_LEADERS=3
BINANCE_GRID_PER_BOT_PCT=0.25
BINANCE_GRID_MIN_24H_QUOTE_USD=10000000
```

## 수동 점검

```bash
python3 binance_copy_intel.py --discover --report-now --json
python3 binance_copy_intel.py --no-telegram --json
tail -n 100 /tmp/godoftrading_binance_copy_intel.log
tail -n 100 /tmp/godoftrading_binance_copy_intel_err.log
```

런타임 파일:

- `binance_copy_intel_state.json`: 시드, 후보, 섀도 포지션
- `binance_copy_intel_snapshots.jsonl`: 생존자 편향 방지를 위한 고정 코호트
- `binance_copy_intel_journal.jsonl`: baseline, 가상 진입·청산, 스킵 사유

공개 `bapi`는 Binance 웹 화면용 비문서화 경로다. 스키마가 바뀌면 이전 정상
스냅샷을 보존하고 실거래에는 아무 영향도 주지 않는다.
