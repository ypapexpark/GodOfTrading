# Hyperliquid whale copy review — 2026-07-19

## 결론

리더보드 고수익 지갑의 **개별 fill**은 독립적인 방향성 베팅이 아니다. 대형 계정은
여러 종목·DEX에 걸친 포트폴리오 헤지, 시장조성, 분할진입을 함께 사용한다. 기존 봇은
그중 한 fill만 고정 $25 taker 포지션으로 떼어 복사했기 때문에 고래의 원래 기대값을
재현하지 못했다.

## v1 forward 결과

- 정산 63건: 21W / 33L / 9BE
- PF 0.697, 누적 -$7.4656, 건당 -$0.1185, 최대 DD $12.0924
- LONG 28건 -$12.2097 / SHORT 35건 +$4.7441
- HIP-3 49건 -$9.0062 / main perp 14건 +$1.5406
- 3–10k source fill 27건 -$7.3359
- 10–50k 25건 -$2.0589
- 50k 이상 11건 +$1.9292 (소표본이므로 확정 우위로 간주하지 않음)
- 신호 지연 60초 이하 14건 +$2.0885, 그 외 49건 -$9.5541
- whale-flat 49건 -$0.3277, 임의 48h 청산 14건 -$7.1379

## 구조적 원인

1. 리더보드는 절대 PnL이 큰 계정을 찾았을 뿐, follower가 복사 가능한 단일 전략을
   찾은 것이 아니다.
2. maker fill(`crossed=false`)까지 follower가 taker로 추종했다. 고래는 스프레드·낮은
   수수료·포트폴리오 헤지로 수익을 내지만 follower는 불리한 진입비용만 부담한다.
3. 개별 fill 이후 고래가 이미 줄이거나 반대로 돌았는지 확인하지 않았다.
4. 180초 스캔의 실제 중앙 지연이 약 111초였다.
5. 고래가 유지 중인 포지션도 48시간에 follower만 임의 청산했다.
6. HIP-3 `allMids` 응답 키에 dex 접두사를 복원하지 않아 일부 초기 거래가 소스 fill
   가격에 고정되는 문제가 있었다.
7. 진입 슬리피지만 반영하고 exit 슬리피지와 양방향 taker fee를 빼지 않아 v1 기록은
   실제보다 낙관적이다.

## v2 정책

- 정책 ID: `2026-07-19-hl-position-delta-taker-v2`
- `userFillsByTime + aggregateByTime`로 커서 이후 체결만 증분 수집
- 30초 스캔, 최대 신호 지연 75초
- 명시적 `Open Long/Short`이며 `crossed=true`인 taker fill만 사용
- 같은 폴링 구간의 지갑·종목·방향 fill을 VWAP/노셔널로 합산
- taker open 합계 50k 이상
- clearinghouse에서 같은 방향 순포지션 증가 50k 이상 재확인
- 현재 포지션 50k 이상 및 해당 dex account value의 0.5% 이상
- HIP-3 mid 키를 `dex:coin`으로 정규화
- 고래 flat/반전 시 종료, 168h는 비정상 장기보유의 emergency cap으로만 사용
- 진입·청산 15bp 슬리피지와 각 방향 5bp taker fee를 반영
- v1/v2 저널과 bankroll/Telegram 통계를 분리
- PAPER 전용. v2 forward 표본 없이 LIVE 전환하지 않음

50k/75초 기준은 v1 반사실과 실행 가능성에 근거한 초기값이다. 소표본 사후 최적화의
위험이 있으므로 수익을 전제하지 않고 v2 forward 결과로만 유지·완화·폐기를 판단한다.

## 공식 API 근거

- Info endpoint: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Rate limits: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- Fees: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
