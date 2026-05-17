# News Trading Bot

Listens to real-time news from Alpaca's WebSocket feed, uses an LLM to decide whether to buy, short, sell, or hold based on the news, and executes trades via Tradier.

## How it works

1. Connects to Alpaca's news WebSocket and receives live news events.
2. Sends each news headline, summary, and mentioned tickers to the LLM along with current long/short positions.
3. The LLM returns a `buy`, `short`, `sell`, or `hold` decision with a confidence score and expected hold duration.
4. Decisions below `MIN_CONFIDENCE` are skipped.
5. On `buy`: uses Alpaca market data for price checks, places a capped DAY limit entry through Tradier, confirms the actual fill, then places a protective OCO take-profit/stop bracket.
6. On `short`: places a short sell order for `SHORT_QTY` shares of the ticker.
7. On `sell`: closes the full long or short position for the ticker.
8. Every 30 seconds, checks all open positions and automatically closes if:
   - P&L drops to **-2%** (stop-loss)
   - P&L reaches **+3%** (take-profit)
   - The LLM's `hold_hours` window has expired
9. All trades are recorded to a local SQLite analytics database.

## Execution Policy

The bot must use Alpaca for all stock market data used in trading decisions: entry quotes, session open, 1-minute confirmation bars, and live prices for position monitoring. Tradier is used only for brokerage/account actions: orders, positions, balances, account history, and realized gain/loss.

Long entries must not be submitted as Tradier OTOCO entry orders. The required flow is:

1. Read the Alpaca snapshot and use the ask price for buy sizing and the slippage-capped entry limit.
2. Submit a plain Tradier DAY limit buy.
3. Wait for Tradier to confirm the entry fill.
4. Place a protective Tradier OCO bracket using the actual fill price.
5. Record the OCO bracket order ID in analytics.

This policy exists because Tradier sandbox advanced OTOCO entries can remain unfilled/canceled even when the live market appears marketable, especially when mixed with non-Tradier market data. Keeping entry and protective bracket placement separate makes fill failures explicit and prevents the CSX-style missed-entry ambiguity.

See [docs/trading-execution-policy.md](docs/trading-execution-policy.md) for the durable engineering note.

## Prerequisites

- [Alpaca](https://alpaca.markets) account - used for the real-time news feed and stock market data (not order execution); a free account works with the IEX feed
- [Tradier](https://developer.tradier.com) account — used for all trading; sandbox is free
- An OpenAI API key for ChatGPT decisions

## Local development

### 1. Create a virtual environment

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your API keys and settings:

| Variable | Description | Default |
|----------|-------------|---------|
| `ALPACA_API_KEY` | Alpaca API key ID for news and stock market data | required |
| `ALPACA_SECRET_KEY` | Alpaca secret key for news and stock market data | required |
| `TRADIER_ACCESS_TOKEN` | Tradier access token | required |
| `TRADIER_ACCOUNT_ID` | Tradier account ID | required |
| `TRADIER_PAPER` | Use Tradier sandbox environment | `true` |
| `TRADIER_LIVE_TOKEN` | Deprecated/unused; market data comes from Alpaca | optional |
| `ALPACA_DATA_FEED` | Alpaca stock-data feed for quotes, snapshots, and entry-confirmation bars (`iex`, `sip`, `delayed_sip`, `otc`) | `iex` |
| `LLM_PROVIDER` | LLM provider to use | `chatgpt` |
| `OPENAI_API_KEY` | OpenAI API key | required |
| `OPENAI_MODEL` | OpenAI model ID | `gpt-5.4-mini` |
| `TRADE_AMOUNT_USD` | Dollar amount per buy order | `5.0` |
| `ALLOW_SHORT` | Enable short selling | `false` |
| `SHORT_QTY` | Shares per short sell order | `1` |
| `STOP_LOSS_PCT` | Stop-loss threshold (e.g. `2` = 2%) | `2` |
| `TAKE_PROFIT_PCT` | Take-profit threshold (e.g. `3` = 3%) | `3` |
| `MIN_CONFIDENCE` | Minimum LLM confidence (0.0–1.0) to act on a decision | `0.7` |
| `ANALYTICS_DB_PATH` | Path to the SQLite analytics database | `data/trades.db` |
| `TELEGRAM_ENABLED` | Send trade notifications via Telegram | `false` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (if enabled) | conditional |
| `TELEGRAM_CHAT_ID` | Telegram chat ID (if enabled) | conditional |

### 4. Run the bot

```bash
python main.py
```

## Docker deployment (VM)

### 1. Clone the repo and configure

```bash
git clone <repo-url>
cd trading-bot
cp .env.example .env
# Edit .env with real API keys
```

### 2. Start the bot

```bash
docker compose up -d
```

This builds the image and starts the container in the background. The container restarts automatically on crash or VM reboot.

### 3. View logs

```bash
docker compose logs -f
```

### 4. Stop the bot

```bash
docker compose down
```

## Automatic DigitalOcean deployment

The repository includes a GitHub Actions workflow at `.github/workflows/deploy-digitalocean.yml` that deploys the bot to a DigitalOcean Droplet whenever changes are **pushed to `master`**. A local commit alone does not deploy until it is pushed to GitHub.

The workflow connects to the Droplet over SSH, pulls the latest repository changes, runs the test suite, verifies that `main` imports cleanly, rebuilds the Docker Compose service, and recreates the running `trading-bot` container.

Before relying on the automatic deployment, keep the Droplet checkout at `/opt/trading-bot/app` clean. Manual hotfixes in that checkout can block the workflow because it uses `git pull --ff-only origin master`.

Required GitHub repository secrets:

| Secret | Description |
|--------|-------------|
| `DROPLET_HOST` | Droplet IP address or DNS name |
| `DROPLET_USER` | SSH user with access to the app checkout and Docker Compose |
| `DROPLET_SSH_KEY` | Private SSH key used by GitHub Actions to connect to the Droplet |

Default Droplet paths:

| Path | Purpose |
|------|---------|
| `/opt/trading-bot/app` | Git checkout of this repository |
| `/opt/trading-bot/docker-compose.yml` | Production Docker Compose stack |
| `/opt/trading-bot/app/.env` | Production environment file, kept only on the Droplet |

Optional GitHub repository variables can override the defaults: `DROPLET_APP_DIR`, `DROPLET_COMPOSE_DIR`, `DROPLET_SERVICE`, and `DROPLET_BRANCH`.

See [docs/digitalocean-deployment.md](docs/digitalocean-deployment.md) for the full setup guide.

## Analytics

All news events, LLM decisions, and trade executions are stored in a local SQLite database (default: `data/trades.db`). Use `analytics/export_db.py` to dump the database as markdown for LLM analysis:

```bash
python analytics/export_db.py
```

### DigitalOcean dashboard

Production runs on a DigitalOcean Droplet with Docker Compose. The live checkout is in `/opt/trading-bot/app`, and the parent Compose stack in `/opt/trading-bot/docker-compose.yml` builds the app image, mounts the persistent analytics database at `/mnt/trading-bot-data`, and serves the FastAPI dashboard through Caddy with Basic Auth. Deployment is automated by GitHub Actions when changes are pushed to `master`.

## Testing

Unit tests cover pure logic with no external API calls:

```bash
pytest tests/ -v
```

End-to-end testing is done against Tradier's sandbox. Set `TRADIER_PAPER=true` and use sandbox credentials from the [Tradier Developer portal](https://developer.tradier.com).

## Limitations

- Only stocks with tickers mentioned in the news are eligible for trades.
- One long and one short position per ticker at a time (duplicate signals are skipped).
- Buys are skipped outside market hours or when buying power is below `TRADE_AMOUNT_USD`.
- For live trading, set `TRADIER_PAPER=false` and use live Tradier credentials.
