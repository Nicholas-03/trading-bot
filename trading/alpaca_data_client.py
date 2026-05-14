# trading/alpaca_data_client.py
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from trading.tradier_client import MarketBar


@dataclass(frozen=True)
class AlpacaSnapshotPrice:
    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    open: float | None
    close: float | None

    @property
    def entry_price(self) -> float | None:
        """Price used for buy sizing/slippage checks. Prefer the ask for entries."""
        if self.ask is not None:
            return self.ask
        if self.last is not None:
            return self.last
        return self.latest_price

    @property
    def latest_price(self) -> float | None:
        """Price used for monitoring/estimates. Prefer last trade, then midpoint."""
        if self.last is not None:
            return self.last
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        if self.ask is not None:
            return self.ask
        if self.bid is not None:
            return self.bid
        return self.close


class AlpacaMarketDataClient:
    _BASE_URL = "https://data.alpaca.markets"

    def __init__(self, api_key: str, secret_key: str, feed: str = "iex") -> None:
        self._feed = feed
        self._http = httpx.Client(
            base_url=self._BASE_URL,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
        )

    def get_intraday_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1Min",
    ) -> list[MarketBar]:
        """Return intraday OHLCV bars from Alpaca's stock market-data API."""
        resp = self._http.get(
            f"/v2/stocks/{symbol}/bars",
            params={
                "timeframe": timeframe,
                "start": _format_rfc3339(start),
                "end": _format_rfc3339(end),
                "adjustment": "raw",
                "feed": self._feed,
            },
        )
        resp.raise_for_status()
        return _parse_alpaca_bars(resp.json())

    def get_snapshots(self, symbols: list[str]) -> dict[str, AlpacaSnapshotPrice]:
        """Return Alpaca stock snapshots keyed by symbol."""
        clean_symbols = [s.upper() for s in symbols if s]
        if not clean_symbols:
            return {}
        resp = self._http.get(
            "/v2/stocks/snapshots",
            params={
                "symbols": ",".join(clean_symbols),
                "feed": self._feed,
            },
        )
        resp.raise_for_status()
        return _parse_alpaca_snapshots(resp.json())

    def get_quote_with_open(self, symbol: str) -> tuple[float, float | None] | None:
        """Return (entry_price, session_open) using Alpaca market data only."""
        snapshot = self.get_snapshots([symbol]).get(symbol.upper())
        if snapshot is None or snapshot.entry_price is None:
            return None
        return snapshot.entry_price, snapshot.open

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        """Return latest monitor prices using Alpaca market data only."""
        result: dict[str, float] = {}
        for symbol, snapshot in self.get_snapshots(symbols).items():
            price = snapshot.latest_price
            if price is not None:
                result[symbol] = price
        return result

    def close(self) -> None:
        self._http.close()


def _format_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _to_positive_float(value) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_alpaca_bars(data: dict) -> list[MarketBar]:
    result: list[MarketBar] = []
    raw_bars = data.get("bars")
    if not isinstance(raw_bars, list):
        return result

    for item in raw_bars:
        if not isinstance(item, dict):
            continue
        ts = item.get("t")
        open_price = _to_positive_float(item.get("o"))
        high = _to_positive_float(item.get("h"))
        low = _to_positive_float(item.get("l"))
        close = _to_positive_float(item.get("c"))
        if not ts or open_price is None or high is None or low is None or close is None:
            continue
        volume = _to_positive_float(item.get("v"))
        result.append(MarketBar(str(ts), open_price, high, low, close, volume))

    return sorted(result, key=lambda bar: bar.time)


def _parse_alpaca_snapshots(data: dict) -> dict[str, AlpacaSnapshotPrice]:
    raw = data.get("snapshots")
    if raw is None:
        raw = data
    if not isinstance(raw, dict):
        return {}

    result: dict[str, AlpacaSnapshotPrice] = {}
    for symbol, item in raw.items():
        if not isinstance(item, dict):
            continue
        latest_quote = item.get("latestQuote") if isinstance(item.get("latestQuote"), dict) else {}
        latest_trade = item.get("latestTrade") if isinstance(item.get("latestTrade"), dict) else {}
        daily_bar = item.get("dailyBar") if isinstance(item.get("dailyBar"), dict) else {}

        parsed = AlpacaSnapshotPrice(
            symbol=str(symbol).upper(),
            bid=_to_positive_float(latest_quote.get("bp")),
            ask=_to_positive_float(latest_quote.get("ap")),
            last=_to_positive_float(latest_trade.get("p")),
            open=_to_positive_float(daily_bar.get("o")),
            close=_to_positive_float(daily_bar.get("c")),
        )
        if parsed.entry_price is not None or parsed.latest_price is not None:
            result[parsed.symbol] = parsed
    return result
