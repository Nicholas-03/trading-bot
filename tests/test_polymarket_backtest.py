import sqlite3
from datetime import datetime, timezone

from analytics.polymarket_backtest import (
    BacktestConfig,
    GammaMarket,
    NewsEvent,
    candidate_score,
    infer_news_probability,
    is_market_open_at,
    parse_string_list,
    price_at_or_after,
    run_backtest,
    simulate_long_binary,
    should_consider_news,
)
from analytics.llm_gate import LLMGateDecision


class _FakePolymarketClient:
    def __init__(self, market, history):
        self._market = market
        self._history = history

    def search_markets(self, query: str, limit: int = 5):
        return [self._market]

    def price_history(self, token_id: str, start_ts: int, end_ts: int):
        return self._history


class _AcceptEmbeddingGate:
    stats_prefix = "embedding"

    def __init__(self) -> None:
        self.calls = 1
        self.cache_hits = 2
        self.limit_skips = 3

    def evaluate(self, news: NewsEvent, market: GammaMarket):
        return LLMGateDecision(True, 0.95, 0.9, "accepted by test gate")


def test_parse_string_list_accepts_json_array_and_csv_fallback():
    assert parse_string_list('["Yes", "No"]') == ["Yes", "No"]
    assert parse_string_list("AAPL, MSFT,,NVDA") == ["AAPL", "MSFT", "NVDA"]
    assert parse_string_list(None) == []


def test_is_market_open_at_rejects_markets_closed_before_news():
    news_ts = datetime(2026, 5, 7, 14, 41, tzinfo=timezone.utc)
    market = GammaMarket(
        id="2062059",
        question="Will McDonald's (MCD) beat quarterly earnings?",
        description="",
        outcomes=["Yes", "No"],
        outcome_prices=[1.0, 0.0],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 5, 7, 13, 0, tzinfo=timezone.utc),
        closed_time=datetime(2026, 5, 7, 13, 14, tzinfo=timezone.utc),
        enable_order_book=True,
        volume=4169.0,
        liquidity=0.0,
        closed=True,
        active=True,
        event_title="Will McDonald's (MCD) beat quarterly earnings?",
    )

    assert not is_market_open_at(market, news_ts)


def test_candidate_score_rewards_ticker_and_earnings_keyword_match():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="Hertz Posts Double Beat In Q1 With Strongest Revenue Growth In Three Years",
        summary="Hertz Global Holdings Inc (NASDAQ: HTZ) reported Q1 results that exceeded expectations.",
        symbols=["HTZ"],
    )
    market = GammaMarket(
        id="m1",
        question="Will Hertz Global Holdings (HTZ) beat quarterly earnings?",
        description="This market resolves Yes if HTZ reports non-GAAP EPS above consensus.",
        outcomes=["Yes", "No"],
        outcome_prices=[1.0, 0.0],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 5, 7, 22, 0, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=100.0,
        closed=False,
        active=True,
        event_title="Will Hertz Global Holdings (HTZ) beat quarterly earnings?",
    )

    score = candidate_score(news, market)

    assert score >= 0.7


def test_candidate_score_rejects_name_collision_without_event_overlap():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="AAON Raises FY2026 Sales Guidance",
        summary="AAON raises sales outlook.",
        symbols=["AAON"],
    )
    market = GammaMarket(
        id="m1",
        question="Will Aaron Judge win the 2026 American League Hank Aaron Award?",
        description="Baseball award market.",
        outcomes=["Yes", "No"],
        outcome_prices=[0.2, 0.8],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 12, 1, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=100.0,
        closed=False,
        active=True,
        event_title="MLB: 2026 AL Hank Aaron Winner",
    )

    assert candidate_score(news, market) < 0.35


def test_candidate_score_rejects_short_ticker_collision_with_political_abbreviation():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="Transcript: Pediatrix Medical Group Q1 2026 Earnings Conference Call",
        summary="",
        symbols=["MD"],
    )
    market = GammaMarket(
        id="m1",
        question="Will the Republican Party win the MD-06 House seat?",
        description="Congressional district market.",
        outcomes=["Yes", "No"],
        outcome_prices=[0.2, 0.8],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 12, 1, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=100.0,
        closed=False,
        active=True,
        event_title="MD-06 House election",
    )

    assert candidate_score(news, market) < 0.35


def test_candidate_score_rejects_common_word_ticker_collision_in_description():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="Shares of companies within the broader tech sector are trading higher",
        summary="Investors react to reports suggesting the U.S. and Iran are nearing an agreement.",
        symbols=["ANY", "AMD", "ORCL"],
    )
    market = GammaMarket(
        id="m1",
        question="Will Viking Therapeutics be acquired before 2027?",
        description="This market resolves Yes if any credible source reports an acquisition.",
        outcomes=["Yes", "No"],
        outcome_prices=[0.2, 0.8],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 12, 1, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=100.0,
        closed=False,
        active=True,
        event_title="Will Viking Therapeutics be acquired before 2027?",
    )

    assert candidate_score(news, market) < 0.35


def test_candidate_score_matches_macro_market_without_symbols():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="Trump announces new tariffs on Japan",
        summary="The administration announced tariff measures targeting Japanese imports.",
        symbols=[],
    )
    market = GammaMarket(
        id="m1",
        question="Will Trump impose tariffs on Japan in the first 100 days?",
        description="This market resolves Yes if Trump imposes tariffs on Japan.",
        outcomes=["Yes", "No"],
        outcome_prices=[0.2, 0.8],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 12, 1, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=100.0,
        closed=False,
        active=True,
        event_title="Which countries will Trump tariff?",
    )

    assert candidate_score(news, market) >= 0.60


def test_candidate_score_rejects_word_utterance_market_without_speech_context():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="WORKER disapprove CANADA tariff worker vance des moines",
        summary="GDELT event with labor dispute context and tariff references.",
        symbols=[],
    )
    market = GammaMarket(
        id="m1",
        question='Will JD Vance say "Worker" in Des Moines?',
        description="This market resolves Yes if JD Vance says the listed word.",
        outcomes=["Yes", "No"],
        outcome_prices=[0.2, 0.8],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 12, 1, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=100.0,
        closed=False,
        active=True,
        event_title='Will JD Vance say "Worker" in Des Moines?',
    )

    assert candidate_score(news, market) < 0.35


def test_price_at_or_after_requires_price_after_signal_within_window():
    signal_ts = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    history = [
        {"t": int(datetime(2026, 5, 7, 11, 59, tzinfo=timezone.utc).timestamp()), "p": 0.4},
        {"t": int(datetime(2026, 5, 7, 12, 3, tzinfo=timezone.utc).timestamp()), "p": 0.6},
    ]

    assert price_at_or_after(history, signal_ts, max_delay_seconds=300) == 0.6
    assert price_at_or_after(history, signal_ts, max_delay_seconds=60) is None


def test_infer_news_probability_for_confirmed_positive_earnings_market():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="Hertz Posts Double Beat In Q1",
        summary="Hertz exceeded expectations on top and bottom lines.",
        symbols=["HTZ"],
    )
    market = GammaMarket(
        id="m1",
        question="Will Hertz Global Holdings (HTZ) beat quarterly earnings?",
        description="",
        outcomes=["Yes", "No"],
        outcome_prices=[1.0, 0.0],
        clob_token_ids=["yes-token", "no-token"],
        end_date=None,
        closed_time=None,
        enable_order_book=True,
        volume=0.0,
        liquidity=0.0,
        closed=False,
        active=True,
        event_title="",
    )

    probability, reason = infer_news_probability(news, market)

    assert probability >= 0.9
    assert "positive" in reason


def test_infer_news_probability_for_acquisition_bid_market():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 5, 13, 37, tzinfo=timezone.utc),
        headline="GameStop's eBay Deal Hinges On Highly Confident Money",
        summary="GameStop's $55.5B eBay bid relies on financing.",
        symbols=["GME", "EBAY"],
    )
    market = GammaMarket(
        id="m1",
        question="Will GameStop acquire eBay?",
        description="Resolves Yes if it is announced that eBay will be acquired by or merged with GameStop.",
        outcomes=["Yes", "No"],
        outcome_prices=[0.15, 0.85],
        clob_token_ids=["yes-token", "no-token"],
        end_date=None,
        closed_time=None,
        enable_order_book=True,
        volume=0.0,
        liquidity=0.0,
        closed=False,
        active=True,
        event_title="",
    )

    probability, reason = infer_news_probability(news, market)

    assert probability >= 0.8
    assert "acquisition" in reason


def test_simulate_long_binary_uses_fixed_notional_and_fee():
    result = simulate_long_binary(entry_price=0.50, exit_price=1.0, notional=100.0, fee_bps=10)

    assert result.shares == 200.0
    assert result.pnl_usd == 99.9
    assert result.roi_pct == 99.9


def test_backtest_config_edge_threshold_includes_fees():
    config = BacktestConfig(min_edge=0.08, fee_bps=10)

    assert config.required_edge == 0.081


def test_should_consider_news_rejects_plain_analyst_price_target_update():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="Cantor Fitzgerald Maintains Overweight on Broadstone Net Lease, Raises Price Target to $20",
        summary="",
        symbols=["BNL"],
    )

    assert not should_consider_news(news)


def test_should_consider_news_rejects_prediction_market_meta_article():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="Uber Earnings Prediction Market Preview: What Will Dara Khosrowshahi Say?",
        summary="Polymarket puts Uber at 13% to beat Q1 earnings.",
        symbols=["UBER"],
    )

    assert not should_consider_news(news)


def test_should_consider_news_rejects_earnings_call_transcript():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="Kyndryl Hldgs Q4 2026 Earnings Call Transcript",
        summary="",
        symbols=["KD"],
    )

    assert not should_consider_news(news)


def test_should_consider_news_rejects_complete_transcript_variant():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="Kennametal Q3 2026 Earnings Call: Complete Transcript",
        summary="",
        symbols=["KMT"],
    )

    assert not should_consider_news(news)


def test_should_consider_news_accepts_geopolitical_news_without_symbols():
    news = NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 30, tzinfo=timezone.utc),
        headline="U.S. and Iran near agreement to end Middle East conflict",
        summary="Negotiators are discussing a ceasefire and sanctions relief.",
        symbols=[],
    )

    assert should_consider_news(news)


def test_run_backtest_keeps_trade_even_when_edge_is_below_previous_threshold(tmp_path):
    db_path = tmp_path / "trades.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE news_events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, headline TEXT NOT NULL, summary TEXT, symbols TEXT)"
    )
    con.execute(
        "INSERT INTO news_events (id, ts, headline, summary, symbols) VALUES (?, ?, ?, ?, ?)",
        (
            1,
            "2026-05-07T12:00:00+00:00",
            "Hertz Posts Double Beat In Q1 With Strongest Revenue Growth In Three Years",
            "Hertz Global Holdings Inc (NASDAQ: HTZ) reported Q1 results that exceeded expectations.",
            "HTZ",
        ),
    )
    con.commit()
    con.close()

    market = GammaMarket(
        id="m1",
        question="Will Hertz Global Holdings (HTZ) beat quarterly earnings?",
        description="This market resolves Yes if HTZ reports non-GAAP EPS above consensus.",
        outcomes=["Yes", "No"],
        outcome_prices=[1.0, 0.0],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 5, 7, 22, 0, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=100.0,
        closed=False,
        active=True,
        event_title="Will Hertz Global Holdings (HTZ) beat quarterly earnings?",
    )
    history = [
        {"t": int(datetime(2026, 5, 7, 12, 1, tzinfo=timezone.utc).timestamp()), "p": 0.99},
    ]

    rows, stats = run_backtest(
        str(db_path),
        _FakePolymarketClient(market, history),
        BacktestConfig(min_edge=0.08, request_sleep_seconds=0),
    )

    assert stats["with_entry_price"] == 1
    assert stats["trades"] == 1
    assert rows[0].edge < BacktestConfig(min_edge=0.08).required_edge


def test_run_backtest_records_generic_gate_stats_for_non_llm_gate(tmp_path):
    db_path = tmp_path / "trades.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE news_events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, headline TEXT NOT NULL, summary TEXT, symbols TEXT)"
    )
    con.execute(
        "INSERT INTO news_events (id, ts, headline, summary, symbols) VALUES (?, ?, ?, ?, ?)",
        (
            1,
            "2026-05-07T12:00:00+00:00",
            "Hertz Posts Double Beat In Q1 With Strongest Revenue Growth In Three Years",
            "Hertz Global Holdings Inc (NASDAQ: HTZ) reported Q1 results that exceeded expectations.",
            "HTZ",
        ),
    )
    con.commit()
    con.close()
    market = GammaMarket(
        id="m1",
        question="Will Hertz Global Holdings (HTZ) beat quarterly earnings?",
        description="This market resolves Yes if HTZ reports non-GAAP EPS above consensus.",
        outcomes=["Yes", "No"],
        outcome_prices=[1.0, 0.0],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 5, 7, 22, 0, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=100.0,
        closed=False,
        active=True,
        event_title="Will Hertz Global Holdings (HTZ) beat quarterly earnings?",
    )
    history = [
        {"t": int(datetime(2026, 5, 7, 12, 1, tzinfo=timezone.utc).timestamp()), "p": 0.74},
    ]

    rows, stats = run_backtest(
        str(db_path),
        _FakePolymarketClient(market, history),
        BacktestConfig(request_sleep_seconds=0),
        gate=_AcceptEmbeddingGate(),
    )

    assert len(rows) == 1
    assert stats["gate_calls"] == 1
    assert stats["gate_cache_hits"] == 2
    assert stats["gate_limit_skips"] == 3
    assert stats["embedding_calls"] == 1
    assert "llm_calls" not in stats
