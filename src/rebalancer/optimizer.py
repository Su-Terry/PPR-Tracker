"""
Alpha Strategist — QP Optimizer (V2.0)

Implements the global weight optimisation defined in spec §4.2–§4.6 using
cvxpy with the CLARABEL solver.

Objective (spec §4.2):
    min_w  −λ_s · sᵀw  +  λ_v · wᵀΣw  +  λ_t · ‖w − w0‖₁

Constraints (spec §4.3):
    w ≥ 0                            (non-negative weights)
    Σ w = 1                          (fully invested)
    w ≤ max_position                 (single-ticker cap)
    B @ w ≤ max_sector               (sector cap, per L1 sector row)
    w[cash_idx] ≥ effective_cash_floor
    ‖w − w0‖₁ ≤ max_turnover

Post-processing (spec §4.6):
    Weights below min_position are zeroed, remaining weights re-normalised.

Infeasibility relaxation (spec §11):
    Relax max_turnover → max_sector → cash_floor, each by +5 pp, max 3 attempts.
    On final infeasibility the function returns w0 with is_hold=True, infeasible=True
    rather than raising, so Sprint 4 dashboard can display the HOLD reason.
    Deviation from spec §4.6 (which says raise) — disclosed in spec deviation D2.

HOLD detection (spec §4.7):
    If ‖w* − w0‖₁ < config.min_total_turnover the result is HOLD.
    min_total_turnover uses the L1 norm (buy + sell deltas summed separately),
    so 0.02 equals ~1% one-way turnover equivalent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cvxpy as cp
import numpy as np

from src.rebalancer.config import RebalanceConfig

logger = logging.getLogger(__name__)

_RELAX_STEP = 0.05
_MAX_RELAX_ATTEMPTS = 3


@dataclass
class OptimizeResult:
    """
    Output of solve_target_weights().

    Attributes
    ----------
    w_target:
        Optimal weight vector (length N, sums to 1, non-negative).
        When is_hold=True due to infeasibility, this equals w0.
    is_hold:
        True when the result is a HOLD recommendation — either because
        the total turnover is below min_total_turnover, or because the
        solver was infeasible after all relaxation attempts.
    hold_reason:
        "infeasible"   — solver could not find a feasible solution
        "min_turnover" — ‖w* − w0‖₁ < config.min_total_turnover
        None           — actionable result (not a HOLD)
    infeasible:
        True only when infeasibility triggered the HOLD. Distinct from
        hold_reason for programmatic checks (e.g. Sprint 4 /why endpoint).
    relaxations_applied:
        Human-readable list of the constraint relaxations that were attempted
        before declaring infeasibility. Empty when the first solve succeeded.
        Example: ["max_turnover: 0.40→0.45", "max_sector: 0.40→0.45"]
    solver_status:
        The cvxpy problem status string from the final solve attempt.
    """

    w_target: np.ndarray
    is_hold: bool
    hold_reason: str | None
    infeasible: bool
    relaxations_applied: list[str] = field(default_factory=list)
    solver_status: str = ""


def solve_target_weights(
    w0: np.ndarray,
    scores: np.ndarray,
    cov: np.ndarray,
    sector_matrix: np.ndarray,
    config: RebalanceConfig,
    cash_idx: int,
    effective_cash_floor: float | None = None,
) -> OptimizeResult:
    """
    Solve the portfolio rebalancing QP and return target weights.

    Parameters
    ----------
    w0:
        Current weight vector (length N, must sum to ~1, non-negative).
    scores:
        Score vector (length N), z-score standardised by the caller.
        High score = more attractive. Assembled from quant_engine in Sprint 5.
    cov:
        N×N annualised covariance matrix from CovEstimate.matrix.
    sector_matrix:
        K×N indicator matrix where B[k, i] = 1 if ticker i belongs to sector k.
    config:
        RebalanceConfig for the target market.
    cash_idx:
        Index of the cash / safe-haven proxy in the ticker universe.
    effective_cash_floor:
        If provided, overrides config.cash_floor. Used for TW IPO cash
        reservations (spec §4.3 dynamic cash floor).

    Returns
    -------
    OptimizeResult
    """
    cash_floor = effective_cash_floor if effective_cash_floor is not None else config.cash_floor

    relaxations: list[str] = []
    current_max_turnover = config.max_turnover
    current_max_sector = config.max_sector
    current_cash_floor = cash_floor

    for attempt in range(_MAX_RELAX_ATTEMPTS + 1):
        result = _solve_once(
            w0=w0,
            scores=scores,
            cov=cov,
            sector_matrix=sector_matrix,
            config=config,
            cash_idx=cash_idx,
            max_turnover=current_max_turnover,
            max_sector=current_max_sector,
            cash_floor=current_cash_floor,
        )

        if result is not None:
            w_opt, status = result
            w_opt = _apply_min_position(w_opt, config.min_position)
            l1_turnover = float(np.sum(np.abs(w_opt - w0)))

            if l1_turnover < config.min_total_turnover:
                logger.info(
                    "[OPT] HOLD: turnover %.4f < min_total_turnover %.4f",
                    l1_turnover,
                    config.min_total_turnover,
                )
                return OptimizeResult(
                    w_target=w0.copy(),
                    is_hold=True,
                    hold_reason="min_turnover",
                    infeasible=False,
                    relaxations_applied=relaxations,
                    solver_status=status,
                )

            return OptimizeResult(
                w_target=w_opt,
                is_hold=False,
                hold_reason=None,
                infeasible=False,
                relaxations_applied=relaxations,
                solver_status=status,
            )

        if attempt < _MAX_RELAX_ATTEMPTS:
            relaxations.append(_relax_one(
                attempt=attempt,
                current_max_turnover=current_max_turnover,
                current_max_sector=current_max_sector,
                current_cash_floor=current_cash_floor,
                config=config,
            ))
            current_max_turnover, current_max_sector, current_cash_floor = _updated_params(
                attempt=attempt,
                current_max_turnover=current_max_turnover,
                current_max_sector=current_max_sector,
                current_cash_floor=current_cash_floor,
            )

    logger.warning(
        "[OPT] Infeasible after %d relaxation attempts: %s",
        _MAX_RELAX_ATTEMPTS,
        relaxations,
    )
    return OptimizeResult(
        w_target=w0.copy(),
        is_hold=True,
        hold_reason="infeasible",
        infeasible=True,
        relaxations_applied=relaxations,
        solver_status="infeasible",
    )


def _solve_once(
    w0: np.ndarray,
    scores: np.ndarray,
    cov: np.ndarray,
    sector_matrix: np.ndarray,
    config: RebalanceConfig,
    cash_idx: int,
    max_turnover: float,
    max_sector: float,
    cash_floor: float,
) -> tuple[np.ndarray, str] | None:
    """
    Attempt one QP solve. Returns (w_value, status) on success, None on
    INFEASIBLE / UNBOUNDED / error.
    """
    n = len(w0)
    w = cp.Variable(n, nonneg=True)

    objective = cp.Minimize(
        -config.lambda_score * (scores @ w)
        + config.lambda_var * cp.quad_form(w, cp.psd_wrap(cov))
        + config.lambda_turnover * cp.norm1(w - w0)
    )

    constraints = [
        cp.sum(w) == 1,
        w <= config.max_position,
        sector_matrix @ w <= max_sector,
        w[cash_idx] >= cash_floor,
        cp.norm1(w - w0) <= max_turnover,
    ]

    prob = cp.Problem(objective, constraints)
    try:
        prob.solve(solver=cp.CLARABEL)
    except cp.SolverError as exc:
        logger.warning("[OPT] Solver error: %s", exc)
        return None

    if prob.status in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE) and w.value is not None:
        return w.value, prob.status

    logger.debug("[OPT] Solver status: %s", prob.status)
    return None


def _relax_one(
    attempt: int,
    current_max_turnover: float,
    current_max_sector: float,
    current_cash_floor: float,
    config: RebalanceConfig,
) -> str:
    """Return a human-readable description of which relaxation will be applied."""
    if attempt == 0:
        new_val = current_max_turnover + _RELAX_STEP
        msg = f"max_turnover: {current_max_turnover:.2f}→{new_val:.2f}"
    elif attempt == 1:
        new_val = current_max_sector + _RELAX_STEP
        msg = f"max_sector: {current_max_sector:.2f}→{new_val:.2f}"
    else:
        new_val = max(current_cash_floor - _RELAX_STEP, 0.0)
        msg = f"cash_floor: {current_cash_floor:.2f}→{new_val:.2f}"
    logger.warning("[OPT] Relaxing constraint — %s", msg)
    return msg


def _updated_params(
    attempt: int,
    current_max_turnover: float,
    current_max_sector: float,
    current_cash_floor: float,
) -> tuple[float, float, float]:
    if attempt == 0:
        return current_max_turnover + _RELAX_STEP, current_max_sector, current_cash_floor
    elif attempt == 1:
        return current_max_turnover, current_max_sector + _RELAX_STEP, current_cash_floor
    else:
        return current_max_turnover, current_max_sector, max(current_cash_floor - _RELAX_STEP, 0.0)


def _apply_min_position(w: np.ndarray, min_position: float) -> np.ndarray:
    """
    Zero out weights below min_position and re-normalise to sum to 1.
    If all weights are below min_position (degenerate case) returns w unchanged.
    """
    w = w.copy()
    w[w < min_position] = 0.0
    total = w.sum()
    if total > 0:
        w /= total
    return w
