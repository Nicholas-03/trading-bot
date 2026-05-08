import pytest
from llm.pricing import compute_cost


def test_chatgpt_cost():
    # 1000 * 0.75 + 500 * 4.50 = 3000; / 1_000_000 = 0.003
    assert compute_cost("gpt-5.4-mini", 1000, 500) == pytest.approx(0.003)


def test_unknown_model_returns_none():
    assert compute_cost("unknown-model-xyz", 1000, 500) is None


def test_zero_tokens_returns_zero():
    assert compute_cost("gpt-5.4-mini", 0, 0) == pytest.approx(0.0)
