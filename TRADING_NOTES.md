# GodOfTrading Trading Notes

## 2026-07-01 Dynamic Surge Expansion

- Goal: catch more TAIKO-style trades where volume surges first, volatility expands, and a strong directional setup follows.
- Current expansion:
  - `VOLUME_SURGE_TOP_N = 8`
  - `BTC_SYNC_SCAN_TOP_N = 8`
  - `BTC_SYNC_DIRECT_TOP_N = 4`
  - `BTC_SYNC_TOP_N = 50`
- Operating principle:
  - Keep collecting candidate and execution data before widening further.
  - If volume-surge trades show positive expectancy, consider raising surge/BTC-sync scan breadth from 8 to 10.
  - If noise or drawdown grows, keep the scan breadth but tighten trade-entry quality, not the market radar itself.
- Strategy interpretation:
  - TAIKO-type winners are classified as dynamic volume-surge continuation opportunities.
  - Prefer evidence from `trade_execution_journal.jsonl` and `trade_candidates.jsonl` before changing thresholds.

## Exchange Expansion Watchlist

- Current production exchange: Bybit futures.
- Expansion should be considered only after Bybit live-trade expectancy is clearer.
- Candidate order for future review:
  1. Binance Futures, if account/API access is available and legally usable.
  2. Hyperliquid, first as a read-only radar and then as a separate wallet-based execution venue after testing.
  3. OKX Futures/Swap, for liquidity diversification.
  4. Bitget Futures, as a secondary altcoin venue.
  5. Gate/MEXC only for radar or small-size testing first, because long-tail liquidity quality can vary.
- Hyperliquid notes:
  - Attractive because it is a high-volume order-book perp DEX with active long-tail/speculative markets.
  - Integration differs from CEXs: wallet/API-wallet signing, USDC collateral, bridge/deposit handling, and on-chain style account state.
  - Add read-only market radar before live execution. Live trading should use a separate adapter and small test capital.

## 2026-07-01 Strategy 5 / Hyperliquid Lead Radar

- Initial mode: read-only radar, not direct Hyperliquid execution.
- Production execution remains on Bybit.
- Signal flow:
  - Hyperliquid `metaAndAssetCtxs` supplies 24h notional volume, funding, mark price, and open interest.
  - Hyperliquid `candleSnapshot` supplies short-term 15m/1h momentum and volume expansion.
  - Only symbols that can be mapped to Bybit USDT futures are passed into the Bybit scan universe.
- Trading use:
  - If Hyperliquid lead direction agrees with the GOT/Bybit entry direction, apply a small risk confidence boost.
  - If funding is overheated or direction disagrees, keep the radar note but skip the boost.
  - Entry journals include the Hyperliquid snapshot so later performance analysis can compare Strategy 5 assisted trades.
- Next review:
  - After enough Strategy 5 assisted candidates accumulate, compare win rate, payoff, MFE/MAE, and realized PnL against non-assisted trades.
  - Only consider direct Hyperliquid live trading after read-only lead signals show positive expectancy.
- Multi-exchange execution must use a common adapter layer before live orders:
  - market metadata normalization
  - tick/lot size handling
  - leverage/margin mode handling
  - order/position state reconciliation
  - exchange-specific failure and retry rules

## 2026-07-01 Portfolio Capacity Gate

- Fixed concurrent-position blocking is no longer used as a live-entry gate.
- New entries are judged by total margin usage, directional margin concentration, and local SL-based total loss risk.
- If a new signal exceeds a portfolio cap, the bot first tries to reduce position size into the available capacity instead of blocking immediately.
- Current caps:
  - normal signals: 82% account margin usage, 28% total SL risk, 80% same-direction margin concentration
  - high-opportunity signals: 92% account margin usage, 40% total SL risk, 93% same-direction margin concentration

## 2026-07-01 Fast Radar

- The original full scan remains on the 5-minute `com.cryptosignal` LaunchAgent.
- A separate `com.cryptosignal.fast` LaunchAgent runs `main.py --auto-trade --fast-radar` every 3 minutes.
- Fast Radar scans only hot candidates:
  - Bybit volume-surge symbols
  - Hyperliquid lead-radar symbols
  - BTC Sync dislocation symbols
  - currently open-position symbols
- Fast Radar uses only 15m, 1h, and 4h timeframes. It does not run 5m-only trading or daily Bithumb/KRX alerts.
- Purpose: catch fast-moving opportunities sooner without changing the full-scan cadence or weakening existing entry gates.
