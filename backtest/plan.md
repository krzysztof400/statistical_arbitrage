# Backtester Implementation Plan

Audience: a coding agent implementing this repository from scratch. Read the whole document before writing code. Where this plan and the official Bybit V5 documentation (https://bybit-exchange.github.io/docs/v5/intro) disagree, the documentation wins; report the discrepancy.

## 0. Context and goal

Bachelor's thesis project: statistical arbitrage (pairs trading / cointegration) on Bybit USDT linear perpetuals using L2 order book data. The backtester is a standalone Python application that simulates Bybit as faithfully as practical, so that:

1. strategy research (parameter sweeps, cost and latency sensitivity) runs fast and deterministically, and
2. the real trading engine, which is a separate program and may be written in C++ or Python, can connect to the simulator through the same Bybit-style interface it will later use against the live exchange.

The system must NOT assume colocation. Network latency is a configurable model, and the results must be reproducible under any latency preset.

Non-goals: sub-millisecond fidelity, cross-exchange arbitrage, spot/inverse/options, GUI.

## 1. Non-negotiable principles

1. **Determinism.** Same data + same config + same seed gives byte-identical outputs. All randomness comes from explicitly seeded generators owned by the run.
2. **Exchange-time axis is `cts`.** Bybit messages carry `cts` (matching-engine timestamp). Simulation time is exchange time. The local receive time recorded by the recorder (`recv_ns`) is NOT used as the time axis, because it embeds the recorder's own network position. `recv_ns - cts` is used only to calibrate latency distributions (section 5).
3. **No look-ahead.** The strategy only sees events delivered at or before the current simulated time, after modeled latency. The API must make it structurally impossible to read future data, not merely discouraged.
4. **Fill realism is the main threat to validity.** Fill models are explicit, selectable by config, and every result report states which one produced it.
5. **Verify, do not assume.** Bybit field names, fee rules, rate limits, error codes, funding schedule: take them from official docs or config files. Never hardcode a number you are not sure of; put it in config with a TODO and a doc link.
6. **One core, two frontends.** All matching, accounting and latency logic lives in the core library. Research mode and server mode are thin layers over it.

## 2. Architecture

```
data/raw/*.jsonl.gz  ->  reader/parser  ->  normalized cache (columnar)
                                              |
                                        simulation core
                  (virtual clock, event queue, books, matching, fills, accounting, latency)
                                   /                              \
              Mode A: in-process research runner      Mode B: Bybit-compatible server
              (Python strategy API, fast, sweeps)     (WS + REST; realtime or lockstep)
```

Mode A is the source of all thesis results. Mode B exists to test the real engine and its exchange adapter end to end; it must agree with Mode A (section 7, parity test).

## 3. Input data

Produced by the recorder: files `bybit_linear_YYYYMMDD_HH.jsonl.gz`, each line `<recv_ns>\t<raw json>`. A file may consist of several concatenated gzip members (recorder restarted within the hour). Lines may be:

- data messages for topics `orderbook.50.<SYMBOL>` and `publicTrade.<SYMBOL>` (and, once added to the recorder, `tickers.<SYMBOL>` for funding rate and mark price),
- subscribe acks and pong frames (ignore, but do not crash),
- markers `{"_meta":"connected"}` and `{"_meta":"disconnected"}` written by the recorder.

### Parsing rules (verify against docs)

- Orderbook message: `topic`, `type` (`snapshot`/`delta`), `ts`, `cts`, `data` with `s`, `b`, `a` (lists of `[price, size]` strings), `u`, `seq`.
- `type == snapshot` replaces the book. `u == 1` also means snapshot/reset regardless of `type`.
- Delta: size `0` deletes the level, unknown price inserts, known price updates size.
- Continuity: per symbol, a delta must have `u == previous_u + 1`. On violation, mark the book INVALID, log a gap event, and ignore updates until the next snapshot. A `disconnected` marker also invalidates all books; `connected` means "wait for fresh snapshots".
- While a book is INVALID the simulator must not fill against it and must report total invalid time per symbol.
- Trades: payload is a list; use `T` (trade time), `S` (taker side), `p`, `v`, `i` (id).
- Convert prices and sizes to integers (ticks and lots) using instrument metadata. No floats in book or accounting logic. Fees and PnL may use floats at the reporting boundary only if the integer path is unaffected; prefer integer or `Decimal` accounting.
- After each applied update assert the book is not crossed (`best_bid < best_ask`); count violations and surface them in the report instead of silently continuing.

### Loader

- Stream files in chronological order, k-way merge across symbols and topics by `cts` (trades by `T`). Deterministic tie-break: `(time, event_kind_order, file_order, line_no)`.
- Deduplicate overlaps caused by recorder restarts.
- On first read, write a normalized columnar cache (Parquet or NumPy) keyed by input file SHA-256 and parser version. Subsequent runs read the cache. Parsing raw JSON per run is too slow for parameter sweeps.

### Instrument metadata

A script `fetch_instruments.py` stores `GET /v5/market/instruments-info?category=linear` output as JSON next to the dataset: tick size, quantity step, min order quantity, min notional, max order quantity. The simulator validates orders against it.

## 4. Simulation core

Single event queue ordered by `(sim_time_ns, tiebreak)`. Event kinds: market data becomes true at the exchange, market data delivered to strategy, order arrives at exchange, order update delivered to strategy, timer, funding tick, end of data.

### Books

- `ExchangeBook` per symbol: ground truth, updated at exchange time from recorded data.
- `StrategyBook` per symbol: same updates delivered after the market-data latency; this is the only book the strategy can read.

### Order handling

An order sent at strategy time `t` arrives at the exchange at `t + L_order`. On arrival, validate in this order: rate limit, instrument rules (tick, lot, min notional, max qty), margin, post-only crossing, reduce-only. Accepted orders produce an ack delivered after `L_ack`. Rejects carry Bybit-style `retCode`/`retMsg`; look up the real codes in the docs.

Supported order types: Limit (GTC), PostOnly, IOC, FOK, Market, reduce-only flag, cancel, amend. One-way position mode only.

### Taker execution

Walk the `ExchangeBook` at the order's arrival time: consume levels in price priority, produce one fill per level touched, compute VWAP. IOC cancels the remainder; FOK is all-or-nothing; Market walks until filled or the book is exhausted.

`shadow_consumption` (default on): quantity our own taker fills removed from a level is hidden from later fills until the next recorded update touches that level. This avoids reusing the same liquidity twice between data updates. Document it as an approximation.

### Maker execution (fill models)

Select with `fill_model` in config. All three must exist; the report always shows the choice.

1. `optimistic`: a resting order fills in full when the opposite best price reaches or crosses its price. Ignores queue position. Upper bound.
2. `trade_through`: a resting order fills only when a trade prints strictly through its price (buy order: trade price below the order price). Lower bound.
3. `queue`: on arrival at the book, `queue_ahead` = visible size at that price (scaled by a `queue_pessimism` factor, default 1.0). `queue_ahead` decreases by trades printing at that price. Reductions of level size not explained by trades are treated as cancellations and attributed by a configurable rule: `behind_us` (pessimistic: cancels never help us), `proportional` (cancel ahead of us with probability proportional to our share of the level), `ahead_of_us` (optimistic). The order fills when `queue_ahead <= 0` and further trade volume arrives at that price; partial fills follow trade size.

Ordering rule when a trade and a book delta carry the same `cts`: configurable, default "trade first, then book update". Document the choice.

L2 data does not contain individual orders, so queue position is always a model. The open-source project `hftbacktest` implements related queue models and is worth reading for ideas. Do not take it as a dependency.

### Fees

Config: `maker_bps`, `taker_bps` (negative allowed for rebates), optional per-symbol override. Do not hardcode Bybit tiers.

### Funding and mark price

Perpetual funding is paid at fixed intervals (verify the schedule per symbol in docs). Payment = position notional at mark price times funding rate; longs pay when the rate is positive. Source of rate and mark price: the `tickers` topic. If tickers were not recorded, config `funding: ignore | constant` applies and the report prints a loud warning. Mark price falls back to mid.

### Position and margin

Linear USDT, one-way mode. Track position, average entry, realized PnL, fees, funding, unrealized PnL at mark. Cross-margin simplification: equity = wallet balance + unrealized PnL. Check initial margin at order placement (configurable leverage). If equity falls below maintenance margin, end the run with status `LIQUIDATED`. Keep the margin model simple and documented; it is not the thesis focus.

### Rate limits

Token buckets per request category with limits from config. Excess requests are rejected with the Bybit-style error. Do not invent default numbers.

## 5. Latency model

Components, each configured independently:

- `L_md`: exchange to strategy, market data.
- `L_order`: strategy to exchange, new/cancel/amend.
- `L_ack`: exchange to strategy, order updates and fills.
- `processing`: virtual strategy compute time per event (used in lockstep mode only).

Distributions: `constant`, `uniform`, `lognormal(mu, sigma)`, `empirical` (sampled from a file of measured values, for example `recv_ns - cts` from the recorder). Per channel enforce ordering: `arrival_i = max(arrival_{i-1}, send_i + sample)`, since a TCP stream does not reorder.

Ship presets `configs/latency/home.yaml` and `configs/latency/colo.yaml` with clearly marked placeholder values. Real values come from measurement. Warn in the docs that an empirical `L_md` derived from recorder data is valid only if the recorder ran in the same network position as the intended deployment.

## 6. Mode A: research runner

Python strategy interface (Protocol or ABC):

```
on_start(ctx)
on_book(symbol, book_view)        # StrategyBook, delayed
on_trade(trade)
on_order_update(order)
on_fill(fill)
on_timer(timer_id)
on_end(ctx)
```

`ctx.gateway` exposes `send_order`, `cancel_order`, `amend_order`, `set_timer`, `now()`. The strategy has no handle to the data reader or to the `ExchangeBook`.

Required features:

- **Sweep runner:** multiprocessing over a config grid (fees, latency preset, fill model, strategy parameters). Each run has its own seed and output directory.
- **Walk-forward split:** config `train_window` and `test_window`. The strategy receives training data through a separate `fit()` call that completes before any test-window event is delivered. Enforce this in the runner. Parameters selected on the same data used for reported PnL is the classic leakage error here.
- Performance target to measure, not assume: a full day for several symbols from the normalized cache in minutes, not hours. Profile before optimizing; use numba or similar only where profiling shows a hot spot.

## 7. Mode B: Bybit-compatible server

Purpose: let the real engine connect with its normal Bybit adapter. Implement with `asyncio` (`websockets` or `aiohttp`). Verify every endpoint and message shape against the docs.

Public WebSocket (`/v5/public/linear`): `subscribe`/`unsubscribe`, `ping`/`pong`, topics `orderbook.{depth}.{symbol}`, `publicTrade.{symbol}`, `tickers.{symbol}`. For public topics, replay the recorded raw JSON verbatim (filtered by subscription) so the client's parser sees real message formats. Only the depth that was recorded is offered.

Private WebSocket (`/v5/private`): auth, topics `order`, `execution`, `position`, `wallet`.

REST: `POST /v5/order/create`, `/v5/order/cancel`, `/v5/order/amend`, `/v5/order/cancel-all`, `GET /v5/order/realtime`, `/v5/position/list`, `/v5/account/wallet-balance`, `/v5/market/instruments-info`, `/v5/market/orderbook` (needed by clients that resync via REST). Signature validation is optional behind a flag (default: accept any key).

Time control:

- `realtime`: paced by wall clock with speed factor `x`. Not deterministic. For adapter integration tests and demos only. Never use for thesis numbers.
- `lockstep`: deterministic. The server delivers all events with time at or before `T`, then sends `{"op":"barrier","t":T}` on a separate control WebSocket. The client replies `{"op":"ready","t":T}` after processing everything it received. Orders sent between barriers are stamped with arrival `T_last_event + processing + L_order`. The client takes its clock from message timestamps, never from the wall clock. This needs a small optional flag in the engine's adapter.

**Parity test (acceptance criterion):** the same strategy implemented through Mode A and through the engine over Mode B lockstep, same data, config and seed, must produce identical orders and fills.

## 8. Outputs

Each run writes a directory containing:

- `config.resolved.yaml`, `manifest.json` (git commit, input file hashes, seed, fill model, latency preset, parser version),
- `orders.parquet`, `fills.parquet`, `equity.parquet`, `book_gaps.parquet`,
- `metrics.json`: gross and net PnL, fees, funding, turnover, Sharpe and max drawdown on fixed-frequency equity returns, fill ratio, slippage versus mid at decision time, and adverse selection (mid move after maker fills at 1 s, 10 s, 60 s).

Every report header prints: fill model, latency preset, funding handling, total invalid-book time, crossed-book count.

## 9. Validation and tests

- Book reconstruction: golden tests on a small recorded sample, including a restart (`u == 1`), a gap, and a `disconnected` marker. Compare reconstructed top of book with a REST snapshot or the recorded level-1 data if available.
- Invariants (property tests): no negative sizes, book not crossed, position equals the sum of fills, `equity_change == realized + unrealized - fees + funding`.
- Determinism: two runs with identical inputs produce byte-identical outputs.
- Look-ahead: a deliberately cheating strategy that tries to access future data must fail structurally; assert delivered event times never exceed `now()` and are monotonic per channel.
- Sanity: with zero latency, a taker buy at the ask fills at the ask; fill-model ordering `optimistic >= queue >= trade_through` holds for fill counts on a fixed sample.
- Parity: Mode A versus Mode B lockstep (section 7).

## 10. Project layout and stack

Python 3.11+. Dependencies: `numpy`, `pyarrow`, `orjson`, `pyyaml`, `websockets` or `aiohttp`, `pytest`, `ruff`, `mypy`. Use dataclasses with `slots`. Avoid per-message object churn in the hot loop.

```
backtester/
  data/        reader, parser, cache, instruments
  core/        clock, events, books, matching, fillmodels, accounting, latency, ratelimit
  research/    strategy api, runner, sweeps, walkforward, reports
  server/      public_ws, private_ws, rest, timecontrol
  configs/
  tests/
  scripts/     fetch_instruments.py
```

## 11. Milestones (each ends with passing tests)

1. Reader, parser, normalized cache, local book with gap handling. Golden tests.
2. Virtual clock, event queue, taker-only exchange, accounting, minimal Mode A runner. Zero-latency sanity tests.
3. Latency model and `StrategyBook`/`ExchangeBook` split. Look-ahead and determinism tests.
4. Maker fill models (`optimistic`, `trade_through`, `queue`), fees, instrument validation, rate limits.
5. Funding, mark price, margin, outputs and metrics, sweep runner, walk-forward enforcement.
6. Server mode: public and private WebSocket, REST, `realtime` pacing.
7. `lockstep` time control and parity test against Mode A.

## 12. Working agreements

- Implement milestone by milestone; do not start the next until tests for the current one pass.
- When behavior is ambiguous (Bybit semantics, tie-breaking, fill attribution), choose the most conservative option, make it a config flag, and note it in `docs/assumptions.md`.
- Never fabricate Bybit field names, error codes, or limits. If you cannot verify, leave a TODO with the doc URL.
- Keep the core free of network and wall-clock dependencies so Mode A stays fast and deterministic.