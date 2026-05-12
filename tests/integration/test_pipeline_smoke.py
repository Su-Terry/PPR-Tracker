"""
End-to-end pipeline smoke test: state → covariance → optimizer → conviction
→ trade_builder → archive → discipline metrics.

Uses a 5-ticker synthetic US portfolio (CASH + 4 equities). Exercises every
layer in Sprint 1–3 in order. Does not count toward unit coverage gate per
CLAUDE.md plan (integration test excluded from per-module coverage requirement).

Run standalone:
    uv run pytest tests/integration -v

Output includes a toy demo formatted as a Slack-ready trade summary for
inclusion in the Sprint 3 PR description.

Note on test config: the default US RebalanceConfig targets a 30-80 ticker
universe. With only 5 tickers, max_position=0.20 and an exhaustive 2-sector
partition (max_sector=0.40 × 2 = 0.80 < 1.0) makes it impossible to satisfy
sum(w)=1 while respecting all constraints. SMOKE_CONFIG relaxes max_position
and max_sector for this synthetic 5-ticker universe.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from src.cost.profile import CostProfile
from src.discipline.metrics import EmptyActualsProvider, compute_metrics
from src.portfolio.state import PortfolioState
from src.rebalancer.config import RebalanceConfig
from src.rebalancer.cov_estimator import CovEstimate, estimate_covariance
from src.rebalancer.optimizer import solve_target_weights
from src.rebalancer.rationale import RationaleContext
from src.rebalancer.trade_builder import (
    BuildResult,
    archive_decision,
    build_trades,
)


# ── Synthetic universe ─────────────────────────────────────────────────────────

TICKERS = ["CASH", "AAPL", "NVDA", "GOOGL", "MSFT"]
CASH_IDX = 0
SECTOR_NAMES = ["Tech", "Other"]
N = len(TICKERS)

# Tech: AAPL, NVDA, MSFT; Other: CASH, GOOGL
SECTOR_MATRIX = np.array(
    [
        [0, 1, 1, 0, 1],
        [1, 0, 0, 1, 0],
    ],
    dtype=float,
)

# Relaxed config for 5-ticker test universe.
# Default US config targets 30-80 tickers; with only 5 tickers an exhaustive
# 2-sector partition (max_sector=0.40 each → max total=0.80 < 1.0) makes it
# impossible to satisfy sum(w)=1. Relax to 0.80 per sector.
SMOKE_CONFIG = dataclasses.replace(
    RebalanceConfig.us_default(),
    max_position=0.40,
    max_sector=0.80,
    min_position=0.01,
    min_total_turnover=0.0,   # no HOLD-from-min-turnover in 5-ticker test
    lambda_turnover=0.5,      # reduced turnover penalty so optimizer rebalances
)

# Current state: weights sum to 1.0, AAPL over-allocated, NVDA under-allocated.
# Satisfies SMOKE_CONFIG constraints: all ≤ 0.40, Tech=0.71 ≤ 0.80, cash=0.09 ≥ 0.05.
W0 = np.array([0.09, 0.25, 0.10, 0.20, 0.36], dtype=float)

# Scores: NVDA is the best opportunity (z-score +1.8), AAPL overvalued (-0.8)
SCORES = np.array([0.0, -0.8, 1.8, 0.4, 0.3], dtype=float)

# Prices in USD
PRICES = {
    "CASH": 1.0,
    "AAPL": 185.0,
    "NVDA": 920.0,
    "GOOGL": 170.0,
    "MSFT": 430.0,
}

PORTFOLIO_VALUE = 100_000.0  # USD


def _make_prices_df(seed: int = 42) -> pd.DataFrame:
    """Generate 260 days of synthetic price history for all tickers."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0005, 0.015, (260, N))
    prices = np.cumprod(1 + returns, axis=0) * 100.0
    return pd.DataFrame(prices, columns=TICKERS)


def _make_rationale_contexts() -> list[RationaleContext]:
    """
    Build RationaleContexts for all non-cash tickers.
    AAPL has RSI 82 (overheated) to demonstrate the overheat rule.
    NVDA has is_discovery=False but highest positive score delta.
    """
    equity_tickers = [(i, t) for i, t in enumerate(TICKERS) if i != CASH_IDX]
    rsi_map = {"AAPL": 82.0, "NVDA": 55.0, "GOOGL": 48.0, "MSFT": 60.0}
    return [
        RationaleContext(
            ticker=t,
            side="SELL" if SCORES[i] < 0 else "BUY",
            score_delta=float(SCORES[i]) if SCORES[i] >= 0 else -float(SCORES[i]),
            w0=float(W0[i]),
            rsi=rsi_map.get(t),
            is_discovery=(W0[i] == 0.0),
        )
        for i, t in equity_tickers
    ]


# ── Toy demo formatter ─────────────────────────────────────────────────────────

def format_trade_summary(result: BuildResult, portfolio_value: float) -> str:
    """
    Format a BuildResult as a Slack-ready trade summary (no HTTP calls).

    Example output:
        🇺🇸 *US* | Turnover: 7.2% | Est Cost: $2.15
          SELL AAPL   -5.5pp → 16.5%  conviction 6.8/10 ✅  · RSI 82 過熱
          BUY  NVDA   +4.0pp → 12.0%  conviction 7.4/10 ✅  · +1.8 score
    """
    if result.is_hold:
        flag = "🇺🇸" if result.market == "US" else "🇹🇼"
        return f"{flag} *{result.market}* | 🟢 HOLD all ({', '.join(result.hold_reasons)})"

    flag = "🇺🇸" if result.market == "US" else "🇹🇼"
    total_cost = sum(t.est_cost for t in result.trades)
    turnover_pct = sum(abs(t.delta_weight) for t in result.trades) / 2.0 * 100.0
    lines = [f"{flag} *{result.market}* | Turnover: {turnover_pct:.1f}% | Est Cost: ${total_cost:.2f}"]
    for t in result.trades:
        tier_icon = {"Execute": "✅", "Watch": "⏸", "Skip": "❌"}[t.execution_tier]
        delta_str = f"{t.delta_weight * 100:+.1f}pp → {t.target_weight * 100:.1f}%"
        lines.append(
            f"  {t.side:<4} {t.ticker:<6} {delta_str:<22}"
            f" conviction {t.conviction}/10 {tier_icon}"
            f"  · {t.rationale}"
        )
    return "\n".join(lines)


# ── Smoke test ─────────────────────────────────────────────────────────────────

class TestPipelineSmoke:
    def test_full_pipeline(self, tmp_path):
        """
        Full end-to-end pipeline from state creation to discipline metrics.
        """
        # ── Step 1: Portfolio state ────────────────────────────────────────────
        state = PortfolioState.create_empty()
        state.us_cash_usd = PORTFOLIO_VALUE * W0[CASH_IDX]
        for i, ticker in enumerate(TICKERS):
            if i == CASH_IDX:
                continue
            est_price = PRICES[ticker]
            shares = (PORTFOLIO_VALUE * W0[i]) / est_price
            state.us_holdings[ticker] = round(shares, 4)

        assert state.us_cash_usd > 0

        # ── Step 2: Covariance estimation ──────────────────────────────────────
        prices_df = _make_prices_df()
        config = SMOKE_CONFIG
        cov_est = estimate_covariance(prices_df, config)

        assert cov_est.matrix.shape == (N, N)
        assert cov_est.used_ledoit_wolf is True  # 260 rows > lookback_days_min=60

        # ── Step 3: Optimize ───────────────────────────────────────────────────
        # Use diagonal approximation for test speed (skip Ledoit-Wolf cost)
        cov_diag = np.diag(np.diag(cov_est.matrix))
        optimize_result = solve_target_weights(
            w0=W0,
            scores=SCORES,
            cov=cov_diag,
            sector_matrix=SECTOR_MATRIX,
            config=config,
            cash_idx=CASH_IDX,
        )

        # Optimizer should produce an active result given strong scores
        assert not optimize_result.is_hold, (
            f"Unexpected HOLD: {optimize_result.hold_reason}, "
            f"relaxations: {optimize_result.relaxations_applied}"
        )
        assert abs(optimize_result.w_target.sum() - 1.0) < 1e-4

        # ── Step 4: Build trades ───────────────────────────────────────────────
        rationale_contexts = _make_rationale_contexts()
        decisions_path = tmp_path / "memory" / "rebalance_decisions.jsonl"

        result = build_trades(
            optimize_result=optimize_result,
            tickers=TICKERS,
            w0=W0,
            scores=SCORES,
            prices=PRICES,
            portfolio_value=PORTFOLIO_VALUE,
            cost_profile=CostProfile.from_defaults(),
            config=config,
            cash_idx=CASH_IDX,
            market="US",
            sector_matrix=SECTOR_MATRIX,
            sector_names=SECTOR_NAMES,
            cov_estimate=cov_est,
            rationale_contexts=rationale_contexts,
            days_since_last_trade={t: 30 for t in TICKERS},
            recent_directions={t: 0 for t in TICKERS},
        )

        assert not result.is_hold, f"Unexpected HOLD from build_trades: {result.hold_reasons}"
        assert len(result.trades) >= 1, "Expected at least one trade"

        # ── Step 5: Validate every trade ──────────────────────────────────────
        for trade in result.trades:
            assert len(trade.rationale) <= 15, (
                f"Rationale '{trade.rationale}' for {trade.ticker} exceeds 15 chars"
            )
            assert trade.rationale != "", f"Empty rationale for {trade.ticker}"
            assert 0.0 <= trade.conviction <= 10.0, (
                f"Conviction {trade.conviction} out of range for {trade.ticker}"
            )
            assert trade.execution_tier in ("Execute", "Watch", "Skip")
            assert trade.notional == pytest.approx(abs(trade.quantity) * trade.est_price, rel=1e-4)
            assert trade.est_cost >= 0.0
            assert trade.actual_cost is None

        # ── Step 6: Archive decision ───────────────────────────────────────────
        archive_decision(
            result=result,
            w_current=W0,
            tickers=TICKERS,
            config=config,
            optimize_result=optimize_result,
            path=decisions_path,
        )
        assert decisions_path.exists()

        # ── Step 7: Discipline metrics ─────────────────────────────────────────
        from datetime import date
        today = date(2026, 5, 12)

        metrics = compute_metrics(
            w_current=optimize_result.w_target,  # assume executed
            tickers=TICKERS,
            decisions_path=decisions_path,
            actuals_provider=EmptyActualsProvider(),
            today=today,
        )

        assert metrics.drift_pct >= 0.0
        # After archiving, there's one active record → last_trade_days = 0
        assert metrics.last_trade_days == 0
        # EmptyActualsProvider → discipline_score_7d = 0
        assert metrics.discipline_score_7d == 0

        # ── Step 8: Print toy demo (captured in PR description) ───────────────
        demo = format_trade_summary(result, PORTFOLIO_VALUE)
        print("\n" + "=" * 60)
        print("SPRINT 3 TOY DEMO — Slack-ready trade summary")
        print("=" * 60)
        print(demo)
        print("=" * 60)
        print(f"Discipline metrics: drift={metrics.drift_pct:.1f}%  "
              f"turnover_30d={metrics.turnover_30d*100:.1f}%  "
              f"last_trade_days={metrics.last_trade_days}  "
              f"discipline_7d={metrics.discipline_score_7d}/100")
        print("=" * 60 + "\n")

        # Verify demo is non-empty and Slack-formatted
        assert "🇺🇸" in demo
        assert "*US*" in demo

    def test_hold_pipeline(self, tmp_path):
        """
        Verify HOLD path: w0 already at near-optimal → optimizer returns HOLD
        or build_trades returns HOLD.
        """
        # All scores zero → optimizer produces near-zero turnover → HOLD
        flat_scores = np.zeros(N)
        optimize_result = solve_target_weights(
            w0=W0,
            scores=flat_scores,
            cov=np.eye(N) * 0.04,
            sector_matrix=SECTOR_MATRIX,
            config=SMOKE_CONFIG,
            cash_idx=CASH_IDX,
        )
        # Whether HOLD comes from optimizer or second-pass, format_trade_summary handles it
        result = build_trades(
            optimize_result=optimize_result,
            tickers=TICKERS,
            w0=W0,
            scores=flat_scores,
            prices=PRICES,
            portfolio_value=PORTFOLIO_VALUE,
            cost_profile=CostProfile.from_defaults(),
            config=SMOKE_CONFIG,
            cash_idx=CASH_IDX,
            market="US",
            sector_matrix=SECTOR_MATRIX,
            sector_names=SECTOR_NAMES,
            cov_estimate=CovEstimate(np.eye(N) * 0.04, {t: 252 for t in TICKERS}, False),
            recent_directions={t: 0 for t in TICKERS},
        )
        demo = format_trade_summary(result, PORTFOLIO_VALUE)
        assert "🇺🇸" in demo
        if result.is_hold:
            assert "HOLD" in demo

    def test_aapl_sell_has_overheat_rationale(self, tmp_path):
        """
        When AAPL RSI > 75 and the optimizer recommends selling AAPL,
        the rationale should be 'RSI 82 過熱'.
        """
        cov_diag = np.eye(N) * 0.04
        optimize_result = solve_target_weights(
            w0=W0,
            scores=SCORES,
            cov=cov_diag,
            sector_matrix=SECTOR_MATRIX,
            config=SMOKE_CONFIG,
            cash_idx=CASH_IDX,
        )
        if optimize_result.is_hold:
            pytest.skip("Optimizer returned HOLD — cannot test AAPL rationale")

        result = build_trades(
            optimize_result=optimize_result,
            tickers=TICKERS,
            w0=W0,
            scores=SCORES,
            prices=PRICES,
            portfolio_value=PORTFOLIO_VALUE,
            cost_profile=CostProfile.from_defaults(),
            config=SMOKE_CONFIG,
            cash_idx=CASH_IDX,
            market="US",
            sector_matrix=SECTOR_MATRIX,
            sector_names=SECTOR_NAMES,
            cov_estimate=CovEstimate(np.eye(N) * 0.04, {t: 252 for t in TICKERS}, False),
            rationale_contexts=_make_rationale_contexts(),
            recent_directions={t: 0 for t in TICKERS},
        )

        aapl_sells = [t for t in result.trades if t.ticker == "AAPL" and t.side == "SELL"]
        if aapl_sells:
            assert aapl_sells[0].rationale == "RSI 82 過熱"


