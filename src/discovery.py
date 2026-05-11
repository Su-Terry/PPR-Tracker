"""
Alpha Strategist — Alpha Discovery Module (V1.3)

Scans a configurable ticker universe and surfaces the top "Discovery" targets:
stocks with strong growth metrics that are not currently held in the portfolio.

Filter logic (purely quantitative, no predictions):
  1. PEG < 1.0         — growth at a reasonable price
  2. Price < MA50 × 1.05 — within 5 % of the 50-day MA (technical proximity)
  3. No bearish signals  — no 減倉停利 / 趨勢走弱
  4. Not already held   — excludes current portfolio tickers

Candidates are ranked by Efficiency Score and the top N returned.

Efficiency Score = peg_norm × dist_norm
  peg_norm  = max(0,  1 − PEG / 5)         → 1 at PEG=0,  0 at PEG≥5
  dist_norm = max(0,  1 − |dist%| / 20)    → 1 at MA50,   0 at ±20 %

Universe source (priority):
  1. data/discovery_universe.json  — user-editable watchlist (JSON array of tickers)
  2. _DEFAULT_UNIVERSE             — hardcoded US growth / AI-infrastructure names

CRITICAL: Output is for human review only. No order triggers. Human-in-the-Loop.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.data_fetcher import DATA_DIR, ScanResult, get_market_data

logger = logging.getLogger(__name__)

_UNIVERSE_FILE = DATA_DIR / "discovery_universe.json"
_TOP_N         = 3
_PEG_THRESHOLD = 1.0    # PEG must be below this
_MA_THRESHOLD  = 1.05   # price must be below MA50 × this

# Hardcoded fallback — US growth across AI, cloud, semiconductor, and defence
_DEFAULT_UNIVERSE: list[str] = [
    "NVDA", "MSFT", "AMZN", "META", "GOOGL",
    "AVGO", "AMD",  "ARM",  "QCOM", "ASML",
    "AMAT", "KLAC", "LRCX", "MRVL", "SMCI",
    "CRWD", "PANW", "NET",  "ZS",   "DDOG",
    "SNOW", "MDB",  "PLTR", "TTD",
]


# ── Scoring ───────────────────────────────────────────────────────────────────

def _efficiency_score(r: ScanResult) -> float:
    """
    Normalised efficiency score in [0, 1] for ranking Discovery candidates
    and identifying swap opportunities.

    Formula:
      peg_norm  = max(0,  1 − peg  / 5)
      dist_norm = max(0,  1 − |dist_to_MA50_%| / 20)
      score     = peg_norm × dist_norm

    Higher = more attractive entry:
      • Low PEG  → growth at a reasonable price
      • Close to MA50 → lower technical entry risk

    Returns 0.0 whenever data are missing, PEG is non-positive, or PEG is infinite.
    """
    if r.current_price is None or r.ma50 is None or r.ma50 == 0:
        return 0.0

    if r.valuation_model == "PEG" and r.modified_peg is not None:
        peg = r.modified_peg
    elif r.valuation_model == "PS" and r.ps_growth_ratio is not None:
        peg = r.ps_growth_ratio
    else:
        return 0.0

    if peg <= 0 or peg == float("inf"):
        return 0.0

    dist_pct = abs((r.current_price - r.ma50) / r.ma50 * 100)
    peg_norm  = max(0.0, 1.0 - peg / 5.0)
    dist_norm = max(0.0, 1.0 - dist_pct / 20.0)
    return peg_norm * dist_norm


# ── Universe management ───────────────────────────────────────────────────────

def load_universe() -> list[str]:
    """
    Load the discovery universe from data/discovery_universe.json.

    Falls back to _DEFAULT_UNIVERSE if the file is absent or invalid.

    Returns:
        List of ticker strings (upper-case, de-duplicated).
    """
    if _UNIVERSE_FILE.exists():
        try:
            data = json.loads(_UNIVERSE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                tickers = list(dict.fromkeys(str(t).strip().upper() for t in data if t))
                logger.info("[DISCOVERY] 自訂宇宙清單：%d 個標的", len(tickers))
                return tickers
        except Exception as exc:
            logger.warning("[DISCOVERY] 宇宙清單讀取失敗：%s — 退回預設清單", exc)

    logger.info("[DISCOVERY] 使用預設宇宙清單（%d 個標的）", len(_DEFAULT_UNIVERSE))
    return list(_DEFAULT_UNIVERSE)


# ── Main scanner ──────────────────────────────────────────────────────────────

def scan_market_for_alpha(
    universe:          list[str] | None = None,
    portfolio_tickers: list[str] | None = None,
    top_n:             int              = _TOP_N,
) -> list[ScanResult]:
    """
    Scan the universe and return the top Discovery targets not already held.

    A candidate must satisfy ALL of:
      - PEG (or P/S Growth Ratio) < 1.0
      - current_price < ma50 × 1.05
      - No 減倉停利 / 趨勢走弱 signals
      - Ticker not in portfolio_tickers

    Results are ranked by _efficiency_score (descending) and tagged
    ``is_discovery = True`` before being returned.

    Args:
        universe:          Explicit ticker list. If None, load_universe() is used.
        portfolio_tickers: Currently held tickers (any format — bare or suffixed).
                           These are excluded from Discovery output.
        top_n:             Maximum number of targets to return.

    Returns:
        Up to ``top_n`` ScanResult objects, sorted by Efficiency Score descending.
    """
    tickers = universe if universe is not None else load_universe()

    # Build bare-code exclusion set (strip .TW / .TWO / .US suffixes)
    held_bare: set[str] = set()
    for t in (portfolio_tickers or []):
        held_bare.add(t.split(".")[0].upper())

    scan_list = [t for t in tickers if t.upper().split(".")[0] not in held_bare]
    if not scan_list:
        logger.info("[DISCOVERY] 宇宙清單與持倉完全重疊，無新標的可掃描。")
        return []

    logger.info("[DISCOVERY] 掃描宇宙中 %d 個標的...", len(scan_list))
    results = get_market_data(scan_list)

    candidates: list[ScanResult] = []
    for r in results:
        if r.error:
            continue
        if r.current_price is None or r.ma50 is None or r.ma50 == 0:
            continue

        # Require a PEG-model result; P/S model tickers may also qualify
        if r.valuation_model == "PEG" and r.modified_peg is not None:
            ratio = r.modified_peg
        elif r.valuation_model == "PS" and r.ps_growth_ratio is not None:
            ratio = r.ps_growth_ratio
        else:
            continue

        if ratio is None or ratio <= 0 or ratio == float("inf") or ratio >= _PEG_THRESHOLD:
            continue

        if r.current_price >= r.ma50 * _MA_THRESHOLD:
            continue

        if any(s in ("減倉停利", "趨勢走弱") for s in r.signals):
            continue

        candidates.append(r)

    # Rank and tag
    candidates.sort(key=_efficiency_score, reverse=True)
    top = candidates[:top_n]
    for r in top:
        r.is_discovery = True

    logger.info(
        "[DISCOVERY] 完成：%d 個候選，返回 Top %d（Efficiency Score 排序）",
        len(candidates), len(top),
    )
    return top
