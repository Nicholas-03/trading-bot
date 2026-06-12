from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from analytics.gdelt_backtest import (
    GdeltEvent,
    build_match_gate_from_args,
    cloud_event_to_news,
    event_to_news,
    iter_gdelt_datetimes,
    parse_export_row,
    should_keep_event,
    news_within_range,
    stable_news_id_from_string,
)


def test_parse_export_row_extracts_core_fields():
    row = [""] * 61
    row[0] = "1302321506"
    row[6] = "IRAN"
    row[16] = "UNITED STATES"
    row[28] = "19"
    row[31] = "10"
    row[34] = "-4.25"
    row[52] = "Strait Of Hormuz"
    row[59] = "20260504130000"
    row[60] = "https://example.com/iran-hormuz"

    event = parse_export_row(row)

    assert event is not None
    assert event.event_id == "1302321506"
    assert event.actor1_name == "IRAN"
    assert event.actor2_name == "UNITED STATES"
    assert event.event_root_code == "19"
    assert event.event_phrase == "fight"
    assert event.date_added == datetime(2026, 5, 4, 13, 0, tzinfo=timezone.utc)


def test_event_to_news_uses_actors_event_phrase_location_and_url():
    event = GdeltEvent(
        event_id="1",
        date_added=datetime(2026, 5, 4, 13, 0, tzinfo=timezone.utc),
        actor1_name="IRAN",
        actor2_name="UNITED STATES",
        event_root_code="19",
        event_phrase="fight",
        action_geo="Strait Of Hormuz",
        source_url="https://example.com/iran-hormuz",
        num_mentions=10,
        avg_tone=-4.25,
    )

    news = event_to_news(event)

    assert news.id == 1
    assert news.symbols == []
    assert "IRAN fight UNITED STATES" in news.headline
    assert "Strait Of Hormuz" in news.headline
    assert "https://example.com/iran-hormuz" in news.summary


def test_cloud_event_to_news_uses_coded_at_and_article_evidence():
    raw = {
        "id": "cameoplus_c34192ac",
        "coded_at": "2026-05-06T12:09:57Z",
        "event_date": "2026-05-06",
        "title": "Saudi and Iran foreign ministers hold phone talks to prevent regional escalation",
        "summary": "Iran and Saudi Arabia stressed diplomacy and de-escalation.",
        "category": "POLITICAL",
        "geo": {"country": "Iran"},
        "top_articles": [
            {
                "title": "Saudi, Iranian FMs urge regional cooperation",
                "url": "https://english.news.cn/example.html",
                "domain": "english.news.cn",
            }
        ],
    }

    news = cloud_event_to_news(raw)

    assert news is not None
    assert news.id == stable_news_id_from_string("cameoplus_c34192ac")
    assert news.ts == datetime(2026, 5, 6, 12, 9, 57, tzinfo=timezone.utc)
    assert news.headline == "Saudi and Iran foreign ministers hold phone talks to prevent regional escalation"
    assert "category=POLITICAL" in news.summary
    assert "country=Iran" in news.summary
    assert "https://english.news.cn/example.html" in news.summary
    assert news.symbols == []


def test_news_within_range_uses_signal_timestamp():
    raw = {
        "id": "cameoplus_late",
        "coded_at": "2026-05-08T00:07:40Z",
        "event_date": "2026-05-07",
        "title": "Pakistan and Iran foreign ministers hold phone talks",
        "summary": "",
    }
    news = cloud_event_to_news(raw)

    assert news is not None
    assert not news_within_range(
        news,
        datetime(2026, 5, 4, tzinfo=timezone.utc),
        datetime(2026, 5, 7, 23, 59, 59, tzinfo=timezone.utc),
    )


def test_should_keep_event_matches_focus_terms_and_material_conflict():
    event = GdeltEvent(
        event_id="1",
        date_added=datetime(2026, 5, 4, 13, 0, tzinfo=timezone.utc),
        actor1_name="IRAN",
        actor2_name="UNITED STATES",
        event_root_code="19",
        event_phrase="fight",
        action_geo="Strait Of Hormuz",
        source_url="https://example.com/iran-hormuz",
        num_mentions=10,
        avg_tone=-4.25,
    )

    assert should_keep_event(event, ("iran", "hormuz"))
    assert not should_keep_event(event, ("japan", "tariff"))


def test_iter_gdelt_datetimes_steps_in_requested_minutes():
    start = datetime(2026, 5, 4, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)

    values = list(iter_gdelt_datetimes(start, end, interval_minutes=30))

    assert values == [
        datetime(2026, 5, 4, 13, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc),
    ]


def test_build_match_gate_rejects_llm_and_embedding_together():
    args = SimpleNamespace(llm_gate=True, embedding_gate=True)

    with pytest.raises(ValueError, match="Choose only one"):
        build_match_gate_from_args(args)


def test_build_match_gate_creates_embedding_gate(monkeypatch):
    created = {}

    class _FakeEmbeddingGate:
        def __init__(
            self,
            model_name: str,
            min_similarity: float,
            cache_path: str,
            require_directional_signal: bool,
        ) -> None:
            created["model_name"] = model_name
            created["min_similarity"] = min_similarity
            created["cache_path"] = cache_path
            created["require_directional_signal"] = require_directional_signal

    monkeypatch.setattr("analytics.gdelt_backtest.EmbeddingGate", _FakeEmbeddingGate)
    args = SimpleNamespace(
        llm_gate=False,
        embedding_gate=True,
        embedding_model="test-model",
        embedding_threshold=0.71,
        embedding_cache="data/test_embedding_cache.json",
        embedding_allow_ambiguous_direction=False,
    )

    gate = build_match_gate_from_args(args)

    assert isinstance(gate, _FakeEmbeddingGate)
    assert created == {
        "model_name": "test-model",
        "min_similarity": 0.71,
        "cache_path": "data/test_embedding_cache.json",
        "require_directional_signal": True,
    }
