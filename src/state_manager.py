"""
Alpha Strategist — Dip-Buy Candidate State Manager (V3.1)

Persists tickers that were *partially* exited (exit_ratio < 1.0) due to
technical overheating while retaining strong fundamentals (PEG ≤ 0.8).
On each subsequent scan the system checks whether the MA50 distance has
cooled into the re-entry window and, if so, promotes the ticker to the
TACTICAL RE-ENTER section of the Slack dashboard.

State is stored as a flat JSON dict at data/dip_buy_candidates.json.
Keys are ticker strings; values are candidate records (see below).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data_fetcher import ScanResult

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent.parent / "data" / "dip_buy_candidates.json"

# EMA Value Zone re-entry thresholds (all decimal, not %).
# The signal fires only when ALL three conditions hold simultaneously:
#   1. MA50 trend is aggressively intact (price well above the 50-day MA).
#   2. Price has pulled back to or just below the 10-day EMA (momentum support).
#   3. Price is still at or above the 21-day EMA (trend-following floor).
REENTRY_MA50_MIN:   float =  0.15   # MA50 dist must be > +15 % (strong uptrend)
REENTRY_EMA10_MAX:  float =  0.01   # price at most +1 % above EMA10 (pullback reached)
REENTRY_EMA21_MIN:  float = -0.01   # price at most -1 % below EMA21 (trend intact)


# ── Persistence ───────────────────────────────────────────────────────────────

def load_dip_buy_candidates() -> dict[str, dict]:
    """
    Load persisted DIP_BUY_CANDIDATE records from disk.

    Returns:
        Dict mapping ticker → candidate record dict.
        Empty dict on first run or if the state file is missing / corrupt.
    """
    if not _STATE_FILE.exists():
        return {}
    try:
        with _STATE_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[STATE] 無法讀取 dip-buy 候選名單：%s", exc)
        return {}


def _save_dip_buy_candidates(candidates: dict[str, dict]) -> None:
    """Write the full candidate dict to disk atomically."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _STATE_FILE.open("w", encoding="utf-8") as fh:
            json.dump(candidates, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.error("[STATE] 無法寫入 dip-buy 候選名單：%s", exc)


# ── Registration ──────────────────────────────────────────────────────────────

def register_dip_buy_candidate(
    ticker:            str,
    peg:               float | None,
    exit_ratio:        float,
    exit_price:        float | None,
    ma50_dist_at_exit: float | None,
) -> None:
    """
    Register a ticker as a DIP_BUY_CANDIDATE after a partial exit.

    Only tickers with exit_ratio < 1.0 should be registered — partial exits
    imply the system still believes in the fundamentals but is reducing
    overheated technical exposure.

    Args:
        ticker:            Ticker string (e.g. "QCOM").
        peg:               Modified PEG (or PS Growth ratio) at the time of exit.
        exit_ratio:        Fraction sold (0.5 or 0.75).
        exit_price:        Current price at time of exit (for reference).
        ma50_dist_at_exit: MA50 distance at exit (decimal, e.g. 0.547 for +54.7%).
    """
    candidates = load_dip_buy_candidates()
    candidates[ticker] = {
        "ticker":            ticker,
        "peg":               peg,
        "exit_ratio":        exit_ratio,
        "exit_date":         date.today().isoformat(),
        "exit_price":        exit_price,
        "ma50_dist_at_exit": ma50_dist_at_exit,
    }
    _save_dip_buy_candidates(candidates)
    logger.info(
        "[STATE] 已登錄 DIP_BUY_CANDIDATE：%s  PEG=%.2f  exit=%.0f%%  MA50_dist=%.1f%%",
        ticker,
        peg or 0.0,
        exit_ratio * 100,
        (ma50_dist_at_exit or 0.0) * 100,
    )


def remove_dip_buy_candidate(ticker: str) -> None:
    """
    Remove a ticker from the candidate registry (e.g. after re-entry fires).

    Args:
        ticker: Ticker to remove.
    """
    candidates = load_dip_buy_candidates()
    if ticker in candidates:
        del candidates[ticker]
        _save_dip_buy_candidates(candidates)
        logger.info("[STATE] 已從 DIP_BUY_CANDIDATE 移除：%s", ticker)


# ── Re-entry detection ────────────────────────────────────────────────────────

def check_reentry_signals(
    results:    list["ScanResult"],
    candidates: dict[str, dict],
) -> list[dict]:
    """
    Scan current ScanResult list for tickers that have entered the EMA Value Zone
    after a prior partial exit (DIP_BUY_CANDIDATE registry).

    The "EMA Value Zone" fires when ALL three conditions hold simultaneously:
      1. MA50 dist > REENTRY_MA50_MIN (+15 %) — macro uptrend aggressively intact.
      2. EMA10 dist ≤ REENTRY_EMA10_MAX (+1 %) — price has pulled back to EMA10.
      3. EMA21 dist ≥ REENTRY_EMA21_MIN (−1 %) — price still supported by EMA21.

    This window is narrow by design: the stock must be in a strong uptrend (MA50
    criterion) while simultaneously bouncing in the tight EMA10–EMA21 band, which
    is the institutional re-entry zone for momentum stocks.

    Args:
        results:    Current scan results (should include portfolio holdings).
        candidates: Output of load_dip_buy_candidates().

    Returns:
        List of signal dicts, each with keys:
          ticker, scan_result, candidate_record,
          ma50_dist, ema10_dist, ema21_dist (all decimal).
        Empty if no signals triggered.
    """
    signals: list[dict] = []
    for r in results:
        if r.error or r.ticker not in candidates:
            continue
        if r.current_price is None or r.ma50 is None or r.ma50 == 0:
            continue
        if r.ema10_dist is None or r.ema21_dist is None:
            continue

        ma50_dist = (r.current_price - r.ma50) / r.ma50

        if (
            ma50_dist   >  REENTRY_MA50_MIN    # still well above MA50
            and r.ema10_dist <= REENTRY_EMA10_MAX   # pulled back to EMA10
            and r.ema21_dist >= REENTRY_EMA21_MIN   # holding EMA21 support
        ):
            signals.append({
                "ticker":           r.ticker,
                "scan_result":      r,
                "candidate_record": candidates[r.ticker],
                "ma50_dist":        ma50_dist,
                "ema10_dist":       r.ema10_dist,
                "ema21_dist":       r.ema21_dist,
            })
            logger.info(
                "[STATE] EMA Value Zone 再進場訊號：%s  "
                "MA50=+%.1f%%  EMA10=%.1f%%  EMA21=%.1f%%",
                r.ticker,
                ma50_dist    * 100,
                r.ema10_dist * 100,
                r.ema21_dist * 100,
            )
    return signals
