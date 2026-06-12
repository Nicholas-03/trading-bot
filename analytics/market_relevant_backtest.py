from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx

from analytics.polymarket_backtest import (
    GammaMarket,
    NewsEvent,
    PolymarketClient,
    candidate_score,
    is_market_open_at,
    load_news_events,
    search_queries_for_news,
    should_consider_news,
    simulate_long_binary,
)

logger = logging.getLogger(__name__)

Side = Literal["yes", "no"]
DedupeMode = Literal["none", "headline", "market-side"]

_EXCLUDED_THRESHOLD_SYMBOLS = frozenset(
    {
        "SPY",
        "QQQ",
        "DIA",
        "IWM",
        "VIX",
        "TLT",
        "HYG",
        "GLD",
        "SLV",
        "USO",
        "BTC",
        "ETH",
        "SOL",
        "BTCUSD",
        "ETHUSD",
        "SOLUSD",
    }
)
_INDEX_MARKET_TERMS = (
    "s&p 500",
    "nasdaq 100",
    "dow jones",
    "russell 2000",
    "bitcoin",
    "ethereum",
    "crypto",
    "oil",
    "gold",
    "silver",
)
_CORPORATE_CATALYST_TERMS = (
    "earnings",
    "results",
    "revenue",
    "eps",
    "guidance",
    "outlook",
    "q1",
    "q2",
    "q3",
    "q4",
    "acquisition",
    "merger",
    "bid",
    "buyout",
    "takeover",
    "fda",
    "approval",
    "approved",
    "phase",
    "trial",
    "contract",
    "partnership",
    "agreement",
    "lawsuit",
    "antitrust",
    "probe",
    "investigation",
    "settlement",
    "patent infringement",
)
_MACRO_OR_BROAD_MARKET_TERMS = (
    "federal reserve",
    "fed's",
    "inflation",
    "cpi",
    "jobs report",
    "tariff",
    "tariffs",
    "treasury",
    "broader market",
    "broader sector",
    "sector are trading",
)
_POSITIVE_TERMS = (
    "beat",
    "beats",
    "upbeat",
    "double beat",
    "strong",
    "record results",
    "raises guidance",
    "raised guidance",
    "raises outlook",
    "boosts outlook",
    "analysts raise",
    "increase their forecasts",
    "increase forecasts",
    "raises price target",
    "price targets",
    "surges",
    "soars",
    "climbs",
    "rallies",
    "moves higher",
    "positive results",
    "approval",
    "approved",
)
_NEGATIVE_TERMS = (
    "miss",
    "misses",
    "cuts guidance",
    "lowers guidance",
    "lowers outlook",
    "weak",
    "slides",
    "falls",
    "sinks",
    "crashes",
    "lawsuit",
    "contempt",
    "antitrust",
    "probe",
    "investigation",
    "patent infringement",
)
_PREVIEW_TERMS = (
    "preview",
    "ahead of",
    "tomorrow",
    "gears up",
    "what to expect",
    "watch",
    "set for",
)
_MNA_TERMS = ("acquire", "acquired", "acquisition", "merger", "bid", "buyout", "takeover")


@dataclass(frozen=True)
class MarketRelevantSignal:
    side: Side
    probability: float
    reason: str


@dataclass(frozen=True)
class MarketRelevantConfig:
    min_candidate_score: float = 0.60
    max_volume: float = 50_000.0
    max_liquidity: float = 20_000.0
    notional: float = 100.0
    fee_bps: float = 10.0
    entry_window_minutes: int = 15
    horizon_hours: int = 72
    min_hours_to_end: int = 72
    min_entry_price: float = 0.0
    max_entry_price: float = 1.0
    target_move: float = 0.10
    stop_move: float = 0.0
    search_limit: int = 7
    max_markets_per_news: int = 12
    dedupe_mode: DedupeMode = "headline"
    include_symbols: tuple[str, ...] = ()
    exclude_symbols: tuple[str, ...] = ()
    single_name_threshold_only: bool = False
    corporate_catalyst_only: bool = False
    request_sleep_seconds: float = 0.0


@dataclass(frozen=True)
class ExitPoint:
    timestamp: int
    price: float
    reason: str


@dataclass(frozen=True)
class MarketRelevantTrade:
    news_id: int
    news_ts: datetime
    headline: str
    symbols: list[str]
    market_id: str
    market_question: str
    side: Side
    volume: float
    liquidity: float
    candidate_score: float
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str
    exit_delay_hours: float
    pnl_usd: float
    roi_pct: float
    signal_probability: float
    signal_reason: str


def infer_market_relevant_signal(news: NewsEvent, market: GammaMarket) -> MarketRelevantSignal | None:
    news_text = _norm(" ".join([news.headline, news.summary]))
    market_text = _norm(" ".join([market.question, market.description]))
    sentiment = _sentiment(news_text)

    if any(term in market_text for term in _MNA_TERMS) and any(term in news_text for term in _MNA_TERMS):
        return MarketRelevantSignal("yes", 0.70, "market-relevant M&A/bid signal")

    if "beat quarterly earnings" in market_text:
        if any(term in news_text for term in _PREVIEW_TERMS):
            return None
        if sentiment == "positive":
            return MarketRelevantSignal("yes", 0.65, "market-relevant positive earnings signal")
        if sentiment == "negative":
            return MarketRelevantSignal("no", 0.65, "market-relevant negative earnings signal")
        return None

    high_market = "hit (high)" in market_text or " hit high " in market_text
    low_market = "hit (low)" in market_text or " hit low " in market_text
    if high_market:
        if sentiment == "positive":
            return MarketRelevantSignal("yes", 0.62, "positive stock/news signal for HIGH threshold")
        if sentiment == "negative":
            return MarketRelevantSignal("no", 0.62, "negative stock/news signal against HIGH threshold")
    if low_market:
        if sentiment == "positive":
            return MarketRelevantSignal("no", 0.62, "positive stock/news signal against LOW threshold")
        if sentiment == "negative":
            return MarketRelevantSignal("yes", 0.62, "negative stock/news signal for LOW threshold")
    return None


def parse_history_points(history: list[dict]) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    for point in history:
        try:
            timestamp = int(point["t"])
            price = float(point["p"])
        except (KeyError, TypeError, ValueError):
            continue
        points.append((timestamp, price))
    return sorted(points)


def first_price_at_or_after(
    points: list[tuple[int, float]],
    target_ts: int,
    max_delay_seconds: int | None = None,
) -> tuple[int, float] | None:
    for timestamp, price in points:
        if timestamp < target_ts:
            continue
        if max_delay_seconds is not None and timestamp - target_ts > max_delay_seconds:
            return None
        return timestamp, price
    return None


def latest_price_before_or_at(points: list[tuple[int, float]], target_ts: int) -> tuple[int, float] | None:
    latest: tuple[int, float] | None = None
    for timestamp, price in points:
        if timestamp > target_ts:
            break
        latest = (timestamp, price)
    return latest


def choose_exit(
    points: list[tuple[int, float]],
    signal_ts: int,
    entry_price: float,
    config: MarketRelevantConfig,
) -> ExitPoint | None:
    horizon_ts = signal_ts + config.horizon_hours * 3600
    target_label = int(round(config.target_move * 100))
    stop_label = int(round(config.stop_move * 100))
    for timestamp, price in points:
        if timestamp < signal_ts:
            continue
        if timestamp > horizon_ts:
            break
        if config.stop_move > 0 and entry_price - price >= config.stop_move:
            return ExitPoint(timestamp, price, f"stop_loss_{stop_label}c")
        if price - entry_price >= config.target_move:
            return ExitPoint(timestamp, price, f"take_profit_{target_label}c")
    fallback = latest_price_before_or_at(points, horizon_ts)
    if fallback is None:
        return None
    return ExitPoint(fallback[0], fallback[1], f"horizon_{config.horizon_hours}h")


def market_has_enough_time_to_end(market: GammaMarket, news_ts: datetime, min_hours_to_end: int) -> bool:
    if min_hours_to_end <= 0 or market.end_date is None:
        return True
    remaining = market.end_date.astimezone(timezone.utc) - news_ts.astimezone(timezone.utc)
    return remaining.total_seconds() >= min_hours_to_end * 3600


def market_relevant_queries_for_news(news: NewsEvent, search_limit: int) -> list[str]:
    queries = search_queries_for_news(news, max_queries=search_limit)
    for symbol in news.symbols[:3]:
        queries.extend([symbol, f"{symbol} stock", f"{symbol} high", f"{symbol} low"])
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(query.strip())
    return deduped[:search_limit]


def run_market_relevant_backtest(
    db_path: str,
    client: PolymarketClient,
    config: MarketRelevantConfig,
    limit: int | None = None,
) -> tuple[list[MarketRelevantTrade], dict[str, int]]:
    news_events = load_news_events(db_path, limit=limit)
    stats = {
        "news_total": len(news_events),
        "news_considered": 0,
        "candidate_markets": 0,
        "matched_markets": 0,
        "market_relevant_candidates": 0,
        "open_at_news": 0,
        "skipped_high_volume": 0,
        "skipped_high_liquidity": 0,
        "no_history": 0,
        "missing_entry": 0,
        "missing_exit": 0,
        "symbol_filtered": 0,
        "pattern_filtered": 0,
        "dedupe_skipped": 0,
        "trades": 0,
    }
    trades: list[MarketRelevantTrade] = []
    seen_trade_keys: set[tuple[str, ...]] = set()

    for news in news_events:
        if not should_consider_news(news):
            continue
        if not _symbol_filter_allows(news.symbols, config.include_symbols, config.exclude_symbols):
            stats["symbol_filtered"] += 1
            continue
        if config.corporate_catalyst_only and not _is_corporate_catalyst_news(news):
            stats["pattern_filtered"] += 1
            continue
        stats["news_considered"] += 1
        seen_market_ids: set[str] = set()
        candidates: list[tuple[float, GammaMarket]] = []
        for query in market_relevant_queries_for_news(news, config.search_limit):
            try:
                markets = client.search_markets(query, limit=config.search_limit)
            except httpx.HTTPError as exc:
                logger.warning("Polymarket search failed for %r: %s", query, exc)
                continue
            time.sleep(config.request_sleep_seconds)
            stats["candidate_markets"] += len(markets)
            for market in markets:
                if market.id in seen_market_ids:
                    continue
                seen_market_ids.add(market.id)
                score = candidate_score(news, market)
                if score >= config.min_candidate_score:
                    candidates.append((score, market))

        candidates.sort(key=lambda item: item[0], reverse=True)
        stats["matched_markets"] += len(candidates)
        for score, market in candidates[: config.max_markets_per_news]:
            if config.single_name_threshold_only and not _is_same_ticker_single_name_threshold(news, market):
                stats["pattern_filtered"] += 1
                continue
            if market.volume > config.max_volume:
                stats["skipped_high_volume"] += 1
                continue
            if market.liquidity > config.max_liquidity:
                stats["skipped_high_liquidity"] += 1
                continue
            if not is_market_open_at(market, news.ts):
                continue
            if not market_has_enough_time_to_end(market, news.ts, config.min_hours_to_end):
                continue
            stats["open_at_news"] += 1
            signal = infer_market_relevant_signal(news, market)
            if signal is None:
                continue
            stats["market_relevant_candidates"] += 1
            dedupe_key = _trade_dedupe_key(market.id, signal.side, news.headline, config.dedupe_mode)
            if dedupe_key is not None:
                if dedupe_key in seen_trade_keys:
                    stats["dedupe_skipped"] += 1
                    continue
                seen_trade_keys.add(dedupe_key)

            token_id = market.yes_token_id if signal.side == "yes" else market.no_token_id
            if token_id is None:
                continue
            signal_ts = int(news.ts.astimezone(timezone.utc).timestamp())
            start_ts = int((news.ts - timedelta(minutes=5)).timestamp())
            end_ts = int((news.ts + timedelta(hours=config.horizon_hours)).timestamp())
            try:
                history = client.price_history(token_id, start_ts, end_ts)
            except httpx.HTTPError as exc:
                logger.warning("Polymarket history failed for market %s: %s", market.id, exc)
                continue
            points = parse_history_points(history)
            if not points:
                stats["no_history"] += 1
                continue
            entry = first_price_at_or_after(
                points,
                signal_ts,
                max_delay_seconds=config.entry_window_minutes * 60,
            )
            if entry is None or not 0 < entry[1] < 1:
                stats["missing_entry"] += 1
                continue
            if entry[1] < config.min_entry_price or entry[1] > config.max_entry_price:
                stats["missing_entry"] += 1
                continue
            exit_point = choose_exit(points, signal_ts, entry[1], config)
            if exit_point is None or exit_point.timestamp <= entry[0]:
                stats["missing_exit"] += 1
                continue

            simulation = simulate_long_binary(entry[1], exit_point.price, config.notional, config.fee_bps)
            stats["trades"] += 1
            trades.append(
                MarketRelevantTrade(
                    news_id=news.id,
                    news_ts=news.ts,
                    headline=news.headline,
                    symbols=news.symbols,
                    market_id=market.id,
                    market_question=market.question,
                    side=signal.side,
                    volume=market.volume,
                    liquidity=market.liquidity,
                    candidate_score=score,
                    entry_ts=datetime.fromtimestamp(entry[0], tz=timezone.utc),
                    entry_price=entry[1],
                    exit_ts=datetime.fromtimestamp(exit_point.timestamp, tz=timezone.utc),
                    exit_price=exit_point.price,
                    exit_reason=exit_point.reason,
                    exit_delay_hours=round((exit_point.timestamp - signal_ts) / 3600, 4),
                    pnl_usd=simulation.pnl_usd,
                    roi_pct=simulation.roi_pct,
                    signal_probability=signal.probability,
                    signal_reason=signal.reason,
                )
            )
    return trades, stats


def write_market_relevant_csv(rows: list[MarketRelevantTrade], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "news_id",
        "news_ts",
        "headline",
        "symbols",
        "market_id",
        "market_question",
        "side",
        "volume",
        "liquidity",
        "candidate_score",
        "entry_ts",
        "entry_price",
        "exit_ts",
        "exit_price",
        "exit_reason",
        "exit_delay_hours",
        "pnl_usd",
        "roi_pct",
        "signal_probability",
        "signal_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "news_id": row.news_id,
                    "news_ts": row.news_ts.isoformat(),
                    "headline": row.headline,
                    "symbols": ",".join(row.symbols),
                    "market_id": row.market_id,
                    "market_question": row.market_question,
                    "side": row.side,
                    "volume": row.volume,
                    "liquidity": row.liquidity,
                    "candidate_score": row.candidate_score,
                    "entry_ts": row.entry_ts.isoformat(),
                    "entry_price": row.entry_price,
                    "exit_ts": row.exit_ts.isoformat(),
                    "exit_price": row.exit_price,
                    "exit_reason": row.exit_reason,
                    "exit_delay_hours": row.exit_delay_hours,
                    "pnl_usd": row.pnl_usd,
                    "roi_pct": row.roi_pct,
                    "signal_probability": row.signal_probability,
                    "signal_reason": row.signal_reason,
                }
            )


def summarize_market_relevant(rows: list[MarketRelevantTrade], stats: dict[str, int]) -> dict:
    pnl = sum(row.pnl_usd for row in rows)
    wins = sum(1 for row in rows if row.pnl_usd > 0)
    take_profits = sum(1 for row in rows if row.exit_reason.startswith("take_profit"))
    return {
        **stats,
        "pnl_usd": round(pnl, 4),
        "avg_roi_pct": round(sum(row.roi_pct for row in rows) / len(rows), 4) if rows else 0.0,
        "win_rate": round(wins / len(rows), 4) if rows else 0.0,
        "take_profit_rate": round(take_profits / len(rows), 4) if rows else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest market-relevant news on low-liquidity Polymarket markets.")
    parser.add_argument("--db", default="data/trades.db")
    parser.add_argument("--output", default="data/market_relevant_low_liquidity_backtest.csv")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=0.60)
    parser.add_argument("--max-volume", type=float, default=50_000.0)
    parser.add_argument("--max-liquidity", type=float, default=20_000.0)
    parser.add_argument("--target-move", type=float, default=0.10)
    parser.add_argument("--stop-move", type=float, default=0.0)
    parser.add_argument("--horizon-hours", type=int, default=72)
    parser.add_argument("--min-hours-to-end", type=int, default=72)
    parser.add_argument("--min-entry-price", type=float, default=0.0)
    parser.add_argument("--max-entry-price", type=float, default=1.0)
    parser.add_argument("--entry-window-minutes", type=int, default=15)
    parser.add_argument("--search-limit", type=int, default=7)
    parser.add_argument("--max-markets-per-news", type=int, default=12)
    parser.add_argument("--dedupe-mode", choices=["none", "headline", "market-side"], default="headline")
    parser.add_argument("--include-symbol", action="append", default=[])
    parser.add_argument("--exclude-symbol", action="append", default=[])
    parser.add_argument("--single-name-threshold-only", action="store_true")
    parser.add_argument("--corporate-catalyst-only", action="store_true")
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--fee-bps", type=float, default=10.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = MarketRelevantConfig(
        min_candidate_score=args.min_score,
        max_volume=args.max_volume,
        max_liquidity=args.max_liquidity,
        target_move=args.target_move,
        stop_move=args.stop_move,
        horizon_hours=args.horizon_hours,
        min_hours_to_end=args.min_hours_to_end,
        min_entry_price=args.min_entry_price,
        max_entry_price=args.max_entry_price,
        entry_window_minutes=args.entry_window_minutes,
        search_limit=args.search_limit,
        max_markets_per_news=args.max_markets_per_news,
        dedupe_mode=args.dedupe_mode,
        include_symbols=_normalise_symbols(args.include_symbol),
        exclude_symbols=_normalise_symbols(args.exclude_symbol),
        single_name_threshold_only=args.single_name_threshold_only,
        corporate_catalyst_only=args.corporate_catalyst_only,
        notional=args.notional,
        fee_bps=args.fee_bps,
    )
    client = PolymarketClient()
    try:
        rows, stats = run_market_relevant_backtest(args.db, client, config, limit=args.limit)
    finally:
        client.close()
    write_market_relevant_csv(rows, args.output)
    summary = summarize_market_relevant(rows, stats)
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Wrote {len(rows)} rows to {args.output}")
    return 0


def _sentiment(text: str) -> Literal["positive", "negative", "mixed", "neutral"]:
    positive = any(term in text for term in _POSITIVE_TERMS)
    negative = any(term in text for term in _NEGATIVE_TERMS)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "neutral"


def _norm(text: str) -> str:
    return html.unescape(text or "").lower().replace("‑", "-").replace("—", "-").replace("&amp;", "&")


def _headline_key(headline: str) -> str:
    return re.sub(r"\s+", " ", _norm(headline)).strip()


def _normalise_symbols(symbols: list[str]) -> tuple[str, ...]:
    return tuple(sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()}))


def _symbol_filter_allows(
    news_symbols: list[str],
    include_symbols: tuple[str, ...],
    exclude_symbols: tuple[str, ...],
) -> bool:
    news_symbol_set = {symbol.upper() for symbol in news_symbols}
    if include_symbols and not news_symbol_set.intersection(include_symbols):
        return False
    return not news_symbol_set.intersection(exclude_symbols)


def _is_corporate_catalyst_news(news: NewsEvent) -> bool:
    text = _norm(" ".join([news.headline, news.summary]))
    headline = _norm(news.headline)
    if any(term in text for term in _MACRO_OR_BROAD_MARKET_TERMS):
        return False
    if any(term in headline for term in _PREVIEW_TERMS):
        return False
    if ("analyst" in text or "price target" in text) and not (
        "following" in text or "after" in text or "earnings" in text or "results" in text
    ):
        return False
    return any(term in text for term in _CORPORATE_CATALYST_TERMS)


def _is_same_ticker_single_name_threshold(news: NewsEvent, market: GammaMarket) -> bool:
    market_text = _norm(" ".join([market.event_title, market.question, market.description, market.slug]))
    if any(term in market_text for term in _INDEX_MARKET_TERMS):
        return False
    if "hit (high)" not in market_text and "hit (low)" not in market_text:
        return False
    threshold_tickers = _threshold_tickers(market)
    if not threshold_tickers:
        return False
    news_symbols = {symbol.upper() for symbol in news.symbols}
    tradable_threshold_tickers = threshold_tickers - _EXCLUDED_THRESHOLD_SYMBOLS
    return bool(tradable_threshold_tickers.intersection(news_symbols))


def _threshold_tickers(market: GammaMarket) -> set[str]:
    text = " ".join([market.event_title, market.question, market.slug])
    return {match.upper() for match in re.findall(r"\(([A-Z]{1,6})\)", text)}


def _trade_dedupe_key(
    market_id: str,
    side: Side,
    headline: str,
    dedupe_mode: DedupeMode,
) -> tuple[str, ...] | None:
    if dedupe_mode == "none":
        return None
    if dedupe_mode == "market-side":
        return (market_id, side)
    return (market_id, side, _headline_key(headline))


if __name__ == "__main__":
    raise SystemExit(main())
