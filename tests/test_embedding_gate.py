from datetime import datetime, timezone

from analytics.embedding_gate import (
    EmbeddingGate,
    cosine_similarity,
    embedding_text_for_market,
    embedding_text_for_news,
)
from analytics.polymarket_backtest import GammaMarket, NewsEvent


class _FakeEncoder:
    def __init__(self) -> None:
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        vectors = []
        for text in texts:
            normalized = text.lower()
            if "iran" in normalized or "diplomatic" in normalized:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


def _news() -> NewsEvent:
    return NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
        headline="IRAN engage diplomatic cooperation UNITED STATES in Baghdad",
        summary="GDELT event 1; root=04; source=example.com; url=https://example.com/story",
        symbols=[],
    )


def _market(**overrides) -> GammaMarket:
    values = {
        "id": "m1",
        "question": "Will a qualifying diplomatic US-Iran meeting occur by June 30, 2026?",
        "description": "Resolves Yes if a qualifying diplomatic US-Iran meeting occurs.",
        "outcomes": ["Yes", "No"],
        "outcome_prices": [0.65, 0.35],
        "clob_token_ids": ["yes-token", "no-token"],
        "end_date": datetime(2026, 6, 30, tzinfo=timezone.utc),
        "closed_time": None,
        "enable_order_book": True,
        "volume": 1000.0,
        "liquidity": 100.0,
        "closed": False,
        "active": True,
        "event_title": "US-Iran diplomatic meeting",
    }
    values.update(overrides)
    return GammaMarket(**values)


def test_cosine_similarity_handles_zero_vectors():
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_embedding_text_builders_include_relevant_context_without_symbols_requirement():
    news_text = embedding_text_for_news(_news())
    market_text = embedding_text_for_market(_market())

    assert "IRAN engage diplomatic cooperation" in news_text
    assert "symbols: none" in news_text
    assert "US-Iran diplomatic meeting" in market_text
    assert "qualifying diplomatic US-Iran meeting" in market_text


def test_embedding_gate_accepts_market_above_similarity_threshold():
    encoder = _FakeEncoder()
    gate = EmbeddingGate(encoder=encoder, min_similarity=0.80, require_directional_signal=False)

    decision = gate.evaluate(_news(), _market())

    assert decision.valid_match is True
    assert decision.confidence == 1.0
    assert decision.p_yes == 0.5
    assert "embedding similarity=1.000" in decision.reason
    assert gate.calls == 1


def test_embedding_gate_rejects_ambiguous_direction_by_default():
    encoder = _FakeEncoder()
    gate = EmbeddingGate(encoder=encoder, min_similarity=0.80)

    decision = gate.evaluate(_news(), _market())

    assert decision.valid_match is False
    assert decision.p_yes == 0.5
    assert decision.confidence == 1.0
    assert "ambiguous direction" in decision.reason


def test_embedding_gate_rejects_market_below_similarity_threshold():
    encoder = _FakeEncoder()
    gate = EmbeddingGate(encoder=encoder, min_similarity=0.80)
    unrelated_market = _market(
        question="Will McDonald's beat quarterly earnings?",
        description="Resolves Yes if McDonald's EPS beats consensus.",
        event_title="McDonald's earnings",
    )

    decision = gate.evaluate(_news(), unrelated_market)

    assert decision.valid_match is False
    assert decision.confidence == 0.0
    assert "below threshold" in decision.reason


def test_embedding_gate_uses_cache_for_repeated_pairs(tmp_path):
    encoder = _FakeEncoder()
    gate = EmbeddingGate(
        encoder=encoder,
        min_similarity=0.80,
        cache_path=str(tmp_path / "cache.json"),
        require_directional_signal=False,
    )

    first = gate.evaluate(_news(), _market())
    second = gate.evaluate(_news(), _market())

    assert first == second
    assert encoder.calls == 1
    assert gate.calls == 1
    assert gate.cache_hits == 1
