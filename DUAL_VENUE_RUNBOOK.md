# Dual Venue Runbook

This project runs one bot process per execution venue.

## Runtime Model

- Bybit production process
  - `AUTO_TRADE_EXCHANGE=bybit`
  - `GOT_MARKET_DATA_EXCHANGE=bybit`
  - `GOT_STATE_NAMESPACE=bybit`
  - state/log files keep the legacy names:
    - `trade_state.json`
    - `trade_candidates.jsonl`
    - `trade_execution_journal.jsonl`

- Binance production process
  - `AUTO_TRADE_EXCHANGE=binance`
  - `GOT_MARKET_DATA_EXCHANGE=binance`
  - `GOT_STATE_NAMESPACE=binance`
  - state/log files are isolated:
    - `trade_state_binance.json`
    - `trade_candidates_binance.jsonl`
    - `trade_execution_journal_binance.jsonl`
    - `market_radar_state_binance.json`
    - `hyperliquid_radar_state_binance.json`

The core strategy still lives in `main.py`.  Venue-specific account, order,
position, and risk calls go through `trade_router.py`.

## Binance API Setup

Add only the Binance credentials and live guard to `.env`:

```env
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_LIVE_TRADING_ENABLED=true
```

For dual LaunchAgent operation, do not rely on `.env` for
`AUTO_TRADE_EXCHANGE`; the plist files set it explicitly per venue.

Recommended Binance API settings:

- Enable USD-M futures trading.
- Disable withdrawals.
- Use IP whitelist when possible.
- Start with small live size until balance, SL, TP, and close detection are verified.

## LaunchAgents

Existing Bybit agents:

- `com.cryptosignal.plist`
- `com.cryptosignal.fast.plist`

New Binance agents:

- `com.cryptosignal.binance.plist`
- `com.cryptosignal.binance.fast.plist`

The Binance agents write logs to:

- `/tmp/godoftrading_binance.log`
- `/tmp/godoftrading_binance_err.log`
- `/tmp/godoftrading_binance_fast.log`
- `/tmp/godoftrading_binance_fast_err.log`

## Manual Smoke Commands

Read-only market-data check:

```bash
AUTO_TRADE_EXCHANGE=binance GOT_MARKET_DATA_EXCHANGE=binance GOT_STATE_NAMESPACE=binance \
python3 -c "from fetcher import fetch_ohlcv; print(fetch_ohlcv('BTC/USDT','1h',3).tail())"
```

Live-order guard check.  This must refuse orders unless
`BINANCE_LIVE_TRADING_ENABLED=true` is set:

```bash
AUTO_TRADE_EXCHANGE=binance GOT_STATE_NAMESPACE=binance \
python3 -c "from trade_router import active_exchange; print(active_exchange())"
```

## Binance-Only Cutover

When Binance is ready to become the only live venue:

1. Unload or disable the two Bybit LaunchAgents.
2. Keep the two Binance LaunchAgents loaded.
3. Keep Bybit state files archived for reporting/history.
4. Keep `AUTO_TRADE_EXCHANGE=binance` explicit in the Binance plist so manual `.env` edits do not change runtime behavior.
