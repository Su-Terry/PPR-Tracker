"""
Alpha Strategist — Unified live price source (Sprint 5.5).

All live price fetches must go through this module.
Background: yfinance fast_info is lazy-cached and returns stale values
in pre/post-market hours (deploy-day bugs: CRWV $107.75 vs $110.61,
NVDA implied $1462 vs ~$130). Using .info["regularMarketPrice"] is
authoritative and consistent regardless of session timing.

Thread safety: do NOT call logging.getLogger("yfinance").setLevel() inside
these methods. APScheduler BackgroundScheduler uses ThreadPoolExecutor and
concurrent jobs would race on the shared logger singleton. Suppress yfinance
logs at app startup via main.py instead (done in Sprint 5.5).

TODO(V2.1): Add an in-process 5-min TTL cache if yfinance rate-limit
(HTTPError 429) is observed in production. Intraday scan runs every 15 min
× ~10 tickers ≈ 960 .info calls/day, which should be within yfinance's
unofficial tolerance, but monitor first 24 h post-deploy.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import yfinance as yf


@runtime_checkable
class PriceProvider(Protocol):
    """Injectable price source — implement to swap in a backtest provider."""

    def get_current_price(self, ticker: str) -> float | None:
        """Return live market price, or None if unavailable."""
        ...

    def get_previous_close(self, ticker: str) -> float | None:
        """Return previous session close, or None if unavailable."""
        ...


class LivePriceProvider:
    """
    Concrete provider using yf.Ticker(ticker).info.

    Field priority:
      current price  : regularMarketPrice → currentPrice
      previous close : previousClose

    Callers must normalise the ticker (e.g. append .TW / .TWO) before
    passing it — this class does not handle exchange suffix logic.
    """

    def get_current_price(self, ticker: str) -> float | None:
        """Return live market price; None if yfinance cannot supply it."""
        try:
            info = yf.Ticker(ticker).info
            price = info.get("regularMarketPrice") or info.get("currentPrice")
            return float(price) if price else None
        except Exception:
            return None

    def get_previous_close(self, ticker: str) -> float | None:
        """Return previous session close; None if yfinance cannot supply it."""
        try:
            info = yf.Ticker(ticker).info
            prev = info.get("previousClose")
            return float(prev) if prev else None
        except Exception:
            return None
