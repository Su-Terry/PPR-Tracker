"""
Alpha Strategist — Trade Builder (V2.0)

Converts optimizer output (w_target) into a human-reviewable list of Trade
objects per spec §5. Also maintains the persistent decision archive used by
discipline metrics (Sprint 3) and Slack /why command (Sprint 4).

Key design decisions (Sprint 3):
  - quantity is the primary source of truth; notional = quantity × est_price
    stored for cost-estimation convenience (spec deviation D-S3-2).
  - score_delta for ConvictionContext: BUY → scores[i], SELL → -scores[i],
    where scores are cross-sectional z-scores (NOT time-series). The
    trade-list-wide percentile rank in conviction.py handles relativization.
  - Binding detection is post-hoc from w_target: sector and position cap
    bindings are exact; cash_floor and max_turnover binding cannot be detected
    without solver dual values (spec deviation D-S3-7).
  - archive_decision() is separated from build_trades() to keep I/O out of
    the computation path — Sprint 5 runner calls archive at the right moment
    (spec deviation D-S3-8).
  - recent_direction for consistency component: reads last 10 archived
    decisions per ticker from rebalance_decisions.jsonl; range [-10, 10].

HOLD conditions checked after optimizer result (spec §4.7):
  1. optimize_result.is_hold  → pass through optimizer hold_reason
  2. All trade convictions < 4.0  → "low_conviction"
  3. All trade notionals < config.min_trade_amount  → "low_notional"
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

import numpy as np

from src.cost.profile import CostProfile
from src.rebalancer.config import RebalanceConfig
from src.rebalancer.conviction import (
    ConstraintBinding,
    ConvictionContext,
    batch_conviction_components,
    compute_convictions,
    execution_tier,
)
from src.rebalancer.cov_estimator import CovEstimate
from src.rebalancer.optimizer import OptimizeResult
from src.rebalancer.rationale import RationaleContext, generate_rationale

logger = logging.getLogger(__name__)

_TZ_TAIPEI = timezone(timedelta(hours=8))
_BINDING_EPS = 0.005       # 0.5% weight tolerance for binding detection
_CONSISTENCY_WINDOW = 10   # last N decisions used for recent_direction


@dataclass
class Trade:
    """
    A single proposed trade from the optimizer, enriched with cost estimates,
    conviction score, and human-readable rationale.

    Attributes
    ----------
    market:
        "US" or "TW".
    ticker:
        Ticker symbol.
    side:
        "BUY" (delta_weight > 0) or "SELL" (delta_weight < 0).
    delta_weight:
        w_target[i] − w0[i]. Positive = buy, negative = sell.
    target_weight:
        Absolute target weight w_target[i] after optimization.
    quantity:
        Number of shares (primary source of truth). For US: fractional shares
        allowed (float). For TW: rounded to nearest integer lot.
        Positive for BUY, negative for SELL.
    est_price:
        Estimated execution price in market currency (USD or TWD).
    notional:
        abs(quantity) × est_price, in market currency. Derived from quantity.
    est_cost:
        Estimated transaction cost from CostProfile.estimate().
    est_cost_breakdown:
        Cost components: {"commission": float, "tax": float, "fx_spread": float}.
    conviction:
        Conviction score 0.0–10.0 per spec §4.4.
    execution_tier:
        "Execute" (≥6.0) / "Watch" (4.0–5.9) / "Skip" (<4.0).
    rationale:
        ≤ 15 char human-readable explanation of the trade driver.
    bindings:
        Names of binding constraints detected post-hoc from w_target
        (e.g. ["sector:半導體", "position:NVDA"]).
    actual_cost:
        Actual cost reported by user after execution. None = outstanding.
    actual_cost_breakdown:
        Breakdown of actual_cost components once reported.
    """

    market: Literal["US", "TW"]
    ticker: str
    side: Literal["BUY", "SELL"]
    delta_weight: float
    target_weight: float
    quantity: float
    est_price: float
    notional: float
    est_cost: float
    est_cost_breakdown: dict
    conviction: float
    execution_tier: Literal["Execute", "Watch", "Skip"]
    rationale: str
    bindings: list[str]
    actual_cost: float | None = None
    actual_cost_breakdown: dict | None = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "delta_weight": self.delta_weight,
            "target_weight": self.target_weight,
            "quantity": self.quantity,
            "est_price": self.est_price,
            "notional": self.notional,
            "est_cost": self.est_cost,
            "est_cost_breakdown": self.est_cost_breakdown,
            "actual_cost": self.actual_cost,
            "actual_cost_breakdown": self.actual_cost_breakdown,
            "conviction": self.conviction,
            "execution_tier": self.execution_tier,
            "rationale": self.rationale,
            "bindings": self.bindings,
        }


@dataclass
class BuildResult:
    """
    Output of build_trades().

    Attributes
    ----------
    trades:
        Non-empty list of Trade objects when is_hold=False; empty list when
        is_hold=True.
    is_hold:
        True when the pipeline concludes the correct action is to hold all
        current positions.
    hold_reasons:
        One or more of: "infeasible", "min_turnover", "low_conviction",
        "low_notional". Empty when is_hold=False.
    market:
        "US" or "TW".
    conviction_components:
        Per-ticker conviction sub-component dicts for archival (Sprint 4).
        Keys: score_delta_pct, constraint_binding, cov_certainty,
        consistency, timing — each in [0.0, 1.0]. Empty when is_hold=True
        or when build_trades was called without covariance data.
    """

    trades: list[Trade]
    is_hold: bool
    hold_reasons: list[str]
    market: Literal["US", "TW"]
    conviction_components: dict[str, dict] = field(default_factory=dict)


def build_trades(
    optimize_result: OptimizeResult,
    tickers: list[str],
    w0: np.ndarray,
    scores: np.ndarray,
    prices: dict[str, float],
    portfolio_value: float,
    cost_profile: CostProfile,
    config: RebalanceConfig,
    cash_idx: int,
    market: Literal["US", "TW"],
    sector_matrix: np.ndarray,
    sector_names: list[str],
    cov_estimate: CovEstimate,
    rationale_contexts: list[RationaleContext] | None = None,
    days_since_last_trade: dict[str, int] | None = None,
    recent_directions: dict[str, int] | None = None,
    decisions_path: Path | None = None,
) -> BuildResult:
    """
    Convert optimizer output into a list of Trade objects.

    Parameters
    ----------
    optimize_result:
        Output of solve_target_weights().
    tickers:
        Ordered ticker list parallel to w0 / w_target arrays.
    w0:
        Current weight vector (N,).
    scores:
        Cross-sectional z-scores (NOT time-series), parallel to tickers.
        Used to compute score_delta for ConvictionContext:
          BUY  → score_delta = scores[i]   (positive = high-quality buy)
          SELL → score_delta = -scores[i]  (positive = selling low-quality)
        The trade-list-wide percentile rank in conviction.py handles
        relativization across all trades.
    prices:
        Dict of ticker → estimated execution price in market currency.
        Missing tickers fall back with a warning (trade skipped).
    portfolio_value:
        Total portfolio value in market currency. Used to convert weight
        deltas to share quantities.
    cost_profile:
        Cost model for est_cost computation.
    config:
        RebalanceConfig for the target market.
    cash_idx:
        Index of the cash / safe-haven proxy in the ticker list. Cash
        position is excluded from the trade list.
    market:
        "US" or "TW".
    sector_matrix:
        K×N sector indicator matrix (same as passed to solve_target_weights).
        Used for post-hoc binding detection.
    sector_names:
        Length-K list of sector names parallel to sector_matrix rows.
    cov_estimate:
        CovEstimate from estimate_covariance(); provides days_per_ticker for
        the cov_certainty component of conviction scoring.
    rationale_contexts:
        Optional list of RationaleContext, parallel to tickers. If None,
        minimal contexts are constructed (RSI=None, is_discovery=w0==0).
        Sprint 5 runner provides full contexts with RSI/PEG/discovery data.
    days_since_last_trade:
        Dict ticker → calendar days since last executed trade. Missing
        tickers get 0 (worst timing score).
    recent_directions:
        Dict ticker → recent_direction [-10, 10] for consistency component.
        If provided, used directly. If None, decisions_path is consulted to
        compute directions from the last 10 archived trades per ticker.
    decisions_path:
        Path to memory/rebalance_decisions.jsonl. Used when recent_directions
        is None to compute consistency component from archive.

    Returns
    -------
    BuildResult
    """
    if optimize_result.is_hold:
        reason = optimize_result.hold_reason if optimize_result.hold_reason else "infeasible"
        return BuildResult(trades=[], is_hold=True, hold_reasons=[reason], market=market)

    w_target = optimize_result.w_target

    # Load recent trade sides for consistency component (one file read)
    recent_sides: dict[str, list[str]] = {}
    if recent_directions is None and decisions_path is not None:
        recent_sides = _load_recent_sides(decisions_path=decisions_path, market=market)

    # Detect binding constraints for every ticker (one matrix multiply)
    all_bindings = detect_bindings(
        w_target=w_target,
        tickers=tickers,
        sector_matrix=sector_matrix,
        sector_names=sector_names,
        config=config,
    )

    # Identify candidate trades (non-cash, non-trivial weight change)
    candidate_indices: list[int] = []
    for i in range(len(tickers)):
        if i == cash_idx:
            continue
        delta = w_target[i] - w0[i]
        notional_approx = abs(delta) * portfolio_value
        if notional_approx < 1.0:
            continue
        candidate_indices.append(i)

    if not candidate_indices:
        return BuildResult(trades=[], is_hold=True, hold_reasons=["min_turnover"], market=market)

    # Determine proposed side and score_delta per candidate
    sides: list[Literal["BUY", "SELL"]] = []
    for i in candidate_indices:
        delta = w_target[i] - w0[i]
        sides.append("BUY" if delta > 0 else "SELL")

    # Build ConvictionContexts
    conviction_contexts: list[ConvictionContext] = []
    for idx, i in enumerate(candidate_indices):
        ticker = tickers[i]
        side = sides[idx]
        score_delta = float(scores[i]) if side == "BUY" else -float(scores[i])

        if recent_directions is not None:
            rd = recent_directions.get(ticker, 0)
        else:
            ticker_sides_hist = recent_sides.get(ticker, [])
            same = sum(1 for s in ticker_sides_hist if s == side)
            opposite = len(ticker_sides_hist) - same
            rd = same - opposite

        conviction_contexts.append(
            ConvictionContext(
                score_delta=score_delta,
                bindings=all_bindings[i],
                days_available=cov_estimate.days_per_ticker.get(ticker, 0),
                days_since_last_trade=(days_since_last_trade or {}).get(ticker, 0),
                recent_direction=rd,
            )
        )

    conviction_scores = compute_convictions(conviction_contexts)
    all_components = batch_conviction_components(conviction_contexts)

    # Build Trade objects
    ticker_components: dict[str, dict] = {}
    trades: list[Trade] = []
    for idx, i in enumerate(candidate_indices):
        ticker = tickers[i]
        side = sides[idx]
        est_price = prices.get(ticker, 0.0)
        if est_price <= 0.0:
            logger.warning("[TRADE] Missing or zero price for %s, skipping", ticker)
            continue

        delta = float(w_target[i] - w0[i])
        raw_qty = delta * portfolio_value / est_price
        quantity = _round_quantity(raw_qty, market)
        if quantity == 0.0:
            continue

        notional = abs(quantity) * est_price
        est_cost = cost_profile.estimate(market=market, side=side, notional=notional)
        est_cost_breakdown = _cost_breakdown(
            market=market, side=side, notional=notional, cost_profile=cost_profile
        )

        conv = conviction_scores[idx]
        tier = execution_tier(conv)
        binding_names = [b.name for b in all_bindings[i]]

        if rationale_contexts is not None and idx < len(rationale_contexts):
            rc = rationale_contexts[idx]
        else:
            rc = RationaleContext(
                ticker=ticker,
                side=side,
                score_delta=float(scores[i]) if side == "BUY" else -float(scores[i]),
                bindings=all_bindings[i],
                w0=float(w0[i]),
                is_discovery=(float(w0[i]) == 0.0),
            )

        rationale = generate_rationale(rc, config)

        trades.append(
            Trade(
                market=market,
                ticker=ticker,
                side=side,
                delta_weight=delta,
                target_weight=float(w_target[i]),
                quantity=quantity,
                est_price=est_price,
                notional=notional,
                est_cost=est_cost,
                est_cost_breakdown=est_cost_breakdown,
                conviction=conv,
                execution_tier=tier,
                rationale=rationale,
                bindings=binding_names,
            )
        )
        ticker_components[ticker] = all_components[idx]

    if not trades:
        return BuildResult(trades=[], is_hold=True, hold_reasons=["min_turnover"], market=market)

    # Second-pass HOLD conditions (spec §4.7)
    hold_reasons: list[str] = []
    if all(t.conviction < 4.0 for t in trades):
        hold_reasons.append("low_conviction")
    if all(t.notional < config.min_trade_amount for t in trades):
        hold_reasons.append("low_notional")

    if hold_reasons:
        return BuildResult(trades=[], is_hold=True, hold_reasons=hold_reasons, market=market)

    return BuildResult(
        trades=trades,
        is_hold=False,
        hold_reasons=[],
        market=market,
        conviction_components=ticker_components,
    )


def detect_bindings(
    w_target: np.ndarray,
    tickers: list[str],
    sector_matrix: np.ndarray,
    sector_names: list[str],
    config: RebalanceConfig,
    eps: float = _BINDING_EPS,
) -> list[list[ConstraintBinding]]:
    """
    Detect which sector and position cap constraints are near their limits.

    Detection is exact for sector cap and max_position constraints (weight /
    cap ratio compared with epsilon tolerance). cash_floor and max_turnover
    binding cannot be detected without solver dual values from cvxpy, which
    are not exposed in OptimizeResult (spec deviation D-S3-7).

    Parameters
    ----------
    w_target:
        Optimal weight vector (N,) from OptimizeResult.
    tickers:
        Ordered ticker list parallel to w_target.
    sector_matrix:
        K×N indicator matrix where B[k, i] = 1 if ticker i is in sector k.
    sector_names:
        Length-K list of sector labels parallel to sector_matrix rows.
    config:
        RebalanceConfig with max_position and max_sector thresholds.
    eps:
        Weight tolerance: a constraint is flagged as binding when
        weight / cap >= 1 - eps.

    Returns
    -------
    list[list[ConstraintBinding]]
        Per-ticker list of ConstraintBinding objects; index parallel to tickers.
    """
    n = len(tickers)
    k = sector_matrix.shape[0]
    per_ticker: list[list[ConstraintBinding]] = [[] for _ in range(n)]

    # Sector cap bindings — propagate to all tickers in the binding sector
    sector_weights = sector_matrix @ w_target  # shape (K,)
    for s in range(k):
        ratio = float(sector_weights[s]) / config.max_sector
        if ratio >= (1.0 - eps):
            binding = ConstraintBinding(name=f"sector:{sector_names[s]}", ratio=min(ratio, 1.0))
            for i in range(n):
                if sector_matrix[s, i] > 0:
                    per_ticker[i].append(binding)

    # Position cap bindings — per ticker
    for i in range(n):
        ratio = float(w_target[i]) / config.max_position
        if ratio >= (1.0 - eps):
            per_ticker[i].append(
                ConstraintBinding(name=f"position:{tickers[i]}", ratio=min(ratio, 1.0))
            )

    return per_ticker


def archive_decision(
    result: BuildResult,
    w_current: np.ndarray,
    tickers: list[str],
    config: RebalanceConfig,
    optimize_result: OptimizeResult,
    path: Path,
) -> None:
    """
    Append one decision record to the JSONL archive at `path`.

    Called by the Sprint 5 runner on Approve (spec §8.5) and on silent log
    runs (spec §8.4). Separated from build_trades() to keep I/O out of the
    computation path (spec deviation D-S3-8).

    Stores full weight vectors (not just changed tickers) so drift computation
    always has the last full target vector, and HOLD decisions are also
    archived for discipline scoring.

    Schema (one JSON object per line):
      timestamp           ISO 8601 +08:00
      market              "US" | "TW"
      is_hold             bool
      hold_reasons        list[str]
      tickers             list[str]
      w_target            list[float]  — full N-vector
      w_current           list[float]  — full N-vector (= w0 at decision time)
      trades              list[trade_dict]
      config              dict — all RebalanceConfig fields
      relaxations_applied list[str]
      infeasible          bool

    Parameters
    ----------
    result:
        BuildResult from build_trades().
    w_current:
        Current weights at decision time (same as w0 passed to build_trades).
    tickers:
        Ordered ticker list parallel to w_current.
    config:
        RebalanceConfig used for this run (stored for historical reproducibility
        — if defaults change, past decisions remain interpretable).
    optimize_result:
        OptimizeResult from solve_target_weights().
    path:
        Append target. Parent directory is created if absent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    def _enrich_trade(t: Trade) -> dict:
        d = t.to_dict()
        comp = result.conviction_components.get(t.ticker)
        if comp:
            d["conviction_components"] = comp
        return d

    record = {
        "timestamp": datetime.now(_TZ_TAIPEI).isoformat(),
        "market": result.market,
        "is_hold": result.is_hold,
        "hold_reasons": result.hold_reasons,
        "tickers": tickers,
        "w_target": optimize_result.w_target.tolist(),
        "w_current": w_current.tolist(),
        "trades": [_enrich_trade(t) for t in result.trades],
        "config": {
            "market": config.market,
            "lambda_score": config.lambda_score,
            "lambda_var": config.lambda_var,
            "lambda_turnover": config.lambda_turnover,
            "max_position": config.max_position,
            "max_sector": config.max_sector,
            "cash_floor": config.cash_floor,
            "max_turnover": config.max_turnover,
            "min_position": config.min_position,
            "min_trade_amount": config.min_trade_amount,
            "min_total_turnover": config.min_total_turnover,
        },
        "relaxations_applied": optimize_result.relaxations_applied,
        "infeasible": optimize_result.infeasible,
    }

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.info(
        "[TRADE] Archived %s decision: %d trade(s), market=%s",
        "HOLD" if result.is_hold else "ACTIVE",
        len(result.trades),
        result.market,
    )


# ── Private helpers ────────────────────────────────────────────────────────────

def _round_quantity(raw_qty: float, market: Literal["US", "TW"]) -> float:
    """
    Round raw share quantity to market convention.
    US: fractional shares allowed; return unrounded.
    TW: round to nearest integer (exchange lot convention).
    Returns 0.0 if the result rounds to zero.
    """
    if market == "TW":
        sign = math.copysign(1.0, raw_qty)
        return sign * round(abs(raw_qty))
    return raw_qty


def _cost_breakdown(
    market: Literal["US", "TW"],
    side: Literal["BUY", "SELL"],
    notional: float,
    cost_profile: CostProfile,
) -> dict:
    """Build est_cost_breakdown dict with commission, tax, fx_spread keys."""
    if market == "US":
        commission = cost_profile.estimate(market="US", side=side, notional=notional)
        return {"commission": round(commission, 6), "tax": 0.0, "fx_spread": 0.0}
    # TW: commission from tw_buy rate (same for both sides); sec_tax on SELL only
    entry = cost_profile.tw_buy
    commission = max(notional * entry.rate, entry.min_cost)
    tax = notional * cost_profile.tw_sec_tax if side == "SELL" else 0.0
    return {"commission": round(commission, 2), "tax": round(tax, 2), "fx_spread": 0.0}


def _load_recent_sides(
    decisions_path: Path,
    market: Literal["US", "TW"],
) -> dict[str, list[str]]:
    """
    Read the archive in reverse order and return the last _CONSISTENCY_WINDOW
    trade sides per ticker for the given market.

    Returns {} on any error or absent file.
    """
    if not decisions_path.exists():
        return {}

    ticker_sides: dict[str, list[str]] = defaultdict(list)
    try:
        lines = decisions_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("market") != market:
            continue
        for trade_dict in rec.get("trades", []):
            t = trade_dict.get("ticker")
            s = trade_dict.get("side")
            if t and s and len(ticker_sides[t]) < _CONSISTENCY_WINDOW:
                ticker_sides[t].append(s)

    return dict(ticker_sides)
