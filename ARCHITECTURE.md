# GodOfTrading Architecture Map

**목적:** `main.py`(5.5k줄) 등을 통째로 읽지 않고도 수정·분석 위치를 잡기.  
**원칙:** 라이브 매매 로직 변경 없이 구조만 안내. 에이전트/사람은 **이 파일을 먼저** 읽는다.

---

## 1. 시스템 한 장

```
┌─────────────────────────────────────────────────────────────┐
│  LaunchAgents (로컬 주기 실행, 빌드/배포 없음)                 │
│  com.cryptosignal* / .binance* / polymarket.* / hyperliquid.* │
└────────────┬───────────────────────────────┬────────────────┘
             │                               │
     ┌───────▼────────┐              ┌───────▼──────────────┐
     │ Futures 본선    │              │ Side bots (분리 계좌)  │
     │ Bybit + Binance │              │ Poly whale/insight    │
     │ main → router   │              │ HL whale paper        │
     │ → trader/binance│              │ BTC 5m poly paper     │
     └───────┬────────┘              └──────────────────────┘
             │
     state: trade_state*.json · candidates · journals
     TG: publisher.py (TRADE_* / SIGNAL_*)
```

| 축 | 설명 |
|----|------|
| **본선** | 선물 스캔·게이트·진입·SL/TP — 돈·리스크 핵심 |
| **사이드** | Polymarket / HL paper·live — **state·자금 본선과 분리** |
| **연구** | `tools/*` 반사실·주간리포트 — 런타임 경로에 안 탐 |

---

## 2. 파일 지도 (어디를 고칠까)

### 2.1 Futures 본선 (토큰 많이 먹는 코어)

| 파일 | 줄 수(대략) | 역할 | 건드릴 때 |
|------|-------------|------|-----------|
| `main.py` | ~5550 | 스캔 오케스트레이션, 진입 게이트, 브레이크아웃/BTC sync, 리포트 | 신호→주문 게이트 |
| `scalping_engine.py` | ~350 | **S1 본선 알파**: 완료 15m 추세·눌림 + 완료 5m 재가속, 비용후 계획, 현 버전 승격 | 신규 진입 로직 |
| `trader.py` | ~2250 | Bybit 실행, 포지션 모니터, SL/트레일, 후보 저널 | 체결·청산 |
| `binance_trader.py` | ~750 | Binance 어댑터 (trader 미러) | 바이낸스만 |
| `trade_router.py` | ~110 | `AUTO_TRADE_EXCHANGE` 로 bybit/binance 분기 | 벤뉴 라우팅 |
| `venue_runtime.py` | ~65 | state/journal 파일명 벤뉴 분리 | 경로 |
| `process_lock.py` | ~90 | LaunchAgent 겹침 방지 (venue×mode flock) | 중복 스캔 |
| `config.py` | ~900 | 리스크·게이트·전략 플래그 (로직 적고 상수 多) | 파라미터 |
| `strategies.py` | ~1050 | 보조 전략 detect_* | 신규 패턴 |
| `divergence.py` | ~690 | 다이버전스 핵심 신호 | 다이버 로직 |
| `analyzer.py` | ~1150 | 학습/품질 리포트, 레버 조정 힌트 | 사후 분석 |
| `fetcher.py` | ~770 | OHLCV·레이더·HL 리드 | 데이터 |
| `formatter.py` | ~420 | 텔레그램 HTML 본선 시그널 | 문구 |
| `mtf.py` / `leading.py` / `regime.py` | 작음 | MTF·선행·레짐 | 필터 |
| `postmortem.py` | ~440 | 청산 복기 | 사후 |
| `publisher.py` | ~100 | TG 라우팅 | 채널 |
| `strategy_catalog.py` | ~180 | 전략 카탈로그/원칙 매핑 | 문서성 코드 |

### 2.2 main.py 내부 앵커 (함수 이름으로 검색)

| 함수 | 용도 |
|------|------|
| `scan` | 스캔 엔트리 (메인 루프) |
| `_try_scalping_engine_trade` | **현재 라이브 진입 경로** — S1 평가·시드 사이징·주문 |
| `_try_auto_trade` | **최대 함수** — 일반 자동진입 게이트 전부 |
| `_try_breakout_trade` | 돌파 경로 |
| `_try_btc_sync_direct_trade` | BTC 동기 직행 |
| `_apply_portfolio_capacity_gate` | 포트 용량 |
| `_apply_min_trade_margin` | 최소 마진 |
| `_mtf_soft_override` / elite MTF | MTF 완화 |
| `_do_pyramid` | 불타기 |
| `_reconcile_orphan_positions` | 고아 포지션 |
| `_maybe_send_periodic_report` | 4h 본선 리포트 |

**에이전트 규칙:** `main.py` 전체를 읽지 말 것. `grep -n "def _try_auto_trade"` 후 해당 구간만.

2026-07-18부터 `SCALP_ENGINE_ENABLED=True`, `LEGACY_AUTO_TRADE_ENABLED=False`가 기본이다.
따라서 일반/돌파/BTC-sync/불타기 코드는 연구·호환용으로 남아 있어도 S1 외 신규 주문은
나가지 않는다. Binance S1은 API 복구와 별도 venue canary 승인 전 shadow다.

### 2.3 trader.py 앵커

| 함수 | 용도 |
|------|------|
| `execute` | 주문 실행 |
| `monitor_positions` | SL/TP/트레일 모니터 |
| `evaluate_trade_candidates` / `log_trade_candidate` | 후보 기록 |
| `get_portfolio_risk_snapshot` | DD·일손 등 |
| `calc_qty` | 수량 |
| `build_trade_*_notification` | 체결/청산 TG |

### 2.4 Side bots (본선과 import 거의 없음)

| 파일 | 계좌 태그 | LaunchAgent |
|------|-----------|-------------|
| `polymarket_whale_paper_bot.py` | whale paper | `com.polymarket.whale.paper` |
| `polymarket_whale_live_bot.py` | whale live | `com.polymarket.whale.live` |
| `polymarket_insight_paper_bot.py` | insight_paper | `com.polymarket.insight.paper` |
| `polymarket_insight_live_bot.py` | skeleton only | (없음) |
| `hyperliquid_whale_paper_bot.py` | hl_whale_paper | `com.hyperliquid.whale.paper` |
| `binance_copy_intel.py` | Binance public lead/grid shadow | `com.godoftrading.binance.copy.intel` |
| `polymarket_paper_bot.py` | BTC 5m paper | `com.polymarket.paper` |
| `polymarket_clob_exec.py` | CLOB 주문 | live만 |
| `*_insights.py` | 리포트 코멘트 | import only |

Poly insight 시그널 원천: **`/Users/ghp/Projects/PolyInsight`** (별 저장소).

### 2.5 tools/ (연구 전용)

| 스크립트 | 용도 |
|----------|------|
| `hl_whale_screen.py` | HL 리더보드 모수 |
| `whale_copy_setup_check.py` | 폴리 고래 셋업 |
| `weekly_learning_report.py` | 주간 학습 제안 |
| `cf_*.py` / `div_counterfactual.py` | 반사실 |
| `pc_maintenance_agent.py` | PC 유지보수 |
| `*_postmortem*.py` | 복기 리포트 |

런타임 `main.py` 경로에서 **import 하지 않음**.

### 2.6 문서

| 파일 | 내용 |
|------|------|
| `CLAUDE.md` | 작업 원칙·리스크 게이트 |
| `ARCHITECTURE.md` | **이 파일** |
| `WHALE_COPY_RUNBOOK.md` | 폴리/HL 고래 운용 |
| `TRADING_NOTES.md` | 실험 메모 |
| `TRADING_PRINCIPLES.md` | 전략 원칙 |
| `DUAL_VENUE_RUNBOOK.md` | Bybit/Binance 듀얼 |

---

## 3. 런타임 데이터 (코드 아님 — 분석 시 주의)

| 경로 | 성격 |
|------|------|
| `trade_state.json` / `_binance` | 라이브 상태 (계속 갱신) |
| `trade_candidates*.jsonl` | **수십 MB 가능** — 통독 금지, `tail` |
| `trade_execution_journal*.jsonl` | 체결 저널 |
| `polymarket_*_state/journal` | 사이드 봇 |
| `hyperliquid_*` | HL paper |

`.gitignore` 로 커밋 제외. 에이전트는 **상태 파일을 코드처럼 읽지 말 것**.

---

## 4. 중복·비효율 진단 (2026-07-11 점검)

### 구조적으로 큰 것 (의도적 부채)

1. **`main.py` 비대** — 게이트·사이징·BTC sync·리포트가 한 파일.  
   → 로직은 동작 중. **당장 쪼개면 회귀 위험 큼.**  
   → 백로그: `_try_auto_trade` / `_try_breakout` / `_try_btc_sync` / report helpers 모듈 분리 (로직 동치 테스트 후).

2. **사이드 봇 공통 유틸 복붙** — `_env_float`, `_json_safe`, `_now_kst`, jsonl append 가 paper 봇마다 복제.  
   → 동작엔 문제 없음. 백로그: `bot_util.py` 추출 (import만 교체).

3. **`trader.py` ↔ `binance_trader.py` 유사** — 벤뉴 어댑터 패턴.  
   → 완전 통합은 리스크 큼. router가 이미 분리 유지.

### 정리해도 되는 것 (로직 무관)

| 항목 | 조치 |
|------|------|
| `tappy-logo-*.png` (~4.5MB) | GOT 무관 → 삭제·gitignore |
| `trend-radar-free/` (~300MB) | 별 앱, 이 레포 루트에 중첩 → gitignore 권장, 이사는 수동 |
| `archive/` | 과거 스냅샷 유지 OK |
| `exchange_venue_compare.py` | 수동 진단용 (orphan OK) |
| candidates 52MB+ | 주기 로테이션 권장 (봇 중단 후 archive) |

### 건드리면 안 되는 것 (이번 정리 범위 밖)

- `config.py` 수치, 게이트 조건, SL/TP, 레버리지 공식  
- `main._try_auto_trade` 분기 순서  
- 라이브 LaunchAgent 주기/경로 (동의 없이 변경 금지)

---

## 5. 에이전트 토큰 절약 체크리스트

1. 먼저 **이 파일** + 해당 runbook 1개.  
2. `main.py`/`trader.py`는 **`grep def` → 구간 read** only.  
3. 로그: `/tmp/godoftrading*.log` 는 `tail`/`grep`.  
4. `trade_candidates*.jsonl` 통독 금지.  
5. 사이드 봇 작업 시 본선(`main`/`trader`) 읽지 말 것.  
6. 상태 JSON은 판단 직전에만, 필요한 키만.

---

## 6. 권장 리팩터 백로그 (로직 동치, 단계적)

| 우선 | 작업 | 효과 | 리스크 |
|------|------|------|--------|
| P1 | `bot_util.py` — 사이드 봇 공통 util | 중복↓ | 낮음 → **완료 2026-07-11** |
| P2 | candidates 로테이션 | 디스크 | 낮음 → **완료 2026-07-11** (`tools/rotate_candidates.py`) |
| P3 | `main_report.py` — 리포트 빌더만 분리 | main −200~400줄 | 낮음 |
| P4 | `main_btc_sync.py` — BTC sync 경로 분리 | main −600줄 | 중 |
| P5 | `main_entry_gates.py` — capacity/margin/roi 헬퍼 | 가독성 | 중 |
| P6 | `_try_auto_trade` 단계 함수 분리 (동작 동일) | 분석성 | 중~높 |
| — | config 상수 그룹 파일 분리 | 탐색 | 중 (import 다수) |

**사이드 봇 `bot_util` 적용:** insight paper/live, whale paper/live, HL paper, BTC paper.  
**본선 `main`/`trader` 로직 미변경.**

### 겹침 방지 (2026-07-11)

LaunchAgent `StartInterval` 은 이전 종료를 기다리지 않음 → 스캔이 주기보다 길면 동일
`main.py` 가 2~3개 겹칠 수 있음.  
`process_lock.py` 가 `main_{venue}_{full|fast}` flock 으로 **같은 키만** 스킵.
Bybit full ↔ fast, Bybit ↔ Binance 는 **동시 허용** (의도된 분리 실행).

---

## 7. LaunchAgent 빠른 표

| Label | 스크립트 |
|-------|----------|
| `com.cryptosignal` / `.fast` | `main.py` Bybit |
| `com.cryptosignal.binance` / `.fast` | `main.py` Binance env |
| `com.polymarket.whale.paper` / `.live` | 폴리 고래 |
| `com.polymarket.insight.paper` | insight paper |
| `com.hyperliquid.whale.paper` | HL paper |
| `com.polymarket.paper` | BTC 5m paper |
| `com.godoftrading.weekly-report` | 주간 리포트 |
| `com.godoftrading.binance.copy.intel` | Binance 공개 리드 60초 섀도 + 6h discovery |
| `com.godoftrading.binance.orderflow.challenger` | Binance C1 v2 상위 40종목 실시간 돌파-눌림/지속수급 PAPER + 졸업식 micro-LIVE |
| `com.godoftrading.telegram.engine.commands` | 테레그램 엔진/성과/포지션/상태 조회 명령 |

상세 주기는 각 `com.*.plist` 참고.

### Binance C1 검증 계층 (2026-07-19)

- D2 다이버전스: 전종목 후보/저널은 유지, 신규 LIVE는 중지. 기존 포지션 관리는 유지.
- D3 MA200 눌림: LIVE 유지.
- C1 v1: LIVE 1W/5L PF 0.05, PAPER 2W/19L PF 0.054로 폐기. 신규 LIVE 중지.
- C1 v2: LONG 전용. 완료된 5m 거래량 돌파 후 레벨/EMA9/VWAP 눌림,
  30초(최소 3표본) raw trade/depth5 지속성, BTC 1h 정렬과 15m 상대강도를 모두 요구.
- 동일 시장 베타 중복은 신호간 2분, 5분당 2건, PAPER 동시 3건으로 제한.
- v2는 비용·슬리피 반영 PAPER 50건, PF≥1.20, 누적/최근25건/최대승리 제외 순익 양수,
  DD≤초기시드 8%를 통과하기 전에는 LIVE가 잠긴다. 통과 후 계좌위험 0.10% micro-LIVE.
- `binance_orderflow_challenger_state.json` / `...journal.jsonl`에 비용후 forward 결과를 보존.
- Telegram 4시간 리포트와 `/paper`, `/results c1`, `/status`: v1/v2 성과,
  졸업 상태, 신호·PAPER/LIVE 진입·승률·PF·순익·DD·차단수를 분리 표시.
- C1 v2는 PAPER 졸업 시 `승격 완료`를 1회 알리고, 사용자 확인 없이 다음 적합
  신호부터 micro-LIVE를 자동 허용한다. 미달/재잠금은 상태에만 반영한다.
- `/hyperliquid` (`/hl`)은 30초 주기 HL 고래 PAPER의 정산 W/L/BE, PF, PnL,
  DD, 오픈 미실현, 지갑별 성과를 조회한다. HL은 자동 LIVE 전환하지 않는다.
- HL v2는 30초 증분 수집, taker open 합산, clearinghouse 순포지션 증가·계좌대비
  확신도 확인 후에만 PAPER 진입한다. v1/v2 통계는 분리한다.
