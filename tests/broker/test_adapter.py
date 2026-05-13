"""
Tests for src/broker/adapter.py — BrokerAdapter Protocol + ManualAdapter.
"""

from __future__ import annotations

import pytest

from src.broker.adapter import BrokerAdapter, ManualAdapter, OrderResult
from src.rebalancer.trade_builder import Trade


def _make_trade(
    ticker: str = "NVDA",
    side: str = "BUY",
    market: str = "US",
    quantity: float = 10.0,
    est_price: float = 100.0,
) -> Trade:
    return Trade(
        market=market,
        ticker=ticker,
        side=side,
        delta_weight=0.05,
        target_weight=0.15,
        quantity=quantity,
        est_price=est_price,
        notional=quantity * est_price,
        est_cost=1.00,
        est_cost_breakdown={"commission": 1.0, "tax": 0.0, "fx_spread": 0.0},
        conviction=7.5,
        execution_tier="Execute",
        rationale="score_delta",
        bindings=[],
    )


class TestManualAdapter:
    def test_place_order_returns_manual_pending(self):
        adapter = ManualAdapter()
        result = adapter.place_order(_make_trade())
        assert result.status == "manual_pending"

    def test_place_order_has_message(self):
        adapter = ManualAdapter()
        result = adapter.place_order(_make_trade(ticker="AAPL", side="SELL"))
        assert "SELL" in result.message
        assert "AAPL" in result.message

    def test_place_order_no_order_id(self):
        adapter = ManualAdapter()
        result = adapter.place_order(_make_trade())
        assert result.order_id is None

    def test_buy_trade(self):
        adapter = ManualAdapter()
        result = adapter.place_order(_make_trade(side="BUY"))
        assert result.status == "manual_pending"

    def test_sell_trade(self):
        adapter = ManualAdapter()
        result = adapter.place_order(_make_trade(side="SELL"))
        assert result.status == "manual_pending"

    def test_tw_market_trade(self):
        adapter = ManualAdapter()
        result = adapter.place_order(_make_trade(market="TW", ticker="2330.TW"))
        assert result.status == "manual_pending"

    def test_manual_adapter_satisfies_protocol(self):
        adapter = ManualAdapter()
        assert isinstance(adapter, BrokerAdapter)

    def test_order_result_fields(self):
        result = OrderResult(status="manual_pending", message="test")
        assert result.status == "manual_pending"
        assert result.order_id is None
        assert result.message == "test"

    def test_order_result_with_order_id(self):
        result = OrderResult(status="submitted", order_id="ORD-123")
        assert result.order_id == "ORD-123"
