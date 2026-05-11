"""
Alpha Strategist — Portfolio Correlation & Sector Concentration (V1.0)

Detects when a portfolio (or a proposed post-swap portfolio) is over-exposed
to a single industry sector, and emits Slack warnings before the operator
executes a swap that would further concentrate risk.

Concentration thresholds
─────────────────────────
  > 35 % single sector → ⚠️  WATCH  (diversification advisory)
  > 50 % single sector → 🔴  ALERT  (concentration risk)

Sector data source: yfinance .info["sector"] / .info["industry"]
Caching: sector info is cached in-memory per process run (TTL: infinity
within a single scan — sector classification doesn't change intraday).
"""

from __future__ import annotations

import logging

import yfinance as yf

from src.data_fetcher import ScanResult

logger = logging.getLogger(__name__)

_WARN_THRESHOLD  = 0.35   # ⚠️ single-sector weight
_ALERT_THRESHOLD = 0.50   # 🔴 single-sector weight

# In-process sector cache — avoids duplicate yfinance calls within one scan
_sector_cache: dict[str, str] = {}


# ── Sector lookup ─────────────────────────────────────────────────────────────

def _get_sector(ticker: str) -> str:
    """
    Return the yfinance sector label for a ticker, with in-process caching.

    Args:
        ticker: Full ticker string (e.g. "NVDA", "2330.TW").

    Returns:
        Sector string (e.g. "Technology"), or "Unknown" on any failure.
    """
    if ticker in _sector_cache:
        return _sector_cache[ticker]

    try:
        sector = yf.Ticker(ticker).info.get("sector") or "Unknown"
    except Exception as exc:
        logger.debug("[CORRELATION] %s sector 查詢失敗：%s", ticker, exc)
        sector = "Unknown"

    _sector_cache[ticker] = sector
    return sector


def get_sector(ticker: str) -> str:
    """
    Public wrapper around _get_sector for cross-module use.

    Used by SimulatedPortfolio (risk_engine.py) during pre-execution gate
    checks so it can reuse the same in-process sector cache without reaching
    into the private _get_sector symbol directly.

    Args:
        ticker: Full ticker string (e.g. "NVDA", "2330.TW").

    Returns:
        Sector string (e.g. "Technology"), or "Unknown" on any failure.
    """
    return _get_sector(ticker)


# ── Public API ────────────────────────────────────────────────────────────────

def build_sector_map(results: list[ScanResult]) -> dict[str, list[str]]:
    """
    Build a sector → [tickers] mapping for the current portfolio.

    Tickers with errors or missing data are placed under "Unknown".
    Sector info is fetched via yfinance and cached for the process lifetime.

    Args:
        results: Portfolio ScanResult list (from get_market_data).

    Returns:
        Dict mapping sector name → list of ticker strings.
    """
    sector_map: dict[str, list[str]] = {}
    for r in results:
        if r.error:
            sector = "Unknown"
        else:
            sector = _get_sector(r.ticker)
        sector_map.setdefault(sector, []).append(r.ticker)

    logger.info(
        "[CORRELATION] 板塊地圖：%s",
        {k: len(v) for k, v in sector_map.items()},
    )
    return sector_map


def check_concentration(
    sector_map:  dict[str, list[str]],
    extra_tickers: list[str] | None = None,
) -> list[dict]:
    """
    Identify sectors that exceed concentration thresholds.

    Optionally include additional tickers (e.g. Discovery targets about to
    be bought) to preview post-swap concentration.

    Args:
        sector_map:    Output of build_sector_map().
        extra_tickers: Additional tickers to include in concentration check
                       (e.g. the buy side of proposed swaps).

    Returns:
        List of warning dicts, each with keys:
          sector, tickers, weight, level ("WARN" | "ALERT"), message.
        Empty list if no thresholds breached.
    """
    # Build working map, optionally injecting extra tickers
    working: dict[str, list[str]] = {k: list(v) for k, v in sector_map.items()}
    if extra_tickers:
        for t in extra_tickers:
            s = _get_sector(t)
            working.setdefault(s, [])
            if t not in working[s]:
                working[s].append(t)

    total = sum(len(v) for v in working.values())
    if total == 0:
        return []

    warnings: list[dict] = []
    for sector, tickers in sorted(working.items(), key=lambda x: -len(x[1])):
        if sector == "Unknown":
            continue
        weight = len(tickers) / total
        if weight >= _ALERT_THRESHOLD:
            level = "ALERT"
            msg   = (
                f"🔴 *集中風險警告* — `{sector}` 佔投資組合 *{weight:.0%}*（{len(tickers)} 檔）。"
                f"  建議在此板塊完成換倉前，確認整體曝險是否符合風險承受度。"
            )
        elif weight >= _WARN_THRESHOLD:
            level = "WARN"
            msg   = (
                f"⚠️ *分散建議* — `{sector}` 佔投資組合 *{weight:.0%}*（{len(tickers)} 檔）。"
                f"  若新換倉標的仍在同一板塊，集中度將進一步上升。"
            )
        else:
            continue

        warnings.append({
            "sector":  sector,
            "tickers": tickers,
            "weight":  weight,
            "level":   level,
            "message": msg,
        })

    return warnings


def format_concentration_blocks(
    warnings: list[dict],
    title: str = "*🗺️ SECTOR CONCENTRATION CHECK*",
) -> list[dict]:
    """
    Convert concentration warnings into Slack Block Kit section blocks.

    Args:
        warnings: Output of check_concentration().
        title:    Header text for the section.  Callers that display a
                  post-trade simulation should pass a descriptive label so the
                  operator knows the weights reflect the recommended swaps, not
                  the current holdings.

    Returns:
        List of Slack section blocks, or empty list if no warnings.
    """
    if not warnings:
        return []

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": title},
        }
    ]
    for w in warnings:
        ticker_str = "  ".join(f"`{t}`" for t in w["tickers"])
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{w['message']}\n_{ticker_str}_",
            },
        })
    return blocks
