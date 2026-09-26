"""Minimum-size live market-making test on Polymarket (user-authorised real money). Hard caps below.

    uv run --no-project --with py-clob-client-v2 --with python-dotenv python scripts/polymarket_mm_live.py [--minutes 30]

Quotes the smallest allowed size as a post-only BUY at the best bid of BOTH outcome tokens (YES and NO) of one liquid,
long-dated market. A YES share plus a NO share always pays $1, so every matched pair locks in 1 - (yes price + no price),
roughly the spread; unmatched shares carry price risk. Never holds more than MAX_IMBALANCE extra shares of one side and
never commits more than MAX_RISK_USD. Everything is cancelled on exit; each event is logged to results/polymarket_mm_live.jsonl.
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
from py_clob_client_v2.clob_types import OrderPayload

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polymarket_live_test as lt  # noqa: E402  (authenticated client, balance)

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "results" / "polymarket_mm_live.jsonl"
MAX_RISK_USD = 10.0
MAX_IMBALANCE = 10  # shares of one outcome beyond the other


def pick_market() -> dict:
    req = urllib.request.Request("https://gamma-api.polymarket.com/markets?closed=false&limit=100&order=volume24hr&ascending=false",
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ms = json.load(r)
    soon = datetime.now(timezone.utc) + timedelta(days=7)
    for m in ms:
        try:
            p = float(json.loads(m["outcomePrices"])[0])
            end = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if (m.get("enableOrderBook") and m.get("acceptingOrders") and end > soon and 0.15 < p < 0.85
                and float(m.get("spread") or 1) <= 0.02 and float(m.get("orderMinSize") or 5) <= 5):
            return m
    sys.exit("no suitable market")


def best_bid(book) -> float | None:
    bids = [float(b.price if hasattr(b, "price") else b["price"]) for b in (book.bids if hasattr(book, "bids") else book["bids"])]
    return max(bids) if bids else None


def log(event: dict) -> None:
    with open(LOG, "a") as f:
        f.write(json.dumps({"t": time.time(), **event}) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    args = ap.parse_args()
    LOG.parent.mkdir(exist_ok=True)
    c = lt.client()
    m = pick_market()
    tokens = dict(zip(("YES", "NO"), json.loads(m["clobTokenIds"])))
    size = float(m.get("orderMinSize") or 5)
    tick = c.get_tick_size(tokens["YES"])
    print(f"market: {m['question']} | size {size:g} | tick {tick} | balance ${lt.balance(c):.2f}", flush=True)
    log({"event": "start", "market": m["question"], "slug": m.get("slug"), "size": size, "tick": tick})

    held = {"YES": 0.0, "NO": 0.0}
    cost = {"YES": 0.0, "NO": 0.0}
    orders = {"YES": None, "NO": None}  # side -> (order id, price, matched so far)
    end = time.time() + args.minutes * 60
    try:
        while time.time() < end:
            for side, tok in tokens.items():
                other = "NO" if side == "YES" else "YES"
                o = orders[side]
                if o:  # record new fills of our resting order
                    info = c.get_order(o[0]) or {}
                    matched = float(info.get("size_matched") or 0)
                    if matched > o[2]:
                        held[side] += matched - o[2]
                        cost[side] += (matched - o[2]) * o[1]
                        log({"event": "fill", "side": side, "price": o[1], "shares": matched - o[2]})
                        print(f"fill {side} {matched - o[2]:g} @ {o[1]}", flush=True)
                        orders[side] = o = (o[0], o[1], matched)
                    if info.get("status") not in (None, "LIVE", "live"):
                        orders[side] = o = None
                bid = best_bid(c.get_order_book(tok))
                committed = cost["YES"] + cost["NO"] + sum(x[1] * (size - x[2]) for x in orders.values() if x)
                allowed = (bid is not None and held[side] - held[other] + size <= MAX_IMBALANCE
                           and committed + (0 if o else bid * size) <= MAX_RISK_USD)
                if o and (not allowed or abs(o[1] - bid) > 1e-9):
                    c.cancel_order(OrderPayload(orderID=o[0]))
                    orders[side] = o = None
                if not o and allowed:
                    order = c.create_order(OrderArgs(token_id=tok, price=bid, size=size, side=Side.BUY),
                                           PartialCreateOrderOptions(tick_size=tick))
                    resp = c.post_order(order, OrderType.GTC, post_only=True)
                    if resp.get("success") and resp.get("orderID"):
                        orders[side] = (resp["orderID"], bid, 0.0)
                        log({"event": "quote", "side": side, "price": bid})
                    else:
                        log({"event": "rejected", "side": side, "price": bid, "resp": resp})
                        print(f"rejected {side} @ {bid}: {resp}", flush=True)
            time.sleep(2)
    finally:
        c.cancel_all()
        pairs = min(held.values())
        avg = {s: cost[s] / held[s] if held[s] else 0 for s in held}
        locked = pairs * (1 - avg["YES"] - avg["NO"]) if pairs else 0.0
        summary = {"event": "end", "held": held, "avg_price": avg, "pairs": pairs, "locked_profit": round(locked, 4),
                   "balance": lt.balance(c)}
        log(summary)
        print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
