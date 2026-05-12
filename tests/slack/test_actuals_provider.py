"""Unit tests for src/slack/actuals_provider.py — JsonlActualsProvider."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.slack.actuals_provider import JsonlActualsProvider


# ── Shared helpers ────────────────────────────────────────────────────────────

def _write_records(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _record(
    ticker: str = "NVDA",
    side: str = "BUY",
    market: str = "US",
    date_str: str | None = None,
    status: str = "pending_confirmation",
) -> dict:
    if date_str is None:
        date_str = date.today().isoformat()
    return {
        "ticker": ticker,
        "side": side,
        "market": market,
        "date": date_str,
        "system_suggested": True,
        "quantity": 4.3,
        "filled_price": None,
        "commission": 0.40,
        "tax": 0.0,
        "fx": 1.0,
        "status": status,
    }


# ── Empty / missing file ──────────────────────────────────────────────────────

class TestEmptyAndMissing:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        provider = JsonlActualsProvider(tmp_path / "missing.jsonl")
        assert provider.actual_actions(days=7) == []

    def test_empty_file_returns_empty(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        p.write_text("")
        provider = JsonlActualsProvider(p)
        assert provider.actual_actions(days=7) == []

    def test_blank_lines_ignored(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        p.write_text("\n\n\n")
        provider = JsonlActualsProvider(p)
        assert provider.actual_actions(days=7) == []


# ── Date filtering ────────────────────────────────────────────────────────────

class TestDateFiltering:
    def test_today_included(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        _write_records(p, [_record("NVDA", date_str=date.today().isoformat())])
        results = JsonlActualsProvider(p).actual_actions(days=1)
        assert len(results) == 1
        assert results[0].ticker == "NVDA"

    def test_old_record_excluded(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        old = (date.today() - timedelta(days=10)).isoformat()
        _write_records(p, [_record("NVDA", date_str=old)])
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert results == []

    def test_boundary_day_included(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        cutoff = (date.today() - timedelta(days=6)).isoformat()  # days=7 → cutoff = today-6
        _write_records(p, [_record("NVDA", date_str=cutoff)])
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert len(results) == 1


# ── Market filtering ──────────────────────────────────────────────────────────

class TestMarketFiltering:
    def test_filter_us_only(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        _write_records(p, [
            _record("NVDA", market="US"),
            _record("2330.TW", market="TW"),
        ])
        results = JsonlActualsProvider(p).actual_actions(days=7, market="US")
        assert all(t.market == "US" for t in results)
        assert len(results) == 1

    def test_filter_tw_only(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        _write_records(p, [
            _record("NVDA", market="US"),
            _record("2330.TW", market="TW"),
        ])
        results = JsonlActualsProvider(p).actual_actions(days=7, market="TW")
        assert all(t.market == "TW" for t in results)
        assert len(results) == 1

    def test_all_market_includes_both(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        _write_records(p, [
            _record("NVDA", market="US"),
            _record("2330.TW", market="TW"),
        ])
        results = JsonlActualsProvider(p).actual_actions(days=7, market="ALL")
        assert len(results) == 2


# ── ActualTrade field mapping ─────────────────────────────────────────────────

class TestActualTradeFields:
    def test_fields_mapped_correctly(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        today = date.today().isoformat()
        _write_records(p, [_record("NVDA", "SELL", "US", today)])
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert len(results) == 1
        t = results[0]
        assert t.ticker == "NVDA"
        assert t.side == "SELL"
        assert t.market == "US"
        assert t.date == today

    def test_hold_side_included(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        rec = _record("NVDA", side="HOLD")
        p.write_text(json.dumps(rec) + "\n")
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert len(results) == 1
        assert results[0].side == "HOLD"


# ── Robustness ────────────────────────────────────────────────────────────────

class TestRobustness:
    def test_malformed_json_skipped(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        good = json.dumps(_record("NVDA"))
        p.write_text('{"bad json\n' + good + "\n")
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert len(results) == 1
        assert results[0].ticker == "NVDA"

    def test_missing_date_skipped(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        bad = {"ticker": "NVDA", "side": "BUY", "market": "US"}
        p.write_text(json.dumps(bad) + "\n")
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert results == []

    def test_invalid_date_format_skipped(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        bad = {"ticker": "NVDA", "side": "BUY", "market": "US", "date": "not-a-date"}
        p.write_text(json.dumps(bad) + "\n")
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert results == []

    def test_missing_ticker_skipped(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        bad = {"side": "BUY", "market": "US", "date": date.today().isoformat()}
        p.write_text(json.dumps(bad) + "\n")
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert results == []

    def test_sorted_ascending(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _write_records(p, [
            _record("NVDA", date_str=today),
            _record("AAPL", date_str=yesterday),
        ])
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert results[0].ticker == "AAPL"
        assert results[1].ticker == "NVDA"

    def test_multiple_records_same_ticker(self, tmp_path: Path):
        p = tmp_path / "trades.jsonl"
        today = date.today().isoformat()
        _write_records(p, [
            _record("NVDA", "BUY", date_str=today),
            _record("NVDA", "SELL", date_str=today),
        ])
        results = JsonlActualsProvider(p).actual_actions(days=7)
        assert len(results) == 2

