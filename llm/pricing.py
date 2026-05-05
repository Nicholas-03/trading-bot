import logging

logger = logging.getLogger(__name__)

# (input_price_per_1M_tokens, output_price_per_1M_tokens) in USD
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "gpt-5.4-mini": (0.75, 4.50),
}


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    pricing = PRICING.get(model)
    if pricing is None:
        logger.warning("No pricing data for model %r — cost will be NULL", model)
        return None
    in_price, out_price = pricing
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000
