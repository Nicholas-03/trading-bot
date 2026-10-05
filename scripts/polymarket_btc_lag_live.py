"""Live run of the Bitcoin up/down latency rule (scripts/polymarket_btc_lag.py): paper by default, real money with --live.

    uv run --no-project --with websockets --with py-clob-client-v2 --with python-dotenv \
        python scripts/polymarket_btc_lag_live.py run [--minutes 60] [--threshold 0.10] [--live]
    uv run ... python scripts/polymarket_btc_lag_live.py report

Streams Binance BTCUSDT trades and the Polymarket books of the running 5- and 15-minute "Bitcoin Up or Down" markets.
At the start of every second: fair P(up) from the last Binance trade before that second, as in the backtest; if the Up
ask (or the Down ask) is at least `threshold` below fair with >= 5 shares shown, buy 5 shares (paper: record the ask;
live: a fill-and-kill limit order at that ask). One trade per side per market per 30 seconds. Held to settlement.
Hard caps for --live: 5 shares per order, MAX_OPEN_USD in unsettled positions, stop after MAX_LOSS_USD of balance lost.
Every signal is logged to results/polymarket_btc_lag_live.jsonl; `report` settles them from gamma and prints P&L.
"""
import argparse
import asyncio
import bisect
import json
import math
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "results" / "polymarket_btc_lag_live.jsonl"
UA = {"User-Agent": "Mozilla/5.0"}
SERIES = (("btc-updown-5m", 300), ("btc-updown-15m", 900))
SIZE = 5
FEE_AT_HALF = 0.0175  # crypto taker fee 0.07 * p * (1 - p)
MAX_OPEN_USD = 10.0
MAX_LOSS_USD = 10.0


def get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return json.load(r)


def log(event: dict) -> None:
    with open(LOG, "a") as f:
        f.write(json.dumps({"logged": time.time(), **event}) + "\n")


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class Btc:
    """Binance trade tape: price of the last trade before any time, and per-second log returns."""

    def __init__(self):
        now = int(time.time())
        rows = get(f"https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1s&startTime={(now - 1000) * 1000}&limit=1000")
        self.t = [r[0] + 999 for r in rows]
        self.p = [float(r[4]) for r in rows]

    def add(self, t_ms: int, price: float) -> None:
        if t_ms >= self.t[-1]:
            self.t.append(t_ms)
            self.p.append(price)

    def before(self, t_ms: int) -> float:
        return self.p[bisect.bisect_left(self.t, t_ms) - 1]

    def sigma(self, s: int, n: int = 900) -> float:
        px = np.array([self.before(k * 1000) for k in range(s - n, s + 1)])
        return float(np.diff(np.log(px)).std())

    def trim(self) -> None:
        cut = bisect.bisect_left(self.t, (time.time() - 1500) * 1000)
        if cut > 0:
            del self.t[:cut], self.p[:cut]


class Book:
    def __init__(self):
        self.bids, self.asks, self.updated = {}, {}, 0.0

    def snapshot(self, e: dict) -> None:
        self.bids = {float(x["price"]): float(x["size"]) for x in e["bids"]}
        self.asks = {float(x["price"]): float(x["size"]) for x in e["asks"]}
        self.updated = time.time()

    def change(self, c: dict) -> None:
        book = self.bids if c["side"] == "BUY" else self.asks
        price, size = float(c["price"]), float(c["size"])
        if size:
            book[price] = size
        else:
            book.pop(price, None)
        self.updated = time.time()

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        a = min(self.asks)
        return a, self.asks[a]


class Bot:
    def __init__(self, threshold: float, live: bool):
        self.threshold, self.live = threshold, live
        self.btc = Btc()
        self.books: dict[str, Book] = defaultdict(Book)
        self.markets: dict[str, dict] = {}  # slug -> market info
        self.last: dict[tuple, int] = {}
        self.open_usd, self.stop = 0.0, False
        self.client = self.start_balance = None
        if live:
            sys.path.insert(0, str(ROOT / "scripts"))
            import polymarket_live_test as lt
            self.lt = lt
            self.client = lt.client()
            self.start_balance = lt.balance(self.client)
            print(f"LIVE: balance ${self.start_balance:.2f}", flush=True)

    def refresh_markets(self) -> None:
        now = time.time()
        for slug in [s for s, m in self.markets.items() if m["t1"] < now]:
            del self.markets[slug]
        for prefix, step in SERIES:
            t0 = int(now) // step * step
            slug = f"{prefix}-{t0}"
            if slug in self.markets:
                continue
            try:
                d = get(f"https://gamma-api.polymarket.com/markets?slug={slug}")
            except OSError:
                continue
            if d:
                up, down = json.loads(d[0]["clobTokenIds"])
                self.markets[slug] = {"slug": slug, "cid": d[0]["conditionId"], "up": up, "down": down,
                                      "t0": t0, "t1": t0 + step, "s_open": self.btc.before(t0 * 1000)}

    def tokens(self) -> list[str]:
        return sorted(tok for m in self.markets.values() for tok in (m["up"], m["down"]))

    async def binance(self) -> None:
        while not self.stop:
            try:
                async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@aggTrade") as ws:
                    async for msg in ws:
                        e = json.loads(msg)
                        self.btc.add(e["T"], float(e["p"]))
            except (OSError, websockets.WebSocketException) as exc:
                print("binance reconnect:", exc, flush=True)
                await asyncio.sleep(1)

    async def polymarket(self) -> None:
        while not self.stop:
            toks = self.tokens()
            try:
                async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market", ping_interval=10) as ws:
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    while self.tokens() == toks and not self.stop:
                        try:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), 1))
                        except asyncio.TimeoutError:
                            continue
                        for e in msg if isinstance(msg, list) else [msg]:
                            if "bids" in e and "asks" in e:
                                self.books[e["asset_id"]].snapshot(e)
                            for c in e.get("price_changes", []):
                                self.books[c["asset_id"]].change(c)
            except (OSError, websockets.WebSocketException) as exc:
                print("polymarket reconnect:", exc, flush=True)
                await asyncio.sleep(1)

    async def buy(self, token: str, price: float) -> dict:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
        c = self.client

        def go():
            order = c.create_order(OrderArgs(token_id=token, price=price, size=SIZE, side=Side.BUY),
                                   PartialCreateOrderOptions(tick_size="0.01"))
            return c.post_order(order, OrderType.FAK)
        try:
            return await asyncio.to_thread(go)
        except Exception as exc:  # the exchange's rejection comes back as an exception
            return {"error": str(exc)}

    async def tick(self, s: int) -> None:
        for m in list(self.markets.values()):
            if not (m["t0"] + 5 <= s <= m["t1"] - 3):
                continue
            left = m["t1"] - s
            sigma = self.btc.sigma(s)
            px = self.btc.before(s * 1000)
            fair = phi(math.log(px / m["s_open"]) / (sigma * math.sqrt(left))) if sigma > 0 else 0.5
            for side, tok, p_win in (("up", m["up"], fair), ("down", m["down"], 1 - fair)):
                book = self.books.get(tok)
                ba = book.best_ask() if book else None
                if not ba or time.time() - book.updated > 30:
                    continue
                price, size = ba
                edge = p_win - price
                key = (m["slug"], side)
                if edge < self.threshold or size < SIZE or not 0.02 <= price <= 0.98 or s - self.last.get(key, -1e9) < 30:
                    continue
                self.last[key] = s
                ev = {"event": "signal", "slug": m["slug"], "cid": m["cid"], "side": side, "token": tok, "s": s,
                      "left": left, "fair": fair, "price": price, "shown": size, "edge": edge,
                      "fee": FEE_AT_HALF * price * (1 - price) / 0.25, "btc": px, "s_open": m["s_open"]}
                if self.live and not self.stop:
                    if self.open_usd + price * SIZE > MAX_OPEN_USD:
                        ev["skipped"] = "open cap"
                    else:
                        t = time.perf_counter()
                        resp = await self.buy(tok, price)
                        ev.update(order=resp, order_ms=(time.perf_counter() - t) * 1000)
                        filled = float(resp.get("takingAmount") or 0) if resp.get("success") else 0.0
                        ev["filled"] = filled
                        self.open_usd += filled * price
                log(ev)
                print(f"{time.strftime('%H:%M:%S')} {m['slug']} {side} fair {p_win:.2f} ask {price:.2f} edge {edge:+.2f}"
                      + (f" -> {ev.get('filled', ev.get('skipped'))}" if self.live else ""), flush=True)

    async def clock(self, minutes: float) -> None:
        end = time.time() + minutes * 60
        next_refresh = 0.0
        while time.time() < end and not self.stop:
            s = int(time.time()) + 1
            await asyncio.sleep(max(0.0, s + 0.005 - time.time()))
            if time.time() >= next_refresh:
                await asyncio.to_thread(self.refresh_markets)
                next_refresh = time.time() + 5
                self.btc.trim()
            await self.tick(s)
            if self.live and s % 60 == 0:
                bal = await asyncio.to_thread(self.lt.balance, self.client)
                open_now = {m["slug"] for m in self.markets.values()}
                self.open_usd = sum(e["filled"] * e["price"] for e in read_log() if e.get("filled") and e["slug"] in open_now)
                if bal < self.start_balance - MAX_LOSS_USD:
                    print(f"loss cap hit: balance ${bal:.2f}", flush=True)
                    self.stop = True
        self.stop = True


def read_log() -> list[dict]:
    return [json.loads(x) for x in open(LOG)] if LOG.exists() else []


def report() -> None:
    evs = [e for e in read_log() if e.get("event") == "signal"]
    outcome = {}
    for slug in sorted({e["slug"] for e in evs}):
        d = get(f"https://gamma-api.polymarket.com/markets?slug={slug}")
        if d and d[0].get("closed"):
            outcome[slug] = json.loads(d[0]["outcomePrices"])[0] == "1"
    rows = defaultdict(list)
    for e in evs:
        if e["slug"] not in outcome:
            continue
        win = outcome[e["slug"]] == (e["side"] == "up")
        pnl = (1.0 if win else 0.0) - e["price"] - e["fee"]
        rows["paper (every signal at the ask seen)"].append((e["slug"], pnl * 100))
        if e.get("filled"):
            rows["live fills"].append((e["slug"], pnl * 100))
    print(f"{len(evs)} signals, {len(outcome)} settled markets")
    for name, xs in rows.items():
        per = defaultdict(list)
        for slug, v in xs:
            per[slug].append(v)
        mm = np.array([np.mean(v) for v in per.values()])
        v = np.array([x[1] for x in xs])
        print(f"{name}: n={len(v)} in {len(mm)} markets, {v.mean():+.2f}¢/share "
              f"(market-averaged {mm.mean():+.2f}¢ ± {mm.std() / np.sqrt(len(mm)):.2f}, won {np.mean(mm > 0):.0%})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("run", "report"))
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--threshold", type=float, default=0.10)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    if args.cmd == "report":
        return report()
    LOG.parent.mkdir(exist_ok=True)
    bot = Bot(args.threshold, args.live)
    log({"event": "start", "live": args.live, "threshold": args.threshold})

    async def run():
        feeds = [asyncio.create_task(bot.binance()), asyncio.create_task(bot.polymarket())]
        await bot.clock(args.minutes)
        for t in feeds:
            t.cancel()
    try:
        asyncio.run(run())
    finally:
        if bot.client:
            bot.client.cancel_all()


if __name__ == "__main__":
    main()
