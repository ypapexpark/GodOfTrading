SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "BNB/USDT", "AVAX/USDT", "LINK/USDT",
    # 추가 심볼 — 잦은 신호 + 충분한 유동성
    "DOGE/USDT", "ADA/USDT", "DOT/USDT", "SUI/USDT",
]

# ─── 동적 거래대금 레이더 ───────────────────────────────────────────────────
# Top10 절대 거래대금 + 직전 스캔 대비 거래대금 급증 종목을 함께 감시한다.
RADAR_TOP_N = 10
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
FAST_TP_BY_STRENGTH = {
    "STRONG":      [{"pct": 50, "atr_mult": 1.0}, {"pct": 30, "atr_mult": 1.6}, {"pct": 20, "atr_mult": 2.4}],
    "VERY STRONG": [{"pct": 45, "atr_mult": 1.0}, {"pct": 35, "atr_mult": 1.8}, {"pct": 20, "atr_mult": 2.8}],
    "ELITE":       [{"pct": 40, "atr_mult": 1.1}, {"pct": 40, "atr_mult": 2.0}, {"pct": 20, "atr_mult": 3.0}],
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
MIN_TRADE_MARGIN_MAX_BALANCE_PCT = 0.25  # 단일 포지션 잔고 비중 하드캡 (25%)
# 목표 증거금/일손실 소프트캡 때문에 좋은 자리가 사라지지 않도록 쓰는 축소진입 하한.
# 실제 거래소 최소수량은 주문 직전 calc_qty()가 다시 확인한다.
MIN_FALLBACK_TRADE_MARGIN_USD = 1.0

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
CONVICTION_MARGIN_PCT_BY_TIER = {
    "BASE": 0.08,
    "STRONG": 0.12,
    "VERY STRONG": 0.20,
    "ELITE": 0.20,
    "GOLDEN": 0.25,
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
HIGH_OPPORTUNITY_MAX_ACCOUNT_RISK_PCT = 0.1000
# 일손실 한도의 N배 도달 시 고기대수익 예외 해제 (10.0 = 1000%로 사실상 무력화 → 1.5로 수정)
HIGH_OPPORTUNITY_DAILY_LOSS_DISABLE_AT = 1.50
# 계좌 DD가 이 비율 이상이면 고기대수익 예외 차단 (0.50=50%로 너무 관대 → 0.15로 수정)
HIGH_OPPORTUNITY_DD_DISABLE_PCT = 0.15

# ─── 초고수익률 시드 극대화 모드 ─────────────────────────────────────────────
# TAIKO/ZBT처럼 TP 수익률은 매우 크지만 SL폭도 큰 종목은 고레버리지로 들어가면
# 손절 위험 때문에 증거금이 오히려 작아진다. 이런 자리는 레버리지를 낮춰
# 같은 계좌위험 안에서 증거금을 크게 쓰는 방식이 더 효율적이다.
PROFIT_SURGE_SIZING_ENABLED = True
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
PROFIT_SURGE_TIGHT_STOP_ENABLED = True
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
GOLDEN_ENTRY_RISK_PCT   = 0.0400
MAX_ACCOUNT_RISK_PCT    = 0.0450
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
    "EMA눌림목",
    "EMA눌림목+거래량급등",
    "EMA눌림목+돌파",
    "EMA눌림목+거래량급등+돌파",
    "hidden_bullish",
    "hidden_bearish",
}
# SHORT 방향 실거래 임시 차단. SHORT 22건 36% 승률, 손실의 84% 차지.
# LONG EMA 전략 집중으로 복리 성장 시작. 충분한 데이터 후 재개.
BLOCK_SHORT_AUTO_TRADE = True

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
PORTFOLIO_MARGIN_USAGE_CAP = 0.80
PORTFOLIO_MARGIN_USAGE_HIGH_OPPORTUNITY_CAP = 0.90
# 동시 SL 위험 캡: 0.50(50%) → 0.25(25%). MAX_DAILY_LOSS_PCT=5%와 정합성 확보.
PORTFOLIO_TOTAL_SL_RISK_CAP_PCT = 0.25
PORTFOLIO_TOTAL_SL_RISK_HIGH_OPPORTUNITY_CAP_PCT = 0.35
# 방향 쏠림 캡: 1.00(무제한) → 0.65. BTC 급락 시 롱 다수 동시 손절 방지.
PORTFOLIO_DIRECTIONAL_MARGIN_CAP = 0.65
PORTFOLIO_DIRECTIONAL_HIGH_OPPORTUNITY_CAP = 0.75
PORTFOLIO_MAX_OPEN_POSITIONS = 12
PORTFOLIO_MAX_OPEN_POSITIONS_HIGH_OPPORTUNITY = 16
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
