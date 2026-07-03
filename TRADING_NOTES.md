# GodOfTrading Trading Notes

## 2026-07-03 Polymarket Paper Bot

- Initial mode: read-only paper trading only. No wallet signing, no live Polymarket orders.
- Runner:
  - `polymarket_paper_bot.py`
  - optional LaunchAgent: `com.polymarket.paper.plist` every 60 seconds
- Report route:
  - Uses existing `publisher.send_review()`, so Polymarket paper reports go to the same trade/review Telegram route as the 4-hour CryptoSignal report.
- Files:
  - state: `polymarket_paper_state.json`
  - paper trade journal: `polymarket_paper_journal.jsonl`
  - observed candidates: `polymarket_paper_candidates.jsonl`
- Default paper logic:
  - Target market: BTC Up/Down 5m.
  - Data: public Polymarket Gamma/CLOB + public Bybit BTC 1m candles.
  - Model: estimates fair Up probability from current BTC price, `priceToBeat`, 1m volatility, and time remaining.
  - Entry: simulates a FOK taker buy only when model probability exceeds average fill price plus Polymarket crypto taker fee and a safety edge buffer.
  - Settlement: waits for Polymarket resolution metadata/outcome prices, then records paper PnL.
- Environment overrides:
  - `POLYMARKET_PAPER_ORDER_USD` default `100`
  - `POLYMARKET_PAPER_MIN_EDGE` default `0.025`
  - `POLYMARKET_PAPER_REPORT_INTERVAL` default `14400`
  - `POLYMARKET_PAPER_RECURRENCE` default `5m`
  - `POLYMARKET_PAPER_MILESTONE_REPORT_AT` installed as `2026-07-10 06:00`
- Scheduled milestone:
  - The running LaunchAgent should send a one-time 1-week paper result report at 2026-07-10 06:00 KST.
- Decision rule before live consideration:
  - Require at least 500 settled paper trades or 2 full weeks of data.
  - Judge by net PnL after simulated taker fee/slippage, not raw win rate.
  - Do not enable live betting unless jurisdiction, KYC/access rules, and API custody risk are explicitly reviewed.

## 2026-07-03 Binance Venue Review

- Binance should be considered as a second execution venue because user capital is larger there.
- Do not replace Bybit first. Run Binance as a separate venue process with isolated state, then cut over later if it performs better.
- Why Binance can help:
  - Deep BTC/ETH/SOL/major alt liquidity.
  - Larger existing seed reduces transfer friction.
  - Useful venue diversification if Bybit API/order routing fails.
  - Can compare fills against Bybit for the same GOT signals.
- Main risks:
  - Region/product availability and account-specific fee tier must be checked inside the account.
  - Binance API has strict request/order limits, timestamp/recvWindow requirements, and unknown-execution handling on `503`; duplicate-order prevention is mandatory.
  - Larger seed increases behavioral risk: venue adapter must enforce per-venue and total-portfolio caps before live orders.
- Current implementation:
  - `exchange_venue_compare.py` compares public Bybit/Binance USD-M books and slippage for the same symbols.
  - `binance_trader.py` implements a Binance USD-M execution adapter with explicit live guard.
  - `trade_router.py` keeps Bybit as the default venue and routes execution/account calls to Binance only when `AUTO_TRADE_EXCHANGE=binance`.
  - `venue_runtime.py` isolates runtime venue, market-data venue, and local state namespace.
  - Binance live orders require both a Binance runtime process and `BINANCE_LIVE_TRADING_ENABLED=true`.
  - Binance state and logs use `_binance` suffix files so Bybit and Binance can run side by side.
  - LaunchAgent templates:
    - Bybit: `com.cryptosignal.plist`, `com.cryptosignal.fast.plist`
    - Binance: `com.cryptosignal.binance.plist`, `com.cryptosignal.binance.fast.plist`
- Required Binance env keys before live use:
  - `BINANCE_API_KEY`
  - `BINANCE_API_SECRET`
  - `BINANCE_LIVE_TRADING_ENABLED=true`
  - For manual single-process Binance runs only: `AUTO_TRADE_EXCHANGE=binance`, `GOT_MARKET_DATA_EXCHANGE=binance`, `GOT_STATE_NAMESPACE=binance`
- Recommended rollout from here:
  1. Keep Bybit LaunchAgents active and add Binance LaunchAgents with small size or live guard disabled.
  2. Confirm Binance OHLCV, balance, position snapshot, SL, TP, and close detection.
  3. Compare Bybit vs Binance journals by venue.
  4. Enable tiny Binance live size only after read-only/account smoke tests pass.
  5. Move larger Binance seed only after expectancy, slippage, and operational reliability beat or match Bybit for at least 2 weeks.
  6. If Binance becomes the only venue, unload the Bybit LaunchAgents and keep the Binance state namespace as-is.

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
