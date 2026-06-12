from analytics.order_book import estimate_buy_fill, parse_order_book, summarize_order_book


def test_parse_order_book_sorts_bids_desc_and_asks_asc():
    raw = {
        "asset_id": "token-1",
        "market": "market-1",
        "timestamp": "1780918156440",
        "bids": [
            {"price": "0.01", "size": "10"},
            {"price": "0.03", "size": "20"},
        ],
        "asks": [
            {"price": "0.99", "size": "100"},
            {"price": "0.09", "size": "50"},
            {"price": "0.14", "size": "60"},
        ],
        "min_order_size": "5",
        "tick_size": "0.01",
        "last_trade_price": "",
    }

    book = parse_order_book(raw)

    assert book.token_id == "token-1"
    assert book.timestamp_ms == 1780918156440
    assert [level.price for level in book.bids] == [0.03, 0.01]
    assert [level.price for level in book.asks] == [0.09, 0.14, 0.99]
    assert book.min_order_size == 5.0
    assert book.tick_size == 0.01
    assert book.last_trade_price is None


def test_estimate_buy_fill_respects_max_price_limit():
    asks = parse_order_book(
        {
            "asks": [
                {"price": "0.10", "size": "500"},
                {"price": "0.12", "size": "500"},
                {"price": "0.20", "size": "500"},
            ]
        }
    ).asks

    fill = estimate_buy_fill(asks, notional_usd=100.0, max_price=0.12)

    assert fill.shares == 916.6666666667
    assert fill.spent_usd == 100.0
    assert fill.unfilled_usd == 0.0
    assert round(fill.avg_price or 0, 4) == 0.1091
    assert fill.max_price_paid == 0.12


def test_estimate_buy_fill_reports_unfilled_amount_when_depth_is_insufficient():
    asks = parse_order_book({"asks": [{"price": "0.10", "size": "100"}]}).asks

    fill = estimate_buy_fill(asks, notional_usd=100.0, max_price=0.10)

    assert fill.shares == 100.0
    assert fill.spent_usd == 10.0
    assert fill.unfilled_usd == 90.0
    assert fill.avg_price == 0.10


def test_summarize_order_book_computes_spread_depth_and_fillability():
    book = parse_order_book(
        {
            "bids": [
                {"price": "0.08", "size": "300"},
                {"price": "0.07", "size": "200"},
            ],
            "asks": [
                {"price": "0.09", "size": "500"},
                {"price": "0.11", "size": "500"},
                {"price": "0.20", "size": "500"},
            ],
        }
    )

    metrics = summarize_order_book(book, notional_usd=100.0, max_buy_slippage=0.02)

    assert metrics.best_bid == 0.08
    assert metrics.best_ask == 0.09
    assert metrics.spread == 0.01
    assert metrics.mid == 0.085
    assert metrics.ask_depth_usd_1c == 45.0
    assert metrics.ask_depth_usd_5c == 100.0
    assert metrics.bid_depth_usd_1c == 38.0
    assert metrics.buy_fillable_within_slippage
    assert metrics.buy_max_price_paid == 0.11
    assert metrics.buy_price_impact is not None
