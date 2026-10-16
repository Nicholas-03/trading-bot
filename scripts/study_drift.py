"""Post-news drift study: variations of "short stocks that fall >= 10% vs SPY in the hour after news, hold 5 days".

    .venv/bin/python scripts/study_drift.py [section ...]     sections: base grid entry stop up vol news price runup day0 borrow book

Events from scripts/study_drift_events.py (first news per stock and day, |first hour| >= 5%, daily OHLC around it).
"in-sample" = pairs passing the bot's gates (price >= $20, liquid), where the rule was found; "hold-out" = the rest.
Returns are for the short side in % unless noted, SPY-hedged (stock return minus SPY's from the entry day's close),
after COST % round trip; t = mean / standard error with trades grouped by entry day.
"""
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
COST = 0.5
MIN_PRICE = 1.0


def clean(e, span=21):
    """False when the window from the news day on holds a split / adjustment problem: the adjusted-to-raw factor changes,
    or the raw price multiplies >= 4x in a day while volume collapses (an unadjusted reverse split)."""
    i0 = e["i0"]
    f = [a[3] / r[3] if a and r and r[3] else None for a, r in zip(e["adj"], e["raw"])]
    for k in range(i0, min(i0 + span, len(f) - 1)):
        if f[k] and f[k + 1] and abs(math.log(f[k + 1] / f[k])) > 0.05:
            return False
        a, b = e["raw"][k], e["raw"][k + 1]
        if a and b and (b[3] / a[3] >= 4 or b[3] / a[3] <= 0.25) and b[4] < 0.3 * a[4]:
            return False
    return True


ALL = [json.loads(l) for l in open(ROOT / "data" / "drift_events.jsonl")]
KEPT = [e for e in ALL if e["entry"] >= MIN_PRICE and clean(e)]
EV = [e for e in KEPT if not e.get("control")]
CTRL = [e for e in KEPT if e.get("control")]
del ALL
print(f"events kept: {len(EV)} moves >= 5% and {len(CTRL)} control news days (same stocks, first hour within 2%); "
      "entry >= $1, no split/adjustment problem in the window")
HEDGE = "spy"


def px(e, k, field="c"):
    """Adjusted price field of window day k (None if missing)."""
    b = e["adj"][k] if 0 <= k < len(e["adj"]) else None
    return None if b is None else b["ohlc".index(field)]


def last_close(e, k):
    """Close of day k, or the last close before it (a halted or delisted stock keeps its last price)."""
    for j in range(min(k, len(e["adj"]) - 1), e["i0"], -1):
        if e["adj"][j]:
            return e["adj"][j][3], j
    return None, None


def entry_price(e, how):
    i0 = e["i0"]
    if how == "close":
        return px(e, i0), i0
    if how == "open1":
        return px(e, i0 + 1, "o"), i0 + 1
    if how == "hour":  # end of the first hour after the news, in adjusted terms
        raw_c, adj_c = e["raw"][i0][3], e["adj"][i0][3]
        return e["entry"] * (1 + e["ret_1h"]) * adj_c / raw_c, i0
    if how == "close2":  # wait a day
        return px(e, i0 + 1), i0 + 1
    raise ValueError(how)


def short_trade(e, how="close", hold=5, stop=None, side=-1, borrow_apr=0.0):
    """P&L % of shorting (side=-1) or buying (side=+1) from `how` for `hold` trading days after the news day."""
    p0, k0 = entry_price(e, how)
    if not p0:
        return None
    k_exit = e["i0"] + hold
    if k_exit >= len(e["adj"]) or k_exit <= k0 and how != "hour":
        return None
    exit_p, k_used = None, k_exit
    if stop is not None:
        for k in range(k0 + 1, k_exit + 1):
            b = e["adj"][k]
            if not b:
                continue
            level = p0 * (1 - side * stop)
            if (side == -1 and b[1] >= level) or (side == 1 and b[2] <= level):
                exit_p = max(level, b[0]) if side == -1 else min(level, b[0])
                k_used = k
                break
    if exit_p is None:
        exit_p, k_used = last_close(e, k_exit)
        if exit_p is None:
            return None
    h = e[HEDGE] if HEDGE != "none" else e["spy"]
    spy0 = h[k0][3] if h[k0] else None
    spy1 = h[k_used][3] if h[k_used] else None
    if not spy0 or not spy1:
        return None
    raw = side * (exit_p / p0 - 1) * 100
    days_held = max(1, k_used - k0)
    borrow = borrow_apr * days_held / 252 * 100 if side == -1 else 0.0
    hedge = 0.0 if HEDGE == "none" else (spy1 / spy0 - 1) * 100
    return raw - side * hedge - COST - borrow


def stats(pairs, label="", show_years=False):
    """pairs: [(event, pnl)]; prints mean, median, day-clustered t, win rate."""
    pairs = [(e, x) for e, x in pairs if x is not None]
    if len(pairs) < 10:
        print(f"  {label:44s} n={len(pairs)}")
        return None
    v = np.array([x for _, x in pairs])
    by_day = defaultdict(list)
    for e, x in pairs:
        by_day[e["day"]].append(x)
    dm = np.array([np.mean(x) for x in by_day.values()])
    t = dm.mean() / (dm.std() / math.sqrt(len(dm))) if dm.std() > 0 else float("nan")
    s = (f"  {label:44s} n={len(v):5d} mean {v.mean():+6.2f} median {np.median(v):+6.2f} "
         f"clip±50 {np.clip(v, -50, 50).mean():+6.2f}  t {t:+5.1f}  win {np.mean(v > 0):.0%}")
    if show_years:
        yr = defaultdict(list)
        for e, x in pairs:
            yr[e["day"][:4]].append(x)
        s += " | " + " ".join(f"{y[2:]}:{np.mean(w):+.1f}" for y, w in sorted(yr.items()))
    print(s)
    return v.mean()


def drops(thr=-0.10, pop=None):
    return [e for e in EV if e["excess_1h"] <= thr and (pop is None or bool(e["tradable"]) == (pop == "in"))]


def pops(thr=-0.10):
    return (("in-sample", drops(thr, "in")), ("hold-out", drops(thr, "out")), ("pooled", drops(thr)))


def sec_base():
    print("\n## Base rule: first hour <= -10%, short at the close, 5 days")
    for name, es in pops():
        stats([(e, short_trade(e)) for e in es], name, True)
    print("  data checks: events with a missing bar in the 5 days:",
          sum(any(b is None for b in e["adj"][e["i0"]:e["i0"] + 6]) for e in drops()),
          "| raw vs adjusted close differ on the news day:",
          sum(abs(e["adj"][e["i0"]][3] / e["raw"][e["i0"]][3] - 1) > 0.02 for e in drops() if e["raw"][e["i0"]]))


def sec_grid():
    print("\n## Threshold x holding period (pooled, short at the close) - mean % (t)")
    holds = (1, 2, 3, 5, 10, 20)
    print("  " + " " * 10 + "".join(f"{h:>14d}d" for h in holds))
    for thr in (-0.05, -0.07, -0.10, -0.15, -0.20, -0.30):
        cells = []
        for h in holds:
            pairs = [(e, short_trade(e, hold=h)) for e in drops(thr)]
            pairs = [(e, x) for e, x in pairs if x is not None]
            v = np.array([x for _, x in pairs])
            by_day = defaultdict(list)
            for e, x in pairs:
                by_day[e["day"]].append(x)
            dm = np.array([np.mean(x) for x in by_day.values()])
            cells.append(f"{v.mean():+7.2f} ({dm.mean() / (dm.std() / math.sqrt(len(dm))):+4.1f})")
        print(f"  <= {thr:5.0%}  n={len(drops(thr)):5d} " + " ".join(cells))


def sec_entry():
    print("\n## Entry time (pooled, <= -10%, 5 days after the news day)")
    for how, name in (("hour", "right after the first hour"), ("close", "news day's close"), ("open1", "next day's open"),
                      ("close2", "next day's close (wait a day)")):
        stats([(e, short_trade(e, how)) for e in drops()], name, True)


def sec_stop():
    print("\n## Stop-loss on daily highs (pooled, <= -10%, short at close, 5 days)")
    for s in (None, 0.10, 0.20, 0.30, 0.50, 1.00):
        stats([(e, short_trade(e, stop=s)) for e in drops()], f"stop {'none' if s is None else f'{s:.0%}'}")
    print("  worst trades without a stop:", sorted(round(x, 1) for x in (short_trade(e) for e in drops()) if x is not None)[:8])


def sec_up():
    print("\n## Mirror: first hour >= +X% (short the pop / buy it), at the close")
    for thr in (0.05, 0.10, 0.20):
        es = [e for e in EV if e["excess_1h"] >= thr]
        for h in (1, 5, 20):
            stats([(e, short_trade(e, hold=h)) for e in es], f">= +{thr:.0%} short, {h}d", h == 5)
    print("  buy the dip instead (long after <= -10%, 5 days):")
    stats([(e, short_trade(e, side=1)) for e in drops()], "long <= -10%, 5d")


def prior_vol(e):
    c = [b[3] for b in e["adj"][max(0, e["i0"] - 21):e["i0"]] if b]
    if len(c) < 10:
        return None
    return float(np.std(np.diff(np.log(c))))


def sec_vol():
    print("\n## Drop measured in the stock's own daily volatility (first hour / 20-day daily sd), pooled")
    es = [(e, prior_vol(e)) for e in EV if e["excess_1h"] < 0]
    for lo, hi in ((-99, -5), (-5, -3), (-3, -2), (-2, -1), (-1, 0)):
        sel = [e for e, v in es if v and lo < e["excess_1h"] / v <= hi]
        stats([(e, short_trade(e)) for e in sel], f"z in ({lo}, {hi}]")
    print("  within <= -10%: calm vs volatile stocks (20-day daily sd)")
    d = [(e, prior_vol(e)) for e in drops()]
    for lo, hi in ((0, 0.03), (0.03, 0.06), (0.06, 0.12), (0.12, 9)):
        stats([(e, short_trade(e)) for e, v in d if v is not None and lo <= v < hi], f"daily sd {lo:.0%}-{hi:.0%}")


CATS = (
    ("offering / dilution", r"offering|priced|pricing|private placement|registered direct|at-the-market|warrant|dilut|s-1|shelf"),
    ("earnings / guidance", r"earnings|results|q[1-4]\b|eps|revenue|guidance|outlook|sales"),
    ("trial / FDA", r"fda|trial|phase|data|study|crl|approval|endpoint"),
    ("analyst action", r"downgrade|price target|maintains|lowers|cuts|analyst"),
    ("halt / resume", r"halted|resume|circuit breaker"),
    ("lists of movers", r"stocks moving|movers|mid-day|intraday session|big stocks"),
    ("deal / merger / SPAC", r"merger|acquisition|acquire|business combination|spac|deal"),
    ("legal / probe / delisting", r"investigation|lawsuit|class action|sec |subpoena|delist|nasdaq notice|fraud"),
)


def category(h):
    h = h.lower()
    return next((name for name, p in CATS if re.search(p, h)), "other")


def sec_news():
    print("\n## By headline type (pooled, <= -10%, short at close, 5 days)")
    d = drops()
    for name in [c for c, _ in CATS] + ["other"]:
        stats([(e, short_trade(e)) for e in d if category(e["headline"]) == name], name)
    print("  single-ticker news only:")
    stats([(e, short_trade(e)) for e in d if e["n_tickers"] == 1], "n_tickers == 1")
    stats([(e, short_trade(e)) for e in d if e["n_tickers"] > 1], "n_tickers > 1")


def dollar_vol(e):
    b = [x for x in e["raw"][max(0, e["i0"] - 20):e["i0"]] if x]
    return float(np.mean([x[3] * x[4] for x in b])) if b else None


def sec_price():
    print("\n## By entry price and liquidity (pooled, <= -10%, short at close, 5 days)")
    d = drops()
    for lo, hi in ((0, 1), (1, 2), (2, 5), (5, 10), (10, 20), (20, 50), (50, 1e9)):
        stats([(e, short_trade(e)) for e in d if lo <= e["entry"] < hi], f"price ${lo}-{hi:g}")
    for lo, hi in ((0, 1e6), (1e6, 5e6), (5e6, 20e6), (20e6, 100e6), (100e6, 1e13)):
        stats([(e, short_trade(e)) for e in d if (dv := dollar_vol(e)) is not None and lo <= dv < hi],
              f"20-day avg $ volume {lo / 1e6:g}-{hi / 1e6:g}M")


def sec_runup():
    print("\n## Pre-news run-up (close before the news day vs 20 days earlier), pooled <= -10%")
    d = drops()
    for lo, hi in ((-9, -0.3), (-0.3, -0.1), (-0.1, 0.1), (0.1, 0.5), (0.5, 99)):
        sel = []
        for e in d:
            a, b = e["adj"][e["i0"] - 1] if e["i0"] >= 1 else None, e["adj"][max(0, e["i0"] - 21)]
            if a and b and b[3] and lo <= a[3] / b[3] - 1 < hi:
                sel.append(e)
        stats([(e, short_trade(e)) for e in sel], f"20-day run-up {lo:+.0%}..{hi:+.0%}")


def sec_day0():
    print("\n## What the stock did between the end of the first hour and the close (pooled, <= -10%)")
    d = drops()
    for lo, hi in ((-9, -0.05), (-0.05, 0), (0, 0.05), (0.05, 9)):
        sel = []
        for e in d:
            ph, _ = entry_price(e, "hour")
            c = px(e, e["i0"])
            if ph and c and lo <= c / ph - 1 < hi:
                sel.append(e)
        stats([(e, short_trade(e)) for e in sel], f"rest of day {lo:+.0%}..{hi:+.0%}")
    print("  gap of the news day's open vs previous close:")
    for lo, hi in ((-9, -0.1), (-0.1, 0), (0, 0.1), (0.1, 9)):
        sel = [e for e in d if e["i0"] >= 1 and e["adj"][e["i0"] - 1] and e["adj"][e["i0"] - 1][3] and lo <= e["adj"][e["i0"]][0] / e["adj"][e["i0"] - 1][3] - 1 < hi]
        stats([(e, short_trade(e)) for e in sel], f"open gap {lo:+.0%}..{hi:+.0%}")


def sec_borrow():
    print("\n## Borrow fee sensitivity (pooled, <= -10%, short at close, 5 days; fee charged per calendar-ish day held)")
    for apr in (0.0, 0.2, 0.5, 1.0, 3.0):
        stats([(e, short_trade(e, borrow_apr=apr)) for e in drops()], f"borrow {apr:.0%}/yr")


def sec_book(capital=100_000, per_trade=5_000, thr=-0.10, hold=5, stop=0.5, pop=None):
    """Daily mark-to-market of a book that shorts `per_trade` $ per signal at the close (SPY-hedged), max capital/per_trade open."""
    print(f"\n## Book simulation: ${capital:,} capital, ${per_trade:,} per short, hold {hold}d, stop {stop}, {pop or 'pooled'}")
    ev = sorted(drops(thr, pop), key=lambda e: e["day"])
    daily = defaultdict(float)
    open_until: list[str] = []
    taken = skipped = 0
    for e in ev:
        open_until = [d for d in open_until if d > e["day"]]
        if len(open_until) >= capital // per_trade:
            skipped += 1
            continue
        p0 = px(e, e["i0"])
        k_exit = e["i0"] + hold
        if not p0 or k_exit >= len(e["adj"]):
            continue
        taken += 1
        prev_p, prev_s = p0, e["spy"][e["i0"]][3]
        for k in range(e["i0"] + 1, k_exit + 1):
            b, s = e["adj"][k], e["spy"][k]
            if not s:
                continue
            p = b[3] if b else prev_p
            stopped = stop is not None and b and b[1] >= p0 * (1 + stop)
            if stopped:
                p = max(p0 * (1 + stop), b[0])
            daily[e["dates"][k]] += per_trade * (-(p / prev_p - 1) + (s[3] / prev_s - 1)) * (prev_p / p0)
            prev_p, prev_s = p, s[3]
            if stopped:
                break
        daily[e["dates"][e["i0"] + 1]] -= per_trade * COST / 100
        open_until.append(e["dates"][min(k_exit, len(e["dates"]) - 1)])
    days = sorted(daily)
    pnl = np.array([daily[d] for d in days])
    eq = capital + np.cumsum(pnl)
    years = (np.datetime64(days[-1]) - np.datetime64(days[0])).astype(int) / 365.25
    ret = pnl / capital
    dd = (eq / np.maximum.accumulate(eq) - 1).min()
    by_year = defaultdict(float)
    for d, x in zip(days, pnl):
        by_year[d[:4]] += x
    print(f"  trades {taken} (skipped for capital {skipped}); total P&L ${pnl.sum():,.0f} over {years:.1f} years = "
          f"{pnl.sum() / capital / years:+.1%}/yr on capital; Sharpe {ret.mean() / ret.std() * math.sqrt(252):.2f} "
          f"(trading days with a position); max drawdown {dd:.1%}")
    print("  by year: " + " ".join(f"{y}: ${v:,.0f}" for y, v in sorted(by_year.items())))


def price_bucket(p):
    return next(i for i, hi in enumerate((2, 5, 10, 20, 50, 1e12)) if p < hi)


def sec_control():
    """Is it the drop, or just shorting these small volatile stocks? Compare with the same stocks' quiet news days."""
    global HEDGE
    for hedge in ("spy", "iwm", "none"):
        HEDGE = hedge
        print(f"\n## Drop vs control, short at the close, hedge {hedge.upper()}")
        for hold in (1, 5, 20):
            ev = [(e, short_trade(e, hold=hold)) for e in drops()]
            ct = [(e, short_trade(e, hold=hold)) for e in CTRL]
            ev, ct = [(e, x) for e, x in ev if x is not None], [(e, x) for e, x in ct if x is not None]
            cell = defaultdict(list)
            for e, x in ct:
                cell[(e["day"][:4], price_bucket(e["entry"]))].append(x)
            base = {k: (np.mean(v), len(v)) for k, v in cell.items()}
            ab = [(e, x - base[(e["day"][:4], price_bucket(e["entry"]))][0]) for e, x in ev
                  if base.get((e["day"][:4], price_bucket(e["entry"])), (0, 0))[1] >= 20]
            stats(ct, f"control days, {hold}d")
            stats(ev, f"<= -10% drop days, {hold}d")
            stats(ab, f"drop minus control (same year & price), {hold}d", hold == 5)
        ups = [e for e in EV if e["excess_1h"] >= 0.10]
        stats([(e, short_trade(e)) for e in ups], ">= +10% pop days (short), 5d")
    HEDGE = "spy"


SECTIONS = {"control": sec_control, "base": sec_base, "grid": sec_grid, "entry": sec_entry, "stop": sec_stop, "up": sec_up, "vol": sec_vol,
            "news": sec_news, "price": sec_price, "runup": sec_runup, "day0": sec_day0, "borrow": sec_borrow,
            "book": lambda: (sec_book(), sec_book(pop="in"), sec_book(per_trade=2_000, stop=0.3))}

if __name__ == "__main__":
    for name in sys.argv[1:] or SECTIONS:
        SECTIONS[name]()
