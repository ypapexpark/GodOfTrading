# GodOfTrading Quant Research System v1

## 목표

목표는 수익을 약속하는 봇이 아니라, **비용 후 양의 기대값이 반복 검증된 전략만
작은 실자본에서 살아남게 하는 연구·승격 시스템**이다. 전략 아이디어와 라이브
권한을 분리하며, Bybit 성과를 Binance 성과로 간주하지 않는다.

## 조사한 원리와 시스템 매핑

| 근거 계열 | 핵심 교훈 | GodOfTrading 적용 |
|---|---|---|
| Time-series momentum | 여러 자산·장기 표본에서 추세 지속 근거가 있으나, 15m 크립토에 그대로 일반화할 수는 없음 | EMA 눌림목 LONG을 유일한 live champion family로 두고 거래소별 실체결로 재검증 |
| Volatility management | 변동성이 커질 때 동일 명목금액을 유지하면 위험이 폭증 | SL 거리와 equity 기반 위험 사이징, high-vol 감액 |
| Kelly growth | 확률과 payoff를 정확히 알 때 성장률 최적화가 가능하나 추정오차에 매우 취약 | full Kelly 금지. 현 버전 표본이 적으면 0.5배 probation, 손실 버전은 0.35배 |
| Execution/market impact | 신호 수익과 체결 수익은 다르며 비용·가격충격을 함께 최적화해야 함 | 계정 실측 taker fee + 왕복 슬리피지 가정, 실제 체결 레버리지·증거금 사후검증 |
| Backtest overfitting | 많은 조합 중 최고 Sharpe를 고르면 선택편향으로 성과가 부풀려짐 | 후보/백테스트는 스스로 live 승격 불가, 버전별 20건 이상 실체결과 PF·기대값 동시 요구 |
| Out-of-sample evidence | 백테스트 Sharpe만으로 OOS 성과를 예측하기 어렵고 DD·구성·헤징이 중요 | PF, 기대값, 최대DD, 원장 일치, 버전 코호트를 함께 기록 |

## 벤치마킹 방식

- AQR/학술 추세추종 사례에서 베끼는 것은 EMA 숫자가 아니라 `다시장 분산`,
  `변동성 정규화`, `긴 표본`, `위기 구간 포함 검증`이라는 연구 절차다.
- Quantopian 888개 알고리즘 연구는 좋은 백테스트가 좋은 OOS를 보장하지 않는다는
  실패 벤치마크다. 따라서 새 지표 조합의 최고 수익률을 live 근거로 쓰지 않는다.
- 거래소 운영은 Binance/Bybit 공식 API의 고유 주문 ID, reduce-only, 실제
  instrument filter, rate limit, unknown execution reconciliation을 기준으로 한다.

## 연구 → 라이브 파이프라인

1. `가설`: 경제적 이유와 실패 조건을 한 문장으로 명시한다.
2. `점시점 데이터`: 미래봉·현재 종목목록 생존편향을 차단한다.
3. `비용 포함 시뮬레이션`: 수수료, 슬리피지, funding, 최소수량을 뺀다.
4. `Walk-forward`: 학습/검증 기간을 시간순으로 이동하고 purge/embargo를 둔다.
5. `Shadow`: 실제 시각의 후보만 기록하며 주문은 내지 않는다.
6. `Probation live`: 거래소별 최소 표본 전에는 정상 위험의 0.5배 이하만 사용한다.
7. `Champion`: 청산 20건+, PF 1.15+, 순기대값 양수일 때만 유지한다.
8. `Kill`: 비용 후 기대값이 0 이하이거나 원장·포지션 검증이 실패하면 신규 진입을 중단한다.

## 현재 적용된 판정

- Bybit EMA-LONG family: 기존 실체결 코호트가 양수라 probation live 유지.
- Binance EMA-LONG family: 기존 실체결 코호트는 음수다. 사용자가 요청한 실제
  OOS 표본을 얻기 위해 v5 승인 신호만 risk×0.10 canary로 운용한다. 8건 조기평가
  또는 20건 정식평가가 실패하면 shadow로 되돌리고, 기존 포지션 SL/TP 관리는 계속한다.
- SHORT, BTC Sync, RSI2, 일반 다이버전스, 파라볼릭은 live champion이 아니다.

## 자동 감사

```bash
python3 tools/quant_research_audit.py
```

결과는 `quant_research_audit_latest.json`과 `.txt`에 남는다. 이 감사기는 설정을
자동 변경하지 않으며, 실행 엔진의 `quant_governor.py`와 같은 판정식을 사용한다.

## 1차 근거

- Moskowitz, Ooi, Pedersen, *Time Series Momentum*: https://ssrn.com/abstract=2089463
- Hurst, Ooi, Pedersen, *A Century of Evidence on Trend-Following Investing*: https://ssrn.com/abstract=2993026
- Moreira, Muir, *Volatility Managed Portfolios*: https://www.nber.org/papers/w22208
- Bailey, López de Prado, *The Deflated Sharpe Ratio*: https://ssrn.com/abstract=2460551
- Wiecki et al., *All that Glitters Is Not Gold*: https://ssrn.com/abstract=2745220
- Kelly, *A New Interpretation of Information Rate*: https://doi.org/10.1002/j.1538-7305.1956.tb03809.x
- Almgren, Chriss, *Optimal Execution of Portfolio Transactions*: https://docslib.org/doc/1384720/optimal-execution-of-portfolio-transactions
- Crypto trend following with transaction costs: https://ssrn.com/abstract=4551518
- Binance USD-M general API/unknown execution handling: https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info
- Binance USD-M new order/client ID: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#new-order
- Bybit V5 place order: https://bybit-exchange.github.io/docs/v5/order/create-order
- Bybit V5 rate limits: https://bybit-exchange.github.io/docs/v5/rate-limit
