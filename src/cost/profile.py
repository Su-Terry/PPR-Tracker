"""
Alpha Strategist — Cost Profile (V2.0)

Manages per-user transaction cost rates for US (USD) and TW (NTD) markets.
Supports three source tiers:
  "default"         — §6.1 textbook priors (IBKR-like US, Cathay pre-discount TW)
  "manual_override" — set via /cost set command
  "learned"         — V2.1 only; populated from actual_cost statistics

The default file ships committed at data/cost_profile.json. If missing, the
module silently falls back to from_defaults() so the optimizer can always run.

Timestamps follow the same Asia/Taipei +08:00 convention as portfolio/state.py.

JSON key "min" maps to Python field "min_cost" (min is a Python builtin).
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION: int = 1

_TZ_TAIPEI = timezone(timedelta(hours=8))

_VALID_KEYS = frozenset({"us_buy", "us_sell", "tw_buy", "tw_sell"})


class SchemaVersionError(ValueError):
    """Raised when a JSON file's schema version exceeds SUPPORTED_SCHEMA_VERSION."""


# ── Value type ────────────────────────────────────────────────────────────────

@dataclass
class RateEntry:
    """
    A single cost rate for one market/side combination.

    rate:      Fraction of notional charged (e.g. 0.0001 = 0.01%).
    min_cost:  Minimum charge in market native currency (USD for US, NTD for TW).
    source:    "default" | "manual_override" | "learned".
    n_samples: Number of actual_cost observations (V2.1 learning; None until then).
    """
    rate: float
    min_cost: float
    source: Literal["default", "manual_override", "learned"]
    n_samples: int | None = None

    def to_dict(self) -> dict:
        d: dict = {"rate": self.rate, "min": self.min_cost, "source": self.source}
        if self.n_samples is not None:
            d["n_samples"] = self.n_samples
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RateEntry":
        return cls(
            rate=float(d["rate"]),
            min_cost=float(d["min"]),
            source=d.get("source", "default"),
            n_samples=d.get("n_samples"),
        )


# ── §6.1 textbook defaults ────────────────────────────────────────────────────
# US:  IBKR-like — max($0.01, notional × 0.0001)
# TW:  Cathay pre-discount — notional × 0.001425, min NT$20
# TW sell also adds tw_sec_tax (statutory 0.3%, handled in estimate()).

_DEFAULT_US_BUY  = RateEntry(rate=0.0001,   min_cost=0.01,  source="default")
_DEFAULT_US_SELL = RateEntry(rate=0.0001,   min_cost=0.01,  source="default")
_DEFAULT_TW_BUY  = RateEntry(rate=0.001425, min_cost=20.0,  source="default")
_DEFAULT_TW_SELL = RateEntry(rate=0.001425, min_cost=20.0,  source="default")
_DEFAULT_TW_SEC_TAX: float = 0.003     # statutory — never overridden or learned
_DEFAULT_FX_SPREAD_BPS: float = 45.0


# ── Main dataclass ────────────────────────────────────────────────────────────

@dataclass
class CostProfile:
    """
    Per-user transaction cost profile.

    Do not instantiate directly — use load(), from_defaults(), or from_dict().
    """

    version: int
    last_updated: str  # ISO 8601 with +08:00 offset
    us_buy: RateEntry
    us_sell: RateEntry
    tw_buy: RateEntry
    tw_sell: RateEntry
    tw_sec_tax: float           # statutory; always 0.003; never learned
    fx_twd_usd_spread_bps: float

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_defaults(cls) -> "CostProfile":
        """Return §6.1 textbook prior values, source='default' on all entries."""
        return cls(
            version=SUPPORTED_SCHEMA_VERSION,
            last_updated=_now_iso(),
            us_buy=deepcopy(_DEFAULT_US_BUY),
            us_sell=deepcopy(_DEFAULT_US_SELL),
            tw_buy=deepcopy(_DEFAULT_TW_BUY),
            tw_sell=deepcopy(_DEFAULT_TW_SELL),
            tw_sec_tax=_DEFAULT_TW_SEC_TAX,
            fx_twd_usd_spread_bps=_DEFAULT_FX_SPREAD_BPS,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "CostProfile":
        """
        Deserialise from a plain dict (the §6.2 JSON schema).

        Raises:
            SchemaVersionError: version > SUPPORTED_SCHEMA_VERSION.
        """
        version = int(data.get("version", 1))
        if version > SUPPORTED_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"cost_profile.json schema version {version} is not supported "
                f"by this code (max={SUPPORTED_SCHEMA_VERSION})."
            )
        return cls(
            version=version,
            last_updated=data.get("last_updated", _now_iso()),
            us_buy=RateEntry.from_dict(data["us_buy"]),
            us_sell=RateEntry.from_dict(data["us_sell"]),
            tw_buy=RateEntry.from_dict(data["tw_buy"]),
            tw_sell=RateEntry.from_dict(data["tw_sell"]),
            tw_sec_tax=float(data.get("tw_sec_tax", _DEFAULT_TW_SEC_TAX)),
            fx_twd_usd_spread_bps=float(
                data.get("fx_twd_usd_spread_bps", _DEFAULT_FX_SPREAD_BPS)
            ),
        )

    @classmethod
    def load(cls, path: Path) -> "CostProfile":
        """
        Load from a JSON file.

        If the file does not exist, logs a warning and returns from_defaults().

        Raises:
            SchemaVersionError: version mismatch.
            ValueError: malformed JSON.
        """
        if not path.exists():
            logger.warning(
                "[COST] cost_profile.json 不存在（%s），使用 §6.1 預設值", path
            )
            return cls.from_defaults()

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OSError(f"[COST] 無法讀取成本檔：{path}  原因：{exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"[COST] 成本檔 JSON 格式錯誤：{path}  原因：{exc}"
            ) from exc

        profile = cls.from_dict(data)
        logger.info(
            "[COST] 已載入成本 profile：version=%d  us_buy=%.4f%%  tw_buy=%.4f%%",
            profile.version,
            profile.us_buy.rate * 100,
            profile.tw_buy.rate * 100,
        )
        return profile

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialise to §6.2 JSON schema. Python min_cost field → JSON key 'min'."""
        return {
            "version": self.version,
            "last_updated": self.last_updated,
            "us_buy": self.us_buy.to_dict(),
            "us_sell": self.us_sell.to_dict(),
            "tw_buy": self.tw_buy.to_dict(),
            "tw_sell": self.tw_sell.to_dict(),
            "tw_sec_tax": self.tw_sec_tax,
            "fx_twd_usd_spread_bps": self.fx_twd_usd_spread_bps,
        }

    def save(self, path: Path) -> None:
        """Atomically write cost profile to path via tmp → os.replace."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            logger.error("[COST] 無法寫入成本檔：%s  原因：%s", path, exc)
            raise

    # ── Core computation ──────────────────────────────────────────────────────

    def estimate(
        self,
        market: Literal["US", "TW"],
        side: Literal["BUY", "SELL"],
        notional: float,
    ) -> float:
        """
        Estimate one-way transaction cost in market native currency.

        Returns USD for US, NTD for TW. Uses max(rate × notional, min_cost).
        TW SELL automatically adds tw_sec_tax × notional (statutory, not in rate).

        Args:
            market:   "US" or "TW".
            side:     "BUY" or "SELL".
            notional: Position size in market native currency (USD / NTD).

        Raises:
            ValueError: unknown market or side.
        """
        entry = self._get_entry(market, side)
        cost = max(entry.rate * notional, entry.min_cost)
        if market == "TW" and side == "SELL":
            cost += self.tw_sec_tax * notional
        return round(cost, 6)

    # ── Mutations ─────────────────────────────────────────────────────────────

    def manual_set(
        self,
        key: Literal["us_buy", "us_sell", "tw_buy", "tw_sell"],
        rate: float,
        min_cost: float,
        source: str = "manual_override",
    ) -> None:
        """
        Overwrite a rate entry (e.g. from /cost set command).

        Args:
            key:      One of "us_buy", "us_sell", "tw_buy", "tw_sell".
            rate:     New commission rate (fraction of notional).
            min_cost: New minimum charge.
            source:   Provenance tag (default "manual_override").

        Raises:
            ValueError: key not in the valid set.
        """
        if key not in _VALID_KEYS:
            raise ValueError(
                f"[COST] 無效成本率 key：{key!r}  (允許：{sorted(_VALID_KEYS)})"
            )
        new_entry = RateEntry(rate=rate, min_cost=min_cost, source=source)
        setattr(self, key, new_entry)
        self.last_updated = _now_iso()
        logger.info(
            "[COST] 已覆寫成本率：%s  rate=%.4f  min=%.2f  source=%s",
            key, rate, min_cost, source,
        )

    def reset_to_default(
        self,
        key: Literal["us_buy", "us_sell", "tw_buy", "tw_sell"],
    ) -> None:
        """
        Restore one rate entry to the §6.1 textbook default.

        Args:
            key: One of "us_buy", "us_sell", "tw_buy", "tw_sell".

        Raises:
            ValueError: key not in the valid set.
        """
        if key not in _VALID_KEYS:
            raise ValueError(
                f"[COST] 無效成本率 key：{key!r}  (允許：{sorted(_VALID_KEYS)})"
            )
        defaults = {
            "us_buy":  _DEFAULT_US_BUY,
            "us_sell": _DEFAULT_US_SELL,
            "tw_buy":  _DEFAULT_TW_BUY,
            "tw_sell": _DEFAULT_TW_SELL,
        }
        setattr(self, key, deepcopy(defaults[key]))
        self.last_updated = _now_iso()
        logger.info("[COST] 已重設成本率為預設值：%s", key)

    def record_actual(
        self,
        ticker: str,
        market: Literal["US", "TW"],
        side: Literal["BUY", "SELL"],
        notional: float,
        actual_cost: float,
    ) -> None:
        """
        Record an observed actual transaction cost.

        V2.0: logs the data point only; no rate update.
        V2.1 will aggregate 30+ samples and update the "learned" rate entry.

        Args:
            ticker:      Ticker traded.
            market:      "US" or "TW".
            side:        "BUY" or "SELL".
            notional:    Position size in market native currency.
            actual_cost: Actual cost paid in market native currency.
        """
        implied_rate = actual_cost / notional if notional > 0 else 0.0
        logger.info(
            "[COST] actual_cost 記錄（V2.0 僅儲存）：%s %s %s  notional=%.2f"
            "  actual=%.4f  implied_rate=%.4f%%",
            ticker, market, side, notional, actual_cost, implied_rate * 100,
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _get_entry(
        self,
        market: Literal["US", "TW"],
        side: Literal["BUY", "SELL"],
    ) -> RateEntry:
        key = f"{market.lower()}_{side.lower()}"
        if key not in _VALID_KEYS:
            raise ValueError(
                f"[COST] 未知 market/side 組合：market={market!r}  side={side!r}"
            )
        return getattr(self, key)


# ── Private helpers ───────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Return current Asia/Taipei time as ISO 8601 with +08:00 offset."""
    return datetime.now(_TZ_TAIPEI).isoformat(timespec="seconds")
