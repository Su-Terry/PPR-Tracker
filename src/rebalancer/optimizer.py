"""
Alpha Strategist — QP Optimizer (V2.0)

Implements the global weight optimisation defined in spec §4.2–§4.6 using
cvxpy with the CLARABEL solver.

Objective (spec §4.2):
    min_w  −λ_s · sᵀw  +  λ_v · wᵀΣw  +  λ_t · ‖w − w0‖₁

Constraints (spec §4.3):
    w ≥ 0                            (non-negative weights)
    Σ w = 1                          (fully invested)
    w[i] ≤ max_position              (single-ticker cap, equity only — CASH exempt)
    w[i] ≤ w0[i]  for over-cap i    (trim-only: over-cap positions cannot grow)
    B @ w ≤ max_sector               (sector cap, per L1 sector row)
    w[cash_idx] ≥ effective_cash_floor
    ‖w − w0‖₁ ≤ max_turnover

Post-processing (spec §4.6):
    Weights below min_position are zeroed, remaining weights re-normalised.

Infeasibility relaxation (spec §11 + D-S5-16/17/18):
    When over-cap positions exist (w0[i] > max_position, i ≠ cash_idx):
        max_turnover → max_sector → max_position → cash_floor  (4 steps)
    Otherwise:
        max_turnover → max_sector → cash_floor                 (3 steps)

    max_position relaxation: +5 pp per step, hard cap at _MAX_POSITION_RELAX_CAP=0.30.
    On final infeasibility returns w0 with is_hold=True, infeasible=True.
    hold_reason="manual_trim_required" when over-cap positions caused the failure;
    hold_reason="infeasible" for clean portfolios.
    Deviation from spec §4.6 (which says raise) — disclosed as spec deviation D2.

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

_RELAX_STEP             = 0.05
_MAX_POSITION_RELAX_CAP = 0.30   # absolute ceiling for max_position relaxation (D-S5-18)


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
        "infeasible"           — solver infeasible, no over-cap positions
        "manual_trim_required" — solver infeasible, over-cap positions present
        "min_turnover"         — ‖w* − w0‖₁ < config.min_total_turnover
        None                   — actionable result (not a HOLD)
    infeasible:
        True only when infeasibility triggered the HOLD. Distinct from
        hold_reason for programmatic checks (e.g. Sprint 4 /why endpoint).
    relaxations_applied:
        Human-readable list of the constraint relaxations that were attempted
        before declaring infeasibility. Empty when the first solve succeeded.
        Example: ["max_turnover: 0.40→0.45", "max_position: 0.20→0.25"]
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
        High score = more attractive.
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
    n = len(w0)

    # Equity positions whose current weight exceeds the position cap (CASH exempt).
    # These positions may hold or trim but cannot grow — see D-S5-17.
    over_cap_indices: list[int] = [
        i for i in range(n)
        if i != cash_idx and w0[i] > config.max_position
    ]

    # 4-step relaxation chain when over-cap positions exist, 3-step otherwise.
    max_relax = 4 if over_cap_indices else 3

    relaxations: list[str] = []
    current_max_turnover = config.max_turnover
    current_max_sector   = config.max_sector
    current_max_position = config.max_position
    current_cash_floor   = cash_floor

    for attempt in range(max_relax + 1):
        result = _solve_once(
            w0=w0,
            scores=scores,
            cov=cov,
            sector_matrix=sector_matrix,
            config=config,
            cash_idx=cash_idx,
            max_turnover=current_max_turnover,
            max_sector=current_max_sector,
            max_position=current_max_position,
            cash_floor=current_cash_floor,
            over_cap_indices=over_cap_indices,
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

        if attempt < max_relax:
            relax_msg = _relax_one(
                attempt=attempt,
                current_max_turnover=current_max_turnover,
                current_max_sector=current_max_sector,
                current_max_position=current_max_position,
                current_cash_floor=current_cash_floor,
                has_over_cap=bool(over_cap_indices),
            )
            relaxations.append(relax_msg)
            (
                current_max_turnover,
                current_max_sector,
                current_max_position,
                current_cash_floor,
            ) = _updated_params(
                attempt=attempt,
                current_max_turnover=current_max_turnover,
                current_max_sector=current_max_sector,
                current_max_position=current_max_position,
                current_cash_floor=current_cash_floor,
                has_over_cap=bool(over_cap_indices),
            )

    hold_reason = "manual_trim_required" if over_cap_indices else "infeasible"
    logger.warning(
        "[OPT] %s after %d relaxation attempts: %s",
        hold_reason,
        max_relax,
        relaxations,
    )
    return OptimizeResult(
        w_target=w0.copy(),
        is_hold=True,
        hold_reason=hold_reason,
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
    max_position: float,
    cash_floor: float,
    over_cap_indices: list[int],
) -> tuple[np.ndarray, str] | None:
    """
    Attempt one QP solve. Returns (w_value, status) on success, None on
    INFEASIBLE / UNBOUNDED / error.
    """
    assert cash_idx not in over_cap_indices, (
        "CASH must not appear in over_cap_indices "
        "(verified by solve_target_weights filter)"
    )

    n = len(w0)
    w = cp.Variable(n, nonneg=True)

    objective = cp.Minimize(
        -config.lambda_score * (scores @ w)
        + config.lambda_var * cp.quad_form(w, cp.psd_wrap(cov))
        + config.lambda_turnover * cp.norm1(w - w0)
    )

    # Position cap excludes CASH (D-S5-16): cash is bounded only by cash_floor below.
    # Trim-only (D-S5-17): over-cap positions may hold or trim, never grow.
    constraints: list = [
        cp.sum(w) == 1,
        *[w[i] <= max_position for i in range(n) if i != cash_idx],
        *[w[i] <= float(w0[i]) for i in over_cap_indices],
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
    current_max_position: float,
    current_cash_floor: float,
    has_over_cap: bool,
) -> str:
    """Return a human-readable description of the relaxation being applied."""
    if attempt == 0:
        new_val = current_max_turnover + _RELAX_STEP
        msg = f"max_turnover: {current_max_turnover:.2f}→{new_val:.2f}"
    elif attempt == 1:
        new_val = current_max_sector + _RELAX_STEP
        msg = f"max_sector: {current_max_sector:.2f}→{new_val:.2f}"
    elif attempt == 2 and has_over_cap:
        new_val = min(current_max_position + _RELAX_STEP, _MAX_POSITION_RELAX_CAP)
        msg = f"max_position: {current_max_position:.2f}→{new_val:.2f}"
    elif (attempt == 2 and not has_over_cap) or (attempt == 3 and has_over_cap):
        new_val = max(current_cash_floor - _RELAX_STEP, 0.0)
        msg = f"cash_floor: {current_cash_floor:.2f}→{new_val:.2f}"
    else:
        raise ValueError(
            f"Unexpected relaxation state: attempt={attempt}, "
            f"has_over_cap={has_over_cap}"
        )
    logger.warning("[OPT] Relaxing constraint — %s", msg)
    return msg


def _updated_params(
    attempt: int,
    current_max_turnover: float,
    current_max_sector: float,
    current_max_position: float,
    current_cash_floor: float,
    has_over_cap: bool,
) -> tuple[float, float, float, float]:
    if attempt == 0:
        return current_max_turnover + _RELAX_STEP, current_max_sector, current_max_position, current_cash_floor
    elif attempt == 1:
        return current_max_turnover, current_max_sector + _RELAX_STEP, current_max_position, current_cash_floor
    elif attempt == 2 and has_over_cap:
        new_mp = min(current_max_position + _RELAX_STEP, _MAX_POSITION_RELAX_CAP)
        return current_max_turnover, current_max_sector, new_mp, current_cash_floor
    elif (attempt == 2 and not has_over_cap) or (attempt == 3 and has_over_cap):
        return current_max_turnover, current_max_sector, current_max_position, max(current_cash_floor - _RELAX_STEP, 0.0)
    else:
        raise ValueError(
            f"Unexpected relaxation state: attempt={attempt}, "
            f"has_over_cap={has_over_cap}"
        )


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
