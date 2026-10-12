# Simulator assumptions

- Exchange time is `cts` for order-book messages and `T` for trades. Recorder `recv_ns` is metadata only.
- Taker shadow consumption is enabled by default. Liquidity consumed by this simulator is hidden until the next recorded update touches the level.
- Taker matching is implemented first. Resting maker orders use explicitly selected `optimistic`, `trade_through`, or `queue` models; L2 data cannot reveal actual queue position.
- Latency preset values are placeholders. An empirical market-data distribution calibrated from recorder data is valid only when the recorder used the same network position as the intended deployment.
- Rate limits, fee tiers, funding rates, and Bybit error semantics must be supplied through configuration or verified against the official V5 documentation; the core does not invent exchange defaults.
