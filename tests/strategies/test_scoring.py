"""
Tests for src/strategies/scoring.py — efficiency_score().

Verifies that the extracted function behaves identically to the original
calculate_efficiency_score() in llm_compiler.py (which now imports from here).
"""

from __future__ import annotations

import pytest

from src.data_fetcher import ScanResult
from src.risk_engine import MarketRegime
from src.strategies.scoring import efficiency_score


def _make_scan_result(
    ticker: str = "NVDA",
    current_price: float | None = 100.0,
    ma50: float | None = 100.0,
    valuation_model: str = "PEG",
    modified_peg: float | None = 0.5,
    ps_growth_ratio: float | None = None,
    beta: float | None = None,
) -> ScanResult:
    return ScanResult(
        ticker=ticker,
        current_price=current_price,
        ma50=ma50,
        valuation_model=valuation_model,
        modified_peg=modified_peg,
        ps_growth_ratio=ps_growth_ratio,
        beta=beta,
    )


class TestEfficiencyScore:
    def test_peg_model_at_ma(self):
        # PEG=0.5 at MA (dist=0) → (0.7/0.5) × (0.3/1) = 1.4 × 0.3 = 0.42
        r = _make_scan_result(modified_peg=0.5)
        score = efficiency_score(r)
        assert score == pytest.approx(0.42, rel=1e-3)

    def test_peg_model_above_ma(self):
        # PEG=1.0, price=110, ma50=100 → dist=0.1 → (0.7/1.0)×(0.3/1.1)=0.190909...
        r = _make_scan_result(current_price=110.0, ma50=100.0, modified_peg=1.0)
        expected = (0.7 / 1.0) * (0.3 / 1.1)
        assert efficiency_score(r) == pytest.approx(expected, rel=1e-5)

    def test_ps_model_path(self):
        r = _make_scan_result(valuation_model="PS", modified_peg=None, ps_growth_ratio=0.8)
        score = efficiency_score(r)
        expected = (0.7 / 0.8) * (0.3 / 1.0)
        assert score == pytest.approx(expected, rel=1e-5)

    def test_missing_price_returns_zero(self):
        r = _make_scan_result(current_price=None)
        assert efficiency_score(r) == 0.0

    def test_missing_ma50_returns_zero(self):
        r = _make_scan_result(ma50=None)
        assert efficiency_score(r) == 0.0

    def test_zero_ma50_returns_zero(self):
        r = _make_scan_result(ma50=0.0)
        assert efficiency_score(r) == 0.0

    def test_inf_ratio_returns_zero(self):
        r = _make_scan_result(modified_peg=float("inf"))
        assert efficiency_score(r) == 0.0

    def test_zero_ratio_returns_zero(self):
        r = _make_scan_result(modified_peg=0.0)
        assert efficiency_score(r) == 0.0

    def test_negative_ratio_returns_zero(self):
        r = _make_scan_result(modified_peg=-0.5)
        assert efficiency_score(r) == 0.0

    def test_unknown_valuation_model_returns_zero(self):
        r = _make_scan_result(valuation_model="TECHNICAL", modified_peg=None, ps_growth_ratio=None)
        assert efficiency_score(r) == 0.0

    def test_beta_penalty_bear_regime(self):
        # beta=2.0, BEAR → penalty=max(0,(2.0-1.0)×0.2)=0.2 → multiplier=0.8
        r = _make_scan_result(modified_peg=0.5, beta=2.0)
        no_penalty = efficiency_score(r, regime=None)
        with_penalty = efficiency_score(r, regime=MarketRegime.BEAR)
        assert with_penalty == pytest.approx(no_penalty * 0.8, rel=1e-5)

    def test_beta_penalty_crash_regime(self):
        r = _make_scan_result(modified_peg=0.5, beta=1.5)
        no_penalty = efficiency_score(r, regime=None)
        with_penalty = efficiency_score(r, regime=MarketRegime.CRASH)
        expected_multiplier = max(0.0, 1.0 - max(0.0, (1.5 - 1.0) * 0.2))
        assert with_penalty == pytest.approx(no_penalty * expected_multiplier, rel=1e-5)

    def test_no_penalty_bull_regime(self):
        r = _make_scan_result(modified_peg=0.5, beta=2.0)
        no_regime = efficiency_score(r, regime=None)
        bull = efficiency_score(r, regime=MarketRegime.BULL)
        assert bull == pytest.approx(no_regime, rel=1e-5)

    def test_no_beta_no_penalty(self):
        r = _make_scan_result(modified_peg=0.5, beta=None)
        no_regime = efficiency_score(r, regime=None)
        bear = efficiency_score(r, regime=MarketRegime.BEAR)
        assert bear == pytest.approx(no_regime, rel=1e-5)

    def test_beta_penalty_capped_at_zero(self):
        r = _make_scan_result(modified_peg=0.5, beta=100.0)
        score = efficiency_score(r, regime=MarketRegime.CRASH)
        assert score == pytest.approx(0.0)

    def test_calculate_efficiency_score_alias_in_llm_compiler(self):
        """After the Sprint 5 refactor, llm_compiler.calculate_efficiency_score
        should still be importable and functionally identical."""
        from src.llm_compiler import calculate_efficiency_score  # type: ignore[attr-defined]
        r = _make_scan_result(modified_peg=0.5)
        assert calculate_efficiency_score(r) == pytest.approx(efficiency_score(r), rel=1e-9)
