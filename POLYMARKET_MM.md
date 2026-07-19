# Polymarket Maker Mirror V2

`polymarket_mm_bot.py`는 고래 카피 봇과 상태·주문·프로세스를 공유하지 않는 별도
마켓메이킹 실험기다. V2는 고래 파이프라인에서 `market_maker_like`로 확인된
지갑의 양방향 시장 활동을 후보 점수에 사용한다. 기본값과 현재 운용 모드는
**paper-only**다.

## 실시간 구조

- `com.polymarket.mm.discovery`: 24시간 거래량 내림차순으로 유동성 `$5,000+`
  시장을 keyset 순회한다. 거래량이 `$2,000` 아래로 내려가는 페이지까지 전부
  확인한 뒤 15초 쉬고 다시 시작한다. 현재 약 1,600개를 한 cycle에 검사한다.
- `com.polymarket.mm.stream`: 선정된 최대 5개 시장/10개 토큰의 공개 WebSocket
  L2 orderbook, price change, trade, 신규시장 이벤트를 연속 수집한다.
- `com.polymarket.mm.paper`: WebSocket snapshot을 사용해 1초마다 paper 체결·재고·
  호가를 판단한다. WebSocket 장애 때만 batch REST fallback으로 5초마다 판단한다.

전체 시장 검색이 느려도 실시간 판단이 멈추지 않도록 세 프로세스와 상태 파일을
분리했다. 기존 시장이 상위 2배 안정권 또는 관측 MM 고래 시장에 남아 있으면
선택을 유지한다. MM 고래 cohort, 거래량 cohort, 순수 경제성 점수 순으로 최대
5개 시장을 구성한다.

## 전략

- 유동성 `$5,000`, 24시간 거래량 `$2,000`, 종료 12시간~30일, 확률 10~90%를
  충족하고 양쪽 스프레드가 2틱 이상, 8센트 이하인 시장만 고른다. 시작된 스포츠
  시장과 급변 시장은 제외한다.
- `$100` 안팎의 시드에서는 최대 5개 시장에 집중한다.
- 시장마다 기본 5 pUSD를 YES/NO 5주씩으로 paper split한다.
- YES/NO 토큰 각각에 BUY와 SELL을 게시해 시장당 최대 네 방향을 호가한다.
- 공정가는 두 토큰 microprice, 보완가격 일관성, 최근 taker flow, 상대 재고를
  합성한다. 재고가 많은 결과는 bid/ask를 낮추고 독성 흐름 쪽 호가는 멈춘다.
- 두 BUY 합은 `$0.98` 이하, 두 SELL 합은 `$1.02` 이상이 되도록 완성세트
  경제성을 사전 검증한다.
- 편측 재고는 15분을 무조건 기다리지 않는다. 반대 토큰 taker BUY 후 merge와
  직접 taker SELL의 수수료 포함 회수액을 비교해 더 나은 경로를 택한다. 수익이
  잠기는 complete set은 즉시 완성한다.
- 가격 변화가 2틱 미만이면 기존 queue를 유지하고, 동일 호가의 고정 30초
  취소/재등록을 하지 않는다. 안전 갱신 상한은 기본 15분이다.
- paper 자금은 `$100`, 전체 약정 상한은 `$50`, 단일 편측 재고 상한은 `$8`이다.
- 유동성 리워드 최소 수량보다 작은 주문은 거래할 수 있지만 리워드 수익은
  계산하지 않는다.

## 체결 검증

공개 taker SELL이 우리 BUY 이하에서 발생하거나 taker BUY가 우리 SELL 이상에서
발생할 때 체결 후보로 본다. 같은 가격의 기존 bid/ask 잔량을 먼저 소진하는
양방향 price-time queue를 모의한다. 과거 거래는 `seen_trades`로 중복 체결하지
않는다.

WebSocket 이벤트는 `polymarket_mm_stream_events.jsonl`에 기록한다. 큰 book
snapshot은 상위 10호가만, 신규시장은 핵심 필드만 남긴다. 실시간 판단은 메모리의
전체 L2를 사용한다. 체결은 전부 저장하고 book은 토큰별 2초, price/top-of-book은
시장별 10초 간격으로 표본 저장한다. 파일은 200MB에서 한 번 회전되어 디스크를
무한히 사용하지 않는다.

## Telegram 알림

- BUY/SELL maker fill, split/merge, 재균형, 시장 정산은 즉시 전송한다.
- equity, BUY/SELL fill, maker/rebalance PnL, 재고, MM 고래 수, 승급 조건은
  30분마다 전송한다.
- `--report-now`로 현재 실제 상태 리포트를 즉시 보낼 수 있다.

## LIVE 차단

실주문 어댑터는 `polymarket_mm_exec.py`에 분리되어 있다. 아래 두 환경변수가 모두
참이어도 봇은 곧바로 주문하지 않는다.

```text
POLYMARKET_LIVE_TRADING_ENABLED=true
POLYMARKET_MM_LIVE_ENABLED=true
```

최소 7일, maker fill 100건, SELL fill 20건, complete-set cycle 30회,
실현손익 `$2` 이상, 최대낙폭 5% 이하, 재균형손실/메이커이익 40% 이하를 모두
충족해야 `live_ready_manual_review`가 된다. 그 뒤에도
WebSocket 사용자 채널, 실제 주문·체결 reconciliation, split/merge 트랜잭션을
검토하기 전까지 `live_execution_started=false`를 유지한다.

## 확인 명령

```bash
.venv-poly/bin/python polymarket_mm_bot.py --json
jq '{mode,equity,realized_pnl,last_scan,promotion}' polymarket_mm_state.json
launchctl print gui/$(id -u)/com.polymarket.mm.paper
launchctl print gui/$(id -u)/com.polymarket.mm.stream
launchctl print gui/$(id -u)/com.polymarket.mm.discovery
tail -f /tmp/polymarket_mm_paper.log
```
