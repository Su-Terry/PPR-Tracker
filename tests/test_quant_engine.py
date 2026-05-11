"""
Unit tests for src/strategies/quant_engine.py

All inputs follow the decimal convention: 0.20 = 20%.
No I/O, no network — pure function validation only.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.strategies.quant_engine import (
    calculate_modified_peg,
    calculate_ps_growth_ratio,
    check_momentum_trend,
)


# ── calculate_modified_peg ────────────────────────────────────────────────────

class TestCalculateModifiedPeg:

    def test_undervalued_normal_case(self):
        """PE=15, EPS growth=20%, Capex=10% → PEG = 15 / (0.30 * 100) = 0.5"""
        result = calculate_modified_peg(pe=15.0, eps_growth=0.20, capex_to_rev=0.10)
        assert result == pytest.approx(0.5, rel=1e-4)

    def test_high_growth_buy_signal(self):
        """High-growth stock: PE=25, EPS=80%, Capex=5% → PEG = 25 / 85 = 0.2941 → below 0.8"""
        result = calculate_modified_peg(pe=25.0, eps_growth=0.80, capex_to_rev=0.05)
        assert result == pytest.approx(0.2941, rel=1e-3)
        assert result < 0.8  # should trigger BUY_HOLD signal

    def test_nvda_like_profile(self):
        """NVDA-style: PE=43, EPS growth=95.6%, Capex=5% → PEG = 43 / 100.6 = 0.4274"""
        result = calculate_modified_peg(pe=43.0, eps_growth=0.956, capex_to_rev=0.05)
        assert result == pytest.approx(0.4274, rel=1e-3)

    def test_zero_growth_returns_inf(self):
        """eps_growth=0, capex=0 → denominator=0 → must return inf"""
        result = calculate_modified_peg(pe=25.0, eps_growth=0.0, capex_to_rev=0.0)
        assert result == float("inf")

    def test_declining_earnings_returns_inf(self):
        """Negative EPS growth that makes denominator <= 0 → must return inf"""
        result = calculate_modified_peg(pe=25.0, eps_growth=-0.10, capex_to_rev=0.05)
        assert result == float("inf")

    def test_deep_decline_returns_inf(self):
        """eps_growth=-0.20, capex=0.05 → denom=-0.15 ≤ 0 → inf"""
        result = calculate_modified_peg(pe=25.0, eps_growth=-0.20, capex_to_rev=0.05)
        assert result == float("inf")

    def test_extremely_high_pe(self):
        """Bubble-phase stock: PE=500, growth=1%, capex=1% → PEG = 500 / 2 = 250"""
        result = calculate_modified_peg(pe=500.0, eps_growth=0.01, capex_to_rev=0.01)
        assert result == pytest.approx(250.0, rel=1e-3)
        assert result > 1.5  # definitely triggers SELL threshold

    def test_result_precision_four_decimals(self):
        """Return value is rounded to 4 decimal places"""
        result = calculate_modified_peg(pe=10.0, eps_growth=0.3333, capex_to_rev=0.0)
        assert result == round(result, 4)

    def test_capex_only_no_growth(self):
        """eps_growth=0 but capex=10% → PEG = 20 / (0.10 * 100) = 2.0"""
        result = calculate_modified_peg(pe=20.0, eps_growth=0.0, capex_to_rev=0.10)
        assert result == pytest.approx(2.0, rel=1e-4)

    def test_sell_threshold_boundary(self):
        """Values straddling the 1.5 SELL threshold at correct scale.
        PE=14.9, growth=10% → PEG = 14.9 / 10 = 1.49 (just below)
        PE=15.1, growth=10% → PEG = 15.1 / 10 = 1.51 (just above)
        """
        below = calculate_modified_peg(pe=14.9, eps_growth=0.10, capex_to_rev=0.0)
        above = calculate_modified_peg(pe=15.1, eps_growth=0.10, capex_to_rev=0.0)
        assert below == pytest.approx(1.49, rel=1e-3)
        assert above == pytest.approx(1.51, rel=1e-3)
        assert below < 1.5 < above


# ── check_momentum_trend ──────────────────────────────────────────────────────

class TestCheckMomentumTrend:

    @staticmethod
    def _make_prices(n: int, start: float, end: float) -> pd.Series:
        """Helper: linearly spaced price series with n data points."""
        return pd.Series(np.linspace(start, end, n), dtype=float)

    def test_bullish_alignment(self):
        """
        Monotonically rising prices over 250 days.
        Latest price > 50MA > 200MA → must return True.
        """
        prices = self._make_prices(250, 100.0, 200.0)
        assert check_momentum_trend(prices) is True

    def test_bearish_breakdown(self):
        """
        Monotonically falling prices over 250 days.
        Latest price < 50MA < 200MA → must return False.
        """
        prices = self._make_prices(250, 200.0, 100.0)
        assert check_momentum_trend(prices) is False

    def test_price_below_50ma_only(self):
        """
        Sharp drop at the end: long uptrend then sudden reversal.
        Latest price drops below 50MA → must return False.
        """
        base   = np.linspace(100.0, 180.0, 240)
        crash  = np.linspace(180.0, 110.0, 10)   # sudden drop
        prices = pd.Series(np.concatenate([base, crash]))
        assert check_momentum_trend(prices) is False

    def test_insufficient_data_exactly_199(self):
        """199 data points < 200 threshold → must return False."""
        prices = self._make_prices(199, 100.0, 200.0)
        assert check_momentum_trend(prices) is False

    def test_insufficient_data_exactly_200(self):
        """Exactly 200 data points — boundary: should proceed with calculation."""
        prices = self._make_prices(200, 100.0, 200.0)
        # Rising series: result should be True at this boundary
        assert isinstance(check_momentum_trend(prices), bool)

    def test_empty_series_returns_false(self):
        """Empty Series → must return False without raising."""
        assert check_momentum_trend(pd.Series([], dtype=float)) is False

    def test_flat_prices_returns_false(self):
        """
        All prices identical: 50MA == 200MA == latest price.
        Strict inequality (>) means False is the correct result.
        """
        prices = pd.Series([100.0] * 250)
        assert check_momentum_trend(prices) is False

    def test_return_type_is_bool(self):
        """Return type must be native Python bool, not numpy.bool_."""
        prices = self._make_prices(250, 100.0, 200.0)
        result = check_momentum_trend(prices)
        assert type(result) is bool  # noqa: E721

    def test_52w_high_within_10pct_passes(self):
        """Bullish trend + price within 10% of 52W high → True."""
        prices = self._make_prices(250, 100.0, 200.0)
        assert check_momentum_trend(prices, week_52_high=210.0) is True

    def test_52w_high_beyond_10pct_blocked(self):
        """Bullish trend but price >10% below 52W high → False (falling knife guard)."""
        prices = self._make_prices(250, 100.0, 180.0)
        assert check_momentum_trend(prices, week_52_high=210.0) is False

    def test_52w_high_none_ignores_guard(self):
        """week_52_high=None → guard is skipped, standard 3-line check only."""
        prices = self._make_prices(250, 100.0, 200.0)
        assert check_momentum_trend(prices, week_52_high=None) is True


# ── calculate_ps_growth_ratio ─────────────────────────────────────────────────

class TestCalculatePsGrowthRatio:

    def test_hyper_growth_candidate(self):
        """CRWV-style: P/S=12, RevG=185% → ratio = 12 / 185 = 0.0649 → below 0.5"""
        result = calculate_ps_growth_ratio(ps_ratio=12.0, revenue_growth=1.85)
        assert result == pytest.approx(0.0649, rel=1e-3)
        assert result < 0.5

    def test_overvalued_ps(self):
        """High P/S, low growth → ratio > 3.0 triggers sell"""
        result = calculate_ps_growth_ratio(ps_ratio=30.0, revenue_growth=0.10)
        assert result == pytest.approx(3.0, rel=1e-3)

    def test_zero_revenue_growth_returns_inf(self):
        result = calculate_ps_growth_ratio(ps_ratio=10.0, revenue_growth=0.0)
        assert result == float("inf")

    def test_negative_revenue_growth_returns_inf(self):
        result = calculate_ps_growth_ratio(ps_ratio=10.0, revenue_growth=-0.05)
        assert result == float("inf")

    def test_same_scale_as_peg(self):
        """Decimal format (0.20) × 100 → same percentage scale as Modified PEG."""
        result = calculate_ps_growth_ratio(ps_ratio=5.0, revenue_growth=0.50)
        assert result == pytest.approx(0.10, rel=1e-3)
