"""
Legacy archive — V1.1 pairwise get_optimal_swaps body.

Preserved per spec §10.4 ("封存 not 刪除") and CLAUDE.md Sprint 5 rollback plan.
This file is NOT imported or executed anywhere in production.

Rollback: copy this body back into src/llm_compiler.py:get_optimal_swaps() and
revert the Sprint 5 squash commit (git revert <sprint5-sha>).

Original location: src/llm_compiler.py, function get_optimal_swaps (lines 370–693).
Archived: 2026-05-12, Sprint 5 cutover.
"""

from __future__ import annotations

# ── Context: imports that the live function depended on (at cutover) ─────────
# from src.data_fetcher import ScanResult
# from src.risk_engine import MarketRegime
# from src.llm_compiler import (
#     calculate_efficiency_score, estimate_round_trip,
#     SimulatedPortfolio, get_sector,
#     _compute_haven_route, _select_safe_haven, register_dip_buy_candidate,
#     DEFAULT_CAPITAL_BLOCK, SAFE_HAVENS,
# )


def get_optimal_swaps_pairwise(
    portfolio_results,   # list[ScanResult]
    discovery_results,   # list[ScanResult]
    threshold = 0.15,
    portfolio_df = None,
    regime = None,
):  # -> list[dict]
    """
    V1.1 pairwise implementation of get_optimal_swaps (V3.0 — Pre-Execution Gate).

    Identify the weakest portfolio tickers and pair each with the strongest
    Discovery target that (a) beats the score threshold and (b) passes the
    pre-execution sector-concentration risk gate.

    Algorithm:
      1. Score every portfolio ticker with calculate_efficiency_score.
         Skip tickers with score == 0.0 (missing data — not actionable).
      2. Bottom Tier = worst ⌊25%⌋ of scored portfolio tickers, minimum 1, max 5.
      3. Score every Discovery ticker; Top Tier = top M by score (max 5).
      4. Instantiate SimulatedPortfolio from current holdings.
      5. For each Bottom Tier ticker, iterate through Top Tier candidates
         (highest score first) and apply the risk gate:
           a. simulate_swap() — compute synthetic post-trade holdings.
           b. check_sector_limits() — reject if target sector > SECTOR_LIMIT_PCT.
           c. First candidate that passes is approved; commit_swap() updates
              SimulatedPortfolio state so the next iteration sees the real
              post-swap world.
      6. Sort results by score_delta descending.
    """
    # ── Score all tickers ────────────────────────────────────────────────────
    def _ratio(r):
        if r.valuation_model == "PEG" and r.modified_peg is not None:
            v = r.modified_peg
            return None if v == float("inf") else v
        if r.valuation_model == "PS" and r.ps_growth_ratio is not None:
            v = r.ps_growth_ratio
            return None if v == float("inf") else v
        return None

    def _dist_pct(r):
        if r.current_price and r.ma50:
            return (r.current_price - r.ma50) / r.ma50 * 100
        return None

    def _compute_exit_ratio(r):
        ratio = _ratio(r)
        if ratio is None:
            return 1.0
        if ratio <= 0.8:
            return 0.5
        if ratio <= 1.5:
            return 0.75
        return 1.0

    # ── CIRCUIT BREAKER: route overheated tickers to Safe Haven ─────────────
    _CB_SIGNAL  = "嚴重技術面過熱：強制減倉停利"
    cash_swaps = []
    cb_tickers = set()

    cb_results = [
        r for r in portfolio_results
        if not r.error and any(s == _CB_SIGNAL for s in r.signals)
    ]
    for r in cb_results:
        cb_tickers.add(r.ticker)

    haven_route  = {}
    boxx_result  = None

    if cb_tickers:
        haven_route = _compute_haven_route(DEFAULT_CAPITAL_BLOCK)
        if haven_route["route"] == "BOXX":
            boxx_result = _select_safe_haven(SAFE_HAVENS)

    for r in cb_results:
        ratio_v     = _ratio(r)
        dist_v      = _dist_pct(r)
        exit_ratio  = _compute_exit_ratio(r)
        safe_haven_r = boxx_result
        haven_dist  = (
            (safe_haven_r.current_price - safe_haven_r.ma50) / safe_haven_r.ma50 * 100
            if safe_haven_r and safe_haven_r.current_price and safe_haven_r.ma50
            else None
        )

        if exit_ratio < 1.0:
            ma50_dist_decimal = (
                (r.current_price - r.ma50) / r.ma50
                if r.current_price and r.ma50
                else None
            )
            register_dip_buy_candidate(
                ticker=r.ticker,
                peg=ratio_v,
                exit_ratio=exit_ratio,
                exit_price=r.current_price,
                ma50_dist_at_exit=ma50_dist_decimal,
            )

        cash_swaps.append({
            "source_ticker":  r,
            "target_ticker":  safe_haven_r,
            "sell_score":     calculate_efficiency_score(r, regime=regime),
            "buy_score":      calculate_efficiency_score(safe_haven_r, regime=regime) if safe_haven_r else 0.0,
            "score_delta":    999.0,
            "is_cash_flight": True,
            "exit_ratio":     exit_ratio,
            "haven_route":    haven_route,
            "delta_metrics": {
                "sell_ratio":       ratio_v,
                "buy_ratio":        None,
                "sell_dist_pct":    dist_v,
                "buy_dist_pct":     haven_dist,
                "peg_improvement":  None,
                "dist_improvement": (
                    (dist_v - haven_dist)
                    if dist_v is not None and haven_dist is not None
                    else None
                ),
            },
        })

    portfolio_scored = [
        (r, calculate_efficiency_score(r, regime=regime))
        for r in portfolio_results
        if not r.error and r.ticker not in cb_tickers
        and calculate_efficiency_score(r, regime=regime) > 0.0
    ]
    portfolio_scored.sort(key=lambda x: x[1])

    discovery_scored = [
        (r, calculate_efficiency_score(r, regime=regime))
        for r in discovery_results
        if not r.error and calculate_efficiency_score(r, regime=regime) > 0.0
    ]
    discovery_scored.sort(key=lambda x: -x[1])

    if not portfolio_scored:
        return cash_swaps
    if not discovery_scored:
        return cash_swaps

    if regime is not None and regime.__class__.__name__ == "MarketRegime":
        from src.risk_engine import MarketRegime as _MR
        if regime == _MR.CRASH:
            cash_swaps.sort(key=lambda s: s["score_delta"], reverse=True)
            return cash_swaps

    n           = max(1, min(5, len(portfolio_scored) // 4))
    bottom_tier = portfolio_scored[:n]
    top_tier    = discovery_scored[:min(5, len(discovery_scored))]

    sim_portfolio = SimulatedPortfolio.from_scan_results(
        [r for r in portfolio_results if not r.error],
        portfolio_df=portfolio_df,
    )

    for r in portfolio_results + discovery_results:
        if not r.error:
            get_sector(r.ticker)

    used_targets = set()
    swaps = []

    for sell_r, sell_score in bottom_tier:
        candidates = [
            (buy_r, buy_score)
            for buy_r, buy_score in top_tier
            if buy_r.ticker not in used_targets
            and buy_score - sell_score > threshold
        ]

        approved_buy_r    = None
        approved_buy_score = 0.0
        approved_friction  = {}

        for buy_r, buy_score in candidates:
            friction          = estimate_round_trip(sell_r.ticker, buy_r.ticker)
            position_val      = sim_portfolio.get_holdings().get(sell_r.ticker, DEFAULT_CAPITAL_BLOCK)
            friction_notional = friction["total_rate"] * position_val
            buy_sector        = get_sector(buy_r.ticker)
            synthetic         = sim_portfolio.simulate_swap(sell_r.ticker, buy_r.ticker, friction_notional)
            if not sim_portfolio.check_sector_limits(synthetic, buy_sector):
                continue
            sim_portfolio.commit_swap(sell_r.ticker, buy_r.ticker, friction_notional)
            approved_buy_r     = buy_r
            approved_buy_score = buy_score
            approved_friction  = friction
            break

        if approved_buy_r is None:
            continue

        used_targets.add(approved_buy_r.ticker)

        sell_ratio = _ratio(sell_r)
        buy_ratio  = _ratio(approved_buy_r)
        sell_dist  = _dist_pct(sell_r)
        buy_dist   = _dist_pct(approved_buy_r)

        swaps.append({
            "source_ticker": sell_r,
            "target_ticker": approved_buy_r,
            "sell_score":    sell_score,
            "buy_score":     approved_buy_score,
            "score_delta":   approved_buy_score - sell_score,
            "friction":      approved_friction,
            "delta_metrics": {
                "peg_improvement":  (sell_ratio - buy_ratio) if sell_ratio is not None and buy_ratio is not None else None,
                "dist_improvement": (sell_dist  - buy_dist)  if sell_dist  is not None and buy_dist  is not None else None,
                "sell_ratio":       sell_ratio,
                "buy_ratio":        buy_ratio,
                "sell_dist_pct":    sell_dist,
                "buy_dist_pct":     buy_dist,
            },
        })

    all_swaps = cash_swaps + swaps
    all_swaps.sort(key=lambda s: s["score_delta"], reverse=True)
    return all_swaps
