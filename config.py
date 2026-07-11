SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "BNB/USDT", "AVAX/USDT", "LINK/USDT",
    # 추가 심볼 — 잦은 신호 + 충분한 유동성
    "DOGE/USDT", "ADA/USDT", "DOT/USDT", "SUI/USDT",
]

# ─── 동적 거래대금 레이더 ───────────────────────────────────────────────────
# Top20 절대 거래대금 + 직전 스캔 대비 거래대금 급증 종목을 함께 감시한다.
# 2026-07-08: 10→20. 후보 부족이 매매빈도 병목은 아니었음(정밀분석 대상이 이미
# 스캔당 12~16종목, 차단 대부분은 신호품질 게이트)이나, 후보풀을 넓혀도 기존
# 품질 게이트(과열도가드/MTF/화이트리스트/포트폴리오캡)가 전부 그대로 적용돼서
# 안전마진은 유지된다 — 저품질 신호가 늘어난 후보에서 나와도 동일 기준으로 걸러짐.
RADAR_TOP_N = 20
VOLUME_SURGE_TOP_N = 8
VOLUME_SURGE_MIN_24H_USD = 3_000_000
VOLUME_SURGE_MIN_DELTA_USD = 500_000
VOLUME_SURGE_MIN_DELTA_PCT = 3.0

# ─── Fast Radar ─────────────────────────────────────────────────────────────
# 전체 스캔은 5분 주기로 유지하고, 급등/선행수급/괴리 후보만 3분 주기로 재확인한다.
# 15m를 최소 판단봉으로 유지하기 위해 5m 단독 매매는 빠른 스캔에서도 제외한다.
FAST_RADAR_ENABLED = True
FAST_RADAR_INTERVAL_SECONDS = 180
FAST_RADAR_MAX_SYMBOLS = 12
FAST_RADAR_SURGE_TOP_N = 5
FAST_RADAR_TIMEFRAMES = {"15m", "1h", "4h"}

# ─── BTC 동조/괴리 레이더 ───────────────────────────────────────────────────
# X에서 유행하는 "50개 이상 시장 스캔 + BTC 데이터 동기화 + 가격 오류 탐지" 아이디어를
# 안전하게 흡수한 모듈이다. 진짜 무위험 차익거래가 아니라, BTC 대비 과도하게 강하거나
# 약한 종목을 찾아 기존 GOT 전략 스캔 대상과 전략 3 독립매매 후보로 제공한다.
BTC_SYNC_RADAR_ENABLED = True
BTC_SYNC_TOP_N = 50
BTC_SYNC_SCAN_TOP_N = 8
BTC_SYNC_TIMEFRAME = "5m"
BTC_SYNC_LOOKBACK = 12          # 5m × 12 = 최근 1시간
BTC_SYNC_MIN_24H_USD = 10_000_000
BTC_SYNC_MIN_ABS_GAP_PCT = 1.20 # BTC 대비 수익률 괴리 최소값
BTC_SYNC_MIN_VOL_RATIO = 1.20   # 최근 거래량이 평균보다 커야 이벤트로 인정
BTC_SYNC_BETA_LOOKBACK = 72      # 5m × 72 = 최근 6시간 베타/상관 계산
BTC_SYNC_MIN_BTC_MOVE_PCT = 0.25 # BTC가 거의 안 움직인 구간은 괴리 신뢰도 낮음
BTC_SYNC_MIN_CORRELATION = 0.15  # BTC와 완전히 무관한 종목은 베타 괴리 신뢰도 낮음
BTC_SYNC_REVERSION_ZSCORE = 2.20 # 잔차 기준 2.2σ 이상이면 평균회귀 후보
BTC_SYNC_MAX_SPREAD_PCT = 0.15   # 호가 스프레드가 넓으면 체결 품질이 나쁨

# ─── 매매전략 3: BTC Sync Dislocation 독립 진입 ─────────────────────────────
# 레이더 후보 중 괴리/거래량이 특히 강한 종목은 기존 다이버전스/MTF 검증을 거치지 않고,
# BTC 동조 실패 자체를 하나의 독립 전략으로 매매한다. 단, 손익비/기대ROI/계좌위험
# 안전장치는 그대로 적용해서 "신호는 독립, 리스크는 통합" 구조를 유지한다.
BTC_SYNC_DIRECT_TRADE_ENABLED = False
BTC_SYNC_DIRECT_TOP_N = 2
BTC_SYNC_DIRECT_TIMEFRAME = "15m"      # 진입/ATR/SL 계산 기준봉. 5m 레이더보다 안정적인 판단봉.
BTC_SYNC_DIRECT_MIN_ABS_GAP_PCT = 2.00 # 기존 레이더보다 높은 실거래 전용 괴리 기준
BTC_SYNC_DIRECT_MIN_VOL_RATIO = 1.80
BTC_SYNC_DIRECT_MIN_CORRELATION = 0.20
BTC_SYNC_DIRECT_MOMENTUM_MIN_ZSCORE = 1.50
BTC_SYNC_DIRECT_REVERSION_MIN_ZSCORE = 2.20
BTC_SYNC_DIRECT_MAX_SPREAD_PCT = 0.05
BTC_SYNC_DIRECT_BASE_LEVERAGE = 10
BTC_SYNC_DIRECT_MAX_LEVERAGE = 20
BTC_SYNC_DIRECT_RISK_PCT = 0.0065
BTC_SYNC_DIRECT_POSITION_CAP = 0.18
BTC_SYNC_DIRECT_MARGIN_USD = 12.0
BTC_SYNC_DIRECT_COOLDOWN_MIN = 120
BTC_SYNC_DIRECT_DAILY_SYMBOL_LOSS_LIMIT = 1
BTC_SYNC_DIRECT_STOP_ATR_MULT = 1.0
BTC_SYNC_DIRECT_STOP_MIN_PCT = 0.004
BTC_SYNC_DIRECT_TP_RR = [1.20, 1.80, 2.80]
BTC_SYNC_DIRECT_TP_PCT = [50, 30, 20]
BTC_SYNC_DIRECT_MIN_TP1_RR = 1.20
BTC_SYNC_DIRECT_MIN_BEST_RR = 2.40

# ─── 매매전략 5: Hyperliquid Lead Radar ────────────────────────────────────
# 하이퍼리퀴드는 지갑/브릿지/서명 구조가 CEX와 달라서 초기에는 실매매가 아니라
# 선행 수급 레이더로만 사용한다. Hyperliquid에서 먼저 터진 거래량/OI/가격 모멘텀이
# Bybit 상장 종목과 일치하면 기존 GOT 전략의 스캔 대상과 진입 근거에 가산한다.
HYPERLIQUID_RADAR_ENABLED = True
HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_TOP_N = 80
HYPERLIQUID_CANDLE_TOP_N = 24
HYPERLIQUID_SCAN_TOP_N = 8
HYPERLIQUID_TIMEFRAME = "15m"
HYPERLIQUID_LOOKBACK_BARS = 32
HYPERLIQUID_MIN_24H_USD = 5_000_000
HYPERLIQUID_MIN_15M_MOVE_PCT = 0.70
HYPERLIQUID_MIN_1H_MOVE_PCT = 1.20
HYPERLIQUID_MIN_VOL_RATIO = 1.35
HYPERLIQUID_MIN_OI_USD = 500_000
HYPERLIQUID_LEAD_RISK_MULT = 1.12
HYPERLIQUID_MAX_FUNDING_ABS = 0.0025

# ─── BTC 장기봉 참고 바이어스 ───────────────────────────────────────────────
# 월봉/주봉/일봉은 단타·스캘핑의 직접 진입 트리거가 아니라 큰 배경 추세 확인용이다.
# 실제 진입/방향/시드는 15m·1h·4h의 타점, 손익비, 거래량, 최근 성과가 결정한다.
BTC_MACRO_SHORT_ONLY_ENABLED = True
BTC_MACRO_SHORT_SYMBOL = "BTC/USDT"
BTC_MACRO_TREND_REFERENCE_ONLY = True
BTC_MACRO_SHORT_BLOCK_LONG = False
BTC_MACRO_SHORT_MIN_SCORE = 4
BTC_MACRO_SHORT_CACHE_TTL = 1800
BTC_MACRO_SHORT_RISK_MULT = 1.00
BTC_MACRO_SHORT_LEVERAGE_MULT = 1.00
BTC_MACRO_SHORT_MAX_LEVERAGE = 20
BTC_MACRO_SHORT_POSITION_CAP = 0.30
BTC_MACRO_SHORT_MARGIN_USD = 20.0
BTC_MACRO_SHORT_MARGIN_PCT = 0.20
BTC_MACRO_SHORT_MAX_ACCOUNT_RISK_PCT = 0.025
BTC_MACRO_SHORT_SWING_MIN_VOL = 1.00

# ─── 수수료 ───────────────────────────────────────────────────────────────────
BYBIT_TAKER_FEE = 0.00055   # 0.055%
ROUND_TRIP_FEE  = BYBIT_TAKER_FEE * 2

# ─── 타임프레임별 최소 TP1 수익률 (gross %, 수수료 전) ──────────────────────
MIN_GROSS_PCT = {
    "5m":  0.45,
    "15m": 0.70,
    "1h":  1.00,
    "4h":  1.50,
    "1d":  2.00,
}

# 5m/15m는 다이버전스 3개 이상 + 전체 확인 4개 이상이어야 발송 (노이즈 필터)
STRICT_TF = {"5m", "15m"}

# 스캘핑 자동매매 신호 신선도 — 피봇 형성 후 최대 허용 봉 수
# PIVOT_RIGHT=5 때문에 최솟값은 5 (5봉 후 확정)
SCALP_FRESHNESS = {"5m": 10, "15m": 8}

# 스윙 자동매매 신호 신선도 — 너무 오래된 피봇은 유효성 상실
# PIVOT_RIGHT=5봉 확정 지연 때문에 모든 값은 반드시 PIVOT_RIGHT+2 이상이어야 함
# 1h: 20봉(20시간) / 4h: 20봉(80시간≈3.3일) / 1d: 8봉(8일)
SWING_FRESHNESS = {"1h": 20, "4h": 20, "1d": 8}

# ─── 신호 강도별 TP 전략 (SL 1.5 ATR 기준 재설계) ────────────────────────────
# 핵심 원칙: SL = 1.5 ATR → TP는 SL의 배수로 맞춰 양의 기댓값 유지
#
# MODERATE:     단일 TP @ 1.8 ATR → R:R 1.2:1  (노이즈 필터, 빠른 확정)
# STRONG:       2분할 — TP1 50%@1.8ATR, TP2 50%@3.0ATR → 가중 R:R ≈ 1.6:1
# VERY STRONG:  3분할 — 45%@2.0ATR + 35%@3.5ATR + 20%@5.0ATR → 가중 R:R ≈ 2.4:1
# ELITE:        3분할 — 40%@2.0ATR + 40%@3.5ATR + 20%@5.5ATR → 가중 R:R ≈ 2.5:1
#               → TP1을 40% 대량 잠금 후, TP2도 40%로 코어 확보, 20%만 장거리
#               구버전(20/30/50 @ 1/3/7ATR)은 50%가 거의 안 닿는 7ATR에 배치 = 수익 파괴
TP_BY_STRENGTH = {
    "MODERATE":    [{"pct": 100, "atr_mult": 1.8}],
    "STRONG":      [{"pct": 50,  "atr_mult": 1.8}, {"pct": 50,  "atr_mult": 3.0}],
    "VERY STRONG": [{"pct": 45,  "atr_mult": 2.0}, {"pct": 35,  "atr_mult": 3.5}, {"pct": 20, "atr_mult": 5.0}],
    "ELITE":       [{"pct": 40,  "atr_mult": 2.0}, {"pct": 40,  "atr_mult": 3.5}, {"pct": 20, "atr_mult": 5.5}],
}

# 15분봉 추가전략(EMA눌림목/마이크로돌파/BB스퀴즈)은 방향은 맞아도 짧게 되돌리는 경우가 많다.
# TP1을 빠르게 당겨 수익을 잠그고, 이후 잔량만 추세 연장에 맡긴다.
FAST_TP_TF = {"15m"}
# 2026-07-11: TP1 비중 소폭 축소·러너 확대 (15m 얕은 승리 개선)
FAST_TP_BY_STRENGTH = {
    "STRONG":      [{"pct": 45, "atr_mult": 1.0}, {"pct": 30, "atr_mult": 1.6}, {"pct": 25, "atr_mult": 2.4}],
    "VERY STRONG": [{"pct": 40, "atr_mult": 1.0}, {"pct": 35, "atr_mult": 1.8}, {"pct": 25, "atr_mult": 2.8}],
    "ELITE":       [{"pct": 35, "atr_mult": 1.1}, {"pct": 40, "atr_mult": 2.0}, {"pct": 25, "atr_mult": 3.0}],
}
FAST_TP1_MIN_RR = 0.65
FAST_EXIT_MIN_BEST_RR = 0.70

# TP는 ATR 목표가를 기본으로 하되, 피봇 손절이 멀어져 R:R이 무너지는 경우
# 최소 R-multiple 목표가를 같이 적용한다. 손절을 억지로 좁히지 않고,
# 수익 목표가 손실위험 대비 충분히 커지게 만드는 장치다.
TARGET_RR_FLOOR_ENABLED = True
TP_RR_FLOOR_BY_STRENGTH = {
    "MODERATE":    [1.20],
    "STRONG":      [1.20, 2.00],
    "VERY STRONG": [1.20, 2.00, 3.00],
    "ELITE":       [1.20, 2.20, 3.20],
}
FAST_TP_RR_FLOOR_BY_STRENGTH = {
    "STRONG":      [1.20, 1.70, 2.30],
    "VERY STRONG": [1.00, 1.60, 2.40],
    "ELITE":       [1.00, 1.80, 2.80],
}
ASYMMETRIC_TP_RR_FLOOR_BY_STRENGTH = {
    "STRONG":      [1.20, 2.20, 3.50],
    "VERY STRONG": [1.20, 2.60, 4.50],
    "ELITE":       [1.20, 3.00, 5.50],
}

# 활발한 자동매매 모드: 손실 학습이 진입 빈도 자체를 말려 죽이지 않도록 상한을 둔다.
ACTIVE_MAX_MIN_RR = 1.35
ACTIVE_FAST_MIN_RR = 0.70
ACTIVE_MAX_MIN_VOL = 1.25
ACTIVE_HIGH_VOL = 3.0
ACTIVE_ULTRA_VOL = ACTIVE_HIGH_VOL * 3

# ─── 수익률/시드 하한 게이트 ───────────────────────────────────────────────
# 자동매매는 "가격 변동률"보다 "레버리지 포함 증거금 ROI"가 실제 체감 수익률이다.
# 왕복 수수료 차감 후 전체 TP 계획의 가중 증거금 ROI가 10% 미만이면,
# 수익보다 노이즈/수수료에 먹힐 가능성이 높다고 보고 실거래를 보류한다.
MIN_EXPECTED_MARGIN_ROI_PCT = 10.0
MIN_TP1_MARGIN_ROI_PCT = 4.0

# 좋은 신호가 "기본 추천 레버리지 기준 ROI 부족" 하나로만 막히지 않도록,
# 목표가의 순가격수익률이 충분하면 이 상한 안에서 필요한 최소 레버리지로 보정한다.
ROI_LEVERAGE_RESCUE_ENABLED = True
ROI_LEVERAGE_RESCUE_MAX = 30
ROI_RESCUE_MIN_TP1_RR = 1.0
ROI_RESCUE_MIN_BEST_RR = 1.4

# 실거래 1회당 최소 투입 증거금. 단, 이 하한을 맞추면 일손실 한도를 넘는 경우는 진입하지 않는다.
# 소액 계좌에서 $20 하한이 계좌의 31%를 강제 투입해 리스크 과대가 발생하던 문제 수정.
MIN_TRADE_MARGIN_USD = 8.0
MIN_TRADE_MARGIN_MAX_BALANCE_PCT = 0.12  # 2026-07-06: 0.25 → 0.12 (2026-07-07 원복). 단일 포지션 최대 계좌 12%
# 목표 증거금/일손실 소프트캡 때문에 좋은 자리가 사라지지 않도록 쓰는 축소진입 하한.
# 실제 거래소 최소수량은 주문 직전 calc_qty()가 다시 확인한다.
MIN_FALLBACK_TRADE_MARGIN_USD = 1.0

# ─── Binance 사이징 (2026-07-11 개정) ───────────────────────────────────────
# 구버전: BINANCE_FIXED_MARGIN_USD=$100 이 위험기반 사이징을 통째로 덮어씀.
# 실측(07/10): DD 16.9% + risk×0.25 상태에서도 BTC SHORT에 $100×19x 강제 →
# 단건 -$34. 알트 와이드SL(20~37%)도 고정 $100과 겹쳐 한 방 -$60~.
# 개정: 고정 override 비활성(0). 위험기반 사이징 유지 + 단건 증거금/최악손실 상한만.
# 0 또는 None = 고정 override 없음.
BINANCE_FIXED_MARGIN_USD = 0.0
BINANCE_FIXED_MARGIN_DOUBLE_AT = 200.0  # 레거시 참고값(자동 동작 없음)
# 단건 증거금 상한(위험기반 결과와 min). 소액 재가동 단계 보수값.
BINANCE_MAX_MARGIN_USD = 40.0
# 단건 최악손실(est SL) ≤ equity × 이 비율. 넘으면 사이즈 축소, 최소마진 이하면 차단.
BINANCE_MAX_TRADE_SL_LOSS_PCT = 0.010  # 1.0%

# 확신도 높은 자리는 "맞췄는데 수익금이 너무 작음"을 막기 위해 증거금 하한을 따로 둔다.
# 목표 증거금 = max(고정 USD, 잔고 비율). 단, 일손실/DD 하드스톱은 그대로 유지한다.
CONVICTION_SIZING_ENABLED = True
CONVICTION_MARGIN_USD_BY_TIER = {
    "BASE": 8.0,
    "STRONG": 12.0,
    "VERY STRONG": 20.0,
    "ELITE": 20.0,
    "GOLDEN": 25.0,
}
# 2026-07-06 긴급 하향: VERY STRONG/ELITE 신호마다 계좌의 20%를 강제베팅하던 게
# 오버사이징 근본원인(넓은 SL 20~27%와 겹쳐 단건위험 4%+로 폭증). 하한 대폭 축소.
# 2026-07-07: 무단 되돌리기 발견 후 원복.
CONVICTION_MARGIN_PCT_BY_TIER = {
    "BASE": 0.08,
    "STRONG": 0.12,
    "VERY STRONG": 0.08,
    "ELITE": 0.08,
    "GOLDEN": 0.10,
}

# $20 이상 시드 상향은 기대ROI만으로 결정하지 않는다.
# 레버리지는 수익과 손실을 동시에 키우므로, TP1/최대 R:R이 낮은 자리는
# "작게 들어가야 할 자리"가 아니라 "실거래 제외"로 처리한다.
SIZING_MIN_TP1_RR_BY_TIER = {
    "BASE": 1.20,
    "STRONG": 1.20,
    "VERY STRONG": 1.00,
    "ELITE": 0.90,
    "GOLDEN": 0.80,
    "BTC-MACRO": 1.00,
}
SIZING_MIN_BEST_RR_BY_TIER = {
    "BASE": 2.00,
    "STRONG": 1.80,
    "VERY STRONG": 1.50,
    "ELITE": 1.30,
    "GOLDEN": 1.10,
    "BTC-MACRO": 1.50,
}

# 고기대수익 기회 예외.
# 의미: 일손실 한도는 기본적으로 지키되, ROI가 큰 고확신 자리는
# "손실이 무서워서 진입 자체를 막는" 대신 계좌위험 하드캡 안에서 축소/예외 진입한다.
HIGH_OPPORTUNITY_MIN_MARGIN_ROI_PCT = 18.0
HIGH_OPPORTUNITY_MIN_TP1_MARGIN_ROI_PCT = 7.0
# 2026-07-03 재검토: 레버리지 ROI%만 보면 "고기대수익"으로 보이지만 실제 TP1
# R:R은 낮은 자리가 낀다 (SHIB1000: 레버 8x로 증거금ROI 22.1% 찍었지만 TP1 R:R
# 1:1.0 — 사실상 동전던지기). 승률이 자산인 화이트리스트 전략을 죽이지 않으려면
# ROI%가 아니라 실제 손익비로 "진짜 좋은 자리"를 걸러야 한다.
HIGH_OPPORTUNITY_MIN_TP1_RR = 1.30
# 2026-07-03 리뷰: 0.10 → 0.05. 예외 진입 1건의 계좌위험이 하루 손실예산(5%)을
# 넘지 못하게 정합화. PYTH 사례에서 하드캡 $10.42(≈16%)로 -$2.95 단일손실 발생.
# 2026-07-06 긴급 하향: 5.0% → 1.8%. 고기대수익 예외조차 단건 5% 위험은 과다.
# MAX_ACCOUNT_RISK_PCT(1.5%)보다 약간만 높게 둬 예외 자리도 실계좌 생존 범위로 제한.
# 2026-07-07: 무단 되돌리기 발견 후 원복.
HIGH_OPPORTUNITY_MAX_ACCOUNT_RISK_PCT = 0.0180
# 2026-07-03 3차 수정: 당일 손실액으로 고기대수익 예외를 막는 임계값
# (구 HIGH_OPPORTUNITY_DAILY_LOSS_DISABLE_AT)을 아예 제거했다.
# 손실은 손실이고 좋은 자리는 그대로 — 당일 손실과 무관하게 TP1 R:R 하한을
# 통과한 자리는 시도해서 멘징/수익전환 기회를 준다. 계좌 자체가 위험한
# 상태(DD/하드스톱)만 아래에서 전면 차단하고, 단건 리스크는
# HIGH_OPPORTUNITY_MAX_ACCOUNT_RISK_PCT로 계속 캡핑한다.
# 계좌 DD가 이 비율 이상이면 고기대수익 예외 차단 (0.50=50%로 너무 관대 → 0.15로 수정)
HIGH_OPPORTUNITY_DD_DISABLE_PCT = 0.15

# ─── 초고수익률 시드 극대화 모드 ─────────────────────────────────────────────
# TAIKO/ZBT처럼 TP 수익률은 매우 크지만 SL폭도 큰 종목은 고레버리지로 들어가면
# 손절 위험 때문에 증거금이 오히려 작아진다. 이런 자리는 레버리지를 낮춰
# 같은 계좌위험 안에서 증거금을 크게 쓰는 방식이 더 효율적이다.
# 2026-07-03 리뷰: True → False. 실측 9건 승률 22% (2승 7패), 순손익 -$9.03, EV -$1.00/건.
# 타이트SL(최소 2.5%)이 구조적 무효화 지점보다 안쪽에 놓여 조기 손절 반복
# (NOM -2.09, BIRB -1.73, TAIKO -1.96, PYTH -2.95 등). 재활성화하려면
# 15건 이상 표본에서 EV 양수 입증 필요.
PROFIT_SURGE_SIZING_ENABLED = False
PROFIT_SURGE_MIN_MARGIN_ROI_PCT = 80.0
PROFIT_SURGE_MIN_TP1_MARGIN_ROI_PCT = 35.0
PROFIT_SURGE_MIN_CONFIRMED = 6
PROFIT_SURGE_MIN_VOL = ACTIVE_HIGH_VOL
PROFIT_SURGE_MIN_TP1_RR = 1.0
PROFIT_SURGE_MIN_BEST_RR = 2.4
PROFIT_SURGE_MAX_ACCOUNT_RISK_PCT = 0.2000
PROFIT_SURGE_TARGET_MARGIN_PCT = 1.00
# 초고수익률 자리는 기존 피봇 SL이 지나치게 멀면 증거금이 작아진다.
# TP는 유지하고 SL만 ATR/타임프레임 기준으로 당겨서 "틀리면 빠르게 인정"한다.
# 2026-07-03 리뷰: 시드극대화와 함께 비활성화 (동일 근거 — 22% 승률 코호트).
PROFIT_SURGE_TIGHT_STOP_ENABLED = False
PROFIT_SURGE_STOP_MAX_PCT_BY_TF = {
    "15m": 0.075,
    "1h":  0.090,
    "4h":  0.120,
    "1d":  0.150,
}
PROFIT_SURGE_STOP_ATR_MULT = 1.0
PROFIT_SURGE_STOP_MIN_PCT = 0.025
# 너무 낮은 레버리지(1~3x)는 수익 속도가 둔해져 사용자의 복리 목표와 맞지 않는다.
# 초고수익률 모드는 최소 5x를 유지하고, 위험캡을 넘으면 증거금을 자동 축소한다.
PROFIT_SURGE_MIN_LEVERAGE = 5

# 후보 사후승률이 충분히 좋은 조합은 레버리지를 단계적으로 높인다.
WINRATE_LEVERAGE_ENABLED = True
WINRATE_LEVERAGE_MIN_SAMPLES = 5
WINRATE_LEVERAGE_GOOD_WR = 0.60
WINRATE_LEVERAGE_GREAT_WR = 0.70
WINRATE_LEVERAGE_EXCELLENT_WR = 0.80
WINRATE_LEVERAGE_GOOD_EDGE = 0.20
WINRATE_LEVERAGE_GREAT_EDGE = 0.50
WINRATE_LEVERAGE_EXCELLENT_EDGE = 0.80
WINRATE_LEVERAGE_GOOD_MULT = 1.15
WINRATE_LEVERAGE_GREAT_MULT = 1.30
WINRATE_LEVERAGE_EXCELLENT_MULT = 1.50
WINRATE_LEVERAGE_MAX = 30

# 심볼별 최소 주문 수량 / 단위 (Bybit USDT 영구 선물 기준)
MIN_QTY_MAP  = {
    "BTC/USDT":  0.001, "ETH/USDT":  0.01,
    "SOL/USDT":  0.1,   "XRP/USDT":  1.0,
    "BNB/USDT":  0.01,  "AVAX/USDT": 0.1,
    "LINK/USDT": 0.1,
    "DOGE/USDT": 10.0,  "ADA/USDT":  1.0,
    "DOT/USDT":  0.1,   "SUI/USDT":  0.1,
}
QTY_STEP_MAP = {
    "BTC/USDT":  0.001, "ETH/USDT":  0.01,
    "SOL/USDT":  0.1,   "XRP/USDT":  1.0,
    "BNB/USDT":  0.01,  "AVAX/USDT": 0.1,
    "LINK/USDT": 0.1,
    "DOGE/USDT": 10.0,  "ADA/USDT":  1.0,
    "DOT/USDT":  0.1,   "SUI/USDT":  0.1,
}

# SL — 피봇 저/고점 밖 (ATR 배수)
# 핵심 원칙: SL은 노이즈 레벨 밖에 위치해야 함
# 크립토 ATR 기준 1.5 ATR = 정상적인 가격 진동 바깥 = 다이버전스 무효화 시만 손절
# 0.3 ATR(구버전) = 캔들 꼬리만으로 손절 → 승률 파괴의 근본 원인
SL_ATR_MULT = 1.5

# Initial stop-loss should not be so wide that a normal SL consumes most of the
# position margin.  If `SL price move % * leverage` exceeds this cap, main.py
# compresses leverage before sizing the trade.
INITIAL_SL_MARGIN_ROI_CAP_PCT = 35.0

# ─── 레버리지 추천표 (신호강도 × 타임프레임) ────────────────────────────────
# 100억 프로젝트: 고신뢰도 자리엔 최대 레버리지 투입, 복리로 스케일업
LEVERAGE_MAP = {
    # ── ELITE: 최고 확신도 — 최대 레버리지 ──
    ("ELITE",       "1d"):  20,
    ("ELITE",       "4h"):  20,   # 18 → 20
    ("ELITE",       "1h"):  20,   # 15 → 20
    ("ELITE",       "15m"): 12,   #  8 → 12
    ("ELITE",       "5m"):  8,    #  5 → 8
    # ── VERY STRONG: 강한 신호 — 공격적 레버 ──
    ("VERY STRONG", "1d"):  15,   # 12 → 15
    ("VERY STRONG", "4h"):  15,   # 10 → 15
    ("VERY STRONG", "1h"):  12,   #  7 → 12
    ("VERY STRONG", "15m"): 8,    #  5 → 8
    ("VERY STRONG", "5m"):  5,    #  3 → 5
    # ── STRONG: 조건부 허용 ──
    ("STRONG",      "1d"):  10,   #  7 → 10
    ("STRONG",      "4h"):  8,    #  5 → 8
    ("STRONG",      "1h"):  6,    #  4 → 6
    ("STRONG",      "15m"): 5,    #  3 → 5
    ("STRONG",      "5m"):  3,
    # ── MODERATE: 구조레벨+EMA 조건 시 소액 ──
    ("MODERATE",    "1d"):  6,
    ("MODERATE",    "4h"):  5,
    ("MODERATE",    "1h"):  4,
    ("MODERATE",    "15m"): 3,
    ("MODERATE",    "5m"):  2,
}

# ─── 신호 강도별 증거금 비율 ────────────────────────────────────────────────
# 이 값은 "목표 베팅"이 아니라 강도별 최대 증거금 캡이다.
# 실제 증거금은 아래 RISK_PCT_BY_STRENGTH(계좌 손실위험)와 SL폭으로 계산한다.
# 목적: 좋은 자리는 크게, 애매한 자리는 후보 기록만 남겨 복리 엔진을 오래 살린다.
MARGIN_BY_STRENGTH = {
    "MODERATE":    0.10,   #  7% → 10%
    "STRONG":      0.15,   # 10% → 15%
    "VERY STRONG": 0.25,   # 18% → 25%
    "ELITE":       0.35,   # 25% → 35%
}

# ─── 황금 진입 (ELITE + MTF 전정렬 + EMA 정렬) ───────────────────────────────
# 세 조건 동시 충족 = 최고 확신도 → 최대 베팅
GOLDEN_ENTRY_POSITION_PCT = 0.55   # 45% → 55%
GOLDEN_LEVERAGE_BOOST     = 1.50   # 기본 레버리지 × 1.5
GOLDEN_MAX_LEVERAGE       = 30     # 25 → 30: 황금 진입 레버리지 절대 상한

# MTF 전정렬 "최고 진입 기회"는 3연패 타임락 때문에 놓치지 않는다.
# 단, 일손실/드로우다운 하드스톱은 그대로 유지한다.
PREMIUM_MTF_AUTO_STRENGTHS = {"STRONG", "VERY STRONG", "ELITE"}

# ─── 복리형 리스크 엔진 ─────────────────────────────────────────────────────
# 자동매매는 계좌 위험률 기준으로 수량을 정한다.
# 예: ELITE 2.0% 리스크, 평균 2.5R 실현 = 계좌 +5% 내외.
# 황금진입은 4.0% 리스크까지 허용해 +8~10% 계좌 성장 기회를 만든다.
PAPER_ONLY_STRENGTHS = {"MODERATE"}  # STRONG은 현재봉 기반 고품질 전략만 소액 허용
RISK_PCT_BY_STRENGTH = {
    "STRONG":      0.0060,
    "VERY STRONG": 0.0125,
    "ELITE":       0.0200,
}
SCALP_RISK_MULT         = 0.55
# 2026-07-06 긴급 하향: 단건 계좌위험 하드캡. 기존 4.0~4.5%는 마이크로 계좌($60)
# 복리 기준값 — Binance $1449 실계좌에서 단건 -$60(4%) 손실을 반복 생성, 2일 -17.9% DD.
# 자산운용 표준(0.5~1.5%)에 맞춰 축소. 이 캡이 conviction 하한/high_opportunity를
# 포함한 모든 사이징 경로의 최종 상한이므로 단건 위험을 근본적으로 제한한다.
# 2026-07-07: 무단 되돌리기 발견 후 원복(검증된 최종값).
GOLDEN_ENTRY_RISK_PCT   = 0.0150
MAX_ACCOUNT_RISK_PCT    = 0.0150
MAX_DAILY_LOSS_PCT      = 0.0500
AUTO_TRADE_DIAGNOSTICS  = True
CANDIDATE_LOG_FILE      = "trade_candidates.jsonl"
EXECUTION_JOURNAL_FILE  = "trade_execution_journal.jsonl"

# ─── 후보 신호 사후성과 기반 퀀트 필터 ─────────────────────────────────────
# 실제 체결 여부와 무관하게 우리가 낸 후보 신호의 20봉 이후 MFE/MAE를 학습한다.
SIGNAL_QUALITY_LOOKBACK_DAYS = 3
SIGNAL_QUALITY_HORIZON = "20"
SIGNAL_QUALITY_MIN_EXACT = 5
SIGNAL_QUALITY_MIN_STRATEGY = 8
SIGNAL_QUALITY_MIN_TF = 20
SIGNAL_QUALITY_BAD_WR = 0.35
SIGNAL_QUALITY_WEAK_WR = 0.45
SIGNAL_QUALITY_GOOD_WR = 0.58
SIGNAL_QUALITY_BAD_EDGE = -0.25
SIGNAL_QUALITY_GOOD_EDGE = 0.25
SIGNAL_QUALITY_WEAK_MULT = 0.65
SIGNAL_QUALITY_GOOD_MULT = 1.12

# ─── 비대칭 손익비 모드 ─────────────────────────────────────────────────────
# 승률이 낮아도 평균 손실 대비 평균 수익폭이 큰 신호군은 별도 러너형 TP로 운용한다.
ASYMMETRIC_MIN_SAMPLES = 5
ASYMMETRIC_MIN_PAYOFF = 2.0
ASYMMETRIC_MIN_AVG_WIN = 1.2
ASYMMETRIC_MIN_MFE_MAE = 1.4
ASYMMETRIC_MIN_EDGE_FLOOR = -0.15
ASYMMETRIC_RISK_MULT = 1.08
ASYMMETRIC_FUNDING_OVERRIDE_MULT = 0.70
ASYMMETRIC_TIMING_OVERRIDE_MULT = 0.70

# ─── 과열도(VWAP 이격) 하드 차단 ────────────────────────────────────────────
# 2026-07-07: 실측 손실사례(US/USDT, EMA눌림목+거래량급등, vol11.44x, 비대칭러너
# 모드) — 진입 시점 1h VWAP 대비 +26.8% 이격(정상 눌림목 허용치 0.6%의 40배+)인데도
# asymmetric_mode/고거래량 예외가 VWAP추격 하드차단을 우회해 리스크×0.70 소프트
# 오버라이드로 그대로 진입, -$21 손실. "눌림목"은 초입 되돌림 진입이어야지 이미
# 다 오른 뒤(블로우오프 탑 직전)의 되돌림이면 안 된다. 이 한도(%) 이상 이격되면
# asymmetric_mode/고확신 등급과 무관하게 무조건 차단한다 — 봇이 3~5분마다
# 스캔하므로 건강한 초입 눌림목은 놓치지 않고 곧 다시 잡힌다.
EXTENSION_HARD_BLOCK_PCT = 8.0

# ─── 진입 품질 하드 게이트 (2026-07-11) ─────────────────────────────────────
# 와이드 SL + 고정/고증거금 조합이 Binance에서 대형 단건 손실(HMSTR -67, CHIP -66,
# US -21 등)을 반복. SL% 가 이 한도를 넘으면 타이트SL 예외 없이 진입 차단.
MAX_ENTRY_SL_PCT = 10.0
# 히든 다이버전스 신선도: 5봉 전 진입(BTC 07/10 -$34) 재발 방지.
HIDDEN_LIVE_MAX_BARS_AGO = 3
# TradFi/특수 상품 — 계정 agreement 미서명 시 -4411, 일반 선물 전략과 분리.
ENTRY_SYMBOL_BLOCKLIST = {
    "XAU/USDT", "XAG/USDT", "XPT/USDT", "XPD/USDT",
}
# 스캘핑/RSI2: 하위TF 보조봉 "강한 역방향" 또는 타이밍 실패 시 soft×0.70 금지.
# 07/10 XRP RSI2: 5m 강한역방향인데 soft override 로 진입 → SL.
SCALP_TIMING_HARD_BLOCK_SIGNAL_TYPES = {
    "rsi2_reversion_long", "rsi2_reversion_short",
    "vwap_reversion_long", "vwap_reversion_short",
    "micro_breakout_long", "micro_breakout_short",
}
# 관찰모드(다이버전스 일반/파라볼릭/스캘핑3종) 라이브 사이즈 0 = paper_only.
# 표본 축적은 후보로그로, 실주문은 EV 검증 후 OBSERVATION_MODE_PAPER_ONLY=False 로 해제.
OBSERVATION_MODE_PAPER_ONLY = True

# ─── 스캘핑 복리 모드 ────────────────────────────────────────────────────────
# 2026-07-07 사용자 요청: 방향판단은 가장 신뢰도 높은 신호(EMA눌림목+거래량급등 계열,
# 오늘 검증 승률 63.6%+)에 맡기고, 포지션은 길게 안 들고 빠르게 TP1 위주로 확정해서
# 복리 회전을 빠르게 한다. SL은 임의 %로 조이지 않고 기존 ATR 기반 그대로 사용
# (오늘 확인: %/레버리지 기반 타이트 SL·래칫은 캔들 노이즈에 취약해 효과 불확실했음).
# 사이징은 기존 %기반 그대로 유지 — 잔고 성장에 따라 자동 복리 반영(별도 장치 불필요).
SCALP_COMPOUND_ENABLED = True
SCALP_COMPOUND_TF = {"15m"}
SCALP_COMPOUND_STRATEGIES = {
    "EMA눌림목+거래량급등", "EMA눌림목+돌파", "EMA눌림목+거래량급등+돌파",
}
# 2026-07-11: 70→55. TP1에 너무 많이 실어 잔량 BE 청산 시 전체 승리가 얕아짐
# (실측 부분익절 후 잔량 보호청산 다수). 55% 확정 + 45% 러너로 기대값 개선.
SCALP_COMPOUND_TP1_PCT = 55

ASYMMETRIC_TF = {"15m", "1h", "4h", "1d"}
ASYMMETRIC_TP_BY_STRENGTH = {
    "STRONG":      [{"pct": 35, "atr_mult": 1.4}, {"pct": 30, "atr_mult": 3.0}, {"pct": 35, "atr_mult": 5.0}],
    "VERY STRONG": [{"pct": 30, "atr_mult": 1.6}, {"pct": 30, "atr_mult": 3.8}, {"pct": 40, "atr_mult": 7.0}],
    "ELITE":       [{"pct": 25, "atr_mult": 1.8}, {"pct": 30, "atr_mult": 4.5}, {"pct": 45, "atr_mult": 9.0}],
}

# ─── 계좌 드로우다운 방어 ───────────────────────────────────────────────────
# 손실 구간에서는 "학습"보다 생존이 우선이다.
# ACCOUNT_START_BALANCE(.env)가 있으면 그 값을 기준으로, 없으면 관측된 최고 equity를 기준으로 방어한다.
DRAWDOWN_WARN_PCT       = 0.08
DRAWDOWN_RISK_OFF_PCT   = 0.12
DRAWDOWN_HARD_STOP_PCT  = 0.18
DRAWDOWN_PAUSE_HOURS    = 6
DRAWDOWN_RISK_MULT      = 0.25
# True면 DD 하드스톱에서 신규매매를 멈춘다.
# 25.89% DD에서도 계속 진입해 손실이 눈덩이처럼 커지던 문제 수정 → True로 변경.
DRAWDOWN_HARD_STOP_BLOCK_NEW_TRADES = True

# ─── 손실 학습 모드 ─────────────────────────────────────────────────────────
# 실제 체결 결과를 다음 진입에 반영한다. 손실난 자리는 같은 조건 반복을 막고,
# 수익이 누적된 자리는 리스크/시드 확대 근거로 사용한다.
MIN_DYNAMIC_RISK_MULT = 0.35
SYMBOL_STRATEGY_DAILY_LOSS_LIMIT = 2
# 전략 불문 동일 심볼 당일 총 손실 횟수 한도. DYDX 6회·TAIKO 6회 반복진입 방지.
SYMBOL_DAILY_TOTAL_LOSS_LIMIT = 2
# 연패 심볼 쿨다운 시간 (시간 단위)
SYMBOL_COOLDOWN_HOURS = 8
ASYMMETRIC_SYMBOL_DAILY_LOSS_LIMIT = 1

# ─── 전략 화이트리스트 & 방향 차단 ──────────────────────────────────────────
# 실거래 허용 전략 집합. 빈 set = 모든 전략 허용. 승률 기반 선별.
# 성과 있는 전략: EMA눌림목+거래량급등 (67%, +$5.79), EMA눌림목+돌파 (75%, +$1.06)
# 배제된 전략: BTC Sync Momentum (38%, -$10.27), 돌파 (0%), 거래량급등추세 (0%)
AUTO_TRADE_STRATEGY_WHITELIST: set = {
    # 2026-07-03 리뷰: 순수 "EMA눌림목"(거래량/돌파 확인 없음) 제거.
    # 실측 5건 전부 SHORT, EV -$0.544/건 (BTC -3.13 포함, 순손익 -$2.72).
    # 거래량급등/돌파 확인이 붙은 조합만 실거래 유지 (확인 조합이 승률 원천).
    "EMA눌림목+거래량급등",
    "EMA눌림목+돌파",
    "EMA눌림목+거래량급등+돌파",
    "hidden_bullish",
    "hidden_bearish",  # 3건 -$2.67 — 표본 부족으로 유지하되 관찰 대상
    # 2026-07-07: 일반(non-hidden) 다이버전스 편입. 사용자가 다이버전스를 선행지표로
    # 신뢰하나 실거래 표본이 8건(대부분 hidden)뿐이라 EV 미검증 → 표본 축적 목적으로
    # 실거래 허용하되 아래 DIVERGENCE_GENERAL_OBSERVATION_RISK_MULT로 소액 관찰모드 진입.
    # divergence.py _check_bullish/_check_bearish가 signal_type="bullish"/"bearish"로 생성.
    "bullish",
    "bearish",
    # 2026-07-08: 파라볼릭 급등-반전 사이클 신규 전략(관찰모드). 리서치 근거:
    # Bybit 거래대금 상위 45종목 최근 3.5일 1h OHLCV 스캔 → 7개 실측 사례
    # (BLUR +79%/-27%, VANRY +204%/-59%, YFI +58%/-30%, EDGE +73%, TLM +96%,
    # OPG +52%, LIT +31%). 기존 EMA눌림목/돌파가 파라볼릭 움직임을 추세미달/돌파
    # 불일치로 반복 차단해 하나도 못 잡음 → 별도 전략 필요. 초입(저이격) 롱 점화 +
    # 고점(고이격) 숏 반전을 한 패밀리로 포착. 완전 미검증 → 아래
    # PARABOLIC_OBSERVATION_RISK_MULT(0.30) 소액 관찰모드로 표본만 축적.
    "파라볼릭점화",
    "파라볼릭반전",
    # 2026-07-08: 스캘핑/단타 고빈도 회전 강화(사용자 요청 "계속 매매") 3종 편입.
    #   1) 마이크로돌파: 반사실 검증 근거 있음. 화이트리스트만 통과못한 순수표본
    #      (모든 게이트 통과, whitelist에서만 차단)으로 ccxt 공개 OHLCV 반사실 시뮬
    #      (SL1.5ATR/TP1.8ATR 선접촉) → 37종목 분산, 시간당중복제거 독립이벤트
    #      LONG n=21 WR52% avgR+0.15 / SHORT n=33 WR50% avgR+0.10 로 양방향 EV양수.
    #      고빈도(15m 중심)라 "계속 매매" 취지 부합. 관찰모드 0.30x.
    #   2) VWAP회귀: 문헌(VWAP 평균회귀 스캘핑) 격차 — 기존 VWAP는 과열필터로만 썼음.
    #      실거래 반사실 표본 0건 → 완전 미검증, 더 보수적 0.25x.
    #   3) RSI2반전: 문헌(Connors RSI(2) 초단기 평균회귀) 격차 — 기존 RSI반전은
    #      RSI(14) 28/72로 더 느림. 완전 미검증 → 0.25x.
    # 셋 다 기존 게이트(추세/과열/포트폴리오캡/5m단독금지/VERY STRONG만 라이브/SHORT
    # 정책) 전부 상속 — 화이트리스트 진입 여부만 다름.
    "마이크로돌파",
    "VWAP회귀",
    "RSI2반전",
}
# SHORT 방향 전체 차단은 하지 않는다.
# EMA눌림목(하락), hidden_bearish 등 EMA 계열 숏 신호는 정상 실거래.
# 나쁜 숏 전략(BTC Macro Short, 돌파 숏 등)은 이미 화이트리스트로 차단됨.
BLOCK_SHORT_AUTO_TRADE = False

# ─── 비-EMA(거래량급등) SHORT 강등 ──────────────────────────────────────────
# 2026-07-06 진단: live SHORT을 방향+전략으로 분해하니 순수 EMA눌림목 계열 SHORT
# (EMA눌림목/EMA눌림목+돌파, core "EMA 눌림목")은 10건 70% WR로 정상인데, "거래량급등"
# 조합 SHORT(EMA눌림목+거래량급등[+돌파], core "거래량 급등 추세")은 SHORT일 때만
# EV음수(승률 25~43%, 순손익 마이너스)였다. 같은 core 전략도 LONG은 최고성과(65.6%)라
# 방향 무관 차단은 부적절 → SHORT 방향의 거래량급등 조합만 소액화한다.
# 하드차단 아님(BLOCK_SHORT_AUTO_TRADE=False 철학 유지) + 표본<20이라 신중히 소프트 강도.
# 장세게이트 배율에 곱해져 최종 SHORT 리스크를 추가로 낮춘다. 1.0=강등해제.
SHORT_NON_EMA_RISK_MULT = 0.40

# ─── SHORT 기대값 방어 (2026-07-11) ──────────────────────────────────────────
# Bybit 체결 실측: SHORT n=46 WR 45.7% 이지만 평균익 +0.57 / 평균손 -1.50
# → 실현 R:R 0.38, 누적 -$25.6 (LONG은 WR 58%·순익 거의 0). 승률 문제가 아니라
# 숏 구조(조기익절 vs 풀SL) + 느슨한 진입이 기대값을 깎음.
# 전면 SHORT 금지는 하지 않고(BLOCK_SHORT=False), 품질 게이트 + 전역 사이즈 감액.
SHORT_STRICT_GATES_ENABLED = True
SHORT_REQUIRE_MTF_ALIGNED = True   # mtf_boost > 1.0 (상위봉 전정렬) 필수
SHORT_REQUIRE_EMA_ALIGNED = True   # EMA 방향 일치 필수
SHORT_GLOBAL_RISK_MULT = 0.50      # 라이브 SHORT 전역 리스크 배율 (2026-07-11: 0.55→0.50)
# 15m SHORT는 EMA 확인 조합만 (히든/역추세 15m 숏 제외)
SHORT_15M_STRATEGY_WHITELIST = {
    "EMA눌림목+돌파",
    "EMA눌림목+거래량급등",
    "EMA눌림목+거래량급등+돌파",
}

# ─── 15m 실거래 전략 제한 (2026-07-11) ───────────────────────────────────────
# 15m n=80 WR 52.5% 인데 누적 -$12.8 — 빈도 높은 노이즈 TF. 양의 엣지가 확인된
# EMA 계열만 라이브, 히든/다이버전스/기타는 1h+ 또는 paper.
LIVE_15M_STRATEGIES = {
    "EMA눌림목+거래량급등",
    "EMA눌림목+돌파",
    "EMA눌림목+거래량급등+돌파",
}

# ─── EMA 전략 MACD 히스토그램 soft 필터 (2026-07-11) ─────────────────────────
# 다이버전스 경로에는 MACD가 이미 포함. EMA 단타(_base_signal)는 macd ok=False 고정
# 이었음. 크로스 필수(후행·신호급감) 대신 히스토그램 부호/기울기 정렬을 soft 적용.
# HARD_BLOCK=False: 미정렬 시 리스크×SOFT_MULT 만. True면 진입 차단.
EMA_MACD_FILTER_ENABLED = True
EMA_MACD_SOFT_RISK_MULT = 0.70
EMA_MACD_HARD_BLOCK = False

# ─── 레짐 라우터 (Principles P1, 2026-07-11) ─────────────────────────────────
# 시장 국면(trend/range/high_vol/mixed)에 따라 전략 허용·사이즈를 분기한다.
# 새 오실레이터 추가 없이 고수 공통 원칙("국면이 다르다")을 시스템화.
REGIME_ROUTER_ENABLED = True
REGIME_ADX_TREND = 22.0          # ADX ≥ 이 값 + EMA 기울기 → trend
REGIME_ADX_RANGE = 18.0          # ADX ≤ 이 값 + 낮은 ATR%ile → range
REGIME_ATR_PCTILE_HIGH = 85.0    # ATR% 백분위 ≥ → high_vol
REGIME_ATR_PCTILE_RANGE_MAX = 50.0
REGIME_EMA_SLOPE_TREND_PCT = 0.15  # EMA20 5봉 기울기 |%| 하한
REGIME_RANGE_EMA_RISK_MULT = 0.50  # 횡보에서 EMA 휩쏘 방어
REGIME_HIGH_VOL_RISK_MULT = 0.65
REGIME_MIXED_RISK_MULT = 0.85
REGIME_RANGE_BLOCK_HIDDEN = True   # 횡보에서 hidden continuation paper
REGIME_HIGH_VOL_BLOCK_MEANREV = True

# 실거래 A/B 귀속 태그 (journal/history에 남겨 "기존 vs 신규 스택" 구분)
# 2026-07-11 이후 진입은 이 버전 문자열로 묶어서 복기한다.
LOGIC_STACK_VERSION = "2026-07-11-v2"

# ─── 일반 다이버전스 관찰모드 사이징 ────────────────────────────────────────
# 2026-07-07: 일반(non-hidden) bullish/bearish 다이버전스를 화이트리스트에 편입하되,
# 실거래 표본이 없어 EV 미검증이므로 검증된 전략처럼 정상 사이징하지 않고 소액으로
# "관찰모드" 진입해 표본만 축적한다(hidden 8건 코호트와 동급의 미검증 취급).
# hidden_bullish/hidden_bearish는 대상 아님 — 기존 정상 사이징 유지.
# 방향 무관(bullish=LONG, bearish=SHORT) 적용. 표본 20건+ 쌓이면 EV 재평가 후 배율 상향/제거 결정.
DIVERGENCE_GENERAL_OBSERVATION_RISK_MULT = 0.35

# ─── 파라볼릭 급등-반전 사이클 전략 (관찰모드, 2026-07-08 신설) ──────────────
# 리서치: Bybit 선물 거래대금 상위 45종목 최근 3.5일 1h OHLCV 스캔에서
# "24~48h내 +30%↑ 급등 → 고점형성 → 10%↑ 되돌림" 7개 실측 사례 수집
# (BLUR/VANRY/YFI/EDGE/TLM/OPG/LIT). 공통패턴 실측치:
#   · 점화(초입)바: 거래량 3.1~9.3x, 실체비율 0.65~0.98, 봉상승 +2.5~11.8%,
#     VWAP(24h)이격 +2.9~7.0% → 전부 EXTENSION_HARD_BLOCK(8%) 미만이라 자연 호환.
#   · 고점/반전바: 최근 블로우오프 고점의 VWAP이격 +9~55%(초입의 3~8배),
#     RSI 66~96, 상단꼬리 0.22~0.81, 거래량 클라이맥스 후 감소, 직전봉 저가이탈 음봉.
#     (반전확정 바에서는 가격이 이미 고점서 내려와 현재봉 이격이 낮으므로 이격/RSI는
#      최근 RECENT봉 블로우오프 고점 기준으로 측정 — 실측 반영한 보정.)
# 초입은 이격≤6%에서만, 반전은 이격≥12%에서만 발화 → 상호배타(동일심볼 초입롱→
# 고점숏 사이클이 겹치지 않음). 기존 EMA눌림목/돌파가 못 잡는 파라볼릭 구간 전용.
# 미검증 신규전략 → main.py에서 관찰모드 소액 사이징(0.30x)으로만 진입.
# 트레일링: 이번 관찰 시작 단계에선 전용 배선 없이 기존 전역 트레일(TRAIL_ATR_MULT)을
# 그대로 쓴다. 파라볼릭 전용 ATR 트레일 배선은 표본 축적 후 별도 승인받아 진행.
PARABOLIC_CYCLE_ENABLED            = True
# 2026-07-09: 1h → {1h, 15m} 확장. 실측 사례(OGN/USDT, 2026-07-09 04시대):
# +30%+ 급등이 단일 1h봉 안에서 전부 발생(거래량 40~90배 압축 폭발) — 1h는 봉
# 마감을 기다려야 평가되므로 이미 과열구간으로 넘어간 뒤에야 반응해 초입을 놓침.
# 15m으로 실제 재현 검증: 04:15(펌프 시작 15분 만에) 이격 +3.6%에서 정확히 점화
# 발화 확인(1h로는 05:00 봉마감까지 대기해야 했음). 5m은 여전히 실거래 제외
# (TIMING_ONLY_TF 전역 규칙 상속 — 노이즈 검증은 오늘 다른 전략들에서 이미 확립됨).
PARABOLIC_CYCLE_TF: set            = {"1h", "15m"}
PARABOLIC_IGNITION_MIN_VOL         = 3.0      # 점화바 최소 거래량 배수(실측 하한 3.1x)
PARABOLIC_IGNITION_MIN_BODY_RATIO  = 0.60     # 실체/전체범위 비율(실측 0.65~0.98)
PARABOLIC_IGNITION_MIN_BAR_GAIN    = 2.5      # 점화바 최소 상승률 %(실측 2.5~11.8)
PARABOLIC_IGNITION_MAX_VWAP_DISLOC = 6.0      # 초입 이격 상한 %(실측 2.9~7.0, <8 하드차단과 호환)
PARABOLIC_REVERSAL_MIN_VWAP_DISLOC = 12.0     # 반전 이격 하한 %(초입 6%와 상호배타)
PARABOLIC_REVERSAL_MIN_RSI         = 72       # 반전 과매수 하한(실측 고점 66~96)
PARABOLIC_REVERSAL_LOOKBACK        = 48       # 급등이력 확인 봉수(1h → 48h)
PARABOLIC_REVERSAL_MIN_PUMP_PCT    = 30       # 반전 전 최소 급등폭 %(고점 형성 확인)
PARABOLIC_REVERSAL_MIN_UPWICK      = 0.35     # 고점봉 상단꼬리 비율(실측 0.22~0.81)
PARABOLIC_REVERSAL_RECENT          = 6        # 반전 직전 블로우오프 고점 탐색 봉수
PARABOLIC_TP_SCHEME                = [        # 러너형 TP(ATR배수, 물량%) — 추세 최대추출
    {"pct": 30, "atr_mult": 2.0},
    {"pct": 30, "atr_mult": 5.0},
    {"pct": 40, "atr_mult": 9.0},
]
PARABOLIC_OBSERVATION_RISK_MULT    = 0.30     # 관찰모드 리스크배율(다이버전스 0.35보다 보수적)
# 실측검증(2026-07-08): SHORT_NON_EMA_RISK_MULT(0.40)는 적용조건이
# `direction=="SHORT" and "거래량급등" in strategy`인데 "파라볼릭반전"엔 "거래량급등"
# 문자열이 없어 적용되지 않는다 → 파라볼릭 숏 실효배율은 0.30 (중첩 0.12 아님).

# ─── 스캘핑/단타 고빈도 3종 관찰모드 사이징 (2026-07-08 신설) ─────────────────
# 사용자 요청 "좋은 자리 빠르게 들어가서 빠르게 수익보고 나오는" 고빈도 회전 강화.
# 세 전략 모두 main.py에서 signal_type 기준으로 아래 배율을 장세게이트 배율에 곱한다.
# SHORT_NON_EMA_RISK_MULT(0.40)와의 중첩: 세 전략명 모두 "거래량급등" 문자열이 없어
# 적용조건 미충족 → SHORT여도 중첩 안 됨(실효 = 아래 배율 그대로). 파라볼릭과 동일 구조.
# 마이크로돌파: 반사실 EV양수(LONG+0.15/SHORT+0.10) 근거 있음 → 0.30 (파라볼릭과 동급).
MICRO_BREAKOUT_OBSERVATION_RISK_MULT = 0.30
# VWAP회귀/RSI2반전: 실거래 반사실 표본 0건(문헌 격차 신규 구현) → 더 보수적 0.25.
VWAP_REVERSION_OBSERVATION_RISK_MULT = 0.25
RSI2_REVERSION_OBSERVATION_RISK_MULT = 0.25
# 2026-07-11: OBSERVATION_MODE_PAPER_ONLY=True 이면 위 mult 적용 전에 paper_only 차단.
# mult 값은 paper 해제 후 소액 재가동용으로 유지.

# ─── VWAP 소폭이격 평균회귀 스캘핑 (신규, 2026-07-08) ────────────────────────
# 문헌: 가격이 세션 VWAP에서 일상적 소폭 이탈했다 되돌아오는 것을 노리는 고빈도 스캘핑.
# 기존 calc_vwap(24봉)은 파라볼릭 극단이격(+8%↑)·과열필터로만 쓰였고, "소폭 이탈→회귀"
# 진입은 우리 시스템에 없던 격차. 파라볼릭(이격≥12%)과 이격대가 상호배타(0.6~2.0%)라 충돌 없음.
# TF: 5m/15m만(초단타). 5m은 기존 단독매매 금지 게이트로 라이브 제외 → 15m만 실거래.
# 방향정합: LONG은 ema_t>=0(하락추세 LONG 금지), SHORT은 ema_t<=0.
# TP/SL은 별도 우회 없이 기존 배선 상속 — 15m은 FAST_TP(TP1 1.0ATR)라 설계의도(빠른익절,
# TP1~1ATR) 자연 충족, SL도 기존 SL_ATR_MULT 사용.
VWAP_REVERSION_TF: set          = {"5m", "15m"}
VWAP_REVERSION_MIN_DISLOC       = 0.6    # |VWAP이격| 하한 %(너무 붙으면 엣지 없음)
VWAP_REVERSION_MAX_DISLOC       = 2.0    # |VWAP이격| 상한 %(그 이상은 추세이탈/파라볼릭 영역)
VWAP_REVERSION_RSI_LONG_MIN     = 30     # LONG시 RSI(14) 하한(그 아래는 자유낙하=칼받기 방지)
VWAP_REVERSION_RSI_LONG_MAX     = 50     # LONG시 RSI 상한(회귀 초기만)
VWAP_REVERSION_RSI_SHORT_MIN    = 50     # SHORT시 RSI 하한
VWAP_REVERSION_RSI_SHORT_MAX    = 70     # SHORT시 RSI 상한(그 위는 과열=파라볼릭 영역)
VWAP_REVERSION_MIN_VOL          = 1.0    # 최소 거래량 배수(빈껍데기 회귀 배제, 완만)

# ─── Connors RSI(2) 초단기 평균회귀 (신규, 2026-07-08) ──────────────────────
# 문헌: Larry Connors의 RSI(2) 평균회귀 — 매우 짧은 RSI(2)로 극단(≤10/≥90)에서 빠른 반전
# 포착. 상위 추세와 같은 방향일 때만 진입(추세순응 눌림/반등). 기존 RSI반전은 RSI(14)
# 28/72로 훨씬 느려 이 초단기 엣지를 못 잡던 격차. 빠른 청산 지향.
# TF: 5m/15m만. 5m은 단독매매 금지로 라이브 제외 → 15m만 실거래(15m FAST_TP로 빠른익절).
# 방향정합: LONG은 ema_t>=0에서 rsi2 과매도, SHORT은 ema_t<=0에서 rsi2 과매수.
RSI2_REVERSION_TF: set          = {"5m", "15m"}
RSI2_PERIOD                     = 2      # Connors식 초단기 RSI 룩백
RSI2_LONG_THRESHOLD             = 10     # LONG 진입 rsi2 상한(과매도)
RSI2_SHORT_THRESHOLD            = 90     # SHORT 진입 rsi2 하한(과매수)
RSI2_EXTREME_LONG               = 5      # 이 이하면 VERY STRONG(라이브), 아니면 STRONG(페이퍼)
RSI2_EXTREME_SHORT              = 95     # 이 이상이면 VERY STRONG(라이브)
RSI2_MIN_VOL                    = 1.0    # 최소 거래량 배수(완만)

REALIZED_TRADE_LEARNING_ENABLED = True
REALIZED_BLOCK_EXACT_MIN_TRADES = 2
REALIZED_BLOCK_SYMBOL_MODE_MIN_TRADES = 2
REALIZED_BLOCK_MODE_TF_MIN_TRADES = 5
REALIZED_BLOCK_WIN_RATE = 0.35
REALIZED_BLOCK_MIN_PNL_USD = -0.50
REALIZED_BOOST_MIN_TRADES = 2
REALIZED_BOOST_WIN_RATE = 0.60
REALIZED_BOOST_MIN_PNL_USD = 0.50
REALIZED_BOOST_MULT = 1.25
REALIZED_MODE_BOOST_MULT = 1.12

# STRONG 실거래는 "늦게 뜬 다이버전스"가 아니라 현재봉 기반 전략만 허용한다.
ACTIVE_STRONG_STRATEGIES = {
    "RSI반전", "EMA눌림목", "BB스퀴즈", "마이크로돌파",
    "BB중단내림롱", "거래량급등", "거래량급등추세",
}
STRONG_LIVE_MAX_BARS_AGO = 1
STRONG_LIVE_MIN_VOL      = 1.10

# ─── 포트폴리오 증거금 기반 진입 허용 엔진 ─────────────────────────────────
# 고정 "동시 N개" 한도 대신, 계좌 전체 증거금 사용률과 총 손절 위험으로 신규 진입을 제어한다.
# 좋은 신호를 개수 제한으로 놓치지 않되, 전체 계좌가 한 번에 과도하게 훼손되지 않게 한다.
# 2026-07-06 긴급 하향 (Binance 2일 -17.9% DD 원인= 동시 8포지션 89% 증거금,
# 7/8 롱 알트 상관 → 알트 동반하락에 전량 손절). 동시 노출/상관 쏠림을 근본 축소.
# 2026-07-07: 실패한 서브에이전트 세션이 이 안전장치를 무단으로 되돌렸다가(0.80/0.90/
# 0.25/0.35/0.65/0.75/12/16) 발견 후 즉시 원복함.
# 2026-07-07 재조정: 그 사이 과열도 하드차단(EXTENSION_HARD_BLOCK_PCT)과 비-EMA
# SHORT 강등이 추가돼 "동시 여러 개가 전부 나쁜 자리"일 위험 자체가 줄었고, 스캘핑
# 복리 모드가 회전율을 필요로 해서 완화한다. 원래의 느슨한 값(0.80/12 등)까지는
# 절대 안 돌아간다 — 방향쏠림 캡(상관손실의 핵심 원인)은 가장 보수적으로 유지.
PORTFOLIO_MARGIN_USAGE_CAP = 0.60
PORTFOLIO_MARGIN_USAGE_HIGH_OPPORTUNITY_CAP = 0.70
# 동시 SL 위험 캡: 0.08 → 0.12. 최악의 날에도 전 포지션 동시손절 시 -12%로 제한.
PORTFOLIO_TOTAL_SL_RISK_CAP_PCT = 0.12
PORTFOLIO_TOTAL_SL_RISK_HIGH_OPPORTUNITY_CAP_PCT = 0.15
# 방향 쏠림 캡: 이번 사고(Binance -17.9%)의 핵심 원인이었던 지표라 가장 보수적으로만
# 완화(0.45→0.55) — 롱/숏 한쪽 쏠림 동반손절 위험은 여전히 최우선으로 억제.
PORTFOLIO_DIRECTIONAL_MARGIN_CAP = 0.55
PORTFOLIO_DIRECTIONAL_HIGH_OPPORTUNITY_CAP = 0.60
PORTFOLIO_MAX_OPEN_POSITIONS = 9             # 6 → 9 (원래 12보다는 여전히 낮음)
PORTFOLIO_MAX_OPEN_POSITIONS_HIGH_OPPORTUNITY = 11   # 8 → 11
PORTFOLIO_POSITION_QUERY_RETRIES = 2

# ─── 뉴스/수급성 거래량 급등 추세 전략 ─────────────────────────────────────
# 별도 뉴스 API 없이도 코인에서는 거래량 급증이 뉴스/수급 이벤트의 선행 대리변수로 자주 작동한다.
VOLUME_MOMENTUM_TF = {"15m", "1h", "4h"}
VOLUME_MOMENTUM_MIN_VOL = 2.50
VOLUME_MOMENTUM_BODY_ATR = 0.45
VOLUME_MOMENTUM_LOOKBACK = 20

# ─── BB 중단 상방 유지 → 내림롱 전략 ────────────────────────────────────────
# "계속 위에서 형성" 기준: 주봉 최근 6개 중 5개, 3일봉 최근 8개 중 6개 이상 종가가 BB 중단 위.
BB_MID_WEEK_LOOKBACK = 6
BB_MID_WEEK_MIN_ABOVE = 5
BB_MID_3D_LOOKBACK = 8
BB_MID_3D_MIN_ABOVE = 6
BB_MID_PULLBACK_TF = {"15m", "1h", "4h"}

# ─── 빗썸 현물 데일리 스크리너 ───────────────────────────────────────────────
# 매일 KST 00:30, 06:30 이후 첫 자동 스캔에서 KRW마켓 전체를 조회한다.
# 조건: 최신 일봉이 MA200/BB 중단선 위이고, 최근 N개 일봉 중 M개 이상이 둘 다 위에 있는 종목.
BITHUMB_MA200_ALERT_ENABLED = True
BITHUMB_MA200_ALERT_HOUR = 8       # 구버전 호환용. 실제 발송은 BITHUMB_MA200_ALERT_TIMES 사용.
BITHUMB_MA200_ALERT_TIMES = ["00:30", "06:30"]  # KST, 하루 2회 시장 스크리닝
BITHUMB_MA200_CANDLE_COUNT = 200   # 일봉 200개로 MA200 계산
BITHUMB_MA200_ABOVE_LOOKBACK_DAYS = 5
BITHUMB_MA200_ABOVE_MIN_DAYS = 4
BITHUMB_MA200_REQUEST_DELAY = 0.08 # 전체 종목 조회 시 API 부하 완화
BITHUMB_MA200_MAX_ROWS_PER_MSG = 45

# ─── 국내주식 데일리 스크리너 ───────────────────────────────────────────────
# 매일 KST 00:30, 06:30 이후 첫 자동 스캔에서 KOSPI/KOSDAQ 전체를 조회한다.
# 조건: 최신 일봉이 MA200/BB 중단선 위이고, 최근 N개 일봉 중 M개 이상이 둘 다 위에 있는 종목.
# 1차 MVP는 무료 데이터 소스인 FinanceDataReader를 사용하고,
# 안정 운영 단계에서는 한국투자증권 KIS Open API로 전환을 검토한다.
KRX_MA200_ALERT_ENABLED = True
KRX_MA200_ALERT_HOUR = 8           # 구버전 호환용. 실제 발송은 KRX_MA200_ALERT_TIMES 사용.
KRX_MA200_ALERT_TIMES = ["00:30", "06:30"]  # KST, 하루 2회 시장 스크리닝
KRX_MA200_CANDLE_COUNT = 200
KRX_MA200_ABOVE_LOOKBACK_DAYS = 5
KRX_MA200_ABOVE_MIN_DAYS = 4
KRX_MA200_LOOKBACK_DAYS = 430
KRX_MA200_REQUEST_DELAY = 0.02
KRX_MA200_MAX_ROWS_PER_MSG = 35
KRX_MA200_MAX_RESULTS = 180

# ─── 향후 데이터 소스 전환 알림 ─────────────────────────────────────────────
# 한투증권 API 키를 지금 바로 요구하지 않되, MVP가 어느 정도 쌓인 뒤
# 정식 시세/종목 데이터 소스로 전환할지 다시 확인하도록 하루 1회 루프에서 알림을 보낸다.
KIS_API_REVIEW_REMINDER_ENABLED = True
KIS_API_REVIEW_REMINDER_DATE = "2026-07-14"  # KST, 이후 첫 실행에서 1회 알림
KIS_API_EARLY_REVIEW_ENABLED = True
KIS_API_EARLY_FAILURE_THRESHOLD = 2     # 국내주식 무료 데이터 전체 스캔 연속 실패 횟수
KIS_API_EARLY_MIN_ERRORS = 50           # 성공한 스캔이어도 이 이상 오류가 쌓이면 품질 저하로 본다.
KIS_API_EARLY_ERROR_RATE = 0.20         # 전체 종목 대비 오류율 20% 이상
KIS_API_EARLY_MIN_SCAN_RATIO = 0.55     # 전체 종목 중 MA200 계산 가능 종목이 55% 미만이면 이상 신호

TIMEFRAMES = {
    "1d":  {"label": "일봉",    "limit": 120},
    "4h":  {"label": "4시간봉", "limit": 200},
    "1h":  {"label": "1시간봉", "limit": 200},
    "15m": {"label": "15분봉",  "limit": 200},
    "5m":  {"label": "5분봉",   "limit": 200},
}

# ─── MTF (다중 타임프레임) 설정 ──────────────────────────────────────────────
# 전 상위봉 정렬 시 포지션 배율 (main.py에서 적용)
MTF_POSITION_BOOST  = 1.30   # 전 TF 정렬 → 30% 증가
MTF_POSITION_CAP    = 0.40   # 부스트 후 최대 포지션 비율 상한 (0.30 → 0.40)
ELITE_MTF_REVERSAL_RISK_MULT = 0.60  # MTF 역방향이지만 7/7 반전 ELITE면 소액 허용
ELITE_MTF_HIDDEN_RISK_MULT   = 0.70  # 히든 ELITE는 추세 재개 성격을 더 반영해 반전보다 크게 허용
ACTIVE_MTF_REVERSAL_RISK_MULT = 0.75  # 고거래량 현재봉 추가전략은 MTF 역방향이어도 정상 후보로 감액 진입
EMA_NEUTRAL_MTF_RISK_MULT = 0.70  # EMA 중립이어도 MTF/확인수/볼륨이 강하면 감액 진입

# MTF 완전 역방향이라도, 다이버전스/거래량 급등은 상위봉이 뒤늦게 따라오는 경우가 있다.
# 이런 신호는 차단보다 "감액 진입 + 후속 게이트 검증"으로 처리한다.
MTF_SOFT_REVERSAL_RISK_MULT = 0.50
MTF_SOFT_HIDDEN_RISK_MULT   = 0.60
MTF_SOFT_MIN_CONFIRMED     = 5
MTF_SOFT_MIN_DIVERGENCE    = 4
MTF_SOFT_MIN_VOL           = 1.00
# 2026-07-03 리뷰: MTF 완전역방향 소프트 통과를 LONG 전용으로 제한.
# 실측 — 통과 LONG 10건 +$1.83 (EV 양수) / 통과 SHORT 5건 2승 3패 -$3.39
# (PYTH -2.95, TAIKO -1.96 포함). SHORT는 상위봉 역방향 시 무조건 차단.
MTF_SOFT_OVERRIDE_LONG_ONLY = True

# MODERATE 신호는 알림만 — 자동매매 제외
MODERATE_AUTO_TRADE = False

# ─── 지표 파라미터 ────────────────────────────────────────────────────────────
RSI_PERIOD   = 14
RSI_OVERSOLD  = 30    # 35 → 30: 과매도 기준 강화 (RSI 30 이하 = 진짜 극단)
RSI_OVERBOUGHT = 70   # 65 → 70: 과매수 기준 강화

STOCH_RSI_PERIOD = 14   # StochRSI 룩백
STOCH_K_SMOOTH   = 3    # %K 스무딩
CCI_PERIOD       = 20   # CCI 다이버전스 룩백

VOL_SPIKE_THRESHOLD = 1.5   # 1.2→1.5: 진짜 거래량 급증만 인정 (1.2x는 일상 노이즈)

EMA_FAST = 20
EMA_SLOW = 50

# 피봇 탐색 범위
PIVOT_LEFT  = 5
PIVOT_RIGHT = 5
LOOKBACK    = 60
