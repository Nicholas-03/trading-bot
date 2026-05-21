# Trading Execution Policy

This note captures the production rule agreed after the CSX missed-entry investigation on 2026-05-14.

## Non-Negotiable Data Boundary

- Alpaca is the source for all stock market data used by the bot.
- Use Alpaca for entry snapshots, ask/last prices, session open, 1-minute entry-confirmation bars, and live prices used by the position monitor.
- Do not use Tradier quotes or Tradier time-and-sales as a fallback for trading decisions.
- Tradier is the broker only: order submission, order status, positions, balances, and account history.
- Do not use Tradier gain/loss for bot performance reporting. It is broker/tax-lot accounting and can include old lots for the same symbol; EOD and weekly Telegram P&L should come from the analytics DB's bot round-trip records.

## Long Entry Flow

Long buys must use a fill-first bracket flow:

1. Fetch the Alpaca snapshot.
2. Use the Alpaca ask as the entry reference price; fall back inside the Alpaca snapshot parser only when ask is unavailable.
3. Calculate the DAY limit buy with `MAX_SLIPPAGE_PCT`.
4. Submit a plain Tradier limit buy with `submit_order(..., "buy", qty, entry_limit)`.
5. Poll Tradier order status until the entry is filled or terminal.
6. If the entry does not fill, cancel it, roll back in-memory state, and send an `ORDER skipped` notification.
7. If the entry fills, use the actual fill price to submit the protective Tradier OCO bracket with `submit_oco_order`.
8. Store the protective OCO order ID in analytics as the trade bracket ID.

## Risk Gates

- `MIN_CONFIDENCE_FLOOR` defaults to `0.80`; lower `MIN_CONFIDENCE` values from an old `.env` are raised to the floor.
- `MIN_TRADE_PRICE_FLOOR` defaults to `$20`; lower `MIN_TRADE_PRICE` values are raised to the floor so low-price symbols are blocked.
- News must pass `REQUIRE_HARD_CATALYST_NEWS=true` before the LLM is called. Valid hard catalysts are quantified earnings/guidance surprises, FDA/EMA or clinical endpoint decisions, signed M&A with value, major contracts/orders with value, or material legal/regulatory decisions with financial amounts.
- Analyst upgrades/downgrades, price-target changes, watchlists, vague commentary, and soft partnerships without material financial impact are skipped before the LLM.
- Entries require Alpaca bid/ask spread, average 1-minute volume, average 1-minute dollar volume, and post-news directional confirmation. Missing spread or volume data is a skip, not a fallback.
- Short entries are limited to the built-in liquid large-cap/ETF allowlist unless `SHORT_LIQUID_SYMBOLS` overrides it. Shorts use capped limit entries, not uncapped market entries.
- `MAX_HOLD_HOURS_CAP` defaults to `1`; longer requested holds are capped to one hour. `CLOSE_BEFORE_MARKET_CLOSE_MINUTES` defaults to `10`, so live bot positions are flattened before the regular-session close.

## What Not To Reintroduce

- Do not submit buy entries as Tradier OTOCO orders.
- Do not calculate entry prices from Tradier market data.
- Do not fall back from Alpaca bars/snapshots to Tradier bars/quotes.
- Do not place quote-based TP/SL legs before the entry fill is confirmed.
- Do not relax catalyst, spread, volume, price, or short-liquidity gates without first checking analytics DB performance after the stricter policy.

## Why

Tradier sandbox advanced OTOCO entries can remain open and later cancel with `exec_quantity=0` even when an external live market source suggests the limit should be marketable. CSX order `29882899` showed this failure mode: the parent OTOCO was accepted, the buy leg never filled, and the bot canceled after the confirmation timeout.

Separating entry from bracket placement gives the bot a clear sequence:

- first prove the entry filled,
- then protect the real position,
- then record and monitor the trade from the actual fill price.

This avoids ambiguous advanced-order failures and keeps all price decisions on one trusted market-data provider.
