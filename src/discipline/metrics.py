"""
Alpha Strategist — Discipline Metrics (V2.0)

Implements the discipline dashboard metrics per spec §8.2:

  drift_pct        ‖w_current − w_target_last‖₁ / 2
                   How far current weights have drifted from the last
                   recommended target. 0% = perfectly aligned.

  turnover_30d     Sum of |delta_weight| across all non-HOLD decisions in
                   the last 30 calendar days from the decisions archive.
                   This reflects suggested turnover, not executed turnover
                   (actuals are not yet tracked in Sprint 3).

  last_trade_days  Calendar days since the most recent non-HOLD decision
                   in the archive. 0 when the archive has a decision today.

  discipline_score_7d
                   Integer 0–100. Ratio of aligned suggestion/actual pairs
                   over the last 7 days. "Aligned" means:
                     - System suggested BUY/SELL and user executed same ticker
                       same direction.
                     - System suggested HOLD and user made no trades.
                   Returns 0 when there are no suggestions (empty archive or
                   archive older than 7 days) — guard for division by zero.

ActualsProvider Contract for Sprint 4
──────────────────────────────────────
Sprint 3 ships EmptyActualsProvider which returns [] for all queries, giving
discipline_score_7d = 0. Sprint 4's Slack /trade add command writes to
memory/actual_trades.jsonl; Sprint 4 implements JsonlActualsProvider that
reads this file and returns ActualTrade objects.

The contract for Sprint 4 implementers:
  - actual_trades.jsonl: one JSON object per line, fields:
        {"ticker": str, "side": "BUY"|"SELL"|"HOLD",
         "market": "US"|"TW", "date": "YYYY-MM-DD"}
  - HOLD entries are written when user clicks "Skip" in Slack
  - BUY/SELL entries are written when /trade add modal is submitted
  - "system-suggested" flag is stored but not required by this Protocol
  - date is Taipei local date (YYYY-MM-DD)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timezone, timedelta
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

import numpy as np

logger = logging.getLogger(__name__)

_TZ_TAIPEI = timezone(timedelta(hours=8))
_TURNOVER_WINDOW_DAYS = 30
_DISCIPLINE_WINDOW_DAYS = 7


@dataclass
class ActualTrade:
    """
    A trade action actually executed (or HOLD) by the user.

    Populated by Sprint 4's Slack /trade add and /skip actions.
    date is in Taipei local time (YYYY-MM-DD string).
    """

    ticker: str
    side: Literal["BUY", "SELL", "HOLD"]
    market: Literal["US", "TW"]
    date: str  # ISO YYYY-MM-DD


@runtime_checkable
class ActualsProvider(Protocol):
    """
    Protocol for retrieving actual trade actions taken by the user.

    Sprint 3 ships EmptyActualsProvider. Sprint 4 implements
    JsonlActualsProvider reading memory/actual_trades.jsonl.

    Parameters
    ----------
    days:
        Look back this many calendar days from today.
    market:
        "US", "TW", or "ALL" for both markets.

    Returns
    -------
    list[ActualTrade]
        All actual actions in the window. Empty list when no data.
    """

    def actual_actions(
        self,
        days: int,
        market: Literal["US", "TW", "ALL"] = "ALL",
    ) -> list[ActualTrade]: ...


class EmptyActualsProvider:
    """
    Sprint 3 placeholder ActualsProvider — returns [] for all queries.

    discipline_score_7d will return 0 while this provider is in use.
    Sprint 4 replaces this with JsonlActualsProvider.
    """

    def actual_actions(
        self,
        days: int,
        market: Literal["US", "TW", "ALL"] = "ALL",
    ) -> list[ActualTrade]:
        return []


@dataclass
class DisciplineMetrics:
    """
    Discipline dashboard metrics per spec §8.2.

    Attributes
    ----------
    drift_pct:
        ‖w_current − w_target_last‖₁ / 2 as a percentage [0, 100].
        0.0 when no prior target exists in the archive.
    turnover_30d:
        Cumulative suggested turnover over the last 30 days as a fraction
        [0.0, ∞). E.g. 0.12 = 12% suggested turnover.
    last_trade_days:
        Calendar days since the most recent non-HOLD decision. 0 if a
        non-HOLD decision was archived today; -1 if archive is empty.
    discipline_score_7d:
        Integer 0–100. 0 when no suggestions exist in the 7-day window.
    """

    drift_pct: float
    turnover_30d: float
    last_trade_days: int
    discipline_score_7d: int


def compute_metrics(
    w_current: np.ndarray,
    tickers: list[str],
    decisions_path: Path,
    actuals_provider: ActualsProvider,
    today: date | None = None,
) -> DisciplineMetrics:
    """
    Compute all four discipline metrics.

    Parameters
    ----------
    w_current:
        Current portfolio weights (N,). Used for drift computation.
    tickers:
        Ordered ticker list parallel to w_current.
    decisions_path:
        Path to memory/rebalance_decisions.jsonl.
    actuals_provider:
        Source of actual user actions. EmptyActualsProvider returns 0
        discipline score.
    today:
        Reference date for all window calculations. Defaults to today in
        Taipei time. Injectable for deterministic testing.

    Returns
    -------
    DisciplineMetrics
    """
    if today is None:
        today = date.today()

    records = _read_decisions(decisions_path)

    drift = _compute_drift(w_current=w_current, tickers=tickers, records=records)
    turnover = _compute_turnover_30d(records=records, today=today)
    last_days = _compute_last_trade_days(records=records, today=today)
    discipline = _compute_discipline_score_7d(
        records=records, actuals_provider=actuals_provider, today=today
    )

    return DisciplineMetrics(
        drift_pct=drift,
        turnover_30d=turnover,
        last_trade_days=last_days,
        discipline_score_7d=discipline,
    )


# ── Private helpers ────────────────────────────────────────────────────────────

def _read_decisions(path: Path) -> list[dict]:
    """Read all lines from the JSONL archive; return [] on error or absent file."""
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("[METRICS] Skipped malformed decision record")
    except OSError as exc:
        logger.warning("[METRICS] Cannot read decisions archive: %s", exc)
    return records


def _compute_drift(
    w_current: np.ndarray,
    tickers: list[str],
    records: list[dict],
) -> float:
    """
    Compute ‖w_current − w_target_last‖₁ / 2 as a percentage.

    Uses the most recent non-HOLD decision's w_target for the relevant market.
    Falls back to 0.0 when no prior target exists.
    """
    if not records:
        return 0.0

    ticker_to_idx = {t: i for i, t in enumerate(tickers)}

    # Find last non-HOLD record that shares tickers with current state
    for rec in reversed(records):
        if rec.get("is_hold", True):
            continue
        archived_tickers: list[str] = rec.get("tickers", [])
        w_target_list: list[float] = rec.get("w_target", [])
        if len(archived_tickers) != len(w_target_list):
            continue

        # Align archived target to current ticker order
        w_target = np.zeros(len(tickers))
        for t, wt in zip(archived_tickers, w_target_list):
            if t in ticker_to_idx:
                w_target[ticker_to_idx[t]] = wt

        l1 = float(np.sum(np.abs(w_current - w_target)))
        return round(l1 / 2 * 100, 4)

    return 0.0


def _compute_turnover_30d(records: list[dict], today: date) -> float:
    """
    Sum |delta_weight| across all non-HOLD decision trades in the last 30 days.
    Returns a fraction (e.g. 0.12 = 12% turnover).
    """
    cutoff = today - timedelta(days=_TURNOVER_WINDOW_DAYS)
    total = 0.0
    for rec in records:
        if rec.get("is_hold", True):
            continue
        rec_date = _parse_date(rec.get("timestamp", ""))
        if rec_date is None or rec_date < cutoff:
            continue
        for trade in rec.get("trades", []):
            total += abs(trade.get("delta_weight", 0.0))
    return round(total, 6)


def _compute_last_trade_days(records: list[dict], today: date) -> int:
    """
    Days since the most recent non-HOLD decision.
    Returns -1 if archive is empty or has no non-HOLD decisions.
    """
    for rec in reversed(records):
        if rec.get("is_hold", True):
            continue
        rec_date = _parse_date(rec.get("timestamp", ""))
        if rec_date is not None:
            return (today - rec_date).days
    return -1


def _compute_discipline_score_7d(
    records: list[dict],
    actuals_provider: ActualsProvider,
    today: date,
) -> int:
    """
    Compute discipline score 0–100 for the last 7 days.

    Suggestion = any decision record (including HOLD) within 7 days.
    Actual = from actuals_provider.actual_actions(days=7).

    Alignment rule:
      - Non-HOLD suggestion: aligned if the user executed any trade for the
        same ticker and same side on the same date.
      - HOLD suggestion: aligned if the user made NO trades on that date.

    Returns 0 when there are no suggestions (division by zero guard, per
    spec deviation D-S3-6).
    """
    cutoff = today - timedelta(days=_DISCIPLINE_WINDOW_DAYS)
    actuals = actuals_provider.actual_actions(days=_DISCIPLINE_WINDOW_DAYS)

    # Build a set of (ticker, side, date) for quick lookup
    actual_set: set[tuple[str, str, str]] = set()
    actual_dates_with_trades: set[str] = set()
    for a in actuals:
        if a.side in ("BUY", "SELL"):
            actual_set.add((a.ticker, a.side, a.date))
            actual_dates_with_trades.add(a.date)

    suggestions = 0
    aligned = 0

    for rec in records:
        rec_date = _parse_date(rec.get("timestamp", ""))
        if rec_date is None or rec_date < cutoff:
            continue

        date_str = rec_date.isoformat()
        suggestions += 1

        if rec.get("is_hold", False):
            # HOLD is aligned if user made no trades that day
            if date_str not in actual_dates_with_trades:
                aligned += 1
        else:
            # Active suggestion: check each trade for matching actual
            for trade in rec.get("trades", []):
                ticker = trade.get("ticker", "")
                side = trade.get("side", "")
                if (ticker, side, date_str) in actual_set:
                    aligned += 1
                    break  # one match per decision record counts

    if suggestions == 0:
        return 0
    return round(aligned / suggestions * 100)


def _parse_date(timestamp: str) -> date | None:
    """Parse ISO 8601 timestamp to Taipei local date. Returns None on failure."""
    if not timestamp:
        return None
    try:
        # Handle both naive and offset-aware strings
        dt_str = timestamp[:19]  # "YYYY-MM-DDTHH:MM:SS"
        from datetime import datetime
        dt = datetime.fromisoformat(dt_str)
        # Treat as Taipei time
        return dt.date()
    except (ValueError, TypeError):
        return None
