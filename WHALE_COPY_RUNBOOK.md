# Whale Copy Runbook (Polymarket live + Hyperliquid paper)

## A. Polymarket 고래 카피 — 초소액 LIVE

### 이미 있는 것
- Paper: `polymarket_whale_paper_bot.py` + `com.polymarket.whale.paper`
- Live 경로: `polymarket_whale_live_bot.py` + `polymarket_clob_exec.py`
- 상태 분리: `polymarket_whale_live_state.json` / `*_live_journal.jsonl`

### 기본 리스크 (초소액)
| 항목 | 기본 |
|------|------|
| bankroll 기준 | $200 (`POLYMARKET_LIVE_BANKROLL`) |
| 단건 | min(1% bankroll, **$5**) |
| 동시 포지션 | 5 |
| 일손실 | $25 |
| LIVE 플래그 기본 | **off (dry-run)** |

### LIVE 켜기 전 체크
0. **Python >= 3.9.10** 필요 (시스템 3.9.6 이면 CLOB 클라이언트 설치 불가)
   ```bash
   brew install python@3.12
   # 반드시 v2 (2026 CLOB 마이그레이션 이후 v1 주문은 invalid order version)
   /Users/ghp/Projects/GodOfTrading/.venv-poly/bin/pip install -U py-clob-client-v2
   ```
1. `pip install py-clob-client-v2` — **3.12 + .venv-poly** (LaunchAgent 가 이 venv 사용)
1b. **서명 지갑에 USDC 잔고** 필요. CLOB `get_balance_allowance` 가 $0 이면 주문 안 나감.  
    Magic/email 프록시 지갑이면 `POLYMARKET_FUNDER=0x...` + `POLYMARKET_SIGNATURE_TYPE=1` (또는 2).  
    점검: ` .venv-poly/bin/python polymarket_whale_live_bot.py --smoke `
2. `.env`:
   ```bash
   POLYMARKET_PRIVATE_KEY=0x...
   # POLYMARKET_FUNDER=0x...   # proxy 쓰면
   POLYMARKET_LIVE_TRADING_ENABLED=false   # 먼저 false로 dry-run
   POLYMARKET_LIVE_BANKROLL=200
   POLYMARKET_LIVE_BET_USD_CAP=5
   POLYMARKET_LIVE_MAX_OPEN=5
   POLYMARKET_LIVE_MAX_DAILY_LOSS=25
   ```
3. 점검 스크립트:
   ```bash
   python3 tools/whale_copy_setup_check.py
   python3 polymarket_whale_live_bot.py --smoke
   python3 polymarket_whale_live_bot.py --json   # dry-run 스캔
   ```
4. 문제 없으면:
   ```bash
   POLYMARKET_LIVE_TRADING_ENABLED=true
   ```
5. LaunchAgent 이미 등록됨 (`com.polymarket.whale.live`). 재시작:
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.polymarket.whale.live
   ```

### 주의
- Paper +45% ≠ 실거래 수익. 슬리피지·지연·동시 포지션 검증 필수.
- paper 봇은 계속 돌려 A/B 비교 권장.

---

## B. Hyperliquid 고래 paper

### 파일
- `hyperliquid_whale_paper_bot.py` — 3분 폴링, 4h TG, **실주문 없음**
- `hyperliquid_whale_config.json` — whales[] 모수 (2026-07-11 리더보드 12지갑 시드)
- `tools/hl_whale_screen.py` — 활동 검증 / 리더보드 재스크리닝
- state/journal: `hyperliquid_whale_paper_state.json`, `*_journal.jsonl`

### 상태 점검 (2026-07-11)
| 항목 | 상태 |
|------|------|
| 봇 코드 | 동작 (콜드스타트 과거 전량 카피 버그 수정) |
| 모수 지갑 | **12개** 리더보드 스크리닝 (월수익+거래량+최근활동) |
| LaunchAgent | `com.hyperliquid.whale.paper` (180s) |
| LIVE | **없음** (paper only — 폴리 insight 와 동일 원칙) |

### 시작 / 재스크리닝
```bash
# 리더보드에서 모수 갱신
python3 tools/hl_whale_screen.py --from-leaderboard --write-config --top 12

# 1회 실행 (첫 사이클은 커서 시드만, 두 번째부터 신규 체결 카피)
python3 hyperliquid_whale_paper_bot.py --json
python3 hyperliquid_whale_paper_bot.py --report-now

# Agent
cp com.hyperliquid.whale.paper.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hyperliquid.whale.paper.plist
```

### 파라미터 (`hyperliquid_whale_config.json` params)
- `min_fill_notional_usd`: 고래 체결 최소 (기본 5000)
- `copy_notional_usd`: paper 카피 크기 (기본 25)
- `max_leverage_copy`: 기록용 캡 (기본 5)
- `max_hold_hours`: paper 최대 보유 (기본 48) — 고래 flat 또는 시간초과 시 정산
- `report_interval_seconds`: TG 주기 (기본 14400 = 4h)

### 카피 로직 요약
1. 지갑 userFills 폴링 → notional ≥ min 인 **Open Long/Short** 만 신호
2. 코인당 1 포지션, 고정 $25 notional, 슬리피지 bps
3. 정산: 고래 clearinghouse flat **또는** max_hold
4. 첫 발견 시 커서를 최신 체결로 시드 → **과거 체결 소급 카피 안 함**

### 다음 단계 (아직 안 함)
- HL 실주문 어댑터 (paper 검증 후)
- 지갑별 z-score suspend (폴리 고래 paper 패턴)
- GOT 본선 시그널과 소프트 결합

---

## 계좌 분리 원칙
- GodOfTrading Bybit/Binance futures
- Polymarket whale (paper / live)
- Hyperliquid whale paper  

상태 파일·자금·리스크 한도를 **절대 섞지 말 것.**
