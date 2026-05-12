"""
Alpha Strategist — Conviction Score (V2.0)

Pure rule-based conviction scoring per spec §4.4. No LLM, fully deterministic,
unit-testable, and cross-version comparable (same inputs → same output).

Each trade receives a score in [0.0, 10.0] composed of five weighted components:

  Component        Weight  Formula
  ─────────────────────────────────────────────────────────────────────────────
  score_delta       40%    Percentile rank of this trade's delta in the trade list
  constraint_binding 20%   max(binding_ratio) across all sector + position limits
  cov_certainty      15%   min(days_available / 252, 1.0) — linear data-quality scale
  consistency        15%   0.5 (neutral stub; Sprint 3 will read rebalance_decisions.jsonl)
  timing             10%   min(days_since_last_trade / 30, 1.0)
  ─────────────────────────────────────────────────────────────────────────────

days_available in ConvictionContext refers to the TARGET ticker's covariance
certainty (the new position being considered), not the source ticker. The existing
position's risk is already being borne; what matters for conviction is how reliably
the optimizer has estimated the new holding's risk.

Execution tiers (spec §4.5):
  ≥ 6.0 → Execute  (✅ system recommends executing)
  4.0–5.9 → Watch  (⏸ borderline; system does not pressure)
  < 4.0 → Skip     (❌ signal too weak)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ConstraintBinding:
    """
    Proximity of a single constraint to its cap after solving.

    name:
        Human-readable identifier, e.g. "sector:半導體" or "position:NVDA".
    ratio:
        current_value / cap, capped at 1.0.
        The caller computes this for every sector AND every position limit,
        not just those above some threshold — max() selects the tightest one.
    """

    name: str
    ratio: float  # ∈ [0.0, 1.0]


@dataclass
class ConvictionContext:
    """
    Per-trade inputs for compute_convictions().

    score_delta:
        Improvement from selling the source ticker and buying this target:
        score(target) − score(source). For a buy-only action, score(target).
    bindings:
        ALL sector and position constraint ratios for this trade's universe,
        with no pre-filtering. Pass current_sector_weight / max_sector and
        current_position_weight / max_position for every constraint.
    days_available:
        Number of non-NaN log-return rows available for the TARGET ticker
        (from CovEstimate.days_per_ticker[target_ticker]).
        Uses the target, not the source: the source's risk is already being borne;
        what matters is how reliably the optimizer has estimated the new holding.
    days_since_last_trade:
        Calendar days since the last executed trade on this ticker.
        Use 0 if the ticker was traded very recently; use a large value (e.g. 999)
        if it has never been traded in the history.
    """

    score_delta: float
    bindings: list[ConstraintBinding] = field(default_factory=list)
    days_available: int = 0
    days_since_last_trade: int = 0


def compute_convictions(contexts: list[ConvictionContext]) -> list[float]:
    """
    Compute conviction scores for a list of proposed trades.

    The score_delta component is normalised as a percentile rank across all
    trades in the same batch. All other components are per-trade only.

    Parameters
    ----------
    contexts:
        One ConvictionContext per proposed trade, in any order.

    Returns
    -------
    list[float]
        Conviction scores in [0.0, 10.0], one per context, same order.
    """
    if not contexts:
        return []

    all_deltas = [c.score_delta for c in contexts]
    return [_score_one(ctx, all_deltas) for ctx in contexts]


def execution_tier(conviction: float) -> Literal["Execute", "Watch", "Skip"]:
    """
    Classify a conviction score into an execution tier per spec §4.5.

    ≥ 6.0 → "Execute"   — system recommends executing
    4.0–5.9 → "Watch"   — borderline; system does not pressure
    < 4.0 → "Skip"      — signal too weak
    """
    if conviction >= 6.0:
        return "Execute"
    if conviction >= 4.0:
        return "Watch"
    return "Skip"


# ── Private helpers ───────────────────────────────────────────────────────────

def _score_one(ctx: ConvictionContext, all_deltas: list[float]) -> float:
    c_score = _normalize_score_delta(ctx.score_delta, all_deltas)
    c_binding = _normalize_constraint_binding(ctx.bindings)
    c_cov = _normalize_cov_certainty(ctx.days_available)
    c_consist = _normalize_consistency()
    c_timing = _normalize_timing(ctx.days_since_last_trade)

    raw = (
        0.40 * c_score
        + 0.20 * c_binding
        + 0.15 * c_cov
        + 0.15 * c_consist
        + 0.10 * c_timing
    )
    return round(raw * 10, 1)


def _normalize_score_delta(delta: float, all_deltas: list[float]) -> float:
    """Percentile rank of delta within all_deltas. Returns 0.5 for a single trade."""
    if len(all_deltas) <= 1:
        return 0.5
    rank = sum(d < delta for d in all_deltas)
    return rank / len(all_deltas)


def _normalize_constraint_binding(bindings: list[ConstraintBinding]) -> float:
    """
    Max ratio across all provided constraint bindings.
    Returns 0.0 when no bindings are passed.
    Caller provides ALL sector and position ratios; no threshold filter here.
    """
    return max((b.ratio for b in bindings), default=0.0)


def _normalize_cov_certainty(days_available: int, ideal: int = 252) -> float:
    """Linear scale: 0 days → 0.0, ideal+ days → 1.0."""
    if ideal <= 0:
        return 1.0
    return min(days_available / ideal, 1.0)


def _normalize_consistency() -> float:
    """
    Stub returning neutral 0.5.
    TODO(Sprint 3): read memory/rebalance_decisions.jsonl and check whether
    recent system recommendations for this ticker have been consistent in direction.
    """
    return 0.5


def _normalize_timing(days_since_last_trade: int) -> float:
    """
    0 days (just traded) → 0.0  |  30+ days → 1.0.
    Linear interpolation between 0 and 30.
    """
    return min(days_since_last_trade / 30, 1.0)
