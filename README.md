# Polymarket BTC 5-Minute Up/Down Trading Bot

Production-ready async Python bot that trades Polymarket's "BTC Up or Down — 5 Minutes"
binary markets using a late-window momentum strategy with a Binance latency edge.

Runs on an Ubuntu 24.04 VPS, managed by PM2. Fully async, maker-only orders,
confidence-scored decisions with every trade logged with its full reason.

## Features

- **Live feeds**: Binance (BTC trades), Polymarket (token prices), Chainlink (price-to-beat)
- **Confidence scoring**: 5-factor score (delta magnitude, time left, trend, volume, token price)
- **Risk manager**: hard session / daily / consecutive-loss limits, auto-cooldowns
- **Maker-only execution**: limit orders with retry ladder — no taker fees
- **Telegram control**: `/dashboard`, `/stats`, `/chart`, `/stop`, `/resume`, `/size`, `/maxloss`
- **Web dashboard**: real-time charts (equity curve, daily PnL, rolling win rate), live trade feed, live window monitor, SSE auto-refresh, mobile-responsive dark theme
- **Dry-run mode**: simulate strategy without placing real orders

## Quick start

```bash
# 1. Clone / copy the project
cd ~/polymarket-5m-bot

# 2. Create venv and install deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Fill in your secrets
cp .env.example .env
nano .env

# 4. Run dry-run first (NO real trades)
python3 bot.py --dry-run

# 5. Once happy, start live with PM2
pm2 start ecosystem.config.js
pm2 save
pm2 startup
```

## Generating Polymarket API credentials

```bash
python3 -c "
from py_clob_client.client import ClobClient
client = ClobClient(host='https://clob.polymarket.com', key='0xYOUR_PRIVATE_KEY', chain_id=137)
creds = client.create_or_derive_api_creds()
print('API Key:', creds.api_key)
print('Secret:', creds.api_secret)
print('Passphrase:', creds.api_passphrase)
"
```

Paste the three values into your `.env` file.

## Telegram setup

1. Message [@BotFather](https://t.me/BotFather), create a bot, grab the token.
2. Start a chat with your new bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` — your chat ID is in the response.
3. Put both values in `.env`.

Available commands:
```
/dashboard   – full overview
/stats       – today / week / all-time summary
/pnl         – quick PnL check
/chart       – PNG performance chart
/trades [n]  – last n trades with reasoning
/today       – today's stats
/history [n] – alias for /trades
/stop        – pause the bot
/resume      – resume the bot
/size <usd>  – change base trade size
/maxloss <usd> – change session loss limit
/config      – show current settings
```

## Web dashboard

After the bot starts, visit:

```
http://YOUR_VPS_IP:8081
```

You'll be prompted for your `DASHBOARD_TOKEN` (from `.env`). The dashboard auto-refreshes via
Server-Sent Events — no manual reload needed. Shows:

- Live window monitor (current BTC, delta, token prices, signal, countdown)
- Today / This Week / All Time stat cards
- Interactive equity curve (Chart.js)
- Daily PnL bar chart
- Rolling 20-trade win rate line
- Live trade feed with full reasoning per entry

Works on mobile.

## Strategy summary

Every 5 minutes a new binary market opens: "Will BTC be UP or DOWN vs the opening
Chainlink price after 5 minutes?". Binance spot leads Chainlink by 1–5 seconds.
We use that gap:

1. Capture the opening Chainlink price (the "price to beat")
2. Watch Binance BTC price live
3. Late in the window (T-60s → T-5s), if BTC is meaningfully off the open
   and the trend is consistent and the target token is still cheap → buy the
   corresponding side with a limit order
4. Hold until resolution (automatic at window close)

Every trade logs: window slug, price to beat, current BTC, delta %, trend, volume,
time remaining, target token, token price, score, decision, expected profit/loss.

## Files

```
bot.py                  – main entry point
config.py               – all settings
core/
  market.py             – window discovery + Gamma metadata
  strategy.py           – confidence scoring + decision
  risk.py               – sizing + limits + cooldowns
  execution.py          – CLOB limit-order placement
  websockets.py         – resilient async WS base class
  backtest/
    replay.py           – pair entry+settled trades, bucket EV/win-rate
    binance_history.py  – cached 1s/1m kline fetcher
    simulate.py         – replay strategy.decide() over historical klines
services/
  binance_feed.py       – Binance BTC trade stream + volume/trend classifier
  polymarket_feed.py    – Polymarket token price feed
  chainlink_feed.py     – Chainlink price-to-beat via RTDS
utils/
  logger.py             – trade logging with full reasoning
  pnl_tracker.py        – session/daily/all-time/equity curve
  chart_generator.py    – matplotlib chart for /chart command
  telegram.py           – Notifier + CommandBot
dashboard/
  server.py             – aiohttp server + SSE + REST API
  index.html            – single-file dashboard (Chart.js, SSE, dark theme)
scripts/
  backtest.py           – CLI: `replay` and `simulate` modes
data/                   – persisted JSON (trades, equity curve, sessions)
  cache/binance_klines/ – on-disk kline cache for backtest simulator
logs/                   – bot.log + PM2 logs
tests/                  – unittest suite (run: python3 -m unittest discover -s tests)
```

## Backtesting

Two modes — both invoked via `scripts/backtest.py`:

### 1. Replay (validates strategy against your live trades)

Pairs every `phase=entry` with its `phase=settled` record in `data/trades.json`,
buckets by the same dimensions the strategy scores on, and reports the realized
**win rate vs break-even** per bucket. Bleeding tiers (negative edge) and strong
tiers (positive edge) are highlighted as tuning suggestions.

```bash
python3 scripts/backtest.py replay
# Or analyze a different file:
python3 scripts/backtest.py replay --trades-file backups/trades-2026-04.json
```

The break-even win rate for each bucket is the average entry price
(e.g. an avg entry of $0.55 needs a 55% win rate just to break even).
The "edge" column is `realized_win_rate − break_even_win_rate` in
percentage points. Anything < 0pp leaks money in that bucket; tighten or
exclude it. Anything ≥ +4pp is worth upsizing.

### 2. Simulate (replays strategy over historical Binance klines)

Fetches BTCUSDT 1-second klines for any UTC date range (cached on disk)
and runs `core.strategy.decide()` over every 5-minute window. This measures
the **upper bound of the signal edge** — Polymarket order-book history is
not exposed by the venue, so the simulator uses a synthetic token price
(default $0.50) to bypass the price gate. Real live performance will
always be lower than simulated because of slippage and the real price gate.

```bash
# One day, 1s resolution, default $0.50 token price
python3 scripts/backtest.py simulate --since 2026-04-29 --until 2026-04-30

# Stress-test what happens if all entries pay $0.55
python3 scripts/backtest.py simulate --since 2026-04-23 --until 2026-04-30 \
    --token-price 0.55

# Faster pass with 1m klines (less precise but ~60× fewer rows)
python3 scripts/backtest.py simulate --since 2026-04-01 --until 2026-04-30 \
    --interval 1m
```

Klines are cached at `data/cache/binance_klines/btcusdt_1s_YYYYMMDD.json`.
Re-running the same date range is free.

The output reports overall win rate, edge, and per-bucket breakdowns by
confidence and |delta_pct| at entry — use it to validate that the scoring
tiers in `core/strategy.py:67-144` actually match observed reality before
you go live with new parameters.

### Workflow for tuning

1. Run `replay` on `data/trades.json` once you have ≥50 settled trades.
   Identify bleeding buckets.
2. Run `simulate` over 1–4 weeks of historical data to confirm the
   bleeding tiers are systemic (not just bad luck on that sample).
3. Edit `core/strategy.py` (tier cutoffs in `_dynamic_max_price` and
   `calculate_confidence`) to exclude the bleeding tiers.
4. Re-run `simulate` to confirm the change improved overall edge.
5. Deploy and let `replay` validate against another batch of live trades.

## Red flags — when to stop

- 4+ consecutive losses (auto-pause)
- Session loss > $20 (auto-pause)
- Daily loss > $30 (auto-pause)
- Win rate below 45% after 30+ trades (manual review)
- Token prices consistently > $0.85 at entry (no edge)

See the system prompt for the full testing sequence (dry-run → micro-live → normal → optimized).
