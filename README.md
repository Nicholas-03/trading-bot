# News Trading Bot

Listens to real-time news from Alpaca's WebSocket feed, uses Claude or Gemini to decide whether to buy or sell a stock based on the news, and executes trades via Tradier.

## How it works

1. Connects to Alpaca's news WebSocket and receives live news events.
2. Sends each news headline, summary, and mentioned tickers to the LLM.
3. The LLM returns a `buy`, `sell`, or `hold` decision.
4. On `buy`: places a market order for `TRADE_AMOUNT_USD` worth of the ticker (only during market hours and when sufficient buying power exists).
5. On `sell`: closes the full position for the ticker.
6. Every 30 seconds, checks all open positions and automatically sells if:
   - P&L drops to **-2%** (stop-loss)
   - P&L reaches **+3%** (take-profit)

## Prerequisites

- [Alpaca](https://alpaca.markets) account — used for the real-time news feed only (not for trading); a free account works
- [Tradier](https://developer.tradier.com) account — used for all trading; sandbox is free
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
| `ALPACA_API_KEY` | Alpaca API key ID (news feed only) | required |
| `ALPACA_SECRET_KEY` | Alpaca secret key (news feed only) | required |
| `TRADIER_ACCESS_TOKEN` | Tradier access token | required |
| `TRADIER_ACCOUNT_ID` | Tradier account ID | required |
| `TRADIER_PAPER` | Use Tradier sandbox environment | `true` |
| `LLM_PROVIDER` | LLM to use: `claude` or `gemini` | required |
| `ANTHROPIC_API_KEY` | Anthropic API key (if using Claude) | conditional |
| `ANTHROPIC_MODEL` | Claude model ID | `claude-opus-4-6` |
| `GOOGLE_API_KEY` | Google API key (if using Gemini) | conditional |
| `GEMINI_MODEL` | Gemini model ID | `gemini-2.0-flash` |
| `TRADE_AMOUNT_USD` | Dollar amount per buy order | `5.0` |
| `ALLOW_SHORT` | Enable short selling | `false` |
| `SHORT_QTY` | Shares per short sell order | `1` |
| `STOP_LOSS_PCT` | Stop-loss threshold (percentage, e.g. `2` = 2%) | `2` |
| `TAKE_PROFIT_PCT` | Take-profit threshold (percentage, e.g. `3` = 3%) | `3` |
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

## Testing

Unit tests cover pure logic with no external API calls:

```bash
pytest tests/ -v
```

End-to-end testing is done against Tradier's sandbox. Set `TRADIER_PAPER=true` and use sandbox credentials from the [Tradier Developer portal](https://developer.tradier.com).

## Limitations

- Only stocks with tickers mentioned in the news are eligible for trades.
- One position per ticker at a time (duplicate buy signals are skipped).
- Buys are skipped outside market hours or when buying power is below `TRADE_AMOUNT_USD`.
- For live trading, set `TRADIER_PAPER=false` and use live Tradier credentials.
