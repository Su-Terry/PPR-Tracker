---
name: V3.0 Macro Regime Filter & Beta Penalty
description: Gap 4 — MarketRegime enum, SPY/VIX fetch, beta-adjusted scoring, CRASH override, Slack banner
type: project
---

V3.0 Gap 4 (Extreme Tail Risk) is implemented as of 2026-05-11.

**Why:** The scoring system previously evaluated PEG/MA50 in a vacuum — a high-beta stock with an attractive PEG is still dangerous when SPY is crashing. The regime filter adds systemic market awareness.

**What was built:**
- `src/risk_engine.py` — added `MarketRegime` enum (BULL/NEUTRAL/BEAR/CRASH) and `classify_regime(vix, spy_ma_dist)` function
- `src/data_fetcher.py` — added `beta: float | None` to `ScanResult`; fetched from yfinance `info["beta"]` in `_evaluate_ticker`; added `get_macro_regime() -> dict` which fetches SPY + ^VIX, computes spy_ma_dist, and returns regime enum (defaults BULL on failure to never suppress swaps due to data outage)
- `src/llm_compiler.py`:
  - `calculate_efficiency_score` now accepts `regime` param; applies `penalty = max(0, (beta-1) * 0.2)` in BEAR/CRASH, de-ranking high-beta assets
  - `get_optimal_swaps` accepts `regime`; in CRASH returns only cash-flight swaps (early return) — no equity swaps emitted
  - `_build_daily_blocks` / `generate_daily_report` accept `regime` + `macro_data`; CRASH injects a prominent banner block; ALPHA section is suppressed; rotation header changes to "CASH FLIGHT ONLY"
- `main.py` — `get_macro_regime()` called at the start of every scan; regime propagated to `get_optimal_swaps` and `generate_daily_report`

**How to apply:** CRASH is triggered by VIX ≥ 30 AND SPY > 5% below its 50-day MA. BEAR (VIX ≥ 20, SPY below MA) applies beta penalty but still allows equity swaps. BULL/NEUTRAL operate as before.
