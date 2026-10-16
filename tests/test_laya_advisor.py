import asyncio
from types import SimpleNamespace

from advisor.laya_advisor import LayaAdvisor


def _config():
    return SimpleNamespace(
        laya_model_id="convaiinnovations/laya",
        laya_subfolder="",
        laya_device="",
        default_hold_hours=1,
    )


def _answer(choice: str, confidence: float, probs: dict[str, float]) -> dict:
    return {"type": "choice", "choice": choice, "probabilities": probs, "confidence": confidence}


class FakeAgent:
    """Mimics laya.Agent.predict_batch; answers come from a {ticker: answer} map."""

    def __init__(self, answers: dict[str, dict]) -> None:
        self._answers = answers
        self.calls: list[tuple[list[dict], dict]] = []

    def predict_batch(self, states, questions):
        self.calls.append((states, questions))
        return [{"answers": {"action": self._answers[s["ticker"]]}} for s in states]


def _analyze(agent, symbols, held=(), shorted=()):
    advisor = LayaAdvisor(_config(), agent=agent)
    return asyncio.run(
        advisor.analyze("headline", "summary", symbols, set(held), set(shorted))
    )


def test_buy_decision_uses_laya_choice_and_confidence():
    agent = FakeAgent({"AAPL": _answer("buy", 0.82, {"buy": 0.9, "short": 0.05, "hold": 0.05})})

    decision = _analyze(agent, ["AAPL"])

    assert decision.action == "buy"
    assert decision.ticker == "AAPL"
    assert decision.confidence == 0.82
    assert decision.hold_hours == 1
    assert decision.provider == "laya"
    assert decision.cost_usd is None
    assert decision.latency_sec is not None


def test_all_hold_returns_hold():
    agent = FakeAgent({
        "AAPL": _answer("hold", 0.9, {"buy": 0.02, "short": 0.02, "hold": 0.96}),
        "MSFT": _answer("hold", 0.8, {"buy": 0.05, "short": 0.05, "hold": 0.9}),
    })

    decision = _analyze(agent, ["AAPL", "MSFT"])

    assert decision.action == "hold"
    assert decision.ticker is None


def test_most_confident_non_hold_ticker_wins():
    agent = FakeAgent({
        "AAPL": _answer("buy", 0.6, {"buy": 0.8, "short": 0.1, "hold": 0.1}),
        "MSFT": _answer("short", 0.9, {"buy": 0.01, "short": 0.98, "hold": 0.01}),
        "GOOG": _answer("hold", 0.99, {"buy": 0.0, "short": 0.0, "hold": 1.0}),
    })

    decision = _analyze(agent, ["AAPL", "MSFT", "GOOG"])

    assert (decision.action, decision.ticker, decision.confidence) == ("short", "MSFT", 0.9)


def test_options_depend_on_current_position():
    agent = FakeAgent({
        "AAPL": _answer("hold", 0.5, {"sell": 0.4, "hold": 0.6}),
        "SPY": _answer("hold", 0.5, {"sell": 0.4, "hold": 0.6}),
        "NVDA": _answer("hold", 0.5, {"buy": 0.3, "short": 0.2, "hold": 0.5}),
    })

    _analyze(agent, ["AAPL", "SPY", "NVDA"], held={"AAPL"}, shorted={"SPY"})

    options = {
        states[0]["current_position"]: set(questions["action"]["criteria"])
        for states, questions in agent.calls
    }
    assert options == {
        "long": {"sell", "hold"},
        "short": {"sell", "hold"},
        "none": {"buy", "short", "hold"},
    }


def test_sell_decision_for_held_ticker_has_no_hold_hours():
    agent = FakeAgent({"AAPL": _answer("sell", 0.75, {"sell": 0.95, "hold": 0.05})})

    decision = _analyze(agent, ["AAPL"], held={"AAPL"})

    assert (decision.action, decision.ticker, decision.hold_hours) == ("sell", "AAPL", 0)


def test_symbols_are_deduplicated_and_uppercased():
    agent = FakeAgent({"AAPL": _answer("hold", 0.9, {"buy": 0.0, "short": 0.0, "hold": 1.0})})

    _analyze(agent, ["aapl", "AAPL", " AAPL "])

    states, _ = agent.calls[0]
    assert [s["ticker"] for s in states] == ["AAPL"]


def test_long_summary_is_truncated():
    agent = FakeAgent({"AAPL": _answer("hold", 0.9, {"buy": 0.0, "short": 0.0, "hold": 1.0})})
    advisor = LayaAdvisor(_config(), agent=agent)

    asyncio.run(advisor.analyze("headline", "x" * 5000, ["AAPL"], set(), set()))

    states, _ = agent.calls[0]
    assert len(states[0]["summary"]) < 1300


def test_inference_error_returns_hold():
    class BrokenAgent:
        def predict_batch(self, states, questions):
            raise RuntimeError("boom")

    decision = _analyze(BrokenAgent(), ["AAPL"])

    assert decision.action == "hold"
    assert "boom" in decision.reasoning
