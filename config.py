# config.py
import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    alpaca_api_key: str
    alpaca_secret_key: str
    tradier_access_token: str
    tradier_account_id: str
    tradier_paper: bool
    anthropic_api_key: str
    anthropic_model: str
    google_api_key: str
    gemini_model: str
    llm_provider: str
    trade_amount_usd: float
    short_qty: int
    allow_short: bool
    stop_loss_pct: float
    take_profit_pct: float
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str


def _parse_float(key: str, default: str) -> float:
    raw = os.getenv(key, default)
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Environment variable {key}={raw!r} is not a valid float")


def load_config() -> Config:
    load_dotenv()

    provider = os.getenv("LLM_PROVIDER", "claude").lower()
    if provider not in ("claude", "gemini"):
        raise ValueError(f"LLM_PROVIDER must be 'claude' or 'gemini', got {provider!r}")

    # ALPACA_API_KEY/SECRET_KEY are still required — used by NewsDataStream (news feed only, not trading)
    required = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TRADIER_ACCESS_TOKEN", "TRADIER_ACCOUNT_ID"]
    if provider == "claude":
        required.append("ANTHROPIC_API_KEY")
    else:
        required.append("GOOGLE_API_KEY")

    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    telegram_enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes")
    if telegram_enabled:
        telegram_missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(k)]
        if telegram_missing:
            raise ValueError(f"TELEGRAM_ENABLED=true but missing: {', '.join(telegram_missing)}")

    cfg = Config(
        alpaca_api_key=os.environ["ALPACA_API_KEY"],
        alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
        tradier_access_token=os.environ["TRADIER_ACCESS_TOKEN"],
        tradier_account_id=os.environ["TRADIER_ACCOUNT_ID"],
        tradier_paper=os.getenv("TRADIER_PAPER", "true").lower() in ("true", "1", "yes"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6"),
        google_api_key=os.getenv("GOOGLE_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        llm_provider=provider,
        trade_amount_usd=_parse_float("TRADE_AMOUNT_USD", "5.0"),
        short_qty=int(os.getenv("SHORT_QTY", "1")),
        allow_short=os.getenv("ALLOW_SHORT", "true").lower() in ("true", "1", "yes"),
        stop_loss_pct=_parse_float("STOP_LOSS_PCT", "2.0") / 100,
        take_profit_pct=_parse_float("TAKE_PROFIT_PCT", "3.0") / 100,
        telegram_enabled=telegram_enabled,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    if cfg.trade_amount_usd <= 0:
        raise ValueError("TRADE_AMOUNT_USD must be positive")
    if cfg.short_qty <= 0:
        raise ValueError("SHORT_QTY must be a positive integer")
    if not (0 < cfg.stop_loss_pct < 1):
        raise ValueError("STOP_LOSS_PCT must be between 0 and 100 exclusive (e.g. 2 = 2%)")
    if not (0 < cfg.take_profit_pct < 1):
        raise ValueError("TAKE_PROFIT_PCT must be between 0 and 100 exclusive (e.g. 3 = 3%)")

    return cfg
