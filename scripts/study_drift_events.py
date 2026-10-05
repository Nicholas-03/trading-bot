"""Event table for the post-news drift study (scripts/study_drift.py).

    .venv/bin/python scripts/study_drift_events.py

Events: the first regular-session news of each stock and day (all pairs in the Alpaca label files, tradable or not)
whose first hour after the news (entry = first 1-minute bar >= news + 60 s, exit 60 minutes later, flattened 15:50 ET)
moved at least 5% against SPY, either way. For each event, daily bars (open, high, low, close; split/dividend adjusted
and raw) for 30 trading days before to 21 after, from Alpaca. Output: data/drift_events.jsonl.
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402

CACHE = ROOT / "data" / "daily_ohlc"
OUT = ROOT / "data" / "drift_events.jsonl"


def fetch(symbols: list[str], adjustment: str) -> dict[str, dict[str, list]]:
    """{symbol: {date: [o, h, l, c, v]}} from 2020-10-01 to 2026-09-27."""
    out: dict[str, dict[str, list]] = {}
    chunks = [symbols[i:i + 100] for i in range(0, len(symbols), 100)]
    done = 0
    while chunks:
        chunk = chunks.pop()
        params = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": "2020-10-01", "end": "2026-09-27",
                  "limit": 10000, "feed": "sip", "adjustment": adjustment}
        token = None
        while True:
            try:
                d = bal.get("/v2/stocks/bars", {**params, **({"page_token": token} if token else {})})
            except bal.urllib.error.HTTPError as e:
                if e.code != 400 or token:
                    raise
                if len(chunk) > 1:  # an invalid symbol rejects the whole request: bisect to drop it
                    chunks += [chunk[: len(chunk) // 2], chunk[len(chunk) // 2:]]
                break
            for sym, rows in (d.get("bars") or {}).items():
                m = out.setdefault(sym, {})
                for b in rows:
                    m[b["t"][:10]] = [b["o"], b["h"], b["l"], b["c"], b["v"]]
            token = d.get("next_page_token")
            if not token:
                done += len(chunk)
                break
        if done and done % 1000 < 100:
            print(f"{adjustment}: ~{done}/{len(symbols)} symbols", flush=True)
    return out


def main():
    load_dotenv(ROOT / ".env")
    CACHE.mkdir(parents=True, exist_ok=True)
    ev = {}
    for f in ("laya_alpaca_labels_2021_2023.jsonl", "laya_alpaca_labels_v2.jsonl"):
        for line in open(ROOT / "data" / f):
            r = json.loads(line)
            if r.get("excess_1h") is None:
                continue
            day = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(bal.ET).date().isoformat()
            k = (r["ticker"], day)
            if k not in ev or r["ts"] < ev[k]["ts"]:
                ev[k] = {"ticker": r["ticker"], "day": day, "ts": r["ts"], "news_id": r["news_id"],
                         "headline": r["headline"], "n_tickers": r["n_tickers"], "source": r.get("source"),
                         "entry": r["entry"], "ret_1h": r["ret_1h"], "excess_1h": r["excess_1h"], "react": r.get("react"),
                         "pre_30m": r.get("pre_30m"), "tradable": r.get("tradable")}
    big = [e for e in ev.values() if abs(e["excess_1h"]) >= 0.05]
    tick = {e["ticker"] for e in big}
    # control: news days of the same stocks when the first hour barely moved (|excess| < 2%)
    rng = __import__("random").Random(1)  # a 10% sample is plenty
    ctrl = [{**e, "control": True} for e in ev.values() if abs(e["excess_1h"]) < 0.02 and e["ticker"] in tick
            and rng.random() < 0.1]
    ev = big + ctrl
    print(len(big), "events with |first hour| >= 5%,", len(ctrl), "control news days of the same stocks")
    syms = sorted(tick | {"SPY"})
    bars = {}
    for adj in ("all", "raw"):
        path = CACHE / f"{adj}.json"
        if not path.exists():
            path.write_text(json.dumps(fetch(syms, adj)))
        bars[adj] = json.loads(path.read_text())
    iwm = CACHE / "iwm.json"  # small-cap index, a closer hedge for these stocks than SPY
    if not iwm.exists():
        iwm.write_text(json.dumps(fetch(["IWM"], "all").get("IWM", {})))
    iwm = json.loads(iwm.read_text())
    days = sorted(bars["all"]["SPY"])
    idx = {d: i for i, d in enumerate(days)}
    n = 0
    with open(OUT, "w") as f:
        for e in ev:
            i = idx.get(e["day"])
            a, r = bars["all"].get(e["ticker"], {}), bars["raw"].get(e["ticker"], {})
            if i is None or e["day"] not in a or e["day"] not in r:
                continue
            window = days[max(0, i - 30): i + 22]
            e["dates"] = window
            e["i0"] = window.index(e["day"])
            e["adj"] = [a.get(d) for d in window]
            e["raw"] = [r.get(d) for d in window]
            e["spy"] = [bars["all"]["SPY"].get(d) for d in window]
            e["iwm"] = [iwm.get(d) for d in window]
            f.write(json.dumps(e) + "\n")
            n += 1
    print(n, "events ->", OUT)


if __name__ == "__main__":
    main()
