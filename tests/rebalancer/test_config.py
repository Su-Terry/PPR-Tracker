"""
Tests for src/rebalancer/config.py

Verifies that US/TW defaults match spec §4.3 exactly and that
the frozen dataclass rejects mutation.
"""

from __future__ import annotations

import pytest

from src.rebalancer.config import RebalanceConfig


class TestUsDefault:
    def test_market_field(self):
        assert RebalanceConfig.us_default().market == "US"

    def test_lambda_score(self):
        assert RebalanceConfig.us_default().lambda_score == 1.0

    def test_lambda_var(self):
        assert RebalanceConfig.us_default().lambda_var == 0.5

    def test_lambda_turnover(self):
        assert RebalanceConfig.us_default().lambda_turnover == 2.0

    def test_max_position(self):
        assert RebalanceConfig.us_default().max_position == 0.20

    def test_max_sector(self):
        assert RebalanceConfig.us_default().max_sector == 0.40

    def test_cash_floor(self):
        assert RebalanceConfig.us_default().cash_floor == 0.05

    def test_max_turnover(self):
        assert RebalanceConfig.us_default().max_turnover == 0.40

    def test_min_position(self):
        assert RebalanceConfig.us_default().min_position == 0.02

    def test_min_trade_amount(self):
        assert RebalanceConfig.us_default().min_trade_amount == 100.0


class TestTwDefault:
    def test_market_field(self):
        assert RebalanceConfig.tw_default().market == "TW"

    def test_lambda_score(self):
        assert RebalanceConfig.tw_default().lambda_score == 1.0

    def test_lambda_var(self):
        assert RebalanceConfig.tw_default().lambda_var == 0.5

    def test_lambda_turnover(self):
        assert RebalanceConfig.tw_default().lambda_turnover == 3.0

    def test_max_position(self):
        assert RebalanceConfig.tw_default().max_position == 0.30

    def test_max_sector(self):
        assert RebalanceConfig.tw_default().max_sector == 0.50

    def test_cash_floor(self):
        assert RebalanceConfig.tw_default().cash_floor == 0.10

    def test_max_turnover(self):
        assert RebalanceConfig.tw_default().max_turnover == 0.30

    def test_min_position(self):
        assert RebalanceConfig.tw_default().min_position == 0.05

    def test_min_trade_amount(self):
        assert RebalanceConfig.tw_default().min_trade_amount == 3000.0


class TestSharedDefaults:
    def test_min_total_turnover_default(self):
        assert RebalanceConfig.us_default().min_total_turnover == 0.02
        assert RebalanceConfig.tw_default().min_total_turnover == 0.02

    def test_lookback_days_min_default(self):
        assert RebalanceConfig.us_default().lookback_days_min == 60
        assert RebalanceConfig.tw_default().lookback_days_min == 60

    def test_lookback_days_ideal_default(self):
        assert RebalanceConfig.us_default().lookback_days_ideal == 252
        assert RebalanceConfig.tw_default().lookback_days_ideal == 252

    def test_min_trade_amount_differs_by_market(self):
        assert RebalanceConfig.us_default().min_trade_amount == 100.0
        assert RebalanceConfig.tw_default().min_trade_amount == 3000.0

    def test_frozen_raises_on_mutation(self):
        cfg = RebalanceConfig.us_default()
        with pytest.raises(Exception):  # FrozenInstanceError
            cfg.max_position = 0.99  # type: ignore[misc]
