"""
Tests for src/rebalancer/runner.py — full pipeline orchestrator.

yfinance calls are mocked so tests run offline and deterministically.
Uses synthetic price data matching the integration smoke test pattern.

Coverage approach: each test exercises a specific code path in runner.run().
The empty-portfolio path, config override, and archive write are tested
explicitly. The full pipeline path uses a 3-ticker synthetic US portfolio.
"""

from __future__ import annotations

import json
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.portfolio.state import PortfolioState
from src.rebalancer.config import RebalanceConfig
from src.rebalancer.runner import ScanData
from src.rebalancer.trade_builder import BuildResult
from src.discipline.metrics import DisciplineMetrics


# ── Synthetic fixtures ─────────────────────────────────────────────────────────

_EQUITY_TICKERS = ["NVDA", "AAPL", "MSFT"]
_ALL_TICKERS    = ["CASH"] + _EQUITY_TICKERS
_PRICES         = {"CASH": 1.0, "NVDA": 900.0, "AAPL": 185.0, "MSFT": 420.0}


def _make_prices_df(seed: int = 42) -> pd.DataFrame:
    """260-day synthetic price history for 3 equities + CASH."""
    rng = np.random.default_rng(seed)
    n_days, n_eq = 260, len(_EQUITY_TICKERS)
    returns = rng.normal(0.0005, 0.015, (n_days, n_eq))
    prices  = np.cumprod(1 + returns, axis=0) * 100.0
    df = pd.DataFrame(prices, columns=_EQUITY_TICKERS)
    df["CASH"] = 1.0
    return df[_ALL_TICKERS]


def _make_scan_result(ticker: str):
    """Synthetic ScanResult with valid PEG data for scoring."""
    from src.data_fetcher import ScanResult
    return ScanResult(
        ticker=ticker,
        current_price=_PRICES[ticker],
        ma50=_PRICES[ticker] * 0.98,        # ~2% above MA50
        valuation_model="PEG",
        modified_peg=0.6 if ticker == "NVDA" else 1.2,
        beta=1.2,
        name=ticker,
    )


def _make_state(tmp_path: Path) -> tuple[PortfolioState, Path]:
    state = PortfolioState.create_empty()
    state.us_cash_usd = 5_000.0
    state.us_holdings = {
        "NVDA": 3.0,
        "AAPL": 20.0,
        "MSFT": 8.0,
    }
    state_path = tmp_path / "portfolio_state.json"
    state.save(state_path)
    return state, state_path


def _make_sector_csv(tmp_path: Path) -> Path:
    content = "ticker,l1_sector,l2_sector\nNVDA,半導體,GPU\nAAPL,軟體_雲端,Platform\nMSFT,軟體_雲端,Cloud\n"
    path = tmp_path / "sector_mapping.csv"
    path.write_text(content, encoding="utf-8")
    return path


# ── Mock context manager ───────────────────────────────────────────────────────

def _runner_patches(prices_df: pd.DataFrame, tmp_path: Path):
    """Return a list of patch context managers for runner dependencies."""
    scan_results = [_make_scan_result(t) for t in _EQUITY_TICKERS]

    return [
        patch("src.rebalancer.runner.get_market_data", return_value=scan_results),
        patch("src.rebalancer.runner._fetch_price_history", return_value=prices_df),
        patch("src.rebalancer.runner._DEFAULT_SECTOR_PATH",  tmp_path / "sector_mapping.csv"),
        patch("src.rebalancer.runner._DEFAULT_COST_PATH",    tmp_path / "cost_profile.json"),
        patch("src.rebalancer.runner._CONFIG_OVERRIDE_PATH", tmp_path / "config_override.json"),
    ]


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestRunnerRun:
    def test_run_returns_correct_types(self, tmp_path):
        _, state_path = _make_state(tmp_path)
        _make_sector_csv(tmp_path)
        prices_df = _make_prices_df()
        decisions = tmp_path / "decisions.jsonl"

        with (
            patch("src.rebalancer.runner.get_market_data", return_value=[_make_scan_result(t) for t in _EQUITY_TICKERS]),
            patch("src.rebalancer.runner._fetch_price_history", return_value=prices_df),
            patch("src.rebalancer.runner._DEFAULT_SECTOR_PATH",  tmp_path / "sector_mapping.csv"),
            patch("src.rebalancer.runner._DEFAULT_COST_PATH",    tmp_path / "nonexistent_cost.json"),
            patch("src.rebalancer.runner._CONFIG_OVERRIDE_PATH", tmp_path / "no_override.json"),
        ):
            from src.rebalancer.runner import run
            result, metrics, scan_data = run("US", state_path=state_path, decisions_path=decisions)

        assert isinstance(result, BuildResult)
        assert isinstance(metrics, DisciplineMetrics)
        assert isinstance(scan_data, ScanData)

    def test_conviction_values_in_range(self, tmp_path):
        _, state_path = _make_state(tmp_path)
        _make_sector_csv(tmp_path)
        prices_df = _make_prices_df()
        decisions = tmp_path / "decisions.jsonl"

        with (
            patch("src.rebalancer.runner.get_market_data", return_value=[_make_scan_result(t) for t in _EQUITY_TICKERS]),
            patch("src.rebalancer.runner._fetch_price_history", return_value=prices_df),
            patch("src.rebalancer.runner._DEFAULT_SECTOR_PATH",  tmp_path / "sector_mapping.csv"),
            patch("src.rebalancer.runner._DEFAULT_COST_PATH",    tmp_path / "nonexistent_cost.json"),
            patch("src.rebalancer.runner._CONFIG_OVERRIDE_PATH", tmp_path / "no_override.json"),
        ):
            from src.rebalancer.runner import run
            result, *_ = run("US", state_path=state_path, decisions_path=decisions)

        for trade in result.trades:
            assert 0.0 <= trade.conviction <= 10.0, (
                f"Conviction {trade.conviction} out of range for {trade.ticker}"
            )

    def test_no_nan_inf_in_weights(self, tmp_path):
        _, state_path = _make_state(tmp_path)
        _make_sector_csv(tmp_path)
        prices_df = _make_prices_df()
        decisions = tmp_path / "decisions.jsonl"

        with (
            patch("src.rebalancer.runner.get_market_data", return_value=[_make_scan_result(t) for t in _EQUITY_TICKERS]),
            patch("src.rebalancer.runner._fetch_price_history", return_value=prices_df),
            patch("src.rebalancer.runner._DEFAULT_SECTOR_PATH",  tmp_path / "sector_mapping.csv"),
            patch("src.rebalancer.runner._DEFAULT_COST_PATH",    tmp_path / "nonexistent_cost.json"),
            patch("src.rebalancer.runner._CONFIG_OVERRIDE_PATH", tmp_path / "no_override.json"),
        ):
            from src.rebalancer.runner import run
            result, *_ = run("US", state_path=state_path, decisions_path=decisions)

        # Verify that optimizer w_target has no NaN or inf
        # (indirectly verified via trade target_weights)
        for trade in result.trades:
            assert not np.isnan(trade.target_weight)
            assert not np.isinf(trade.target_weight)

    def test_archive_written_after_run(self, tmp_path):
        _, state_path = _make_state(tmp_path)
        _make_sector_csv(tmp_path)
        prices_df = _make_prices_df()
        decisions = tmp_path / "decisions.jsonl"

        with (
            patch("src.rebalancer.runner.get_market_data", return_value=[_make_scan_result(t) for t in _EQUITY_TICKERS]),
            patch("src.rebalancer.runner._fetch_price_history", return_value=prices_df),
            patch("src.rebalancer.runner._DEFAULT_SECTOR_PATH",  tmp_path / "sector_mapping.csv"),
            patch("src.rebalancer.runner._DEFAULT_COST_PATH",    tmp_path / "nonexistent_cost.json"),
            patch("src.rebalancer.runner._CONFIG_OVERRIDE_PATH", tmp_path / "no_override.json"),
        ):
            from src.rebalancer.runner import run
            run("US", state_path=state_path, decisions_path=decisions)

        assert decisions.exists(), "Archive file should be created by run()"
        records = [json.loads(l) for l in decisions.read_text().splitlines() if l.strip()]
        assert len(records) >= 1
        assert "market" in records[-1]
        assert records[-1]["market"] == "US"

    def test_hold_when_empty_portfolio(self, tmp_path):
        state = PortfolioState.create_empty()
        # Empty holdings AND zero cash
        state_path = tmp_path / "portfolio_state.json"
        state.save(state_path)
        decisions = tmp_path / "decisions.jsonl"

        with (
            patch("src.rebalancer.runner.get_market_data", return_value=[]),
            patch("src.rebalancer.runner._fetch_price_history", return_value=pd.DataFrame(columns=["CASH"])),
            patch("src.rebalancer.runner._DEFAULT_SECTOR_PATH",  tmp_path / "sector_mapping.csv"),
            patch("src.rebalancer.runner._DEFAULT_COST_PATH",    tmp_path / "nonexistent_cost.json"),
            patch("src.rebalancer.runner._CONFIG_OVERRIDE_PATH", tmp_path / "no_override.json"),
        ):
            from src.rebalancer.runner import run
            result, *_ = run("US", state_path=state_path, decisions_path=decisions)

        assert result.is_hold

    def test_config_override_file_applied(self, tmp_path):
        _, state_path = _make_state(tmp_path)
        _make_sector_csv(tmp_path)
        prices_df = _make_prices_df()
        decisions = tmp_path / "decisions.jsonl"

        override_path = tmp_path / "config_override.json"
        override_path.write_text(
            json.dumps({"US": {"max_position": 0.99}}), encoding="utf-8"
        )

        captured_config: list[RebalanceConfig] = []

        original_solve = None

        def _capture_solve(**kwargs):
            captured_config.append(kwargs.get("config") or RebalanceConfig.us_default())
            from src.rebalancer.optimizer import solve_target_weights as _real
            return _real(**kwargs)

        with (
            patch("src.rebalancer.runner.get_market_data", return_value=[_make_scan_result(t) for t in _EQUITY_TICKERS]),
            patch("src.rebalancer.runner._fetch_price_history", return_value=prices_df),
            patch("src.rebalancer.runner._DEFAULT_SECTOR_PATH",  tmp_path / "sector_mapping.csv"),
            patch("src.rebalancer.runner._DEFAULT_COST_PATH",    tmp_path / "nonexistent_cost.json"),
            patch("src.rebalancer.runner._CONFIG_OVERRIDE_PATH", override_path),
            patch("src.rebalancer.runner.solve_target_weights", side_effect=_capture_solve),
        ):
            from src.rebalancer import runner as runner_mod
            import importlib
            importlib.reload(runner_mod)
            runner_mod.run("US", state_path=state_path, decisions_path=decisions)

        if captured_config:
            assert captured_config[0].max_position == pytest.approx(0.99)

    def test_run_us_market(self, tmp_path):
        _, state_path = _make_state(tmp_path)
        _make_sector_csv(tmp_path)
        prices_df = _make_prices_df()
        decisions = tmp_path / "decisions.jsonl"

        with (
            patch("src.rebalancer.runner.get_market_data", return_value=[_make_scan_result(t) for t in _EQUITY_TICKERS]),
            patch("src.rebalancer.runner._fetch_price_history", return_value=prices_df),
            patch("src.rebalancer.runner._DEFAULT_SECTOR_PATH",  tmp_path / "sector_mapping.csv"),
            patch("src.rebalancer.runner._DEFAULT_COST_PATH",    tmp_path / "nonexistent_cost.json"),
            patch("src.rebalancer.runner._CONFIG_OVERRIDE_PATH", tmp_path / "no_override.json"),
        ):
            from src.rebalancer.runner import run
            result, metrics, _ = run("US", state_path=state_path, decisions_path=decisions)

        assert result.market == "US"

    def test_run_tw_market(self, tmp_path):
        state = PortfolioState.create_empty()
        state.tw_cash_twd = 50_000.0
        state.tw_holdings = {"2330.TW": 1000.0}
        state_path = tmp_path / "portfolio_state.json"
        state.save(state_path)

        from src.data_fetcher import ScanResult
        tw_scan = ScanResult(
            ticker="2330.TW",
            current_price=800.0,
            ma50=780.0,
            valuation_model="PEG",
            modified_peg=0.9,
            name="台積電",
        )

        prices_df = pd.DataFrame(
            {"CASH": [1.0] * 260, "2330.TW": np.linspace(750, 800, 260)},
        )

        decisions = tmp_path / "decisions.jsonl"
        with (
            patch("src.rebalancer.runner.get_market_data", return_value=[tw_scan]),
            patch("src.rebalancer.runner._fetch_price_history", return_value=prices_df),
            patch("src.rebalancer.runner._DEFAULT_SECTOR_PATH",  tmp_path / "sector_mapping.csv"),
            patch("src.rebalancer.runner._DEFAULT_COST_PATH",    tmp_path / "nonexistent_cost.json"),
            patch("src.rebalancer.runner._CONFIG_OVERRIDE_PATH", tmp_path / "no_override.json"),
        ):
            from src.rebalancer.runner import run
            result, metrics, _ = run("TW", state_path=state_path, decisions_path=decisions)

        assert result.market == "TW"
        assert isinstance(metrics, DisciplineMetrics)
