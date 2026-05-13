"""
Tests for src/data/price_provider.py — LivePriceProvider.

All tests mock yfinance.Ticker to avoid live network calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.data.price_provider import LivePriceProvider, PriceProvider


@pytest.fixture()
def provider() -> LivePriceProvider:
    return LivePriceProvider()


class TestLivePriceProvider:
    def test_satisfies_protocol(self, provider: LivePriceProvider) -> None:
        assert isinstance(provider, PriceProvider)

    # ── get_current_price ─────────────────────────────────────────────────────

    def test_get_current_price_uses_regular_market_price(
        self, provider: LivePriceProvider
    ) -> None:
        with patch("yfinance.Ticker") as mock_cls:
            mock_cls.return_value.info = {"regularMarketPrice": 110.61}
            result = provider.get_current_price("CRWV")
        assert result == pytest.approx(110.61)

    def test_get_current_price_falls_back_to_current_price(
        self, provider: LivePriceProvider
    ) -> None:
        with patch("yfinance.Ticker") as mock_cls:
            mock_cls.return_value.info = {
                "regularMarketPrice": None,
                "currentPrice": 130.0,
            }
            result = provider.get_current_price("NVDA")
        assert result == pytest.approx(130.0)

    def test_get_current_price_returns_none_when_both_missing(
        self, provider: LivePriceProvider
    ) -> None:
        with patch("yfinance.Ticker") as mock_cls:
            mock_cls.return_value.info = {}
            result = provider.get_current_price("UNKNOWN")
        assert result is None

    def test_get_current_price_returns_none_on_exception(
        self, provider: LivePriceProvider
    ) -> None:
        with patch("yfinance.Ticker") as mock_cls:
            type(mock_cls.return_value).info = PropertyMock(
                side_effect=Exception("network error")
            )
            result = provider.get_current_price("CRWV")
        assert result is None

    def test_get_current_price_returns_none_for_zero_price(
        self, provider: LivePriceProvider
    ) -> None:
        with patch("yfinance.Ticker") as mock_cls:
            mock_cls.return_value.info = {"regularMarketPrice": 0}
            result = provider.get_current_price("ZERO")
        assert result is None

    # ── get_previous_close ────────────────────────────────────────────────────

    def test_get_previous_close_uses_previous_close(
        self, provider: LivePriceProvider
    ) -> None:
        with patch("yfinance.Ticker") as mock_cls:
            mock_cls.return_value.info = {"previousClose": 107.75}
            result = provider.get_previous_close("CRWV")
        assert result == pytest.approx(107.75)

    def test_get_previous_close_returns_none_when_missing(
        self, provider: LivePriceProvider
    ) -> None:
        with patch("yfinance.Ticker") as mock_cls:
            mock_cls.return_value.info = {}
            result = provider.get_previous_close("UNKNOWN")
        assert result is None

    def test_get_previous_close_returns_none_on_exception(
        self, provider: LivePriceProvider
    ) -> None:
        with patch("yfinance.Ticker") as mock_cls:
            type(mock_cls.return_value).info = PropertyMock(
                side_effect=Exception("timeout")
            )
            result = provider.get_previous_close("CRWV")
        assert result is None

    # ── no internal logger suppression (thread-safety guarantee) ─────────────

    def test_does_not_call_getlogger_yfinance(
        self, provider: LivePriceProvider
    ) -> None:
        """LivePriceProvider must not mutate shared yfinance logger level."""
        with patch("yfinance.Ticker") as mock_cls:
            mock_cls.return_value.info = {"regularMarketPrice": 110.61}
            with patch("logging.getLogger") as mock_get_logger:
                provider.get_current_price("CRWV")
                provider.get_previous_close("CRWV")
        # getLogger("yfinance") must never be called from inside the provider
        for call in mock_get_logger.call_args_list:
            assert call.args != ("yfinance",), (
                "LivePriceProvider must not call getLogger('yfinance') — "
                "use main.py startup suppression instead"
            )
