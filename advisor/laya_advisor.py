import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from config import Config

logger = logging.getLogger(__name__)

PROVIDER_NAME = "laya"

# The English checkpoint leaves ~320 tokens for the state, so long summaries are truncated.
_MAX_SUMMARY_CHARS = 1200

_INSTRUCTIONS = (
    "You are a short-term stock trader. Based only on this news, what should you do "
    "with the stock in the 'ticker' field during the next hour?"
)

# Options offered depend on the position we already hold in the ticker.
_CRITERIA_BY_POSITION: dict[str, dict[str, str]] = {
    "none": {
        "buy": "the news is clearly good for the company; its share price is likely to rise",
        "short": "the news is clearly bad for the company; its share price is likely to fall",
        "hold": "the news has no clear short-term effect on the share price; do nothing",
    },
    "long": {
        "sell": "the news is bad for the company; close the long position before the price falls",
        "hold": "the news does not hurt the company; keep the long position open",
    },
    "short": {
        "sell": "the news is good for the company; close the short position before the price rises",
        "hold": "the news does not help the company; keep the short position open",
    },
}


@dataclass
class Decision:
    action: Literal["buy", "short", "sell", "hold"]
    ticker: str | None
    reasoning: str
    confidence: float = 0.0
    hold_hours: int = 0
    provider: str = PROVIDER_NAME
    latency_sec: float | None = None
    cost_usd: float | None = None


def _unique_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    clean: list[str] = []
    for symbol in symbols:
        s = str(symbol).strip().upper()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    return clean


def _position_for(ticker: str, held: set[str], shorted: set[str]) -> str:
    if ticker in held:
        return "long"
    if ticker in shorted:
        return "short"
    return "none"


def _build_state(
    headline: str, summary: str, ticker: str, position: str, reaction: float | None = None
) -> dict[str, str]:
    """`reaction`: the stock's return from the last close before the news to now (e.g. 0.012 for +1.2%).
    Only models fine-tuned with it (finetune_laya.py --with-reaction) should be given it."""
    summary = (summary or "").strip()
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS].rstrip() + "..."
    state = {
        "headline": headline,
        "summary": summary or "(no summary)",
        "ticker": ticker,
        "current_position": position,
    }
    if reaction is not None:
        state["price_reaction"] = f"{reaction * 100:+.1f}% since the news"
    return state


def _build_questions(position: str) -> dict[str, dict[str, Any]]:
    return {
        "action": {
            "type": "choice",
            "instructions": _INSTRUCTIONS,
            "criteria": _CRITERIA_BY_POSITION[position],
        }
    }


def _format_probs(probs: dict[str, float]) -> str:
    return " ".join(f"{k}={v:.2f}" for k, v in probs.items())


class LayaAdvisor:
    """Asks the Laya decision model for buy/short/sell/hold on each ticker in a news event."""

    def __init__(self, config: Config, agent: Any = None) -> None:
        self._model_id = config.laya_model_id
        self._subfolder = config.laya_subfolder or None
        self._device = config.laya_device or None
        self._default_hold_hours = config.default_hold_hours
        self._agent = agent

    def load(self) -> None:
        """Load the checkpoint (downloads it from Hugging Face on first use). Blocking."""
        if self._agent is not None:
            return
        import laya

        logger.info(
            "Loading Laya model %s (subfolder=%s, device=%s)",
            self._model_id, self._subfolder, self._device or "auto",
        )
        start = time.monotonic()
        self._agent = laya.load(self._model_id, device=self._device, subfolder=self._subfolder)
        logger.info("Laya model loaded in %.1fs", time.monotonic() - start)

    async def analyze(
        self,
        headline: str,
        summary: str,
        symbols: list[str],
        held_tickers: set[str],
        shorted_tickers: set[str],
    ) -> Decision:
        start = time.monotonic()
        try:
            if self._agent is None:
                await asyncio.to_thread(self.load)
            answers = await asyncio.to_thread(
                self._predict,
                headline,
                summary,
                _unique_symbols(symbols),
                {s.upper() for s in held_tickers},
                {s.upper() for s in shorted_tickers},
            )
            decision = self._pick_decision(answers)
        except Exception as e:
            logger.error("Laya inference error: %s", e)
            decision = Decision(action="hold", ticker=None, reasoning=f"laya error: {e}")
        decision.latency_sec = time.monotonic() - start
        return decision

    def _predict(
        self,
        headline: str,
        summary: str,
        symbols: list[str],
        held: set[str],
        shorted: set[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return (ticker, answer) pairs; one batched forward pass per position group."""
        groups: dict[str, list[str]] = {}
        for ticker in symbols:
            groups.setdefault(_position_for(ticker, held, shorted), []).append(ticker)

        results: list[tuple[str, dict[str, Any]]] = []
        for position, tickers in groups.items():
            states = [_build_state(headline, summary, t, position) for t in tickers]
            outputs = self._agent.predict_batch(states, _build_questions(position))
            for ticker, output in zip(tickers, outputs):
                results.append((ticker, output["answers"]["action"]))
        return results

    def _pick_decision(self, answers: list[tuple[str, dict[str, Any]]]) -> Decision:
        """Take the most confident non-hold answer; hold if every ticker says hold."""
        for ticker, answer in answers:
            logger.info(
                "LAYA %s: %s confidence=%.2f [%s]",
                ticker, answer["choice"], answer["confidence"], _format_probs(answer["probabilities"]),
            )

        actionable = [(t, a) for t, a in answers if a["choice"] != "hold"]
        if not actionable:
            return Decision(
                action="hold",
                ticker=None,
                reasoning=f"laya chose hold for all tickers ({', '.join(t for t, _ in answers) or 'none'})",
            )

        ticker, answer = max(actionable, key=lambda item: item[1]["confidence"])
        action = answer["choice"]
        return Decision(
            action=action,
            ticker=ticker,
            reasoning=f"laya probabilities: {_format_probs(answer['probabilities'])}",
            confidence=float(answer["confidence"]),
            hold_hours=self._default_hold_hours if action in ("buy", "short") else 0,
        )
