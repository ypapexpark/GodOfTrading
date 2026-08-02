# GodOfTrading Trading Notes

## 2026-08-03 S1 마이너스 구조 보정 (exit asymmetry)

**원장 답:** S1 n=18 WR 33% (필요 ~44%), 승은 본전/부분TP로 잘리고 패는 풀 SL.
**조치 (ENGINE `2026-08-03-s1v3-exit-fix` — canary 리셋):**
1. S1 **TP1 전 PRE_TP BE 금지** (`S1_DISABLE_PRE_TP_BE`)
2. 주봉+일봉 **더블 SHORT이면 롱 S1 차단**
3. TP1 이득 < 왕복수수료×2 진입 금지
4. 리포트: `python3 tools/daily_realized_r_report.py --days 3`

자동화 수익은 “불가능”이 아니라 **EV>0 버킷만 키우는 느린 게임**. EMA 롱 +EV / S1은 실험.

## 2026-08-02 목표 스타일: Cupsey형 복리 단타 (매핑)

**레퍼런스:** [Cupsey / Solana grinder](https://x.com/0xeduu/status/2083537722187166044)
- 시작 ~$2k → 순익 수천만$, 262k+ trades, WR ~68%, 95% 포지션 **1분 이내** 청산
- 초소액 반복 (≈3 SOL), 신규 런치 스캔, pump.fun snipes

**우리 시스템에 그대로 복제할 수 없는 이유:**
- Cupsey 엣지는 **Solana 밈 런치 초단위 스나이프** (정보·지연·유동성 구조).
- GodOfTrading Bybit 코어는 **15m/5m 완료봉 선물** + 비용후 R:R. 초단위 온체인 스나이프와 시장 구조가 다름.
- 초단위 스나이프를 Bybit 15m 게이트에 억지로 넣으면 노이즈·수수료로 EV가 음수가 된다 (S1 v1/v2 canary가 증명).

**대신 채택하는 동일 원칙 (이미 코드에 반영/유지):**
1. **소액 고정 위험% 복리** — canary 0.25% → probation → champion, `COMPOUND_ENGINE` equity% 스케일
2. **고빈도 품질 필터** — 많이 치는 게 아니라 **많이 스캔하고 적게 체결** (Cupsey도 하루 수백 스캔)
3. **빠른 확정 + 러너** — S1 40/60 @ 1.35R/2.40R, EMA FAST_TP 복리형, PRE_TP 0.85R 보호
4. **당일 반복 금지** — 동일 심볼 과매매 차단 (grinder의 “한 방에 몰빵” 반대)
5. **−EV 버킷 제거** — SHORT/비-EMA 실주문 차단, +EV EMA 롱만 사이즈 유지

**다음 단계 (별도 제품, 이 PR 범위 밖):**
- 진짜 초단위 밈 스나이프는 pump.fun/DEX 전용 러너 + 별도 시드·리스크 한도가 필요.
- Bybit에서는 S1 v3 OOS 12건 + EMA 15m 챔피언 유지가 복리 경로.

## 2026-08-02 Bybit 무체결 복구 + S1 v3 / EMA soft

**증상:** `main.py --auto-trade` 정상 가동, 포지션 0, 7/31 이후 체결 0.
**원인 (프로세스가 죽은 게 아님):**
1. S1 v2 canary 8건 PF 0.84 E −$0.002 → 승격게이트 shadow (신규 0).
2. EMA 롱 퀀트게이트는 allow(probation×0.5)인데 5m VWAP **hard-block** + 역추세 **6/6** 요구로 실주문 굶주림.
3. 전기간 손실의 주범은 여전히 SHORT/BTC Sync 등 (−$25 숏). 유일 +EV는 EMA 롱 15m.

**전기간 로컬 원장 (Bybit, n=142):** WR 50% · PF 0.59 · pnl −$31 · equity 76.55→59.71 (−22%).
- EMA 롱 화이트리스트 15m: n=22 WR~64–78% pnl **+$8.84**
- S1 전체: n=16 WR 37.5% pnl −$0.24 (v2 8건 ≈본전 미소)

**조치:**
- S1 `ENGINE_VERSION=2026-08-02-s1v3-liquid-rr` 리셋 (과거 버전 성과 미차용).
- 주식 무기한 denylist(TSLA 등) · 당일 심볼 1회 · stop% 2.5→3.2 · canary 평가 12건.
- EMA 5m 보조: 강한역방향/과열 hard 유지, EMA OK+경미 VWAP만 risk×0.70 soft.
- 화이트리스트 EMA LONG 15m 역추세 confirmed 6→5 (vol≥1.25, 실측 conf=5 +EV).

## 2026-07-30 복리 엔진 v1 (수익 직결)

**목표:** 승률 집착이 아니라 EV>0 + equity% 복리.
**원장 근거:** avg win R 0.54 / avg loss R -1.06 → EV -0.26R. 이 상태로는 복리 불가.
**청산:** PRE_TP 0.85R 보호, POST_TP1 +0.50R 잠금, 트레일 1.55ATR, 복리TP 40/60 러너.
**사이징:** COMPOUND 스케일(DD 감액·신고가 소폭 가속) + 코어 리스크 0.90%(캡 1.20%).
**S1:** TP 1.35R/2.40R × 40/60, 비용후 손익분기 강화.

## 2026-07-30 실체결 학습 재활성 (승률 함정 수정)

**오해 정리:** 후보 로그 ≈7.8만 건 ≠ 체결. Bybit 실제 청산 ≈131건(WR≈50%).
**진짜 문제:** 승률보다 **평균손실($1.17) ≫ 평균이익($0.69), PF 0.59**.
**학습 상태 (수정 전):**
- `analyze_and_adjust` 소프트 적응 ON (쿨다운/confirmed) — 하지만 "손익학습 반영" 문구와 불일치
- `REALIZED_TRADE_LEARNING_ENABLED=False` → 실체결 차단/부스트 **꺼짐**
- 후보 품질 학습은 의도적으로 라이브 사이징에 미연결 (오염 방지)

**조치:** REALIZED ON + PF/기대값 차단(고승률·저R:R 포함) + 전략×TF 코호트 + 현실적 표본 하한.

## 2026-07-29 Bybit 재개 — S1 v2 + EMA-LONG 레거시 병행

**중지 원인 (프로세스는 살아 있었음):**
- `main.py --auto-trade` LaunchAgent 정상 가동 중.
- S1 v1 canary 8건 후 승격게이트 조기중단: PF 0.64, E −$0.027 → 신규 진입 전부 차단.
- `LEGACY_AUTO_TRADE_ENABLED=False` 로 검증된 EMA 롱 코어까지 같이 꺼져 있었음.

**수익 부진 원인 (거래소/로컬 원장):**
- 전기간 로컬 PnL 합 ≈ −$31. 주 손실: BTC Sync/Macro Short, 광범위 SHORT, RSI2 등.
- 유일 명확 +EV: `EMA눌림목+거래량급등` / `+돌파` LONG 15m (n≈21, WR≈67%, +$8.8).
- S1 v1 손실 집중: PUMPFUN/1000PEPE/LAB 반복 + 저거래량(0.23x) 통과.

**조치:**
- S1 `ENGINE_VERSION=2026-07-29-s1v2-quality-liquid` (canary 리셋, 과거 성과 미차용).
- S1: LONG-only, min_score 78, volume≥0.90x, 심볼 denylist, 연패 쿨다운.
- `LEGACY_AUTO_TRADE_ENABLED=True` + `BLOCK_SHORT` + EMA 화이트리스트 유지.
- 계좌위험은 canary 0.25% / governor probation 경로 유지 (시드 확대 없음).

## 2026-07-15 Venue Quant Governor v5

- Bybit/Binance는 같은 신호여도 별도 실체결 코호트로 승격한다.
- Bybit 허용 EMA-LONG 코호트: 31건, pnl +$9.10, PF 1.70 → 새 버전은 risk×0.50 probation.
- Binance 동일 코호트: 8건, pnl -$138.83, PF 0.09. v5 실제 OOS 확보를 위해 승인 EMA-LONG만 risk×0.10 canary; 8건 조기평가 실패 시 자동중단.
- 계정 API 실측 taker fee(Bybit 0.055%, Binance 0.050%)와 편도 3bp 슬리피지를 비용/위험 산정에 반영.
- Binance 진입 후 실제 진입가·수량·레버리지·증거금을 재조회하고 위험캡 초과 시 reduce-only 긴급청산.
- Binance 조건부 SL이 별도 Algo 원장에 존재하는 API 변경을 반영해, SL 갱신 시 신규 보호주문 확인 후 구 Algo SL만 취소한다.
- Binance canary는 일손실 주문가능 잔고×0.5%, 동시 포지션 3개(기존 포함), 정식 20건 통과 전 증액 금지.
- 과거 동시 모니터가 남긴 가짜 open 3건은 PnL을 만들지 않고 `ledger_orphan`으로 격리.
- 감사: `python3 tools/quant_research_audit.py`; 방법론: `QUANT_RESEARCH_SYSTEM.md`.

## 2026-07-11 Hyperliquid whale paper (가동)

- **상태:** paper only, 12 지갑 리더보드 시드, LaunchAgent 180s.
- **핵심 수정:** 콜드스타트 시 과거 체결 소급 카피 금지 (커서 시드).
- **정산:** 고래 flat 또는 max_hold 48h. 카피 notional $25, min whale fill $5k.

### 2026-07-19 v2 교체

- v1 63건 PF 0.70, -$7.47로 개별 fill 복사를 폐기.
- 30초 `userFillsByTime`, taker open 합산 $50k+, clearinghouse 순포지션 증가와
  계좌대비 0.5% 확신도를 확인하는 position-delta PAPER로 교체.
- 진입/청산 슬리피지와 양방향 taker fee를 반영하며 v1/v2 통계를 분리.
- 상세 원인: `HYPERLIQUID_WHALE_REVIEW_2026-07-19.md`.
- **재스크리닝:** `python3 tools/hl_whale_screen.py --from-leaderboard --write-config --top 12`
- **LIVE:** 없음. 폴리 고래처럼 paper 성과 본 뒤 결정.

## 2026-07-11 PolyInsight momentum paper (고래 카피와 분리)

- **전략:** PolyInsight analytics `momentum_break` / `prob_shock` 만 paper 진입.
  `extreme_price` 는 진입 안 함 (avoid_chase 필터 용).
- **계좌 분리:** state/journal 전용
  - `polymarket_insight_paper_state.json`
  - `polymarket_insight_paper_journal.jsonl`
  - 태그 `account=insight_paper` — 고래 paper/live 와 bankroll·한도 공유 안 함.
- **러너:** `polymarket_insight_paper_bot.py`
  - LaunchAgent: `com.polymarket.insight.paper.plist` (15분 스캔, TG는 봇 내부 4h)
  - 리포트: `send_review` (고래 4h와 동일 TG 채널, 제목으로 구분)
- **졸업 게이트 (LIVE 전, 수동):** 정산 ≥30 · WR≥55% · PnL>0 · ≥7일
  (`polymarket_insight_insights.py`). 통과해도 paper 봇은 LIVE 안 함.
- **LIVE 스켈레톤:** `polymarket_insight_live_bot.py`
  - “나중에 실주문 넣을 빈 집” 파일. 기본 `POLYMARKET_INSIGHT_LIVE=false`,
    `orders_implemented=False`. 졸업 전/플래그 off 면 상태 출력만.
  - 확인: `python3 polymarket_insight_live_bot.py --status`
- **의견 원칙:** 검증 전 실매매 금지. 검증 후에도 초소액 + 고래 LIVE 지갑/한도와 분리.

## 2026-07-03 Polymarket Paper Bot

- Initial mode: read-only paper trading only. No wallet signing, no live Polymarket orders.
- Runner:
  - `polymarket_paper_bot.py`
  - optional LaunchAgent: `com.polymarket.paper.plist` every 60 seconds
- Report route:
  - Uses existing `publisher.send_review()`, so Polymarket paper reports go to the same trade/review Telegram route as the 4-hour CryptoSignal report.
- Files:
  - state: `polymarket_paper_state.json`
  - paper trade journal: `polymarket_paper_journal.jsonl`
  - observed candidates: `polymarket_paper_candidates.jsonl`
- Default paper logic:
  - Target market: BTC Up/Down 5m.
  - Data: public Polymarket Gamma/CLOB + public Bybit BTC 1m candles.
  - Model: estimates fair Up probability from current BTC price, `priceToBeat`, 1m volatility, and time remaining.
  - Entry: simulates a FOK taker buy only when model probability exceeds average fill price plus Polymarket crypto taker fee and a safety edge buffer.
  - Settlement: waits for Polymarket resolution metadata/outcome prices, then records paper PnL.
- Environment overrides:
  - `POLYMARKET_PAPER_ORDER_USD` default `100`
  - `POLYMARKET_PAPER_MIN_EDGE` default `0.025`
  - `POLYMARKET_PAPER_REPORT_INTERVAL` default `14400`
  - `POLYMARKET_PAPER_RECURRENCE` default `5m`
  - `POLYMARKET_PAPER_MILESTONE_REPORT_AT` installed as `2026-07-10 06:00`
- Scheduled milestone:
  - The running LaunchAgent should send a one-time 1-week paper result report at 2026-07-10 06:00 KST.
- Decision rule before live consideration:
  - Require at least 500 settled paper trades or 2 full weeks of data.
  - Judge by net PnL after simulated taker fee/slippage, not raw win rate.
  - Do not enable live betting unless jurisdiction, KYC/access rules, and API custody risk are explicitly reviewed.

## 2026-07-03 Binance Venue Review

- Binance should be considered as a second execution venue because user capital is larger there.
- Do not replace Bybit first. Run Binance as a separate venue process with isolated state, then cut over later if it performs better.
- Why Binance can help:
  - Deep BTC/ETH/SOL/major alt liquidity.
  - Larger existing seed reduces transfer friction.
  - Useful venue diversification if Bybit API/order routing fails.
  - Can compare fills against Bybit for the same GOT signals.
- Main risks:
  - Region/product availability and account-specific fee tier must be checked inside the account.
  - Binance API has strict request/order limits, timestamp/recvWindow requirements, and unknown-execution handling on `503`; duplicate-order prevention is mandatory.
  - Larger seed increases behavioral risk: venue adapter must enforce per-venue and total-portfolio caps before live orders.
- Current implementation:
  - `exchange_venue_compare.py` compares public Bybit/Binance USD-M books and slippage for the same symbols.
  - `binance_trader.py` implements a Binance USD-M execution adapter with explicit live guard.
  - `trade_router.py` keeps Bybit as the default venue and routes execution/account calls to Binance only when `AUTO_TRADE_EXCHANGE=binance`.
  - `venue_runtime.py` isolates runtime venue, market-data venue, and local state namespace.
  - Binance live orders require both a Binance runtime process and `BINANCE_LIVE_TRADING_ENABLED=true`.
  - Binance state and logs use `_binance` suffix files so Bybit and Binance can run side by side.
  - LaunchAgent templates:
    - Bybit: `com.cryptosignal.plist`, `com.cryptosignal.fast.plist`
    - Binance: `com.cryptosignal.binance.plist`, `com.cryptosignal.binance.fast.plist`
- Required Binance env keys before live use:
  - `BINANCE_API_KEY`
  - `BINANCE_API_SECRET`
  - `BINANCE_LIVE_TRADING_ENABLED=true`
  - For manual single-process Binance runs only: `AUTO_TRADE_EXCHANGE=binance`, `GOT_MARKET_DATA_EXCHANGE=binance`, `GOT_STATE_NAMESPACE=binance`
- Recommended rollout from here:
  1. Keep Bybit LaunchAgents active and add Binance LaunchAgents with small size or live guard disabled.
  2. Confirm Binance OHLCV, balance, position snapshot, SL, TP, and close detection.
  3. Compare Bybit vs Binance journals by venue.
  4. Enable tiny Binance live size only after read-only/account smoke tests pass.
  5. Move larger Binance seed only after expectancy, slippage, and operational reliability beat or match Bybit for at least 2 weeks.
  6. If Binance becomes the only venue, unload the Bybit LaunchAgents and keep the Binance state namespace as-is.

## 2026-07-01 Dynamic Surge Expansion

- Goal: catch more TAIKO-style trades where volume surges first, volatility expands, and a strong directional setup follows.
- Current expansion:
  - `VOLUME_SURGE_TOP_N = 8`
  - `BTC_SYNC_SCAN_TOP_N = 8`
  - `BTC_SYNC_DIRECT_TOP_N = 4`
  - `BTC_SYNC_TOP_N = 50`
- Operating principle:
  - Keep collecting candidate and execution data before widening further.
  - If volume-surge trades show positive expectancy, consider raising surge/BTC-sync scan breadth from 8 to 10.
  - If noise or drawdown grows, keep the scan breadth but tighten trade-entry quality, not the market radar itself.
- Strategy interpretation:
  - TAIKO-type winners are classified as dynamic volume-surge continuation opportunities.
  - Prefer evidence from `trade_execution_journal.jsonl` and `trade_candidates.jsonl` before changing thresholds.

## Exchange Expansion Watchlist

- Current production exchange: Bybit futures.
- Expansion should be considered only after Bybit live-trade expectancy is clearer.
- Candidate order for future review:
  1. Binance Futures, if account/API access is available and legally usable.
  2. Hyperliquid, first as a read-only radar and then as a separate wallet-based execution venue after testing.
  3. OKX Futures/Swap, for liquidity diversification.
  4. Bitget Futures, as a secondary altcoin venue.
  5. Gate/MEXC only for radar or small-size testing first, because long-tail liquidity quality can vary.
- Hyperliquid notes:
  - Attractive because it is a high-volume order-book perp DEX with active long-tail/speculative markets.
  - Integration differs from CEXs: wallet/API-wallet signing, USDC collateral, bridge/deposit handling, and on-chain style account state.
  - Add read-only market radar before live execution. Live trading should use a separate adapter and small test capital.

## 2026-07-01 Strategy 5 / Hyperliquid Lead Radar

- Initial mode: read-only radar, not direct Hyperliquid execution.
- Production execution remains on Bybit.
- Signal flow:
  - Hyperliquid `metaAndAssetCtxs` supplies 24h notional volume, funding, mark price, and open interest.
  - Hyperliquid `candleSnapshot` supplies short-term 15m/1h momentum and volume expansion.
  - Only symbols that can be mapped to Bybit USDT futures are passed into the Bybit scan universe.
- Trading use:
  - If Hyperliquid lead direction agrees with the GOT/Bybit entry direction, apply a small risk confidence boost.
  - If funding is overheated or direction disagrees, keep the radar note but skip the boost.
  - Entry journals include the Hyperliquid snapshot so later performance analysis can compare Strategy 5 assisted trades.
- Next review:
  - After enough Strategy 5 assisted candidates accumulate, compare win rate, payoff, MFE/MAE, and realized PnL against non-assisted trades.
  - Only consider direct Hyperliquid live trading after read-only lead signals show positive expectancy.
- Multi-exchange execution must use a common adapter layer before live orders:
  - market metadata normalization
  - tick/lot size handling
  - leverage/margin mode handling
  - order/position state reconciliation
  - exchange-specific failure and retry rules

## 2026-07-01 Portfolio Capacity Gate

- Fixed concurrent-position blocking is no longer used as a live-entry gate.
- New entries are judged by total margin usage, directional margin concentration, and local SL-based total loss risk.
- If a new signal exceeds a portfolio cap, the bot first tries to reduce position size into the available capacity instead of blocking immediately.
- Current caps:
  - normal signals: 82% account margin usage, 28% total SL risk, 80% same-direction margin concentration
  - high-opportunity signals: 92% account margin usage, 40% total SL risk, 93% same-direction margin concentration

## 2026-07-01 Fast Radar

- The original full scan remains on the 5-minute `com.cryptosignal` LaunchAgent.
- A separate `com.cryptosignal.fast` LaunchAgent runs `main.py --auto-trade --fast-radar` every 3 minutes.
- Fast Radar scans only hot candidates:
  - Bybit volume-surge symbols
  - Hyperliquid lead-radar symbols
  - BTC Sync dislocation symbols
  - currently open-position symbols
- Fast Radar uses only 15m, 1h, and 4h timeframes. It does not run 5m-only trading or daily Bithumb/KRX alerts.
- Purpose: catch fast-moving opportunities sooner without changing the full-scan cadence or weakening existing entry gates.
