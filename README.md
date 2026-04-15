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
data/                   – persisted JSON (trades, equity curve, sessions)
logs/                   – bot.log + PM2 logs
```

## Red flags — when to stop

- 4+ consecutive losses (auto-pause)
- Session loss > $20 (auto-pause)
- Daily loss > $30 (auto-pause)
- Win rate below 45% after 30+ trades (manual review)
- Token prices consistently > $0.85 at entry (no edge)

See the system prompt for the full testing sequence (dry-run → micro-live → normal → optimized).
