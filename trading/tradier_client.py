# trading/tradier_client.py
import time
import httpx
from dataclasses import dataclass

_RETRYABLE_STATUSES = {429, 502, 503, 504}
_RETRY_DELAYS = (1.0, 2.0, 4.0)  # seconds between attempts 1→2, 2→3, 3→4


@dataclass
class TradierClock:
    is_open: bool


@dataclass
class TradierPosition:
    symbol: str
    qty: float       # positive = long, negative = short
    cost_basis: float  # total cost basis (dollars), not per-share


class TradierClient:
    _LIVE_BASE = "https://api.tradier.com/v1"
    _SANDBOX_BASE = "https://sandbox.tradier.com/v1"

    def __init__(
        self,
        access_token: str,
        account_id: str,
        paper: bool = True,
        quote_token: str | None = None,
    ) -> None:
        self._account_id = account_id
        base = self._SANDBOX_BASE if paper else self._LIVE_BASE
        self._http = httpx.Client(
            base_url=base,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
        # When paper trading, optionally use a live-account token for real-time quotes.
        # Sandbox quotes are 15-min delayed; the live /markets/quotes endpoint is real-time.
        self._quote_http: httpx.Client | None = None
        if paper and quote_token:
            self._quote_http = httpx.Client(
                base_url=self._LIVE_BASE,
                headers={
                    "Authorization": f"Bearer {quote_token}",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            )

    def get_clock(self) -> TradierClock:
        resp = self._request("GET", "/markets/clock")
        state = resp.json()["clock"]["state"]
        return TradierClock(is_open=(state == "open"))

    def get_all_positions(self) -> list[TradierPosition]:
        resp = self._request("GET", f"/accounts/{self._account_id}/positions")
        return _parse_positions(resp.json())

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        resp = self._request(
            "GET",
            "/markets/quotes",
            http=self._quote_http or self._http,
            params={"symbols": ",".join(symbols)},
        )
        return _parse_quotes(resp.json())

    def get_quotes_with_open(self, symbols: list[str]) -> dict[str, tuple[float, float | None]]:
        """Return {symbol: (last_price, session_open_price)} for each symbol."""
        if not symbols:
            return {}
        resp = self._request(
            "GET",
            "/markets/quotes",
            http=self._quote_http or self._http,
            params={"symbols": ",".join(symbols)},
        )
        return _parse_quotes_with_open(resp.json())

    def get_buying_power(self) -> float:
        """Return available buying power for the account."""
        resp = self._request("GET", f"/accounts/{self._account_id}/balances")
        return _parse_buying_power(resp.json())

    def submit_order(self, symbol: str, side: str, qty: int, limit_price: float | None = None) -> str:
        """Side: buy | sell | sell_short | buy_to_cover. limit_price=None uses a market order."""
        data: dict[str, str] = {
            "class": "equity",
            "symbol": symbol,
            "side": side,
            "quantity": str(qty),
            "type": "market" if limit_price is None else "limit",
            "duration": "day",
        }
        if limit_price is not None:
            data["price"] = f"{limit_price:.2f}"
        resp = self._request(
            "POST",
            f"/accounts/{self._account_id}/orders",
            data=data,
        )
        return str(resp.json()["order"]["id"])

    def submit_otoco_order(
        self,
        symbol: str,
        qty: int,
        tp_price: float,
        sl_price: float,
        entry_limit: float | None = None,
    ) -> str:
        """Place a bracket (OTOCO) order: entry + take-profit limit + stop-loss stop.

        entry_limit=None uses a market entry; a float value uses a limit entry.
        The TP/SL legs are GTC so they persist until triggered.
        """
        data: dict[str, str] = {
            "class": "otoco",
            "symbol[0]": symbol,
            "side[0]": "buy",
            "quantity[0]": str(qty),
            "type[0]": "market" if entry_limit is None else "limit",
            "duration[0]": "day",
            "symbol[1]": symbol,
            "side[1]": "sell",
            "quantity[1]": str(qty),
            "type[1]": "limit",
            "price[1]": f"{tp_price:.2f}",
            "duration[1]": "gtc",
            "symbol[2]": symbol,
            "side[2]": "sell",
            "quantity[2]": str(qty),
            "type[2]": "stop",
            "stop[2]": f"{sl_price:.2f}",
            "duration[2]": "gtc",
        }
        if entry_limit is not None:
            data["price[0]"] = f"{entry_limit:.2f}"
        resp = self._request("POST", f"/accounts/{self._account_id}/orders", data=data)
        return str(resp.json()["order"]["id"])

    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order by ID."""
        self._request("DELETE", f"/accounts/{self._account_id}/orders/{order_id}")

    def close_position(self, symbol: str) -> str:
        """Sell long or cover short — looks up current position to determine side/qty."""
        positions = self.get_all_positions()
        pos = next((p for p in positions if p.symbol == symbol), None)
        if pos is None:
            raise ValueError(f"No open position for {symbol}")
        side = "sell" if pos.qty > 0 else "buy_to_cover"
        qty = max(1, abs(round(pos.qty)))
        return self.submit_order(symbol, side, qty)

    def get_order(self, order_id: str) -> tuple[str, float | None]:
        """Return (status, avg_fill_price) for an order."""
        resp = self._request("GET", f"/accounts/{self._account_id}/orders/{order_id}")
        return _parse_order_status(resp.json())

    def _request(self, method: str, path: str, *, http: httpx.Client | None = None, **kwargs) -> httpx.Response:
        """Execute an HTTP request with automatic retry on transient server errors."""
        client = http or self._http
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            resp = client.request(method, path, **kwargs)
            if resp.status_code not in _RETRYABLE_STATUSES:
                _raise_for_status(resp)
                return resp
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code} {resp.text}",
                request=resp.request,
                response=resp,
            )
            if delay is None:
                break
            time.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def close(self) -> None:
        self._http.close()
        if self._quote_http:
            self._quote_http.close()


def _raise_for_status(resp: httpx.Response) -> None:
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise httpx.HTTPStatusError(
            f"{exc.response.status_code} {exc.response.text}",
            request=exc.request,
            response=exc.response,
        ) from exc


def _parse_positions(data: dict) -> list[TradierPosition]:
    """Parse Tradier positions response. Handles null, single object, and array."""
    raw = data.get("positions")
    if raw is None or raw == "null":
        return []
    pos_data = raw.get("position")
    if pos_data is None:
        return []
    if isinstance(pos_data, dict):
        pos_data = [pos_data]
    return [
        TradierPosition(
            symbol=p["symbol"],
            qty=float(p["quantity"]),
            cost_basis=float(p["cost_basis"]),
        )
        for p in pos_data
    ]


def _parse_quotes(data: dict) -> dict[str, float]:
    """Parse Tradier quotes response. Handles single quote and array."""
    raw = data.get("quotes", {})
    quote_data = raw.get("quote")
    if quote_data is None:
        return {}
    if isinstance(quote_data, dict):
        quote_data = [quote_data]
    return {
        q["symbol"]: float(q["last"])
        for q in quote_data
        if q.get("last") is not None
    }


def _parse_quotes_with_open(data: dict) -> dict[str, tuple[float, float | None]]:
    """Parse Tradier quotes response into {symbol: (last, open_price)} pairs."""
    raw = data.get("quotes", {})
    quote_data = raw.get("quote")
    if quote_data is None:
        return {}
    if isinstance(quote_data, dict):
        quote_data = [quote_data]
    result: dict[str, tuple[float, float | None]] = {}
    for q in quote_data:
        last = q.get("last")
        if last is None:
            continue
        open_price = q.get("open")
        result[q["symbol"]] = (float(last), float(open_price) if open_price is not None else None)
    return result


def _parse_order_status(data: dict) -> tuple[str, float | None]:
    """Parse Tradier order response. Returns (status, avg_fill_price)."""
    order = data.get("order", {})
    status = str(order.get("status", "unknown"))
    avg_fill = order.get("avg_fill_price")
    fill_price = float(avg_fill) if avg_fill else None
    return status, fill_price


def _parse_buying_power(data: dict) -> float:
    """Parse Tradier balances response. Supports margin, PDT, and cash accounts."""
    balances = data.get("balances", {})
    if "margin" in balances:
        m = balances["margin"]
        return float(m.get("buying_power") or m["stock_buying_power"])
    if "pdt" in balances:
        p = balances["pdt"]
        return float(p.get("buying_power") or p["stock_buying_power"])
    if "cash" in balances:
        return float(balances["cash"]["cash_available"])
    raise ValueError("Cannot determine buying power from balances response")
