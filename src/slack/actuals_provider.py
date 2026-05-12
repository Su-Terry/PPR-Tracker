"""
Alpha Strategist — Actuals Provider (V2.0 Sprint 4)

JsonlActualsProvider implements the ActualsProvider Protocol from
src/discipline/metrics.py by reading memory/actual_trades.jsonl.

File schema (one JSON object per line):
  ticker           str          e.g. "NVDA"
  side             "BUY"|"SELL"|"HOLD"
  market           "US"|"TW"
  date             "YYYY-MM-DD"
  system_suggested bool
  quantity         float
  filled_price     float | null
  commission       float
  tax              float
  fx               float
  status           "pending_confirmation"|"reconciled"

Only the first four fields are required by the ActualsProvider Protocol.
Extra fields are stored for portfolio state tracking (Sprint 4/5).
Malformed lines are skipped with a warning log and never raise.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from src.discipline.metrics import ActualTrade

logger = logging.getLogger(__name__)


class JsonlActualsProvider:
    """
    ActualsProvider backed by memory/actual_trades.jsonl.

    Thread-safe for reading (file is opened per call; no shared state).
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def actual_actions(
        self,
        days: int,
        market: Literal["US", "TW", "ALL"] = "ALL",
    ) -> list[ActualTrade]:
        """
        Return ActualTrade records from the last `days` calendar days.

        Parameters
        ----------
        days:
            Lookback window in calendar days (inclusive of today).
        market:
            "US", "TW", or "ALL".

        Returns
        -------
        list[ActualTrade] sorted by date ascending (oldest first).
        """
        if not self._path.exists():
            return []

        cutoff = date.today() - timedelta(days=days - 1)
        results: list[ActualTrade] = []

        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("[ACTUALS] 無法讀取 %s：%s", self._path, exc)
            return []

        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[ACTUALS] 跳過格式錯誤行：%r", raw[:80])
                continue

            try:
                rec_date = date.fromisoformat(rec["date"])
            except (KeyError, ValueError):
                logger.warning("[ACTUALS] 跳過缺少/錯誤 date 欄位的行：%r", raw[:80])
                continue

            if rec_date < cutoff:
                continue

            rec_market = rec.get("market", "")
            if market != "ALL" and rec_market != market:
                continue

            try:
                results.append(
                    ActualTrade(
                        ticker=rec["ticker"],
                        side=rec["side"],
                        market=rec_market,
                        date=rec["date"],
                    )
                )
            except KeyError as exc:
                logger.warning(
                    "[ACTUALS] 跳過缺少必填欄位 %s 的行：%r", exc, raw[:80]
                )

        return sorted(results, key=lambda t: t.date)
