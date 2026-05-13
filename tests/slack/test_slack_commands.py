"""Unit tests for Sprint 4 Slack command handlers in src/slack_bot.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.discipline.metrics import DisciplineMetrics
from src.portfolio.state import PortfolioState
from src.rebalancer.trade_builder import BuildResult, Trade


# ── Fixture: SlackWarden without live Slack connection ────────────────────────

@pytest.fixture
def warden(tmp_path: Path):
    """SlackWarden with mocked App and isolated tmp_path file paths."""
    with patch("slack_bolt.App"):
        from src.slack_bot import SlackWarden
        w: SlackWarden = object.__new__(SlackWarden)
        w.app = MagicMock()
        w._channel_id = "C123"
        w._adhoc_fn = None
        w._refresh_fn = None
        w._check_auctions_fn = None
        w._rebalance_fn = None
        w._state_path = tmp_path / "state.json"
        w._decision_archive = tmp_path / "decisions.jsonl"
        w._actual_trades_path = tmp_path / "actual_trades.jsonl"
    return w


def _say() -> MagicMock:
    return MagicMock()


def _msg(ts: str = "1234567890.000001") -> dict:
    return {"ts": ts}


def _ctx(*matches) -> dict:
    return {"matches": matches}


def _sample_trade_dict(
    ticker: str = "NVDA",
    side: str = "BUY",
    conviction: float = 7.4,
    tier: str = "Execute",
) -> dict:
    return {
        "ticker": ticker,
        "side": side,
        "conviction": conviction,
        "execution_tier": tier,
        "rationale": "+1.8 score",
        "bindings": [],
        "quantity": 4.3,
        "est_price": 920.0,
        "notional": 3956.0,
        "est_cost": 0.40,
        "est_cost_breakdown": {"commission": 0.40, "tax": 0.0, "fx_spread": 0.0},
        "delta_weight": 0.04,
        "target_weight": 0.12,
        "actual_cost": None,
        "actual_cost_breakdown": None,
    }


def _sample_archive_record(
    market: str = "US",
    is_hold: bool = False,
    trades: list[dict] | None = None,
) -> dict:
    return {
        "timestamp": "2026-05-12T08:30:00+08:00",
        "market": market,
        "is_hold": is_hold,
        "hold_reasons": [],
        "trades": trades or [_sample_trade_dict()],
    }


# ── Tests: /rebalance preview ─────────────────────────────────────────────────

class TestRebalancePreview:
    def test_no_pipeline_graceful_error(self, warden):
        say = _say()
        warden._on_rebalance_preview(_msg(), say, _ctx(None))
        say.assert_called_once()
        text = say.call_args[1].get("text", "")
        assert "Sprint 5" in text or "未連接" in text or "尚未" in text

    def test_calls_rebalance_fn_with_market(self, warden):
        result = BuildResult(trades=[], is_hold=True, hold_reasons=["min_turnover"], market="US")
        metrics = DisciplineMetrics(
            drift_pct=2.0, turnover_30d=5.0, last_trade_days=9, discipline_score_7d=78
        )
        warden._rebalance_fn = MagicMock(return_value=(result, metrics))

        warden._on_rebalance_preview(_msg(), _say(), _ctx("US"))

        warden._rebalance_fn.assert_called_once_with("US")

    def test_posts_blocks_to_channel(self, warden):
        result = BuildResult(trades=[], is_hold=True, hold_reasons=["min_turnover"], market="US")
        metrics = DisciplineMetrics(
            drift_pct=2.0, turnover_30d=5.0, last_trade_days=9, discipline_score_7d=78
        )
        warden._rebalance_fn = MagicMock(return_value=(result, metrics))

        warden._on_rebalance_preview(_msg(), _say(), _ctx("US"))

        warden.app.client.chat_postMessage.assert_called_once()
        kwargs = warden.app.client.chat_postMessage.call_args[1]
        assert "blocks" in kwargs
        assert isinstance(kwargs["blocks"], list)

    def test_all_market_calls_fn_twice(self, warden):
        result = BuildResult(trades=[], is_hold=True, hold_reasons=["min_turnover"], market="US")
        metrics = DisciplineMetrics(
            drift_pct=2.0, turnover_30d=5.0, last_trade_days=9, discipline_score_7d=78
        )
        warden._rebalance_fn = MagicMock(return_value=(result, metrics))

        warden._on_rebalance_preview(_msg(), _say(), _ctx("ALL"))

        assert warden._rebalance_fn.call_count == 2

    def test_pipeline_error_handled(self, warden):
        warden._rebalance_fn = MagicMock(side_effect=RuntimeError("pipeline down"))
        say = _say()
        warden._on_rebalance_preview(_msg(), say, _ctx("US"))
        say.assert_called()
        text = say.call_args[1].get("text", "")
        assert "ERROR" in text or "失敗" in text


# ── Tests: /holdings show ─────────────────────────────────────────────────────

class TestHoldingsShow:
    def test_missing_state_file_error(self, warden):
        say = _say()
        warden._on_holdings_show(_msg(), say, _ctx(None))
        say.assert_called_once()
        text = say.call_args[1].get("text", "")
        assert "❌" in text or "不存在" in text

    def test_shows_us_holdings(self, warden):
        state = PortfolioState.create_empty()
        state.us_cash_usd = 1000.0
        state.us_holdings = {"NVDA": 4.3}
        state.save(warden._state_path)

        say = _say()
        warden._on_holdings_show(_msg(), say, _ctx("US"))

        say.assert_called_once()
        text = say.call_args[1].get("text", "")
        assert "NVDA" in text
        assert "1000" in text

    def test_shows_tw_holdings(self, warden):
        state = PortfolioState.create_empty()
        state.tw_cash_twd = 500000.0
        state.tw_holdings = {"2330.TW": 1000.0}
        state.save(warden._state_path)

        say = _say()
        warden._on_holdings_show(_msg(), say, _ctx("TW"))

        say.assert_called_once()
        text = say.call_args[1].get("text", "")
        assert "2330.TW" in text

    def test_no_market_arg_shows_both(self, warden):
        state = PortfolioState.create_empty()
        state.us_holdings = {"NVDA": 4.0}
        state.tw_holdings = {"2330.TW": 1000.0}
        state.save(warden._state_path)

        say = _say()
        warden._on_holdings_show(_msg(), say, _ctx(None))

        text = say.call_args[1].get("text", "")
        assert "US" in text
        assert "TW" in text


# ── Tests: /why <ticker> ──────────────────────────────────────────────────────

class TestWhy:
    def test_no_archive_returns_not_found(self, warden):
        say = _say()
        warden._on_why(_msg(), say, _ctx("NVDA"))
        say.assert_called_once()
        text = say.call_args[1].get("text", "")
        assert "❌" in text or "找不到" in text

    def test_finds_ticker_shows_conviction(self, warden):
        rec = _sample_archive_record()
        warden._decision_archive.write_text(json.dumps(rec) + "\n")

        say = _say()
        warden._on_why(_msg(), say, _ctx("NVDA"))

        say.assert_called_once()
        text = say.call_args[1].get("text", "")
        assert "NVDA" in text
        assert "7.4" in text

    def test_ticker_not_in_trades_returns_error(self, warden):
        rec = _sample_archive_record()
        warden._decision_archive.write_text(json.dumps(rec) + "\n")

        say = _say()
        warden._on_why(_msg(), say, _ctx("AAPL"))

        text = say.call_args[1].get("text", "")
        assert "❌" in text or "不在" in text

    def test_no_matches_no_response(self, warden):
        say = _say()
        warden._on_why(_msg(), say, _ctx())
        say.assert_not_called()

    def test_shows_conviction_components_when_present(self, warden):
        trade = _sample_trade_dict()
        trade["conviction_components"] = {
            "score_delta_pct": 0.82,
            "constraint_binding": 0.20,
            "cov_certainty": 0.95,
            "consistency": 0.70,
            "timing": 0.87,
        }
        rec = _sample_archive_record(trades=[trade])
        warden._decision_archive.write_text(json.dumps(rec) + "\n")

        say = _say()
        warden._on_why(_msg(), say, _ctx("NVDA"))

        text = say.call_args[1].get("text", "")
        assert "Score delta" in text or "分解" in text

    def test_missing_components_shows_fallback(self, warden):
        rec = _sample_archive_record()  # no conviction_components
        warden._decision_archive.write_text(json.dumps(rec) + "\n")

        say = _say()
        warden._on_why(_msg(), say, _ctx("NVDA"))

        text = say.call_args[1].get("text", "")
        assert "only summary" in text or "not archived" in text


# ── Tests: /trade add ─────────────────────────────────────────────────────────

class TestTradeAddMessage:
    def test_posts_open_button(self, warden):
        say = _say()
        warden._on_trade_add_msg(_msg(), say, _ctx())
        say.assert_called_once()
        kwargs = say.call_args[1]
        assert "blocks" in kwargs
        elems = kwargs["blocks"][0].get("elements", [])
        action_ids = [e.get("action_id") for e in elems]
        assert "trade_open_modal" in action_ids


class TestTradeOpenModal:
    def test_opens_modal(self, warden):
        ack = MagicMock()
        body = {"trigger_id": "T12345", "actions": [{"value": "open"}]}
        client = MagicMock()
        warden._on_trade_open_modal(ack, body, client)
        ack.assert_called_once()
        client.views_open.assert_called_once()


class TestTradeAddSubmit:
    def _body(self, **overrides) -> dict:
        values = {
            "market": {"market_select": {"selected_option": {"value": "US"}}},
            "ticker": {"ticker_input": {"value": "NVDA"}},
            "side": {"side_select": {"selected_option": {"value": "BUY"}}},
            "quantity": {"quantity_input": {"value": "4"}},
            "filled_price": {"filled_price_input": {"value": "920.0"}},
            "commission": {"commission_input": {"value": "0.40"}},
            "tax": {"tax_input": {"value": "0"}},
            "fx": {"fx_input": {"value": "1.0"}},
            "system_suggested": {"system_suggested_check": {"selected_options": []}},
        }
        values.update(overrides)
        return {"view": {"state": {"values": values}}, "user": {"id": "U123"}}

    def test_writes_to_jsonl(self, warden):
        state = PortfolioState.create_empty()
        state.us_cash_usd = 10000.0
        state.save(warden._state_path)

        ack = MagicMock()
        warden._on_trade_add_submit(ack, self._body(), MagicMock())

        ack.assert_called_once()
        assert warden._actual_trades_path.exists()
        lines = warden._actual_trades_path.read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["ticker"] == "NVDA"
        assert rec["side"] == "BUY"
        assert rec["status"] == "reconciled"

    def test_updates_portfolio_state(self, warden):
        state = PortfolioState.create_empty()
        state.us_cash_usd = 10000.0
        state.save(warden._state_path)

        warden._on_trade_add_submit(MagicMock(), self._body(), MagicMock())

        updated = PortfolioState.load(warden._state_path)
        assert updated.us_holdings.get("NVDA", 0) == pytest.approx(4.0)

    def test_zero_quantity_rejected(self, warden):
        client = MagicMock()
        body = self._body()
        body["view"]["state"]["values"]["quantity"]["quantity_input"]["value"] = "0"
        warden._on_trade_add_submit(MagicMock(), body, client)
        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args[1].get("text", "")
        assert "❌" in text or "大於" in text

    def test_system_suggested_flag(self, warden):
        state = PortfolioState.create_empty()
        state.us_cash_usd = 50000.0
        state.save(warden._state_path)

        body = self._body()
        body["view"]["state"]["values"]["system_suggested"]["system_suggested_check"][
            "selected_options"
        ] = [{"value": "yes"}]

        warden._on_trade_add_submit(MagicMock(), body, MagicMock())

        rec = json.loads(warden._actual_trades_path.read_text().splitlines()[0])
        assert rec["system_suggested"] is True


# ── Tests: Approve button ─────────────────────────────────────────────────────

class TestRebalanceApprove:
    def _body(self, market: str = "US") -> dict:
        return {
            "actions": [{"value": market}],
            "channel": {"id": "C123"},
            "message": {"ts": "1234"},
        }

    def test_no_archive_returns_error(self, warden):
        ack = MagicMock()
        client = MagicMock()
        warden._on_rebalance_approve(ack, self._body(), client)
        ack.assert_called_once()
        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args[1].get("text", "")
        assert "❌" in text or "找不到" in text

    def test_approve_writes_pending_confirmation(self, warden):
        state = PortfolioState.create_empty()
        state.us_cash_usd = 50000.0
        state.save(warden._state_path)

        rec = _sample_archive_record()
        warden._decision_archive.write_text(json.dumps(rec) + "\n")

        warden._on_rebalance_approve(MagicMock(), self._body("US"), MagicMock())

        assert warden._actual_trades_path.exists()
        written = json.loads(warden._actual_trades_path.read_text().splitlines()[0])
        assert written["ticker"] == "NVDA"
        assert written["system_suggested"] is True
        assert written["status"] == "pending_confirmation"

    def test_approve_updates_portfolio_state(self, warden):
        state = PortfolioState.create_empty()
        state.us_cash_usd = 50000.0
        state.save(warden._state_path)

        rec = _sample_archive_record()
        warden._decision_archive.write_text(json.dumps(rec) + "\n")

        warden._on_rebalance_approve(MagicMock(), self._body("US"), MagicMock())

        updated = PortfolioState.load(warden._state_path)
        assert updated.us_holdings.get("NVDA", 0) == pytest.approx(4.3)

    def test_no_execute_trades_info_message(self, warden):
        rec = _sample_archive_record(
            trades=[_sample_trade_dict("MSFT", "BUY", 2.8, "Skip")]
        )
        warden._decision_archive.write_text(json.dumps(rec) + "\n")

        client = MagicMock()
        warden._on_rebalance_approve(MagicMock(), self._body("US"), client)

        text = client.chat_postMessage.call_args[1].get("text", "")
        assert "ℹ️" in text or "無" in text

    def test_ack_called(self, warden):
        ack = MagicMock()
        warden._on_rebalance_approve(ack, self._body(), MagicMock())
        ack.assert_called_once()


# ── Tests: stretch stubs ──────────────────────────────────────────────────────

class TestRebalanceConfigCommand:
    def test_show_no_overrides(self, warden, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        say = _say()
        warden._on_stub_rebalance_config({"text": "!rebalance config show"}, say, _ctx())
        text = say.call_args[1]["text"]
        assert "無 override" in text

    def test_show_active_override(self, warden, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = tmp_path / "data" / "rebalance_config_override.json"
        cfg.parent.mkdir()
        cfg.write_text(json.dumps({"US": {"max_position": 0.99}}), encoding="utf-8")
        say = _say()
        warden._on_stub_rebalance_config({"text": "!rebalance config show us"}, say, _ctx())
        text = say.call_args[1]["text"]
        assert "max_position" in text
        assert "0.99" in text

    def test_set_writes_override_file(self, warden, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        say = _say()
        warden._on_stub_rebalance_config(
            {"text": "!rebalance config set us max_position 0.25"}, say, _ctx()
        )
        text = say.call_args[1]["text"]
        assert "✅" in text
        data = json.loads((tmp_path / "data" / "rebalance_config_override.json").read_text())
        assert data["US"]["max_position"] == pytest.approx(0.25)

    def test_set_invalid_market(self, warden, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        say = _say()
        warden._on_stub_rebalance_config(
            {"text": "!rebalance config set jp max_position 0.25"}, say, _ctx()
        )
        text = say.call_args[1]["text"]
        assert "❌" in text and "market" in text

    def test_set_invalid_field(self, warden, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        say = _say()
        warden._on_stub_rebalance_config(
            {"text": "!rebalance config set us bad_field 0.25"}, say, _ctx()
        )
        text = say.call_args[1]["text"]
        assert "❌" in text and "field" in text


class TestCashCommand:
    def test_show_no_state_file_returns_error(self, warden):
        say = _say()
        warden._on_stub_cash({"text": "!cash show us"}, say, _ctx())
        text = say.call_args[1]["text"]
        assert "❌" in text

    def test_show_returns_cash_amounts(self, warden, tmp_path):
        from src.portfolio.state import PortfolioState
        state = PortfolioState.create_empty()
        state.us_cash_usd = 5_000.0
        state.tw_cash_twd = 100_000.0
        state.save(warden._state_path)
        say = _say()
        warden._on_stub_cash({"text": "!cash show"}, say, _ctx())
        text = say.call_args[1]["text"]
        assert "5,000.00" in text
        assert "100,000" in text

    def test_set_updates_cash(self, warden):
        from src.portfolio.state import PortfolioState
        state = PortfolioState.create_empty()
        state.us_cash_usd = 1_000.0
        state.save(warden._state_path)
        say = _say()
        warden._on_stub_cash({"text": "!cash set us 8000"}, say, _ctx())
        text = say.call_args[1]["text"]
        assert "✅" in text
        loaded = PortfolioState.load(warden._state_path)
        assert loaded.us_cash_usd == pytest.approx(8_000.0)

    def test_adjust_reports_v21_deferral(self, warden):
        say = _say()
        warden._on_stub_cash({"text": "!cash adjust"}, say, _ctx())
        assert "V2.1" in say.call_args[1]["text"]

    def test_unknown_subcmd_shows_usage(self, warden):
        say = _say()
        warden._on_stub_cash({"text": "!cash badcmd"}, say, _ctx())
        assert "用法" in say.call_args[1]["text"]


class TestIpoCommand:
    def test_list_empty(self, warden):
        from src.portfolio.state import PortfolioState
        PortfolioState.create_empty().save(warden._state_path)
        say = _say()
        warden._on_stub_ipo({"text": "!ipo list"}, say, _ctx())
        assert "無待審" in say.call_args[1]["text"]

    def test_apply_insufficient_cash_rejected(self, warden):
        from src.portfolio.state import PortfolioState
        state = PortfolioState.create_empty()
        state.tw_cash_twd = 10_000.0
        state.save(warden._state_path)
        say = _say()
        warden._on_stub_ipo({"text": "!ipo apply 6488.TW 500000 2026-06-01"}, say, _ctx())
        text = say.call_args[1]["text"]
        assert "❌" in text and "不足" in text

    def test_apply_records_subscription(self, warden):
        from src.portfolio.state import PortfolioState
        state = PortfolioState.create_empty()
        state.tw_cash_twd = 200_000.0
        state.save(warden._state_path)
        say = _say()
        warden._on_stub_ipo({"text": "!ipo apply 6488.TW 100000 2026-06-15"}, say, _ctx())
        assert "✅" in say.call_args[1]["text"]
        loaded = PortfolioState.load(warden._state_path)
        assert len(loaded.tw_pending_ipo_details) == 1
        assert loaded.tw_cash_twd == pytest.approx(100_000.0)

    def test_release_not_in_list_returns_error(self, warden):
        from src.portfolio.state import PortfolioState
        PortfolioState.create_empty().save(warden._state_path)
        say = _say()
        warden._on_stub_ipo({"text": "!ipo release 9999.TW awarded"}, say, _ctx())
        assert "❌" in say.call_args[1]["text"]

    def test_unknown_subcmd_shows_usage(self, warden):
        say = _say()
        warden._on_stub_ipo({"text": "!ipo badcmd"}, say, _ctx())
        assert "用法" in say.call_args[1]["text"]
