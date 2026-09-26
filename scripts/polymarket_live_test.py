"""Small real-money checks on Polymarket (the user authorised minimum-size tests). Hard cap: MAX_RISK_USD.

    uv run --no-project --with py-clob-client-v2 --with python-dotenv python scripts/polymarket_live_test.py balance
    uv run ... scripts/polymarket_live_test.py latency [--market-slug SLUG] [--n 5]

balance  - collateral balance of the account in .env (POLYMARKET_PRIVATE_KEY signs for the POLYMARKET_ADDRESS wallet,
           POLYMARKET_SIGNATURE_TYPE 3 = POLY_1271 smart wallet; the exchange runs CLOB V2, so py-clob-client-v2)
latency  - posts a post-only BUY of the minimum size at 1 cent (far below the market, so it cannot fill) and cancels it,
           n times, timing the round trips: the real speed of our order path.
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from py_clob_client_v2 import (AssetType, BalanceAllowanceParams, ClobClient, OrderArgs, OrderType,
                               PartialCreateOrderOptions, Side)
from py_clob_client_v2.clob_types import OrderPayload

ROOT = Path(__file__).resolve().parent.parent
HOST = "https://clob.polymarket.com"
MAX_RISK_USD = 10.0  # never commit more than this to any open order or position in these tests


def client() -> ClobClient:
    load_dotenv(ROOT / ".env")
    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    creds = ClobClient(host=HOST, chain_id=137, key=key).create_or_derive_api_key()  # API keys belong to the signer
    return ClobClient(host=HOST, chain_id=137, key=key, creds=creds,
                      signature_type=int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "3")),
                      funder=os.environ["POLYMARKET_ADDRESS"])


def balance(c: ClobClient) -> float:
    return int(c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))["balance"]) / 1e6


def liquid_market(slug: str | None) -> dict:
    q = f"slug={slug}" if slug else "closed=false&limit=20&order=volume24hr&ascending=false"
    req = urllib.request.Request(f"https://gamma-api.polymarket.com/markets?{q}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ms = json.load(r)
    for m in ms:
        if m.get("enableOrderBook") and m.get("acceptingOrders") and 0.2 < float(json.loads(m["outcomePrices"])[0]) < 0.8:
            return m
    sys.exit("no suitable market found")


def latency(c: ClobClient, slug: str | None, n: int) -> None:
    m = liquid_market(slug)
    token = json.loads(m["clobTokenIds"])[0]
    size = float(m.get("orderMinSize") or 5)
    tick = str(m.get("orderPriceMinTickSize") or "0.01")
    price = float(tick)
    assert size * price <= MAX_RISK_USD
    print(f"market: {m['question']} | {size:g} shares at {price} = ${size * price:.2f} (cannot fill: market is ~{json.loads(m['outcomePrices'])[0]})")
    post_ms, cancel_ms = [], []
    for _ in range(n):
        order = c.create_order(OrderArgs(token_id=token, price=price, size=size, side=Side.BUY),
                               PartialCreateOrderOptions(tick_size=tick))
        t0 = time.perf_counter()
        resp = c.post_order(order, OrderType.GTC, post_only=True)
        post_ms.append((time.perf_counter() - t0) * 1000)
        oid = resp.get("orderID")
        if not resp.get("success") or not oid:
            sys.exit(f"order rejected: {resp}")
        t0 = time.perf_counter()
        c.cancel_order(OrderPayload(orderID=oid))
        cancel_ms.append((time.perf_counter() - t0) * 1000)
    print(f"post: median {statistics.median(post_ms):.0f} ms (min {min(post_ms):.0f}, max {max(post_ms):.0f}); "
          f"cancel: median {statistics.median(cancel_ms):.0f} ms")
    left = c.get_open_orders()
    print("open orders left:", len(left))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("balance", "latency"))
    ap.add_argument("--market-slug")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()
    c = client()
    bal = balance(c)
    print(f"balance ${bal:.2f}")
    if args.cmd == "latency":
        if bal < 0.10:
            sys.exit("not enough USDC in the Polymarket account for even a 1-cent test order")
        latency(c, args.market_slug, args.n)


if __name__ == "__main__":
    main()
