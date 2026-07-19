# Polymarket copyable-wallet pipeline

목표는 수익 상위 지갑을 그대로 복제하는 것이 아니라, **우리의 폴링 지연과 taker
체결비용을 적용한 뒤에도 재현되는 지갑만** live 후보로 쓰는 것이다. 수익을
보장하는 시스템은 아니며, 전진검증 실패 시 자동으로 제외하는 연구·실행 체계다.

## 단계

1. `discovery`: 공개 leaderboard와 최신 global trades에서 후보를 수집한다.
2. `screened/rejected`: 최근 거래를 재구성해 양방향 재고, maker rebate, 활동성,
   방향성 시장 수를 검사한다.
3. `paper`: 3% 추격 슬리피지와 2% 실행비용을 반영한 과거 재생을 통과한 지갑만
   실제 CLOB 호가 전진검증에 등록한다.
4. `live_canary`: 비용후 백테스트 정산 50건 이상, ROI 20% 이상, bootstrap 5% 하단
   10% 이상, 최대 낙폭 5단위 이하, 최대 단일수익 기여 25% 이하인 순수 taker형만
   정상 티켓의 50%로 먼저 실매매한다. 전진 8건 후 ROI -5% 이하이면 조기 중단한다.
5. `live_approved`: 실제호가 paper 정산 30건 이상, ROI 5% 이상, bootstrap 5% 하단
   0 초과, 최대 낙폭과 단일 대박 집중 기준을 모두 통과해야 한다.
6. `suspended`: 승인 뒤 전진 ROI -3% 이하, bootstrap 하단 -5% 미만, 또는 과도한
   낙폭이 확인되면 live 목록에서 자동 제거한다.

과거 검증만으로 정상위험 `live_approved`가 되지는 않는다. watchlist의
`live_approved` 배열에는 정상승인과 `live_canary`가 함께 들어가며, 각 행의
`live_risk_mult`로 실제 현금위험을 구분한다. `paper` 목록은 live 봇이 읽지 않는다.
기존 live 설정의 지갑도 같은 검증을 받으며, 승인되지 않은 모든 고정 지갑은
`blocked_live`에 들어가 신규 주문만 중단한다. 이미 열린 포지션은 기존 청산·정산
정책을 유지한다.

## 자동 실행

- `com.polymarket.wallet.discovery`: 1시간마다 최대 30개 후보 수집·재검증·승급·강등
- `com.polymarket.wallet.radar`: 15초마다 paper 후보의 신규 체결을 병렬 조회하고
  현재 ask VWAP으로 모의 진입, 현재 bid VWAP 또는 시장 결과로 모의 청산
- `com.polymarket.whale.live`: 기존 15초 live 루프에서 승인된 동적 지갑을 반영

수동 실행:

```bash
.venv-poly/bin/python polymarket_wallet_pipeline.py --max-profiles 30 --json
.venv-poly/bin/python polymarket_wallet_pipeline.py --radar --json
```

운영 상태는 다음 로컬 런타임 파일에 기록되며 Git에는 포함하지 않는다.

- `polymarket_wallet_pipeline_state.json`
- `polymarket_wallet_watchlist.json`
- `polymarket_wallet_radar_state.json`
- `polymarket_wallet_radar_journal.jsonl`
