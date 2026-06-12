from datetime import datetime, timezone

from analytics.market_relevant_backtest import (
    MarketRelevantConfig,
    _is_corporate_catalyst_news,
    _is_same_ticker_single_name_threshold,
    _symbol_filter_allows,
    _trade_dedupe_key,
    choose_exit,
    first_price_at_or_after,
    infer_market_relevant_signal,
    latest_price_before_or_at,
    market_has_enough_time_to_end,
)
from analytics.polymarket_backtest import GammaMarket, NewsEvent


def _news(headline: str, summary: str = "", symbols: list[str] | None = None) -> NewsEvent:
    return NewsEvent(
        id=1,
        ts=datetime(2026, 5, 5, 17, 12, 26, tzinfo=timezone.utc),
        headline=headline,
        summary=summary,
        symbols=symbols or ["PLTR"],
    )


def _market(question: str, description: str = "") -> GammaMarket:
    return GammaMarket(
        id="m1",
        question=question,
        description=description,
        outcomes=["Yes", "No"],
        outcome_prices=[0.1, 0.9],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=0.0,
        closed=False,
        active=True,
        event_title=question,
    )


def test_infer_market_relevant_signal_buys_high_threshold_on_positive_stock_news():
    news = _news(
        "These Analysts Revise Their Forecasts On Palantir Following Q1 Earnings",
        "Palantir reports strong Q1 results and analysts raise price targets.",
    )
    market = _market("Will Palantir Technologies Inc. (PLTR) hit (HIGH) $180 in May?")

    signal = infer_market_relevant_signal(news, market)

    assert signal is not None
    assert signal.side == "yes"
    assert signal.reason == "positive stock/news signal for HIGH threshold"


def test_infer_market_relevant_signal_buys_no_on_low_threshold_after_positive_news():
    news = _news(
        "These Analysts Revise Their Forecasts On Palantir Following Q1 Earnings",
        "Palantir reports strong Q1 results and analysts raise price targets.",
    )
    market = _market("Will Palantir Technologies Inc. (PLTR) hit (LOW) $108 in May?")

    signal = infer_market_relevant_signal(news, market)

    assert signal is not None
    assert signal.side == "no"
    assert signal.reason == "positive stock/news signal against LOW threshold"


def test_infer_market_relevant_signal_skips_earnings_preview_for_beat_market():
    news = _news("The Trade Desk Gears Up For Q1 Earnings", "Preview of what investors expect.", ["TTD"])
    market = _market("Will Trade Desk (TTD) beat quarterly earnings?")

    assert infer_market_relevant_signal(news, market) is None


def test_infer_market_relevant_signal_accepts_reported_positive_earnings_for_beat_market():
    news = _news("DigitalOcean Stock Soars After Q1 Double Beat", "DigitalOcean beat estimates and raises guidance.", ["DOCN"])
    market = _market("Will DigitalOcean (DOCN) beat quarterly earnings?")

    signal = infer_market_relevant_signal(news, market)

    assert signal is not None
    assert signal.side == "yes"
    assert signal.reason == "market-relevant positive earnings signal"


def test_first_price_at_or_after_respects_entry_window():
    points = [
        (100, 0.20),
        (130, 0.25),
        (190, 0.35),
    ]

    assert first_price_at_or_after(points, 120, max_delay_seconds=40) == (130, 0.25)
    assert first_price_at_or_after(points, 120, max_delay_seconds=5) is None


def test_latest_price_before_or_at_uses_last_tick_inside_horizon():
    points = [
        (100, 0.20),
        (130, 0.25),
        (190, 0.35),
    ]

    assert latest_price_before_or_at(points, 150) == (130, 0.25)


def test_choose_exit_prefers_target_before_horizon():
    config = MarketRelevantConfig(target_move=0.10, horizon_hours=2)
    points = [
        (100, 0.20),
        (130, 0.25),
        (190, 0.32),
        (400, 0.18),
    ]

    exit_point = choose_exit(points, signal_ts=100, entry_price=0.20, config=config)

    assert exit_point is not None
    assert exit_point.reason == "take_profit_10c"
    assert exit_point.timestamp == 190
    assert exit_point.price == 0.32


def test_choose_exit_can_stop_loss_before_target():
    config = MarketRelevantConfig(target_move=0.10, stop_move=0.05, horizon_hours=2)
    points = [
        (100, 0.20),
        (130, 0.14),
        (190, 0.32),
    ]

    exit_point = choose_exit(points, signal_ts=100, entry_price=0.20, config=config)

    assert exit_point is not None
    assert exit_point.reason == "stop_loss_5c"
    assert exit_point.timestamp == 130
    assert exit_point.price == 0.14


def test_choose_exit_falls_back_to_last_tick_before_horizon():
    config = MarketRelevantConfig(target_move=0.10, horizon_hours=2)
    points = [
        (100, 0.20),
        (130, 0.25),
        (8_000, 0.80),
    ]

    exit_point = choose_exit(points, signal_ts=100, entry_price=0.20, config=config)

    assert exit_point is not None
    assert exit_point.reason == "horizon_2h"
    assert exit_point.timestamp == 130
    assert exit_point.price == 0.25


def test_market_has_enough_time_to_end_rejects_near_expiry_contracts():
    news_ts = datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc)
    near_expiry = _market("Will PLTR hit (HIGH) $165 Week of May 4 2026?")
    near_expiry = GammaMarket(**{**near_expiry.__dict__, "end_date": datetime(2026, 5, 8, tzinfo=timezone.utc)})
    monthly = _market("Will PLTR hit (HIGH) $180 in May?")
    monthly = GammaMarket(**{**monthly.__dict__, "end_date": datetime(2026, 6, 1, tzinfo=timezone.utc)})

    assert not market_has_enough_time_to_end(near_expiry, news_ts, min_hours_to_end=168)
    assert market_has_enough_time_to_end(monthly, news_ts, min_hours_to_end=168)


def test_symbol_filter_supports_include_and_exclude_lists():
    assert _symbol_filter_allows(["PLTR", "SPY"], ("PLTR",), ())
    assert not _symbol_filter_allows(["SPY"], ("PLTR",), ())
    assert not _symbol_filter_allows(["PLTR", "SPY"], (), ("SPY",))


def test_trade_dedupe_key_can_collapse_to_market_side():
    headline = "These Analysts Revise Their Forecasts On Palantir Following Q1 Earnings"

    assert _trade_dedupe_key("m1", "yes", headline, "none") is None
    assert _trade_dedupe_key("m1", "yes", headline, "market-side") == ("m1", "yes")
    assert _trade_dedupe_key("m1", "yes", headline, "headline") == (
        "m1",
        "yes",
        "these analysts revise their forecasts on palantir following q1 earnings",
    )


def test_single_name_threshold_filter_accepts_same_ticker_stock_market():
    news = _news(
        "These Analysts Revise Their Forecasts On Palantir Following Q1 Earnings",
        "Palantir reports strong Q1 results.",
        ["PLTR"],
    )
    market = _market("Will Palantir Technologies Inc. (PLTR) hit (HIGH) $180 in May?")

    assert _is_same_ticker_single_name_threshold(news, market)


def test_single_name_threshold_filter_rejects_spy_index_market():
    news = _news(
        "Fed's Hammack Says U.S. Banking System Remains Healthy And Strong",
        "US Dollar continues to be dominant currency.",
        ["SPY"],
    )
    market = _market("Will S&P 500 (SPY) hit (LOW) $680 in May?")

    assert not _is_same_ticker_single_name_threshold(news, market)


def test_corporate_catalyst_filter_accepts_earnings_followup_news():
    news = _news(
        "These Analysts Revise Their Forecasts On Palantir Following Q1 Earnings",
        "Palantir reports strong Q1 results and analysts raise price targets.",
        ["PLTR"],
    )

    assert _is_corporate_catalyst_news(news)


def test_corporate_catalyst_filter_rejects_macro_fed_news():
    news = _news(
        "Fed's Hammack Says U.S. Banking System Remains Healthy And Strong",
        "US Dollar continues to be dominant currency.",
        ["SPY"],
    )

    assert not _is_corporate_catalyst_news(news)
