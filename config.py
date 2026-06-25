SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "BNB/USDT", "AVAX/USDT", "LINK/USDT",
    # 추가 심볼 — 잦은 신호 + 충분한 유동성
    "DOGE/USDT", "ADA/USDT", "DOT/USDT", "SUI/USDT",
]

# ─── 수수료 ───────────────────────────────────────────────────────────────────
BYBIT_TAKER_FEE = 0.00055   # 0.055%
ROUND_TRIP_FEE  = BYBIT_TAKER_FEE * 2

# ─── 타임프레임별 최소 TP1 수익률 (gross %, 수수료 전) ──────────────────────
MIN_GROSS_PCT = {
    "5m":  2.0,
    "15m": 2.0,
    "1h":  2.0,
    "4h":  2.5,
    "1d":  3.0,
}

# 5m/15m는 5지표 중 4개 이상 확인돼야 발송 (노이즈 필터)
STRICT_TF = {"5m", "15m"}

# 스캘핑 자동매매 신호 신선도 — 피봇 형성 후 최대 허용 봉 수
# PIVOT_RIGHT=5 때문에 최솟값은 5 (5봉 후 확정)
SCALP_FRESHNESS = {"5m": 10, "15m": 8}

# 스윙 자동매매 신호 신선도 — 너무 오래된 피봇은 유효성 상실
# 1h: 12봉(12시간) / 4h: 8봉(32시간) / 1d: 3봉(3일)
SWING_FRESHNESS = {"1h": 12, "4h": 8, "1d": 3}

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
    ("ELITE",       "1d"):  20,   # 15 → 20
    ("ELITE",       "4h"):  18,   # 12 → 18
    ("ELITE",       "1h"):  15,   # 10 → 15
    ("ELITE",       "15m"): 8,    #  5 → 8
    ("ELITE",       "5m"):  5,    #  3 → 5
    ("VERY STRONG", "1d"):  12,   # 10 → 12
    ("VERY STRONG", "4h"):  10,   #  7 → 10
    ("VERY STRONG", "1h"):  7,    #  5 → 7
    ("VERY STRONG", "15m"): 5,    #  3 → 5
    ("VERY STRONG", "5m"):  3,    #  2 → 3
    ("STRONG",      "1d"):  7,    #  5 → 7
    ("STRONG",      "4h"):  5,    #  4 → 5
    ("STRONG",      "1h"):  4,    #  3 → 4
    ("STRONG",      "15m"): 3,    #  2 → 3
    ("STRONG",      "5m"):  2,
}

# ─── 신호 강도별 증거금 비율 (확신도 높을수록 베팅 증가) ───────────────────
# 복리 성장용 공격적 배분 — 황금 진입 시 GOLDEN_ENTRY_POSITION_PCT로 오버라이드
#
# MODERATE (3/6): 구조레벨+EMA 조건 충족 시만 허용, 소액 7% 베팅
# STRONG (4/6): 10% — 조건부 허용 (EMA 정렬 필수)
MARGIN_BY_STRENGTH = {
    "MODERATE":    0.07,   # 신규: 구조레벨+EMA 조건 충족 시 소액 베팅
    "STRONG":      0.10,   # 13% → 10%: 더 자주 진입 but 소액 유지
    "VERY STRONG": 0.18,
    "ELITE":       0.25,
}

# ─── 황금 진입 (ELITE + MTF 전정렬 + EMA 정렬) ───────────────────────────────
# 세 조건이 동시 충족 = 최고 확신도 → 최대 베팅, 복리 극대화
GOLDEN_ENTRY_POSITION_PCT = 0.45   # 잔고의 45% 증거금
GOLDEN_LEVERAGE_BOOST     = 1.50   # 기본 레버리지 × 1.5
GOLDEN_MAX_LEVERAGE       = 25     # 황금 진입 레버리지 절대 상한

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

# MODERATE(3/6) 신호는 알림만 — 자동매매 제외
MODERATE_AUTO_TRADE = False

# ─── 지표 파라미터 ────────────────────────────────────────────────────────────
RSI_PERIOD   = 14
RSI_OVERSOLD  = 30    # 35 → 30: 과매도 기준 강화 (RSI 30 이하 = 진짜 극단)
RSI_OVERBOUGHT = 70   # 65 → 70: 과매수 기준 강화

STOCH_RSI_PERIOD = 14   # StochRSI 룩백
STOCH_K_SMOOTH   = 3    # %K 스무딩

VOL_SPIKE_THRESHOLD = 1.5   # 1.2→1.5: 진짜 거래량 급증만 인정 (1.2x는 일상 노이즈)

EMA_FAST = 20
EMA_SLOW = 50

# 피봇 탐색 범위
PIVOT_LEFT  = 5
PIVOT_RIGHT = 5
LOOKBACK    = 60
