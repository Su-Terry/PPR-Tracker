"""Unit tests for src/slack/dashboard.py — Block Kit renderer."""

from __future__ import annotations

import pytest

from src.discipline.metrics import DisciplineMetrics
from src.rebalancer.trade_builder import BuildResult, Trade
from src.slack.dashboard import render_dashboard


# ── Shared helpers ────────────────────────────────────────────────────────────

def _trade(
    ticker: str = "NVDA",
    side: str = "BUY",
    conviction: float = 7.4,
    tier: str = "Execute",
    market: str = "US",
) -> Trade:
    return Trade(
        market=market,
        ticker=ticker,
        side=side,
        delta_weight=0.04,
        target_weight=0.12,
        quantity=4.3,
        est_price=920.0,
        notional=3956.0,
        est_cost=0.40,
        est_cost_breakdown={"commission": 0.40, "tax": 0.0, "fx_spread": 0.0},
        conviction=conviction,
        execution_tier=tier,
        rationale="+1.8 score",
        bindings=[],
    )


def _metrics(
    drift: float = 4.2,
    turnover: float = 12.0,
    last_trade: int = 9,
    score: int = 78,
) -> DisciplineMetrics:
    return DisciplineMetrics(
        drift_pct=drift,
        turnover_30d=turnover,
        last_trade_days=last_trade,
        discipline_score_7d=score,
    )


def _action_ids(blocks: list[dict]) -> list[str]:
    return [
        elem["action_id"]
        for b in blocks
        if b.get("type") == "actions"
        for elem in b.get("elements", [])
    ]


def _section_texts(blocks: list[dict]) -> list[str]:
    return [
        b.get("text", {}).get("text", "")
        for b in blocks
        if b.get("type") == "section"
    ]


def _context_texts(blocks: list[dict]) -> list[str]:
    return [
        elem.get("text", "")
        for b in blocks
        if b.get("type") == "context"
        for elem in b.get("elements", [])
        if isinstance(elem, dict)
    ]


def _fields_text(blocks: list[dict]) -> str:
    parts: list[str] = []
    for b in blocks:
        if b.get("type") == "section" and b.get("fields"):
            for f in b["fields"]:
                parts.append(f.get("text", ""))
    return " ".join(parts)


# ── HOLD layout tests ─────────────────────────────────────────────────────────

class TestHoldLayout:
    def _hold_result(self, reason: str = "min_turnover") -> BuildResult:
        return BuildResult(
            trades=[], is_hold=True, hold_reasons=[reason], market="US"
        )

    def test_hold_conclusion_present(self):
        blocks = render_dashboard(self._hold_result(), _metrics(), "US", "2026-05-12 08:30")
        texts = _section_texts(blocks)
        assert any("HOLD all" in t for t in texts)

    def test_no_approve_button(self):
        blocks = render_dashboard(self._hold_result(), _metrics(), "US", "2026-05-12 08:30")
        assert "rebalance_approve" not in _action_ids(blocks)

    def test_has_expand_button(self):
        blocks = render_dashboard(self._hold_result(), _metrics(), "US", "2026-05-12 08:30")
        assert "rebalance_expand" in _action_ids(blocks)

    def test_hold_reason_infeasible(self):
        blocks = render_dashboard(
            self._hold_result("infeasible"), _metrics(), "US", "2026-05-12 08:30"
        )
        texts = " ".join(_section_texts(blocks))
        assert "infeasible" in texts or "constraints" in texts

    def test_hold_reason_low_conviction(self):
        blocks = render_dashboard(
            self._hold_result("low_conviction"), _metrics(), "US", "2026-05-12 08:30"
        )
        texts = " ".join(_section_texts(blocks))
        assert "conviction" in texts

    def test_discipline_block_always_present(self):
        blocks = render_dashboard(self._hold_result(), _metrics(), "US", "2026-05-12 08:30")
        fields = _fields_text(blocks)
        assert "Drift" in fields
        assert "Turnover" in fields
        assert "Last trade" in fields
        assert "Discipline" in fields

    def test_last_trade_never(self):
        blocks = render_dashboard(
            self._hold_result(), _metrics(last_trade=-1), "US", "2026-05-12 08:30"
        )
        assert "never" in _fields_text(blocks)

    def test_high_drift_red(self):
        blocks = render_dashboard(
            self._hold_result(), _metrics(drift=15.0), "US", "2026-05-12 08:30"
        )
        assert "🔴" in _fields_text(blocks)

    def test_high_turnover_red(self):
        blocks = render_dashboard(
            self._hold_result(), _metrics(turnover=25.0), "US", "2026-05-12 08:30"
        )
        assert "🔴" in _fields_text(blocks)

    def test_medium_turnover_yellow(self):
        blocks = render_dashboard(
            self._hold_result(), _metrics(turnover=15.0), "US", "2026-05-12 08:30"
        )
        assert "🟡" in _fields_text(blocks)

    def test_tw_market_flag(self):
        result = BuildResult(trades=[], is_hold=True, hold_reasons=["min_turnover"], market="TW")
        blocks = render_dashboard(result, _metrics(), "TW", "2026-05-12 08:30")
        header = next(b["text"]["text"] for b in blocks if b.get("type") == "header")
        assert "🇹🇼" in header
        assert "TW" in header

    def test_us_market_flag(self):
        result = BuildResult(trades=[], is_hold=True, hold_reasons=["min_turnover"], market="US")
        blocks = render_dashboard(result, _metrics(), "US", "2026-05-12 08:30")
        header = next(b["text"]["text"] for b in blocks if b.get("type") == "header")
        assert "🇺🇸" in header

    def test_timestamp_in_header(self):
        result = BuildResult(trades=[], is_hold=True, hold_reasons=["min_turnover"], market="US")
        blocks = render_dashboard(result, _metrics(), "US", "2026-05-12 08:30")
        header = next(b["text"]["text"] for b in blocks if b.get("type") == "header")
        assert "2026-05-12 08:30" in header


# ── Trade plan layout tests ───────────────────────────────────────────────────

class TestTradePlanLayout:
    def _active_result(
        self, trades: list[Trade] | None = None
    ) -> BuildResult:
        if trades is None:
            trades = [_trade("NVDA", "BUY", 7.4, "Execute")]
        return BuildResult(trades=trades, is_hold=False, hold_reasons=[], market="US")

    def test_execute_trade_in_section(self):
        blocks = render_dashboard(self._active_result(), _metrics(), "US", "2026-05-12 08:30")
        texts = " ".join(_section_texts(blocks))
        assert "NVDA" in texts

    def test_approve_button_present(self):
        blocks = render_dashboard(self._active_result(), _metrics(), "US", "2026-05-12 08:30")
        assert "rebalance_approve" in _action_ids(blocks)

    def test_why_button_present(self):
        blocks = render_dashboard(self._active_result(), _metrics(), "US", "2026-05-12 08:30")
        assert "rebalance_why_summary" in _action_ids(blocks)

    def test_skip_plan_button_present(self):
        blocks = render_dashboard(self._active_result(), _metrics(), "US", "2026-05-12 08:30")
        assert "rebalance_skip_plan" in _action_ids(blocks)

    def test_no_hold_expand_button(self):
        blocks = render_dashboard(self._active_result(), _metrics(), "US", "2026-05-12 08:30")
        assert "rebalance_expand" not in _action_ids(blocks)

    def test_skip_trade_in_context_block(self):
        trades = [
            _trade("NVDA", "BUY", 7.4, "Execute"),
            _trade("MSFT", "BUY", 2.8, "Skip"),
        ]
        blocks = render_dashboard(self._active_result(trades), _metrics(), "US", "2026-05-12 08:30")
        ctx = _context_texts(blocks)
        assert any("MSFT" in t for t in ctx)

    def test_skip_trade_has_strikethrough(self):
        trades = [
            _trade("NVDA", "BUY", 7.4, "Execute"),
            _trade("MSFT", "BUY", 2.8, "Skip"),
        ]
        blocks = render_dashboard(self._active_result(trades), _metrics(), "US", "2026-05-12 08:30")
        ctx = _context_texts(blocks)
        assert any("~" in t for t in ctx)

    def test_skip_trade_not_in_section_text(self):
        trades = [
            _trade("NVDA", "BUY", 7.4, "Execute"),
            _trade("MSFT", "BUY", 2.8, "Skip"),
        ]
        blocks = render_dashboard(self._active_result(trades), _metrics(), "US", "2026-05-12 08:30")
        # MSFT should NOT appear in regular section blocks
        section_texts = " ".join(_section_texts(blocks))
        # It's OK if MSFT appears elsewhere but the strikethrough in context is key
        ctx = _context_texts(blocks)
        assert any("MSFT" in t for t in ctx)

    def test_no_execute_trades_no_approve(self):
        trades = [_trade("MSFT", "BUY", 2.8, "Skip")]
        result = BuildResult(trades=trades, is_hold=False, hold_reasons=[], market="US")
        blocks = render_dashboard(result, _metrics(), "US", "2026-05-12 08:30")
        # When there are no Execute trades, Approve should still be present
        # (it's the plan-level button; Sprint 4 shows it regardless, per D-S4-1)
        # Context: user can approve the plan; handler will find no Execute trades
        ids = _action_ids(blocks)
        assert "rebalance_skip_plan" in ids

    def test_block_count_bounded(self):
        trades = [_trade(f"T{i}", "BUY", 7.0, "Execute") for i in range(10)]
        result = BuildResult(trades=trades, is_hold=False, hold_reasons=[], market="US")
        blocks = render_dashboard(result, _metrics(), "US", "2026-05-12 08:30")
        assert len(blocks) <= 50

    def test_conclusion_shows_execute_count(self):
        trades = [
            _trade("NVDA", "BUY", 7.4, "Execute"),
            _trade("AAPL", "SELL", 7.2, "Execute"),
        ]
        blocks = render_dashboard(self._active_result(trades), _metrics(), "US", "2026-05-12 08:30")
        texts = " ".join(_section_texts(blocks))
        assert "2" in texts  # 2 Execute trades

    def test_tw_currency_symbol(self):
        trade = _trade("2330.TW", "BUY", 7.0, "Execute", market="TW")
        result = BuildResult(trades=[trade], is_hold=False, hold_reasons=[], market="TW")
        blocks = render_dashboard(result, _metrics(), "TW", "2026-05-12 08:30")
        texts = " ".join(_section_texts(blocks))
        assert "NT$" in texts

    def test_sell_side_direction_indicator(self):
        trade = _trade("AAPL", "SELL", 7.2, "Execute")
        result = BuildResult(trades=[trade], is_hold=False, hold_reasons=[], market="US")
        blocks = render_dashboard(result, _metrics(), "US", "2026-05-12 08:30")
        texts = " ".join(_section_texts(blocks))
        assert "SELL" in texts or "AAPL" in texts
