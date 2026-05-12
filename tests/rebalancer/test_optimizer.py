"""
Tests for src/rebalancer/optimizer.py

All tests use a synthetic 5-ticker universe:
  T0: high-score equity   (score  2.0)
  T1: moderate equity     (score  0.5)
  T2: cash proxy          (score -1.0, cash_idx)
  T3: bond proxy          (score -1.0)
  T4: safe-haven proxy    (score -1.5, lowest)

Design choices vs. US/TW production defaults
---------------------------------------------
max_position=0.40: US default is 0.20, but with N=5 tickers and sum(w)=1,
  that creates a degenerate single-point polytope (all weights must equal 0.20
  exactly), leaving no room for optimization. 0.40 restores the full polytope.

max_sector=0.60: T0+T1 are the only Tech-sector tickers; cap at 0.60 avoids
  the constraint colliding with sum=1 (which requires non-Tech ≥ 0.40).

lambda_turnover=0.5: US default is 2.0. With N=5, score signal 3.5 (=2.0−(−1.5))
  and L1 cost 2×Δ×lambda_turnover, the optimizer stays at w0 when lambda_turnover≥1.75.
  0.5 lets the score signal dominate so score-responsiveness tests can observe movement.
  Constraint tests (sector cap, position cap, cash floor) are independent of lambda values.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import cvxpy as cp
import numpy as np
import pytest

from src.rebalancer.config import RebalanceConfig
from src.rebalancer.optimizer import OptimizeResult, _apply_min_position, solve_target_weights


N = 5
CASH_IDX = 2  # T2 is the cash / safe-haven position


def _config() -> RebalanceConfig:
    return replace(
        RebalanceConfig.us_default(),
        max_position=0.40,
        max_sector=0.60,
        lambda_turnover=0.5,
    )


def _w0() -> np.ndarray:
    # Strictly feasible: Tech(T0+T1)=0.40≤0.60, all≤0.40, T2=0.20≥cash_floor=0.05
    return np.array([0.20, 0.20, 0.20, 0.20, 0.20])


def _scores() -> np.ndarray:
    return np.array([2.0, 0.5, -1.0, -1.0, -1.5])


def _cov() -> np.ndarray:
    diag = np.array([0.20, 0.15, 0.001, 0.001, 0.001])
    return np.diag(diag)


def _sector_matrix() -> np.ndarray:
    # Tech sector: T0, T1 only. T2–T4 are unconstrained.
    return np.array([[1, 1, 0, 0, 0]], dtype=float)


def _solve(**overrides) -> OptimizeResult:
    kwargs = dict(
        w0=_w0(),
        scores=_scores(),
        cov=_cov(),
        sector_matrix=_sector_matrix(),
        config=_config(),
        cash_idx=CASH_IDX,
    )
    kwargs.update(overrides)
    return solve_target_weights(**kwargs)


# ── Feasibility and basic constraints ────────────────────────────────────────

class TestFeasibleSolve:

    def test_weights_sum_to_1(self):
        result = _solve()
        assert result.w_target.sum() == pytest.approx(1.0, abs=1e-4)

    def test_all_weights_nonneg(self):
        result = _solve()
        assert np.all(result.w_target >= -1e-6)

    def test_max_position_respected(self):
        result = _solve()
        assert np.all(result.w_target <= _config().max_position + 1e-4)

    def test_cash_floor_respected(self):
        result = _solve()
        assert result.w_target[CASH_IDX] >= _config().cash_floor - 1e-4

    def test_sector_cap_respected(self):
        result = _solve()
        tech_weight = float((_sector_matrix() @ result.w_target)[0])
        assert tech_weight <= _config().max_sector + 1e-4

    def test_max_turnover_respected(self):
        result = _solve()
        l1 = float(np.sum(np.abs(result.w_target - _w0())))
        assert l1 <= _config().max_turnover + 1e-4

    def test_solver_status_populated(self):
        result = _solve()
        assert result.solver_status != ""

    def test_is_hold_false_on_feasible(self):
        result = _solve()
        assert result.is_hold is False

    def test_infeasible_false_on_feasible(self):
        result = _solve()
        assert result.infeasible is False

    def test_hold_reason_none_on_feasible(self):
        result = _solve()
        assert result.hold_reason is None


# ── Score drives allocation ───────────────────────────────────────────────────

class TestScoreDrivesAllocation:

    def test_high_score_ticker_weight_increases(self):
        """T0 has score 2.0; starting from 0.20, should increase toward max_position."""
        result = _solve()
        assert result.w_target[0] > _w0()[0]

    def test_lowest_score_ticker_weight_decreases(self):
        """T4 has score −1.5; starting from 0.20, should decrease."""
        result = _solve()
        assert result.w_target[4] < _w0()[4]


# ── HOLD: min_total_turnover ──────────────────────────────────────────────────

class TestMinTotalTurnoverHold:

    def test_hold_when_lambda_score_is_zero(self):
        """
        With lambda_score=0 the score term vanishes; the high turnover penalty
        keeps the optimizer at w0; turnover≈0 < min_total_turnover → HOLD.
        """
        cfg = replace(_config(), lambda_score=0.0)
        result = _solve(config=cfg)
        assert result.is_hold is True
        assert result.hold_reason == "min_turnover"

    def test_hold_returns_w0_when_min_turnover(self):
        cfg = replace(_config(), lambda_score=0.0)
        result = _solve(config=cfg)
        np.testing.assert_allclose(result.w_target, _w0(), atol=1e-3)


# ── Infeasibility (contradictory constraints) ─────────────────────────────────

class TestInfeasibility:

    @staticmethod
    def _infeasible_config() -> RebalanceConfig:
        """cash_floor=0.50 > max_position=0.40 — always infeasible."""
        return replace(_config(), cash_floor=0.50, max_position=0.40)

    def test_infeasible_returns_hold(self):
        result = _solve(config=self._infeasible_config())
        assert result.is_hold is True

    def test_infeasible_returns_w0(self):
        result = _solve(config=self._infeasible_config())
        np.testing.assert_allclose(result.w_target, _w0(), atol=1e-4)

    def test_infeasible_flag_set(self):
        result = _solve(config=self._infeasible_config())
        assert result.infeasible is True

    def test_hold_reason_is_infeasible(self):
        result = _solve(config=self._infeasible_config())
        assert result.hold_reason == "infeasible"

    def test_relaxations_list_populated(self):
        result = _solve(config=self._infeasible_config())
        assert len(result.relaxations_applied) > 0


# ── max_turnover = 0 (hold via min_turnover, not infeasibility) ───────────────

class TestZeroMaxTurnover:

    def test_zero_max_turnover_returns_hold(self):
        """max_turnover=0 → only feasible solution is w=w0 → turnover=0 → HOLD."""
        cfg = replace(_config(), max_turnover=0.0)
        result = _solve(config=cfg)
        assert result.is_hold is True

    def test_zero_max_turnover_returns_w0(self):
        cfg = replace(_config(), max_turnover=0.0)
        result = _solve(config=cfg)
        np.testing.assert_allclose(result.w_target, _w0(), atol=1e-4)


# ── Near-zero variance (cash proxy) ──────────────────────────────────────────

class TestNearZeroVariance:

    def test_no_crash_with_cash_proxy_variance(self):
        """Solver must succeed when T2–T4 have near-zero variance."""
        result = _solve()
        assert result.w_target is not None
        assert result.w_target.sum() == pytest.approx(1.0, abs=1e-4)


# ── min_position post-processing ─────────────────────────────────────────────

class TestMinPositionPostProcessing:

    def test_small_weights_zeroed(self):
        w = np.array([0.50, 0.40, 0.008, 0.002, 0.09])
        result = _apply_min_position(w, min_position=0.02)
        assert result[2] == 0.0
        assert result[3] == 0.0

    def test_renormalized_after_zeroing(self):
        w = np.array([0.50, 0.40, 0.008, 0.002, 0.09])
        result = _apply_min_position(w, min_position=0.02)
        assert result.sum() == pytest.approx(1.0, abs=1e-9)

    def test_unchanged_when_all_above_min(self):
        w = np.array([0.30, 0.25, 0.20, 0.15, 0.10])
        result = _apply_min_position(w, min_position=0.02)
        np.testing.assert_allclose(result, w / w.sum(), atol=1e-9)


# ── SolverError path ─────────────────────────────────────────────────────────

class TestSolverError:

    def test_solver_error_treated_as_infeasible(self):
        """If the solver raises SolverError, the result is a HOLD with infeasible=True."""
        with patch("cvxpy.Problem.solve", side_effect=cp.SolverError("forced crash")):
            result = _solve()
        assert result.is_hold is True
        assert result.infeasible is True


# ── effective_cash_floor override (TW IPO) ───────────────────────────────────

class TestEffectiveCashFloor:

    def test_override_increases_cash_floor_constraint(self):
        """effective_cash_floor=0.30 forces w[CASH_IDX] >= 0.30 (from 0.20)."""
        result = _solve(effective_cash_floor=0.30)
        assert result.w_target[CASH_IDX] >= 0.30 - 1e-4
