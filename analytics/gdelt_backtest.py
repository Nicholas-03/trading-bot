from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from analytics.embedding_gate import DEFAULT_EMBEDDING_MODEL, EmbeddingGate
from analytics.llm_gate import LLMGate
from analytics.polymarket_backtest import (
    BacktestConfig,
    NewsEvent,
    PolymarketClient,
    run_backtest,
    summarize_results,
    write_csv,
)

logger = logging.getLogger(__name__)

GDELT_V2_BASE_URL = "http://data.gdeltproject.org/gdeltv2"
GDELT_CLOUD_BASE_URL = "https://gdeltcloud.com"
DEFAULT_GDELT_CLOUD_QUERIES = (
    "market moving geopolitics tariffs sanctions conflict inflation rates",
)

DEFAULT_FOCUS_TERMS = (
    "iran",
    "israel",
    "hamas",
    "gaza",
    "hormuz",
    "ukraine",
    "russia",
    "china",
    "taiwan",
    "tariff",
    "tariffs",
    "sanctions",
    "ceasefire",
    "war",
    "conflict",
    "fed",
    "rate",
    "rates",
    "cpi",
    "jobs",
    "inflation",
    "election",
    "trump",
)

EVENT_ROOT_PHRASES = {
    "01": "make public statement",
    "02": "appeal",
    "03": "express intent",
    "04": "consult",
    "05": "engage diplomatic cooperation",
    "06": "engage material cooperation",
    "07": "provide aid",
    "08": "yield",
    "09": "investigate",
    "10": "demand",
    "11": "disapprove",
    "12": "reject",
    "13": "threaten",
    "14": "protest",
    "15": "exhibit force posture",
    "16": "reduce relations",
    "17": "coerce",
    "18": "assault",
    "19": "fight",
    "20": "use unconventional mass violence",
}


@dataclass(frozen=True)
class GdeltEvent:
    event_id: str
    date_added: datetime
    actor1_name: str
    actor2_name: str
    event_root_code: str
    event_phrase: str
    action_geo: str
    source_url: str
    num_mentions: int
    avg_tone: float


def parse_export_row(row: list[str]) -> GdeltEvent | None:
    if len(row) < 61:
        return None
    date_added = _parse_gdelt_datetime(row[59])
    if date_added is None:
        return None
    event_root_code = row[28].strip()
    return GdeltEvent(
        event_id=row[0].strip(),
        date_added=date_added,
        actor1_name=row[6].strip(),
        actor2_name=row[16].strip(),
        event_root_code=event_root_code,
        event_phrase=EVENT_ROOT_PHRASES.get(event_root_code, f"event {event_root_code}"),
        action_geo=row[52].strip(),
        source_url=row[60].strip(),
        num_mentions=_safe_int(row[31]),
        avg_tone=_safe_float(row[34]),
    )


def event_to_news(event: GdeltEvent) -> NewsEvent:
    parts = [part for part in [event.actor1_name, event.event_phrase, event.actor2_name] if part]
    headline = " ".join(parts) if parts else event.event_phrase
    if event.action_geo:
        headline = f"{headline} in {event.action_geo}"
    domain = urlparse(event.source_url).netloc
    summary = (
        f"GDELT event {event.event_id}; root={event.event_root_code}; "
        f"mentions={event.num_mentions}; tone={event.avg_tone:.2f}; "
        f"source={domain}; url={event.source_url}"
    )
    return NewsEvent(
        id=int(event.event_id),
        ts=event.date_added,
        headline=headline,
        summary=summary,
        symbols=[],
    )


def cloud_event_to_news(raw: dict) -> NewsEvent | None:
    event_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not event_id or not title:
        return None
    timestamp = (
        raw.get("coded_at")
        or raw.get("processed_at")
        or raw.get("updated_at")
        or raw.get("event_date")
    )
    if not timestamp:
        return None
    try:
        ts = _parse_iso_utc(str(timestamp))
    except ValueError:
        return None

    geo = raw.get("geo") if isinstance(raw.get("geo"), dict) else {}
    article = (raw.get("top_articles") or [{}])[0]
    if not isinstance(article, dict):
        article = {}
    evidence = [
        f"GDELT Cloud event {event_id}",
        f"category={raw.get('category') or ''}",
        f"subcategory={raw.get('subcategory') or ''}",
        f"country={geo.get('country') or ''}",
        f"source={article.get('domain') or ''}",
        f"url={article.get('url') or ''}",
    ]
    summary = " ".join(
        part
        for part in [
            str(raw.get("summary") or "").strip(),
            "; ".join(evidence),
        ]
        if part
    )
    return NewsEvent(
        id=stable_news_id_from_string(event_id),
        ts=ts,
        headline=title,
        summary=summary,
        symbols=[],
    )


def stable_news_id_from_string(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:15], 16)


def news_within_range(news: NewsEvent, start: datetime, end: datetime) -> bool:
    ts = news.ts.astimezone(timezone.utc)
    return start.astimezone(timezone.utc) <= ts <= end.astimezone(timezone.utc)


def should_keep_event(event: GdeltEvent, focus_terms: tuple[str, ...]) -> bool:
    text = " ".join(
        [
            event.actor1_name,
            event.actor2_name,
            event.event_phrase,
            event.action_geo,
            event.source_url,
        ]
    ).lower()
    return any(term.lower() in text for term in focus_terms)


def iter_gdelt_datetimes(
    start: datetime,
    end: datetime,
    interval_minutes: int = 15,
):
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    current = _floor_to_interval(start.astimezone(timezone.utc), interval_minutes)
    end_utc = end.astimezone(timezone.utc)
    while current <= end_utc:
        yield current
        current += timedelta(minutes=interval_minutes)


def fetch_export_events(client: httpx.Client, dt: datetime) -> list[GdeltEvent]:
    url = f"{GDELT_V2_BASE_URL}/{dt.strftime('%Y%m%d%H%M%S')}.export.CSV.zip"
    response = client.get(url, timeout=30, follow_redirects=True)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        name = archive.namelist()[0]
        with archive.open(name) as fp:
            reader = csv.reader(io.TextIOWrapper(fp, encoding="utf-8", errors="replace"), delimiter="\t")
            events = []
            for row in reader:
                event = parse_export_row(row)
                if event is not None:
                    events.append(event)
            return events


def collect_gdelt_news(
    start: datetime,
    end: datetime,
    focus_terms: tuple[str, ...] = DEFAULT_FOCUS_TERMS,
    interval_minutes: int = 60,
    max_events: int = 200,
    max_files: int | None = None,
) -> list[NewsEvent]:
    selected: dict[str, GdeltEvent] = {}
    with httpx.Client(headers={"User-Agent": "trading-bot-gdelt-backtest/1.0"}) as client:
        for file_count, dt in enumerate(iter_gdelt_datetimes(start, end, interval_minutes), start=1):
            if max_files is not None and file_count > max_files:
                break
            try:
                events = fetch_export_events(client, dt)
            except httpx.HTTPError as exc:
                logger.warning("GDELT fetch failed for %s: %s", dt.isoformat(), exc)
                continue
            for event in events:
                if not event.source_url or not should_keep_event(event, focus_terms):
                    continue
                existing = selected.get(event.source_url)
                if existing is None or event.num_mentions > existing.num_mentions:
                    selected[event.source_url] = event

    ranked = sorted(
        selected.values(),
        key=lambda event: (event.num_mentions, abs(event.avg_tone)),
        reverse=True,
    )[:max_events]
    return [event_to_news(event) for event in ranked]


def fetch_gdelt_cloud_events(
    client: httpx.Client,
    api_key: str,
    start: datetime,
    end: datetime,
    query: str,
    limit: int = 25,
) -> list[dict]:
    response = client.get(
        f"{GDELT_CLOUD_BASE_URL}/api/v2/events",
        params={
            "date_start": start.astimezone(timezone.utc).date().isoformat(),
            "date_end": end.astimezone(timezone.utc).date().isoformat(),
            "search": query,
            "limit": min(max(int(limit), 1), 100),
            "sort": "significance",
        },
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("data") or []
    return [row for row in rows if isinstance(row, dict)]


def collect_gdelt_cloud_news(
    api_key: str,
    start: datetime,
    end: datetime,
    queries: tuple[str, ...] = DEFAULT_GDELT_CLOUD_QUERIES,
    limit_per_query: int = 25,
    max_events: int = 200,
) -> list[NewsEvent]:
    if not api_key:
        raise ValueError("GDELT_CLOUD_API_KEY is required for --source cloud")
    selected: dict[int, NewsEvent] = {}
    with httpx.Client(headers={"User-Agent": "trading-bot-gdeltcloud-backtest/1.0"}) as client:
        for query in queries:
            try:
                events = fetch_gdelt_cloud_events(
                    client=client,
                    api_key=api_key,
                    start=start,
                    end=end,
                    query=query,
                    limit=limit_per_query,
                )
            except httpx.HTTPError as exc:
                logger.warning("GDELT Cloud fetch failed for %r: %s", query, exc)
                continue
            for raw in events:
                news = cloud_event_to_news(raw)
                if news is not None and news_within_range(news, start, end):
                    selected[news.id] = news
    return sorted(selected.values(), key=lambda news: news.ts, reverse=True)[:max_events]


def write_news_temp_db(news_events: list[NewsEvent]) -> str:
    fd, path = tempfile.mkstemp(prefix="gdelt_news_", suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE news_events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, headline TEXT NOT NULL, summary TEXT, symbols TEXT)"
    )
    for news in news_events:
        con.execute(
            "INSERT INTO news_events (id, ts, headline, summary, symbols) VALUES (?, ?, ?, ?, ?)",
            (news.id, news.ts.isoformat(), news.headline, news.summary, ",".join(news.symbols)),
        )
    con.commit()
    con.close()
    return path


def date_range_from_analytics_db(db_path: str) -> tuple[datetime, datetime]:
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT MIN(ts), MAX(ts) FROM news_events").fetchone()
    con.close()
    if row is None or row[0] is None or row[1] is None:
        raise ValueError(f"No news_events found in {db_path}")
    return _parse_iso_utc(row[0]), _parse_iso_utc(row[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest GDELT raw events against public Polymarket markets.")
    parser.add_argument("--db", default="data/trades.db", help="Analytics DB used only for default date range")
    parser.add_argument("--start", help="UTC start, e.g. 2026-05-04T13:00:00Z")
    parser.add_argument("--end", help="UTC end, e.g. 2026-05-07T15:00:00Z")
    parser.add_argument("--output", default="data/gdelt_polymarket_backtest.csv")
    parser.add_argument("--source", choices=("raw", "cloud"), default="raw", help="GDELT source: public raw exports or GDELT Cloud API")
    parser.add_argument("--interval-minutes", type=int, default=60)
    parser.add_argument("--max-events", type=int, default=200)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--focus-term", action="append", dest="focus_terms")
    parser.add_argument("--gdelt-cloud-query", action="append", dest="gdelt_cloud_queries")
    parser.add_argument("--gdelt-cloud-api-key", default=None, help="Prefer GDELT_CLOUD_API_KEY env var to avoid shell history")
    parser.add_argument("--cloud-limit-per-query", type=int, default=25)
    parser.add_argument("--llm-gate", action="store_true", help="Use OpenAI LLM to validate candidate market matches")
    parser.add_argument("--llm-model", default=None, help="OpenAI model for LLM gate; defaults to OPENAI_MODEL")
    parser.add_argument("--llm-cache", default="data/llm_gate_cache.json")
    parser.add_argument("--llm-max-calls", type=int, default=20)
    parser.add_argument("--llm-min-confidence", type=float, default=0.60)
    parser.add_argument("--embedding-gate", action="store_true", help="Use sentence-transformer embeddings to validate candidate market matches")
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--embedding-threshold", type=float, default=0.50)
    parser.add_argument("--embedding-cache", default="data/embedding_gate_cache.json")
    parser.add_argument(
        "--embedding-allow-ambiguous-direction",
        action="store_true",
        help="Allow embedding matches even when deterministic probability remains neutral",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    if args.start and args.end:
        start = _parse_iso_utc(args.start)
        end = _parse_iso_utc(args.end)
    else:
        start, end = date_range_from_analytics_db(args.db)

    if args.source == "cloud":
        load_dotenv()
        api_key = args.gdelt_cloud_api_key or os.getenv("GDELT_CLOUD_API_KEY", "")
        queries = tuple(args.gdelt_cloud_queries) if args.gdelt_cloud_queries else DEFAULT_GDELT_CLOUD_QUERIES
        news_events = collect_gdelt_cloud_news(
            api_key=api_key,
            start=start,
            end=end,
            queries=queries,
            limit_per_query=args.cloud_limit_per_query,
            max_events=args.max_events,
        )
    else:
        focus_terms = tuple(args.focus_terms) if args.focus_terms else DEFAULT_FOCUS_TERMS
        news_events = collect_gdelt_news(
            start=start,
            end=end,
            focus_terms=focus_terms,
            interval_minutes=args.interval_minutes,
            max_events=args.max_events,
            max_files=args.max_files,
        )
    temp_db = write_news_temp_db(news_events)
    client = PolymarketClient()
    gate = build_match_gate_from_args(args)
    try:
        rows, stats = run_backtest(temp_db, client, BacktestConfig(request_sleep_seconds=0), gate=gate)
    finally:
        client.close()
        Path(temp_db).unlink(missing_ok=True)
    write_csv(rows, args.output)
    summary = summarize_results(rows, stats)
    summary["gdelt_news_collected"] = len(news_events)
    summary["gdelt_source"] = args.source
    print(summary)
    print(f"Wrote {len(rows)} rows to {args.output}")
    return 0


def build_match_gate_from_args(args):
    if getattr(args, "llm_gate", False) and getattr(args, "embedding_gate", False):
        raise ValueError("Choose only one gate: --llm-gate or --embedding-gate")
    if getattr(args, "embedding_gate", False):
        return EmbeddingGate(
            model_name=args.embedding_model,
            min_similarity=args.embedding_threshold,
            cache_path=args.embedding_cache,
            require_directional_signal=not getattr(args, "embedding_allow_ambiguous_direction", False),
        )
    if getattr(args, "llm_gate", False):
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY", "")
        model = args.llm_model or os.getenv("OPENAI_MODEL", "")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for --llm-gate")
        if not model:
            raise ValueError("OPENAI_MODEL or --llm-model is required for --llm-gate")
        return LLMGate(
            api_key=api_key,
            model=model,
            cache_path=args.llm_cache,
            max_calls=args.llm_max_calls,
            min_confidence=args.llm_min_confidence,
        )
    return None


def _parse_gdelt_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_iso_utc(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _floor_to_interval(dt: datetime, interval_minutes: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    minute = (dt.minute // interval_minutes) * interval_minutes
    return dt.replace(minute=minute)


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _safe_float(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
