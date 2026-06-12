from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, Sequence

from analytics.llm_gate import LLMGateDecision
from analytics.polymarket_backtest import GammaMarket, NewsEvent, infer_news_probability

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class TextEncoder(Protocol):
    def encode(self, texts: Sequence[str]): ...


class SentenceTransformerEncoder:
    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is required for --embedding-gate. "
                "Install it with: pip install sentence-transformers"
            ) from exc
        self._model = SentenceTransformer(model_name)

    def encode(self, texts: Sequence[str]):
        return self._model.encode(list(texts), normalize_embeddings=True)


class EmbeddingGate:
    stats_prefix = "embedding"

    def __init__(
        self,
        encoder: TextEncoder | None = None,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        min_similarity: float = 0.50,
        cache_path: str | None = None,
        require_directional_signal: bool = True,
    ) -> None:
        if not 0 <= min_similarity <= 1:
            raise ValueError("min_similarity must be in [0, 1]")
        self._encoder = encoder if encoder is not None else SentenceTransformerEncoder(model_name)
        self._model_name = model_name
        self._min_similarity = min_similarity
        self._require_directional_signal = require_directional_signal
        self._cache_path = Path(cache_path) if cache_path else None
        self.calls = 0
        self.cache_hits = 0
        self.limit_skips = 0
        self._cache = self._load_cache()

    def evaluate(self, news: NewsEvent, market: GammaMarket) -> LLMGateDecision:
        key = self._cache_key(news, market)
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return _decision_from_data(cached)

        self.calls += 1
        news_text = embedding_text_for_news(news)
        market_text = embedding_text_for_market(market)
        news_vector, market_vector = self._encoder.encode([news_text, market_text])
        similarity = cosine_similarity(news_vector, market_vector)

        if similarity < self._min_similarity:
            decision = LLMGateDecision(
                valid_match=False,
                p_yes=0.5,
                confidence=0.0,
                reason=f"embedding similarity={similarity:.3f} below threshold={self._min_similarity:.3f}",
            )
        else:
            p_yes, probability_reason = infer_news_probability(news, market)
            confidence = min(1.0, max(0.0, similarity))
            if self._require_directional_signal and p_yes == 0.5:
                decision = LLMGateDecision(
                    valid_match=False,
                    p_yes=p_yes,
                    confidence=confidence,
                    reason=f"embedding similarity={similarity:.3f}; ambiguous direction: {probability_reason}",
                )
            else:
                decision = LLMGateDecision(
                    valid_match=True,
                    p_yes=p_yes,
                    confidence=confidence,
                    reason=f"embedding similarity={similarity:.3f}; {probability_reason}",
                )

        self._cache[key] = asdict(decision)
        self._save_cache()
        return decision

    def _cache_key(self, news: NewsEvent, market: GammaMarket) -> str:
        payload = {
            "kind": "embedding-gate-v1",
            "model": self._model_name,
            "min_similarity": self._min_similarity,
            "require_directional_signal": self._require_directional_signal,
            "news_id": news.id,
            "news_ts": news.ts.isoformat(),
            "headline": news.headline,
            "summary": news.summary,
            "symbols": news.symbols,
            "market_id": market.id,
            "question": market.question,
            "description": market.description,
            "outcomes": market.outcomes,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_cache(self) -> dict[str, dict]:
        if self._cache_path is None or not self._cache_path.exists():
            return {}
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_cache(self) -> None:
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8")


def embedding_text_for_news(news: NewsEvent) -> str:
    symbols = ", ".join(news.symbols) if news.symbols else "none"
    return "\n".join(
        [
            f"Headline: {news.headline}",
            f"Summary: {news.summary}",
            f"symbols: {symbols}",
        ]
    )


def embedding_text_for_market(market: GammaMarket) -> str:
    return "\n".join(
        [
            f"Event: {market.event_title}",
            f"Question: {market.question}",
            f"Resolution: {market.description[:1500]}",
            f"Outcomes: {', '.join(market.outcomes)}",
        ]
    )


def cosine_similarity(left, right) -> float:
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    if len(left_values) != len(right_values):
        raise ValueError("vectors must have the same length")
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    dot = sum(left_value * right_value for left_value, right_value in zip(left_values, right_values))
    return dot / (left_norm * right_norm)


def _decision_from_data(data: dict) -> LLMGateDecision:
    return LLMGateDecision(
        valid_match=bool(data["valid_match"]),
        p_yes=float(data["p_yes"]),
        confidence=float(data["confidence"]),
        reason=str(data["reason"]),
    )
