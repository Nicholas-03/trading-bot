# Trading Execution Policy

This note captures the production rule agreed after the CSX missed-entry investigation on 2026-05-14.

## Non-Negotiable Data Boundary

- Alpaca is the source for all stock market data used by the bot.
- Use Alpaca for entry snapshots, ask/last prices, session open, 1-minute entry-confirmation bars, and live prices used by the position monitor.
- Do not use Tradier quotes or Tradier time-and-sales as a fallback for trading decisions.
- Tradier is the broker only: order submission, order status, positions, balances, account history, and realized gain/loss.

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

## What Not To Reintroduce

- Do not submit buy entries as Tradier OTOCO orders.
- Do not calculate entry prices from Tradier market data.
- Do not fall back from Alpaca bars/snapshots to Tradier bars/quotes.
- Do not place quote-based TP/SL legs before the entry fill is confirmed.

## Why

Tradier sandbox advanced OTOCO entries can remain open and later cancel with `exec_quantity=0` even when an external live market source suggests the limit should be marketable. CSX order `29882899` showed this failure mode: the parent OTOCO was accepted, the buy leg never filled, and the bot canceled after the confirmation timeout.

Separating entry from bracket placement gives the bot a clear sequence:

- first prove the entry filled,
- then protect the real position,
- then record and monitor the trade from the actual fill price.

This avoids ambiguous advanced-order failures and keeps all price decisions on one trusted market-data provider.
