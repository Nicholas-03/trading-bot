import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    anthropic_api_key: str
    trade_amount_usd: float
    stop_loss_pct: float
    take_profit_pct: float

    @property
    def paper(self) -> bool:
        return "paper" in self.alpaca_base_url.lower()


def _parse_float(key: str, default: str) -> float:
    raw = os.getenv(key, default)
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Environment variable {key}={raw!r} is not a valid float")


def load_config() -> Config:
    load_dotenv()
    missing = [k for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY") if not os.getenv(k)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    cfg = Config(
        alpaca_api_key=os.environ["ALPACA_API_KEY"],
        alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        trade_amount_usd=_parse_float("TRADE_AMOUNT_USD", "5.0"),
        stop_loss_pct=_parse_float("STOP_LOSS_PCT", "0.05"),
        take_profit_pct=_parse_float("TAKE_PROFIT_PCT", "0.10"),
    )

    if cfg.trade_amount_usd <= 0:
        raise ValueError("TRADE_AMOUNT_USD must be positive")
    if not (0 < cfg.stop_loss_pct < 1):
        raise ValueError("STOP_LOSS_PCT must be between 0 and 1 exclusive")
    if not (0 < cfg.take_profit_pct < 1):
        raise ValueError("TAKE_PROFIT_PCT must be between 0 and 1 exclusive")

    return cfg
