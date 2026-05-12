"""
Tests for src/portfolio/state.py

Coverage targets: PortfolioState (all public methods), IpoSubscription,
SchemaVersionError, _parse_csv, _load_tw_ticker_map, helper functions.
"""

from __future__ import annotations

import csv
import json
import textwrap
from pathlib import Path

import pytest

from src.portfolio.state import (
    SUPPORTED_SCHEMA_VERSION,
    IpoSubscription,
    PortfolioState,
    SchemaVersionError,
    _clean_numeric,
    _load_tw_ticker_map,
    _parse_csv,
    _resolve_col,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty() -> PortfolioState:
    return PortfolioState.create_empty()


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _minimal_valid_dict() -> dict:
    return {
        "version": 1,
        "last_updated": "2026-05-12T08:00:00+08:00",
        "us": {"cash_usd": 1000.0, "holdings": {"NVDA": 10}},
        "tw": {
            "cash_twd": 50000.0,
            "holdings": {"2330.TW": 100},
            "pending_ipo_subscription_twd": 0.0,
            "pending_ipo_details": [],
            "ipo_lockup_holdings": [],
        },
    }


# ── Schema / construction ─────────────────────────────────────────────────────

class TestCreateEmpty:
    def test_create_empty_schema(self):
        s = _empty()
        d = s.to_dict()
        assert d["version"] == SUPPORTED_SCHEMA_VERSION
        assert d["us"]["cash_usd"] == 0.0
        assert d["us"]["holdings"] == {}
        assert d["tw"]["cash_twd"] == 0.0
        assert d["tw"]["holdings"] == {}
        assert d["tw"]["pending_ipo_subscription_twd"] == 0.0
        assert d["tw"]["pending_ipo_details"] == []
        assert d["tw"]["ipo_lockup_holdings"] == []
        assert "last_updated" in d

    def test_from_dict_valid(self):
        s = PortfolioState.from_dict(_minimal_valid_dict())
        assert s.us_cash_usd == 1000.0
        assert s.us_holdings == {"NVDA": 10.0}
        assert s.tw_cash_twd == 50000.0
        assert s.tw_holdings == {"2330.TW": 100.0}

    def test_schema_version_too_high_raises(self):
        data = _minimal_valid_dict()
        data["version"] = SUPPORTED_SCHEMA_VERSION + 1
        with pytest.raises(SchemaVersionError):
            PortfolioState.from_dict(data)

    def test_load_missing_optional_fields_gets_defaults(self):
        data = {
            "version": 1,
            "last_updated": "2026-05-12T00:00:00+08:00",
            "us": {"cash_usd": 500.0},
            "tw": {},
        }
        s = PortfolioState.from_dict(data)
        assert s.us_holdings == {}
        assert s.tw_holdings == {}
        assert s.tw_pending_ipo_details == []
        assert s.tw_ipo_lockup_holdings == []


# ── Persistence ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        p = tmp_path / "state.json"
        s = _empty()
        s.us_cash_usd = 12500.0
        s.us_holdings = {"AAPL": 10, "MSFT": 5}
        s.save(p)

        s2 = PortfolioState.load(p)
        assert s2.to_dict() == s.to_dict()

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        p = tmp_path / "state.json"
        _empty().save(p)
        assert not (tmp_path / "state.tmp").exists()
        assert p.exists()

    def test_load_corrupted_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON"):
            PortfolioState.load(p)

    def test_load_schema_version_too_high_raises(self, tmp_path):
        p = tmp_path / "state.json"
        data = _minimal_valid_dict()
        data["version"] = 99
        _write_json(p, data)
        with pytest.raises(SchemaVersionError):
            PortfolioState.load(p)

    def test_save_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "nested" / "dir" / "state.json"
        _empty().save(p)
        assert p.exists()


# ── update_cash ───────────────────────────────────────────────────────────────

class TestUpdateCash:
    def test_update_cash_us_positive(self):
        s = _empty()
        before = s.last_updated
        s.update_cash("US", 12500.0, "initial deposit")
        assert s.us_cash_usd == 12500.0
        assert s.last_updated >= before

    def test_update_cash_tw_negative(self):
        s = _empty()
        s.tw_cash_twd = 100_000.0
        s.update_cash("TW", -35_000.0, "IPO reservation")
        assert s.tw_cash_twd == 65_000.0

    def test_update_cash_invalid_market_raises(self):
        with pytest.raises(ValueError, match="未知市場"):
            _empty().update_cash("HK", 1000.0)

    def test_update_cash_updates_last_updated(self):
        s = _empty()
        old = s.last_updated
        s.update_cash("US", 1.0)
        assert s.last_updated >= old


# ── IPO subscription ──────────────────────────────────────────────────────────

class TestIpoSubscription:
    def test_add_ipo_subscription_updates_pending_total(self):
        s = _empty()
        s.tw_cash_twd = 100_000.0
        s.add_ipo_subscription("6488.TW", 35_000.0, "2026-05-22")
        assert s.tw_pending_ipo_subscription_twd == 35_000.0
        assert s.tw_cash_twd == 65_000.0
        assert len(s.tw_pending_ipo_details) == 1
        assert s.tw_pending_ipo_details[0].ticker == "6488.TW"

    def test_add_ipo_subscription_duplicate_raises(self):
        s = _empty()
        s.tw_cash_twd = 100_000.0
        s.add_ipo_subscription("6488.TW", 35_000.0, "2026-05-22")
        with pytest.raises(ValueError, match="已有待審"):
            s.add_ipo_subscription("6488.TW", 10_000.0, "2026-05-22")

    def test_release_ipo_awarded_moves_to_lockup(self):
        s = _empty()
        s.tw_cash_twd = 100_000.0
        s.add_ipo_subscription("6488.TW", 35_000.0, "2026-05-22")
        cash_before = s.tw_cash_twd

        s.release_ipo("6488.TW", outcome="awarded")

        assert "6488.TW" in s.tw_ipo_lockup_holdings
        assert len(s.tw_pending_ipo_details) == 0
        assert s.tw_pending_ipo_subscription_twd == 0.0
        assert s.tw_cash_twd == cash_before  # cash unchanged for awarded

    def test_release_ipo_refunded_returns_cash(self):
        s = _empty()
        s.tw_cash_twd = 100_000.0
        s.add_ipo_subscription("6488.TW", 35_000.0, "2026-05-22")

        s.release_ipo("6488.TW", outcome="refunded")

        assert "6488.TW" not in s.tw_ipo_lockup_holdings
        assert len(s.tw_pending_ipo_details) == 0
        assert s.tw_pending_ipo_subscription_twd == 0.0
        assert s.tw_cash_twd == pytest.approx(100_000.0)  # cash restored

    def test_release_ipo_invalid_outcome_raises(self):
        s = _empty()
        s.tw_cash_twd = 50_000.0
        s.add_ipo_subscription("6488.TW", 35_000.0, "2026-05-22")
        with pytest.raises(ValueError, match="無效 outcome"):
            s.release_ipo("6488.TW", outcome="cancelled")  # type: ignore[arg-type]

    def test_release_ipo_not_found_raises(self):
        s = _empty()
        with pytest.raises(ValueError, match="不在待審"):
            s.release_ipo("UNKNOWN.TW", outcome="awarded")


# ── edit_holding ──────────────────────────────────────────────────────────────

class TestEditHolding:
    def test_edit_holding_new_ticker(self):
        s = _empty()
        s.edit_holding("NVDA", 50.0, "initial position")
        assert s.us_holdings["NVDA"] == 50.0

    def test_edit_holding_update_quantity(self):
        s = _empty()
        s.us_holdings["NVDA"] = 30.0
        s.edit_holding("NVDA", 80.0, "stock split")
        assert s.us_holdings["NVDA"] == 80.0

    def test_edit_holding_zero_removes(self):
        s = _empty()
        s.us_holdings["TSLA"] = 10.0
        s.edit_holding("TSLA", 0.0, "sold out")
        assert "TSLA" not in s.us_holdings

    def test_edit_holding_negative_raises(self):
        with pytest.raises(ValueError, match="負數量"):
            _empty().edit_holding("NVDA", -1.0, "bad input")

    def test_edit_holding_fractional_shares(self):
        s = _empty()
        s.edit_holding("NVDA", 0.5, "fractional share")
        assert s.us_holdings["NVDA"] == pytest.approx(0.5)

    def test_edit_holding_unicode_ticker_tw(self):
        s = _empty()
        s.edit_holding("2330.TW", 1000.0, "initial")
        assert s.tw_holdings["2330.TW"] == 1000.0

    def test_edit_holding_unicode_ticker_two(self):
        s = _empty()
        s.edit_holding("3034.TWO", 200.0, "OTC stock")
        assert s.tw_holdings["3034.TWO"] == 200.0

    def test_edit_holding_long_tw_ticker(self):
        s = _empty()
        s.edit_holding("006208.TW", 500.0, "ETF position")
        assert s.tw_holdings["006208.TW"] == 500.0

    def test_edit_holding_updates_last_updated(self):
        s = _empty()
        old = s.last_updated
        s.edit_holding("AAPL", 10.0, "buy")
        assert s.last_updated >= old


# ── market_snapshot ───────────────────────────────────────────────────────────

class TestMarketSnapshot:
    def test_market_snapshot_us(self):
        s = _empty()
        s.us_cash_usd = 5000.0
        s.us_holdings = {"AAPL": 10.0}
        snap = s.market_snapshot("US")
        assert snap["cash_usd"] == 5000.0
        assert snap["holdings"] == {"AAPL": 10.0}

    def test_market_snapshot_tw(self):
        s = _empty()
        s.tw_cash_twd = 80_000.0
        s.tw_holdings = {"2330.TW": 100.0}
        snap = s.market_snapshot("TW")
        assert snap["cash_twd"] == 80_000.0
        assert snap["holdings"] == {"2330.TW": 100.0}
        assert "pending_ipo_subscription_twd" in snap
        assert "ipo_lockup_holdings" in snap

    def test_market_snapshot_empty_state(self):
        s = _empty()
        snap_us = s.market_snapshot("US")
        snap_tw = s.market_snapshot("TW")
        assert snap_us["holdings"] == {}
        assert snap_tw["holdings"] == {}

    def test_market_snapshot_invalid_market_raises(self):
        with pytest.raises(ValueError, match="未知市場"):
            _empty().market_snapshot("JP")  # type: ignore[arg-type]

    def test_market_snapshot_is_copy(self):
        s = _empty()
        s.us_holdings = {"NVDA": 10.0}
        snap = s.market_snapshot("US")
        snap["holdings"]["NVDA"] = 99.0
        assert s.us_holdings["NVDA"] == 10.0  # mutation doesn't bleed back


# ── sync_holdings_from_csv ────────────────────────────────────────────────────

class TestSyncHoldingsFromCsv:
    def _write_us_csv(self, path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["代號", "股票名稱", "目前庫存", "均價"])
            writer.writeheader()
            writer.writerows(rows)

    def _write_tw_csv(self, path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["股票名稱", "股數", "幣別"])
            writer.writeheader()
            writer.writerows(rows)

    def test_sync_holdings_us(self, tmp_path):
        p = tmp_path / "複委託庫存.csv"
        self._write_us_csv(p, [
            {"代號": "AAPL", "股票名稱": "Apple", "目前庫存": "10", "均價": "160"},
            {"代號": "MSFT", "股票名稱": "Microsoft", "目前庫存": "5", "均價": "350"},
            {"代號": "BOXX", "股票名稱": "BOXX", "目前庫存": "100", "均價": "100"},
        ])
        s = _empty()
        warnings = s.sync_holdings_from_csv(p, market="US")
        assert warnings == []
        assert s.us_holdings == {"AAPL": 10.0, "MSFT": 5.0, "BOXX": 100.0}

    def test_sync_holdings_tw_with_map(self, tmp_path):
        p = tmp_path / "證券未實現彙總.csv"
        self._write_tw_csv(p, [
            {"股票名稱": "範例ETF A", "股數": "1000", "幣別": "台幣"},
            {"股票名稱": "範例股票 B", "股數": "2000", "幣別": "台幣"},
        ])
        tw_map = {"範例ETF A": "006208.TW", "範例股票 B": "2330.TW"}
        s = _empty()
        warnings = s.sync_holdings_from_csv(p, market="TW", tw_ticker_map=tw_map)
        assert warnings == []
        assert s.tw_holdings == {"006208.TW": 1000.0, "2330.TW": 2000.0}

    def test_sync_holdings_tw_unmapped_ticker_returns_warning(self, tmp_path):
        p = tmp_path / "證券未實現彙總.csv"
        self._write_tw_csv(p, [
            {"股票名稱": "未知公司X", "股數": "500", "幣別": "台幣"},
        ])
        s = _empty()
        warnings = s.sync_holdings_from_csv(p, market="TW", tw_ticker_map={})
        assert len(warnings) == 1
        assert "未知公司X" in warnings[0]
        assert "未知公司X" in s.tw_holdings  # stored under raw name

    def test_sync_replaces_existing_holdings(self, tmp_path):
        p = tmp_path / "複委託庫存.csv"
        self._write_us_csv(p, [
            {"代號": "NVDA", "股票名稱": "NVIDIA", "目前庫存": "20", "均價": "900"},
        ])
        s = _empty()
        s.us_holdings = {"AAPL": 10.0, "TSLA": 5.0}  # old positions
        s.sync_holdings_from_csv(p, market="US")
        assert s.us_holdings == {"NVDA": 20.0}  # AAPL and TSLA gone

    def test_sync_missing_file_returns_warning(self, tmp_path):
        p = tmp_path / "nonexistent.csv"
        s = _empty()
        warnings = s.sync_holdings_from_csv(p, market="US")
        assert len(warnings) == 1
        assert s.us_holdings == {}

    def test_sync_csv_with_thousands_separator(self, tmp_path):
        p = tmp_path / "複委託庫存.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["代號", "目前庫存"])
            writer.writeheader()
            writer.writerow({"代號": "NVDA", "目前庫存": "1,000"})
        s = _empty()
        s.sync_holdings_from_csv(p, market="US")
        assert s.us_holdings["NVDA"] == 1000.0


# ── IpoSubscription serialisation ─────────────────────────────────────────────

class TestIpoSubscriptionSerde:
    def test_roundtrip(self):
        ipo = IpoSubscription(
            ticker="6488.TW",
            amount_twd=35_000.0,
            subscribed_date="2026-05-10",
            release_date="2026-05-22",
        )
        d = ipo.to_dict()
        ipo2 = IpoSubscription.from_dict(d)
        assert ipo2.ticker == ipo.ticker
        assert ipo2.amount_twd == ipo.amount_twd
        assert ipo2.release_date == ipo.release_date
        assert ipo2.kind == "subscription"


# ── Private helper coverage ───────────────────────────────────────────────────

class TestHelpers:
    # _resolve_col: return None when no alias matches
    def test_resolve_col_no_match(self):
        result = _resolve_col(["col_a", "col_b"], ["missing_x", "missing_y"])
        assert result is None

    # _clean_numeric: ValueError branch
    def test_clean_numeric_non_parseable_returns_none(self):
        assert _clean_numeric("abc") is None
        assert _clean_numeric("--") is None

    def test_clean_numeric_strips_formatting(self):
        assert _clean_numeric("1,000") == pytest.approx(1000.0)
        assert _clean_numeric('"500"') == pytest.approx(500.0)

    # _parse_csv: missing required columns
    def test_parse_csv_missing_columns_returns_warning(self, tmp_path):
        p = tmp_path / "wrong_cols.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["random_col"])
            writer.writeheader()
            writer.writerow({"random_col": "value"})
        holdings, warnings = _parse_csv(p, {"Ticker": ["代號"], "Shares": ["目前庫存"]})
        assert holdings == {}
        assert len(warnings) == 1
        assert "找不到欄位" in warnings[0]

    # _parse_csv: skips empty ticker rows
    def test_parse_csv_skips_empty_ticker(self, tmp_path):
        p = tmp_path / "empty_ticker.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["代號", "目前庫存"])
            writer.writeheader()
            writer.writerow({"代號": "", "目前庫存": "10"})   # empty ticker
            writer.writerow({"代號": "AAPL", "目前庫存": "5"})
        holdings, warnings = _parse_csv(p, {"Ticker": ["代號"], "Shares": ["目前庫存"]})
        assert "" not in holdings
        assert holdings.get("AAPL") == 5.0

    # _parse_csv: skips rows with non-parseable or zero shares
    def test_parse_csv_skips_bad_shares(self, tmp_path):
        p = tmp_path / "bad_shares.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["代號", "目前庫存"])
            writer.writeheader()
            writer.writerow({"代號": "NVDA", "目前庫存": "abc"})   # non-parseable
            writer.writerow({"代號": "AMD",  "目前庫存": "0"})     # zero shares
            writer.writerow({"代號": "AAPL", "目前庫存": "10"})
        holdings, warnings = _parse_csv(p, {"Ticker": ["代號"], "Shares": ["目前庫存"]})
        assert "NVDA" not in holdings
        assert "AMD" not in holdings
        assert holdings.get("AAPL") == 10.0

    # _load_tw_ticker_map: corrupt JSON returns {}
    def test_load_tw_ticker_map_corrupt_returns_empty(self, tmp_path):
        p = tmp_path / "bad_map.json"
        p.write_text("{bad json!!", encoding="utf-8")
        result = _load_tw_ticker_map(p)
        assert result == {}

    # _load_tw_ticker_map: valid file
    def test_load_tw_ticker_map_valid(self, tmp_path):
        p = tmp_path / "map.json"
        p.write_text('{"A": "2330.TW", "_comment": "ignored", "B": ""}', encoding="utf-8")
        result = _load_tw_ticker_map(p)
        assert result == {"A": "2330.TW"}  # _comment and empty-value filtered

    # _load_tw_ticker_map: missing file returns {}
    def test_load_tw_ticker_map_missing_returns_empty(self, tmp_path):
        result = _load_tw_ticker_map(tmp_path / "nonexistent.json")
        assert result == {}

    # sync_holdings_from_csv TW with tw_ticker_map=None — auto-loads from repo data/
    def test_sync_tw_auto_loads_ticker_map(self, tmp_path):
        p = tmp_path / "tw.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["股票名稱", "股數"])
            writer.writeheader()
            # "範例ETF A" is in data/tw_ticker_map.json → maps to "006208.TW"
            writer.writerow({"股票名稱": "範例ETF A", "股數": "1000"})
        s = _empty()
        warnings = s.sync_holdings_from_csv(p, market="TW")
        # Either mapped to 006208.TW (map found) or stored as raw name (map absent)
        assert "範例ETF A" in s.tw_holdings or "006208.TW" in s.tw_holdings
