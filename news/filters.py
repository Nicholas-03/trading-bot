import re
from datetime import datetime, timezone

_RETROSPECTIVE_PATTERNS = [
    re.compile(r"\bwhy\b.{0,30}\bstock\b.{0,20}\b(surging|skyrocketing|jumping|rising|gaining|soaring|rallying|climbing)\b", re.IGNORECASE),
    re.compile(r"\bshares?\b.{0,20}\b(are |is )?(trading|moving)\s+(higher|lower)\b", re.IGNORECASE),
    re.compile(r"\bshares?\b.{0,10}\b(surging|skyrocketing|jumping|rallying|soaring)\b", re.IGNORECASE),
    re.compile(r"\bstock\b.{0,10}\b(surging|skyrocketing|jumping|rallying|soaring)\b", re.IGNORECASE),
]

_ROUTINE_PATTERNS = [
    re.compile(r"\b(monthly|weekly|annual)\b.{0,30}\b(sales|revenue|shipments?)\b", re.IGNORECASE),
    re.compile(r"\breports?\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(sales|units|shipments?)\b", re.IGNORECASE),
    re.compile(r"\b(shareholder|shareholders|investor|annual)\s+letter\b", re.IGNORECASE),
    re.compile(r"\bceo\s+letter\b", re.IGNORECASE),
    re.compile(r"\bfiles?\s+(annual|quarterly)\s+report\b", re.IGNORECASE),
    re.compile(r"\b\d{1,3},\d{3}\s+units?\b", re.IGNORECASE),
]


def is_retrospective_headline(headline: str) -> bool:
    """Return True if the headline describes a price move that already happened."""
    return any(p.search(headline) for p in _RETROSPECTIVE_PATTERNS)


def is_routine_news(headline: str) -> bool:
    """Return True if the headline matches scheduled routine data with no surprise alpha."""
    return any(p.search(headline) for p in _ROUTINE_PATTERNS)


def compute_news_age_hours(article_ts: datetime) -> float:
    """Return hours elapsed since article_ts. Raises ValueError for naive datetimes."""
    if article_ts.tzinfo is None:
        raise ValueError("article_ts must be timezone-aware")
    delta = datetime.now(timezone.utc) - article_ts
    return delta.total_seconds() / 3600
