"""S&P index changes: index funds must buy added stocks (and sell removed ones) by a known date. Does the forced flow
leave a tradable move between the announcement and the effective date, or a reversal after it?

    .venv/bin/python scripts/index_changes.py collect   # Alpaca news 16:00-23:59 ET each weekday -> data/index_change_news.jsonl
    .venv/bin/python scripts/index_changes.py study     # parse the announcements, price them with daily bars, print the results

S&P Dow Jones Indices announces changes after the close (usually ~17:15 ET), effective before the open some days later.
collect keeps only headlines that look like index-change announcements (resumable: days done in a .checked file).
"""
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402

NEWS = ROOT / "data" / "index_change_news.jsonl"
CHECKED = NEWS.with_suffix(".checked")
INDEX = re.compile(r"S&P\s*(500|Mid\s*Cap\s*400|Small\s*Cap\s*600|400|600)", re.I)
CHANGE = re.compile(r"\b(join|joins|joining|added|add|adds|replace|replaces|replacing|removed|remove|drop|dropped|"
                    r"move|moves|moving|inclusion|included|deleted|deletion)\b", re.I)


def collect(start: date = date(2016, 1, 1), end: date | None = None) -> None:
    end = end or date.today() - timedelta(days=1)
    seen = set(CHECKED.read_text().split()) if CHECKED.exists() else set()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    todo = [d for d in days if d.weekday() < 5 and d.isoformat() not in seen]
    print(f"{len(todo)} weekdays to scan", flush=True)
    lock, n, hits = threading.Lock(), 0, 0

    def one(d: date):
        nonlocal n, hits
        try:
            news = bal.fetch_news(d, ((16, 0), (23, 59)))
        except Exception as exc:
            print("error", d, exc, flush=True)
            return
        keep = [x for x in news if INDEX.search(x["headline"]) and CHANGE.search(x["headline"])]
        with lock:
            with open(NEWS, "a") as f:
                f.writelines(json.dumps({"day": d.isoformat(), "id": x["id"], "created_at": x["created_at"],
                                         "headline": x["headline"], "summary": x.get("summary") or "",
                                         "symbols": x.get("symbols") or [], "source": x.get("source")}) + "\n"
                             for x in keep)
            with open(CHECKED, "a") as f:
                f.write(d.isoformat() + "\n")
            n += 1
            hits += len(keep)
            if n % 100 == 0:
                print(f"  {n}/{len(todo)} days, {hits} index-change headlines", flush=True)

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, todo))
    print(f"done: {n} days, {hits} headlines", flush=True)


def changes() -> list[dict]:
    """S&P 500 changes from Wikipedia's 'Selected changes' table (a May 2026 revision: the current page dropped it)."""
    import pandas as pd
    t = pd.read_html(ROOT / "data" / "wiki_sp500_old.html")[1]
    t.columns = ["eff", "add", "add_name", "rem", "rem_name", "reason"]
    out = []
    for r in t.itertuples():
        eff = pd.to_datetime(r.eff, errors="coerce")
        if pd.isna(eff) or eff.year < 2016:
            continue
        for side, sym in (("add", r.add), ("rem", r.rem)):
            if isinstance(sym, str) and sym.strip():
                out.append({"side": side, "ticker": sym.strip().replace(".", "/"), "eff": eff.date().isoformat(),
                            "reason": str(r.reason)})
    return out


def daily(sym: str) -> dict[str, tuple[float, float]]:
    """Split-adjusted daily (open, close) since 2015-12 from Alpaca SIP; {} for an unknown symbol."""
    out, token = {}, None
    while True:
        try:
            d = bal.get("/v2/stocks/bars", {"symbols": sym, "timeframe": "1Day", "start": "2015-12-01",
                                            "limit": 10000, "feed": "sip", "adjustment": "all",
                                            **({"page_token": token} if token else {})})
        except bal.urllib.error.HTTPError:
            return out
        for b in (d.get("bars") or {}).get(sym, []):
            out[b["t"][:10]] = (b["o"], b["c"])
        token = d.get("next_page_token")
        if not token:
            return out


def study(cost: float = 0.2) -> None:
    import math
    news = [json.loads(line) for line in open(NEWS)]
    ch = changes()
    bars_file = ROOT / "data" / "index_change_bars.json"
    bars = json.loads(bars_file.read_text()) if bars_file.exists() else {}
    need = sorted({c["ticker"] for c in ch} | {"SPY"} - set(bars))
    with ThreadPoolExecutor(4) as ex:
        for sym, b in zip(need, ex.map(daily, need)):
            bars[sym] = b
    bars_file.write_text(json.dumps(bars))
    spy = bars["SPY"]
    days = sorted(spy)
    rows = []
    for c in ch:
        b = bars.get(c["ticker"]) or {}
        e = next((i for i, d in enumerate(days) if d >= c["eff"]), None)
        if e is None or e < 1:
            continue
        # the announcement: the first index-change headline tagging the stock in the 40 days before the effective date
        ann = sorted((n["created_at"], n["day"]) for n in news if c["ticker"].replace("/", ".") in n["symbols"]
                     and c["eff"] > n["day"] >= (date.fromisoformat(c["eff"]) - timedelta(days=40)).isoformat())
        if not ann:
            rows.append({**c, "found": False})
            continue
        a = max(i for i, d in enumerate(days) if d <= ann[0][1])  # the session the after-close announcement follows
        last = e - 1  # index funds trade at this close
        def ex(i, j, field_i=1, field_j=1):  # stock minus SPY, from bar i (open=0/close=1) to bar j
            if not (0 <= i < len(days) and 0 <= j < len(days)) or days[i] not in b or days[j] not in b:
                return None
            return (b[days[j]][field_j] / b[days[i]][field_i] - 1) - (spy[days[j]][field_j] / spy[days[i]][field_i] - 1)
        rows.append({**c, "found": True, "ann": ann[0][0], "days_to_eff": last - a,
                     "gap": ex(a, a + 1, 1, 0), "runup": ex(a + 1, last, 0, 1), "ann_to_eff": ex(a, last),
                     "post5": ex(last, last + 5), "post20": ex(last, last + 20)})
    found = [r for r in rows if r["found"]]
    print(f"{len(rows)} S&P 500 changes since 2016 ({sum(r['side'] == 'add' for r in rows)} adds), "
          f"announcement found for {len(found)}")

    def st(name, xs, sign=1):
        xs = [sign * x * 100 - cost for x in xs if x is not None and abs(x) < 1]
        if len(xs) < 3:
            print(f"  {name:48s} n={len(xs)}")
            return
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
        print(f"  {name:48s} n={len(xs):4d}  {m:+6.2f}%  t {m / sd * math.sqrt(len(xs)):+5.1f}  win {sum(x > 0 for x in xs) / len(xs):.0%}")

    for side, sign, word in (("add", 1, "long"), ("rem", -1, "short")):
        rs = [r for r in found if r["side"] == side]
        mc = [r for r in rs if "market cap" in r["reason"].lower()]
        print(f"\n== {'ADDITIONS' if side == 'add' else 'REMOVALS'} ({word}, minus SPY, after {cost}% cost; gap is not tradable)")
        for lab, sub in (("all", rs), ("market-cap changes", mc), ("2016-2020", [r for r in rs if r["eff"] < "2021"]),
                         ("2021-2026", [r for r in rs if r["eff"] >= "2021"])):
            print(f" {lab}:")
            st("gap: announcement-day close -> next open", [r["gap"] for r in sub], sign)
            st("run-up: next open -> close before effective", [r["runup"] for r in sub], sign)
            st(f"post: rebalance close -> +5 days ({'short' if side == 'add' else 'long'})", [r["post5"] for r in sub], -sign)
            st(f"post: rebalance close -> +20 days ({'short' if side == 'add' else 'long'})", [r["post20"] for r in sub], -sign)
    with open(ROOT / "results" / "index_changes_events.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    {"collect": collect, "study": study}[sys.argv[1]]()
