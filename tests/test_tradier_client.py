# tests/test_tradier_client.py
import httpx
import pytest
from datetime import date
from trading.tradier_client import (
    TradierClient,
    TradierActivity,
    TradierGainLoss,
    TradierPosition,
    _parse_account_history,
    _parse_account_orders,
    _parse_buying_power,
    _parse_gain_loss,
    _parse_market_bars,
    _parse_order_status,
    _parse_positions,
    _parse_quotes,
    _parse_quotes_with_open,
)


def test_parse_positions_null_string():
    assert _parse_positions({"positions": "null"}) == []


def test_parse_positions_none_value():
    assert _parse_positions({"positions": None}) == []


def test_parse_positions_missing_key():
    assert _parse_positions({}) == []


def test_parse_positions_single_object():
    data = {
        "positions": {
            "position": {"symbol": "AAPL", "quantity": 2.0, "cost_basis": 300.0}
        }
    }
    result = _parse_positions(data)
    assert result == [TradierPosition(symbol="AAPL", qty=2.0, cost_basis=300.0)]


def test_parse_positions_multiple():
    data = {
        "positions": {
            "position": [
                {"symbol": "AAPL", "quantity": 2.0, "cost_basis": 300.0},
                {"symbol": "MSFT", "quantity": -1.0, "cost_basis": 400.0},
            ]
        }
    }
    result = _parse_positions(data)
    assert len(result) == 2
    assert result[0].symbol == "AAPL"
    assert result[1].qty == -1.0


def test_parse_positions_long_positive_short_negative():
    data = {
        "positions": {
            "position": {"symbol": "TSLA", "quantity": -3.0, "cost_basis": 900.0}
        }
    }
    result = _parse_positions(data)
    assert result[0].qty < 0


def test_parse_quotes_single():
    data = {"quotes": {"quote": {"symbol": "AAPL", "last": 175.5}}}
    assert _parse_quotes(data) == {"AAPL": 175.5}


def test_parse_quotes_multiple():
    data = {
        "quotes": {
            "quote": [
                {"symbol": "AAPL", "last": 175.5},
                {"symbol": "MSFT", "last": 420.0},
            ]
        }
    }
    assert _parse_quotes(data) == {"AAPL": 175.5, "MSFT": 420.0}


def test_parse_quotes_empty_quotes():
    assert _parse_quotes({"quotes": {}}) == {}


def test_parse_quotes_missing_key():
    assert _parse_quotes({}) == {}


def test_parse_quotes_null_string():
    assert _parse_quotes({"quotes": "null"}) == {}


def test_parse_quotes_skips_string_null_last():
    assert _parse_quotes({"quotes": {"quote": {"symbol": "AAPL", "last": "null"}}}) == {}


def test_parse_buying_power_margin():
    data = {"balances": {"margin": {"buying_power": 5000.0}}}
    assert _parse_buying_power(data) == 5000.0


def test_parse_buying_power_pdt():
    data = {"balances": {"pdt": {"buying_power": 3000.0}}}
    assert _parse_buying_power(data) == 3000.0


def test_parse_buying_power_cash():
    data = {"balances": {"cash": {"cash_available": 1500.0}}}
    assert _parse_buying_power(data) == 1500.0


def test_parse_buying_power_missing_balances_key():
    with pytest.raises(ValueError, match="buying power"):
        _parse_buying_power({})


def test_parse_buying_power_margin_stock_buying_power():
    # Tradier sandbox omits "buying_power" and uses "stock_buying_power" instead
    data = {"balances": {"margin": {"stock_buying_power": 200000.0, "option_buying_power": 100000.0}}}
    assert _parse_buying_power(data) == 200000.0


def test_parse_buying_power_pdt_stock_buying_power():
    data = {"balances": {"pdt": {"stock_buying_power": 75000.0}}}
    assert _parse_buying_power(data) == 75000.0


def test_parse_buying_power_unrecognised_structure():
    with pytest.raises(ValueError, match="buying power"):
        _parse_buying_power({"balances": {"something_else": {}}})


# --- _parse_order_status ---

def test_parse_order_status_filled_with_price():
    data = {"order": {"status": "filled", "avg_fill_price": 175.5}}
    status, price = _parse_order_status(data)
    assert status == "filled"
    assert price == 175.5


def test_parse_order_status_pending_no_price():
    data = {"order": {"status": "pending", "avg_fill_price": None}}
    status, price = _parse_order_status(data)
    assert status == "pending"
    assert price is None


def test_parse_order_status_zero_fill_price_is_none():
    data = {"order": {"status": "filled", "avg_fill_price": "0"}}
    status, price = _parse_order_status(data)
    assert status == "filled"
    assert price is None


def test_parse_order_status_rejected():
    data = {"order": {"status": "rejected"}}
    status, price = _parse_order_status(data)
    assert status == "rejected"
    assert price is None


def test_parse_order_status_missing_order_key():
    status, price = _parse_order_status({})
    assert status == "unknown"
    assert price is None


# --- _parse_quotes_with_open ---

def test_parse_quotes_with_open_single():
    data = {"quotes": {"quote": {"symbol": "AAPL", "last": 175.5, "open": 170.0}}}
    result = _parse_quotes_with_open(data)
    assert result == {"AAPL": (175.5, 170.0)}

def test_parse_quotes_with_open_multiple():
    data = {
        "quotes": {
            "quote": [
                {"symbol": "AAPL", "last": 175.5, "open": 170.0},
                {"symbol": "MSFT", "last": 420.0, "open": 415.0},
            ]
        }
    }
    result = _parse_quotes_with_open(data)
    assert result["AAPL"] == (175.5, 170.0)
    assert result["MSFT"] == (420.0, 415.0)

def test_parse_quotes_with_open_missing_open():
    data = {"quotes": {"quote": {"symbol": "AAPL", "last": 175.5}}}
    result = _parse_quotes_with_open(data)
    assert result == {"AAPL": (175.5, None)}

def test_parse_quotes_with_open_empty():
    assert _parse_quotes_with_open({}) == {}

def test_parse_quotes_with_open_null_last_excluded():
    data = {"quotes": {"quote": {"symbol": "AAPL", "last": None, "open": 170.0}}}
    assert _parse_quotes_with_open(data) == {}


def test_parse_market_bars_single():
    data = {
        "series": {
            "data": {
                "time": "2026-05-07T10:21:00",
                "open": 13.52,
                "high": 13.53,
                "low": 13.45,
                "close": 13.45,
                "volume": 81843,
            }
        }
    }

    bars = _parse_market_bars(data)

    assert len(bars) == 1
    assert bars[0].close == 13.45


def test_parse_account_orders_flattens_nested_otoco_legs():
    data = {
        "orders": {
            "order": {
                "symbol": "AAPL",
                "side": "buy",
                "status": "filled",
                "type": "market",
                "avg_fill_price": "100.00",
                "quantity": "2",
                "transaction_date": "2026-05-01T14:00:00Z",
                "leg": [
                    {
                        "symbol": "AAPL",
                        "side": "sell",
                        "status": "open",
                        "type": "limit",
                        "avg_fill_price": None,
                        "quantity": "2",
                    },
                    {
                        "symbol": "AAPL",
                        "side": "sell",
                        "status": "filled",
                        "type": "stop",
                        "avg_fill_price": "98.00",
                        "quantity": "2",
                        "transaction_date": "2026-05-01T15:00:00Z",
                    },
                ],
            }
        }
    }

    orders = _parse_account_orders(data)

    assert [(o.side, o.status, o.order_type, o.avg_fill_price) for o in orders] == [
        ("buy", "filled", "market", 100.0),
        ("sell", "open", "limit", None),
        ("sell", "filled", "stop", 98.0),
    ]


def test_parse_gain_loss_single_object():
    data = {
        "gainloss": {
            "closed_position": {
                "close_date": "2026-05-08T00:00:00.000Z",
                "cost": 491.15,
                "gain_loss": -1.71,
                "gain_loss_percent": -0.35,
                "open_date": "2026-05-07T00:00:00.000Z",
                "proceeds": 489.44,
                "quantity": 19.0,
                "symbol": "MCFT",
                "term": 1,
            }
        }
    }

    rows = _parse_gain_loss(data)

    assert rows == [
        TradierGainLoss(
            symbol="MCFT",
            quantity=19.0,
            cost=491.15,
            proceeds=489.44,
            gain_loss=-1.71,
            gain_loss_percent=-0.35,
            open_date="2026-05-07T00:00:00.000Z",
            close_date="2026-05-08T00:00:00.000Z",
            term=1,
        )
    ]


def test_parse_gain_loss_null():
    assert _parse_gain_loss({"gainloss": "null"}) == []


def test_gain_loss_summary_uses_broker_close_date_without_et_shift():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["sortBy"] == "closeDate"
        return httpx.Response(
            200,
            json={
                "gainloss": {
                    "closed_position": {
                        "close_date": "2026-05-08T00:00:00.000Z",
                        "cost": 45.84,
                        "gain_loss": 1.34,
                        "gain_loss_percent": 2.92,
                        "open_date": "2026-05-06T00:00:00.000Z",
                        "proceeds": 47.18,
                        "quantity": 2.0,
                        "symbol": "KHC",
                        "term": 2,
                    }
                }
            },
            request=request,
        )

    client = TradierClient("token", "acct")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.test")
    try:
        count, pnl = client.gain_loss_summary_for_close_date(date(2026, 5, 8))
    finally:
        client.close()

    assert count == 1
    assert pnl == 1.34


def test_parse_account_history_trade_events():
    data = {
        "history": {
            "event": [
                {
                    "amount": -26.24,
                    "date": "2026-05-05T00:00:00Z",
                    "type": "trade",
                    "trade": {
                        "commission": 0.0,
                        "description": "PFIZER INC",
                        "price": 26.24,
                        "quantity": 1.0,
                        "symbol": "PFE",
                        "trade_type": "Equity",
                    },
                },
                {
                    "amount": 26.53,
                    "date": "2026-05-06T00:00:00Z",
                    "type": "trade",
                    "trade": {
                        "commission": 0.0,
                        "description": "PFIZER INC",
                        "price": 26.53,
                        "quantity": -1.0,
                        "symbol": "PFE",
                        "trade_type": "Equity",
                    },
                },
            ]
        }
    }

    rows = _parse_account_history(data)

    assert rows[0] == TradierActivity(
        amount=-26.24,
        date="2026-05-05T00:00:00Z",
        type="trade",
        trade={
            "commission": 0.0,
            "description": "PFIZER INC",
            "price": 26.24,
            "quantity": 1.0,
            "symbol": "PFE",
            "trade_type": "Equity",
        },
    )
    assert rows[1].trade["quantity"] == -1.0


def test_parse_account_history_null():
    assert _parse_account_history({"history": "null"}) == []


def test_request_retries_429_with_retry_after_zero():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = TradierClient("token", "acct")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.test")
    try:
        resp = client._request("GET", "/anything")
    finally:
        client.close()

    assert resp.status_code == 200
    assert calls == 2
