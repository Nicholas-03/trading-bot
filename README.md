# Alpaca News Trading Bot

Listens to real-time news from Alpaca's WebSocket feed, uses Claude (Anthropic) to decide whether to buy or sell a stock based on the news, and executes trades on Alpaca.

## How it works

1. Connects to Alpaca's news WebSocket and receives live news events.
2. Sends each news headline, summary, and mentioned tickers to Claude.
3. Claude returns a `buy`, `sell`, or `hold` decision.
4. On `buy`: places a $5 notional market order for the ticker.
5. On `sell`: closes the full position for the ticker.
6. Every 30 seconds, checks all open positions and automatically sells if:
   - P&L drops to **-5%** (stop-loss)
   - P&L reaches **+10%** (take-profit)

## Prerequisites

- [Alpaca](https://alpaca.markets) account (paper trading is free)
- An LLM API key — either [Anthropic](https://console.anthropic.com) or [Google AI](https://aistudio.google.com)

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
| `ALPACA_API_KEY` | Alpaca API key ID | required |
| `ALPACA_SECRET_KEY` | Alpaca secret key | required |
| `ALPACA_BASE_URL` | Alpaca base URL | `https://paper-api.alpaca.markets` |
| `LLM_PROVIDER` | LLM to use: `chatgpt`, `claude`, or `gemini` | `chatgpt` |
| `OPENAI_API_KEY` | OpenAI API key (if using ChatGPT) | conditional |
| `OPENAI_MODEL` | OpenAI model ID | `gpt-5.4-mini` |
| `ANTHROPIC_API_KEY` | Anthropic API key (if using Claude) | conditional |
| `ANTHROPIC_MODEL` | Claude model ID | `claude-opus-4-6` |
| `GOOGLE_API_KEY` | Google API key (if using Gemini) | conditional |
| `GEMINI_MODEL` | Gemini model ID | `gemini-2.0-flash` |
| `TRADE_AMOUNT_USD` | Dollar amount per buy order | `5.0` |
| `ALLOW_SHORT` | Enable short selling | `false` |
| `SHORT_QTY` | Shares per short sell order | `1` |
| `STOP_LOSS_PCT` | Stop-loss threshold (decimal) | `0.05` (5%) |
| `TAKE_PROFIT_PCT` | Take-profit threshold (decimal) | `0.10` (10%) |
| `ANALYTICS_DB_PATH` | SQLite path for the analytics dashboard | `data/trades.db` |

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

This builds the image and starts the bot plus the FastAPI analytics dashboard in the same container. The dashboard listens on port `8080`, and analytics data is written to `ANALYTICS_DB_PATH`. The container restarts automatically on crash or VM reboot.

### 3. View logs

```bash
docker compose logs -f
```

### 4. Stop the bot

```bash
docker compose down
```

## DigitalOcean deployment

GitHub Actions deploys this branch to the DigitalOcean Droplet on every push to `alpaca`. The workflow is `.github/workflows/deploy-digitalocean.yml`; it SSHes to the Droplet, checks out/pulls `origin/alpaca` in `/opt/trading-bot/app`, runs `python -m pytest tests/ -v`, verifies `python -c "import main; print('OK')"`, then rebuilds/recreates the Docker Compose `trading-bot` service.

The production analytics DB is persisted outside the app checkout at `/mnt/trading-bot-data` and mounted into the container as `/app/data`.

## Testing

All testing is done against Alpaca's paper trading environment. After starting the bot, monitor the [paper trading dashboard](https://app.alpaca.markets/paper/dashboard/overview) to see orders placed in response to news.

To run the unit tests for pure logic (no external calls):

```bash
pytest tests/ -v
```

## Limitations

- The bot runs only while the process is active — there is no scheduling or market-hours gating.
- Only stocks with tickers mentioned in the news are eligible for trades.
- One position per ticker at a time (duplicate buy signals are skipped).
- For live trading, change `ALPACA_BASE_URL` to `https://api.alpaca.markets` and use live API keys.
