"""
Alpha Strategist — Efficiency Score (shared module)

Extracted from llm_compiler.py so both the V1.1 adapter path (llm_compiler.py)
and the V2.0 runner (src/rebalancer/runner.py) can import from a single source of
truth without creating a circular dependency.

Function body is verbatim from llm_compiler.py:calculate_efficiency_score (V1.1).
"""

from __future__ import annotations

import logging

from src.data_fetcher import ScanResult
from src.risk_engine import MarketRegime

logger = logging.getLogger(__name__)


def efficiency_score(
    r: ScanResult,
    growth_weight: float = 0.7,
    stability_weight: float = 0.3,
    regime: MarketRegime | None = None,
) -> float:
    """
    Standardised efficiency score applied uniformly to ALL assets
    (Portfolio, Discovery, and Watch).

    Base formula:
      ma_dist    = |price − MA50| / MA50
      base_score = (growth_weight / ratio) × (stability_weight / (1 + ma_dist))

    Beta penalty (BEAR / CRASH regimes only):
      penalty           = max(0, (beta − 1.0) × 0.2)
      regime_multiplier = max(0, 1.0 − penalty)
      final_score       = base_score × regime_multiplier

    Returns 0.0 for any ticker missing price, MA50, or a valid valuation ratio,
    and for ratios ≤ 0 or infinite (guards against division-by-zero).

    Args:
        r:                ScanResult for any ticker.
        growth_weight:    Weight applied to the inverse-PEG component (default 0.7).
        stability_weight: Weight applied to the MA-proximity component (default 0.3).
        regime:           Current MacroRegime; enables beta penalty when BEAR/CRASH.

    Returns:
        Non-negative float efficiency score.
    """
    if r.current_price is None or r.ma50 is None or r.ma50 == 0:
        return 0.0

    if r.valuation_model == "PEG" and r.modified_peg is not None:
        ratio = r.modified_peg
    elif r.valuation_model == "PS" and r.ps_growth_ratio is not None:
        ratio = r.ps_growth_ratio
    else:
        return 0.0

    if ratio <= 0 or ratio == float("inf"):
        return 0.0

    ma_dist    = abs(r.current_price - r.ma50) / r.ma50
    base_score = (growth_weight / ratio) * (stability_weight / (1.0 + ma_dist))

    if regime in (MarketRegime.BEAR, MarketRegime.CRASH) and r.beta is not None:
        penalty           = max(0.0, (r.beta - 1.0) * 0.2)
        regime_multiplier = max(0.0, 1.0 - penalty)
        base_score       *= regime_multiplier
        logger.debug(
            "[SCORE] %s  beta=%.2f  penalty=%.0f%%  multiplier=%.2f  score=%.4f",
            r.ticker, r.beta, penalty * 100, regime_multiplier, base_score,
        )

    return base_score
