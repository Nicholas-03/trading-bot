# tests/test_tradier_client.py
import pytest
from trading.tradier_client import _parse_positions, _parse_quotes, _parse_buying_power, _parse_order_status, TradierPosition


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


def test_parse_order_status_rejected():
    data = {"order": {"status": "rejected"}}
    status, price = _parse_order_status(data)
    assert status == "rejected"
    assert price is None


def test_parse_order_status_missing_order_key():
    status, price = _parse_order_status({})
    assert status == "unknown"
    assert price is None
