"""
Tests for src/cost/profile.py

Coverage targets: CostProfile (all public methods), RateEntry,
SchemaVersionError, estimate() branches, manual override, reset, record_actual.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cost.profile import (
    SUPPORTED_SCHEMA_VERSION,
    CostProfile,
    RateEntry,
    SchemaVersionError,
    _DEFAULT_TW_SEC_TAX,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _valid_profile_dict() -> dict:
    return {
        "version": 1,
        "last_updated": "2026-05-12T00:00:00+08:00",
        "us_buy":  {"rate": 0.0001, "min": 0.01, "source": "default"},
        "us_sell": {"rate": 0.0001, "min": 0.01, "source": "default"},
        "tw_buy":  {"rate": 0.001425, "min": 20.0, "source": "default"},
        "tw_sell": {"rate": 0.001425, "min": 20.0, "source": "default"},
        "tw_sec_tax": 0.003,
        "fx_twd_usd_spread_bps": 45.0,
    }


# ── from_defaults ─────────────────────────────────────────────────────────────

class TestFromDefaults:
    def test_from_defaults_matches_spec_us(self):
        cp = CostProfile.from_defaults()
        assert cp.us_buy.rate == pytest.approx(0.0001)
        assert cp.us_buy.min_cost == pytest.approx(0.01)
        assert cp.us_buy.source == "default"
        assert cp.us_sell.rate == pytest.approx(0.0001)

    def test_from_defaults_matches_spec_tw(self):
        cp = CostProfile.from_defaults()
        assert cp.tw_buy.rate == pytest.approx(0.001425)
        assert cp.tw_buy.min_cost == pytest.approx(20.0)
        assert cp.tw_sell.rate == pytest.approx(0.001425)

    def test_from_defaults_tw_sec_tax(self):
        cp = CostProfile.from_defaults()
        assert cp.tw_sec_tax == pytest.approx(0.003)

    def test_from_defaults_fx_spread(self):
        cp = CostProfile.from_defaults()
        assert cp.fx_twd_usd_spread_bps == pytest.approx(45.0)

    def test_from_defaults_all_source_default(self):
        cp = CostProfile.from_defaults()
        for key in ("us_buy", "us_sell", "tw_buy", "tw_sell"):
            assert getattr(cp, key).source == "default"


# ── Persistence ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        p = tmp_path / "cost_profile.json"
        cp = CostProfile.from_defaults()
        cp.save(p)
        cp2 = CostProfile.load(p)
        assert cp2.to_dict() == cp.to_dict()

    def test_load_missing_file_returns_defaults(self, tmp_path):
        p = tmp_path / "no_such_file.json"
        cp = CostProfile.load(p)
        assert cp.us_buy.rate == pytest.approx(0.0001)
        assert cp.tw_buy.rate == pytest.approx(0.001425)

    def test_load_corrupted_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{bad json!!}", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON"):
            CostProfile.load(p)

    def test_schema_version_too_high_raises(self, tmp_path):
        p = tmp_path / "future.json"
        data = _valid_profile_dict()
        data["version"] = SUPPORTED_SCHEMA_VERSION + 1
        _save_json(p, data)
        with pytest.raises(SchemaVersionError):
            CostProfile.load(p)

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        p = tmp_path / "cost_profile.json"
        CostProfile.from_defaults().save(p)
        assert not (tmp_path / "cost_profile.tmp").exists()

    def test_min_cost_key_roundtrips_as_min_in_json(self, tmp_path):
        p = tmp_path / "cost_profile.json"
        cp = CostProfile.from_defaults()
        cp.save(p)
        raw = json.loads(p.read_text(encoding="utf-8"))
        # JSON must use "min" key, not "min_cost"
        assert "min" in raw["us_buy"]
        assert "min_cost" not in raw["us_buy"]
        # Round-trip: from_dict restores min_cost field
        cp2 = CostProfile.from_dict(raw)
        assert cp2.us_buy.min_cost == pytest.approx(cp.us_buy.min_cost)


# ── estimate ──────────────────────────────────────────────────────────────────

class TestEstimate:
    def test_us_buy_large_notional(self):
        cp = CostProfile.from_defaults()
        cost = cp.estimate("US", "BUY", 10_000.0)
        # max(0.01, 10000 * 0.0001) = max(0.01, 1.0) = 1.0
        assert cost == pytest.approx(1.0)

    def test_us_buy_tiny_notional_hits_min(self):
        cp = CostProfile.from_defaults()
        cost = cp.estimate("US", "BUY", 50.0)
        # max(0.01, 50 * 0.0001) = max(0.01, 0.005) = 0.01
        assert cost == pytest.approx(0.01)

    def test_tw_buy_standard(self):
        cp = CostProfile.from_defaults()
        cost = cp.estimate("TW", "BUY", 200_000.0)
        # max(20, 200000 * 0.001425) = max(20, 285) = 285
        assert cost == pytest.approx(285.0)

    def test_tw_sell_adds_sec_tax(self):
        cp = CostProfile.from_defaults()
        cost = cp.estimate("TW", "SELL", 200_000.0)
        # max(20, 200000 * 0.001425) + 200000 * 0.003 = 285 + 600 = 885
        assert cost == pytest.approx(885.0)

    def test_tw_buy_small_hits_min(self):
        cp = CostProfile.from_defaults()
        cost = cp.estimate("TW", "BUY", 1_000.0)
        # max(20, 1000 * 0.001425) = max(20, 1.425) = 20
        assert cost == pytest.approx(20.0)

    def test_us_sell_equals_us_buy_at_defaults(self):
        cp = CostProfile.from_defaults()
        buy = cp.estimate("US", "BUY", 5_000.0)
        sell = cp.estimate("US", "SELL", 5_000.0)
        assert buy == pytest.approx(sell)

    def test_tw_sell_always_greater_than_tw_buy(self):
        cp = CostProfile.from_defaults()
        for notional in (500.0, 10_000.0, 500_000.0):
            assert cp.estimate("TW", "SELL", notional) > cp.estimate("TW", "BUY", notional)


# ── manual_set and reset ──────────────────────────────────────────────────────

class TestManualSet:
    def test_manual_set_changes_estimate(self):
        cp = CostProfile.from_defaults()
        cp.manual_set("us_buy", rate=0.0024, min_cost=35.0)
        cost = cp.estimate("US", "BUY", 10_000.0)
        # max(35, 10000 * 0.0024) = max(35, 24) = 35
        assert cost == pytest.approx(35.0)

    def test_manual_set_source_tag(self):
        cp = CostProfile.from_defaults()
        cp.manual_set("tw_sell", rate=0.0005, min_cost=10.0)
        assert cp.tw_sell.source == "manual_override"

    def test_manual_set_invalid_key_raises(self):
        cp = CostProfile.from_defaults()
        with pytest.raises(ValueError, match="無效成本率"):
            cp.manual_set("us_both", rate=0.001, min_cost=5.0)  # type: ignore[arg-type]

    def test_reset_to_default_after_override(self):
        cp = CostProfile.from_defaults()
        cp.manual_set("us_buy", rate=0.005, min_cost=50.0)
        cp.reset_to_default("us_buy")
        assert cp.us_buy.rate == pytest.approx(0.0001)
        assert cp.us_buy.min_cost == pytest.approx(0.01)
        assert cp.us_buy.source == "default"

    def test_reset_invalid_key_raises(self):
        cp = CostProfile.from_defaults()
        with pytest.raises(ValueError, match="無效成本率"):
            cp.reset_to_default("xx_buy")  # type: ignore[arg-type]

    def test_manual_set_updates_last_updated(self):
        cp = CostProfile.from_defaults()
        old = cp.last_updated
        cp.manual_set("tw_buy", rate=0.001, min_cost=15.0)
        assert cp.last_updated >= old


# ── record_actual (V2.0 no-op) ───────────────────────────────────────────────

class TestRecordActual:
    def test_record_actual_does_not_change_rates(self):
        cp = CostProfile.from_defaults()
        before = cp.to_dict()
        cp.record_actual("NVDA", "US", "BUY", 10_000.0, 2.50)
        after = cp.to_dict()
        # last_updated may differ; compare the rate fields
        for key in ("us_buy", "us_sell", "tw_buy", "tw_sell"):
            assert after[key]["rate"] == before[key]["rate"]

    def test_record_actual_does_not_raise(self):
        cp = CostProfile.from_defaults()
        cp.record_actual("2330.TW", "TW", "SELL", 200_000.0, 900.0)


# ── _get_entry (invalid market / side) ────────────────────────────────────────

class TestGetEntry:
    def test_estimate_invalid_market_raises(self):
        cp = CostProfile.from_defaults()
        with pytest.raises(ValueError, match="未知 market"):
            cp.estimate("JP", "BUY", 1000.0)  # type: ignore[arg-type]

    def test_estimate_invalid_side_raises(self):
        cp = CostProfile.from_defaults()
        with pytest.raises(ValueError, match="未知 market"):
            cp.estimate("US", "SHORT", 1000.0)  # type: ignore[arg-type]


# ── RateEntry serialisation ───────────────────────────────────────────────────

class TestRateEntrySerde:
    def test_roundtrip_with_n_samples(self):
        entry = RateEntry(rate=0.0005, min_cost=10.0, source="learned", n_samples=42)
        d = entry.to_dict()
        assert d["min"] == 10.0
        assert "min_cost" not in d
        assert d["n_samples"] == 42
        entry2 = RateEntry.from_dict(d)
        assert entry2.rate == pytest.approx(0.0005)
        assert entry2.n_samples == 42

    def test_roundtrip_without_n_samples(self):
        entry = RateEntry(rate=0.0001, min_cost=0.01, source="default")
        d = entry.to_dict()
        assert "n_samples" not in d
        entry2 = RateEntry.from_dict(d)
        assert entry2.n_samples is None
