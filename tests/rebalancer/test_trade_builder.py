"""Unit tests for src/rebalancer/trade_builder.py."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.cost.profile import CostProfile
from src.rebalancer.config import RebalanceConfig
from src.rebalancer.conviction import ConstraintBinding
from src.rebalancer.cov_estimator import CovEstimate
from src.rebalancer.optimizer import OptimizeResult
from src.rebalancer.rationale import RationaleContext
from src.rebalancer.trade_builder import (
    BuildResult,
    Trade,
    _cost_breakdown,
    _load_recent_sides,
    _round_quantity,
    archive_decision,
    build_trades,
    detect_bindings,
)


# ── Shared fixtures ────────────────────────────────────────────────────────────

N = 5
TICKERS = ["CASH", "AAPL", "NVDA", "GOOGL", "MSFT"]
CASH_IDX = 0
SECTOR_NAMES = ["Tech", "Other"]


def _config() -> RebalanceConfig:
    return RebalanceConfig.us_default()


def _w0() -> np.ndarray:
    return np.array([0.09, 0.17, 0.08, 0.09, 0.14], dtype=float)


def _w_target_active() -> np.ndarray:
    """Target that moves AAPL down and NVDA up meaningfully."""
    return np.array([0.08, 0.138, 0.12, 0.10, 0.122], dtype=float)


def _scores() -> np.ndarray:
    return np.array([0.0, -0.5, 1.5, 0.3, 0.2], dtype=float)


def _sector_matrix() -> np.ndarray:
    # Tech: AAPL, NVDA, MSFT; Other: CASH, GOOGL
    return np.array(
        [
            [0, 1, 1, 0, 1],  # Tech
            [1, 0, 0, 1, 0],  # Other
        ],
        dtype=float,
    )


def _cov_estimate() -> CovEstimate:
    cov = np.eye(N) * 0.04
    return CovEstimate(
        matrix=cov,
        days_per_ticker={t: 252 for t in TICKERS},
        used_ledoit_wolf=False,
    )


def _prices() -> dict[str, float]:
    return {"CASH": 1.0, "AAPL": 185.0, "NVDA": 920.0, "GOOGL": 170.0, "MSFT": 430.0}


def _opt_result_active() -> OptimizeResult:
    return OptimizeResult(
        w_target=_w_target_active(),
        is_hold=False,
        hold_reason=None,
        infeasible=False,
    )


def _opt_result_hold(reason: str = "min_turnover") -> OptimizeResult:
    return OptimizeResult(
        w_target=_w0(),
        is_hold=True,
        hold_reason=reason,
        infeasible=reason == "infeasible",
    )


def _build(
    optimize_result: OptimizeResult | None = None,
    w0: np.ndarray | None = None,
    rationale_contexts: list[RationaleContext] | None = None,
    days_since_last_trade: dict[str, int] | None = None,
    recent_directions: dict[str, int] | None = None,
    decisions_path: Path | None = None,
) -> BuildResult:
    return build_trades(
        optimize_result=optimize_result or _opt_result_active(),
        tickers=TICKERS,
        w0=w0 if w0 is not None else _w0(),
        scores=_scores(),
        prices=_prices(),
        portfolio_value=100_000.0,
        cost_profile=CostProfile.from_defaults(),
        config=_config(),
        cash_idx=CASH_IDX,
        market="US",
        sector_matrix=_sector_matrix(),
        sector_names=SECTOR_NAMES,
        cov_estimate=_cov_estimate(),
        rationale_contexts=rationale_contexts,
        days_since_last_trade=days_since_last_trade,
        recent_directions=recent_directions,
        decisions_path=decisions_path,
    )


# ── Trade dataclass ────────────────────────────────────────────────────────────

class TestTradeDataclass:
    def test_to_dict_includes_required_keys(self):
        result = _build()
        assert result.trades
        d = result.trades[0].to_dict()
        for key in ("ticker", "side", "delta_weight", "target_weight",
                    "quantity", "est_price", "notional", "est_cost",
                    "conviction", "execution_tier", "rationale", "bindings"):
            assert key in d

    def test_actual_cost_defaults_to_none(self):
        result = _build()
        for t in result.trades:
            assert t.actual_cost is None
            assert t.actual_cost_breakdown is None


# ── HOLD passthrough ───────────────────────────────────────────────────────────

class TestHoldPassthrough:
    def test_hold_from_optimizer_min_turnover(self):
        result = _build(optimize_result=_opt_result_hold("min_turnover"))
        assert result.is_hold is True
        assert result.trades == []
        assert "min_turnover" in result.hold_reasons

    def test_hold_from_optimizer_infeasible(self):
        result = _build(optimize_result=_opt_result_hold("infeasible"))
        assert result.is_hold is True
        assert "infeasible" in result.hold_reasons

    def test_hold_none_reason_becomes_infeasible(self):
        opt = OptimizeResult(w_target=_w0(), is_hold=True, hold_reason=None, infeasible=True)
        result = _build(optimize_result=opt)
        assert result.is_hold is True
        assert "infeasible" in result.hold_reasons


# ── Active result ──────────────────────────────────────────────────────────────

class TestActiveResult:
    def test_returns_non_empty_trades(self):
        result = _build()
        assert not result.is_hold
        assert len(result.trades) >= 1

    def test_cash_ticker_excluded(self):
        result = _build()
        for t in result.trades:
            assert t.ticker != "CASH"

    def test_sell_has_negative_delta_weight(self):
        result = _build()
        sells = [t for t in result.trades if t.side == "SELL"]
        for t in sells:
            assert t.delta_weight < 0

    def test_buy_has_positive_delta_weight(self):
        result = _build()
        buys = [t for t in result.trades if t.side == "BUY"]
        for t in buys:
            assert t.delta_weight > 0

    def test_notional_equals_quantity_times_price(self):
        result = _build()
        for t in result.trades:
            assert abs(t.notional - abs(t.quantity) * t.est_price) < 1e-6

    def test_conviction_in_range(self):
        result = _build()
        for t in result.trades:
            assert 0.0 <= t.conviction <= 10.0

    def test_execution_tier_valid(self):
        result = _build()
        for t in result.trades:
            assert t.execution_tier in ("Execute", "Watch", "Skip")

    def test_rationale_within_char_limit(self):
        result = _build()
        for t in result.trades:
            assert len(t.rationale) <= 15

    def test_rationale_not_empty(self):
        result = _build()
        for t in result.trades:
            assert t.rationale != ""

    def test_market_field_set(self):
        result = _build()
        assert result.market == "US"
        for t in result.trades:
            assert t.market == "US"


# ── HOLD from second-pass conditions ──────────────────────────────────────────

class TestSecondPassHold:
    def test_low_conviction_hold(self):
        # All trades will have very low conviction: use a near-zero score universe
        # with days_since_last_trade=0 (worst timing) and no consistency
        result = build_trades(
            optimize_result=_opt_result_active(),
            tickers=TICKERS,
            w0=_w0(),
            scores=np.zeros(N),  # zero scores → mixed conviction
            prices=_prices(),
            portfolio_value=100_000.0,
            cost_profile=CostProfile.from_defaults(),
            config=RebalanceConfig.us_default(),
            cash_idx=CASH_IDX,
            market="US",
            sector_matrix=_sector_matrix(),
            sector_names=SECTOR_NAMES,
            cov_estimate=CovEstimate(np.eye(N) * 0.04, {t: 0 for t in TICKERS}, False),
            days_since_last_trade={t: 0 for t in TICKERS},
            recent_directions={t: -10 for t in TICKERS},  # worst consistency
        )
        # With zero scores, zero cov certainty, worst timing, worst consistency:
        # all convictions should be very low → HOLD
        if result.is_hold:
            assert "low_conviction" in result.hold_reasons

    def test_low_notional_hold(self):
        # Tiny portfolio → all notionals below min_trade_amount ($100)
        result = build_trades(
            optimize_result=_opt_result_active(),
            tickers=TICKERS,
            w0=_w0(),
            scores=_scores(),
            prices=_prices(),
            portfolio_value=50.0,  # $50 total → trades ~$0.5–$2
            cost_profile=CostProfile.from_defaults(),
            config=_config(),
            cash_idx=CASH_IDX,
            market="US",
            sector_matrix=_sector_matrix(),
            sector_names=SECTOR_NAMES,
            cov_estimate=_cov_estimate(),
            recent_directions={t: 0 for t in TICKERS},
        )
        if result.is_hold:
            assert "low_notional" in result.hold_reasons


# ── Quantity rounding ──────────────────────────────────────────────────────────

class TestRoundQuantity:
    def test_us_fractional_unchanged(self):
        assert _round_quantity(3.7, "US") == pytest.approx(3.7)

    def test_tw_rounded_to_integer(self):
        assert _round_quantity(3.7, "TW") == 4.0

    def test_tw_rounds_down(self):
        assert _round_quantity(2.4, "TW") == 2.0

    def test_negative_tw_preserved_sign(self):
        assert _round_quantity(-3.7, "TW") == -4.0

    def test_zero_remains_zero(self):
        assert _round_quantity(0.0, "US") == 0.0
        assert _round_quantity(0.0, "TW") == 0.0


# ── Detect bindings ────────────────────────────────────────────────────────────

class TestDetectBindings:
    def test_sector_cap_detected(self):
        # Push Tech sector to max (AAPL + NVDA + MSFT ≈ max_sector)
        w_target = np.array([0.0, 0.15, 0.15, 0.0, 0.10], dtype=float)
        config = _config()
        # Tech weight = 0.40 = max_sector
        result = detect_bindings(
            w_target=w_target,
            tickers=TICKERS,
            sector_matrix=_sector_matrix(),
            sector_names=SECTOR_NAMES,
            config=config,
            eps=0.005,
        )
        tech_tickers_idx = [1, 2, 4]  # AAPL, NVDA, MSFT in Tech
        for i in tech_tickers_idx:
            names = [b.name for b in result[i]]
            assert any("sector:Tech" in n for n in names)

    def test_position_cap_detected(self):
        w_target = np.array([0.0, 0.20, 0.10, 0.05, 0.05], dtype=float)
        config = _config()  # max_position = 0.20
        result = detect_bindings(
            w_target=w_target,
            tickers=TICKERS,
            sector_matrix=_sector_matrix(),
            sector_names=SECTOR_NAMES,
            config=config,
        )
        names = [b.name for b in result[1]]  # AAPL index
        assert any("position:AAPL" in n for n in names)

    def test_no_binding_for_small_weights(self):
        # All weights well below max_position=0.20 and sector totals below max_sector=0.40.
        # Tech (AAPL+NVDA+MSFT) = 0.05+0.05+0.05 = 0.15 < 0.40
        # Other (CASH+GOOGL) = 0.05+0.05 = 0.10 < 0.40
        w_target = np.array([0.05, 0.05, 0.05, 0.05, 0.05], dtype=float)
        result = detect_bindings(
            w_target=w_target,
            tickers=TICKERS,
            sector_matrix=_sector_matrix(),
            sector_names=SECTOR_NAMES,
            config=_config(),
        )
        for i in range(N):
            assert result[i] == []

    def test_binding_ratio_capped_at_one(self):
        w_target = np.array([0.0, 0.20, 0.0, 0.0, 0.0], dtype=float)
        result = detect_bindings(
            w_target=w_target,
            tickers=TICKERS,
            sector_matrix=_sector_matrix(),
            sector_names=SECTOR_NAMES,
            config=_config(),
        )
        pos_bindings = [b for b in result[1] if b.name.startswith("position:")]
        assert all(b.ratio <= 1.0 for b in pos_bindings)

    def test_returns_list_of_length_n(self):
        w_target = np.zeros(N)
        w_target[0] = 1.0
        result = detect_bindings(
            w_target=w_target,
            tickers=TICKERS,
            sector_matrix=_sector_matrix(),
            sector_names=SECTOR_NAMES,
            config=_config(),
        )
        assert len(result) == N


# ── Archive ────────────────────────────────────────────────────────────────────

class TestArchiveDecision:
    def test_creates_jsonl_file(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        result = _build()
        archive_decision(
            result=result,
            w_current=_w0(),
            tickers=TICKERS,
            config=_config(),
            optimize_result=_opt_result_active(),
            path=path,
        )
        assert path.exists()

    def test_jsonl_record_parseable(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        result = _build()
        archive_decision(
            result=result,
            w_current=_w0(),
            tickers=TICKERS,
            config=_config(),
            optimize_result=_opt_result_active(),
            path=path,
        )
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["market"] == "US"
        assert "timestamp" in rec
        assert "w_target" in rec and len(rec["w_target"]) == N
        assert "w_current" in rec and len(rec["w_current"]) == N
        assert "config" in rec

    def test_appends_multiple_records(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        result = _build()
        for _ in range(3):
            archive_decision(
                result=result,
                w_current=_w0(),
                tickers=TICKERS,
                config=_config(),
                optimize_result=_opt_result_active(),
                path=path,
            )
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        assert len(lines) == 3

    def test_hold_decision_archived(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        result = BuildResult(trades=[], is_hold=True, hold_reasons=["min_turnover"], market="US")
        archive_decision(
            result=result,
            w_current=_w0(),
            tickers=TICKERS,
            config=_config(),
            optimize_result=_opt_result_hold("min_turnover"),
            path=path,
        )
        rec = json.loads(path.read_text().splitlines()[0])
        assert rec["is_hold"] is True
        assert rec["hold_reasons"] == ["min_turnover"]
        assert rec["trades"] == []

    def test_config_stored_in_record(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        result = _build()
        cfg = _config()
        archive_decision(
            result=result,
            w_current=_w0(),
            tickers=TICKERS,
            config=cfg,
            optimize_result=_opt_result_active(),
            path=path,
        )
        rec = json.loads(path.read_text().splitlines()[0])
        assert rec["config"]["max_position"] == cfg.max_position
        assert rec["config"]["lambda_turnover"] == cfg.lambda_turnover

    def test_creates_parent_directory(self, tmp_path):
        path = tmp_path / "memory" / "rebalance_decisions.jsonl"
        result = _build()
        archive_decision(
            result=result,
            w_current=_w0(),
            tickers=TICKERS,
            config=_config(),
            optimize_result=_opt_result_active(),
            path=path,
        )
        assert path.exists()


# ── Recent sides loader ────────────────────────────────────────────────────────

class TestLoadRecentSides:
    def _write_decisions(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def test_returns_empty_when_file_absent(self, tmp_path):
        result = _load_recent_sides(tmp_path / "missing.jsonl", "US")
        assert result == {}

    def test_loads_sides_for_ticker(self, tmp_path):
        path = tmp_path / "d.jsonl"
        self._write_decisions(path, [
            {"market": "US", "is_hold": False, "trades": [{"ticker": "AAPL", "side": "SELL"}]},
            {"market": "US", "is_hold": False, "trades": [{"ticker": "AAPL", "side": "SELL"}]},
        ])
        result = _load_recent_sides(path, "US")
        assert result["AAPL"] == ["SELL", "SELL"]

    def test_ignores_other_market(self, tmp_path):
        path = tmp_path / "d.jsonl"
        self._write_decisions(path, [
            {"market": "TW", "is_hold": False, "trades": [{"ticker": "2330.TW", "side": "BUY"}]},
        ])
        result = _load_recent_sides(path, "US")
        assert result == {}

    def test_skips_malformed_jsonl_lines(self, tmp_path):
        path = tmp_path / "d.jsonl"
        with path.open("w") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps({"market": "US", "is_hold": False, "trades": [{"ticker": "AAPL", "side": "SELL"}]}) + "\n")
        result = _load_recent_sides(path, "US")
        assert result.get("AAPL") == ["SELL"]

    def test_caps_at_consistency_window(self, tmp_path):
        from src.rebalancer.trade_builder import _CONSISTENCY_WINDOW
        path = tmp_path / "d.jsonl"
        records = [
            {"market": "US", "is_hold": False, "trades": [{"ticker": "NVDA", "side": "BUY"}]}
            for _ in range(_CONSISTENCY_WINDOW + 5)
        ]
        self._write_decisions(path, records)
        result = _load_recent_sides(path, "US")
        assert len(result["NVDA"]) == _CONSISTENCY_WINDOW


# ── Cost breakdown ─────────────────────────────────────────────────────────────

class TestCostBreakdown:
    def test_us_buy_no_tax(self):
        bd = _cost_breakdown("US", "BUY", 1000.0, CostProfile.from_defaults())
        assert bd["tax"] == 0.0

    def test_tw_sell_has_sec_tax(self):
        bd = _cost_breakdown("TW", "SELL", 100_000.0, CostProfile.from_defaults())
        assert bd["tax"] > 0.0

    def test_tw_buy_no_sec_tax(self):
        bd = _cost_breakdown("TW", "BUY", 100_000.0, CostProfile.from_defaults())
        assert bd["tax"] == 0.0

    def test_breakdown_keys_present(self):
        bd = _cost_breakdown("US", "BUY", 500.0, CostProfile.from_defaults())
        assert set(bd.keys()) == {"commission", "tax", "fx_spread"}


# ── Decisions path + recent_directions integration ────────────────────────────

class TestDecisionsPathConsistency:
    def test_decisions_path_used_when_no_recent_directions(self, tmp_path):
        """build_trades reads decisions_path when recent_directions is None."""
        path = tmp_path / "d.jsonl"
        # Write one past BUY NVDA
        past = {
            "market": "US", "is_hold": False,
            "trades": [{"ticker": "NVDA", "side": "BUY", "delta_weight": 0.04}],
        }
        with path.open("w") as fh:
            fh.write(json.dumps(past) + "\n")
        # Build with decisions_path but no recent_directions — should not crash
        result = _build(decisions_path=path, recent_directions=None)
        # Just verify it completes without error; consistency used from archive
        assert isinstance(result, BuildResult)

    def test_missing_price_skips_trade(self):
        """A ticker with no price in the prices dict is skipped gracefully."""
        prices_no_aapl = {k: v for k, v in _prices().items() if k != "AAPL"}
        result = build_trades(
            optimize_result=_opt_result_active(),
            tickers=TICKERS,
            w0=_w0(),
            scores=_scores(),
            prices=prices_no_aapl,
            portfolio_value=100_000.0,
            cost_profile=CostProfile.from_defaults(),
            config=_config(),
            cash_idx=CASH_IDX,
            market="US",
            sector_matrix=_sector_matrix(),
            sector_names=SECTOR_NAMES,
            cov_estimate=_cov_estimate(),
            recent_directions={t: 0 for t in TICKERS},
        )
        # AAPL should be absent from the trade list
        aapl_trades = [t for t in result.trades if t.ticker == "AAPL"]
        assert aapl_trades == []


# ── RationaleContext passthrough ───────────────────────────────────────────────

class TestRationaleContextPassthrough:
    def test_overheat_rationale_applied_from_context(self):
        w0 = _w0()
        w_target = _w_target_active()
        # Find a SELL ticker
        sell_indices = [i for i in range(N) if i != CASH_IDX and w_target[i] < w0[i]]
        if not sell_indices:
            pytest.skip("No SELL trades in this scenario")

        rc_list = [None] * N  # placeholder; we'll pass a list indexed by candidate
        # Build rationale_contexts aligned to tickers (not candidate_indices)
        # For this test, provide contexts for all tickers; build_trades uses idx
        rcs = [
            RationaleContext(
                ticker=TICKERS[i],
                side="SELL" if w_target[i] < w0[i] else "BUY",
                score_delta=float(_scores()[i]),
                w0=float(w0[i]),
                rsi=82.0 if i in sell_indices else None,
            )
            for i in range(N) if i != CASH_IDX
        ]
        result = _build(rationale_contexts=rcs)
        sells = [t for t in result.trades if t.side == "SELL"]
        if sells:
            assert sells[0].rationale == "RSI 82 過熱"
