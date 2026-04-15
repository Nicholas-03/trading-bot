# tests/test_tradier_client.py
from trading.tradier_client import _parse_positions, _parse_quotes, TradierPosition


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
