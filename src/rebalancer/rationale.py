"""
Alpha Strategist — Rationale Generator (V2.0)

Pure-rule rationale strings for Trade objects per spec §5. No LLM, fully
deterministic, ≤ 15 characters per output. The five rules are evaluated in
priority order; the first matching rule wins:

  Priority  Rule          Fires when
  ────────────────────────────────────────────────────────────────────────────
  1         overheat      SELL side AND rsi is not None AND rsi > 75
  2         sector_cap    any binding whose name starts with "sector:"
  3         min_position  any binding whose name starts with "min_pos"
                          OR w0 < config.min_position (position being topped up)
  4         score_delta   score_delta > 0 (BUY improving score) or
                          score_delta < 0 (SELL reducing drag)
  5         new_position  w0 == 0.0 (ticker not in current holdings)
  fallback  "rebalance"   none of the above matched

Character budgets for each rule output (≤ 15 chars guaranteed):
  overheat     → "RSI 82 過熱"        (9 chars)
  sector_cap   → "sector cap 觸發"    (12 chars)
  min_position → "min pos 補齊"       (9 chars)
  score_delta  → "+2.1 score"         (10 chars) or "-1.3 score" (10 chars)
  new_position → "Discovery #N"       (max 12 chars for rank ≤ 9)
                 "新增部位"            (4 chars, no rank)
  fallback     → "rebalance"          (9 chars)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from src.rebalancer.config import RebalanceConfig
    from src.rebalancer.conviction import ConstraintBinding


@dataclass
class RationaleContext:
    """
    Per-trade signal inputs for generate_rationale().

    Separate from ConvictionContext to avoid polluting the conviction module
    with quant_engine signals (RSI, PEG, discovery rank) that are not needed
    for scoring. Sprint 5 runner populates these from quant_engine output;
    smoke tests pass synthetic values.

    Attributes
    ----------
    ticker:
        Ticker symbol for this trade.
    side:
        "BUY" or "SELL".
    score_delta:
        Same value as ConvictionContext.score_delta:
          BUY  → scores[i]   (cross-sectional z-score of the target ticker)
          SELL → -scores[i]
        Positive = beneficial trade direction (buying high-score or selling
        low-score); negative = optimizer forcing a low-quality move for
        diversification.
    bindings:
        ConstraintBinding list from detect_bindings() — all sector and
        position ratios for this ticker's universe, no pre-filtering.
    w0:
        Current portfolio weight of this ticker before the proposed trade.
        Used to detect new positions (w0 == 0.0).
    rsi:
        RSI-14 for the ticker. If provided and SELL side with rsi > 75,
        the overheat rule fires. None disables the overheat rule.
    peg_ratio:
        Optional PEG ratio. Reserved for future rules; not used in Sprint 3.
    is_discovery:
        True when the ticker was surfaced by discovery.scan_market_for_alpha
        (i.e., it is a new candidate, not a current holding that is being sized
        up). Sprint 5 runner sets this; defaults to False.
    discovery_rank:
        1-based rank from the discovery scan (1 = highest-ranked new idea).
        Only meaningful when is_discovery=True.
    """

    ticker: str
    side: Literal["BUY", "SELL"]
    score_delta: float
    bindings: list[ConstraintBinding] = field(default_factory=list)
    w0: float = 0.0
    rsi: float | None = None
    peg_ratio: float | None = None
    is_discovery: bool = False
    discovery_rank: int | None = None


_RSI_OVERHEAT_THRESHOLD: float = 75.0
_MAX_RATIONALE_CHARS: int = 15


def generate_rationale(ctx: RationaleContext, config: "RebalanceConfig") -> str:
    """
    Return the highest-priority matching rule string for a trade.

    Always returns a non-empty string of at most 15 characters.

    Parameters
    ----------
    ctx:
        Signal context for the trade.
    config:
        Market config, used to determine min_position threshold for the
        min_position rule.

    Returns
    -------
    str
        Human-readable rationale, ≤ 15 chars.
    """
    rationale = (
        _overheat_rule(ctx)
        or _sector_cap_rule(ctx)
        or _min_position_rule(ctx, config)
        or _score_delta_rule(ctx)
        or _new_position_rule(ctx)
        or "rebalance"
    )
    assert len(rationale) <= _MAX_RATIONALE_CHARS, (
        f"Rationale '{rationale}' exceeds {_MAX_RATIONALE_CHARS} chars"
    )
    return rationale


# ── Private rule functions ─────────────────────────────────────────────────────

def _overheat_rule(ctx: RationaleContext) -> str:
    """
    Fire on SELL when RSI > 75. Format: "RSI 82 過熱" (capped at 2 RSI digits
    to guarantee ≤ 15 chars).
    """
    if ctx.side == "SELL" and ctx.rsi is not None and ctx.rsi > _RSI_OVERHEAT_THRESHOLD:
        rsi_int = int(ctx.rsi)
        return f"RSI {rsi_int} 過熱"
    return ""


def _sector_cap_rule(ctx: RationaleContext) -> str:
    """Fire when any binding name starts with 'sector:'."""
    for b in ctx.bindings:
        if b.name.startswith("sector:"):
            return "sector cap 觸發"
    return ""


def _min_position_rule(ctx: RationaleContext, config: "RebalanceConfig") -> str:
    """
    Fire when any binding name starts with 'min_pos', OR when the current
    weight is below min_position (position is being topped up to reach floor).
    """
    for b in ctx.bindings:
        if b.name.startswith("min_pos"):
            return "min pos 補齊"
    if ctx.side == "BUY" and ctx.w0 < config.min_position and ctx.w0 > 0.0:
        return "min pos 補齊"
    return ""


def _score_delta_rule(ctx: RationaleContext) -> str:
    """
    Fire when score_delta is non-zero (meaningful direction signal).
    Format: "+2.1 score" or "-1.3 score". One decimal place, fits ≤ 15 chars.
    """
    if abs(ctx.score_delta) > 1e-6:
        return f"{ctx.score_delta:+.1f} score"
    return ""


def _new_position_rule(ctx: RationaleContext) -> str:
    """
    Fire when the ticker has zero current weight (new position being opened)
    or when is_discovery=True (surfaced by discovery scan).
    """
    if ctx.is_discovery or ctx.w0 == 0.0:
        if ctx.discovery_rank is not None:
            return f"Discovery #{ctx.discovery_rank}"
        return "新增部位"
    return ""
