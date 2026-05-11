"""
Alpha Strategist — Decision Memory Logger (V1.0)

Archives every daily scan to JSONL so V2.0 backtesting can:
  • Replay signal decisions against historical prices
  • Calibrate PEG / Efficiency Score thresholds automatically
  • Evaluate swap advice quality (did the target actually outperform?)

File layout:
  data/scan_history/YYYY-MM-DD.jsonl   ← one record per scan run

Each JSONL record is a self-contained snapshot of one scan run. Multiple
records per day are allowed (e.g. TW scan + US scan both write).

CRITICAL: Read-only observer — no side effects on scan data or Slack output.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.data_fetcher import DATA_DIR, ScanResult

logger = logging.getLogger(__name__)

_SCAN_HISTORY_DIR = DATA_DIR / "scan_history"

# ── Schema version — bump when record format changes incompatibly ─────────────
_SCHEMA_VERSION = "1.0"

# Scoring weights — mirrors calculate_efficiency_score defaults in llm_compiler
_GROWTH_WEIGHT    = 0.7
_STABILITY_WEIGHT = 0.3


# ── Internal helpers ──────────────────────────────────────────────────────────

def _category(r: ScanResult) -> str:
    """Derive the display category assigned to this ticker during the scan."""
    if r.error:
        return "ERROR"
    if any(s in ("減倉停利", "趨勢走弱") for s in r.signals):
        return "CRITICAL"
    if any("加倉 Alpha" in s for s in r.signals):
        return "ALPHA"
    if r.is_discovery:
        return "DISCOVERY"
    return "WATCH"


def _ma_dist_pct(r: ScanResult) -> float | None:
    """Return (price − MA50) / MA50 × 100, or None if data unavailable."""
    if r.current_price is not None and r.ma50 and r.ma50 > 0:
        return round((r.current_price - r.ma50) / r.ma50 * 100, 4)
    return None


def _valuation_ratio(r: ScanResult) -> float | None:
    """Return the primary valuation ratio (Modified PEG or P/S Growth Ratio)."""
    if r.valuation_model == "PEG" and r.modified_peg is not None:
        return None if r.modified_peg == float("inf") else round(r.modified_peg, 4)
    if r.valuation_model == "PS" and r.ps_growth_ratio is not None:
        return None if r.ps_growth_ratio == float("inf") else round(r.ps_growth_ratio, 4)
    return None


def _efficiency_score(r: ScanResult) -> float | None:
    """
    Recompute efficiency score for the archive record.

    Uses the same formula as llm_compiler.calculate_efficiency_score to keep
    the archive self-consistent without importing from llm_compiler (which
    would create an unnecessary cross-module dependency).

    Formula: (growth_weight / ratio) × (stability_weight / (1 + |ma_dist|))
    """
    if r.current_price is None or r.ma50 is None or r.ma50 == 0:
        return None
    ratio = _valuation_ratio(r)
    if ratio is None or ratio <= 0:
        return None
    ma_dist = abs(r.current_price - r.ma50) / r.ma50
    raw = (_GROWTH_WEIGHT / ratio) * (_STABILITY_WEIGHT / (1.0 + ma_dist))
    return round(raw, 6)


def _serialize_ticker(r: ScanResult) -> dict:
    """Serialize one ScanResult into an archive-ready dict."""
    return {
        "ticker":           r.ticker,
        "name":             r.name,
        "category":         _category(r),
        "valuation_model":  r.valuation_model or None,
        "valuation_ratio":  _valuation_ratio(r),
        "price":            round(r.current_price, 4) if r.current_price is not None else None,
        "ma50":             round(r.ma50, 4)          if r.ma50          is not None else None,
        "ma_dist_pct":      _ma_dist_pct(r),
        "efficiency_score": _efficiency_score(r),
        "signals":          list(r.signals),
        "momentum":         r.momentum,
        "trailing_pe":      round(r.trailing_pe, 4)      if r.trailing_pe      is not None else None,
        "earnings_growth":  round(r.earnings_growth, 6)  if r.earnings_growth  is not None else None,
        "revenue_growth":   round(r.revenue_growth, 6)   if r.revenue_growth   is not None else None,
        "week_52_high":     round(r.week_52_high, 4)     if r.week_52_high     is not None else None,
        "is_discovery":     r.is_discovery,
        "error":            r.error or None,
    }


def _serialize_swap(swap: dict) -> dict:
    """Serialize one swap dict from get_optimal_swaps() into archive form."""
    src: ScanResult = swap["source_ticker"]
    tgt: ScanResult = swap["target_ticker"]
    dm               = swap.get("delta_metrics", {})
    return {
        "from_ticker":      src.ticker,
        "to_ticker":        tgt.ticker,
        "sell_score":       round(swap["sell_score"],  6),
        "buy_score":        round(swap["buy_score"],   6),
        "score_delta":      round(swap["score_delta"], 6),
        "peg_improvement":  round(dm["peg_improvement"],  4) if dm.get("peg_improvement")  is not None else None,
        "dist_improvement": round(dm["dist_improvement"], 4) if dm.get("dist_improvement") is not None else None,
        "sell_ratio":       dm.get("sell_ratio"),
        "buy_ratio":        dm.get("buy_ratio"),
        "sell_dist_pct":    dm.get("sell_dist_pct"),
        "buy_dist_pct":     dm.get("buy_dist_pct"),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def archive_scan_context(
    portfolio_results: list[ScanResult],
    discovery_targets: list[ScanResult] | None = None,
    swap_advice:       list[dict]        | None = None,
    market:            str               = "ALL",
) -> Path | None:
    """
    Append one scan snapshot to today's JSONL archive file.

    Each call writes exactly one JSON line to
    ``data/scan_history/YYYY-MM-DD.jsonl``.  The function is designed to be
    called at the very end of daily_portfolio_scan — after Slack delivery —
    so a failure here never blocks the main pipeline.

    Record schema
    -------------
    {
      "schema_version": "1.0",
      "timestamp":      "<ISO-8601 UTC>",
      "market":         "TW" | "US" | "ALL",
      "scoring_weights": {"growth": 0.7, "stability": 0.3},
      "model_versions":  {"efficiency_score": "llm_compiler.calculate_efficiency_score@1.0"},
      "portfolio": [ <ticker_snapshot>, ... ],
      "discovery":  [ <ticker_snapshot>, ... ],
      "swaps":      [ <swap_pair>,       ... ]
    }

    Args:
        portfolio_results: All ScanResult objects from the portfolio scan.
        discovery_targets: Discovery candidates from scan_market_for_alpha().
        swap_advice:       Swap pairs from get_optimal_swaps().
        market:            Market label used in this scan run (TW / US / ALL).

    Returns:
        Path to the JSONL file written, or None on failure.
    """
    _SCAN_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    now        = datetime.now(timezone.utc)
    date_str   = now.strftime("%Y-%m-%d")
    out_path   = _SCAN_HISTORY_DIR / f"{date_str}.jsonl"

    record: dict = {
        "schema_version": _SCHEMA_VERSION,
        "timestamp":      now.isoformat(),
        "market":         market,
        "scoring_weights": {
            "growth":    _GROWTH_WEIGHT,
            "stability": _STABILITY_WEIGHT,
        },
        "model_versions": {
            "efficiency_score": "llm_compiler.calculate_efficiency_score@1.0",
        },
        "portfolio": [_serialize_ticker(r) for r in portfolio_results],
        "discovery": [_serialize_ticker(r) for r in (discovery_targets or [])],
        "swaps":     [_serialize_swap(s)   for s in (swap_advice       or [])],
    }

    try:
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info(
            "[MEMORY] 掃描快照已存檔 → %s  "
            "(%d portfolio, %d discovery, %d swaps)",
            out_path,
            len(portfolio_results),
            len(discovery_targets or []),
            len(swap_advice or []),
        )
        return out_path
    except Exception as exc:
        logger.error("[MEMORY] 存檔失敗（主流程不受影響）：%s", exc)
        return None
