from datetime import datetime, timezone

from trading.alpaca_data_client import _format_rfc3339, _parse_alpaca_bars


def test_format_rfc3339_normalizes_to_utc():
    ts = datetime(2026, 5, 8, 14, 30, 12, tzinfo=timezone.utc)

    assert _format_rfc3339(ts) == "2026-05-08T14:30:12Z"


def test_parse_alpaca_bars_sorts_and_skips_incomplete_rows():
    bars = _parse_alpaca_bars(
        {
            "bars": [
                {"t": "2026-05-08T14:31:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100},
                {"t": "2026-05-08T14:30:00Z", "o": 9, "h": 10, "l": 8, "c": 9.5, "v": 200},
                {"t": "2026-05-08T14:32:00Z", "o": None, "h": 10, "l": 8, "c": 9.5},
            ]
        }
    )

    assert [bar.time for bar in bars] == ["2026-05-08T14:30:00Z", "2026-05-08T14:31:00Z"]
    assert bars[0].open == 9.0
    assert bars[1].volume == 100.0
