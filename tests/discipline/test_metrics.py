"""Unit tests for src/discipline/metrics.py."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from src.discipline.metrics import (
    ActualTrade,
    ActualsProvider,
    DisciplineMetrics,
    EmptyActualsProvider,
    _compute_discipline_score_7d,
    _compute_drift,
    _compute_last_trade_days,
    _compute_turnover_30d,
    _parse_date,
    _read_decisions,
    compute_metrics,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

TICKERS = ["CASH", "AAPL", "NVDA"]
TODAY = date(2026, 5, 12)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _active_record(
    market: str = "US",
    date_str: str = "2026-05-12T08:30:00+08:00",
    tickers: list[str] | None = None,
    w_target: list[float] | None = None,
    w_current: list[float] | None = None,
    trades: list[dict] | None = None,
) -> dict:
    tickers = tickers or TICKERS
    n = len(tickers)
    return {
        "timestamp": date_str,
        "market": market,
        "is_hold": False,
        "hold_reasons": [],
        "tickers": tickers,
        "w_target": w_target or [0.08, 0.46, 0.46],
        "w_current": w_current or [0.09, 0.50, 0.41],
        "trades": trades or [{"ticker": "NVDA", "side": "BUY", "delta_weight": 0.05}],
    }


def _hold_record(
    market: str = "US",
    date_str: str = "2026-05-12T08:30:00+08:00",
) -> dict:
    return {
        "timestamp": date_str,
        "market": market,
        "is_hold": True,
        "hold_reasons": ["min_turnover"],
        "tickers": TICKERS,
        "w_target": [0.09, 0.50, 0.41],
        "w_current": [0.09, 0.50, 0.41],
        "trades": [],
    }


# ── EmptyActualsProvider ───────────────────────────────────────────────────────

class TestEmptyActualsProvider:
    def test_returns_empty_list(self):
        provider = EmptyActualsProvider()
        assert provider.actual_actions(days=7) == []

    def test_accepts_market_filter(self):
        provider = EmptyActualsProvider()
        assert provider.actual_actions(days=7, market="US") == []

    def test_is_actuals_provider_protocol(self):
        provider = EmptyActualsProvider()
        assert isinstance(provider, ActualsProvider)


# ── _read_decisions ────────────────────────────────────────────────────────────

class TestReadDecisions:
    def test_returns_empty_for_absent_file(self, tmp_path):
        result = _read_decisions(tmp_path / "missing.jsonl")
        assert result == []

    def test_reads_valid_records(self, tmp_path):
        path = tmp_path / "d.jsonl"
        _write_jsonl(path, [_active_record(), _hold_record()])
        result = _read_decisions(path)
        assert len(result) == 2

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "d.jsonl"
        with path.open("w") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps(_active_record()) + "\n")
        result = _read_decisions(path)
        assert len(result) == 1


# ── _compute_drift ─────────────────────────────────────────────────────────────

class TestComputeDrift:
    def test_zero_when_no_records(self):
        w_current = np.array([0.09, 0.50, 0.41])
        result = _compute_drift(w_current=w_current, tickers=TICKERS, records=[])
        assert result == 0.0

    def test_zero_when_aligned(self):
        w_current = np.array([0.08, 0.46, 0.46])
        records = [_active_record(w_target=[0.08, 0.46, 0.46])]
        result = _compute_drift(w_current=w_current, tickers=TICKERS, records=records)
        assert result == pytest.approx(0.0, abs=1e-4)

    def test_nonzero_when_drifted(self):
        # w_current differs from w_target by 0.10 total L1 → drift_pct = 5%
        w_current = np.array([0.09, 0.46, 0.45])
        records = [_active_record(w_target=[0.08, 0.51, 0.41])]
        result = _compute_drift(w_current=w_current, tickers=TICKERS, records=records)
        # L1 = |0.09-0.08| + |0.46-0.51| + |0.45-0.41| = 0.01+0.05+0.04 = 0.10 → 5%
        assert result == pytest.approx(5.0, rel=0.01)

    def test_uses_most_recent_non_hold_record(self):
        w_current = np.array([0.08, 0.46, 0.46])
        records = [
            _active_record(date_str="2026-05-10T08:30:00+08:00", w_target=[0.10, 0.45, 0.45]),
            _hold_record(date_str="2026-05-11T08:30:00+08:00"),
            _active_record(date_str="2026-05-12T08:30:00+08:00", w_target=[0.08, 0.46, 0.46]),
        ]
        result = _compute_drift(w_current=w_current, tickers=TICKERS, records=records)
        assert result == pytest.approx(0.0, abs=1e-4)

    def test_skips_hold_records(self):
        w_current = np.array([0.09, 0.50, 0.41])
        records = [_hold_record()]
        result = _compute_drift(w_current=w_current, tickers=TICKERS, records=records)
        assert result == 0.0


# ── _compute_turnover_30d ──────────────────────────────────────────────────────

class TestComputeTurnover30d:
    def test_zero_when_no_records(self):
        result = _compute_turnover_30d(records=[], today=TODAY)
        assert result == 0.0

    def test_sums_delta_weights_within_window(self):
        records = [
            _active_record(
                date_str="2026-05-12T08:30:00+08:00",
                trades=[{"ticker": "AAPL", "side": "SELL", "delta_weight": -0.032},
                        {"ticker": "NVDA", "side": "BUY", "delta_weight": 0.040}],
            ),
        ]
        result = _compute_turnover_30d(records=records, today=TODAY)
        # |−0.032| + |0.040| = 0.072
        assert result == pytest.approx(0.072, rel=1e-4)

    def test_excludes_records_older_than_30_days(self):
        records = [
            _active_record(
                date_str="2026-04-01T08:30:00+08:00",
                trades=[{"ticker": "AAPL", "side": "SELL", "delta_weight": -0.10}],
            ),
        ]
        result = _compute_turnover_30d(records=records, today=TODAY)
        assert result == 0.0

    def test_skips_hold_records(self):
        records = [_hold_record()]
        result = _compute_turnover_30d(records=records, today=TODAY)
        assert result == 0.0


# ── _compute_last_trade_days ───────────────────────────────────────────────────

class TestComputeLastTradeDays:
    def test_minus_one_when_no_records(self):
        result = _compute_last_trade_days(records=[], today=TODAY)
        assert result == -1

    def test_zero_when_decision_today(self):
        records = [_active_record(date_str="2026-05-12T08:30:00+08:00")]
        result = _compute_last_trade_days(records=records, today=TODAY)
        assert result == 0

    def test_correct_days_since(self):
        records = [_active_record(date_str="2026-05-09T08:30:00+08:00")]
        result = _compute_last_trade_days(records=records, today=TODAY)
        assert result == 3

    def test_skips_hold_records(self):
        records = [_hold_record(date_str="2026-05-12T08:30:00+08:00")]
        result = _compute_last_trade_days(records=records, today=TODAY)
        assert result == -1

    def test_uses_most_recent(self):
        records = [
            _active_record(date_str="2026-05-05T08:30:00+08:00"),
            _active_record(date_str="2026-05-10T08:30:00+08:00"),
        ]
        result = _compute_last_trade_days(records=records, today=TODAY)
        assert result == 2


# ── _compute_discipline_score_7d ──────────────────────────────────────────────

class TestComputeDisciplineScore7d:
    def test_zero_when_no_suggestions(self):
        result = _compute_discipline_score_7d(
            records=[],
            actuals_provider=EmptyActualsProvider(),
            today=TODAY,
        )
        assert result == 0

    def test_zero_with_empty_actuals_provider(self):
        records = [_active_record()]
        result = _compute_discipline_score_7d(
            records=records,
            actuals_provider=EmptyActualsProvider(),
            today=TODAY,
        )
        assert result == 0

    def test_perfect_score_when_all_aligned(self):
        records = [
            _active_record(
                date_str="2026-05-12T08:30:00+08:00",
                trades=[{"ticker": "NVDA", "side": "BUY", "delta_weight": 0.04}],
            ),
        ]

        class PerfectProvider:
            def actual_actions(self, days: int, market: str = "ALL") -> list[ActualTrade]:
                return [ActualTrade(ticker="NVDA", side="BUY", market="US", date="2026-05-12")]

        result = _compute_discipline_score_7d(
            records=records,
            actuals_provider=PerfectProvider(),
            today=TODAY,
        )
        assert result == 100

    def test_hold_aligned_when_no_trades(self):
        records = [_hold_record(date_str="2026-05-12T08:30:00+08:00")]
        result = _compute_discipline_score_7d(
            records=records,
            actuals_provider=EmptyActualsProvider(),
            today=TODAY,
        )
        assert result == 100

    def test_hold_misaligned_when_user_traded(self):
        records = [_hold_record(date_str="2026-05-12T08:30:00+08:00")]

        class TradedProvider:
            def actual_actions(self, days: int, market: str = "ALL") -> list[ActualTrade]:
                return [ActualTrade(ticker="AAPL", side="BUY", market="US", date="2026-05-12")]

        result = _compute_discipline_score_7d(
            records=records,
            actuals_provider=TradedProvider(),
            today=TODAY,
        )
        assert result == 0

    def test_excludes_records_older_than_7_days(self):
        records = [_active_record(date_str="2026-05-01T08:30:00+08:00")]
        result = _compute_discipline_score_7d(
            records=records,
            actuals_provider=EmptyActualsProvider(),
            today=TODAY,
        )
        assert result == 0


# ── compute_metrics integration ────────────────────────────────────────────────

class TestComputeMetrics:
    def test_all_zeros_when_archive_empty(self, tmp_path):
        path = tmp_path / "d.jsonl"
        w_current = np.array([0.09, 0.50, 0.41])
        metrics = compute_metrics(
            w_current=w_current,
            tickers=TICKERS,
            decisions_path=path,
            actuals_provider=EmptyActualsProvider(),
            today=TODAY,
        )
        assert metrics.drift_pct == 0.0
        assert metrics.turnover_30d == 0.0
        assert metrics.last_trade_days == -1
        assert metrics.discipline_score_7d == 0

    def test_metrics_populated_from_archive(self, tmp_path):
        path = tmp_path / "d.jsonl"
        _write_jsonl(path, [
            _active_record(
                date_str="2026-05-12T08:30:00+08:00",
                w_target=[0.08, 0.46, 0.46],
                trades=[{"ticker": "NVDA", "side": "BUY", "delta_weight": 0.05}],
            ),
        ])
        w_current = np.array([0.09, 0.50, 0.41])
        metrics = compute_metrics(
            w_current=w_current,
            tickers=TICKERS,
            decisions_path=path,
            actuals_provider=EmptyActualsProvider(),
            today=TODAY,
        )
        assert metrics.drift_pct > 0.0
        assert metrics.turnover_30d > 0.0
        assert metrics.last_trade_days == 0
        assert metrics.discipline_score_7d == 0  # EmptyActualsProvider

    def test_today_injectable(self, tmp_path):
        path = tmp_path / "d.jsonl"
        _write_jsonl(path, [_active_record(date_str="2026-05-10T08:30:00+08:00")])
        w_current = np.array([0.09, 0.50, 0.41])
        metrics = compute_metrics(
            w_current=w_current,
            tickers=TICKERS,
            decisions_path=path,
            actuals_provider=EmptyActualsProvider(),
            today=date(2026, 5, 15),  # 5 days after decision
        )
        assert metrics.last_trade_days == 5

    def test_returns_discipline_metrics_type(self, tmp_path):
        metrics = compute_metrics(
            w_current=np.zeros(3),
            tickers=TICKERS,
            decisions_path=tmp_path / "d.jsonl",
            actuals_provider=EmptyActualsProvider(),
            today=TODAY,
        )
        assert isinstance(metrics, DisciplineMetrics)


# ── _parse_date ────────────────────────────────────────────────────────────────

class TestParseDate:
    def test_parses_valid_iso_with_offset(self):
        result = _parse_date("2026-05-12T08:30:00+08:00")
        assert result == date(2026, 5, 12)

    def test_parses_naive_iso(self):
        result = _parse_date("2026-05-12T08:30:00")
        assert result == date(2026, 5, 12)

    def test_returns_none_for_empty(self):
        assert _parse_date("") is None

    def test_returns_none_for_invalid(self):
        assert _parse_date("not-a-date") is None
