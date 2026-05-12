"""
Alpha Strategist — Portfolio State (V2.0)

JSON-backed single source of truth for cash, holdings, IPO subscriptions,
and lockup positions. Covers both the US (USD) and TW (NTD) sub-portfolios
independently, as required by spec §3.1 and §7.1.

Timestamps are stored as ISO 8601 strings with a fixed +08:00 (Asia/Taipei)
offset. This matches the spec §7.1 examples and avoids pytz as a dependency.

State file layout (data/portfolio_state.json) is defined in spec §7.1.
The file is not committed to the repo — create it via PortfolioState.create_empty().

Atomic writes use os.replace() so a crash mid-write never corrupts the live file.
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION: int = 1

_TZ_TAIPEI = timezone(timedelta(hours=8))

# ── Column aliases for CSV parsing (Cathay broker formats) ────────────────────
# Mirrors portfolio_gateway._FOREIGN_ALIASES / _TW_ALIASES but self-contained
# to avoid importing from the MCP server module (which has side effects at
# import time: mcp = FastMCP(...)).
_FOREIGN_COL_ALIASES: dict[str, list[str]] = {
    "Ticker": ["代號", "股票代號", "商品代號", "證券代號", "Ticker"],
    "Shares": ["目前庫存", "持倉股數", "庫存股數", "股數", "Shares"],
}

_TW_COL_ALIASES: dict[str, list[str]] = {
    "Ticker": ["股票名稱", "證券名稱", "名稱", "Ticker"],
    "Shares": ["股數", "庫存股數", "持倉股數", "目前庫存", "Shares"],
}

_TW_VALID_CURRENCIES: set[str] = {"台幣", "美元", "港幣"}


class SchemaVersionError(ValueError):
    """Raised when a JSON file's schema version exceeds SUPPORTED_SCHEMA_VERSION."""


# ── Nested value types ────────────────────────────────────────────────────────

@dataclass
class IpoSubscription:
    """
    A pending IPO cash reservation on the TW side.

    amount_twd is deducted from cash_twd at subscription time and returned
    (via update_cash) only if outcome="refunded".
    """
    ticker: str
    amount_twd: float
    subscribed_date: str  # ISO date YYYY-MM-DD
    release_date: str     # ISO date YYYY-MM-DD
    kind: str = "subscription"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "IpoSubscription":
        return cls(
            ticker=d["ticker"],
            amount_twd=float(d["amount_twd"]),
            subscribed_date=d["subscribed_date"],
            release_date=d["release_date"],
            kind=d.get("kind", "subscription"),
        )


# ── Main dataclass ────────────────────────────────────────────────────────────

@dataclass
class PortfolioState:
    """
    Complete portfolio state for both US (USD) and TW (NTD) sub-portfolios.

    Do not instantiate directly — use load(), create_empty(), or from_dict().
    All mutation methods update last_updated automatically.
    """

    version: int
    last_updated: str  # ISO 8601 with +08:00 offset

    # US sub-portfolio
    us_cash_usd: float
    us_holdings: dict[str, float]  # ticker → quantity (fractional allowed)

    # TW sub-portfolio
    tw_cash_twd: float
    tw_holdings: dict[str, float]  # ticker → quantity
    tw_pending_ipo_subscription_twd: float
    tw_pending_ipo_details: list[IpoSubscription]
    tw_ipo_lockup_holdings: list[str]

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def create_empty(cls) -> "PortfolioState":
        """Return a zero-cash, empty-holdings state at schema version 1."""
        return cls(
            version=SUPPORTED_SCHEMA_VERSION,
            last_updated=_now_iso(),
            us_cash_usd=0.0,
            us_holdings={},
            tw_cash_twd=0.0,
            tw_holdings={},
            tw_pending_ipo_subscription_twd=0.0,
            tw_pending_ipo_details=[],
            tw_ipo_lockup_holdings=[],
        )

    @classmethod
    def from_dict(cls, data: dict) -> "PortfolioState":
        """
        Deserialise from a plain dict (the §7.1 JSON schema).

        Raises:
            SchemaVersionError: if data["version"] > SUPPORTED_SCHEMA_VERSION.
            ValueError: if required top-level keys are missing.
        """
        version = int(data.get("version", 1))
        if version > SUPPORTED_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"portfolio_state.json schema version {version} is not supported "
                f"by this code (max={SUPPORTED_SCHEMA_VERSION}). "
                "Upgrade the src/portfolio/state.py module."
            )

        us = data.get("us", {})
        tw = data.get("tw", {})

        return cls(
            version=version,
            last_updated=data.get("last_updated", _now_iso()),
            us_cash_usd=float(us.get("cash_usd", 0.0)),
            us_holdings={k: float(v) for k, v in us.get("holdings", {}).items()},
            tw_cash_twd=float(tw.get("cash_twd", 0.0)),
            tw_holdings={k: float(v) for k, v in tw.get("holdings", {}).items()},
            tw_pending_ipo_subscription_twd=float(
                tw.get("pending_ipo_subscription_twd", 0.0)
            ),
            tw_pending_ipo_details=[
                IpoSubscription.from_dict(d)
                for d in tw.get("pending_ipo_details", [])
            ],
            tw_ipo_lockup_holdings=list(tw.get("ipo_lockup_holdings", [])),
        )

    @classmethod
    def load(cls, path: Path) -> "PortfolioState":
        """
        Load state from a JSON file.

        Raises:
            SchemaVersionError: version mismatch.
            ValueError: malformed JSON or missing required structure.
            OSError: file not readable.
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OSError(f"[PORTFOLIO] 無法讀取狀態檔：{path}  原因：{exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"[PORTFOLIO] 狀態檔 JSON 格式錯誤：{path}  原因：{exc}"
            ) from exc

        state = cls.from_dict(data)
        logger.info(
            "[PORTFOLIO] 已載入狀態：version=%d  us_holdings=%d  tw_holdings=%d",
            state.version,
            len(state.us_holdings),
            len(state.tw_holdings),
        )
        return state

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialise to the §7.1 JSON schema structure."""
        return {
            "version": self.version,
            "last_updated": self.last_updated,
            "us": {
                "cash_usd": self.us_cash_usd,
                "holdings": dict(self.us_holdings),
            },
            "tw": {
                "cash_twd": self.tw_cash_twd,
                "holdings": dict(self.tw_holdings),
                "pending_ipo_subscription_twd": self.tw_pending_ipo_subscription_twd,
                "pending_ipo_details": [d.to_dict() for d in self.tw_pending_ipo_details],
                "ipo_lockup_holdings": list(self.tw_ipo_lockup_holdings),
            },
        }

    def save(self, path: Path) -> None:
        """
        Atomically write state to path.

        Writes to a .tmp file first, then os.replace() so a crash mid-write
        never leaves a corrupt state file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            logger.error("[PORTFOLIO] 無法寫入狀態檔：%s  原因：%s", path, exc)
            raise

    # ── Queries ───────────────────────────────────────────────────────────────

    def market_snapshot(self, market: Literal["US", "TW"]) -> dict:
        """
        Return a read-only snapshot dict for one market.

        US returns: {cash_usd, holdings}
        TW returns: {cash_twd, holdings, pending_ipo_subscription_twd,
                     pending_ipo_details, ipo_lockup_holdings}
        """
        if market == "US":
            return {
                "cash_usd": self.us_cash_usd,
                "holdings": dict(self.us_holdings),
            }
        if market == "TW":
            return {
                "cash_twd": self.tw_cash_twd,
                "holdings": dict(self.tw_holdings),
                "pending_ipo_subscription_twd": self.tw_pending_ipo_subscription_twd,
                "pending_ipo_details": [deepcopy(d) for d in self.tw_pending_ipo_details],
                "ipo_lockup_holdings": list(self.tw_ipo_lockup_holdings),
            }
        raise ValueError(f"[PORTFOLIO] 未知市場代碼：{market!r}  (只接受 'US' 或 'TW')")

    # ── Mutations (all touch last_updated) ───────────────────────────────────

    def update_cash(
        self,
        market: Literal["US", "TW"],
        amount: float,
        reason: str = "",
    ) -> None:
        """
        Add `amount` to cash for the given market (negative = deduction).

        Args:
            market: "US" (USD) or "TW" (NTD).
            amount: Signed delta. Positive = deposit, negative = withdrawal.
            reason: Optional annotation for the log.

        Raises:
            ValueError: Unknown market.
        """
        if market == "US":
            self.us_cash_usd += amount
        elif market == "TW":
            self.tw_cash_twd += amount
        else:
            raise ValueError(f"[PORTFOLIO] 未知市場代碼：{market!r}")
        self.last_updated = _now_iso()
        logger.info(
            "[PORTFOLIO] 現金調整 %s  Δ=%.2f  reason=%s",
            market, amount, reason or "(無)",
        )

    def add_ipo_subscription(
        self,
        ticker: str,
        amount_twd: float,
        release_date: str,
    ) -> None:
        """
        Reserve TW cash for an IPO subscription.

        Cash is deducted from tw_cash_twd immediately.
        pending_ipo_subscription_twd is updated to track the total earmarked.

        Args:
            ticker:       Ticker of the IPO (e.g. "6488.TW").
            amount_twd:   NT dollars to reserve.
            release_date: Expected allotment / refund date (YYYY-MM-DD).

        Raises:
            ValueError: ticker already has a pending subscription.
        """
        existing = [s.ticker for s in self.tw_pending_ipo_details]
        if ticker in existing:
            raise ValueError(
                f"[PORTFOLIO] {ticker} 已有待審 IPO 申購，請先 release_ipo 再重新申購"
            )
        sub = IpoSubscription(
            ticker=ticker,
            amount_twd=amount_twd,
            subscribed_date=_today_iso(),
            release_date=release_date,
        )
        self.tw_pending_ipo_details.append(sub)
        self.tw_pending_ipo_subscription_twd += amount_twd
        self.tw_cash_twd -= amount_twd
        self.last_updated = _now_iso()
        logger.info(
            "[PORTFOLIO] IPO 申購登錄：%s  金額=NT$%.0f  撥券日=%s",
            ticker, amount_twd, release_date,
        )

    def release_ipo(
        self,
        ticker: str,
        outcome: Literal["awarded", "refunded"],
    ) -> None:
        """
        Close out a pending IPO subscription.

        - "awarded": ticker moves to ipo_lockup_holdings; cash unchanged
          (already deducted at subscription, now a real position).
        - "refunded": subscription removed; cash returned to tw_cash_twd.

        Args:
            ticker:  Ticker being released.
            outcome: "awarded" or "refunded".

        Raises:
            ValueError: ticker not in pending_ipo_details, or invalid outcome.
        """
        if outcome not in ("awarded", "refunded"):
            raise ValueError(
                f"[PORTFOLIO] 無效 outcome：{outcome!r}  (只接受 'awarded' 或 'refunded')"
            )

        match = next(
            (s for s in self.tw_pending_ipo_details if s.ticker == ticker), None
        )
        if match is None:
            raise ValueError(
                f"[PORTFOLIO] {ticker} 不在待審 IPO 名單中，無法 release"
            )

        self.tw_pending_ipo_details = [
            s for s in self.tw_pending_ipo_details if s.ticker != ticker
        ]
        self.tw_pending_ipo_subscription_twd -= match.amount_twd

        if outcome == "awarded":
            self.tw_ipo_lockup_holdings.append(ticker)
            logger.info(
                "[PORTFOLIO] IPO 撥券成功：%s → ipo_lockup_holdings", ticker
            )
        else:  # refunded
            self.tw_cash_twd += match.amount_twd
            logger.info(
                "[PORTFOLIO] IPO 退款：%s  退回=NT$%.0f", ticker, match.amount_twd
            )

        self.last_updated = _now_iso()

    def apply_trade(
        self,
        market: Literal["US", "TW"],
        ticker: str,
        side: Literal["BUY", "SELL"],
        quantity: float,
        cash_delta: float,
    ) -> None:
        """
        Record an executed trade: update holdings and cash atomically.

        Use for all normal execution paths (e.g., Approve button, /trade add).
        Unlike edit_holding(), this does NOT log a WARNING and does NOT count
        toward the emergency-override monitoring metric (spec §10.4).

        cash_delta is caller-computed:
            BUY:  -(quantity * filled_price + commission + tax)
            SELL: +(quantity * filled_price - commission - tax)

        Args:
            market:     "US" or "TW".
            ticker:     Ticker symbol (e.g. "NVDA", "2330.TW").
            side:       "BUY" or "SELL".
            quantity:   Absolute share count (positive).
            cash_delta: Signed cash change in market currency.

        Raises:
            ValueError: quantity <= 0 or unknown market.
        """
        if quantity <= 0:
            raise ValueError(
                f"[PORTFOLIO] apply_trade 拒絕非正數量：{ticker}  quantity={quantity}"
            )
        if market == "US":
            holdings = self.us_holdings
        elif market == "TW":
            holdings = self.tw_holdings
        else:
            raise ValueError(f"[PORTFOLIO] 未知市場代碼：{market!r}")

        current_qty = holdings.get(ticker, 0.0)
        new_qty = current_qty + quantity if side == "BUY" else current_qty - quantity

        if new_qty <= 0:
            holdings.pop(ticker, None)
        else:
            holdings[ticker] = new_qty

        if market == "US":
            self.us_cash_usd += cash_delta
        else:
            self.tw_cash_twd += cash_delta

        self.last_updated = _now_iso()
        logger.info(
            "[PORTFOLIO] 成交記錄：%s %s %s  qty=%.4f  cash_delta=%.2f",
            side, ticker, market, quantity, cash_delta,
        )

    def sync_holdings_from_csv(
        self,
        csv_path: Path,
        market: Literal["US", "TW"],
        tw_ticker_map: dict[str, str] | None = None,
    ) -> list[str]:
        """
        Parse a Cathay broker CSV and replace holdings for the given market.

        US format (複委託庫存*): ticker in "代號" column, shares in "目前庫存".
        TW format (證券未實現彙總*): name in "股票名稱" column, mapped to ticker
        via tw_ticker_map.

        The holdings dict for the market is fully replaced (not merged) so
        that positions closed at the broker are removed from state.

        Args:
            csv_path:      Path to the Cathay CSV file.
            market:        "US" or "TW".
            tw_ticker_map: name→ticker dict for TW. If None and market="TW",
                           attempts to load data/tw_ticker_map.json relative
                           to this module's repo root.

        Returns:
            List of warning strings (unmapped tickers, parse issues, etc.).
        """
        warnings: list[str] = []
        aliases = _FOREIGN_COL_ALIASES if market == "US" else _TW_COL_ALIASES

        holdings, parse_warnings = _parse_csv(csv_path, aliases)
        warnings.extend(parse_warnings)

        if not holdings:
            logger.warning(
                "[PORTFOLIO] sync_holdings_from_csv：%s 解析後無有效持倉", csv_path.name
            )
            return warnings

        # TW: map stock names → tickers
        if market == "TW":
            if tw_ticker_map is None:
                tw_ticker_map = _load_tw_ticker_map(
                    Path(__file__).parent.parent.parent / "data" / "tw_ticker_map.json"
                )
            mapped: dict[str, float] = {}
            for name, qty in holdings.items():
                ticker = tw_ticker_map.get(name, name)
                if ticker == name and not _looks_like_ticker(name):
                    warnings.append(
                        f"tw_ticker_map.json 缺少對應：{name!r}，以原名儲存"
                    )
                mapped[ticker] = qty
            holdings = mapped

        if market == "US":
            self.us_holdings = holdings
        else:
            self.tw_holdings = holdings

        self.last_updated = _now_iso()
        logger.info(
            "[PORTFOLIO] sync_holdings_from_csv 完成：%s  %s  %d 筆持倉",
            market, csv_path.name, len(holdings),
        )
        return warnings

    def edit_holding(
        self,
        ticker: str,
        qty: float,
        reason: str,
    ) -> None:
        """
        Emergency override for a single holding.

        qty=0 removes the ticker from holdings.
        This operation is logged at WARNING level so the weekly report can
        surface all manual overrides.

        Args:
            ticker: Ticker to edit (e.g. "NVDA", "2330.TW").
            qty:    New quantity. 0 = remove. Must be ≥ 0.
            reason: Mandatory explanation (e.g. "stock split", "correction").

        Raises:
            ValueError: qty < 0.
        """
        if qty < 0:
            raise ValueError(
                f"[PORTFOLIO] edit_holding 拒絕負數量：{ticker}  qty={qty}"
            )

        market = "TW" if ticker.endswith((".TW", ".TWO")) else "US"
        holdings = self.tw_holdings if market == "TW" else self.us_holdings

        if qty == 0:
            holdings.pop(ticker, None)
            logger.warning(
                "[PORTFOLIO] 緊急覆寫（移除）：%s  reason=%s", ticker, reason
            )
        else:
            holdings[ticker] = qty
            logger.warning(
                "[PORTFOLIO] 緊急覆寫 holdings：%s  qty=%.4f  reason=%s",
                ticker, qty, reason,
            )

        self.last_updated = _now_iso()


# ── Private helpers ───────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Return current Asia/Taipei time as ISO 8601 with +08:00 offset."""
    return datetime.now(_TZ_TAIPEI).isoformat(timespec="seconds")


def _today_iso() -> str:
    """Return today's date in Asia/Taipei as YYYY-MM-DD."""
    return datetime.now(_TZ_TAIPEI).date().isoformat()


def _resolve_col(columns: list[str], aliases: list[str]) -> str | None:
    """Return the first alias that appears in columns, or None."""
    for alias in aliases:
        if alias in columns:
            return alias
    return None


def _clean_numeric(value: str) -> float | None:
    """Strip thousands separators, quotes, %, then parse as float."""
    cleaned = (
        str(value).strip().strip('"')
        .replace(",", "")
        .replace("%", "")
    )
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_csv(
    path: Path,
    aliases: dict[str, list[str]],
) -> tuple[dict[str, float], list[str]]:
    """
    Parse a Cathay CSV into {ticker: quantity} dict.

    Returns (holdings_dict, warnings_list). On hard failure returns ({}, warnings).
    """
    warnings: list[str] = []
    try:
        import csv as _csv
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = _csv.DictReader(fh)
            if reader.fieldnames is None:
                return {}, [f"{path.name} 為空檔案或無標頭列"]
            cols = list(reader.fieldnames)
            ticker_col = _resolve_col(cols, aliases["Ticker"])
            shares_col = _resolve_col(cols, aliases["Shares"])
            if ticker_col is None or shares_col is None:
                missing = []
                if ticker_col is None:
                    missing.append("Ticker")
                if shares_col is None:
                    missing.append("Shares")
                return {}, [
                    f"{path.name} 找不到欄位 {missing}（現有欄位：{cols}）"
                ]
            holdings: dict[str, float] = {}
            for row in reader:
                raw_ticker = str(row.get(ticker_col, "")).strip()
                raw_shares = row.get(shares_col, "")
                if not raw_ticker:
                    continue
                qty = _clean_numeric(raw_shares)
                if qty is None or qty <= 0:
                    continue
                holdings[raw_ticker] = qty
    except OSError as exc:
        return {}, [f"讀取 {path.name} 失敗：{exc}"]
    return holdings, warnings


def _load_tw_ticker_map(map_path: Path) -> dict[str, str]:
    """Load name→ticker JSON; return empty dict on missing or corrupt file."""
    if not map_path.exists():
        return {}
    try:
        raw = json.loads(map_path.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_") and v}
    except Exception as exc:
        logger.warning("[PORTFOLIO] tw_ticker_map.json 讀取失敗：%s", exc)
        return {}


def _looks_like_ticker(name: str) -> bool:
    """Heuristic: pure digits 4-6 chars = TW ticker without .TW suffix."""
    return name.isdigit() and 4 <= len(name) <= 6
