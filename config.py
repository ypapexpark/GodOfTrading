SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "BNB/USDT", "AVAX/USDT", "LINK/USDT",
    # 추가 심볼 — 잦은 신호 + 충분한 유동성
    "DOGE/USDT", "ADA/USDT", "DOT/USDT", "SUI/USDT",
]

# ─── 동적 거래대금 레이더 ───────────────────────────────────────────────────
# Top10 절대 거래대금 + 직전 스캔 대비 거래대금 급증 종목을 함께 감시한다.
RADAR_TOP_N = 10
VOLUME_SURGE_TOP_N = 5
VOLUME_SURGE_MIN_24H_USD = 3_000_000
VOLUME_SURGE_MIN_DELTA_USD = 500_000
VOLUME_SURGE_MIN_DELTA_PCT = 3.0

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
MAX_DAILY_LOSS_PCT      = 0.0300
AUTO_TRADE_DIAGNOSTICS  = True
CANDIDATE_LOG_FILE      = "trade_candidates.jsonl"
EXECUTION_JOURNAL_FILE  = "trade_execution_journal.jsonl"

# ─── 계좌 드로우다운 방어 ───────────────────────────────────────────────────
# 손실 구간에서는 "학습"보다 생존이 우선이다.
# ACCOUNT_START_BALANCE(.env)가 있으면 그 값을 기준으로, 없으면 관측된 최고 equity를 기준으로 방어한다.
DRAWDOWN_WARN_PCT       = 0.08
DRAWDOWN_RISK_OFF_PCT   = 0.12
DRAWDOWN_HARD_STOP_PCT  = 0.18
DRAWDOWN_PAUSE_HOURS    = 24
DRAWDOWN_RISK_MULT      = 0.25

# ─── 방어형 리스크 거버너 ───────────────────────────────────────────────────
# 최근 성과가 나쁜 TF/전략은 자동으로 줄이거나 쉬게 한다.
# 목적: 신호를 더 많이 잡는 것보다, 음의 기댓값 구간에서 계좌 노출을 줄이는 것.
TF_LOSS_COOLDOWN = {
    "5m":  {"losses": 2, "hours": 12, "lookback_hours": 24},
    "15m": {"losses": 2, "hours": 8,  "lookback_hours": 16},
}
STRATEGY_LOSS_COOLDOWN = {"losses": 2, "hours": 12}
LOSS_STREAK_RISK_MULT = {1: 0.75, 2: 0.50, 3: 0.35}
PROBATION_MIN_TRADES = 5
PROBATION_RISK_MULT = 0.50
MIN_DYNAMIC_RISK_MULT = 0.35

# STRONG 실거래는 "늦게 뜬 다이버전스"가 아니라 현재봉 기반 전략만 허용한다.
ACTIVE_STRONG_STRATEGIES = {"RSI반전", "EMA눌림목", "BB스퀴즈", "마이크로돌파"}
STRONG_LIVE_MAX_BARS_AGO = 1
STRONG_LIVE_MIN_VOL      = 1.10

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
