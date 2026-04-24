# trading/tradier_client.py
import httpx
from dataclasses import dataclass


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

    def __init__(self, access_token: str, account_id: str, paper: bool = True) -> None:
        self._account_id = account_id
        base = self._SANDBOX_BASE if paper else self._LIVE_BASE
        self._http = httpx.Client(
            base_url=base,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=10.0,
        )

    def get_clock(self) -> TradierClock:
        resp = self._http.get("/markets/clock")
        _raise_for_status(resp)
        state = resp.json()["clock"]["state"]
        return TradierClock(is_open=(state == "open"))

    def get_all_positions(self) -> list[TradierPosition]:
        resp = self._http.get(f"/accounts/{self._account_id}/positions")
        _raise_for_status(resp)
        return _parse_positions(resp.json())

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        resp = self._http.get(
            "/markets/quotes",
            params={"symbols": ",".join(symbols)},
        )
        _raise_for_status(resp)
        return _parse_quotes(resp.json())

    def get_buying_power(self) -> float:
        """Return available buying power for the account."""
        resp = self._http.get(f"/accounts/{self._account_id}/balances")
        _raise_for_status(resp)
        return _parse_buying_power(resp.json())

    def submit_order(self, symbol: str, side: str, qty: int) -> str:
        """Side: buy | sell | sell_short | buy_to_cover"""
        resp = self._http.post(
            f"/accounts/{self._account_id}/orders",
            data={
                "class": "equity",
                "symbol": symbol,
                "side": side,
                "quantity": str(qty),
                "type": "market",
                "duration": "day",
            },
        )
        _raise_for_status(resp)
        return str(resp.json()["order"]["id"])

    def close_position(self, symbol: str) -> str:
        """Sell long or cover short — looks up current position to determine side/qty."""
        positions = self.get_all_positions()
        pos = next((p for p in positions if p.symbol == symbol), None)
        if pos is None:
            raise ValueError(f"No open position for {symbol}")
        side = "sell" if pos.qty > 0 else "buy_to_cover"
        qty = max(1, abs(round(pos.qty)))
        return self.submit_order(symbol, side, qty)

    def close(self) -> None:
        self._http.close()


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
