"""
Tests for src/rebalancer/conviction.py

Pure function tests — no I/O, no network.
"""

from __future__ import annotations

import pytest

from src.rebalancer.conviction import (
    ConstraintBinding,
    ConvictionContext,
    _normalize_constraint_binding,
    _normalize_consistency,
    _normalize_cov_certainty,
    _normalize_score_delta,
    _normalize_timing,
    compute_convictions,
    execution_tier,
)


# ── compute_convictions: range and rounding ───────────────────────────────────

class TestComputeConvictionsRange:

    def test_output_in_0_to_10_range(self):
        contexts = [
            ConvictionContext(score_delta=5.0, bindings=[], days_available=252, days_since_last_trade=30),
            ConvictionContext(score_delta=-2.0, bindings=[], days_available=0, days_since_last_trade=0),
        ]
        for c in compute_convictions(contexts):
            assert 0.0 <= c <= 10.0

    def test_output_rounded_to_1_decimal(self):
        contexts = [
            ConvictionContext(score_delta=1.0, bindings=[], days_available=126, days_since_last_trade=15),
        ]
        result = compute_convictions(contexts)[0]
        assert result == round(result, 1)

    def test_empty_list_returns_empty(self):
        assert compute_convictions([]) == []

    def test_single_context_returns_neutral_score_delta(self):
        """Single trade → percentile rank = 0.5 (neutral)."""
        ctx = ConvictionContext(
            score_delta=999.0, bindings=[], days_available=252, days_since_last_trade=30
        )
        result = compute_convictions([ctx])[0]
        # c_score=0.5, c_binding=0.0, c_cov=1.0, c_consist=0.5, c_timing=1.0
        # raw = 0.40*0.5 + 0.20*0.0 + 0.15*1.0 + 0.15*0.5 + 0.10*1.0
        #      = 0.20 + 0.00 + 0.15 + 0.075 + 0.10 = 0.525
        # conviction = round(5.25, 1) = 5.3
        assert result == pytest.approx(5.3, abs=0.1)

    def test_order_preserved(self):
        contexts = [
            ConvictionContext(score_delta=3.0, bindings=[], days_available=252, days_since_last_trade=30),
            ConvictionContext(score_delta=1.0, bindings=[], days_available=252, days_since_last_trade=30),
        ]
        results = compute_convictions(contexts)
        assert len(results) == 2
        assert results[0] > results[1]  # higher score_delta → higher conviction


# ── score_delta percentile rank ───────────────────────────────────────────────

class TestNormalizeScoreDelta:

    def test_worst_delta_returns_zero(self):
        assert _normalize_score_delta(-10.0, [-10.0, 0.0, 5.0]) == pytest.approx(0.0)

    def test_best_delta_approaches_one(self):
        # rank of 5.0 in [-10, 0, 5] = 2 → 2/3 ≈ 0.667
        assert _normalize_score_delta(5.0, [-10.0, 0.0, 5.0]) == pytest.approx(2 / 3)

    def test_single_delta_returns_half(self):
        assert _normalize_score_delta(1.0, [1.0]) == pytest.approx(0.5)

    def test_empty_all_deltas_returns_half(self):
        assert _normalize_score_delta(1.0, []) == pytest.approx(0.5)

    def test_tie_counting(self):
        """Tied values: rank counts strictly less-than."""
        all_d = [1.0, 1.0, 1.0]
        assert _normalize_score_delta(1.0, all_d) == pytest.approx(0.0)


# ── constraint binding ────────────────────────────────────────────────────────

class TestNormalizeConstraintBinding:

    def test_empty_bindings_returns_zero(self):
        assert _normalize_constraint_binding([]) == pytest.approx(0.0)

    def test_max_ratio_selected(self):
        bindings = [
            ConstraintBinding("sector:Tech", 0.80),
            ConstraintBinding("position:NVDA", 0.95),
            ConstraintBinding("sector:Bond", 0.30),
        ]
        assert _normalize_constraint_binding(bindings) == pytest.approx(0.95)

    def test_single_binding(self):
        bindings = [ConstraintBinding("sector:半導體", 0.72)]
        assert _normalize_constraint_binding(bindings) == pytest.approx(0.72)

    def test_all_zero_ratios(self):
        bindings = [
            ConstraintBinding("sector:A", 0.0),
            ConstraintBinding("sector:B", 0.0),
        ]
        assert _normalize_constraint_binding(bindings) == pytest.approx(0.0)


# ── cov certainty ─────────────────────────────────────────────────────────────

class TestNormalizeCovCertainty:

    def test_252_days_returns_1(self):
        assert _normalize_cov_certainty(252) == pytest.approx(1.0)

    def test_0_days_returns_0(self):
        assert _normalize_cov_certainty(0) == pytest.approx(0.0)

    def test_126_days_returns_half(self):
        assert _normalize_cov_certainty(126) == pytest.approx(0.5)

    def test_above_ideal_capped_at_1(self):
        assert _normalize_cov_certainty(500) == pytest.approx(1.0)

    def test_60_days_floor_is_below_1(self):
        assert _normalize_cov_certainty(60) < 1.0


# ── consistency stub ──────────────────────────────────────────────────────────

class TestNormalizeConsistency:

    def test_returns_half(self):
        assert _normalize_consistency() == pytest.approx(0.5)


# ── timing ────────────────────────────────────────────────────────────────────

class TestNormalizeTiming:

    def test_30_days_returns_1(self):
        assert _normalize_timing(30) == pytest.approx(1.0)

    def test_0_days_returns_0(self):
        assert _normalize_timing(0) == pytest.approx(0.0)

    def test_15_days_returns_half(self):
        assert _normalize_timing(15) == pytest.approx(0.5)

    def test_above_30_capped_at_1(self):
        assert _normalize_timing(999) == pytest.approx(1.0)


class TestNormalizeCovCertaintyEdgeCases:

    def test_ideal_zero_returns_1(self):
        """ideal=0 would cause division by zero; guard returns 1.0 (max certainty)."""
        assert _normalize_cov_certainty(0, ideal=0) == pytest.approx(1.0)


# ── execution tier ────────────────────────────────────────────────────────────

class TestExecutionTier:

    def test_execute_at_6(self):
        assert execution_tier(6.0) == "Execute"

    def test_execute_above_6(self):
        assert execution_tier(9.5) == "Execute"

    def test_watch_at_4(self):
        assert execution_tier(4.0) == "Watch"

    def test_watch_at_5_9(self):
        assert execution_tier(5.9) == "Watch"

    def test_skip_below_4(self):
        assert execution_tier(3.9) == "Skip"

    def test_skip_at_0(self):
        assert execution_tier(0.0) == "Skip"
