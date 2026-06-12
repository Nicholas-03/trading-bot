from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class FillEstimate:
    shares: float
    spent_usd: float
    unfilled_usd: float
    avg_price: float | None
    max_price_paid: float | None


@dataclass(frozen=True)
class ParsedOrderBook:
    token_id: str
    market: str
    timestamp_ms: int | None
    bids: list[BookLevel]
    asks: list[BookLevel]
    min_order_size: float | None
    tick_size: float | None
    last_trade_price: float | None


@dataclass(frozen=True)
class OrderBookMetrics:
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    mid: float | None
    ask_depth_usd_1c: float
    ask_depth_usd_5c: float
    bid_depth_usd_1c: float
    bid_depth_usd_5c: float
    buy_shares: float
    buy_spent_usd: float
    buy_unfilled_usd: float
    buy_avg_price: float | None
    buy_max_price_paid: float | None
    buy_price_impact: float | None
    buy_fillable_within_slippage: bool


def parse_order_book(raw: dict[str, Any], token_id: str | None = None) -> ParsedOrderBook:
    return ParsedOrderBook(
        token_id=str(token_id or raw.get("asset_id") or ""),
        market=str(raw.get("market") or ""),
        timestamp_ms=_optional_int(raw.get("timestamp")),
        bids=_parse_levels(raw.get("bids"), reverse=True),
        asks=_parse_levels(raw.get("asks"), reverse=False),
        min_order_size=_optional_float(raw.get("min_order_size")),
        tick_size=_optional_float(raw.get("tick_size")),
        last_trade_price=_optional_float(raw.get("last_trade_price")),
    )


def estimate_buy_fill(
    asks: list[BookLevel],
    notional_usd: float,
    max_price: float | None = None,
) -> FillEstimate:
    remaining = notional_usd
    shares = 0.0
    spent = 0.0
    max_paid: float | None = None

    for level in asks:
        if max_price is not None and level.price > max_price:
            break
        level_capacity = level.price * level.size
        spend_here = min(remaining, level_capacity)
        if spend_here <= 0:
            continue
        shares += spend_here / level.price
        spent += spend_here
        remaining -= spend_here
        max_paid = level.price
        if remaining <= 1e-9:
            remaining = 0.0
            break

    avg_price = spent / shares if shares > 0 else None
    return FillEstimate(
        shares=round(shares, 10),
        spent_usd=round(spent, 10),
        unfilled_usd=round(max(0.0, remaining), 10),
        avg_price=round(avg_price, 10) if avg_price is not None else None,
        max_price_paid=max_paid,
    )


def summarize_order_book(
    book: ParsedOrderBook,
    notional_usd: float = 100.0,
    max_buy_slippage: float = 0.02,
) -> OrderBookMetrics:
    best_bid = book.bids[0].price if book.bids else None
    best_ask = book.asks[0].price if book.asks else None
    spread = None
    mid = None
    if best_bid is not None and best_ask is not None:
        spread = round(best_ask - best_bid, 10)
        mid = round((best_ask + best_bid) / 2, 10)

    ask_depth_usd_1c = _ask_depth_usd(book.asks, best_ask, 0.01)
    ask_depth_usd_5c = _ask_depth_usd(book.asks, best_ask, 0.05)
    bid_depth_usd_1c = _bid_depth_usd(book.bids, best_bid, 0.01)
    bid_depth_usd_5c = _bid_depth_usd(book.bids, best_bid, 0.05)

    max_buy_price = best_ask + max_buy_slippage if best_ask is not None else None
    fill = estimate_buy_fill(book.asks, notional_usd, max_price=max_buy_price)
    price_impact = None
    if best_ask is not None and fill.avg_price is not None:
        price_impact = round(fill.avg_price - best_ask, 10)

    return OrderBookMetrics(
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        mid=mid,
        ask_depth_usd_1c=ask_depth_usd_1c,
        ask_depth_usd_5c=ask_depth_usd_5c,
        bid_depth_usd_1c=bid_depth_usd_1c,
        bid_depth_usd_5c=bid_depth_usd_5c,
        buy_shares=fill.shares,
        buy_spent_usd=fill.spent_usd,
        buy_unfilled_usd=fill.unfilled_usd,
        buy_avg_price=fill.avg_price,
        buy_max_price_paid=fill.max_price_paid,
        buy_price_impact=price_impact,
        buy_fillable_within_slippage=fill.unfilled_usd <= 1e-9,
    )


def _parse_levels(raw_levels: Any, reverse: bool) -> list[BookLevel]:
    levels: list[BookLevel] = []
    if not isinstance(raw_levels, list):
        return levels
    for raw in raw_levels:
        if not isinstance(raw, dict):
            continue
        price = _optional_float(raw.get("price"))
        size = _optional_float(raw.get("size"))
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        levels.append(BookLevel(price=price, size=size))
    return sorted(levels, key=lambda level: level.price, reverse=reverse)


def _ask_depth_usd(asks: list[BookLevel], best_ask: float | None, window: float) -> float:
    if best_ask is None:
        return 0.0
    limit = best_ask + window
    return round(sum(level.price * level.size for level in asks if level.price <= limit), 10)


def _bid_depth_usd(bids: list[BookLevel], best_bid: float | None, window: float) -> float:
    if best_bid is None:
        return 0.0
    limit = best_bid - window
    return round(sum(level.price * level.size for level in bids if level.price >= limit), 10)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
