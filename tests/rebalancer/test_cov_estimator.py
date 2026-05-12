"""
Tests for src/rebalancer/cov_estimator.py

All tests use synthetic price series — no network, no I/O.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.rebalancer.config import RebalanceConfig
from src.rebalancer.cov_estimator import (
    CovEstimate,
    _ANNUALISE,
    _MIN_SAMPLES_FOR_PRIOR,
    _PRIOR_DAILY_VAR,
    _diagonal_fallback_vars,
    estimate_covariance,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_prices(n_rows: int, n_tickers: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0005, 0.015, (n_rows, n_tickers))
    prices = np.cumprod(1 + returns, axis=0) * 100
    cols = [f"T{i}" for i in range(n_tickers)]
    return pd.DataFrame(prices, columns=cols)


def _config() -> RebalanceConfig:
    return RebalanceConfig.us_default()


def _is_psd(matrix: np.ndarray, tol: float = 1e-8) -> bool:
    """Returns True if matrix is positive semi-definite (all eigenvalues ≥ -tol)."""
    eigvals = np.linalg.eigvalsh(matrix)
    return bool(np.all(eigvals >= -tol))


# ── Ledoit-Wolf path ──────────────────────────────────────────────────────────

class TestLedoitWolfPath:

    def test_psd(self):
        prices = _make_prices(260, 4)
        est = estimate_covariance(prices, _config())
        assert _is_psd(est.matrix)

    def test_symmetric(self):
        prices = _make_prices(260, 4)
        est = estimate_covariance(prices, _config())
        assert np.allclose(est.matrix, est.matrix.T, atol=1e-10)

    def test_shape(self):
        prices = _make_prices(260, 5)
        est = estimate_covariance(prices, _config())
        assert est.matrix.shape == (5, 5)

    def test_used_ledoit_wolf_flag_true(self):
        prices = _make_prices(260, 3)
        est = estimate_covariance(prices, _config())
        assert est.used_ledoit_wolf is True

    def test_annualised_scale(self):
        """Ledoit-Wolf result should be ~252× the raw daily covariance scale."""
        prices = _make_prices(260, 3)
        est = estimate_covariance(prices, _config())
        # For ~1.5% daily vol tickers, annualised variance should be in (0.01, 2.0)
        diag = np.diag(est.matrix)
        assert np.all(diag > 0.01)
        assert np.all(diag < 2.0)

    def test_days_per_ticker_full_history(self):
        prices = _make_prices(100, 3)  # 100 rows → 99 return rows
        # common window = 99 < 60 → diagonal fallback, but days_per_ticker still filled
        est = estimate_covariance(prices, _config())
        for count in est.days_per_ticker.values():
            assert count == 99

    def test_days_per_ticker_with_nans(self):
        prices = _make_prices(300, 3)
        prices.iloc[:50, 2] = np.nan  # T2 missing first 50 rows
        est = estimate_covariance(prices, _config())
        assert est.days_per_ticker["T2"] < est.days_per_ticker["T0"]


# ── Diagonal fallback path ────────────────────────────────────────────────────

class TestDiagonalFallback:

    def test_used_ledoit_wolf_flag_false(self):
        prices = _make_prices(30, 3)  # 29 common rows < 60
        est = estimate_covariance(prices, _config())
        assert est.used_ledoit_wolf is False

    def test_diagonal_shape_preserved(self):
        prices = _make_prices(30, 4)
        est = estimate_covariance(prices, _config())
        assert est.matrix.shape == (4, 4)

    def test_diagonal_is_psd(self):
        prices = _make_prices(30, 4)
        est = estimate_covariance(prices, _config())
        assert _is_psd(est.matrix)

    def test_off_diagonal_zeros(self):
        prices = _make_prices(30, 4)
        est = estimate_covariance(prices, _config())
        m = est.matrix.copy()
        np.fill_diagonal(m, 0)
        assert np.allclose(m, 0)


# ── Near-zero variance (cash proxy) ──────────────────────────────────────────

class TestNearZeroVariance:

    @staticmethod
    def _prices_with_cash_proxy(n: int) -> pd.DataFrame:
        rng = np.random.default_rng(7)
        equity = pd.DataFrame(
            np.cumprod(1 + rng.normal(0.001, 0.02, (n, 3)), axis=0) * 100,
            columns=["A", "B", "C"],
        )
        # BOXX: near-zero daily return (0.001 * 0.01% ≈ 0)
        boxx = pd.DataFrame(
            100 + np.arange(n) * 0.001,
            columns=["BOXX"],
        )
        return pd.concat([equity, boxx], axis=1)

    def test_no_crash_ledoit_wolf_path(self):
        prices = self._prices_with_cash_proxy(300)
        est = estimate_covariance(prices, _config())
        assert est.matrix is not None

    def test_no_crash_diagonal_path(self):
        prices = self._prices_with_cash_proxy(30)
        est = estimate_covariance(prices, _config())
        assert est.matrix is not None

    def test_boxx_diagonal_entry_positive(self):
        prices = self._prices_with_cash_proxy(30)
        est = estimate_covariance(prices, _config())
        boxx_idx = list(prices.columns).index("BOXX")
        assert est.matrix[boxx_idx, boxx_idx] > 0


# ── Few-samples prior ─────────────────────────────────────────────────────────

class TestFewSamplesPrior:

    def test_prior_var_used_when_below_threshold(self):
        """Ticker with < _MIN_SAMPLES_FOR_PRIOR returns → prior variance."""
        tickers = ["A", "B"]
        prices = pd.DataFrame({"A": [100.0] * 10, "B": [100.0] * 10})
        # make B have NaN except 3 rows
        prices.iloc[:-3, 1] = np.nan
        log_ret = np.log(prices / prices.shift(1)).iloc[1:]
        vars_ = _diagonal_fallback_vars(log_ret, tickers)
        # B has 2 non-NaN returns (< _MIN_SAMPLES_FOR_PRIOR = 5)
        assert vars_[1] == pytest.approx(_PRIOR_DAILY_VAR, rel=1e-6)

    def test_prior_var_not_used_when_above_threshold(self):
        """Ticker with >= _MIN_SAMPLES_FOR_PRIOR returns → sample variance, not prior."""
        prices = _make_prices(50, 2)
        log_ret = np.log(prices / prices.shift(1)).iloc[1:]
        tickers = list(prices.columns)
        vars_ = _diagonal_fallback_vars(log_ret, tickers)
        assert vars_[0] != pytest.approx(_PRIOR_DAILY_VAR, rel=1e-2)


# ── Log returns (not arithmetic) ─────────────────────────────────────────────

class TestEmptyInput:

    def test_empty_dataframe_raises(self):
        with pytest.raises(ValueError, match="empty"):
            estimate_covariance(pd.DataFrame(), _config())

    def test_no_columns_raises(self):
        prices = pd.DataFrame(index=range(300))
        with pytest.raises(ValueError, match="empty"):
            estimate_covariance(prices, _config())


class TestLogReturns:

    def test_log_transform_applied(self):
        """Ensure we use ln(P_t / P_{t-1}), not (P_t - P_{t-1}) / P_{t-1}."""
        prices = pd.DataFrame({"X": [100.0, 110.0, 121.0]})
        log_ret = np.log(prices / prices.shift(1)).dropna()
        expected = np.log(110 / 100)
        assert float(log_ret.iloc[0, 0]) == pytest.approx(expected, rel=1e-9)
