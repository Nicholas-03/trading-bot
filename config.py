# config.py
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_data_feed: str
    tradier_access_token: str
    tradier_account_id: str
    tradier_paper: bool
    tradier_live_token: str
    openai_api_key: str
    openai_model: str
    llm_provider: str
    trade_amount_usd: float
    short_qty: int
    allow_short: bool
    stop_loss_pct: float
    take_profit_pct: float
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    analytics_db_path: str
    min_confidence: float
    max_slippage_pct: float
    extended_move_low_price_pct: float
    extended_move_any_pct: float
    entry_confirmation_enabled: bool
    entry_confirmation_lookback_minutes: int
    entry_confirmation_trend_minutes: int
    entry_confirmation_max_fade_pct: float
    entry_confirmation_max_quote_premium_pct: float
    fast_fail_enabled: bool
    fast_fail_minutes: int
    fast_fail_loss_pct: float
    fast_fail_min_favorable_pct: float
    news_stale_hours: float


def _parse_float(key: str, default: str) -> float:
    raw = os.getenv(key, default)
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Environment variable {key}={raw!r} is not a valid float")


def _parse_bool(key: str, default: str) -> bool:
    return os.getenv(key, default).lower() in ("true", "1", "yes")


def load_config() -> Config:
    load_dotenv()

    provider = os.getenv("LLM_PROVIDER", "chatgpt").lower()
    if provider != "chatgpt":
        raise ValueError(f"LLM_PROVIDER must be 'chatgpt', got {provider!r}")

    # ALPACA_API_KEY/SECRET_KEY are used by NewsDataStream only, not trading.
    required = [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "TRADIER_ACCESS_TOKEN",
        "TRADIER_ACCOUNT_ID",
        "OPENAI_API_KEY",
    ]
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
        alpaca_data_feed=os.getenv("ALPACA_DATA_FEED", "iex").lower(),
        tradier_access_token=os.environ["TRADIER_ACCESS_TOKEN"],
        tradier_account_id=os.environ["TRADIER_ACCOUNT_ID"],
        tradier_paper=os.getenv("TRADIER_PAPER", "true").lower() in ("true", "1", "yes"),
        tradier_live_token=os.getenv("TRADIER_LIVE_TOKEN", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        llm_provider=provider,
        trade_amount_usd=_parse_float("TRADE_AMOUNT_USD", "5.0"),
        short_qty=int(os.getenv("SHORT_QTY", "1")),
        allow_short=os.getenv("ALLOW_SHORT", "true").lower() in ("true", "1", "yes"),
        stop_loss_pct=_parse_float("STOP_LOSS_PCT", "2.0") / 100,
        take_profit_pct=_parse_float("TAKE_PROFIT_PCT", "3.0") / 100,
        telegram_enabled=telegram_enabled,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        analytics_db_path=os.getenv("ANALYTICS_DB_PATH", "data/trades.db"),
        min_confidence=_parse_float("MIN_CONFIDENCE", "0.7"),
        max_slippage_pct=_parse_float("MAX_SLIPPAGE_PCT", "0.5") / 100,
        extended_move_low_price_pct=_parse_float("EXTENDED_MOVE_LOW_PRICE_PCT", "15.0") / 100,
        extended_move_any_pct=_parse_float("EXTENDED_MOVE_ANY_PCT", "10.0") / 100,
        entry_confirmation_enabled=_parse_bool("ENTRY_CONFIRMATION_ENABLED", "true"),
        entry_confirmation_lookback_minutes=int(os.getenv("ENTRY_CONFIRMATION_LOOKBACK_MINUTES", "8")),
        entry_confirmation_trend_minutes=int(os.getenv("ENTRY_CONFIRMATION_TREND_MINUTES", "3")),
        entry_confirmation_max_fade_pct=_parse_float("ENTRY_CONFIRMATION_MAX_FADE_PCT", "1.5") / 100,
        entry_confirmation_max_quote_premium_pct=_parse_float("ENTRY_CONFIRMATION_MAX_QUOTE_PREMIUM_PCT", "1.0") / 100,
        fast_fail_enabled=_parse_bool("FAST_FAIL_ENABLED", "true"),
        fast_fail_minutes=int(os.getenv("FAST_FAIL_MINUTES", "5")),
        fast_fail_loss_pct=_parse_float("FAST_FAIL_LOSS_PCT", "1.5") / 100,
        fast_fail_min_favorable_pct=_parse_float("FAST_FAIL_MIN_FAVORABLE_PCT", "0.25") / 100,
        news_stale_hours=_parse_float("NEWS_STALE_HOURS", "2.0"),
    )

    if cfg.trade_amount_usd <= 0:
        raise ValueError("TRADE_AMOUNT_USD must be positive")
    if cfg.short_qty <= 0:
        raise ValueError("SHORT_QTY must be a positive integer")
    if not (0 < cfg.stop_loss_pct < 1):
        raise ValueError("STOP_LOSS_PCT must be between 0 and 100 exclusive (e.g. 2 = 2%)")
    if not (0 < cfg.take_profit_pct < 1):
        raise ValueError("TAKE_PROFIT_PCT must be between 0 and 100 exclusive (e.g. 3 = 3%)")
    if not (0.0 <= cfg.min_confidence <= 1.0):
        raise ValueError("MIN_CONFIDENCE must be between 0.0 and 1.0")
    if not (0.0 < cfg.max_slippage_pct < 0.10):
        raise ValueError("MAX_SLIPPAGE_PCT must be between 0 and 10 (exclusive)")
    if not (0.0 < cfg.extended_move_low_price_pct < 1.0):
        raise ValueError("EXTENDED_MOVE_LOW_PRICE_PCT must be between 0 and 100 exclusive")
    if not (0.0 < cfg.extended_move_any_pct < 1.0):
        raise ValueError("EXTENDED_MOVE_ANY_PCT must be between 0 and 100 exclusive")
    if cfg.entry_confirmation_lookback_minutes < 3:
        raise ValueError("ENTRY_CONFIRMATION_LOOKBACK_MINUTES must be at least 3")
    if cfg.entry_confirmation_trend_minutes < 1:
        raise ValueError("ENTRY_CONFIRMATION_TREND_MINUTES must be at least 1")
    if not (0.0 < cfg.entry_confirmation_max_fade_pct < 1.0):
        raise ValueError("ENTRY_CONFIRMATION_MAX_FADE_PCT must be between 0 and 100 exclusive")
    if not (0.0 < cfg.entry_confirmation_max_quote_premium_pct < 1.0):
        raise ValueError("ENTRY_CONFIRMATION_MAX_QUOTE_PREMIUM_PCT must be between 0 and 100 exclusive")
    if cfg.fast_fail_minutes < 1:
        raise ValueError("FAST_FAIL_MINUTES must be at least 1")
    if not (0.0 < cfg.fast_fail_loss_pct < 1.0):
        raise ValueError("FAST_FAIL_LOSS_PCT must be between 0 and 100 exclusive")
    if not (0.0 <= cfg.fast_fail_min_favorable_pct < 1.0):
        raise ValueError("FAST_FAIL_MIN_FAVORABLE_PCT must be between 0 and 100 exclusive")
    if cfg.news_stale_hours <= 0:
        raise ValueError("NEWS_STALE_HOURS must be positive")
    if cfg.alpaca_data_feed not in ("iex", "sip", "delayed_sip", "otc"):
        raise ValueError("ALPACA_DATA_FEED must be one of: iex, sip, delayed_sip, otc")

    return cfg
