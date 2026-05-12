"""
Alpha Strategist — Covariance Estimator (V2.0)

Estimates annualized return covariance for the QP optimizer using
Ledoit-Wolf shrinkage (scikit-learn). Falls back to a diagonal matrix
when there are not enough common-window data points (spec §11, Risk row 4).

Estimation procedure
--------------------
1. Compute daily log returns for each ticker.
2. Identify the "common window": rows where ALL tickers have a non-NaN return.
3. If len(common window) >= config.lookback_days_min → Ledoit-Wolf on the
   common window, then annualise by × 252.
4. Otherwise → diagonal fallback: per-ticker variance from all available
   returns (or a prior of (0.01)² if fewer than _MIN_SAMPLES_FOR_PRIOR rows
   exist), then annualise by × 252.

The diagonal fallback guarantees a positive definite matrix because every
diagonal entry is strictly > 0 (either from data or from the prior).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

from src.rebalancer.config import RebalanceConfig

logger = logging.getLogger(__name__)

_ANNUALISE = 252
_MIN_SAMPLES_FOR_PRIOR = 5
_PRIOR_DAILY_VAR = (0.01) ** 2  # 1% daily vol prior, annualises to 0.0252


@dataclass
class CovEstimate:
    """
    Result of estimate_covariance().

    Attributes
    ----------
    matrix:
        N×N annualised covariance matrix (symmetric, positive definite).
    days_per_ticker:
        Number of non-NaN log-return rows available per ticker.
        Used by conviction.py to compute cov_certainty per trade.
    used_ledoit_wolf:
        True if the full Ledoit-Wolf path was taken.
        False if the common window was too short and a diagonal fallback
        was used instead.
    """

    matrix: np.ndarray
    days_per_ticker: dict[str, int]
    used_ledoit_wolf: bool


def estimate_covariance(
    prices: pd.DataFrame,
    config: RebalanceConfig,
) -> CovEstimate:
    """
    Estimate the annualised return covariance matrix for the given price history.

    Parameters
    ----------
    prices:
        DataFrame with dates as the index (ascending) and ticker symbols as
        columns. May contain NaN for tickers with shorter histories.
    config:
        RebalanceConfig whose lookback_days_min and lookback_days_ideal fields
        govern the fallback decision and ideal window length.

    Returns
    -------
    CovEstimate
        See CovEstimate dataclass for field descriptions.
    """
    if prices.empty or prices.shape[1] == 0:
        raise ValueError("prices DataFrame is empty")

    tickers: list[str] = list(prices.columns)
    n = len(tickers)

    log_returns: pd.DataFrame = np.log(prices / prices.shift(1)).iloc[1:]

    days_per_ticker: dict[str, int] = {
        t: int(log_returns[t].notna().sum()) for t in tickers
    }

    common_mask: pd.Series = log_returns.notna().all(axis=1)
    common_returns: pd.DataFrame = log_returns.loc[common_mask]
    common_days = len(common_returns)

    if common_days >= config.lookback_days_min:
        window = common_returns.iloc[-config.lookback_days_ideal :]
        lw = LedoitWolf()
        lw.fit(window.values)
        cov_daily = lw.covariance_
        cov_annual = cov_daily * _ANNUALISE
        logger.debug(
            "[COV] Ledoit-Wolf on %d common rows (%d tickers)", len(window), n
        )
        return CovEstimate(
            matrix=cov_annual,
            days_per_ticker=days_per_ticker,
            used_ledoit_wolf=True,
        )

    logger.warning(
        "[COV] Common window %d < min %d — falling back to diagonal matrix",
        common_days,
        config.lookback_days_min,
    )
    diag_vars = _diagonal_fallback_vars(log_returns, tickers)
    cov_annual = np.diag(diag_vars) * _ANNUALISE
    return CovEstimate(
        matrix=cov_annual,
        days_per_ticker=days_per_ticker,
        used_ledoit_wolf=False,
    )


def _diagonal_fallback_vars(
    log_returns: pd.DataFrame,
    tickers: list[str],
) -> np.ndarray:
    """
    Per-ticker daily variance for the diagonal fallback matrix.

    Uses the sample variance of all available returns for each ticker.
    Falls back to _PRIOR_DAILY_VAR when fewer than _MIN_SAMPLES_FOR_PRIOR
    non-NaN returns exist (e.g. newly listed tickers).
    """
    vars_: list[float] = []
    for t in tickers:
        col = log_returns[t].dropna()
        if len(col) >= _MIN_SAMPLES_FOR_PRIOR:
            vars_.append(float(np.var(col, ddof=1)))
        else:
            logger.debug(
                "[COV] %s has only %d samples — using prior daily var %.6f",
                t,
                len(col),
                _PRIOR_DAILY_VAR,
            )
            vars_.append(_PRIOR_DAILY_VAR)
    return np.array(vars_, dtype=float)
