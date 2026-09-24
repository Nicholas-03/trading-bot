"""Label Alpaca (Benzinga) news with what the bot's trade would have done, using Alpaca 1-minute SIP bars.

    .venv/bin/python scripts/build_alpaca_labels.py --start 2024-01-02 --end 2026-09-23 [--workers 3]

Same news feed the bot trades live. Only regular-session news (09:31-15:30 ET) is kept. For each (news, ticker):
  entry   = open of the first 1-minute bar starting >= news time + 60s (the bot's reaction latency)
  exit    = close of the bar 60 minutes later, or at 15:50 ET (the bot flattens 10 minutes before the close)
  ret_1h  = raw return entry -> exit;  excess_1h = ret_1h - SPY's return over the same minutes
  sim_long / sim_short = the bot's bracket trade (stop-loss 2%, take-profit 3%, time exit), stop checked first
  tradable = entry >= $20 and the 8 minutes before entry average >= 1000 shares and >= $50k per minute
Labels: `label` = buy / short / hold on excess_1h at +-1%;  `label_sim` = buy if sim_long >= 1%, short if sim_short >= 1%.
Per-day results are cached in data/alpaca_labels/ (re-runs skip finished days); all days are merged into
data/laya_alpaca_labels.jsonl.
"""
import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "alpaca_labels"
ET = ZoneInfo("America/New_York")
DATA = "https://data.alpaca.markets"
STOP, TAKE, HORIZON_MIN, LATENCY_S = 0.02, 0.03, 60, 60


class RateLimiter:
    """Free Alpaca data plan: 200 requests/minute across all threads."""

    def __init__(self, per_minute: int = 190) -> None:
        self.gap, self.next, self.lock = 60.0 / per_minute, 0.0, threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            self.next = max(self.next, now) + self.gap
            delay = self.next - self.gap - now
        if delay > 0:
            time.sleep(delay)


LIMIT = RateLimiter()


def get(path: str, params: dict) -> dict:
    url = f"{DATA}{path}?{urllib.parse.urlencode(params)}"
    headers = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
    for attempt in range(6):
        LIMIT.wait()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError(f"giving up on {url}")


def fetch_news(day: date) -> list[dict]:
    start = datetime(day.year, day.month, day.day, 9, 31, tzinfo=ET)
    end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=ET)
    params = {"start": start.astimezone(timezone.utc).isoformat(), "end": end.astimezone(timezone.utc).isoformat(),
              "limit": 50, "sort": "asc", "include_content": "false"}
    out, token = [], None
    while True:
        d = get("/v1beta1/news", {**params, **({"page_token": token} if token else {})})
        out += d["news"]
        token = d.get("next_page_token")
        if not token:
            return out


def fetch_bars(day: date, symbols: list[str]) -> dict[str, dict[int, tuple]]:
    """{symbol: {minute_epoch: (open, high, low, close, volume)}} for the regular session."""
    start = datetime(day.year, day.month, day.day, 9, 0, tzinfo=ET).astimezone(timezone.utc)
    end = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET).astimezone(timezone.utc)
    bars: dict[str, dict[int, tuple]] = {}
    chunks = [symbols[i:i + 200] for i in range(0, len(symbols), 200)]
    while chunks:
        chunk = chunks.pop()
        params = {"symbols": ",".join(chunk), "timeframe": "1Min", "start": start.isoformat(),
                  "end": end.isoformat(), "limit": 10000, "feed": "sip", "adjustment": "raw"}
        token = None
        while True:
            try:
                d = get("/v2/stocks/bars", {**params, **({"page_token": token} if token else {})})
            except urllib.error.HTTPError as e:
                if e.code != 400 or token:
                    raise
                if len(chunk) > 1:  # an invalid symbol rejects the whole request: bisect to drop it
                    chunks += [chunk[: len(chunk) // 2], chunk[len(chunk) // 2:]]
                break
            for sym, rows in (d.get("bars") or {}).items():
                m = bars.setdefault(sym, {})
                for b in rows:
                    t = int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp())
                    m[t] = (b["o"], b["h"], b["l"], b["c"], b["v"])
            token = d.get("next_page_token")
            if not token:
                break
    return bars


def simulate(path: list[tuple], entry: float, side: int) -> float:
    """Bracket trade over `path` bars: returns the % result for side +1 (long) or -1 (short)."""
    stop = entry * (1 - STOP * side)
    take = entry * (1 + TAKE * side)
    for _, h, lo, c, _ in path:
        if (side == 1 and lo <= stop) or (side == -1 and h >= stop):
            return -STOP
        if (side == 1 and h >= take) or (side == -1 and lo <= take):
            return TAKE
    return side * (path[-1][3] / entry - 1)


def label_pair(ts: int, bars: dict[int, tuple], spy: dict[int, tuple], flatten: int) -> dict | None:
    first = ((ts + LATENCY_S + 59) // 60) * 60
    entry_t = next((t for t in range(first, first + 5 * 60, 60) if t in bars), None)
    if entry_t is None or entry_t not in spy:
        return None
    exit_t = min(entry_t + HORIZON_MIN * 60, flatten)
    if exit_t - entry_t < 15 * 60:
        return None
    path = [bars[t] for t in range(entry_t, exit_t, 60) if t in bars]
    spy_last = next((t for t in range(exit_t - 60, entry_t, -60) if t in spy), None)
    if len(path) < 2 or spy_last is None:
        return None
    entry = bars[entry_t][0]
    before = [bars[t] for t in range(entry_t - 8 * 60, entry_t, 60) if t in bars]
    avg_vol = sum(b[4] for b in before) / 8
    avg_dollar = sum(b[4] * b[3] for b in before) / 8
    ret = path[-1][3] / entry - 1
    spy_ret = spy[spy_last][3] / spy[entry_t][0] - 1
    pre = [bars[t] for t in range(entry_t - 30 * 60, entry_t, 60) if t in bars]
    return {
        "entry": round(entry, 4),
        "ret_1h": round(ret, 5),
        "excess_1h": round(ret - spy_ret, 5),
        "sim_long": round(simulate(path, entry, 1), 5),
        "sim_short": round(simulate(path, entry, -1), 5),
        "pre_30m": round(entry / pre[0][0] - 1, 5) if pre else None,
        "tradable": entry >= 20 and avg_vol >= 1000 and avg_dollar >= 50_000,
    }


def do_day(day: date, threshold: float) -> tuple[date, int]:
    out = CACHE / f"{day}.jsonl"
    if out.exists():
        return day, sum(1 for _ in open(out))
    news = fetch_news(day)
    symbols = sorted({s for n in news for s in n["symbols"] if s.isascii() and s.replace(".", "").isalpha()} | {"SPY"})
    bars = fetch_bars(day, symbols) if news else {}
    spy = bars.get("SPY", {})
    flatten = int(datetime(day.year, day.month, day.day, 15, 50, tzinfo=ET).timestamp())
    rows = []
    for n in news:
        ts = int(datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")).timestamp())
        syms = list(dict.fromkeys(n["symbols"]))
        for sym in syms:
            lab = label_pair(ts, bars.get(sym, {}), spy, flatten) if spy else None
            if lab is None:
                continue
            ex, sl, ss = lab["excess_1h"] * 100, lab["sim_long"] * 100, lab["sim_short"] * 100
            rows.append({
                "news_id": n["id"], "ts": n["created_at"], "ticker": sym, "headline": n["headline"],
                "summary": n.get("summary") or "", "source": n.get("source") or "", "n_tickers": len(syms), **lab,
                "label": "buy" if ex >= threshold else "short" if ex <= -threshold else "hold",
                "label_sim": "buy" if sl >= threshold and sl >= ss else "short" if ss >= threshold else "hold",
            })
    tmp = out.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    tmp.rename(out)
    return day, len(rows)


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default=str(date.today() - timedelta(days=1)))
    ap.add_argument("--threshold", type=float, default=1.0, help="label threshold in percent")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "data" / "laya_alpaca_labels.jsonl"))
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)

    d, end, days = date.fromisoformat(args.start), date.fromisoformat(args.end), []
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    t0, total = time.time(), 0
    with ThreadPoolExecutor(args.workers) as pool:
        for i, (day, n) in enumerate(pool.map(lambda x: do_day(x, args.threshold), days), 1):
            total += n
            if i % 10 == 0 or i == len(days):
                print(f"{i}/{len(days)} days ({day}), {total} pairs, {time.time() - t0:.0f}s", flush=True)

    counts: dict[str, int] = {}
    with open(args.out, "w") as f:
        for day in days:
            for line in open(CACHE / f"{day}.jsonl"):
                r = json.loads(line)
                counts[r["label"]] = counts.get(r["label"], 0) + 1
                f.write(line)
    print(json.dumps(counts), "->", args.out)


if __name__ == "__main__":
    main()
