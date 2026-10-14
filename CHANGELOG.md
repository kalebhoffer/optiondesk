# Changelog

## 0.1.0 (2026-10-14)

First release.

- Engine: bars -> strategies -> risk checks -> contract selection -> sizing ->
  orders, with exit management on every bar.
- Example strategies: EMA crossover and opening range breakout; plugin loading
  for your own.
- Risk gates: market hours, daily loss limit, max positions, one position per
  symbol, cooldowns, total exposure.
- Sizing: fixed fraction or fractional Kelly.
- Exits: stop loss, take profit, trailing stop, max hold time, end of day.
- Data: synthetic, CSV replay, Schwab live quotes and option chains.
- Brokers: conservative paper broker; Schwab (guarded live trading).
- SQLite event journal, dashboard with halt/resume, backtest report.
- Docker image and Compose file.
