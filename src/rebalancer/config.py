"""
Alpha Strategist — Rebalancer Configuration (V2.0)

Per-market constraint parameters and lambda weights for the QP optimizer.
All constraints are defined in spec §4.3. US and TW are always run as
independent optimizations (spec §3.1); never share a single RebalanceConfig.

min_total_turnover uses ‖w* − w0‖₁ (L1 norm): buy deltas and sell deltas
are both counted, so 0.02 equals roughly 1% one-way turnover equivalent.
Do not confuse this with a one-way percentage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RebalanceConfig:
    """
    Frozen constraint and weight parameters for one market's QP solve.

    Attributes
    ----------
    market:
        "US" or "TW". The two markets are always solved independently.
    lambda_score:
        Weight on the score maximization term (−λ_s · sᵀw).
    lambda_var:
        Weight on the variance minimization term (λ_v · wᵀΣw).
    lambda_turnover:
        Weight on the L1 turnover penalty (λ_t · ‖w − w0‖₁).
    max_position:
        Maximum weight for any single ticker.
    max_sector:
        Maximum aggregate weight for any L1 sector.
    cash_floor:
        Minimum weight held in the cash / safe-haven proxy position.
        For TW, pass effective_cash_floor to solve_target_weights() instead
        to incorporate pending IPO reservations (spec §4.3).
    max_turnover:
        Maximum allowed ‖w − w0‖₁ in a single rebalance.
    min_position:
        Positions below this threshold are zeroed and the portfolio is
        re-normalised after the QP solve (spec §4.6).
    min_trade_amount:
        Notional threshold (USD for US, TWD for TW) below which individual
        trades are suppressed. Evaluated in Sprint 3 trade_builder.
    min_total_turnover:
        If ‖w* − w0‖₁ < this value the result is declared HOLD (spec §4.7).
        Uses the L1 norm (buy + sell deltas summed separately), so 0.02 ≈ 1%
        one-way equivalent. Do NOT confuse with a one-way turnover percentage.
    lookback_days_min:
        Minimum common-window rows of log returns required to use Ledoit-Wolf
        shrinkage. Below this, cov_estimator falls back to a diagonal matrix.
    lookback_days_ideal:
        Target lookback for full Ledoit-Wolf estimation (spec §4.1, §11).
    """

    market: Literal["US", "TW"]
    lambda_score: float
    lambda_var: float
    lambda_turnover: float
    max_position: float
    max_sector: float
    cash_floor: float
    max_turnover: float
    min_position: float
    min_trade_amount: float
    min_total_turnover: float = 0.02
    lookback_days_min: int = 60
    lookback_days_ideal: int = 252

    @classmethod
    def us_default(cls) -> "RebalanceConfig":
        """US market defaults per spec §4.3."""
        return cls(
            market="US",
            lambda_score=1.0,
            lambda_var=0.5,
            lambda_turnover=2.0,
            max_position=0.20,
            max_sector=0.40,
            cash_floor=0.05,
            max_turnover=0.40,
            min_position=0.02,
            min_trade_amount=100.0,
        )

    @classmethod
    def tw_default(cls) -> "RebalanceConfig":
        """TW market defaults per spec §4.3."""
        return cls(
            market="TW",
            lambda_score=1.0,
            lambda_var=0.5,
            lambda_turnover=3.0,
            max_position=0.30,
            max_sector=0.50,
            cash_floor=0.10,
            max_turnover=0.30,
            min_position=0.05,
            min_trade_amount=3000.0,
        )
