---
name: V3.1 Quantamental Sizing & Re-Entry Radar
description: Dynamic exit sizing (exit_ratio), DIP_BUY_CANDIDATE state, TACTICAL RE-ENTER Slack block, partial SimulatedPortfolio
type: project
---

V3.1 implemented 2026-05-11.

**Why:** Binary all-or-nothing exits are inefficient when a stock is technically overheated but fundamentally exceptional (e.g. QCOM PEG 0.13 at +54.7% MA50). Partial exit preserves upside exposure while reducing overheated position.

**What was built:**

### `src/state_manager.py` (NEW)
- `load_dip_buy_candidates() -> dict` — reads `data/dip_buy_candidates.json`
- `register_dip_buy_candidate(ticker, peg, exit_ratio, exit_price, ma50_dist_at_exit)` — writes partial-exit record to disk
- `remove_dip_buy_candidate(ticker)` — removes after re-entry signal fires
- `check_reentry_signals(results, candidates, ma_lo=0.0, ma_hi=0.10) -> list[dict]` — fires when MA50_dist ∈ [0%, +10%]

### `src/risk_engine.py`
- `SimulatedPortfolio.simulate_swap(...)` now accepts `exit_ratio: float = 1.0`; partial exit retains `(1-exit_ratio)` of source position
- `SimulatedPortfolio.commit_swap(...)` also accepts `exit_ratio: float = 1.0`
- `get_post_trade_snapshot(...)` only removes source tickers where `exit_ratio >= 1.0`; partial exits stay in the snapshot for correct sector concentration count

### `src/llm_compiler.py`
- `_compute_exit_ratio(r)` closure inside `get_optimal_swaps`: PEG ≤ 0.8 → 0.50, PEG ≤ 1.5 → 0.75, PEG > 1.5 → 1.0, None → 1.0
- CB loop now computes exit_ratio per ticker, calls `register_dip_buy_candidate` when exit_ratio < 1.0, and stores `exit_ratio` in the swap dict
- `_make_rotation_block` cash-flight path: shows sizing-specific action text ("減倉 50%/75%") with DIP_BUY_CANDIDATE notification for partial exits
- `_build_daily_blocks`: loads candidates + checks re-entry signals; emits `🎯 TACTICAL RE-ENTER (短期回踩接回)` block between STRATEGIC ROTATION and ALPHA; clears fired signals from registry

**How to apply:**
- Re-entry window: MA50_dist ∈ [0.0, 0.10] (0% to +10% above MA50)
- Exit thresholds: PEG ≤ 0.8 → 50%, PEG ≤ 1.5 → 75%, PEG > 1.5 → 100%
- State file: `data/dip_buy_candidates.json` — human-editable, survives restarts
- Re-entry block auto-removes the ticker from state once surfaced to operator
