"""
Alpha Strategist — Broker Adapter (V2.0 Sprint 5)

Defines the BrokerAdapter Protocol and ManualAdapter per spec §9.3.

The Approve handler calls adapter.place_order() for each Execute-tier trade.
ManualAdapter (Sprint 5 default) is a no-op: the user executes the trade
manually in their broker app and reconciles via /trade add or /reconcile upload.

Future adapters (FubonAdapter — V2.1+) implement the same Protocol without
requiring any caller changes.

CRITICAL: No adapter may trigger real orders automatically.
         Human-in-the-Loop is an absolute system invariant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from src.rebalancer.trade_builder import Trade

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """Result returned by BrokerAdapter.place_order()."""

    status: Literal["manual_pending", "submitted", "filled", "rejected"]
    order_id: str | None = None
    message: str = ""


@runtime_checkable
class BrokerAdapter(Protocol):
    """
    Protocol for all broker integrations.

    Implementations must be stateless across calls. Each place_order() call
    is independent and idempotent from the caller's perspective.
    """

    def place_order(self, trade: Trade) -> OrderResult:
        """
        Submit one trade to the broker.

        Args:
            trade: Fully-specified trade to execute (BUY or SELL).

        Returns:
            OrderResult with status and optional broker order ID.
        """
        ...


class ManualAdapter:
    """
    Sprint 5 default broker adapter.

    place_order() is a no-op that logs the intended trade and returns
    manual_pending. The user executes the trade manually in their broker
    app. Post-execution reconciliation happens via /trade add or
    /reconcile upload.

    Swap for FubonAdapter (V2.1) without changing any caller.
    """

    def place_order(self, trade: Trade) -> OrderResult:
        """
        Log the trade instruction and return manual_pending.

        Args:
            trade: Trade to be executed manually by the operator.

        Returns:
            OrderResult(status="manual_pending").
        """
        logger.info(
            "[BROKER] manual_pending — %s %s  qty=%.4f  est_price=%.4f  market=%s",
            trade.side,
            trade.ticker,
            trade.quantity,
            trade.est_price,
            trade.market,
        )
        return OrderResult(
            status="manual_pending",
            message=f"人工執行：{trade.side} {trade.ticker} {trade.quantity:.4f} 股",
        )
