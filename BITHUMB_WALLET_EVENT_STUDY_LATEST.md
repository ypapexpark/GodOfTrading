# Bithumb Wallet Pause Event Study

- Generated: 2026-07-17T07:56:30.687903+09:00
- Emergency clusters: 13
- Scheduled clusters: 16
- Entry assumption: next 15-minute open after public notice
- Emergency multi-asset outages are equal-weighted once per chain event

## Aggregate

| cohort | pre 1h | pre volume ratio | post 1h | post 6h | post 24h | 24h MFE | 24h ≥5% | Binance-relative 24h |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| emergency | +3.32% | +436.73x | -0.61% | -0.62% | -2.42% | +8.04% | +70.00% | -2.45% |
| scheduled | +0.43% | +0.86x | +0.19% | +0.42% | +0.00% | +1.82% | +20.00% | +0.16% |

## Emergency pre-notice matched controls

- pre_ret_1h_pct: n=10, median(event-control)=+2.26%, positive=60.0%, median event percentile=+90.74%, sign-test p=0.75390625
- pre_ret_6h_pct: n=10, median(event-control)=+5.29%, positive=70.0%, median event percentile=+96.43%, sign-test p=0.34375
- pre_ret_24h_pct: n=10, median(event-control)=+4.92%, positive=60.0%, median event percentile=+82.69%, sign-test p=0.75390625
- pre_qvol_ratio: n=10, median(event-control)=+387.81x, positive=90.0%, median event percentile=+100.00%, sign-test p=0.021484375
- pre_qvol_log10_ratio: n=10, median(event-control)=+2.25 log10(x), positive=90.0%, median event percentile=+100.00%, sign-test p=0.021484375

## Event clusters

| published KST | kind | assets n | post 1h | post 6h | post 24h | 24h MFE | 24h MAE | abnormal 24h |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-01-15 00:45:06 | emergency_network | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| 2026-02-08 11:45:07 | emergency_network | 1 | -1.18% | -4.25% | -7.31% | +6.84% | -8.49% | -3.64% |
| 2026-02-10 08:54:08 | emergency_network | 1 | -2.02% | +78.09% | -2.49% | +93.92% | -14.40% | -4.15% |
| 2026-02-13 08:46:44 | emergency_network | 1 | +20.75% | -13.50% | -30.15% | +28.30% | -31.80% | -30.15% |
| 2026-04-02 03:07:55 | emergency_security | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| 2026-05-21 01:55:03 | emergency_security | 1 | -5.64% | +1.91% | -7.89% | +9.21% | -8.59% | n/a |
| 2026-05-28 23:48:38 | emergency_network | 4 | -3.17% | -1.72% | -2.35% | +1.80% | -6.13% | -2.45% |
| 2026-05-29 21:24:26 | emergency_network | 4 | -1.51% | -0.40% | +0.58% | +6.87% | -3.97% | -1.79% |
| 2026-06-21 01:16:14 | emergency_network | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| 2026-06-22 07:41:38 | emergency_network | 1 | +11.20% | +25.20% | +5.60% | +47.60% | -16.80% | +29.03% |
| 2026-06-27 02:58:38 | emergency_network | 16 | -0.05% | -0.83% | -1.37% | +2.62% | -3.69% | -0.05% |
| 2026-07-11 17:38:17 | emergency_security | 1 | +0.00% | -0.96% | -1.92% | +0.96% | -3.85% | +1.08% |
| 2026-07-14 07:20:08 | emergency_network | 1 | +19.53% | +36.70% | -34.42% | +50.79% | -34.79% | -31.13% |
| 2026-04-03 12:00:00 | scheduled_upgrade | 1 | +0.36% | +0.81% | -0.12% | +1.66% | -0.47% | -0.15% |
| 2026-04-06 11:00:00 | scheduled_wallet_change | 1 | +0.78% | +0.00% | -3.91% | +1.82% | -4.17% | +0.18% |
| 2026-04-09 19:00:00 | scheduled_upgrade | 1 | -0.75% | +0.75% | +0.00% | +1.50% | -1.50% | +0.20% |
| 2026-05-15 18:10:00 | scheduled_upgrade | 1 | +0.00% | -0.72% | -2.88% | +0.72% | -2.88% | +0.69% |
| 2026-05-18 18:30:00 | scheduled_upgrade | 1 | +2.14% | +6.98% | +11.35% | +16.31% | +0.00% | n/a |
| 2026-05-20 18:00:00 | scheduled_upgrade | 1 | +0.00% | +0.42% | +4.02% | +4.58% | -1.13% | +0.14% |
| 2026-06-01 18:30:00 | scheduled_upgrade | 1 | +2.49% | +6.36% | +11.77% | +13.47% | -0.37% | -0.23% |
| 2026-06-02 18:00:00 | scheduled_upgrade | 1 | -0.49% | -1.60% | -1.25% | +0.45% | -5.89% | +0.91% |
| 2026-06-05 12:00:00 | scheduled_upgrade | 1 | +1.32% | -0.08% | -5.72% | +2.99% | -7.27% | +1.71% |
| 2026-06-16 18:30:00 | scheduled_rebrand | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| 2026-06-23 19:00:00 | scheduled_upgrade | 1 | +0.27% | -0.80% | -1.87% | +1.07% | -4.81% | +0.52% |
| 2026-07-02 16:30:00 | scheduled_upgrade | 1 | +0.68% | +2.72% | +3.40% | +4.08% | +0.00% | -1.35% |
| 2026-07-09 18:00:00 | scheduled_upgrade | 1 | -0.04% | -0.41% | +1.19% | +1.49% | -1.19% | n/a |
| 2026-07-10 11:00:00 | scheduled_upgrade | 1 | +0.19% | +0.20% | -0.59% | +0.90% | -0.49% | n/a |
| 2026-07-13 18:30:00 | scheduled_upgrade | 1 | -0.39% | +1.41% | +2.60% | +4.54% | -1.65% | -0.37% |
| 2026-07-14 19:00:00 | scheduled_upgrade | 1 | -0.43% | +4.26% | +3.40% | +5.53% | -0.85% | -0.45% |

## Interpretation guardrails

- A pause blocks arbitrage inventory transfer; it does not mechanically create buy demand.
- Security-related pauses can be negative fundamental news, unlike scheduled upgrades.
- Buying before an emergency notice is not an implementable rule unless a public precursor is observed in real time.
- Historical wallet status was not available; block lag and wallet_state must be collected prospectively.
