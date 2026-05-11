"""
Alpha Strategist — Pre-Execution Risk Gate (V3.0)

Implements a stateful SimulatedPortfolio that evaluates proposed swaps
against hard sector-concentration limits *before* they are emitted as
recommendations.  Each approved swap is committed to internal state so
subsequent evaluations see the realistic post-swap portfolio composition,
not the stale pre-trade snapshot.

Public surface
──────────────
  MarketRegime                : enum — BULL | NEUTRAL | BEAR | CRASH
  classify_regime()           : map (vix_close, spy_ma_dist) → MarketRegime
  SimulatedPortfolio          : mutable state machine (holdings as USD notional)
  get_post_trade_snapshot()   : derive a synthetic ScanResult list for dashboard rendering

Design principles
─────────────────
  • Gate logic is a pure read: simulate_swap() returns a copy, never mutates.
  • Mutation is explicit: commit_swap() must be called after an approval.
  • Sector lookups reuse correlation._sector_cache, so they are O(1) after the
    first scan fetches each ticker's sector from yfinance.
  • TW stock notionals (NTD) and US stock notionals (USD) are both stored as
    raw numbers.  Relative sector weights are still meaningful within each
    currency group, and in mixed portfolios the DEFAULT_CAPITAL_BLOCK fallback
    ensures no single unknown position distorts the weight calculation.
"""

from __future__ import annotations

import enum
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd
    from src.data_fetcher import ScanResult

from src.config import DEFAULT_CAPITAL_BLOCK
from src.correlation import get_sector

logger = logging.getLogger(__name__)

# Hard sector-concentration ceiling applied to regular (non-cash-flight) swaps.
SECTOR_LIMIT_PCT: float = 0.40   # 40 % of total portfolio notional


# ── Macro Regime ──────────────────────────────────────────────────────────────

class MarketRegime(enum.Enum):
    """
    Four-state macro regime classification derived from SPY/VIX data.

    States (in severity order):
      BULL    – SPY at or above its 50-day MA; normal operation.
      NEUTRAL – SPY mildly below MA but VIX not yet elevated; cautious operation.
      BEAR    – VIX ≥ 20 AND SPY below its 50-day MA; beta penalty applied.
      CRASH   – VIX ≥ 30 AND SPY > 5 % below its 50-day MA; equity swaps halted.
    """
    BULL    = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR    = "BEAR"
    CRASH   = "CRASH"


def classify_regime(vix_close: float, spy_ma_dist: float) -> MarketRegime:
    """
    Classify the market regime from two macro indicators.

    Evaluation is ordered from most to least severe so the strongest signal
    always wins.

    Args:
        vix_close:   Latest VIX closing price (e.g. 28.5).
        spy_ma_dist: SPY signed distance from its 50-day MA as a decimal
                     (e.g. -0.07 = 7 % below).

    Returns:
        MarketRegime enum value.
    """
    if vix_close >= 30.0 and spy_ma_dist < -0.05:
        return MarketRegime.CRASH
    if vix_close >= 20.0 and spy_ma_dist < 0.0:
        return MarketRegime.BEAR
    if spy_ma_dist >= 0.0:
        return MarketRegime.BULL
    return MarketRegime.NEUTRAL   # VIX < 20, SPY slightly below MA


class SimulatedPortfolio:
    """
    Mutable state machine representing portfolio holdings as a
    ticker → notional-value mapping.

    Notional values are in USD for US stocks and NTD for TW stocks when
    real data is available; DEFAULT_CAPITAL_BLOCK (USD) is the fallback.
    Because the sector gate only compares relative weights, currency mixing
    only matters when TW and US positions are of wildly different scale —
    in practice the fallback keeps them comparable.

    Usage pattern inside get_optimal_swaps
    ──────────────────────────────────────
      1. Instantiate once via from_scan_results().
      2. For each candidate swap:
           synthetic = portfolio.simulate_swap(src, tgt, friction)
           approved  = portfolio.check_sector_limits(synthetic, tgt_sector)
      3. On approval: portfolio.commit_swap(src, tgt, friction) then record swap.
      4. On rejection: try next candidate — do NOT commit.
    """

    def __init__(self, holdings: dict[str, float]) -> None:
        self._holdings: dict[str, float] = dict(holdings)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_scan_results(
        cls,
        results: list["ScanResult"],
        portfolio_df: "pd.DataFrame | None" = None,
    ) -> "SimulatedPortfolio":
        """
        Build a SimulatedPortfolio from the current scan.

        Notional = shares × current_price when both are available from the
        portfolio CSV + ScanResult.  Falls back to DEFAULT_CAPITAL_BLOCK for
        any ticker whose share count or price is missing.

        Args:
            results:      Non-errored ScanResult list for current holdings.
            portfolio_df: DataFrame with columns [Ticker, Shares, Cost_Basis, source]
                          from get_portfolio().  Optional — triggers real-notional path.

        Returns:
            Initialised SimulatedPortfolio.
        """
        # Build shares lookup from portfolio DF (Ticker → share count)
        shares_map: dict[str, float] = {}
        if portfolio_df is not None and not portfolio_df.empty:
            for _, row in portfolio_df.iterrows():
                raw_ticker = str(row.get("Ticker", "")).strip()
                shares_val = row.get("Shares")
                if raw_ticker and shares_val is not None:
                    try:
                        s = float(shares_val)
                        if s > 0:
                            shares_map[raw_ticker] = s
                    except (ValueError, TypeError):
                        pass

        holdings: dict[str, float] = {}
        for r in results:
            if r.error:
                continue
            shares = shares_map.get(r.ticker)
            if shares is not None and r.current_price is not None:
                notional = shares * r.current_price
            else:
                notional = DEFAULT_CAPITAL_BLOCK
            holdings[r.ticker] = notional
            logger.debug(
                "[RISK ENGINE] %s notional=%.2f (%s)",
                r.ticker, notional,
                "real" if (shares is not None and r.current_price is not None) else "fallback",
            )

        return cls(holdings)

    # ── State read ────────────────────────────────────────────────────────────

    def get_holdings(self) -> dict[str, float]:
        """Return a shallow copy of current holdings."""
        return dict(self._holdings)

    # ── Simulation (non-mutating) ─────────────────────────────────────────────

    def simulate_swap(
        self,
        source_ticker: str,
        target_ticker: str,
        friction_cost: float,
        exit_ratio:    float = 1.0,
    ) -> dict[str, float]:
        """
        Return the hypothetical holdings dict that would result from executing
        this swap.  Does NOT mutate internal state.

        Supports partial exits via exit_ratio — e.g. 0.5 sells half the position
        while retaining the other half in the source ticker.

        Logic:
          1. Compute sold_proceeds = exit_ratio × source notional.
          2. Retain (1 − exit_ratio) of the source position (removed if zero).
          3. Deduct friction_cost from proceeds (floor at 0).
          4. Credit net proceeds to target_ticker (additive).

        Args:
            source_ticker: Ticker being sold.
            target_ticker: Ticker being bought.
            friction_cost: Round-trip friction in the same units as notional.
            exit_ratio:    Fraction of position to sell (default 1.0 = full exit).

        Returns:
            New holdings dict (copy of internal state with swap applied).
        """
        synthetic    = dict(self._holdings)
        full_pos     = synthetic.get(source_ticker, 0.0)
        sold_portion = exit_ratio * full_pos
        retained     = full_pos - sold_portion

        if retained > 0.0:
            synthetic[source_ticker] = retained
        else:
            synthetic.pop(source_ticker, None)

        net_proceeds = max(0.0, sold_portion - friction_cost)
        synthetic[target_ticker] = synthetic.get(target_ticker, 0.0) + net_proceeds
        return synthetic

    # ── Gate check (non-mutating) ─────────────────────────────────────────────

    def check_sector_limits(
        self,
        synthetic_holdings: dict[str, float],
        target_sector: str,
        limit_pct: float = SECTOR_LIMIT_PCT,
    ) -> bool:
        """
        Evaluate whether the target sector's notional weight in the synthetic
        (post-swap) portfolio exceeds the hard concentration limit.

        Args:
            synthetic_holdings: Output of simulate_swap().
            target_sector:      Sector label of the proposed buy ticker
                                (pre-fetched by the caller to avoid repeated lookups).
            limit_pct:          Hard ceiling as a decimal (default SECTOR_LIMIT_PCT = 0.40).

        Returns:
            True  → swap approved (sector weight within limits).
            False → swap rejected (sector weight would breach limit).
        """
        total = sum(synthetic_holdings.values())
        if total <= 0:
            return True  # empty portfolio — no constraint

        # Accumulate notional per sector
        sector_totals: dict[str, float] = {}
        for ticker, notional in synthetic_holdings.items():
            sector = get_sector(ticker)
            sector_totals[sector] = sector_totals.get(sector, 0.0) + notional

        target_weight = sector_totals.get(target_sector, 0.0) / total
        approved = target_weight <= limit_pct

        logger.info(
            "[RISK GATE] sector='%s'  post-swap weight=%.1f%%  limit=%.0f%%  → %s",
            target_sector,
            target_weight * 100,
            limit_pct * 100,
            "APPROVED" if approved else "REJECTED",
        )
        if not approved:
            # Log the full sector breakdown to aid debugging
            breakdown = "  ".join(
                f"{s}: {v / total:.0%}" for s, v in
                sorted(sector_totals.items(), key=lambda x: -x[1])
            )
            logger.info("[RISK GATE] Post-swap sector breakdown: %s", breakdown)

        return approved

    # ── Mutation (commit after approval) ─────────────────────────────────────

    def commit_swap(
        self,
        source_ticker: str,
        target_ticker: str,
        friction_cost: float,
        exit_ratio:    float = 1.0,
    ) -> None:
        """
        Persist an approved swap into internal state.

        Must only be called after check_sector_limits() returns True for the
        same (source, target, friction_cost, exit_ratio) combination.

        Args:
            source_ticker: Ticker being sold.
            target_ticker: Ticker being bought.
            friction_cost: Round-trip friction cost.
            exit_ratio:    Fraction of source position to sell (default 1.0).
        """
        self._holdings = self.simulate_swap(
            source_ticker, target_ticker, friction_cost, exit_ratio=exit_ratio
        )
        logger.debug(
            "[RISK ENGINE] Committed swap %s → %s (friction=%.2f, exit=%.0f%%)",
            source_ticker, target_ticker, friction_cost, exit_ratio * 100,
        )


# ── Dashboard helper ──────────────────────────────────────────────────────────

def get_post_trade_snapshot(
    portfolio_results: list["ScanResult"],
    approved_swaps: list[dict],
) -> list["ScanResult"]:
    """
    Produce the synthetic post-trade ScanResult list from a set of approved swaps.

    Removes every source_ticker that was sold; appends each target_ticker's
    ScanResult.  Cash-flight swaps remove the source and add the safe-haven
    ScanResult (BOXX or None for pure-cash routes).

    This list is used by the dashboard to compute the SECTOR CONCENTRATION
    block on the *simulated* post-trade state rather than the stale pre-trade
    portfolio, proving that all recommended swaps respect hard risk limits.

    Args:
        portfolio_results: All ScanResult objects from the current portfolio scan.
        approved_swaps:    Output of get_optimal_swaps() (may include cash-flights).

    Returns:
        Synthetic ScanResult list representing the portfolio after all swaps.
    """
    # Partial exits (exit_ratio < 1.0) keep the ticker in the portfolio — only
    # full exits (exit_ratio == 1.0, the default) remove the source entirely.
    sold_tickers: set[str] = {
        s["source_ticker"].ticker
        for s in approved_swaps
        if s.get("exit_ratio", 1.0) >= 1.0
    }

    bought: list["ScanResult"] = [
        s["target_ticker"]
        for s in approved_swaps
        if s["target_ticker"] is not None
    ]

    post_trade = [r for r in portfolio_results if r.ticker not in sold_tickers] + bought
    logger.debug(
        "[RISK ENGINE] Post-trade snapshot: %d holdings (fully sold %d, bought %d)",
        len(post_trade), len(sold_tickers), len(bought),
    )
    return post_trade
