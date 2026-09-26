"""Price and volume path around each tradable news pair, from Alpaca 1-minute SIP bars (pre-market included).

    .venv/bin/python scripts/build_price_paths.py [--labels data/laya_alpaca_labels_v2.jsonl] [--workers 4]

Everything in `pre` / `early` is known before the bot's entry (the open of the first bar >= news + 60s), so it can be
used as model input; `fwd` is what happens after entry (labels).
  p_news   = close of the last 1-minute bar that finished before the news
  pre      = p_news / price k minutes before the news - 1, k in 60, 30, 10, 5, 2, 1 (and the same for SPY)
  early    = entry / p_news - 1, high/low between news and entry vs p_news, and volume since the news vs the
             average minute of the previous 30 (a volume spike means the news is getting attention)
  fwd      = raw and SPY-excess return from entry at +1, +2, +5, +10, +15, +30, +60 minutes (flattened by 15:50 ET)
Per-day results are cached in data/price_paths/; merged into data/laya_price_paths.jsonl.
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402  (rate-limited Alpaca client + bar fetching)

CACHE = ROOT / "data" / "price_paths"
PRE = (60, 30, 10, 5, 2, 1)
FWD = (1, 2, 5, 10, 15, 30, 60)


def fetch_day_bars(day: date, symbols: list[str]) -> dict[str, dict[int, tuple]]:
    """1-minute bars from 04:00 to 16:00 ET (fetch_bars covers 09:00-16:00; widen it for the 60-minute lookback)."""
    start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=bal.ET).astimezone(timezone.utc)
    end = datetime(day.year, day.month, day.day, 16, 0, tzinfo=bal.ET).astimezone(timezone.utc)
    bars: dict[str, dict[int, tuple]] = {}
    chunks = [symbols[i:i + 100] for i in range(0, len(symbols), 100)]
    while chunks:
        chunk = chunks.pop()
        params = {"symbols": ",".join(chunk), "timeframe": "1Min", "start": start.isoformat(),
                  "end": end.isoformat(), "limit": 10000, "feed": "sip", "adjustment": "raw"}
        token = None
        while True:
            try:
                d = bal.get("/v2/stocks/bars", {**params, **({"page_token": token} if token else {})})
            except bal.urllib.error.HTTPError as e:
                if e.code != 400 or token:
                    raise
                if len(chunk) > 1:
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


def price_asof(bars: dict[int, tuple], t: int, lookback_min: int = 15) -> float | None:
    """Close of the last bar that finished at or before t (bar start <= t - 60)."""
    start = (t // 60) * 60 - 60
    for s in range(start, start - lookback_min * 60, -60):
        if s in bars:
            return bars[s][3]
    return None


def path(ts: int, bars: dict[int, tuple], spy: dict[int, tuple], flatten: int) -> dict | None:
    first = ((ts + bal.LATENCY_S + 59) // 60) * 60
    entry_t = next((t for t in range(first, first + 5 * 60, 60) if t in bars), None)
    p_news, s_news = price_asof(bars, ts), price_asof(spy, ts)
    if entry_t is None or entry_t not in spy or not p_news or not s_news:
        return None
    entry, s_entry = bars[entry_t][0], spy[entry_t][0]
    pre, spy_pre = {}, {}
    for k in PRE:
        p, s = price_asof(bars, ts - k * 60, 30), price_asof(spy, ts - k * 60, 30)
        pre[k] = round(p_news / p - 1, 5) if p else None
        spy_pre[k] = round(s_news / s - 1, 5) if s else None
    since = [bars[t] for t in range((ts // 60) * 60, entry_t, 60) if t in bars]  # bars touched by the news, before entry
    base = [bars[t][4] for t in range((ts // 60) * 60 - 30 * 60, (ts // 60) * 60, 60) if t in bars]
    base_vol = sum(base) / 30
    minutes_since = max(1, (entry_t - (ts // 60) * 60) // 60)
    early = {
        "move": round(entry / p_news - 1, 5),
        "spy_move": round(s_entry / s_news - 1, 5),
        "high": round(max(b[1] for b in since) / p_news - 1, 5) if since else None,
        "low": round(min(b[2] for b in since) / p_news - 1, 5) if since else None,
        "vol_ratio": round(sum(b[4] for b in since) / minutes_since / base_vol, 3) if since and base_vol > 0 else None,
        "pre_vol_min": round(base_vol, 1),
    }
    fwd = {}
    for k in FWD:
        end = min(entry_t + k * 60, flatten)
        t = next((t for t in range(end - 60, entry_t - 60, -60) if t in bars), None)
        u = next((t for t in range(end - 60, entry_t - 60, -60) if t in spy), None)
        if t is None or u is None or end <= entry_t:
            fwd[k] = None
            continue
        r = bars[t][3] / entry - 1
        fwd[k] = [round(r, 5), round(r - (spy[u][3] / s_entry - 1), 5)]
    return {"entry": entry, "p_news": p_news, "pre": pre, "spy_pre": spy_pre, "early": early, "fwd": fwd,
            "minute_et": (datetime.fromtimestamp(ts, bal.ET).hour * 60 + datetime.fromtimestamp(ts, bal.ET).minute)}


def do_day(day: str, pairs: list[dict]) -> tuple[str, int]:
    out = CACHE / f"{day}.jsonl"
    if out.exists():
        return day, sum(1 for _ in open(out))
    d = date.fromisoformat(day)
    bars = fetch_day_bars(d, sorted({p["ticker"] for p in pairs} | {"SPY"}))
    spy = bars.get("SPY", {})
    flatten = int(datetime(d.year, d.month, d.day, 15, 50, tzinfo=bal.ET).timestamp())
    rows = []
    for p in pairs:
        ts = int(datetime.fromisoformat(p["ts"].replace("Z", "+00:00")).timestamp())
        x = path(ts, bars.get(p["ticker"], {}), spy, flatten) if spy else None
        if x:
            rows.append({"news_id": p["news_id"], "ticker": p["ticker"], "ts": p["ts"], **x})
    tmp = out.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    tmp.rename(out)
    return day, len(rows)


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(ROOT / "data" / "laya_alpaca_labels_v2.jsonl"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "data" / "laya_price_paths.jsonl"))
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for line in open(args.labels):
        r = json.loads(line)
        if r["tradable"]:
            by_day[r["ts"][:10]].append({k: r[k] for k in ("news_id", "ticker", "ts")})
    days = sorted(by_day)
    t0, total = time.time(), 0
    with ThreadPoolExecutor(args.workers) as pool:
        for i, (day, n) in enumerate(pool.map(lambda d: do_day(d, by_day[d]), days), 1):
            total += n
            if i % 20 == 0 or i == len(days):
                print(f"{i}/{len(days)} days ({day}), {total} pairs, {time.time() - t0:.0f}s", flush=True)
    with open(args.out, "w") as f:
        for day in days:
            f.writelines(open(CACHE / f"{day}.jsonl"))
    print(total, "pairs ->", args.out)


if __name__ == "__main__":
    main()
