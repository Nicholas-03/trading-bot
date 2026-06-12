from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import openai

from analytics.polymarket_backtest import GammaMarket, NewsEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMGateDecision:
    valid_match: bool
    p_yes: float
    confidence: float
    reason: str


class LLMGate:
    stats_prefix = "llm"

    def __init__(
        self,
        api_key: str,
        model: str,
        cache_path: str = "data/llm_gate_cache.json",
        max_calls: int = 25,
        min_confidence: float = 0.60,
        max_completion_tokens: int = 180,
    ) -> None:
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model
        self._cache_path = Path(cache_path)
        self._max_calls = max_calls
        self._min_confidence = min_confidence
        self._max_completion_tokens = max_completion_tokens
        self.calls = 0
        self.cache_hits = 0
        self.limit_skips = 0
        self._cache = self._load_cache()

    def evaluate(self, news: NewsEvent, market: GammaMarket) -> LLMGateDecision:
        key = cache_key_for_gate(news, market)
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return _decision_from_cache(cached)
        if self.calls >= self._max_calls:
            self.limit_skips += 1
            return LLMGateDecision(False, 0.5, 0.0, "llm call limit reached")

        self.calls += 1
        prompt = build_gate_prompt(news, market)
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                max_completion_tokens=self._max_completion_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
            )
            content = response.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("LLM gate call failed for market %s: %s", market.id, exc)
            decision = LLMGateDecision(False, 0.5, 0.0, f"llm error: {exc}")
        else:
            decision = parse_gate_response(content)
            if decision.valid_match and decision.confidence < self._min_confidence:
                decision = LLMGateDecision(
                    False,
                    decision.p_yes,
                    decision.confidence,
                    f"low confidence: {decision.reason}",
                )

        self._cache[key] = asdict(decision)
        self._save_cache()
        return decision

    def _load_cache(self) -> dict[str, dict]:
        if not self._cache_path.exists():
            return {}
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8")


def parse_gate_response(text: str) -> LLMGateDecision:
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        pos = text.find("{", idx)
        if pos == -1:
            break
        try:
            data, _ = decoder.raw_decode(text, pos)
            return _decision_from_cache(data)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            idx = pos + 1
    return LLMGateDecision(False, 0.5, 0.0, "parse error: no valid gate JSON found")


def cache_key_for_gate(news: NewsEvent, market: GammaMarket) -> str:
    payload = {
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


def apply_llm_gate_decision(
    decision: LLMGateDecision,
) -> tuple[Literal["yes", "no"], float, str] | None:
    if not decision.valid_match:
        return None
    p_yes = min(1.0, max(0.0, decision.p_yes))
    if p_yes >= 0.5:
        return "yes", p_yes, decision.reason
    return "no", 1 - p_yes, decision.reason


def build_gate_prompt(news: NewsEvent, market: GammaMarket) -> str:
    return f"""\
You are validating whether a real-time event is tradable for a Polymarket prediction market.

Return ONLY a JSON object with exactly:
{{"valid_match": boolean, "p_yes": number, "confidence": number, "reason": "short explanation"}}

Definitions:
- valid_match=true only if the news/event directly affects the exact market question and resolution rules.
- valid_match=false for generic topical overlap, same country only, same institution only, word/speech markets without speech evidence, or unclear GDELT actor/event noise.
- p_yes is your estimated probability that the YES outcome resolves true after this event, between 0 and 1.
- confidence is confidence in the match and probability estimate, between 0 and 1.
- Be conservative. If unsure, valid_match=false.

Event:
Time UTC: {news.ts.isoformat()}
Headline: {news.headline}
Summary: {news.summary}
Symbols: {", ".join(news.symbols) if news.symbols else "none"}

Polymarket:
Question: {market.question}
Outcomes: {", ".join(market.outcomes)}
Resolution/rules: {market.description[:2500]}
"""


def _decision_from_cache(data: dict) -> LLMGateDecision:
    valid_match = bool(data["valid_match"])
    p_yes = float(data["p_yes"])
    confidence = float(data["confidence"])
    reason = str(data["reason"])
    if not 0 <= p_yes <= 1:
        raise ValueError("p_yes outside [0, 1]")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence outside [0, 1]")
    return LLMGateDecision(valid_match, p_yes, confidence, reason)
