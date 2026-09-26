"""Record what a live Bitcoin up/down bot sees, for backtests under the current settlement rules.

    uv run --no-project --with websockets python scripts/polymarket_btc_record.py [--hours 3]

Since mid-2026 the 5- and 15-minute "Bitcoin Up or Down" markets settle on Chainlink's BTC/USD TWAP over the window
instead of the end price, so the June backtest (scripts/polymarket_btc_lag.py) no longer describes them. This writes,
with the local receive time t (ms) on every row, to data/btc_live/<start>.jsonl:
  {"k": "m", ...}                     each 5m/15m market as it opens (slug, condition id, Up/Down tokens, start, end)
  {"k": "cl", "t", "ts", "v"}         Chainlink BTC/USD from Polymarket's live-data feed (ts = Chainlink's own time)
  {"k": "bn", "t", "T", "p"}          Binance BTCUSDT trades (T = Binance time), one per 100 ms at most
  {"k": "bk", "t", "a", "b", "bs", "x", "xs"}  best bid, its size, best ask, its size of an outcome token when they change
"""
import argparse
import asyncio
import json
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "btc_live"
SERIES = (("btc-updown-5m", 300), ("btc-updown-15m", 900))


def now_ms() -> int:
    return int(time.time() * 1000)


class Recorder:
    def __init__(self, path: Path):
        self.f = open(path, "a", buffering=1 << 16)
        self.markets, self.books, self.best = {}, {}, {}
        self.last_bn = 0

    def write(self, row: dict) -> None:
        self.f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def refresh(self) -> None:
        t = time.time()
        for slug in [s for s, m in self.markets.items() if m["end"] < t - 60]:
            del self.markets[slug]
        for prefix, step in SERIES:
            for t0 in (int(t) // step * step, int(t) // step * step + step):  # the running market and the next one
                slug = f"{prefix}-{t0}"
                if slug in self.markets:
                    continue
                try:
                    req = urllib.request.Request(f"https://gamma-api.polymarket.com/markets?slug={slug}", headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        d = json.load(r)
                except OSError:
                    continue
                if d:
                    up, down = json.loads(d[0]["clobTokenIds"])
                    self.markets[slug] = m = {"slug": slug, "cid": d[0]["conditionId"], "up": up, "down": down,
                                              "start": t0, "end": t0 + step}
                    self.write({"k": "m", "t": now_ms(), **m})

    def tokens(self) -> list[str]:
        return sorted(x for m in self.markets.values() for x in (m["up"], m["down"]))

    def book_event(self, tok: str) -> None:
        bids, asks = self.books[tok]
        b = max(bids) if bids else None
        x = min(asks) if asks else None
        state = (b, bids.get(b), x, asks.get(x))
        if state != self.best.get(tok):
            self.best[tok] = state
            self.write({"k": "bk", "t": now_ms(), "a": tok[-8:], "b": b, "bs": state[1], "x": x, "xs": state[3]})

    async def polymarket(self) -> None:
        while True:
            toks = self.tokens()
            try:
                async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market", ping_interval=10) as ws:
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    while self.tokens() == toks:
                        try:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), 1))
                        except asyncio.TimeoutError:
                            continue
                        for e in msg if isinstance(msg, list) else [msg]:
                            if "bids" in e and "asks" in e:
                                self.books[e["asset_id"]] = ({float(x["price"]): float(x["size"]) for x in e["bids"]},
                                                             {float(x["price"]): float(x["size"]) for x in e["asks"]})
                                self.book_event(e["asset_id"])
                            for c in e.get("price_changes", []):
                                if c["asset_id"] not in self.books:
                                    continue
                                book = self.books[c["asset_id"]][0 if c["side"] == "BUY" else 1]
                                p, s = float(c["price"]), float(c["size"])
                                if s:
                                    book[p] = s
                                else:
                                    book.pop(p, None)
                                self.book_event(c["asset_id"])
            except (OSError, websockets.WebSocketException, json.JSONDecodeError) as exc:
                print("polymarket reconnect:", exc, flush=True)
                await asyncio.sleep(1)

    async def chainlink(self) -> None:
        while True:
            try:
                async with websockets.connect("wss://ws-live-data.polymarket.com") as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                        {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))
                    while True:
                        try:
                            m = await asyncio.wait_for(ws.recv(), 4)
                        except asyncio.TimeoutError:
                            await ws.send("PING")
                            continue
                        try:
                            p = json.loads(m).get("payload", {})
                        except ValueError:
                            continue
                        if p.get("symbol") == "btc/usd":
                            self.write({"k": "cl", "t": now_ms(), "ts": p["timestamp"], "v": p["value"]})
            except (OSError, websockets.WebSocketException) as exc:
                print("chainlink reconnect:", exc, flush=True)
                await asyncio.sleep(1)

    async def binance(self) -> None:
        while True:
            try:
                async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@aggTrade") as ws:
                    async for msg in ws:
                        e = json.loads(msg)
                        t = now_ms()
                        if t - self.last_bn >= 100:
                            self.last_bn = t
                            self.write({"k": "bn", "t": t, "T": e["T"], "p": float(e["p"])})
            except (OSError, websockets.WebSocketException) as exc:
                print("binance reconnect:", exc, flush=True)
                await asyncio.sleep(1)

    async def markets_loop(self, hours: float) -> None:
        end = time.time() + hours * 3600
        while time.time() < end:
            await asyncio.to_thread(self.refresh)
            self.f.flush()
            await asyncio.sleep(5)


async def main(hours: float) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rec = Recorder(OUT / f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}.jsonl")
    rec.refresh()
    feeds = [asyncio.create_task(x) for x in (rec.polymarket(), rec.chainlink(), rec.binance())]
    await rec.markets_loop(hours)
    for x in feeds:
        x.cancel()
    rec.f.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=3)
    asyncio.run(main(ap.parse_args().hours))
