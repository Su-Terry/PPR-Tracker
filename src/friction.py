"""
Alpha Strategist — Transaction Friction Cost Module (V1.0)

Computes round-trip trading costs for a swap recommendation so the
rotation engine can suppress or flag advice that cannot pay for itself.

Cost model
──────────
TW stocks (.TW / .TWO):
  Buy  commission : 0.1425 %, min NT$20
  Sell commission : 0.1425 %, min NT$20
  Securities tax  : 0.300  % (sell side only)
  ─────────────────────────────────────────
  Round-trip rate : ≈ 0.585 % (on NT$200k position = NT$1,170)

US stocks via 複委託 (foreign brokerage):
  Buy  commission : 0.100  %, min NT$300
  Sell commission : 0.100  %, min NT$300
  ─────────────────────────────────────────
  Round-trip rate : ≈ 0.200 % large, ≈ 0.600 % small (NT$100k or less)

Justification heuristic
────────────────────────
Because efficiency score delta cannot yet be converted to expected return %
(requires V2.0 backtesting calibration), we use a conservative multiplier:

  min_delta = friction_rate × JUSTIFY_MULTIPLIER   (default 0.25)

Rationale: a score_delta of 0.15 (our swap threshold) vs a TW round-trip
of 0.585 % gives 0.15 / 0.00585 ≈ 25.6×. At JUSTIFY_MULTIPLIER=0.25,
min_delta = 0.585 % × 0.25 = 0.00146 — well below our 0.15 threshold, so
all swaps already above threshold are considered friction-justified unless
the position is so small that minimum fees dominate.

The flag is deliberately conservative: the system should surface the
friction data, not silently kill swap advice.
"""

from __future__ import annotations

# ── Parameters (broker: Cathay 國泰) ─────────────────────────────────────────
_TW_BUY_RATE    = 0.001425   # 0.1425 %
_TW_SELL_RATE   = 0.001425   # 0.1425 %
_TW_TAX_RATE    = 0.003      # 0.3 %  (sell-side only)
_TW_MIN_NTD     = 20         # NT$20 minimum per side

_US_BUY_RATE    = 0.001      # 0.1 %  (複委託)
_US_SELL_RATE   = 0.001      # 0.1 %
_US_MIN_NTD     = 300        # NT$300 minimum per side

_DEFAULT_POS_NTD   = 200_000   # assumed position size when unknown
_HIGH_FRICTION_PCT = 0.004     # flag when total cost ≥ 0.4 % of position
_JUSTIFY_MULTIPLIER = 0.25     # min_delta = friction_rate × this


def _is_tw(ticker: str) -> bool:
    return ticker.upper().endswith((".TW", ".TWO"))


def estimate_round_trip(
    sell_ticker: str,
    buy_ticker:  str,
    position_ntd: float = _DEFAULT_POS_NTD,
) -> dict:
    """
    Compute round-trip transaction cost for one swap pair.

    Covers both legs: selling the source position and buying the target.
    Minimum-fee logic prevents underestimating cost on small positions.

    Args:
        sell_ticker:  Ticker being sold (e.g. "TSLA", "2330.TW").
        buy_ticker:   Ticker being bought.
        position_ntd: Assumed position size in NT dollars (default NT$200,000).

    Returns:
        Dict with keys:
          sell_cost_ntd  – cost of selling the source position (NT$)
          buy_cost_ntd   – cost of buying the target position (NT$)
          total_cost_ntd – total round-trip cost (NT$)
          total_rate     – total cost as a fraction of position (e.g. 0.00585)
          high_friction  – True if total_rate ≥ _HIGH_FRICTION_PCT
          justified      – True when score_delta > min_delta_to_justify (see note)
          min_delta      – minimum score delta needed to justify this friction
          label          – human-readable summary for Slack
    """
    # ── Sell leg ─────────────────────────────────────────────────────────────
    if _is_tw(sell_ticker):
        sell_comm = max(position_ntd * _TW_SELL_RATE, _TW_MIN_NTD)
        sell_tax  = position_ntd * _TW_TAX_RATE
        sell_cost = sell_comm + sell_tax
    else:
        sell_cost = max(position_ntd * _US_SELL_RATE, _US_MIN_NTD)

    # ── Buy leg ──────────────────────────────────────────────────────────────
    if _is_tw(buy_ticker):
        buy_cost = max(position_ntd * _TW_BUY_RATE, _TW_MIN_NTD)
    else:
        buy_cost = max(position_ntd * _US_BUY_RATE, _US_MIN_NTD)

    total_cost = sell_cost + buy_cost
    total_rate = total_cost / position_ntd
    high_friction = total_rate >= _HIGH_FRICTION_PCT
    min_delta     = total_rate * _JUSTIFY_MULTIPLIER

    # Compose a compact label for the rotation block
    rate_pct  = total_rate * 100
    cost_str  = f"NT${total_cost:,.0f}"
    flag      = "  ⚠️ _高摩擦成本_" if high_friction else ""
    label     = f"💸 摩擦成本：{cost_str}（{rate_pct:.2f}%）{flag}"

    return {
        "sell_cost_ntd":  round(sell_cost, 1),
        "buy_cost_ntd":   round(buy_cost,  1),
        "total_cost_ntd": round(total_cost, 1),
        "total_rate":     round(total_rate, 6),
        "high_friction":  high_friction,
        "min_delta":      round(min_delta, 6),
        "label":          label,
    }


def is_friction_justified(score_delta: float, friction: dict) -> bool:
    """
    Return True if the efficiency score gain is large enough to justify
    the round-trip cost.

    Args:
        score_delta: Efficiency score improvement (buy_score − sell_score).
        friction:    Output of estimate_round_trip().

    Returns:
        True when score_delta ≥ friction["min_delta"].
    """
    return score_delta >= friction["min_delta"]
