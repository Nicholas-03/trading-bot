# trading/alpaca_data_client.py
from datetime import datetime, timezone

import httpx

from trading.tradier_client import MarketBar


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
