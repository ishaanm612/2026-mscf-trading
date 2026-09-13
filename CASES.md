# Case notes and sources

Reviewed September 13, 2026. [Event and setup page](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/ritcx/ritcx-cards/cmu/).

## Algorithmic ETF Arbitrage

[Official specification](https://rotmanfrtl.github.io/RITCx-Algorithmic%20ETF%20Arb%20Case.pdf): 300 seconds; BULL and BEAR are CAD-denominated, RITC is USD-denominated. Equilibrium is `RITC × USD/CAD = BULL + BEAR`. RITC carries 2× position weight. Equity orders are capped at 10,000; market fees are 0.02/share. Converters are manual only. Numeric gross/net limits are not supplied in the specification; obtain them from the session. The template's limits are illustrative.

Our foundation evaluates both basket directions and tender liquidation against visible depth. ETF fee conversion assumes fees in quote currency; verify this in practice. FX is priced but not traded. Tender fees, expiration, current inventory, outstanding orders and evolving unwind depth need execution-stage handling. Statistical convergence is not guaranteed immediate arbitrage.

## Volatility Trading

[Official specification](https://rotmanfrtl.github.io/RITCx-Volatility%20Trading%20Case.pdf): RTM options are European; the table lists strikes 48–52, calls and puts. One contract covers 100 shares. Duration is 300 seconds, representing 1/12 year. Delta band is ±7,000 shares. RTM gross/net limit is 50,000; options gross/net limits are 2,500/1,000 contracts. Order caps are 10,000 shares or 100 contracts; fees are 0.02/share or 2/contract.

The PDF overview's “10 different strike prices” conflicts with its five-strike table; its sample news timing also conflicts with the 300-second duration. Discover available tickers and inspect actual session news. Our runner parses ticker strikes rather than assuming security order. Time uses `(300 - tick)/3600`, matching the official starter.

Model assumption: one supplied sigma applies to all remaining time. A later forecast module should combine future intervals using time-weighted **variance**, handle news validity explicitly, and track uncertainty. Delta hedge suggestions include current stock holdings and require splitting into legal child orders. Cost reserves omit future rehedging and execution uncertainty. Before automation, add projected option gross/net checks, intermediate delta checks, hedge capacity, and fill reconciliation.

## Official support files

- [ETF REST](https://rotmanfrtl.github.io/RITCx%20ETF%20Arbitrage%20Case%20base%20script-REST%20API.py) → `reference/etf_rest.py`
- [ETF DMA](https://rotmanfrtl.github.io/RITCx%20ETF%20Arbitrage%20Case%20base%20script-DMA%20API.py) → `reference/etf_dma.py`
- [Volatility REST](https://rotmanfrtl.github.io/RITCx%20Volatility%20Trading%20Case%20base%20script-REST%20API.py) → `reference/volatility_rest.py`
- [Volatility DMA](https://rotmanfrtl.github.io/RITCx%20Volatility%20Trading%20Case%20base%20script-DMA%20API.py) → `reference/volatility_dma.py`

Copyright remains with Rotman as stated in those files. The original ETF scripts accept tenders indiscriminately and use unweighted risk checks; the original volatility hedge expression divides by current stock inventory. Our modules replace those behaviors with evaluation-only tender reports, weighted checks, and additive delta accounting.
