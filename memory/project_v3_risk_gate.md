---
name: V3.0 Pre-Execution Risk Gate
description: Gap 3 from the V3.0 gap analysis is implemented — SimulatedPortfolio gates swaps before they are emitted
type: project
---

V3.0 Gap 3 (Post-Trade State Simulation) is implemented as of 2026-05-11.

**Why:** The pre-V3.0 sector concentration check ran on the pre-trade portfolio plus buy-tickers as additive extras, never removing sold tickers — a state inconsistency that could recommend a swap that breaches the sector limit.

**What was built:**
- `src/risk_engine.py` — new module with `SimulatedPortfolio` class and `get_post_trade_snapshot()`
- `src/correlation.py` — added `get_sector()` public wrapper; `format_concentration_blocks()` accepts `title=` param
- `src/llm_compiler.py` — `get_optimal_swaps()` now instantiates `SimulatedPortfolio`, pre-warms sector cache, and evaluates each candidate through the gate before committing; friction pre-computed and stored in swap dict; `_build_daily_blocks` uses `get_post_trade_snapshot` for the sector check block
- `main.py` — passes `portfolio_df=df_all` into `get_optimal_swaps` for real notional sizing

**How to apply:** Remaining V3.0 gaps (slippage model, MVO portfolio optimization, regime filter) are the next roadmap items. The backtesting data from V2.0 is needed before calibrating the scoring weights used by MVO.
