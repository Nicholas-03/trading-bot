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

_ANALYST_PATTERNS = [
    re.compile(r"\banalyst\b", re.IGNORECASE),
    re.compile(r"\b(price\s+target|pt)\b", re.IGNORECASE),
    re.compile(r"\b(upgrades?|downgrades?|initiates?|reiterates?|maintains?)\b", re.IGNORECASE),
    re.compile(r"\b(rating|outperform|underperform|neutral|buy rating|sell rating)\b", re.IGNORECASE),
]

_VAGUE_NEWS_PATTERNS = [
    re.compile(r"\bstocks?\s+to\s+(watch|buy|sell)\b", re.IGNORECASE),
    re.compile(r"\bmay\s+(rise|fall|gain|lose|benefit)\b", re.IGNORECASE),
    re.compile(r"\bcould\s+(rise|fall|gain|lose|benefit)\b", re.IGNORECASE),
    re.compile(r"\bsees?\s+(shares?|stock)\b", re.IGNORECASE),
    re.compile(r"\btraders?\s+(watch|eye)\b", re.IGNORECASE),
]

_MATERIAL_AMOUNT_RE = re.compile(
    r"(\$|\busd\b|\b\d+(?:\.\d+)?\s*(?:b|bn|billion|m|mn|million)\b|\b\d+(?:\.\d+)?%)",
    re.IGNORECASE,
)

_EARNINGS_TERMS_RE = re.compile(
    r"\b(earnings|eps|revenue|sales|results|quarter|q[1-4]|profit|margin)\b",
    re.IGNORECASE,
)
_EARNINGS_SURPRISE_RE = re.compile(
    r"\b(beats?|misses?|tops?|above|below|better-than-expected|worse-than-expected|raises?|lifts?|cuts?|lowers?|guidance|outlook|forecast)\b",
    re.IGNORECASE,
)
_FDA_EVENT_RE = re.compile(
    r"\b(fda|ema|pdufa|complete response letter|crl|phase\s*[23]|primary endpoint|clinical trial|trial)\b",
    re.IGNORECASE,
)
_BIOTECH_RESULT_RE = re.compile(
    r"\b(approves?|approval|clears?|clearance|rejects?|rejection|denies?|meets?|met|fails?|failed|positive|negative|topline|primary endpoint)\b",
    re.IGNORECASE,
)
_MA_EVENT_RE = re.compile(
    r"\b(acquires?|acquisition|merger|merge|buyout|takeover|to buy|take private)\b",
    re.IGNORECASE,
)
_CONTRACT_EVENT_RE = re.compile(
    r"\b(contract|order|award|awarded|deal|selected by|purchase agreement)\b",
    re.IGNORECASE,
)
_REGULATORY_EVENT_RE = re.compile(
    r"\b(doj|ftc|sec|regulator|regulatory|court|judge|settlement|fine|penalty|license|approval|approved|blocks?|clears?)\b",
    re.IGNORECASE,
)
_REGULATORY_MATERIALITY_RE = re.compile(
    r"\b(merger|acquisition|settlement|fine|penalty|license|ban|blocked|cleared|approval|approved|lawsuit|court)\b",
    re.IGNORECASE,
)


def is_retrospective_headline(headline: str) -> bool:
    """Return True if the headline describes a price move that already happened."""
    return any(p.search(headline) for p in _RETROSPECTIVE_PATTERNS)


def is_routine_news(headline: str) -> bool:
    """Return True if the headline matches scheduled routine data with no surprise alpha."""
    return any(p.search(headline) for p in _ROUTINE_PATTERNS)


def is_vague_or_analyst_news(headline: str, summary: str | None = None) -> bool:
    """Return True for analyst, watchlist, and speculative narrative items."""
    text = f"{headline or ''} {summary or ''}"
    return any(p.search(text) for p in (*_ANALYST_PATTERNS, *_VAGUE_NEWS_PATTERNS))


def is_hard_catalyst_news(headline: str, summary: str | None = None) -> bool:
    """Return True only for concrete catalysts that are plausibly tradeable."""
    text = f"{headline or ''} {summary or ''}"
    has_amount = _MATERIAL_AMOUNT_RE.search(text) is not None

    if _FDA_EVENT_RE.search(text) and _BIOTECH_RESULT_RE.search(text):
        return True
    if _EARNINGS_TERMS_RE.search(text) and _EARNINGS_SURPRISE_RE.search(text) and has_amount:
        return True
    if _MA_EVENT_RE.search(text) and has_amount:
        return True
    if _CONTRACT_EVENT_RE.search(text) and has_amount:
        return True
    if _REGULATORY_EVENT_RE.search(text) and _REGULATORY_MATERIALITY_RE.search(text) and has_amount:
        return True
    return False


_SOFT_PARTNERSHIP_TERMS = (
    "partnership",
    "partners with",
    "partnered with",
    "collaboration",
    "collaborates",
    "strategic investment",
    "invests in",
    "investment in",
)

_MATERIAL_CATALYST_TERMS = (
    "acquisition",
    "acquire",
    "merger",
    "contract",
    "order",
    "award",
    "revenue",
    "sales",
    "eps",
    "earnings",
    "guidance",
    "profit",
    "approval",
    "rejection",
    "fda",
    "settlement",
)


def is_soft_partnership_without_materiality(headline: str, summary: str | None = None) -> bool:
    """Return True for partnership/investment items without a concrete financial catalyst."""
    text = f"{headline or ''} {summary or ''}".lower()
    if not any(term in text for term in _SOFT_PARTNERSHIP_TERMS):
        return False
    return not any(term in text for term in _MATERIAL_CATALYST_TERMS)


def compute_news_age_hours(article_ts: datetime) -> float:
    """Return hours elapsed since article_ts. Raises ValueError for naive datetimes."""
    if article_ts.tzinfo is None:
        raise ValueError("article_ts must be timezone-aware")
    delta = datetime.now(timezone.utc) - article_ts
    return delta.total_seconds() / 3600
