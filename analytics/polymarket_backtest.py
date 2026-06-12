from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol

import httpx

logger = logging.getLogger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9&.'-]*", re.IGNORECASE)
_TICKER_RE_TEMPLATE = r"(?<![A-Z0-9]){}(?![A-Z0-9])"

_KEYWORD_GROUPS: dict[str, tuple[str, ...]] = {
    "earnings": ("earnings", "eps", "quarterly", "q1", "q2", "q3", "q4", "results", "beat", "miss"),
    "guidance": ("guidance", "outlook", "forecast", "sees", "sales", "revenue"),
    "fda": ("fda", "approval", "approved", "approve", "drug", "trial", "phase"),
    "mna": ("merger", "acquisition", "acquire", "buyout", "takeover"),
    "macro": ("tariff", "tariffs", "cpi", "jobs", "rates", "fed", "inflation", "gdp"),
    "geopolitics": ("iran", "israel", "middle east", "ceasefire", "war", "conflict", "sanctions", "peace"),
    "legal": ("lawsuit", "court", "ruling", "settlement", "probe", "investigation"),
    "contract": ("contract", "agreement", "partnership", "order", "deal"),
}

_POSITIVE_PATTERNS = (
    "beat",
    "beats",
    "double beat",
    "exceeded expectations",
    "above consensus",
    "raises guidance",
    "raises fy",
    "strong earnings",
    "strong q",
    "approved",
    "approval",
    "wins contract",
    "strikes",
    "agreement",
)
_NEGATIVE_PATTERNS = (
    "miss",
    "misses",
    "below consensus",
    "cuts guidance",
    "lowers guidance",
    "weak guidance",
    "lawsuit",
    "probe",
    "investigation",
    "recall",
)


@dataclass(frozen=True)
class NewsEvent:
    id: int
    ts: datetime
    headline: str
    summary: str
    symbols: list[str]


@dataclass(frozen=True)
class GammaMarket:
    id: str
    question: str
    description: str
    outcomes: list[str]
    outcome_prices: list[float]
    clob_token_ids: list[str]
    end_date: datetime | None
    closed_time: datetime | None
    enable_order_book: bool
    volume: float
    liquidity: float
    closed: bool
    active: bool
    event_title: str
    slug: str = ""

    @property
    def yes_token_id(self) -> str | None:
        return _token_for_outcome(self.outcomes, self.clob_token_ids, "yes")

    @property
    def no_token_id(self) -> str | None:
        return _token_for_outcome(self.outcomes, self.clob_token_ids, "no")

    @property
    def final_yes_price(self) -> float | None:
        return _price_for_outcome(self.outcomes, self.outcome_prices, "yes")

    @property
    def final_no_price(self) -> float | None:
        return _price_for_outcome(self.outcomes, self.outcome_prices, "no")


@dataclass(frozen=True)
class BacktestConfig:
    min_edge: float = 0.08
    fee_bps: float = 10.0
    notional: float = 100.0
    max_price_delay_seconds: int = 15 * 60
    history_lookback_minutes: int = 5
    history_lookahead_hours: int = 24
    min_candidate_score: float = 0.60
    min_volume: float = 100.0
    request_sleep_seconds: float = 0.05

    @property
    def required_edge(self) -> float:
        return self.min_edge + self.fee_bps / 10_000


@dataclass(frozen=True)
class SimulationResult:
    shares: float
    pnl_usd: float
    roi_pct: float
    fee_usd: float


@dataclass(frozen=True)
class BacktestRow:
    news_id: int
    news_ts: datetime
    headline: str
    symbols: list[str]
    market_id: str
    market_question: str
    side: Literal["yes", "no"]
    entry_price: float
    exit_price: float
    model_probability: float
    edge: float
    candidate_score: float
    pnl_usd: float
    roi_pct: float
    reason: str


class MatchGate(Protocol):
    calls: int
    cache_hits: int
    limit_skips: int
    stats_prefix: str

    def evaluate(self, news: NewsEvent, market: GammaMarket): ...


def parse_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def candidate_score(news: NewsEvent, market: GammaMarket) -> float:
    news_text = _norm(" ".join([news.headline, news.summary]))
    market_text = _norm(" ".join([market.event_title, market.question, market.description, market.slug]))
    ticker_market_text = _norm(" ".join([market.event_title, market.question, market.slug]))
    score = 0.0

    token_overlap = _important_token_overlap(news_text, market_text)
    ticker_match = any(_has_market_ticker_match(ticker_market_text, symbol) for symbol in news.symbols)

    if ticker_match:
        score += 0.45
    elif not news.symbols:
        score += min(0.45, token_overlap * 0.10)

    news_groups = _feature_groups(news_text)
    market_groups = _feature_groups(market_text)
    overlap_groups = news_groups & market_groups
    group_weight = 0.20 if not news.symbols else 0.14
    group_cap = 0.40 if not news.symbols else 0.35
    score += min(group_cap, group_weight * len(overlap_groups))

    score += min(0.20, token_overlap * 0.04)

    if _looks_like_sports_market(market_text) and not any(_has_market_ticker_match(ticker_market_text, symbol) for symbol in news.symbols):
        score -= 0.30

    if _looks_like_utterance_market(market_text) and not _looks_like_speech_news(news_text):
        score -= 0.45

    if not market.enable_order_book or not market.yes_token_id or not market.no_token_id:
        score -= 0.25

    return max(0.0, min(1.0, score))


def is_market_open_at(market: GammaMarket, news_ts: datetime) -> bool:
    ts = news_ts.astimezone(timezone.utc)
    if not market.enable_order_book or not market.yes_token_id or not market.no_token_id:
        return False
    if market.closed_time is not None and market.closed_time <= ts:
        return False
    if market.end_date is not None and market.end_date <= ts:
        return False
    if market.closed and market.closed_time is None and market.end_date is None:
        return False
    return True


def price_at_or_after(
    history: Iterable[dict[str, Any]],
    signal_ts: datetime,
    max_delay_seconds: int,
) -> float | None:
    target = int(signal_ts.astimezone(timezone.utc).timestamp())
    best: tuple[int, float] | None = None
    for point in history:
        try:
            point_ts = int(point["t"])
            price = float(point["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if point_ts < target:
            continue
        delay = point_ts - target
        if delay > max_delay_seconds:
            continue
        if best is None or point_ts < best[0]:
            best = (point_ts, price)
    return None if best is None else best[1]


def infer_news_probability(news: NewsEvent, market: GammaMarket) -> tuple[float, str]:
    news_text = _norm(" ".join([news.headline, news.summary]))
    market_text = _norm(" ".join([market.question, market.description]))
    positive = any(pattern in news_text for pattern in _POSITIVE_PATTERNS)
    negative = any(pattern in news_text for pattern in _NEGATIVE_PATTERNS)
    acquisition_market = any(term in market_text for term in ("acquire", "acquired", "acquisition", "merger"))
    acquisition_news = any(
        term in news_text
        for term in ("acquire", "acquired", "acquisition", "merger", "bid", "buyout", "takeover")
    )

    if "beat" in market_text and "earnings" in market_text:
        if positive and not negative:
            return 0.95, "positive earnings/news signal for YES"
        if negative and not positive:
            return 0.05, "negative earnings/news signal for NO"
    if "approve" in market_text or "approval" in market_text or "fda" in market_text:
        if positive and not negative:
            return 0.90, "positive approval signal for YES"
        if negative and not positive:
            return 0.10, "negative approval signal for NO"
    if acquisition_market and acquisition_news:
        return 0.85, "acquisition/merger signal for YES"
    if positive and not negative:
        return 0.75, "positive event signal for YES"
    if negative and not positive:
        return 0.25, "negative event signal for NO"
    return 0.50, "neutral or ambiguous news"


def simulate_long_binary(entry_price: float, exit_price: float, notional: float, fee_bps: float = 0.0) -> SimulationResult:
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    shares = notional / entry_price
    gross_value = shares * exit_price
    fee_usd = notional * fee_bps / 10_000
    pnl_usd = round(gross_value - notional - fee_usd, 10)
    roi_pct = round((pnl_usd / notional) * 100, 10)
    return SimulationResult(shares=shares, pnl_usd=pnl_usd, roi_pct=roi_pct, fee_usd=fee_usd)


def should_consider_news(news: NewsEvent) -> bool:
    text = _norm(" ".join([news.headline, news.summary]))
    headline = _norm(news.headline)
    if "prediction market" in text or "polymarket" in text:
        return False
    if (
        headline.startswith("transcript:")
        or "earnings call transcript" in headline
        or ("earnings call" in headline and "transcript" in headline)
    ):
        return False
    return bool(_feature_groups(text))


def load_news_events(db_path: str, limit: int | None = None) -> list[NewsEvent]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    sql = "SELECT id, ts, headline, summary, symbols FROM news_events ORDER BY ts ASC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = []
    for row in con.execute(sql):
        ts = parse_utc_datetime(row["ts"])
        if ts is None:
            continue
        rows.append(
            NewsEvent(
                id=int(row["id"]),
                ts=ts,
                headline=html.unescape(row["headline"] or ""),
                summary=html.unescape(row["summary"] or ""),
                symbols=parse_string_list(row["symbols"]),
            )
        )
    con.close()
    return rows


class PolymarketClient:
    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": "trading-bot-polymarket-backtest/1.0"})
        self._search_cache: dict[str, list[GammaMarket]] = {}
        self._history_cache: dict[tuple[str, int, int], list[dict[str, Any]]] = {}

    def close(self) -> None:
        self._client.close()

    def search_markets(self, query: str, limit: int = 5) -> list[GammaMarket]:
        cache_key = f"{query}|{limit}"
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]
        response = self._client.get(f"{GAMMA_BASE_URL}/public-search", params={"q": query, "limit": limit})
        response.raise_for_status()
        data = response.json()
        markets: list[GammaMarket] = []
        for event in data.get("events", []) or []:
            event_title = str(event.get("title") or "")
            for raw_market in event.get("markets", []) or []:
                market = market_from_gamma(raw_market, event_title=event_title)
                if market is not None:
                    markets.append(market)
        self._search_cache[cache_key] = markets
        return markets

    def price_history(self, token_id: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        cache_key = (token_id, start_ts, end_ts)
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]
        response = self._client.get(
            f"{CLOB_BASE_URL}/prices-history",
            params={"market": token_id, "startTs": start_ts, "endTs": end_ts},
        )
        response.raise_for_status()
        history = response.json().get("history", []) or []
        self._history_cache[cache_key] = history
        return history

    def order_book(self, token_id: str) -> dict[str, Any]:
        response = self._client.get(f"{CLOB_BASE_URL}/book", params={"token_id": token_id})
        response.raise_for_status()
        return response.json()


def market_from_gamma(raw: dict[str, Any], event_title: str = "") -> GammaMarket | None:
    outcomes = parse_string_list(raw.get("outcomes"))
    outcome_prices = [_safe_float(value) for value in parse_string_list(raw.get("outcomePrices"))]
    token_ids = parse_string_list(raw.get("clobTokenIds"))
    if not raw.get("id") or not raw.get("question"):
        return None
    return GammaMarket(
        id=str(raw.get("id")),
        question=html.unescape(str(raw.get("question") or "")),
        description=html.unescape(str(raw.get("description") or "")),
        outcomes=outcomes,
        outcome_prices=outcome_prices,
        clob_token_ids=token_ids,
        end_date=parse_utc_datetime(raw.get("endDate")),
        closed_time=parse_utc_datetime(raw.get("closedTime")),
        enable_order_book=bool(raw.get("enableOrderBook")),
        volume=_safe_float(raw.get("volumeNum", raw.get("volume"))),
        liquidity=_safe_float(raw.get("liquidityNum", raw.get("liquidity"))),
        closed=bool(raw.get("closed")),
        active=bool(raw.get("active", True)),
        event_title=html.unescape(event_title),
        slug=str(raw.get("slug") or ""),
    )


def search_queries_for_news(news: NewsEvent, max_queries: int = 3) -> list[str]:
    text = _norm(" ".join([news.headline, news.summary]))
    queries: list[str] = []
    if "earnings" in text or "eps" in text or "beat" in text or "miss" in text:
        for symbol in news.symbols[:2]:
            queries.append(f"{symbol} earnings")
    if "guidance" in text or "revenue" in text or "sales" in text:
        for symbol in news.symbols[:2]:
            queries.append(f"{symbol} guidance")
    if "fda" in text or "approval" in text or "approved" in text:
        queries.append(_short_query(news.headline, suffix="FDA approval"))
    if not queries:
        queries.append(_short_query(news.headline))

    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        query = query.strip()
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped[:max_queries]


def run_backtest(
    db_path: str,
    client: PolymarketClient,
    config: BacktestConfig,
    limit: int | None = None,
    gate: MatchGate | None = None,
) -> tuple[list[BacktestRow], dict[str, int]]:
    news_events = load_news_events(db_path, limit=limit)
    stats = {
        "news_total": len(news_events),
        "news_considered": 0,
        "candidate_markets": 0,
        "matched_markets": 0,
        "open_at_news": 0,
        "closed_before_news": 0,
        "missing_final_price": 0,
        "missing_entry_price": 0,
        "with_entry_price": 0,
        "trades": 0,
        "gate_checked": 0,
        "gate_rejected": 0,
    }
    results: list[BacktestRow] = []

    for news in news_events:
        if not should_consider_news(news):
            continue
        stats["news_considered"] += 1
        seen_market_ids: set[str] = set()
        candidates: list[tuple[float, GammaMarket]] = []
        for query in search_queries_for_news(news):
            try:
                markets = client.search_markets(query)
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
                if score >= config.min_candidate_score and market.volume >= config.min_volume:
                    candidates.append((score, market))

        if not candidates:
            continue
        stats["matched_markets"] += len(candidates)
        candidates.sort(key=lambda item: item[0], reverse=True)

        for score, market in candidates[:2]:
            if not is_market_open_at(market, news.ts):
                stats["closed_before_news"] += 1
                continue
            stats["open_at_news"] += 1
            if gate is not None:
                from analytics.llm_gate import apply_llm_gate_decision

                stats["gate_checked"] += 1
                gate_decision = gate.evaluate(news, market)
                gate_result = apply_llm_gate_decision(gate_decision)
                if gate_result is None:
                    stats["gate_rejected"] += 1
                    continue
                gate_side, side_probability, reason = gate_result
                model_yes_probability = gate_decision.p_yes
            else:
                model_yes_probability, reason = infer_news_probability(news, market)
                gate_side = None
            side: Literal["yes", "no"]
            token_id: str | None
            final_price: float | None
            if gate_side == "yes" or (gate_side is None and model_yes_probability >= 0.5):
                side = "yes"
                token_id = market.yes_token_id
                final_price = market.final_yes_price
                if gate_side is None:
                    side_probability = model_yes_probability
            else:
                side = "no"
                token_id = market.no_token_id
                final_price = market.final_no_price
                if gate_side is None:
                    side_probability = 1 - model_yes_probability
            if token_id is None or final_price is None or not (0 <= final_price <= 1):
                stats["missing_final_price"] += 1
                continue

            start_ts = int((news.ts - timedelta(minutes=config.history_lookback_minutes)).timestamp())
            end_ts = int((news.ts + timedelta(hours=config.history_lookahead_hours)).timestamp())
            try:
                history = client.price_history(token_id, start_ts, end_ts)
            except httpx.HTTPError as exc:
                logger.warning("Polymarket history failed for market %s: %s", market.id, exc)
                continue
            entry_price = price_at_or_after(history, news.ts, config.max_price_delay_seconds)
            if entry_price is None or entry_price <= 0 or entry_price >= 1:
                stats["missing_entry_price"] += 1
                continue
            stats["with_entry_price"] += 1

            edge = side_probability - entry_price
            simulation = simulate_long_binary(
                entry_price=entry_price,
                exit_price=final_price,
                notional=config.notional,
                fee_bps=config.fee_bps,
            )
            stats["trades"] += 1
            results.append(
                BacktestRow(
                    news_id=news.id,
                    news_ts=news.ts,
                    headline=news.headline,
                    symbols=news.symbols,
                    market_id=market.id,
                    market_question=market.question,
                    side=side,
                    entry_price=entry_price,
                    exit_price=final_price,
                    model_probability=side_probability,
                    edge=edge,
                    candidate_score=score,
                    pnl_usd=simulation.pnl_usd,
                    roi_pct=simulation.roi_pct,
                    reason=reason,
                )
            )
    if gate is not None:
        stats["gate_calls"] = gate.calls
        stats["gate_cache_hits"] = gate.cache_hits
        stats["gate_limit_skips"] = gate.limit_skips
        prefix = getattr(gate, "stats_prefix", "gate")
        if prefix != "gate":
            stats[f"{prefix}_calls"] = gate.calls
            stats[f"{prefix}_cache_hits"] = gate.cache_hits
            stats[f"{prefix}_limit_skips"] = gate.limit_skips
    return results, stats


def write_csv(rows: list[BacktestRow], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "news_id",
                "news_ts",
                "headline",
                "symbols",
                "market_id",
                "market_question",
                "side",
                "entry_price",
                "exit_price",
                "model_probability",
                "edge",
                "candidate_score",
                "pnl_usd",
                "roi_pct",
                "reason",
            ],
        )
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
                    "entry_price": row.entry_price,
                    "exit_price": row.exit_price,
                    "model_probability": row.model_probability,
                    "edge": row.edge,
                    "candidate_score": row.candidate_score,
                    "pnl_usd": row.pnl_usd,
                    "roi_pct": row.roi_pct,
                    "reason": row.reason,
                }
            )


def summarize_results(rows: list[BacktestRow], stats: dict[str, int]) -> dict[str, Any]:
    pnl = sum(row.pnl_usd for row in rows)
    wins = sum(1 for row in rows if row.pnl_usd > 0)
    return {
        **stats,
        "pnl_usd": round(pnl, 4),
        "avg_roi_pct": round(sum(row.roi_pct for row in rows) / len(rows), 4) if rows else 0.0,
        "win_rate": round(wins / len(rows), 4) if rows else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest DB news against public Polymarket markets.")
    parser.add_argument("--db", default="data/trades.db", help="SQLite analytics DB path")
    parser.add_argument("--output", default="data/polymarket_news_backtest.csv", help="CSV output path")
    parser.add_argument("--limit", type=int, default=None, help="Optional max news rows")
    parser.add_argument("--min-edge", type=float, default=0.08)
    parser.add_argument("--min-score", type=float, default=0.60)
    parser.add_argument("--min-volume", type=float, default=100.0)
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--fee-bps", type=float, default=10.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = BacktestConfig(
        min_edge=args.min_edge,
        min_candidate_score=args.min_score,
        min_volume=args.min_volume,
        notional=args.notional,
        fee_bps=args.fee_bps,
    )
    client = PolymarketClient()
    try:
        rows, stats = run_backtest(args.db, client, config, limit=args.limit)
    finally:
        client.close()
    write_csv(rows, args.output)
    summary = summarize_results(rows, stats)
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Wrote {len(rows)} rows to {args.output}")
    return 0


def _norm(text: str) -> str:
    return html.unescape(text or "").lower().replace("’", "'").replace("&amp;", "&")


def _feature_groups(text: str) -> set[str]:
    return {
        group
        for group, keywords in _KEYWORD_GROUPS.items()
        if any(_has_keyword(text, keyword) for keyword in keywords)
    }


def _has_keyword(text: str, keyword: str) -> bool:
    if " " in keyword:
        return keyword in text
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def _contains_ticker(text: str, ticker: str) -> bool:
    ticker = str(ticker).strip().upper()
    if not ticker or len(ticker) > 6:
        return False
    return re.search(_TICKER_RE_TEMPLATE.format(re.escape(ticker)), text.upper()) is not None


def _has_market_ticker_match(text: str, ticker: str) -> bool:
    ticker = str(ticker).strip().upper()
    if not ticker or len(ticker) > 6:
        return False
    upper_text = text.upper()
    if re.search(rf"\({re.escape(ticker)}\)", upper_text):
        return True
    if len(ticker) <= 2:
        return False
    return re.search(_TICKER_RE_TEMPLATE.format(re.escape(ticker)), upper_text) is not None


def _important_token_overlap(left: str, right: str) -> int:
    stop = {
        "the", "and", "with", "from", "will", "this", "that", "stock", "stocks",
        "inc", "corp", "company", "market", "markets", "raises", "lowers",
        "q1", "q2", "q3", "q4", "fy2026", "fy2025", "year", "years",
    }
    left_tokens = {token for token in _WORD_RE.findall(left) if len(token) >= 4 and token not in stop}
    right_tokens = {token for token in _WORD_RE.findall(right) if len(token) >= 4 and token not in stop}
    return len(left_tokens & right_tokens)


def _looks_like_sports_market(text: str) -> bool:
    sports_terms = ("mlb", "nba", "nfl", "nhl", "tennis", "soccer", "ufc", "game", "match", "player")
    return any(term in text for term in sports_terms)


def _looks_like_utterance_market(text: str) -> bool:
    return (
        " say " in text
        or " says " in text
        or " mention " in text
        or " mentioned " in text
        or "during speech" in text
    )


def _looks_like_speech_news(text: str) -> bool:
    speech_terms = ("speech", "remarks", "said", "says", "quote", "interview", "transcript", "debate", "address")
    return any(term in text for term in speech_terms)


def _safe_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def _price_for_outcome(outcomes: list[str], prices: list[float], outcome_name: str) -> float | None:
    outcome_name = outcome_name.lower()
    for idx, outcome in enumerate(outcomes):
        if outcome.lower() == outcome_name and idx < len(prices):
            return prices[idx]
    return None


def _token_for_outcome(outcomes: list[str], token_ids: list[str], outcome_name: str) -> str | None:
    outcome_name = outcome_name.lower()
    for idx, outcome in enumerate(outcomes):
        if outcome.lower() == outcome_name and idx < len(token_ids):
            return token_ids[idx]
    return None


def _short_query(headline: str, suffix: str = "") -> str:
    tokens = [
        token
        for token in _WORD_RE.findall(_norm(headline))
        if len(token) >= 4 and token not in {"stock", "shares", "today", "what", "with", "from"}
    ]
    query = " ".join(tokens[:5])
    return f"{query} {suffix}".strip()


if __name__ == "__main__":
    raise SystemExit(main())
