import argparse
import csv
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.market_relevant_backtest import (  # noqa: E402
    _EXCLUDED_THRESHOLD_SYMBOLS,
    _is_corporate_catalyst_news,
    _is_same_ticker_single_name_threshold,
    market_has_enough_time_to_end,
)
from analytics.order_book import parse_order_book, summarize_order_book  # noqa: E402
from analytics.polymarket_backtest import (  # noqa: E402
    NewsEvent,
    PolymarketClient,
    is_market_open_at,
    load_news_events,
)


FIELDNAMES = [
    "snapshot_ts",
    "symbol",
    "market_id",
    "market_question",
    "market_end_date",
    "market_volume",
    "market_liquidity",
    "outcome",
    "token_id",
    "orderbook_ts_ms",
    "best_bid",
    "best_ask",
    "spread",
    "mid",
    "two_sided_book",
    "ask_depth_usd_1c",
    "ask_depth_usd_5c",
    "bid_depth_usd_1c",
    "bid_depth_usd_5c",
    "buy_notional_usd",
    "buy_shares",
    "buy_spent_usd",
    "buy_unfilled_usd",
    "buy_avg_price",
    "buy_max_price_paid",
    "buy_price_impact",
    "buy_fillable_within_slippage",
    "bid_levels",
    "ask_levels",
    "error",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect live Polymarket CLOB order book snapshots for threshold markets.")
    parser.add_argument("--db", default="data/trades.db")
    parser.add_argument("--output", default="data/polymarket_order_book_snapshots.csv")
    parser.add_argument("--symbols", default="", help="Comma-separated override. Defaults to top symbols from corporate news.")
    parser.add_argument("--max-symbols", type=int, default=50)
    parser.add_argument("--search-limit", type=int, default=20)
    parser.add_argument("--max-markets-per-symbol", type=int, default=30)
    parser.add_argument("--min-hours-to-end", type=int, default=72)
    parser.add_argument("--max-volume", type=float, default=50_000.0)
    parser.add_argument("--max-liquidity", type=float, default=20_000.0)
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--max-buy-slippage", type=float, default=0.02)
    parser.add_argument("--include-all-news-symbols", action="store_true")
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args(argv)

    snapshot_ts = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc)
    symbols = _symbols_from_args_or_db(args.symbols, args.db, args.max_symbols, args.include_all_news_symbols)

    client = PolymarketClient()
    rows: list[dict[str, Any]] = []
    stats = Counter(symbols_loaded=len(symbols))
    try:
        for symbol in symbols:
            markets = _discover_symbol_markets(client, symbol, args.search_limit)
            stats["markets_seen"] += len(markets)
            kept_for_symbol = 0
            for market in markets:
                if kept_for_symbol >= args.max_markets_per_symbol:
                    break
                fake_news = NewsEvent(id=0, ts=now, headline=symbol, summary="", symbols=[symbol])
                if not _is_same_ticker_single_name_threshold(fake_news, market):
                    stats["pattern_filtered"] += 1
                    continue
                if not is_market_open_at(market, now):
                    stats["not_open"] += 1
                    continue
                if not market_has_enough_time_to_end(market, now, args.min_hours_to_end):
                    stats["too_near_expiry"] += 1
                    continue
                if market.volume > args.max_volume:
                    stats["high_volume"] += 1
                    continue
                if market.liquidity > args.max_liquidity:
                    stats["high_liquidity"] += 1
                    continue
                kept_for_symbol += 1
                stats["markets_kept"] += 1
                rows.extend(
                    _snapshot_market_books(
                        client=client,
                        symbol=symbol,
                        market=market,
                        snapshot_ts=snapshot_ts,
                        notional=args.notional,
                        max_buy_slippage=args.max_buy_slippage,
                    )
                )
    finally:
        client.close()

    stats["book_rows"] = len(rows)
    stats["book_errors"] = sum(1 for row in rows if row["error"])
    stats["fillable_100_within_slippage"] = sum(1 for row in rows if row["buy_fillable_within_slippage"] == "true")
    _write_rows(args.output, rows, append=args.append)
    print(dict(stats))
    print(f"Wrote {len(rows)} order book rows to {args.output}")
    return 0


def _symbols_from_args_or_db(
    raw_symbols: str,
    db_path: str,
    max_symbols: int,
    include_all_news_symbols: bool,
) -> list[str]:
    if raw_symbols.strip():
        symbols = [symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip()]
        return _dedupe_symbols(symbols)[:max_symbols]

    counts: Counter[str] = Counter()
    for news in load_news_events(db_path):
        if not include_all_news_symbols and not _is_corporate_catalyst_news(news):
            continue
        for symbol in news.symbols:
            symbol = symbol.upper()
            if symbol and symbol not in _EXCLUDED_THRESHOLD_SYMBOLS:
                counts[symbol] += 1
    return [symbol for symbol, _ in counts.most_common(max_symbols)]


def _discover_symbol_markets(client: PolymarketClient, symbol: str, search_limit: int):
    seen: set[str] = set()
    markets = []
    for query in (f"{symbol} high", f"{symbol} low", symbol):
        try:
            candidates = client.search_markets(query, limit=search_limit)
        except httpx.HTTPError:
            continue
        for market in candidates:
            if market.id in seen:
                continue
            seen.add(market.id)
            markets.append(market)
    return markets


def _snapshot_market_books(
    client: PolymarketClient,
    symbol: str,
    market,
    snapshot_ts: str,
    notional: float,
    max_buy_slippage: float,
) -> list[dict[str, Any]]:
    rows = []
    for outcome, token_id in (("yes", market.yes_token_id), ("no", market.no_token_id)):
        if token_id is None:
            rows.append(_base_row(snapshot_ts, symbol, market, outcome, "", error="missing_token_id"))
            continue
        try:
            raw_book = client.order_book(token_id)
            book = parse_order_book(raw_book, token_id=token_id)
            metrics = summarize_order_book(book, notional_usd=notional, max_buy_slippage=max_buy_slippage)
            two_sided_book = metrics.best_bid is not None and metrics.best_ask is not None
            row = _base_row(snapshot_ts, symbol, market, outcome, token_id, error="")
            row.update(
                {
                    "orderbook_ts_ms": book.timestamp_ms,
                    "best_bid": metrics.best_bid,
                    "best_ask": metrics.best_ask,
                    "spread": metrics.spread,
                    "mid": metrics.mid,
                    "two_sided_book": str(two_sided_book).lower(),
                    "ask_depth_usd_1c": metrics.ask_depth_usd_1c,
                    "ask_depth_usd_5c": metrics.ask_depth_usd_5c,
                    "bid_depth_usd_1c": metrics.bid_depth_usd_1c,
                    "bid_depth_usd_5c": metrics.bid_depth_usd_5c,
                    "buy_notional_usd": notional,
                    "buy_shares": metrics.buy_shares,
                    "buy_spent_usd": metrics.buy_spent_usd,
                    "buy_unfilled_usd": metrics.buy_unfilled_usd,
                    "buy_avg_price": metrics.buy_avg_price,
                    "buy_max_price_paid": metrics.buy_max_price_paid,
                    "buy_price_impact": metrics.buy_price_impact,
                    "buy_fillable_within_slippage": str(metrics.buy_fillable_within_slippage).lower(),
                    "bid_levels": len(book.bids),
                    "ask_levels": len(book.asks),
                }
            )
            rows.append(row)
        except httpx.HTTPError as exc:
            rows.append(_base_row(snapshot_ts, symbol, market, outcome, token_id, error=str(exc)))
    return rows


def _base_row(snapshot_ts: str, symbol: str, market, outcome: str, token_id: str, error: str) -> dict[str, Any]:
    return {
        "snapshot_ts": snapshot_ts,
        "symbol": symbol,
        "market_id": market.id,
        "market_question": market.question,
        "market_end_date": market.end_date.isoformat() if market.end_date else "",
        "market_volume": market.volume,
        "market_liquidity": market.liquidity,
        "outcome": outcome,
        "token_id": token_id,
        "orderbook_ts_ms": "",
        "best_bid": "",
        "best_ask": "",
        "spread": "",
        "mid": "",
        "two_sided_book": "",
        "ask_depth_usd_1c": "",
        "ask_depth_usd_5c": "",
        "bid_depth_usd_1c": "",
        "bid_depth_usd_5c": "",
        "buy_notional_usd": "",
        "buy_shares": "",
        "buy_spent_usd": "",
        "buy_unfilled_usd": "",
        "buy_avg_price": "",
        "buy_max_price_paid": "",
        "buy_price_impact": "",
        "buy_fillable_within_slippage": "",
        "bid_levels": "",
        "ask_levels": "",
        "error": error,
    }


def _write_rows(path: str, rows: list[dict[str, Any]], append: bool) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_header = not append or not output.exists() or output.stat().st_size == 0
    mode = "a" if append else "w"
    with output.open(mode, newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol and symbol not in seen:
            seen.add(symbol)
            deduped.append(symbol)
    return deduped


if __name__ == "__main__":
    raise SystemExit(main())
