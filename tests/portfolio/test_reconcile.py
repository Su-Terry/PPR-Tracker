"""
Tests for src/portfolio/reconcile.py — reconcile_from_csv() + ReconcileReport.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.portfolio.reconcile import ReconcileReport, reconcile_from_csv
from src.portfolio.state import PortfolioState


def _make_state(
    us_holdings: dict[str, float] | None = None,
    tw_holdings: dict[str, float] | None = None,
) -> PortfolioState:
    state = PortfolioState.create_empty()
    if us_holdings:
        state.us_holdings = dict(us_holdings)
    if tw_holdings:
        state.tw_holdings = dict(tw_holdings)
    return state


def _write_us_csv(path: Path, rows: list[dict]) -> None:
    """Write a minimal US (foreign) CSV matching Cathay broker format."""
    fieldnames = ["代號", "目前庫存"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_tw_csv(path: Path, rows: list[dict]) -> None:
    """Write a minimal TW CSV matching Cathay broker format."""
    fieldnames = ["股票名稱", "股數"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class TestReconcileFromCsv:
    def test_no_discrepancies(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0, "AAPL": 5.0})
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [
            {"代號": "NVDA", "目前庫存": "10.0"},
            {"代號": "AAPL", "目前庫存": "5.0"},
        ])
        report = reconcile_from_csv(csv_path, "US", state)
        assert not report.has_discrepancies
        assert report.mismatches == []
        assert report.unknown_tickers == []
        assert report.missing_tickers == []

    def test_quantity_mismatch(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0})
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [{"代號": "NVDA", "目前庫存": "15.0"}])
        report = reconcile_from_csv(csv_path, "US", state)
        assert len(report.mismatches) == 1
        m = report.mismatches[0]
        assert m["ticker"] == "NVDA"
        assert m["state_qty"] == pytest.approx(10.0)
        assert m["csv_qty"] == pytest.approx(15.0)
        assert m["delta"] == pytest.approx(5.0)

    def test_unknown_ticker_in_csv(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0})
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [
            {"代號": "NVDA", "目前庫存": "10.0"},
            {"代號": "MSFT", "目前庫存": "3.0"},  # not in state
        ])
        report = reconcile_from_csv(csv_path, "US", state)
        assert "MSFT" in report.unknown_tickers
        assert report.mismatches == []

    def test_missing_ticker_from_csv(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0, "AAPL": 5.0})
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [{"代號": "NVDA", "目前庫存": "10.0"}])
        # AAPL is in state but not in CSV
        report = reconcile_from_csv(csv_path, "US", state)
        assert "AAPL" in report.missing_tickers

    def test_empty_csv(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0, "AAPL": 5.0})
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [])
        report = reconcile_from_csv(csv_path, "US", state)
        assert set(report.missing_tickers) == {"NVDA", "AAPL"}
        assert report.unknown_tickers == []

    def test_multiple_mismatches(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0, "AAPL": 5.0})
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [
            {"代號": "NVDA", "目前庫存": "12.0"},
            {"代號": "AAPL", "目前庫存": "3.0"},
        ])
        report = reconcile_from_csv(csv_path, "US", state)
        assert len(report.mismatches) == 2

    def test_market_field_set_correctly(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0})
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [{"代號": "NVDA", "目前庫存": "10.0"}])
        report = reconcile_from_csv(csv_path, "US", state)
        assert report.market == "US"

    def test_state_not_mutated(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0})
        original_qty = state.us_holdings["NVDA"]
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [{"代號": "NVDA", "目前庫存": "20.0"}])
        reconcile_from_csv(csv_path, "US", state)
        assert state.us_holdings["NVDA"] == original_qty  # not mutated

    def test_missing_csv_raises_os_error(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0})
        with pytest.raises((OSError, Exception)):
            reconcile_from_csv(tmp_path / "nonexistent.csv", "US", state)

    def test_to_slack_text_no_discrepancies(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0})
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [{"代號": "NVDA", "目前庫存": "10.0"}])
        report = reconcile_from_csv(csv_path, "US", state)
        text = report.to_slack_text()
        assert "✅" in text
        assert "US" in text

    def test_to_slack_text_with_mismatch(self, tmp_path):
        state = _make_state(us_holdings={"NVDA": 10.0})
        csv_path = tmp_path / "holdings.csv"
        _write_us_csv(csv_path, [{"代號": "NVDA", "目前庫存": "20.0"}])
        report = reconcile_from_csv(csv_path, "US", state)
        text = report.to_slack_text()
        assert "NVDA" in text
        assert "數量差異" in text

    def test_has_discrepancies_property(self):
        report_clean = ReconcileReport(market="US")
        assert not report_clean.has_discrepancies

        report_dirty = ReconcileReport(market="US", mismatches=[{"ticker": "X"}])
        assert report_dirty.has_discrepancies
