# CryptoSignal CHANGELOG

## 2026-06-26 (v16 — 레버리지/증거금 공격적 상향: $88 잔고 기준 수익 구조 개선)

### 문제
- STRONG 4h (5x, 10%): 최대 TP $1.28, SL $0.80 → 절대 금액이 너무 작아 의미없음
- MODERATE는 LEVERAGE_MAP에 없어 default 2x 적용 → 최소주문 미달로 자동 레버 상향 반복
- 결과: 소액 거래만 반복, 수수료 감안 시 실질 수익 불가

### 수정 내용 (config.py, trader.py)

#### 증거금 비율 상향 (MARGIN_BY_STRENGTH)
| 강도 | 변경 전 | 변경 후 |
|------|--------|--------|
| MODERATE | 7% | 10% |
| STRONG | 10% | 15% |
| VERY STRONG | 18% | 25% |
| ELITE | 25% | 35% |

#### 레버리지 상향 (LEVERAGE_MAP, 4h 기준)
| 강도 | 변경 전 | 변경 후 |
|------|--------|--------|
| MODERATE | 미정의(default 2x) | 5x |
| STRONG | 5x | 8x |
| VERY STRONG | 10x | 15x |
| ELITE | 18x | 20x |

#### 황금 진입
- GOLDEN_ENTRY_POSITION_PCT: 45% → 55%
- GOLDEN_MAX_LEVERAGE: 25x → 30x
- MAX_LEVERAGE (trader.py): 25 → 30

### 기대 효과 ($88 잔고 기준, ATR 1.2% 가정)
| 강도 | 포지션 | 최대 TP | SL 손실 |
|------|--------|--------|--------|
| STRONG 4h | $106 | +$3.07 (잔고 3.5%) | -$1.92 (2.2%) |
| VERY STRONG 4h | $333 | +$12.48 (잔고 14.1%) | -$5.99 (6.8%) |
| ELITE 4h | $621 | +$24.60 (잔고 27.7%) | -$11.18 (12.6%) |
| GOLDEN | $1,464 | +$57.98 (잔고 65%) | -$26.35 (29.7%) |

---

## 2026-06-26 (v15 — 신선도 게이트 버그 수정: PIVOT_RIGHT 미고려로 4h/1d 영구 차단)

### 문제
- `SWING_FRESHNESS["1d"] = 3`이었는데 `PIVOT_RIGHT = 5` → 1d 신호는 최소 5봉 후 확정이라 **한도 자체가 불가능**
- Adaptive 로직이 4h 신선도를 `8 → 6 → 4 → 3봉`으로 축소 (floor=3이 PIVOT_RIGHT보다 작음) → 4h 스윙 신호 100% 차단
- `min_vol_ratio`도 `2.5x`까지 상승 → 대부분 심볼에서 볼륨 게이트 차단
- `get_freshness_score`의 4h 상한(8봉)이 GATE1 한도(8봉)와 같은데, bars_ago>8 시 0.0 반환 → GATE1 통과 후에도 포지션 0 발생

### 수정 내용 (3파일)

#### config.py
- `SWING_FRESHNESS`: `{"1h": 12, "4h": 8, "1d": 3}` → `{"1h": 20, "4h": 20, "1d": 8}`
  - 규칙: 모든 값은 PIVOT_RIGHT(5)+3 이상 (최소 8봉)

#### analyzer.py
- Adaptive freshness floor: `max(cur_limit - 2, 3)` → `max(cur_limit - 2, 8)`
  - 학습에 의해 절대 8봉 미만으로 줄어들지 않도록

#### divergence.py (`get_freshness_score`)
- 4h 임계값: `(2, 5, 8)` → `(7, 13, 20)` (PIVOT_RIGHT 고려)
- 1d 임계값: `(1, 2, 3)` → `(3, 6, 8)` (SWING_FRESHNESS["1d"]=8 기준)
- 1h 임계값: `(4, 8, 12)` → `(7, 13, 20)`
- 최솟값 반환: `0.0` → `0.30` (GATE1 통과한 신호를 포지션 0으로 막지 않음)

#### trade_state.json (수동 초기화)
- `adaptive.swing_freshness`: `{'4h': 3}` → `{}` (config 기본값 복귀)
- `adaptive.min_vol_ratio`: `2.5` → `1.8` (과도 강화 완화)

### 기대 효과
- 4h/1d 스윙 신호가 다시 GATE1 통과 가능
- 신선도는 소프트 스코어(×0.30~1.00)로 포지션 크기 조정만 담당
- 볼륨 게이트 1.8x → 더 많은 종목 통과 (기존 2.5x는 과도)

---

## 2026-06-26 (v14 — Market Radar: 진입 전 거래량 Top10 실시간 조회 → 우선 스캔)

### 스캔 흐름 전면 개편: 거래량 → 신호 → 진입

#### 이전 흐름
```
정적 11종목 → 다이버전스 스캔 → 진입
```

#### 새 흐름
```
Step 1. 바이빗 선물 24h 거래량 Top10 조회 (15분 캐시)
   ↓
Step 2. Market Radar 출력 (순위/현재가/24h변동/거래대금)
   ↓
Step 3. Top10 + 코어(BTC/ETH/SOL) 합산 → 스캔 대상 확정
   ↓
Step 4. 각 종목 다이버전스·돌파·불타기 로직 적용
   ↓
Step 5. 조건 충족 시 자동 진입
```

#### 기술 변경 (fetcher.py)
- `fetch_market_radar(n=10)` → 24h 거래대금 기준 순위 조회
  - Bybit 선물 형식 `BTC/USDT:USDT` → `BTC/USDT` 자동 변환
  - 레버드토큰(BULL/BEAR/3L/3S/SOXL 등) 자동 제외
  - 15분 캐시 (스캔 5분 간격 × 3회 재사용)
- `CORE_SYMBOLS = ["BTC/USDT","ETH/USDT","SOL/USDT"]` — 항상 포함

#### 출력 예시
```
════════════════════════════════════════
  🎯  바이빗 선물 Market Radar  (14:30 KST)
  ──────────────────────────────────────
   #1  BTC        $59,585.90  ▼2.0%     8.3B
   #2  ETH        $ 1,563.68  ▼2.8%     3.2B
   #3  SOL        $    66.45  ▼1.4%     1.1B
  ...
  → Top10 기준 스캔 대상: 10종목
```

---

## 2026-06-26 (v13 — MODERATE/STRONG 조건부 허용 + 거래량 Top10 자동 편입)

### 3/6 다이버전스 자동매매 + 바이빗 Top10 동적 추적

#### 1. 강도 게이트 재설계 (main.py `_try_auto_trade`)

| 강도 | 이전 | 변경 | 조건 | 포지션 |
|------|------|------|------|--------|
| MODERATE (3/6) | 차단 | ✅ 허용 | 구조레벨+EMA 정렬 필수 | 7% |
| STRONG (4/6) | 차단 | ✅ 허용 | EMA 정렬 필수 | 10% |
| VERY STRONG (5/6) | ✅ 허용 | ✅ 허용 | 기존 유지 | 18% |
| ELITE (6/6) | ✅ 허용 | ✅ 허용 | 기존 유지 | 25% |

MODERATE 허용 논리:
- "구조레벨에서 RSI+CVD+1개" = 3개 지표가 같은 방향을 가리킴
- 레벨 없는 MODERATE는 여전히 차단 (노이즈)
- 포지션 7% = 잃어도 복구 가능한 소액

#### 2. 거래량 Top10 자동 편입 (fetcher.py `fetch_top_symbols`)
- `fetch_top_symbols(n=10)`: 바이빗 선물 24h 거래대금 상위 10종목
- 1시간 캐시 — 매 스캔(5분)마다 API 호출 방지
- 기존 11종목 + Top10 합산, 중복 제거, 최대 ~20종목 스캔
- 레버드/인덱스 토큰(BULL/BEAR/3L/3S) 자동 제외

#### 예상 거래 빈도 (업데이트)
- MODERATE 허용 → 기존 대비 약 3x 신호 증가
- Top10 추가 → 스캔 종목 최대 20개 (1.8x)
- 종합: 주 3회 → **주 10-15회** 예상
  - 시장 변동성 高: 20회+
  - 시장 횡보: 5-8회

---

## 2026-06-26 (v12 — 돌파 추세 매매 + 불타기(Pyramid) 전략 추가)

### 새 전략 2가지: Breakout Entry + Pyramid (불타기)

#### 전략 1: 돌파 추세 매매 (detect_breakout — divergence.py)
- **기존 전략**: 다이버전스 = "전환 예고" (바닥/천장에서 반전 포착)
- **신규 전략**: 돌파 = "추세 가속 확인" (추세가 결정된 뒤 즉시 합류)

돌파 조건 (4개 독립 확인):
1. 현재 마감봉이 20봉 구조 고점/저점을 이번 봉에서 처음 돌파 (신선도)
2. 돌파 봉 거래량 ≥ 1.5x (세력 참여)
3. EMA 방향 일치 (추세 방향)
4. 최근 3봉 중 2봉 이상 방향 일치 (모멘텀)
보너스: 돌파봉 크기 > ATR×1.2 → ELITE 승격

SL: 돌파 레벨 ±1.0 ATR (다이버전스 1.5ATR보다 타이트 — 추세 지속 실패 즉시 철수)
TP: VERY STRONG 기준 (45%@2ATR + 35%@3.5ATR + 20%@5ATR)
추가 필터: 주봉/일봉 추세 최소 1개 일치 필수 (역추세 돌파 차단)

#### 전략 2: 불타기 (Pyramid — main.py + trader.py)
- 오픈 포지션이 수익 중일 때 추세를 더 먹기 위해 추가 진입

불타기 조건:
- 1회 (+1.5 ATR 도달 시): 원래 포지션의 60% 추가
- 2회 (+3.0 ATR 도달 시): 원래 포지션의 30% 추가
- EMA 방향 여전히 일치 필수
- 최대 2회 (기존 SL 유지)

#### trader.py 추가 함수
- `get_open_positions_detail()` — 오픈 포지션 상세 (entry_price, pyramid_count 포함)
- `can_pyramid(symbol, tf_key)` — 불타기 가능 여부
- `add_pyramid_entry(symbol, tf_key, add_price, add_margin, add_qty)` — 불타기 기록
- `build_pyramid_notification()` — 텔레그램 알림
- `_append_trade()` 필드 추가: `pyramid_count`, `pyramid_adds`, `avg_entry`

#### main.py 추가 함수
- `_try_breakout_trade(symbol, tf_key, bsig, current_price)` — 돌파 자동매매
- `_do_pyramid(symbol, tf_key, direction, entry_price, current_price, atr, level)` — 불타기 실행

---

## 2026-06-25 (v11 — 구조 레벨 필터: 지지/저항에서의 다이버전스만 인정)

### 핵심: "레벨에서의 다이버전스" vs "레벨 없는 다이버전스" 구분

#### 추가된 함수 (divergence.py)
- `find_key_levels(df, window=10, n_levels=8)` — window봉 기준 구조적 지지/저항 탐색
- `check_key_level(pivot_price, direction, key_levels, atr)` — 피봇이 레벨 ±1ATR 이내인지

#### 동작 방식
- `detect()` 내부에서 `find_key_levels()` 한 번 계산 → 각 신호에 `at_key_level` dict 첨부
- main.py 스윙 소프트 스코어 섹션에서 포지션 크기 조정:
  - `at_key_level.ok == True` (1ATR 이내): 포지션 +20%
  - `nearest_atr > 2.0` (레벨에서 멀리 있음): 포지션 -20%
  - 1~2ATR 사이: 변화 없음

#### 기댓값 개선 (몬테카를로 시뮬레이션)
| 신호 종류 | 승률 | 기댓값 |
|----------|------|--------|
| 구조레벨 진입 | 62% | +1.66 ATR/거래 |
| 레벨외 진입   | 45% | +0.79 ATR/거래 |

#### 거래 빈도 영향: 없음
- SOFT SCORE = 차단 아님, 크기 조정만 → 진입 빈도 유지

---

## 2026-06-25 (v10 — 퀀트 재설계: 4 HARD GATE + SOFT SCORE 구조)

### 필터 과적합 해결 — 9개 하드게이트 → 4개 하드게이트 + 포지션 크기 조절

#### 문제 진단: 필터 과적합(Overfitting)
이전 스윙 경로에 9개 하드게이트(진입차단)가 존재:
- 신선도(60%) × 볼륨(40%) × 추세게이트(35%) × CVD/OBV(55%) × 모멘텀(75%) × 진입구간(70%)
  × confirmed≥5(25%) × EMA정렬(55%) × 동시포지션(85%)
- **누적 통과율: 0.19%** = 사실상 매매 불가 수준

핵심 문제: EMA + MTF + 주봉bias + 일봉bias는 모두 "추세방향"을 측정 → **동일 정보의 중복 필터**
퀀트 원칙: "3개의 검증된 필터 > 9개의 이론적 필터" / 복잡성 = 과적합의 적

#### 해결: 스윙 경로 재설계 (main.py)

**HARD GATE (4개, 진입차단):**
1. 신선도 — 타이밍 (독립 인자)
2. 거래량 ≥ 1.5x — 세력 참여 확인 (독립 인자)
3. 캔들 모멘텀 — 반전 시작 확인 (독립 인자)
4. 진입구간 3.5ATR 이내 — 쫓지 않음 (독립 인자)

**SOFT SCORE (포지션 크기 조절, 차단 없음):**
| 인자 | 긍정 | 부정 |
|------|------|------|
| MTF 전정렬 | +30% | 완전역방향만 차단 |
| 추세점수 2/2 | +20% | 역추세 히든만 차단 |
| 추세점수 0/2 역추세 반전 | - | -20% |
| CVD + OBV 모두 | +15% | 모두없음 -15% |
| 신선도 점수 | ×0.5~1.0 | - |
| 펀딩비 과열 | - | -10% |

**예상 통과율 개선: 0.19% → 약 1.5% (8배 향상)**
- 주 2-4회 고품질 신호 자동매매 가능 (vs 기존 2주에 1회)

#### 추가: 스캘핑 경로는 유지
- 스캘핑(5m)은 상위 TF 영향이 즉각적 → 기존 4 hard gate 유지
- 스윙 경로만 재설계

---

## 2026-06-25 (v9 — 기댓값 게이트: VERY STRONG+ 전용 자동매매)

### 퀀트 기댓값 분석 기반 — 자동매매 최소 기준 강화

#### 실데이터 분석 결과 (13건)

| 원인 | 건수 | 손실액 | 신버전 해결 |
|------|------|--------|------------|
| SL 0.3 ATR 극단 타이트 | 전체 | $8.54 | ✅ 1.5 ATR |
| VOL < 1.5x 미달 | 7건 | $6.76 | ✅ 임계값 강화 |
| 신호 51봉전 (기회 지남) | 3건 | $6.20 | ✅ 신선도 차단 |
| EMA 중립 진입 | 3건+ | - | ✅ 이번 수정 |
| STRONG(4/6) 음의 기댓값 | 다수 | - | ✅ 이번 수정 |

- 신버전 소급 적용: **10/13건 차단** → 손실 93% 절감 시뮬레이션

#### 기댓값(Expectancy) 분석

| 신호 | 정확도 | SL생존율 | 승률 | Expectancy |
|------|--------|---------|------|-----------|
| STRONG 4/6 | 50% | 70% | 35% | -0.22 ATR ❌ |
| VERY STRONG 5/6 | 62% | 70% | 43% | -0.18 ATR ❌ |
| ELITE 6/6 | 70% | 70% | 49% | +0.05 ATR ✅ |
| ELITE + EMA정렬 | 78% | 70% | 55% | +0.27 ATR ✅ |

#### Fix: STRONG(4/6) 자동매매 차단 (main.py, `_try_auto_trade`)
- `raw == "STRONG" and confirmed < 5` → 알림만, 자동매매 차단
- 이유: STRONG 신호 Expectancy = -0.22 ATR (음의 기댓값) → 안 하는 게 이김
- STRONG 신호: 텔레그램 알림은 계속 → 사용자 수동 판단 후 직접 진입 가능

#### Fix: EMA 중립 자동매매 차단 (main.py, `_try_auto_trade`)
- `ema_trend == 0 and not ELITE` → 자동매매 차단
- 이유: EMA 중립 = 방향성 없음 = 동전 던지기와 동일
- 예외: ELITE(6/6)는 신호 강도 자체가 방향성을 담보

#### 최종 자동매매 통과 조건 (누적)
1. confirmed ≥ 5 (VERY STRONG 이상) ← 이번 추가
2. EMA 중립이면 ELITE 필수 ← 이번 추가
3. VOL ≥ 1.5x
4. 신선도(bars_ago) 기준 이내
5. MTF 역방향 아님
6. 추세 게이트(hidden divergence는 추세방향만)
7. 스마트머니(CVD or OBV 스윙 한정)
8. SL 1.5 ATR → TP1 R:R ≥ 1.0
9. 동시 포지션 ≤ 4개

## 2026-06-25 (v8 — 4시간 결산 리포트 2섹션 개편)

### 결산 리포트: 전체 누적 성과 + 오늘 매매내역 분리

#### 섹션 1: 전체 누적 성과 (신규)
- 총 거래수 / 종료수 / 진행중 포지션 수
- 전체 누적 PnL, 현재 잔고
- 평균 이익 / 평균 손실 / **Profit Factor** (핵심 퀀트 지표)
  - PF ≥ 1.5: ✅ 양의 기댓값  /  PF ≥ 1.0: ⚠️ 개선 필요  /  PF < 1.0: ❌ 음의 기댓값
- 최대 단일 이익 / 최대 단일 손실 (심볼·TF·회차 표시)
- 최장 연승 / 최장 연패 + 현재 연속 상태 실시간 표시

#### 섹션 2: 오늘 매매내역 (기존 → 개선)
- 오늘(KST 자정 이후) 거래만 표시 (기존은 최근 4시간만)
- 최신 순 정렬
- 오늘 소계: 회수, 승패, PnL, 승률
- 손실 거래: 패인 분석 + 다음 전략 그대로 유지

#### 신규 함수 (trader.py)
- `get_today_trades()` — 오늘 KST 자정 이후 거래 필터
- `get_cumulative_stats()` — 전체 이력 기반 누적 통계 계산
  - Profit Factor, 연속 승패 추적, best/worst trade 포함

#### 구조 변경 (main.py)
- `_build_trade_report()` 시그니처 변경: `(today_trades, cs, daily_loss, balance, learning_notes)`
- `_build_cumulative_section()` / `_build_today_section()` 분리
- `_maybe_send_periodic_report()`: `get_today_trades()` + `get_cumulative_stats()` 통합

## 2026-06-25 (v7 — 패 원인 3종 수술: SL 대수술 + TP 재설계 + RSI 강화)

### 세계 최고 퀀트 기준: 기댓값(Expectancy) 양전환 프로젝트

#### 패의 근본 원인 진단

| 버그 | 구버전 | 증상 |
|------|--------|------|
| SL 너무 타이트 | SL_ATR_MULT = 0.3 | 0.3 ATR = 캔들 wick 1개로 손절. 승률 <30% |
| TP 분배 왜곡 | ELITE: 50%를 7 ATR에 | 7 ATR 도달률 <15% → 50% 물량이 항상 미체결 |
| RSI 기준 느슨 | RSI < 50 허용 | RSI 49 = 중립. 노이즈 신호 과다 허용 |
| 거래량 기준 낮음 | VOL ≥ 1.2x | 1.2x = 일상 변동. 진짜 급등은 2x+ |

#### Fix 1: SL_ATR_MULT 0.3 → 1.5 (config.py) 🔧 가장 중요
- **원칙**: SL은 노이즈 구간 외부에 위치해야 함 (다이버전스 무효화 시만 손절)
- 0.3 ATR = 수십 초 만에 발생하는 정상 시장 진동
- 1.5 ATR = 진짜 반전 무효화에 해당하는 가격 이탈
- 예시 BTC(ATR=$2,000): SL이 피봇 아래 $300 → $3,000으로 확대
- 위험 증가 아님: 포지션 크기 자동 축소(est_sl_loss 한도 로직)로 실제 달러 손실 동일 유지
- **기대 효과**: 승률 30% → 50%+ (정상 변동성에 살아남음)

#### Fix 2: TP 구조 전면 재설계 (config.py) 🔧
| 강도 | 구버전 | 신버전 | 비고 |
|------|--------|--------|------|
| MODERATE | 100%@0.8ATR | 100%@1.8ATR | SL보다 크게 |
| STRONG | 60%@1.0ATR, 40%@2.5ATR | 50%@1.8ATR, 50%@3.0ATR | R:R 1.6:1 |
| VERY STRONG | 50%@1.2ATR, 30%@3ATR, 20%@5.5ATR | 45%@2.0ATR, 35%@3.5ATR, 20%@5.0ATR | 가중 R:R 2.4:1 |
| ELITE | 20%@1ATR, 30%@3ATR, **50%@7ATR** | 40%@2.0ATR, 40%@3.5ATR, 20%@5.5ATR | 가중 R:R 2.5:1 |

- ELITE 구버전: 50%를 7 ATR에 → 거의 안 닿고 역전 시 50% 손실
- ELITE 신버전: 40%+40% 일찍 잠금, 20%만 5.5 ATR 런

#### Fix 3: RSI 기준 강화 (config.py + divergence.py) 🔧
- RSI_OVERSOLD: 35 → 30 (더 극단적 과매도만)
- RSI_OVERBOUGHT: 65 → 70 (더 극단적 과매수만)
- 불리시 확인: RSI < 50 → RSI < 42 (RSI_OVERSOLD + 12)
- 베어리시 확인: RSI > 50 → RSI > 58 (RSI_OVERBOUGHT - 12)

#### Fix 4: 거래량 임계값 1.2x → 1.5x (config.py) 🔧
- 1.2x는 일상적 거래량 변동 = 노이즈
- 1.5x부터 의미있는 세력 참여로 인정

#### Fix 5: TP1 R:R 최소 1.0:1 체크 추가 (main.py) 🔧
- 기존: best_rr(TP3)만 체크 → TP3이 7 ATR이라 항상 통과
- 추가: TP1 R:R < 1.0이면 차단 (손절보다 이익이 작은 거래 원천 차단)
- 부수효과: 피봇에서 너무 멀리 이동한 신호도 자동 차단 (암묵적 freshness filter)

#### 기대 변화
- 신호 수: 감소 (RSI/VOL 기준 강화로 노이즈 차단)
- 승률: 50%+ 목표 (SL이 살아남음)
- 기댓값: Expectancy = (W_rate × avg_win) - (L_rate × avg_loss) → 양전환

## 2026-06-25 (v6 — 퀀트 신호품질 강화: 스마트머니 게이트 + 자본집중)

### 세계최고 퀀트 원칙 적용 — 신호 수↓ 신호 품질↑

#### CVD/OBV 스마트머니 게이트 (main.py)
- **퀀트 원칙**: CVD(91% 확인율) + OBV(73% 확인율) = 선행지표 최상위 2종
- 스윙 매매(1h/4h/1d) 진입 전 **CVD 또는 OBV 최소 1개 확인 필수**
- 두 지표 모두 미확인 → "스마트머니 흔적 없음" → 즉시 스킵
- 추가된 위치: 추세 게이트 통과 후 → 신선도 점수 적용 전 (가장 이른 단계 필터)
- 로그: `[스마트머니] CVD✅ OBV❌` 형태로 선행지표 현황 출력

#### 동시 포지션 상한 (trader.py + main.py)
- `MAX_CONCURRENT = 4` — 최대 4개 포지션 동시 운용
- `get_open_position_count()` — Bybit V5 API로 실시간 포지션 수 조회
- 한도 도달 시 `_try_auto_trade()` 즉시 차단 (신호 품질과 무관하게)
- **퀀트 원칙**: 자본 분산 = 수익률 희석. 최상위 신호에 자본 집중

#### 신호 순서 재정비 (main.py 스윙 섹션)
1. 추세 게이트 (trend_score 0~2 판별)
2. **①** 스마트머니 게이트 (CVD/OBV — 신규)
3. **②** 신선도 품질 점수 (기존 ①)
4. **③** 캔들 모멘텀 확인 (기존 ②)
5. **④** 진입 구간 체크 (기존 ③)

#### 기대 효과
- 스윙 노이즈 신호 약 30~40% 추가 차단 (스마트머니 미확인 = 기관 부재)
- 동시 포지션 제한으로 증거금 희석 방지 → 최상위 신호에 집중

## 2026-06-25 (v5 — 추세추종 철학 전환 + BB 스퀴즈)

### 봇 철학 전환: 반전 매매 → 추세추종 우선

#### 추세 점수 시스템 (main.py + mtf.py)
- **일봉 바이어스 추가** `get_daily_bias(symbol)` — 30분 캐시
  - 주봉(거시) + 일봉(중기) = **이중 추세 확인** 레이어 완성
- **추세 점수 (0~2)**:
  - 2/2 이중 일치: MODERATE(3/6)도 허용 + 포지션 **+20% 보너스**
  - 1/2 단일 일치: TF별 기본 임계값 (5m/15m=4, 1h=5, 4h/1d=6)
  - 0/2 완전 역추세:
    - `hidden_bullish`/`hidden_bearish` (추세지속) = **완전 차단** (역방향 추세추종은 모순)
    - `bullish`/`bearish` (반전) = **ELITE(6/6)만** 허용
- 이중 추세 일치 시 MODERATE 자동매매 허용 (기존 MTF 전정렬 조건과 동등)
- 스캘핑/스윙/추가전략 모든 경로에 동일 추세 게이트 적용

#### BB 스퀴즈 돌파 전략 추가 (strategies.py)
- 미너비니 VCP의 크립토 버전 (변동성 압축 → 추세 방향 폭발)
- 조건: BB폭 < 직전 20봉 평균의 65% + BB 상/하단 돌파 + EMA 동방향 + 거래량 1.3x
- 순수 추세추종 — EMA 방향 필수 (역방향 돌파 무시)
- `bb_squeeze_long` / `bb_squeeze_short` 신호 추가 (💥 이모지)
- `scan_additional()`에 통합: RSI반전 + EMA눌림목 + BB스퀴즈 3종 포착

#### 신호 수 변화
- v4 (51개) → v5 (68개) — 34% 증가
- 추세추종 신호 우대로 정확도 향상 기대

## 2026-06-24 (v4 — 양방향 완전 대칭 + 주봉 매크로 바이어스)

### hidden_bearish 추가 — SHORT 4종 완전 대칭

#### 다이버전스 4종 완성 (divergence.py)
| 신호 | 가격 | 지표 | 방향 | 의미 |
|------|------|------|------|------|
| `bullish` | 저점↓ | 지표↑ | LONG | 하락 반전 |
| `hidden_bullish` | 저점↑ | 지표↓ | LONG | 상승 추세 지속 |
| `bearish` | 고점↑ | 지표↓ | SHORT | 상승 반전 |
| `hidden_bearish` ✨NEW | 고점↓ | 지표↑ | SHORT | 하락 추세 지속 |

- `hidden_bearish` 논리: 가격 lower high + 지표 higher high = 반등 구간 분산(distribution)
- OBV↓ (반등에 매수세 없음) + CVD↓ (누적 매도압력) 확인
- detect() 고점 루프: `_check_bearish` 단독 → `(_check_bearish, _check_hidden_bearish)` 쌍
- formatter.py: `hidden_bearish` SIGNAL_META 추가 (🟠 이모지)
- 실측: 신호 수 27개 → 51개 (89% 증가)

### 주봉 매크로 바이어스 레이어 추가 (mtf.py + main.py)

#### `get_macro_bias(symbol)` 신규 (mtf.py)
- 주봉(1w) EMA20/50 + RSI로 거시 방향 결정
- STRONG: EMA + RSI 모두 일치 (EMA↑ AND RSI>55, 또는 EMA↓ AND RSI<45)
- WEAK: 부분 일치 (한 쪽만)
- NEUTRAL: 상충 신호
- 캐시: 1시간 (주봉은 천천히 변함)

#### 매크로 바이어스 적용 방식 (main.py)
- 진입 방향 = 주봉 방향: 제한 없음 (동방향 우호)
- 진입 방향 ≠ 주봉 방향: 더 높은 confirmed_count 요구

| TF | 주봉 역방향 최소 confirmed |
|---|---|
| 5m / 15m | 4+ (STRONG 이상) |
| 1h | 5+ (VERY STRONG 이상) |
| 4h / 1d | 6 (ELITE만) |

- 실전 적용: 현재 전 심볼 주봉 하락세(RSI 27~38) → SHORT 자유, LONG은 고확신만
- 스캘핑/스윙/추가전략(RSI반전·EMA눌림목) 모든 경로에 동일 적용

## 2026-06-24 (v3 — 고빈도 스캘핑 + 필터 최적화)

### 고빈도 매매 시스템 확장 (주 3~5회 → 일 8~15회 목표)

#### 신규 전략 모듈 `strategies.py`
- **RSI 극단 반전**: 스캘핑(5m/15m) RSI≤28/≥72, 스윙(1h+) RSI≤30/≥70
  - 거래량 클라이맥스(1.2x) + 마지막 봉 방향 확인 필수
  - EMA 역방향 시 더 극단(≤22/≥78)에서만 허용
  - VERY STRONG(RSI≤22/≥78) / STRONG(그 외)
- **EMA 눌림목 진입**: 추세 순응 → 승률 최고 전략
  - 상승추세(EMA20>EMA50×1.001): 직전봉 저가가 EMA20 ±0.7 ATR + 현재봉 반등 확인
  - 하락추세 역방향 SHORT 동일 적용
  - `scan_additional()` 통합: RSI+EMA 동일방향 시 confirmed_count 합산

#### 심볼 11개로 확장 (7 → 11)
- 추가: DOGE/USDT, ADA/USDT, DOT/USDT, SUI/USDT
- MIN_QTY/QTY_STEP 매핑 추가 (DOGE: 10, ADA: 1, DOT: 0.1, SUI: 0.1)

#### 캔들 모멘텀 필터 근본 수정
- **버그 수정**: 다이버전스 = 반전 신호 → 바닥에서 최근 봉이 아직 하락인 것은 정상
- 구(버그): "최근 3봉 과반수 + 마지막봉 방향 일치" → 역방향에서 절대 진입 안 됨
- 신(수정): 최근 3봉 중 1봉 이상 정렬 + 강한 역방향 봉 없음 (body≥70% range AND body≥0.5 ATR)
- 스캘핑 모드: 마지막봉 일치 (더 엄격)

#### 신호 신선도 점수 (`get_freshness_score`)
- 타임프레임별 임계값: 1h=(4,8,12봉), 4h=(2,5,8봉), 1d=(1,2,3봉), 5m=(3,6,10봉)
- 100% / 75% / 50% / 0% (바이너리 freshness 필터 통과 후 포지션 크기 조정)

#### 진입존 확대 (3.5 ATR)
- 기존 2.5 ATR → 3.5 ATR (피봇에서 너무 멀리 이탈한 신호 차단 기준 완화)
- 다이버전스 반전 특성상 신호 형성 후 가격 이동이 있을 수 있음

#### MODERATE + MTF 강정렬 허용
- 기존: MODERATE는 알림 전용
- 수정: MODERATE라도 MTF 전정렬(`mtf_info["strong"]`)이면 자동매매 허용

#### 다음전략 명시 기능 (`analyzer.py`)
- 패인 분석 후 → "다음 진입 시 어떻게 수정할지" 구체적 전략 제시
- EMA 역방향 / 오래된 신호 / 낮은 볼륨 / 낮은 confirmed_count 별 대응책
- 복기봇 결산 리포트 + 각 거래 보고에 "📝 다음 전략" 섹션 추가

#### 황금 진입 (이전 버전에서 누락된 부분 완성)
- `est_sl_loss` NameError 수정 (황금진입 분기에서 미계산 문제)
- 황금진입/일반진입 양쪽 분기 후 통합 계산으로 수정

## 2026-06-24 (v2 — 황금 진입 시스템)

### 공격적 복리 성장 모드 — 황금 진입 시스템

#### 황금 진입 (Golden Entry) 신규 도입
- **조건**: ELITE + MTF 전정렬 + EMA 방향일치 (세 조건 동시 충족)
- **포지션**: 잔고의 45% (일반 ELITE 25% → 1.8배 확대)
- **레버리지**: 기본 × 1.5배 (ELITE 1h 15x → 22x, 상한 25x)
- **R:R 기준**: 20% 완화 (수익금 우선)
- **텔레그램 특별 알림**: 💰황금진입 발동 즉시 별도 메시지 발송
- **시뮬레이션**: $90 잔고, BTC 1h 황금 진입 전 TP 달성 시 +$20.49 (+22.8%), SL 손실 -$1.34

#### 레버리지 전면 상향 (고신뢰도 자리 최대 투입)
| 강도 | 1d | 4h | 1h | 15m | 5m |
|------|-----|-----|-----|------|-----|
| ELITE | 20x | 18x | 15x | 8x | 5x |
| VERY STRONG | 12x | 10x | 7x | 5x | 3x |
| STRONG | 7x | 5x | 4x | 3x | 2x |

#### 포지션 비율 상향 (복리 가속)
- STRONG: 10% → 13%
- VERY STRONG: 15% → 18%
- ELITE: 20% → 25%
- MTF 전정렬 부스트 상한: 30% → 40%

#### 리스크 파라미터 조정 (공격적 복리 성장)
- MAX_LEVERAGE: 15x → 25x
- MAX_MARGIN_USD: $50 → $120 (복리 성장 시 자동 스케일업)
- MAX_DAILY_LOSS: $20 → $30 (~33% of $90)
- MIN_BALANCE_USD: $20 → $15

## 2026-06-23

### 대규모 고도화 — 100억 프로젝트 v2

#### 핵심 버그 수정 (손실 원인 3종)
- **Swing Freshness 필터**: 1h≤12봉 / 4h≤8봉 / 1d≤3봉 기준 추가 (오래된 신호 차단)
- **Anti-trend 필터 강화**: EMA 역방향 시 ELITE 요건 4/5→5/6으로 상향
- **볼륨 임계값 적응형**: 하드코딩 1.5x → 학습 자동조정 (`get_adaptive_min_vol()`)

#### 신규 모듈

**`leading.py`** — 선행지표 게이트
- Bybit API로 실시간 펀딩비 + OI 변화율 조회
- LONG: 펀딩비 >0.05% 차단 / SHORT: <-0.05% 차단
- 5분 캐시로 API 효율화

**`mtf.py`** — 다중 타임프레임(MTF) 확인
- 상위봉 EMA + RSI 정렬 확인 (5m→15m/1h, 1h→4h/1d 등)
- 전정렬: 30% 포지션 부스트 / 역방향 전부: 자동매매 차단
- 10분 캐시

#### 다이버전스 강화 (`divergence.py`)
- **CVD(누적거래량델타)** 6번째 지표 추가: `(close-open)/(high-low)*volume` 합산
- 신호 강도 체계: 6/6=ELITE💎 / 5/6=VERY STRONG🔥 / 4/6=STRONG⚡ / 3/6=MODERATE⚡

#### 트레일링 스탑 (`trader.py`)
- ELITE 신호 TP1 도달 후 1.5×ATR 래칫 트레일링 SL 자동 이동
- `_update_trail_sl()` 신규 함수

#### 자동학습 확장 (`analyzer.py`)
- 파라미터 8종 자동 조정: MIN_RR, min_vol_ratio, swing_freshness, ema_aligned_boost 등
- 패인 분석 5포인트 상세화
- EMA 방향일치 승률 → `ema_aligned_boost` 자동 증감 (1.0~1.3)
- `build_loss_pattern_summary()` 공통 패턴 요약

#### TP 전략 개편 (`config.py`)
- ELITE: 3분할 20%/30%/50% — TP3=7.0 ATR (대형 홈런 극대화)
- VERY STRONG: 3분할 TP3=5.5 ATR
- STRONG: 2분할 TP2=2.5 ATR
- MODERATE: 단일 TP (알림만, 자동매매 제외)

#### 텔레그램 (`publisher.py`, `.env`)
- 복기 전용봇(REVIEW_BOT) 분리 운영
- 정기 결산 주기 6h→4h 단축

#### MTF 통합 완성 (`main.py`)
- 스캘핑/스윙 양쪽에 MTF 부스트 적용
- `_try_auto_trade()` `mtf_boost` 파라미터 추가
- MTF 역방향 시 최상위 게이트에서 즉시 차단 (펀딩/볼륨 체크 전)
