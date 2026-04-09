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

- Python 3.11+
- An [Alpaca](https://alpaca.markets) account (paper trading is free)
- An [Anthropic](https://console.anthropic.com) account with API access

## Setup

### 1. Get Alpaca paper trading API keys

1. Sign up or log in at [alpaca.markets](https://alpaca.markets)
2. Switch to **Paper Trading** mode in the top-left dropdown
3. Go to **Overview → API Keys** → generate a new key pair
4. Copy the **API Key ID** and **Secret Key**

### 2. Get a Claude API key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Navigate to **API Keys** → **Create Key**
3. Copy the key

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

| Variable | Description | Default |
|----------|-------------|---------|
| `ALPACA_API_KEY` | Alpaca API key ID | required |
| `ALPACA_SECRET_KEY` | Alpaca secret key | required |
| `ALPACA_BASE_URL` | Alpaca base URL | `https://paper-api.alpaca.markets` |
| `ANTHROPIC_API_KEY` | Anthropic API key | required |
| `TRADE_AMOUNT_USD` | Dollar amount per buy order | `5.0` |
| `STOP_LOSS_PCT` | Stop-loss threshold (decimal) | `0.05` (5%) |
| `TAKE_PROFIT_PCT` | Take-profit threshold (decimal) | `0.10` (10%) |

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Run the bot

```bash
python main.py
```

The bot runs until stopped (`Ctrl+C`). All decisions and orders are logged to stdout.

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
