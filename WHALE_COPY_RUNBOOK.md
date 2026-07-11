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
0. **Python >= 3.9.10** 필요 (시스템 3.9.6 이면 py-clob-client 설치 불가)
   ```bash
   brew install python@3.12
   /opt/homebrew/bin/python3.12 -m pip install py-clob-client
   # plist ProgramArguments 의 python 경로를 3.12 로 바꾸는 것 권장
   ```
1. `pip install py-clob-client` (또는 `py-clob-client-v2`) — **3.12 파이썬으로**
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
- `hyperliquid_whale_paper_bot.py`
- `hyperliquid_whale_config.json` (seed_wallets / whales)
- `tools/hl_whale_screen.py`

### 시작
1. 추적할 HL 주소 확보 (탐색/리서치) 후:
   ```bash
   # config seed_wallets에 넣거나
   python3 tools/hl_whale_screen.py --wallets 0xabc...,0xdef... --write-config
   ```
2. 실행:
   ```bash
   python3 hyperliquid_whale_paper_bot.py --json
   ```
3. Agent (선택):
   ```bash
   cp com.hyperliquid.whale.paper.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hyperliquid.whale.paper.plist
   ```

### 파라미터 (`hyperliquid_whale_config.json` params)
- `min_fill_notional_usd`: 고래 체결 최소 규모 (기본 5000)
- `copy_notional_usd`: paper 카피 크기 (기본 25)
- `max_leverage_copy`: 기록용 캡 (기본 5) — 실주문 시 강제에 사용 예정

### 다음 단계 (아직 안 함)
- HL 실주문 어댑터
- 장기 성과 스크리너 (폴리 PolyBacktest급)
- GOT 본선과 시그널 소프트 결합

---

## 계좌 분리 원칙
- GodOfTrading Bybit/Binance futures
- Polymarket whale (paper / live)
- Hyperliquid whale paper  

상태 파일·자금·리스크 한도를 **절대 섞지 말 것.**
