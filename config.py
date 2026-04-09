import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
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
        return "paper" in self.alpaca_base_url


def load_config() -> Config:
    missing = [k for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY") if not os.getenv(k)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return Config(
        alpaca_api_key=os.environ["ALPACA_API_KEY"],
        alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        trade_amount_usd=float(os.getenv("TRADE_AMOUNT_USD", "5.0")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.05")),
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.10")),
    )
