# Binance Pump Derivatives Precursor Study

- Generated UTC: 2026-07-16T23:29:52.935104+00:00
- Eligible pump events: 155
- Events with derivatives + matched controls: 133
- Anchor: last fully closed 1h bucket before the bar preceding first +5% daily breakout
- Controls: same symbol/time on up to 3 prior non-pump days

| feature | event n | event median | control median | matched difference | event > control | sign p |
|---|---:|---:|---:|---:|---:|---:|
| oi_value_change_1h_pct | 133 | +0.5172 | +0.0355 | +0.3766 | +58.0153% | +0.0802 |
| oi_value_change_6h_pct | 133 | +0.5038 | -0.5761 | +1.8880 | +62.0155% | +0.0080 |
| oi_value_change_24h_pct | 133 | +3.0110 | -2.5288 | +6.3603 | +64.0625% | +0.0019 |
| taker_buy_sell_ratio | 133 | +1.0328 | +0.9571 | +0.0677 | +65.4135% | +0.0005 |
| global_long_short_ratio | 133 | +1.1000 | +1.0855 | -0.0104 | +48.8722% | +0.8624 |
| top_long_short_ratio | 133 | +1.1608 | +1.1920 | -0.0173 | +44.3609% | +0.2246 |
| top_minus_global_ratio | 133 | +0.0699 | +0.0817 | -0.0093 | +45.8647% | +0.3860 |

## Guardrails

- These are public Binance derivatives aggregates, not blockchain exchange-wallet flows.
- The +5% onset definition means even the prior bucket can reflect an already-started move.
- A common precursor is not automatically profitable after latency, fees, slippage, and false positives.
