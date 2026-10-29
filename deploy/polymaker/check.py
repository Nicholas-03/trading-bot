"""Is the poly-maker reward farm making money? Open orders (and whether they score), the bot market's position
marked at the midpoint, fills, and liquidity rewards earned per day.

    python check.py [--days 7]     # needs PK and BROWSER_ADDRESS (the poly-maker .env) and py-clob-client-v2

P&L = rewards earned + (position value at the midpoint - cost of the filled shares). Fills come from the CLOB trade
history of the bot's market only, so the account's older, unrelated positions don't count.
"""
import argparse
import json
import os
import urllib.request
from datetime import date, timedelta

from dotenv import find_dotenv, load_dotenv
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import OrderScoringParams, TradeParams

SLUG = "will-chinas-annual-inflation-in-2026-be-between-0pt6-and-1pt0"
START = date(2026, 10, 5)  # first live day on this market


def main():
    load_dotenv(find_dotenv(usecwd=True))  # the .env of the directory it runs in
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    c = ClobClient("https://clob.polymarket.com", key=os.environ["PK"], chain_id=137, signature_type=3,
                   funder=os.environ["BROWSER_ADDRESS"])
    c.set_api_creds(c.create_or_derive_api_key())
    m = json.load(urllib.request.urlopen(urllib.request.Request(
        f"https://gamma-api.polymarket.com/markets?slug={SLUG}", headers={"User-Agent": "polymaker-check"})))[0]
    cid = m["conditionId"]
    tokens = json.loads(m["clobTokenIds"])
    outcomes = json.loads(m["outcomes"])
    mids = {t: float(c.get_midpoint(t).get("mid", 0)) for t in tokens}
    print(f"{m['question']}  mid YES {mids[tokens[0]]:.3f}  reward min {m.get('rewardsMinSize')} sh, "
          f"band {m.get('rewardsMaxSpread')}c, pool ${m.get('clobRewards', [{}])[0].get('rewardsDailyRate')}/day")

    oo = c.get_open_orders()
    oo = oo.get("data", oo) if isinstance(oo, dict) else oo
    oo = [o for o in oo if o.get("market") == cid]
    for o in oo:
        sc = c.is_order_scoring(OrderScoringParams(orderId=o["id"])).get("scoring")
        print(f"  open {o.get('outcome'):3s} {o['side']} {float(o['original_size']):.0f} @ {o['price']}  "
              f"filled {o['size_matched']}  scoring={sc}")
    if not oo:
        print("  no open orders in the bot's market")

    trades = c.get_trades(TradeParams(market=cid))
    trades = trades.get("data", trades) if isinstance(trades, dict) else trades
    held = {t: 0.0 for t in tokens}
    cost = 0.0
    for t in trades or []:
        if (t.get("match_time") or "") and date.fromtimestamp(int(t["match_time"])) < START:
            continue
        size, price = float(t["size"]), float(t["price"])
        sign = 1 if t["side"] == "BUY" else -1
        if t.get("trader_side") == "MAKER":  # as maker, our side is in maker_orders
            for mo in t.get("maker_orders", []):
                if mo.get("maker_address", "").lower() == os.environ["BROWSER_ADDRESS"].lower():
                    s = float(mo["matched_amount"])
                    sg = 1 if mo["side"] == "BUY" else -1
                    held[mo["asset_id"]] = held.get(mo["asset_id"], 0) + sg * s
                    cost += sg * s * float(mo["price"])
            continue
        held[t["asset_id"]] = held.get(t["asset_id"], 0) + sign * size
        cost += sign * size * price
    value = sum(held[t] * mids.get(t, 0) for t in held)
    print(f"  fills since {START}: {len(trades or [])} trades; held " +
          ", ".join(f"{o} {held[t]:+.1f}" for o, t in zip(outcomes, tokens)) +
          f"; cost ${cost:.2f}, value at mid ${value:.2f}, trading P&L ${value - cost:+.2f}")

    total = 0.0
    for i in range(args.days):
        d = date.today() - timedelta(days=i)
        if d < START:
            break
        try:
            e = c.get_total_earnings_for_user_for_day(d.isoformat())
        except Exception as exc:  # noqa: BLE001
            print(f"  rewards {d}: error {exc!r}"[:160])
            continue
        rows = e if isinstance(e, list) else e.get("data", [e])
        amt = sum(float(r.get("earnings", 0) or 0) for r in rows if isinstance(r, dict))
        total += amt
        print(f"  rewards {d}: ${amt:.4f}")
    print(f"TOTAL: rewards ${total:.4f} + trading ${value - cost:+.2f} = ${total + value - cost:+.2f}")


if __name__ == "__main__":
    main()
