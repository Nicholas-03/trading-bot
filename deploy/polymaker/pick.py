"""Which Polymarket reward market should the $22 bot quote? Ranks markets by our estimated share of the daily pool.

    python pick.py [--max-min-size 20] [--min-days 30] [--top 20]   # run from the poly-maker dir (needs its .env)

Only markets whose reward minimum (rewards_min_size) fits the budget on both sides, still open, ending at least
`--min-days` away, YES midpoint 0.15-0.85. Our share: Polymarket scores each resting order by size * ((v - s) / v)^2
(s = distance from the midpoint, v = max spread); we assume a min-size order `ours_at` cents from the mid on each side
and compare with the competing bids in the band on both sides (NO bids are YES asks). Estimated $/day =
pool * ours / (ours + competitors), averaged over the two sides. A rough guide: it ignores how the book moves.
"""
import argparse
import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from dotenv import find_dotenv, load_dotenv
from py_clob_client_v2 import ClobClient


def gamma(cid: str) -> dict | None:
    req = urllib.request.Request(f"https://gamma-api.polymarket.com/markets?condition_ids={cid}",
                                 headers={"User-Agent": "polymaker-pick"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception:  # noqa: BLE001
        return None
    return d[0] if d else None


def book(token: str) -> dict | None:
    try:
        req = urllib.request.Request(f"https://clob.polymarket.com/book?token_id={token}",
                                     headers={"User-Agent": "polymaker-pick"})
        return json.load(urllib.request.urlopen(req, timeout=20))
    except Exception:  # noqa: BLE001
        return None


def main():
    load_dotenv(find_dotenv(usecwd=True))
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-min-size", type=float, default=20)
    ap.add_argument("--min-days", type=float, default=30)
    ap.add_argument("--ours-at", type=float, default=2.0, help="cents from the midpoint our orders rest")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    c = ClobClient("https://clob.polymarket.com", key=os.environ["PK"], chain_id=137, signature_type=3,
                   funder=os.environ["BROWSER_ADDRESS"])
    c.set_api_creds(c.create_or_derive_api_key())
    rewards = c.get_current_rewards()
    rewards = [r for r in rewards if float(r.get("rewards_min_size") or 1e9) <= args.max_min_size
               and float(r.get("total_daily_rate") or 0) >= 5]
    print(f"{len(rewards)} reward markets with min size <= {args.max_min_size:g} and >= $5/day; scoring...", flush=True)
    now = datetime.now(timezone.utc)

    def one(r):
        m = gamma(r["condition_id"])
        if not m or m.get("closed") or not m.get("active") or not m.get("enableOrderBook"):
            return None
        try:
            end = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
            yes_tok = json.loads(m["clobTokenIds"])[0]
        except (KeyError, ValueError, TypeError):
            return None
        days = (end - now).total_seconds() / 86400
        if days < args.min_days:
            return None
        b = book(yes_tok)
        if not b or not b.get("bids") or not b.get("asks"):
            return None
        bid = max(float(x["price"]) for x in b["bids"])
        ask = min(float(x["price"]) for x in b["asks"])
        mid = (bid + ask) / 2
        if not 0.15 <= mid <= 0.85:
            return None
        v = float(r["rewards_max_spread"]) / 100
        q = lambda s, size: size * max(0.0, (v - s) / v) ** 2  # noqa: E731
        comp_yes = sum(q(mid - float(x["price"]), float(x["size"])) for x in b["bids"] if mid - float(x["price"]) < v)
        comp_no = sum(q(float(x["price"]) - mid, float(x["size"])) for x in b["asks"] if float(x["price"]) - mid < v)
        size = float(r["rewards_min_size"])
        ours = q(args.ours_at / 100, size)
        rate = float(r["total_daily_rate"])
        est = rate * 0.5 * (ours / (ours + comp_yes) + ours / (ours + comp_no))
        cost = size * (mid - args.ours_at / 100) + size * (1 - mid - args.ours_at / 100)
        return {"slug": m.get("slug"), "q": m["question"], "days": days, "mid": mid, "spread": ask - bid,
                "rate": rate, "min": size, "band": v * 100, "est": est, "cost": cost, "vol24": float(m.get("volume24hr") or 0)}

    with ThreadPoolExecutor(16) as ex:
        rows = [x for x in ex.map(one, rewards) if x]
    rows.sort(key=lambda x: -x["est"])
    print(f"{'est $/day':>9} {'pool':>5} {'min':>4} {'band':>4} {'mid':>5} {'sprd':>5} {'days':>5} {'cost':>6} {'vol24h':>8}  question / slug")
    for x in rows[:args.top]:
        print(f"{x['est']:9.3f} {x['rate']:5.0f} {x['min']:4.0f} {x['band']:4.1f} {x['mid']:5.3f} {x['spread']:5.3f} "
              f"{x['days']:5.0f} {x['cost']:6.2f} {x['vol24']:8.0f}  {x['q'][:70]}  [{x['slug']}]")


if __name__ == "__main__":
    main()
