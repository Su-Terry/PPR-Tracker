"""Unit tests for src/rebalancer/rationale.py."""

from __future__ import annotations

import pytest

from src.rebalancer.config import RebalanceConfig
from src.rebalancer.conviction import ConstraintBinding
from src.rebalancer.rationale import (
    RationaleContext,
    _MAX_RATIONALE_CHARS,
    _overheat_rule,
    _sector_cap_rule,
    _min_position_rule,
    _score_delta_rule,
    _new_position_rule,
    generate_rationale,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _config() -> RebalanceConfig:
    return RebalanceConfig.us_default()


def _ctx(
    ticker: str = "AAPL",
    side: str = "BUY",
    score_delta: float = 1.0,
    bindings: list[ConstraintBinding] | None = None,
    w0: float = 0.10,
    rsi: float | None = None,
    is_discovery: bool = False,
    discovery_rank: int | None = None,
) -> RationaleContext:
    return RationaleContext(
        ticker=ticker,
        side=side,
        score_delta=score_delta,
        bindings=bindings or [],
        w0=w0,
        rsi=rsi,
        is_discovery=is_discovery,
        discovery_rank=discovery_rank,
    )


# ── Overheat rule ──────────────────────────────────────────────────────────────

class TestOverheatRule:
    def test_fires_on_sell_with_high_rsi(self):
        result = _overheat_rule(_ctx(side="SELL", rsi=82.0))
        assert result == "RSI 82 過熱"

    def test_fires_at_threshold_boundary(self):
        result = _overheat_rule(_ctx(side="SELL", rsi=75.1))
        assert result.startswith("RSI")

    def test_no_fire_at_exactly_75(self):
        result = _overheat_rule(_ctx(side="SELL", rsi=75.0))
        assert result == ""

    def test_no_fire_below_threshold(self):
        result = _overheat_rule(_ctx(side="SELL", rsi=60.0))
        assert result == ""

    def test_no_fire_on_buy_even_with_high_rsi(self):
        result = _overheat_rule(_ctx(side="BUY", rsi=90.0))
        assert result == ""

    def test_no_fire_when_rsi_is_none(self):
        result = _overheat_rule(_ctx(side="SELL", rsi=None))
        assert result == ""

    def test_output_within_char_limit(self):
        result = _overheat_rule(_ctx(side="SELL", rsi=99.0))
        assert len(result) <= _MAX_RATIONALE_CHARS

    def test_rsi_truncated_to_int(self):
        result = _overheat_rule(_ctx(side="SELL", rsi=82.9))
        assert "82" in result


# ── Sector cap rule ────────────────────────────────────────────────────────────

class TestSectorCapRule:
    def test_fires_with_sector_binding(self):
        bindings = [ConstraintBinding(name="sector:半導體", ratio=0.98)]
        result = _sector_cap_rule(_ctx(bindings=bindings))
        assert result == "sector cap 觸發"

    def test_no_fire_without_bindings(self):
        result = _sector_cap_rule(_ctx(bindings=[]))
        assert result == ""

    def test_no_fire_with_position_binding_only(self):
        bindings = [ConstraintBinding(name="position:AAPL", ratio=0.95)]
        result = _sector_cap_rule(_ctx(bindings=bindings))
        assert result == ""

    def test_fires_with_mixed_bindings(self):
        bindings = [
            ConstraintBinding(name="position:NVDA", ratio=0.95),
            ConstraintBinding(name="sector:AI_基礎設施", ratio=0.99),
        ]
        result = _sector_cap_rule(_ctx(bindings=bindings))
        assert result == "sector cap 觸發"

    def test_output_within_char_limit(self):
        bindings = [ConstraintBinding(name="sector:半導體", ratio=1.0)]
        result = _sector_cap_rule(_ctx(bindings=bindings))
        assert len(result) <= _MAX_RATIONALE_CHARS


# ── Min position rule ──────────────────────────────────────────────────────────

class TestMinPositionRule:
    def test_fires_with_min_pos_binding(self):
        bindings = [ConstraintBinding(name="min_pos:AAPL", ratio=0.5)]
        result = _min_position_rule(_ctx(bindings=bindings), _config())
        assert result == "min pos 補齊"

    def test_fires_when_w0_below_min_position_buy(self):
        # w0 = 0.01, config.min_position = 0.02 → undersized position being topped up
        result = _min_position_rule(
            _ctx(side="BUY", w0=0.01),
            _config(),
        )
        assert result == "min pos 補齊"

    def test_no_fire_when_w0_zero_on_buy(self):
        # w0=0 is a new position, not a min_pos top-up
        result = _min_position_rule(_ctx(side="BUY", w0=0.0), _config())
        assert result == ""

    def test_no_fire_on_sell_for_undersized(self):
        result = _min_position_rule(_ctx(side="SELL", w0=0.01), _config())
        assert result == ""

    def test_no_fire_when_w0_above_min_position(self):
        # w0 = 0.05, min_position = 0.02 → no top-up needed
        result = _min_position_rule(_ctx(side="BUY", w0=0.05), _config())
        assert result == ""

    def test_output_within_char_limit(self):
        bindings = [ConstraintBinding(name="min_pos:X", ratio=0.5)]
        result = _min_position_rule(_ctx(bindings=bindings), _config())
        assert len(result) <= _MAX_RATIONALE_CHARS


# ── Score delta rule ───────────────────────────────────────────────────────────

class TestScoreDeltaRule:
    def test_fires_for_positive_delta(self):
        result = _score_delta_rule(_ctx(score_delta=2.1))
        assert result == "+2.1 score"

    def test_fires_for_negative_delta(self):
        result = _score_delta_rule(_ctx(score_delta=-1.3))
        assert result == "-1.3 score"

    def test_no_fire_for_zero_delta(self):
        result = _score_delta_rule(_ctx(score_delta=0.0))
        assert result == ""

    def test_no_fire_for_near_zero_delta(self):
        result = _score_delta_rule(_ctx(score_delta=1e-8))
        assert result == ""

    def test_output_within_char_limit_positive(self):
        result = _score_delta_rule(_ctx(score_delta=9.9))
        assert len(result) <= _MAX_RATIONALE_CHARS

    def test_output_within_char_limit_negative(self):
        result = _score_delta_rule(_ctx(score_delta=-9.9))
        assert len(result) <= _MAX_RATIONALE_CHARS


# ── New position rule ──────────────────────────────────────────────────────────

class TestNewPositionRule:
    def test_fires_when_w0_is_zero(self):
        result = _new_position_rule(_ctx(w0=0.0))
        assert result == "新增部位"

    def test_fires_with_discovery_flag(self):
        result = _new_position_rule(_ctx(w0=0.0, is_discovery=True, discovery_rank=1))
        assert result == "Discovery #1"

    def test_fires_with_discovery_rank(self):
        result = _new_position_rule(_ctx(w0=0.05, is_discovery=True, discovery_rank=3))
        assert result == "Discovery #3"

    def test_no_fire_when_w0_positive_no_discovery(self):
        result = _new_position_rule(_ctx(w0=0.10, is_discovery=False))
        assert result == ""

    def test_output_within_char_limit(self):
        result = _new_position_rule(_ctx(w0=0.0, is_discovery=True, discovery_rank=9))
        assert len(result) <= _MAX_RATIONALE_CHARS


# ── Priority and fallback ──────────────────────────────────────────────────────

class TestGenerateRationalePriority:
    def test_overheat_beats_sector_cap(self):
        bindings = [ConstraintBinding(name="sector:半導體", ratio=0.99)]
        ctx = _ctx(side="SELL", rsi=82.0, bindings=bindings)
        result = generate_rationale(ctx, _config())
        assert result == "RSI 82 過熱"

    def test_sector_cap_beats_min_position(self):
        bindings = [
            ConstraintBinding(name="sector:AI_基礎設施", ratio=0.99),
            ConstraintBinding(name="min_pos:X", ratio=0.5),
        ]
        ctx = _ctx(bindings=bindings)
        result = generate_rationale(ctx, _config())
        assert result == "sector cap 觸發"

    def test_min_position_beats_score_delta(self):
        bindings = [ConstraintBinding(name="min_pos:AAPL", ratio=0.6)]
        ctx = _ctx(bindings=bindings, score_delta=2.0)
        result = generate_rationale(ctx, _config())
        assert result == "min pos 補齊"

    def test_score_delta_beats_new_position(self):
        # is_discovery=True would fire new_position, but score_delta fires first
        ctx = _ctx(score_delta=1.5, w0=0.0, is_discovery=True, discovery_rank=1)
        result = generate_rationale(ctx, _config())
        assert result == "+1.5 score"

    def test_fallback_when_no_rule_fires(self):
        ctx = _ctx(side="BUY", rsi=None, bindings=[], score_delta=0.0, w0=0.10)
        result = generate_rationale(ctx, _config())
        assert result == "rebalance"

    def test_all_outputs_within_char_limit(self):
        cases = [
            _ctx(side="SELL", rsi=82.0),
            _ctx(bindings=[ConstraintBinding(name="sector:半導體", ratio=1.0)]),
            _ctx(bindings=[ConstraintBinding(name="min_pos:X", ratio=0.5)]),
            _ctx(score_delta=2.1),
            _ctx(score_delta=-1.3),
            _ctx(w0=0.0, is_discovery=True, discovery_rank=1),
            _ctx(w0=0.0),
            _ctx(score_delta=0.0, w0=0.10),
        ]
        for c in cases:
            r = generate_rationale(c, _config())
            assert len(r) <= _MAX_RATIONALE_CHARS, f"'{r}' exceeds {_MAX_RATIONALE_CHARS} chars"

    def test_result_is_never_empty(self):
        ctx = _ctx(score_delta=0.0, w0=0.15, bindings=[], rsi=None)
        result = generate_rationale(ctx, _config())
        assert result != ""
