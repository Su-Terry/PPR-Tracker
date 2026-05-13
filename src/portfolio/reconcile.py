"""
Alpha Strategist — CSV Reconciliation (V2.0 Sprint 5)

Implements spec §7.5 Layer 4: Weekly CSV Upload reconciliation.
Compares a broker-exported CSV against PortfolioState and returns a
ReconcileReport listing all discrepancies.

The report drives the /reconcile upload [us|tw] Slack command implemented
in slack_bot.py Sprint 5. The caller is responsible for presenting the
report to the operator and deciding whether to apply corrections via
state.edit_holding() (emergency override path per the portfolio state
mutation rules in CLAUDE.md).
"""

from __future__ import annotations

import csv as csv_mod
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.portfolio.state import PortfolioState

logger = logging.getLogger(__name__)


# Column names for each market's broker CSV export (Cathay format)
_US_TICKER_COL = "代號"
_US_QTY_COL    = "目前庫存"
_TW_TICKER_COL = "股票名稱"
_TW_QTY_COL    = "股數"


def _parse_broker_csv(
    csv_path: Path,
    market: Literal["US", "TW"],
) -> dict[str, float]:
    """
    Parse a Cathay broker CSV and return {ticker: qty} without mutating state.

    Raises:
        FileNotFoundError: If csv_path does not exist.
        OSError:           If the file cannot be opened.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV 檔案不存在: {csv_path}")

    ticker_col = _US_TICKER_COL if market == "US" else _TW_TICKER_COL
    qty_col    = _US_QTY_COL    if market == "US" else _TW_QTY_COL

    holdings: dict[str, float] = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv_mod.DictReader(f)
        for row in reader:
            ticker  = (row.get(ticker_col) or "").strip()
            qty_str = (row.get(qty_col) or "").strip()
            if not ticker or not qty_str:
                continue
            try:
                qty = float(qty_str.replace(",", ""))
                if qty > 0:
                    holdings[ticker] = qty
            except ValueError:
                logger.warning("[RECONCILE] 無法解析數量: ticker=%r  qty_str=%r", ticker, qty_str)

    return holdings


@dataclass
class ReconcileReport:
    """
    Result of comparing a broker CSV export against the local PortfolioState.

    Attributes:
        market:          Which sub-portfolio was reconciled ("US" or "TW").
        mismatches:      Tickers where CSV quantity differs from state quantity.
                         Each dict has keys: ticker, state_qty, csv_qty, delta.
        unknown_tickers: Tickers present in the CSV but not in local state.
                         Likely dividends re-invested as new positions, IPO
                         issuances, or split-adjusted entries.
        missing_tickers: Tickers present in local state but absent from the CSV.
                         May indicate a fully-closed position not yet reconciled.
        cash_variance:   Estimated cash discrepancy in market currency (USD for US,
                         NTD for TW). None when the CSV contains no cash row.
        warnings:        Non-fatal messages from the CSV parser (e.g. unmapped
                         column names, skipped rows).
    """

    market: Literal["US", "TW"]
    mismatches: list[dict] = field(default_factory=list)
    unknown_tickers: list[str] = field(default_factory=list)
    missing_tickers: list[str] = field(default_factory=list)
    cash_variance: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def has_discrepancies(self) -> bool:
        """True when any actionable discrepancy was found."""
        return bool(self.mismatches or self.unknown_tickers or self.missing_tickers)

    def to_slack_text(self) -> str:
        """
        Format the report as a Slack mrkdwn string for the /reconcile command.

        Returns:
            Human-readable summary suitable for chat_postMessage(text=...).
        """
        flag = "🇺🇸" if self.market == "US" else "🇹🇼"
        lines = [f"{flag} *Reconcile Report — {self.market}*"]

        if not self.has_discrepancies:
            lines.append("✅ 無差異 — 持倉與券商 CSV 完全吻合。")
        else:
            if self.mismatches:
                lines.append(f"\n*數量差異* ({len(self.mismatches)} 筆):")
                for m in self.mismatches:
                    lines.append(
                        f"  • `{m['ticker']}` 系統={m['state_qty']:.4f}  "
                        f"券商={m['csv_qty']:.4f}  差異={m['delta']:+.4f}"
                    )
            if self.unknown_tickers:
                lines.append(f"\n*未知標的* (CSV 有、系統無) ({len(self.unknown_tickers)} 筆):")
                for t in self.unknown_tickers:
                    lines.append(f"  • `{t}` — 請確認來源（股利再投資/IPO/分割？）")
            if self.missing_tickers:
                lines.append(f"\n*缺少標的* (系統有、CSV 無) ({len(self.missing_tickers)} 筆):")
                for t in self.missing_tickers:
                    lines.append(f"  • `{t}` — 已全數出清但尚未在系統登記？")

        if self.cash_variance is not None:
            ccy = "USD" if self.market == "US" else "NTD"
            lines.append(
                f"\n現金差異: {self.cash_variance:+.2f} {ccy}"
                + (" ⚠️" if abs(self.cash_variance) > 5.0 else "")
            )

        if self.warnings:
            lines.append(f"\n_解析警告 ({len(self.warnings)} 條):_")
            for w in self.warnings[:5]:
                lines.append(f"  _{w}_")
            if len(self.warnings) > 5:
                lines.append(f"  _...共 {len(self.warnings)} 條，詳見 log。_")

        return "\n".join(lines)


def reconcile_from_csv(
    csv_path: Path,
    market: Literal["US", "TW"],
    state: PortfolioState,
) -> ReconcileReport:
    """
    Compare a broker-exported CSV against the local PortfolioState.

    Delegates CSV parsing to state.sync_holdings_from_csv() (which handles
    Cathay column aliases and TW ticker map) in dry-run fashion — it does NOT
    persist any changes to state.

    Spec §7.5 discrepancy handling:
      - Holdings quantity mismatch → listed in mismatches
      - Unknown ticker (in CSV, not in state) → unknown_tickers
      - Missing ticker (in state, not in CSV) → missing_tickers
      - Cash variance → cash_variance (informational only in Sprint 5)

    Args:
        csv_path: Path to the broker-exported CSV file.
        market:   Which sub-portfolio to reconcile ("US" or "TW").
        state:    Current PortfolioState loaded by the caller.

    Returns:
        ReconcileReport with all discrepancies populated.

    Raises:
        OSError:    If csv_path does not exist or cannot be read.
        ValueError: If the CSV cannot be parsed (empty, no recognised columns).
    """
    # ── Parse broker CSV (raises OSError if file missing) ─────────────────────
    csv_holdings: dict[str, float] = _parse_broker_csv(csv_path, market)

    # ── Get current state holdings (not mutated) ───────────────────────────────
    state_holdings: dict[str, float] = (
        state.us_holdings if market == "US" else state.tw_holdings
    )

    state_tickers = set(state_holdings.keys())
    csv_tickers   = set(csv_holdings.keys())

    # Tickers in CSV but not in current state
    unknown = sorted(csv_tickers - state_tickers)

    # Tickers in current state but absent from CSV
    missing = sorted(state_tickers - csv_tickers)

    # Tickers in both — compare quantities
    mismatches: list[dict] = []
    for ticker in sorted(state_tickers & csv_tickers):
        state_qty = state_holdings[ticker]
        csv_qty   = csv_holdings[ticker]
        delta     = csv_qty - state_qty
        if abs(delta) > 1e-6:
            mismatches.append({
                "ticker":    ticker,
                "state_qty": state_qty,
                "csv_qty":   csv_qty,
                "delta":     delta,
            })

    logger.info(
        "[RECONCILE] %s — mismatches=%d  unknown=%d  missing=%d",
        market, len(mismatches), len(unknown), len(missing),
    )

    return ReconcileReport(
        market=market,
        mismatches=mismatches,
        unknown_tickers=unknown,
        missing_tickers=missing,
        cash_variance=None,
        warnings=[],
    )
