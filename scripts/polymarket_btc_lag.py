"""Are Polymarket's short "Bitcoin Up or Down" markets slow to follow Binance? (latency-arbitrage backtest)

    .venv/bin/python scripts/polymarket_btc_lag.py data/marketlens/bitcoin-5m-15m-hourly-2026-06-15 [--latency 0.2]

Data: the marketlens sample day (every order-book change and trade of 408 markets, plus the Binance BTC trade tape).
Each second of a market's life: fair P(up) = Phi(ln(S_t / S_open) / (sigma * sqrt(seconds left))), sigma = realised
per-second volatility of the previous 15 minutes. We act on the book as it stands `latency` seconds later: buy Up at the
best ask if fair - ask >= threshold, buy Down at 1 - best bid if best bid - fair >= threshold (at least 5 shares shown),
at most one trade per side per 30 seconds, held to settlement. P&L per share = outcome - price - taker fee.
"""
import argparse
import bisect
import csv
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def book_series(path: Path) -> tuple[list[int], list[tuple]]:
    """Times and (best bid, bid size, best ask, ask size) after every book event."""
    rows = pq.read_table(path, columns=["event_type", "t", "price", "size", "side", "bids", "asks"]).to_pylist()
    rows.sort(key=lambda r: r["t"])
    bids, asks, ts, states = {}, {}, [], []
    for r in rows:
        if r["event_type"] == "snapshot":
            bids = {float(x["price"]): float(x["size"]) for x in json.loads(r["bids"])}
            asks = {float(x["price"]): float(x["size"]) for x in json.loads(r["asks"])}
        elif r["event_type"] == "delta":
            book = bids if r["side"] == "BUY" else asks
            if r["size"]:
                book[r["price"]] = r["size"]
            else:
                book.pop(r["price"], None)
        else:
            continue
        if bids and asks:
            bb, ba = max(bids), min(asks)
            ts.append(r["t"])
            states.append((bb, bids[bb], ba, asks[ba]))
    return ts, states


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class Reference:
    """Binance BTC price per second (last trade strictly before the second starts) and trailing volatility."""

    def __init__(self, t_ms: np.ndarray, price: np.ndarray):
        self.sec = np.arange(t_ms[0] // 1000, t_ms[-1] // 1000 + 1)
        self.px = price[np.maximum(0, np.searchsorted(t_ms, self.sec * 1000, side="left") - 1)]
        self.lr = np.diff(np.log(self.px), prepend=np.log(self.px[0]))

    def at(self, t_ms: int) -> float:
        return self.px[min(len(self.px) - 1, max(0, t_ms // 1000 - self.sec[0]))]


def evaluate_market(ref: Reference, ts: list[int], states: list[tuple], t0: int, t1: int, up: bool,
                    latency: float, fee_at_half: float, tag: dict) -> dict[float, list[dict]]:
    """Signals and settled P&L for one market; ts/states = time-ordered (best bid, bid size, best ask, ask size)."""
    out, last = defaultdict(list), {}
    s_open = ref.at(t0 - 1)
    for s in range(t0 // 1000 + 5, t1 // 1000 - 3):
        i = s - ref.sec[0]
        if i < 900 or i >= len(ref.px):
            continue
        sigma = ref.lr[i - 900:i].std()
        left = t1 / 1000 - s
        fair = phi(math.log(ref.px[i] / s_open) / (sigma * math.sqrt(left))) if sigma > 0 else 0.5
        j = bisect.bisect_right(ts, int((s + latency) * 1000)) - 1  # the book when our order arrives
        if j < 0:
            continue
        bb, bsz, ba, asz = states[j]
        for thr in (0.03, 0.05, 0.10):
            for side, edge, price, size, win in (("up", fair - ba, ba, asz, up), ("down", bb - fair, 1 - bb, bsz, not up)):
                if edge >= thr and size >= 5 and s - last.get((thr, side), -1e9) >= 30 and 0.02 <= price <= 0.98:
                    last[(thr, side)] = s
                    fee = fee_at_half * price * (1 - price) / 0.25
                    out[thr].append({**tag, "pnl": (1.0 if win else 0.0) - price - fee, "edge": edge, "price": price,
                                     "left": left})
    return out


def report(trades: dict[float, list[dict]]) -> None:
    for thr, xs in sorted(trades.items()):
        v = np.array([x["pnl"] for x in xs]) * 100
        e = np.mean([x["edge"] for x in xs]) * 100
        per_m = defaultdict(list)
        for x in xs:
            per_m[x["market"]].append(x["pnl"] * 100)
        mm = np.array([np.mean(w) for w in per_m.values()])
        print(f"threshold {thr:.2f}: n={len(v)} trades in {len(mm)} markets, model edge {e:.1f}¢, realised {v.mean():+.2f}¢/share "
              f"(market-averaged {mm.mean():+.2f}¢ ± {mm.std() / np.sqrt(len(mm)):.2f}, markets won {np.mean(mm > 0):.0%})")
        for ser in sorted({x["series"] for x in xs}):
            ids = {x["market"] for x in xs if x["series"] == ser}
            wm = np.array([np.mean(per_m[k]) for k in ids])
            print(f"    {ser:24s} {len(wm):4d} markets  market-averaged {wm.mean():+.2f}¢ ± {wm.std() / np.sqrt(len(wm)):.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--latency", type=float, default=0.2)
    ap.add_argument("--fee", type=float, default=0.0, help="taker fee per share at p=0.5, scaled by p*(1-p)/0.25")
    args = ap.parse_args()
    folder = Path(args.folder)
    rows = pq.read_table(folder / "reference-BTC.parquet").to_pylist()
    ref = Reference(np.array([r["timestamp"] for r in rows]), np.array([float(r["price"]) for r in rows]))
    trades = defaultdict(list)
    for m in csv.DictReader(open(folder / "markets.csv")):
        if not (folder / m["file"]).exists():
            continue
        ts, states = book_series(folder / m["file"])
        if ts:
            for thr, xs in evaluate_market(ref, ts, states, ms(m["open_time"]), ms(m["close_time"]),
                                           m["winning_outcome"] == "Up", args.latency, args.fee,
                                           {"series": m["series"], "market": m["market_id"]}).items():
                trades[thr] += xs
    report(trades)


if __name__ == "__main__":
    main()
