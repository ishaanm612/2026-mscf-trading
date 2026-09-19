# Case notes and sources

Reviewed September 13, 2026. [Event and setup page](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/ritcx/ritcx-cards/cmu/).

## Algorithmic ETF Arbitrage

[Official specification](https://rotmanfrtl.github.io/RITCx-Algorithmic%20ETF%20Arb%20Case.pdf): 300 seconds; BULL and BEAR are CAD-denominated, RITC is USD-denominated. Equilibrium is `RITC × USD/CAD = BULL + BEAR`. RITC carries 2× position weight. Equity orders are capped at 10,000; market fees are 0.02/share. Converters are manual only. Numeric gross/net limits are not supplied in the specification; obtain them from the session. The template's limits are illustrative.

Our strategy evaluates both basket directions and complete post-tender liquidation routes against visible depth. Basket entry uses executable equity prices and an FX midpoint reference; no gross FX funding order is submitted. Tender scoring compares the current portfolio with the post-acceptance portfolio, including direct, creation, redemption and mixed exits. Frozen-book routes split orders and consume displayed depth once. A separate staged direct route can forecast limited replenishment only after observing new near-touch order arrivals, with a complete frozen-book stress exit, a loss threshold and a bounded schedule. Its forecast is not guaranteed executable profit. Separate execution/FX reserves, their coefficients, startup floors and scheduling times are strategy assumptions documented in OPERATIONS.md. They are not competition rules. Acceptance refreshes the complete snapshot and checks intermediate equity and cash limits. Offer expiry is an acceptance deadline. The execution layer keeps naturally offsetting RITC/USD cash together and converts final net USD after equity liquidation. Statistical convergence is not guaranteed immediate arbitrage.

ETF Creation and Redemption are manual 10,000-unit converters costing USD 1,500 per use. Creation plans may first acquire the required stocks through legal API child orders. Manual instructions have a timeout and a direct-liquidation fallback, and cannot override forced liquidation. The strategy never invokes a converter through the API.

Optional basket trading is explicitly capped statistical convergence, not guaranteed converter arbitrage. `models/etf_basket.py` separates a payoff conditional on convergence from current executable liquidation P&L, budgets exit friction and uncertainty, prohibits underwater additions, and applies profit, loss, age and dynamic session-time exits. Its size cap, reserve coefficients and thresholds are strategy assumptions, not case rules or calibrated performance guarantees. A basket smaller than 10,000 units cannot use a whole converter block.

## Volatility Trading

[Official specification](https://rotmanfrtl.github.io/RITCx-Volatility%20Trading%20Case.pdf): RTM options are European; the table lists strikes 48–52, calls and puts. One contract covers 100 shares. Duration is 300 seconds, representing 1/12 year. Delta band is ±7,000 shares. RTM gross/net limit is 50,000; options gross/net limits are 2,500/1,000 contracts. Order caps are 10,000 shares or 100 contracts; fees are 0.02/share or 2/contract.

The PDF overview's “10 different strike prices” conflicts with its five-strike table; its sample news timing also conflicts with the 300-second duration. Discover available tickers and inspect actual session news. Our runner parses ticker strikes rather than assuming security order. Time uses `(300 - tick)/3600`, matching the official starter.

Standalone analysis uses one supplied sigma for remaining time. Bot mode combines weekly forecasts using time-weighted **variance**, uses the mean of endpoint variances for ranges, and fills unannounced weeks with a 20% prior (or `--sigma` when supplied) rather than carrying the last print. Unparsed volatility news blocks decisions. Delta hedge suggestions include current stock holdings and require splitting into legal child orders. Cost reserves omit future rehedging and execution uncertainty. The bot checks projected gross/net exposure, intermediate delta, and hedge capacity; execution confirms every fill before the next action.

## Official support files

- [ETF REST](https://rotmanfrtl.github.io/RITCx%20ETF%20Arbitrage%20Case%20base%20script-REST%20API.py) → `reference/etf_rest.py`
- [ETF DMA](https://rotmanfrtl.github.io/RITCx%20ETF%20Arbitrage%20Case%20base%20script-DMA%20API.py) → `reference/etf_dma.py`
- [Volatility REST](https://rotmanfrtl.github.io/RITCx%20Volatility%20Trading%20Case%20base%20script-REST%20API.py) → `reference/volatility_rest.py`
- [Volatility DMA](https://rotmanfrtl.github.io/RITCx%20Volatility%20Trading%20Case%20base%20script-DMA%20API.py) → `reference/volatility_dma.py`

Copyright remains with Rotman as stated in those files. The original ETF scripts accept tenders indiscriminately and use unweighted risk checks; the original volatility hedge expression divides by current stock inventory. Our modules replace those behaviors with evaluation-only tender reports, weighted checks, and additive delta accounting.

## Practice observations (September 13, 2026)

Both DMA accounts authenticated after correcting the account-to-case mapping. The active ETF session exposed stock gross/net limits of 300,000/200,000 and cash limits of 10,000,000. Its RITC limit binding reported `units: 0.5` (instrument units per risk unit). The strategy also applies the published 2x local ETF weight.

The volatility session reported RTM commission 0.01/share and options commission 1/contract, lower than the PDF defaults. Pricing currently retains the more conservative published cost reserves (0.02/share and 2/contract). Both sessions reported 300 ticks per round. The volatility parser recognizes actual opening, weekly announcement, and forecast-range formats. Observed weekly announcements occur at ticks 75 and 150; the parser also honors explicit `Week N` ticker labels.

API request/response reference: [official DMA schema](https://rit.306w.ca/RIT-DMA-API/1.0.5/swagger.yaml). Fixed-price tender acceptance includes its price, and order confirmation polls `GET /orders/{id}`. Session observations are not assumptions about future competition settings; use `--check` before trading.
