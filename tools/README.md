# tools/ — 연구·진단 스크립트

`main.py` 런타임 경로에서 **import 하지 않음**. 수동/LaunchAgent(유지보수·주간리포트)만.

| 스크립트 | 용도 |
|----------|------|
| `hl_whale_screen.py` | Hyperliquid 리더보드 모수 스크리닝 |
| `whale_copy_setup_check.py` | 폴리 고래 live 셋업 점검 |
| `weekly_learning_report.py` | 주간 학습 제안 (config 자동 변경 없음) |
| `cf_sim.py` / `cf_recent.py` / `cf_categorize.py` | 반사실 시뮬 |
| `div_counterfactual.py` | 다이버전스 반사실 |
| `macd_ema_filter_cf.py` | 필터 CF |
| `ev_deep_diagnosis.py` | EV 진단 |
| `fractal_candle_postmortem.py` | 캔들 복기 |
| `logic_attribution_report.py` | 로직 귀속 리포트 |
| `postmortem_report.py` | 포스트모템 요약 |
| `ratchet_trail_sim.py` | 트레일 시뮬 |
| `pc_maintenance_agent.py` | PC 유지보수 에이전트 |
| `install_pc_maintenance_agent.sh` / `uninstall_*.sh` | 위 에이전트 설치 |

실행 예:

```bash
python3 tools/hl_whale_screen.py --from-leaderboard --write-config --top 12
python3 tools/weekly_learning_report.py
```
