"""
Alpha Strategist — Multi-Source Data Fetcher (V1.1)

匯出 ScanResult dataclass 供系統各模組共用。

Functions:
  get_portfolio()         : 讀取 data/ CSV，回傳合併持倉字典。
  get_market_data(tickers): 批次評估標的，回傳 ScanResult 列表。
  get_tw_auctions()       : 抓取台股近期 IPO / 競拍日程（TWSE OpenAPI，失敗回傳 []）。
  get_target_news(ticker) : 取得單一標的近期新聞標題摘要列表。

CRITICAL: 純 Data Layer，禁止任何下單或市場預測邏輯。Human-in-the-Loop。
"""

from __future__ import annotations

import json as _json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import yfinance as yf

from src.mcp_servers.portfolio_gateway import load_portfolio
from src.strategies.quant_engine import (
    calculate_modified_peg,
    calculate_ps_growth_ratio,
    check_momentum_trend,
)
from src.data.price_provider import LivePriceProvider

logger = logging.getLogger(__name__)
_price_provider = LivePriceProvider()

DATA_DIR              = Path("data")
PRICE_HISTORY_PERIOD  = "1y"
THRESHOLD_PEG_SELL    = 1.5
THRESHOLD_PEG_BUY     = 0.8
THRESHOLD_PS_SELL     = 3.0
THRESHOLD_PS_BUY      = 0.5

# ── Circuit Breaker thresholds (override ALL valuation signals) ───────────────
CB_MA50_UPPER         = 0.25   # distance_to_ma50 >= +25% → 嚴重過熱，強制 CRITICAL
CB_MA50_LOWER         = -0.20  # distance_to_ma50 <= -20% → 嚴重破線，強制 CRITICAL

# TWSE ex-rights/dividend announcement table — the only publicly accessible
# JSON endpoint that contains upcoming 現金增資 (cash capital increase) events.
# Endpoint verified working: returns list-of-dicts with Date, Code, Name,
# SubscriptionPricePerShare, SubscriptionRatio, etc.
_TWSE_EX_RIGHTS_URL  = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"
# TPEX OTC (上櫃) ex-rights pre-announcement. Verified working; field names differ
# from TWSE: ExRrightsExDividendDate (ROC 7-digit), SecuritiesCompanyCode, CompanyName,
# SubscriptionPricePerShare (same field name as TWSE).
_TPEX_EX_RIGHTS_URL  = "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost"
# TWSE upcoming IPO listings pipeline. Verified working; contains UnderwritingPrice and
# ApprovedListingDate (ROC 7-digit) for all approved-but-not-yet-listed companies.
_TWSE_NEW_LISTING_URL = "https://openapi.twse.com.tw/v1/company/newlisting"
# TPEX OTC applicant companies pipeline. Verified working; contains OfferingPrice and
# TPExApprovedTradingDate (YYYYMMDD) for companies approved for OTC listing.
_TPEX_APPLICANT_URL  = "https://www.tpex.org.tw/openapi/v1/tpex_esb_applicant_companies"
# TWSE competitive-bidding auction schedule (競價拍賣). Single JSON endpoint covers
# TWSE mainboard, TWSE Innovation Board, AND TPEX OTC auctions.
# Returns a table with fields including 最低投標價格(元), 投標結束日, 撥券日期.
# NOTE: also contains convertible-bond auctions — filter those out by 發行性質.
_TWSE_AUCTION_URL    = "https://www.twse.com.tw/zh/announcement/auction"
# Only surface IPO events whose approved trading date falls within this window.
_IPO_LOOKBACK_DAYS   = 30   # include events approved up to N days in the past
_IPO_LOOKAHEAD_DAYS  = 90   # include events approved up to N days in the future
# Valuation data sources (for IPO bidding ceiling calculator)
_TPEX_ESB_PRICE_URL  = "https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics"
_TPEX_ESB_EPS_URL    = "https://www.tpex.org.tw/openapi/v1/tpex_esb_eps_rank"
_TPEX_MBOARD_PE_URL  = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
_TWSE_PE_URL         = "https://www.twse.com.tw/zh/exchangeReport/BWIBBU_d?response=json&selectType=ALL"
_REQUEST_TIMEOUT     = 10
_CACHE_TTL_HOURS     = 24

_AUCTIONS_FILE        = DATA_DIR / "auctions.json"         # manual override (highest priority)
_AUCTIONS_CACHE_FILE  = DATA_DIR / "auctions_cache.json"   # live-fetch 24h cache

# Matches bare Taiwan stock codes: 4-6 digits, optional letter suffix (e.g. 2330, 00878, 00878B)
_TW_CODE_RE = re.compile(r"^\d{4,6}[A-Z]?$")


# ── TW Ticker 正規化 ──────────────────────────────────────────────────────────

def _normalize_ticker(ticker: str) -> str:
    """
    將裸露的台股代號補上 .TW 後綴供 yfinance 使用。

    規則：
      - 已含 .TW / .TWO 後綴 → 原樣回傳
      - 純數字代號（4-6 碼，可帶單字母後綴如 00878B）→ 補上 .TW（TWSE 預設）
      - 其他（美股英文代號等）→ 原樣回傳

    Args:
        ticker: 來自持倉 CSV 的原始代號。

    Returns:
        可直接傳入 yfinance 的正規化代號。
    """
    upper = ticker.upper()
    if upper.endswith(".TW") or upper.endswith(".TWO"):
        return ticker
    if _TW_CODE_RE.match(upper):
        return ticker + ".TW"
    return ticker


# ── ScanResult（全系統共用）──────────────────────────────────────────────────

@dataclass
class ScanResult:
    ticker:           str
    name:             str                = ""
    # ── PEG Model ──────────────────────────────────────────────────────────
    current_price:    float | None       = None
    trailing_pe:      float | None       = None
    earnings_growth:  float | None       = None
    capex_to_rev:     float | None       = None
    modified_peg:     float | None       = None
    # ── P/S Model (pre-profit hyper-growth fallback) ────────────────────
    ps_ratio:         float | None       = None
    revenue_growth:   float | None       = None
    ps_growth_ratio:  float | None       = None
    # ── Technical ──────────────────────────────────────────────────────────
    ma50:             float | None       = None
    ema10:            float | None       = None   # 10-day EMA
    ema10_dist:       float | None       = None   # (price - ema10) / ema10
    ema21:            float | None       = None   # 21-day EMA
    ema21_dist:       float | None       = None   # (price - ema21) / ema21
    week_52_high:     float | None       = None
    beta:             float | None       = None   # market beta (yfinance info["beta"])
    momentum:         bool  | None       = None
    # ── Meta ───────────────────────────────────────────────────────────────
    valuation_model:  str                = ""    # "PEG" | "PS" | "TECHNICAL"
    signals:          list[str]          = field(default_factory=list)
    alerts_sent:      list[str]          = field(default_factory=list)
    error:            str                = ""
    is_discovery:     bool               = False   # True → from universe scan, not portfolio


# ── 單檔評估（內部）──────────────────────────────────────────────────────────

def _evaluate_ticker(ticker: str) -> ScanResult:
    """
    對單一股票執行完整量化評估（yfinance + quant_engine）。

    任何步驟失敗僅設 ScanResult.error，不拋出例外，確保批次掃描不中斷。

    Args:
        ticker: 正規化股票代號（'NVDA'、'2330.TW'）。

    Returns:
        填充完整的 ScanResult。
    """
    result = ScanResult(ticker=ticker)

    # ── 基本面資料 ────────────────────────────────────────────────────────────
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        result.name            = info.get("shortName") or info.get("longName") or ""
        result.current_price   = info.get("currentPrice") or info.get("regularMarketPrice")
        result.trailing_pe     = info.get("trailingPE")
        result.earnings_growth = info.get("earningsGrowth")
        result.ps_ratio        = info.get("priceToSalesTrailing12Months")
        result.revenue_growth  = info.get("revenueGrowth")
        result.week_52_high    = info.get("fiftyTwoWeekHigh")
        result.beta            = info.get("beta")

        try:
            cf            = t.cashflow
            total_revenue = info.get("totalRevenue") or 0
            if "Capital Expenditure" in cf.index and total_revenue > 0:
                capex               = abs(float(cf.loc["Capital Expenditure"].iloc[0]))
                result.capex_to_rev = round(capex / total_revenue, 4)
        except Exception:
            pass

    except Exception as exc:
        result.error = f"info 取得失敗: {exc}"
        logger.warning("[SKIP] %s — %s", ticker, result.error)
        return result

    if result.current_price is None:
        result.error = "currentPrice 為空，代號可能無效"
        logger.warning("[SKIP] %s — %s", ticker, result.error)
        return result

    # ── 價格歷史（50MA / 動能）───────────────────────────────────────────────
    try:
        hist = t.history(period=PRICE_HISTORY_PERIOD)
        if hist.empty:
            raise ValueError("yfinance 回傳空歷史")
        closes       = hist["Close"]
        result.ma50  = float(closes.rolling(50).mean().iloc[-1])
        ema10_series = closes.ewm(span=10, adjust=False).mean()
        ema21_series = closes.ewm(span=21, adjust=False).mean()
        result.ema10 = float(ema10_series.iloc[-1])
        result.ema21 = float(ema21_series.iloc[-1])
        if result.current_price and result.ema10 and result.ema21:
            result.ema10_dist = (result.current_price - result.ema10) / result.ema10
            result.ema21_dist = (result.current_price - result.ema21) / result.ema21
        result.momentum = check_momentum_trend(closes, week_52_high=result.week_52_high)
    except Exception as exc:
        logger.warning("[%s] 價格歷史取得失敗，跳過 MA 判斷：%s", ticker, exc)

    # ── 估值模型選擇 ──────────────────────────────────────────────────────────
    has_peg_data = (
        result.trailing_pe is not None
        and result.trailing_pe > 0
        and result.earnings_growth is not None
    )
    has_ps_data = (
        result.ps_ratio is not None
        and result.ps_ratio > 0
        and result.revenue_growth is not None
        and result.revenue_growth > 0
    )

    if has_peg_data:
        result.valuation_model = "PEG"
        result.modified_peg = calculate_modified_peg(
            pe=result.trailing_pe,
            eps_growth=result.earnings_growth,
            capex_to_rev=result.capex_to_rev or 0.0,
        )
    elif has_ps_data:
        result.valuation_model = "PS"
        result.ps_growth_ratio = calculate_ps_growth_ratio(
            ps_ratio=result.ps_ratio,
            revenue_growth=result.revenue_growth,
        )
    else:
        result.valuation_model = "TECHNICAL"

    # ── CIRCUIT BREAKER — 極端技術面強制覆蓋，無視估值訊號 ───────────────────
    if (
        result.current_price is not None
        and result.ma50 is not None
        and result.ma50 > 0
    ):
        dist_to_ma50 = (result.current_price - result.ma50) / result.ma50
        if dist_to_ma50 >= CB_MA50_UPPER:
            result.signals.append("嚴重技術面過熱：強制減倉停利")
            return result
        if dist_to_ma50 <= CB_MA50_LOWER:
            result.signals.append("技術面嚴重破線：需停損")
            return result

    # ── 訊號判斷（三線獨立）──────────────────────────────────────────────────
    # [A] 過高估值 → 減倉停利
    if result.valuation_model == "PEG":
        if result.modified_peg is not None and result.modified_peg > THRESHOLD_PEG_SELL:
            result.signals.append("減倉停利")
    elif result.valuation_model == "PS":
        if result.ps_growth_ratio is not None and result.ps_growth_ratio > THRESHOLD_PS_SELL:
            result.signals.append("減倉停利")

    # [B] Price < 50MA → 趨勢走弱（適用所有模型）
    if (
        result.current_price is not None
        and result.ma50 is not None
        and result.current_price < result.ma50
    ):
        result.signals.append("趨勢走弱")

    # [C] 低估值 + 多頭動能 → 加倉
    if result.valuation_model == "PEG":
        if (
            result.modified_peg is not None
            and result.modified_peg < THRESHOLD_PEG_BUY
            and result.momentum is True
        ):
            result.signals.append("加倉 Alpha")
    elif result.valuation_model == "PS":
        if (
            result.ps_growth_ratio is not None
            and result.ps_growth_ratio < THRESHOLD_PS_BUY
            and result.momentum is True
        ):
            result.signals.append("加倉 Alpha [HYPER-GROWTH]")

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def get_portfolio(data_dir: Path = DATA_DIR) -> dict:
    """
    讀取 data_dir 下最新的國泰雙格式 CSV，回傳合併持倉字典。

    Returns:
        keys: df (DataFrame), us_count, tw_count, files, warnings。
    """
    return load_portfolio(data_dir)


def get_market_data(tickers: list[str]) -> list[ScanResult]:
    """
    批次評估標的列表，回傳完整的 ScanResult 列表。

    台股裸代號（如 2330）自動補 .TW；若 TWSE 無報價，自動重試 .TWO（上櫃股）。
    任何單一標的失敗均記錄於 ScanResult.error，不中斷批次執行。

    Args:
        tickers: 持倉代號列表（含裸台股代號、.TW/.TWO、或美股英文代號均可）。

    Returns:
        與輸入等長的 ScanResult 列表。
    """
    results: list[ScanResult] = []
    for ticker in tickers:
        normalized = _normalize_ticker(ticker)
        logger.info("[DATA_FETCHER] 評估 %s ...", normalized)
        result = _evaluate_ticker(normalized)

        # TPEX fallback：裸台股代號 + .TW 無報價 → 改試 .TWO（上櫃）
        if (
            result.error
            and normalized.endswith(".TW")
            and _TW_CODE_RE.match(ticker.upper())
        ):
            tpex_ticker = ticker + ".TWO"
            logger.info(
                "[DATA_FETCHER] %s 無報價，嘗試 TPEX（%s）...", normalized, tpex_ticker
            )
            alt = _evaluate_ticker(tpex_ticker)
            if not alt.error:
                result = alt
                logger.info("[DATA_FETCHER] %s 成功切換至 TPEX", tpex_ticker)

        results.append(result)
    return results



def get_macro_regime() -> dict:
    """
    Fetch SPY and ^VIX to classify the current macroeconomic market regime.

    Uses a local import of MarketRegime / classify_regime from risk_engine to
    avoid a circular import at module load time (correlation → data_fetcher).

    Regime classification rules:
      CRASH  : VIX ≥ 30  AND  SPY > 5 % below its 50-day MA
      BEAR   : VIX ≥ 20  AND  SPY below its 50-day MA
      BULL   : SPY at or above its 50-day MA
      NEUTRAL: everything else (mild VIX, SPY slightly below MA)

    On any fetch failure the function returns regime=BULL (conservative —
    we never suppress swap recommendations due to a data outage).

    Returns:
        Dict with keys:
          spy_price   – float | None    current SPY price
          spy_ma50    – float | None    SPY 50-day MA
          spy_ma_dist – float | None    signed distance (e.g. -0.07 = 7 % below)
          vix_close   – float | None    latest VIX close
          regime      – MarketRegime    enum value
          error       – str | None      description of any fetch failure
    """
    # Local import avoids circular dependency:
    # data_fetcher → risk_engine → correlation → data_fetcher (module-level)
    from src.risk_engine import MarketRegime, classify_regime  # noqa: PLC0415

    spy_price:   float | None = None
    spy_ma50:    float | None = None
    spy_ma_dist: float | None = None
    vix_close:   float | None = None
    error_parts: list[str]    = []

    try:
        spy_hist = yf.Ticker("SPY").history(period="3mo")
        if spy_hist.empty:
            raise ValueError("empty SPY history")
        closes      = spy_hist["Close"]
        spy_price   = float(closes.iloc[-1])
        spy_ma50    = float(closes.rolling(50).mean().iloc[-1])
        spy_ma_dist = (spy_price - spy_ma50) / spy_ma50
    except Exception as exc:
        error_parts.append(f"SPY fetch failed: {exc}")
        logger.warning("[MACRO] SPY data unavailable: %s", exc)

    try:
        vix_hist = yf.Ticker("^VIX").history(period="5d")
        if vix_hist.empty:
            raise ValueError("empty VIX history")
        vix_close = float(vix_hist["Close"].iloc[-1])
    except Exception as exc:
        error_parts.append(f"VIX fetch failed: {exc}")
        logger.warning("[MACRO] VIX data unavailable: %s", exc)

    if spy_ma_dist is not None and vix_close is not None:
        regime = classify_regime(vix_close, spy_ma_dist)
    else:
        regime = MarketRegime.BULL   # safe default — never suppress on data failure
        error_parts.append("Regime defaulted to BULL (insufficient data)")

    logger.info(
        "[MACRO] SPY=%.2f MA50=%.2f dist=%+.2f%%  VIX=%.1f  Regime=%s",
        spy_price or 0.0,
        spy_ma50  or 0.0,
        (spy_ma_dist or 0.0) * 100,
        vix_close or 0.0,
        regime.value,
    )

    return {
        "spy_price":   spy_price,
        "spy_ma50":    spy_ma50,
        "spy_ma_dist": spy_ma_dist,
        "vix_close":   vix_close,
        "regime":      regime,
        "error":       "  |  ".join(error_parts) or None,
    }

def _roc_to_iso(date_str: str) -> str:
    """
    Convert a ROC (Republic of China) date string to ISO 8601.

    Examples:
      '1150514' → '2026-05-14'   (ROC year 115 + 1911 = 2026)
      '1150101' → '2026-01-01'

    Args:
        date_str: 7-digit ROC date string (YYYMMDD).

    Returns:
        ISO date string 'YYYY-MM-DD', or the original string if unparseable.
    """
    s = date_str.strip()
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
    return s


def _fetch_twse_ex_rights() -> list[dict]:
    """
    Fetch TWSE ex-rights / subscription schedule and return 現金增資 events.

    Endpoint: /v1/exchangeReport/TWT48U_ALL  (上市股票除權除息預告表)
    Filter:   rows where SubscriptionPricePerShare is present and non-zero,
              AND the ex-rights date is today or in the future.

    Returns:
        Standardised event dicts; empty list on any failure.
    """
    try:
        resp = requests.get(
            _TWSE_EX_RIGHTS_URL,
            timeout=_REQUEST_TIMEOUT,
            headers={
                "Accept":     "application/json",
                "User-Agent": "AlphaStrategist/1.2 (portfolio monitor)",
            },
        )
        resp.raise_for_status()

        if not resp.content or not resp.text.strip():
            logger.info("[TW_AUCTION] TWT48U_ALL 回傳空白內容")
            return []

        try:
            rows = resp.json()
        except ValueError:
            logger.warning(
                "[TW_AUCTION] TWT48U_ALL 回傳非 JSON（Content-Type: %s）",
                resp.headers.get("Content-Type", "unknown"),
            )
            return []

        today = datetime.now().date()
        events: list[dict] = []

        logger.info("[TW_AUCTION][DEBUG] TWT48U_ALL 原始筆數：%d", len(rows))

        for row in rows:
            if not isinstance(row, dict):
                logger.debug("[TW_AUCTION][DEBUG] 跳過非 dict 列：%r", row)
                continue

            code      = (row.get("Code") or "").strip()
            label     = f"{code} {(row.get('Name') or '').strip()}".strip()
            sub_price = (row.get("SubscriptionPricePerShare") or "").strip()

            if not sub_price or sub_price == "0":
                logger.info(
                    "[TW_AUCTION][DEBUG] 略過（無認購價 / 純除息）：%s  sub_price=%r",
                    label, sub_price,
                )
                continue  # skip pure dividend rows with no rights offering

            iso_date = _roc_to_iso(row.get("Date", ""))

            # Keep only future events (including today)
            try:
                event_date = datetime.strptime(iso_date, "%Y-%m-%d").date()
                if event_date < today:
                    logger.info(
                        "[TW_AUCTION][DEBUG] 略過（已過期）：%s  date=%s",
                        label, iso_date,
                    )
                    continue
            except ValueError:
                logger.info(
                    "[TW_AUCTION][DEBUG] 日期解析失敗，保守保留：%s  raw_date=%r",
                    label, row.get("Date", ""),
                )

            if not code:
                logger.info("[TW_AUCTION][DEBUG] 略過（無代碼）：%r", row)
                continue

            events.append({
                "ticker":      code,
                "name":        (row.get("Name") or "").strip(),
                "kind":        "增資",
                "exchange":    "TWSE",
                "date":        iso_date,
                "floor_price": sub_price,
            })

        logger.info(
            "[TW_AUCTION][DEBUG] 過濾後保留 %d / %d 筆增資事件（認購日 ≥ 今日）",
            len(events), len(rows),
        )
        return events

    except requests.exceptions.Timeout:
        logger.warning("[TW_AUCTION] TWT48U_ALL 請求逾時（%ds）", _REQUEST_TIMEOUT)
    except requests.exceptions.ConnectionError as exc:
        logger.warning("[TW_AUCTION] TWT48U_ALL 連線失敗：%s", exc)
    except requests.exceptions.HTTPError as exc:
        logger.warning("[TW_AUCTION] TWT48U_ALL HTTP 錯誤：%s", exc)
    except Exception as exc:
        logger.warning("[TW_AUCTION] TWT48U_ALL 未預期錯誤：%s", exc)
    return []


def _fetch_tpex_ex_rights() -> list[dict]:
    """
    Fetch TPEX OTC (上櫃) ex-rights pre-announcement and return 現金增資 events.

    Endpoint: /openapi/v1/tpex_exright_prepost  (上櫃股票除權息預告表)
    Filter:   rows where SubscriptionPricePerShare is present and non-zero.
              Date field: ExRrightsExDividendDate, same 7-digit ROC format as TWSE.

    Note: 興櫃 (emerging board) stocks are NOT included in this endpoint — they
    trade via negotiated prices through registered dealers and have no public
    subscription calendar. Use data/auctions.json manual override for those.

    Returns:
        Standardised event dicts (exchange="TPEX"); empty list on any failure.
    """
    try:
        resp = requests.get(
            _TPEX_EX_RIGHTS_URL,
            timeout=_REQUEST_TIMEOUT,
            headers={
                "Accept":     "application/json",
                "User-Agent": "AlphaStrategist/1.2 (portfolio monitor)",
            },
        )
        resp.raise_for_status()

        if not resp.content or not resp.text.strip():
            logger.info("[TW_AUCTION] tpex_exright_prepost 回傳空白內容")
            return []

        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct:
            logger.warning(
                "[TW_AUCTION] tpex_exright_prepost 回傳非 JSON（Content-Type: %s）", ct
            )
            return []

        try:
            rows = resp.json()
        except ValueError:
            logger.warning("[TW_AUCTION] tpex_exright_prepost JSON 解析失敗")
            return []

        today = datetime.now().date()
        events: list[dict] = []

        logger.info("[TW_AUCTION][DEBUG] tpex_exright_prepost 原始筆數：%d", len(rows))

        for row in rows:
            if not isinstance(row, dict):
                continue

            code      = (row.get("SecuritiesCompanyCode") or "").strip()
            label     = f"{code} {(row.get('CompanyName') or '').strip()}".strip()
            sub_price = (row.get("SubscriptionPricePerShare") or "").strip()

            # Skip pure dividend / stock-split rows (no cash subscription price)
            if not sub_price or sub_price in ("0", "0.00"):
                logger.info(
                    "[TW_AUCTION][DEBUG] 略過（無認購價 / 純除息）：%s  sub_price=%r",
                    label, sub_price,
                )
                continue

            iso_date = _roc_to_iso(row.get("ExRrightsExDividendDate", ""))

            try:
                event_date = datetime.strptime(iso_date, "%Y-%m-%d").date()
                if event_date < today:
                    logger.info(
                        "[TW_AUCTION][DEBUG] 略過（已過期）：%s  date=%s",
                        label, iso_date,
                    )
                    continue
            except ValueError:
                logger.info(
                    "[TW_AUCTION][DEBUG] 日期解析失敗，保守保留：%s  raw_date=%r",
                    label, row.get("ExRrightsExDividendDate", ""),
                )

            if not code:
                logger.info("[TW_AUCTION][DEBUG] 略過（無代碼）：%r", row)
                continue

            events.append({
                "ticker":      code,
                "name":        (row.get("CompanyName") or "").strip(),
                "kind":        "增資",
                "exchange":    "TPEX",
                "date":        iso_date,
                "floor_price": sub_price,
            })

        logger.info(
            "[TW_AUCTION][DEBUG] TPEX 過濾後保留 %d / %d 筆增資事件（除權日 ≥ 今日）",
            len(events), len(rows),
        )
        return events

    except requests.exceptions.Timeout:
        logger.warning("[TW_AUCTION] tpex_exright_prepost 請求逾時（%ds）", _REQUEST_TIMEOUT)
    except requests.exceptions.ConnectionError as exc:
        logger.warning("[TW_AUCTION] tpex_exright_prepost 連線失敗：%s", exc)
    except requests.exceptions.HTTPError as exc:
        logger.warning("[TW_AUCTION] tpex_exright_prepost HTTP 錯誤：%s", exc)
    except Exception as exc:
        logger.warning("[TW_AUCTION] tpex_exright_prepost 未預期錯誤：%s", exc)
    return []


def _fetch_twse_ipo_listings() -> list[dict]:
    """
    Fetch upcoming TWSE IPO listings from the new-listing pipeline.

    Endpoint: /v1/company/newlisting  (最近上市公司)
    Filter:   records where ListingDate is empty (not yet trading) AND
              UnderwritingPrice is populated AND ApprovedListingDate falls
              within [today - _IPO_LOOKBACK_DAYS, today + _IPO_LOOKAHEAD_DAYS].

    Date format: ApprovedListingDate is ROC 7-digit (YYYMMDD).

    Returns:
        Standardised event dicts (kind='IPO 承銷', exchange='TWSE');
        empty list on any failure.
    """
    try:
        resp = requests.get(
            _TWSE_NEW_LISTING_URL,
            timeout=_REQUEST_TIMEOUT,
            headers={
                "Accept":     "application/json",
                "User-Agent": "AlphaStrategist/1.2 (portfolio monitor)",
            },
        )
        resp.raise_for_status()

        if not resp.content or not resp.text.strip():
            logger.info("[TW_AUCTION] newlisting 回傳空白內容")
            return []

        try:
            rows = resp.json()
        except ValueError:
            logger.warning(
                "[TW_AUCTION] newlisting 回傳非 JSON（Content-Type: %s）",
                resp.headers.get("Content-Type", "unknown"),
            )
            return []

        today    = datetime.now().date()
        lo_date  = today
        hi_date  = today + timedelta(days=_IPO_LOOKAHEAD_DAYS)
        events: list[dict] = []

        logger.info("[TW_AUCTION][DEBUG] TWSE newlisting 原始筆數：%d", len(rows))

        for row in rows:
            if not isinstance(row, dict):
                continue

            # Skip already-listed companies
            if (row.get("ListingDate") or "").strip():
                continue

            code  = (row.get("Code") or "").strip()
            label = f"{code} {(row.get('Company') or '').strip()}".strip()

            price = (row.get("UnderwritingPrice") or "").strip()
            if not price or price in ("0", "0.00"):
                logger.info(
                    "[TW_AUCTION][DEBUG] 略過（無承銷價）：%s  price=%r", label, price
                )
                continue

            # ApprovedListingDate: ROC 7-digit
            iso_date = _roc_to_iso((row.get("ApprovedListingDate") or "").strip())
            try:
                event_date = datetime.strptime(iso_date, "%Y-%m-%d").date()
                if not (lo_date <= event_date <= hi_date):
                    logger.info(
                        "[TW_AUCTION][DEBUG] 略過（日期超出窗口）：%s  date=%s", label, iso_date
                    )
                    continue
            except ValueError:
                logger.info(
                    "[TW_AUCTION][DEBUG] 日期解析失敗，略過：%s  raw=%r",
                    label, row.get("ApprovedListingDate", ""),
                )
                continue

            if not code:
                continue

            events.append({
                "ticker":      code,
                "name":        (row.get("Company") or "").strip(),
                "kind":        "IPO 承銷",
                "exchange":    "TWSE",
                "date":        iso_date,
                "floor_price": price,
            })

        logger.info(
            "[TW_AUCTION][DEBUG] TWSE IPO 保留 %d / %d 筆（窗口：%s ~ %s）",
            len(events), len(rows), lo_date, hi_date,
        )
        return events

    except requests.exceptions.Timeout:
        logger.warning("[TW_AUCTION] newlisting 請求逾時（%ds）", _REQUEST_TIMEOUT)
    except requests.exceptions.ConnectionError as exc:
        logger.warning("[TW_AUCTION] newlisting 連線失敗：%s", exc)
    except requests.exceptions.HTTPError as exc:
        logger.warning("[TW_AUCTION] newlisting HTTP 錯誤：%s", exc)
    except Exception as exc:
        logger.warning("[TW_AUCTION] newlisting 未預期錯誤：%s", exc)
    return []


def _fetch_tpex_ipo_listings() -> list[dict]:
    """
    Fetch upcoming TPEX OTC IPO listings from the applicant companies pipeline.

    Endpoint: /openapi/v1/tpex_esb_applicant_companies  (申請上櫃公司)
    Filter:   records where ListingDate is empty AND OfferingPrice is populated
              AND TPExApprovedTradingDate falls within the lookback/lookahead window.

    Date format: TPExApprovedTradingDate is YYYYMMDD (Gregorian, not ROC).

    Returns:
        Standardised event dicts (kind='IPO 承銷', exchange='TPEX');
        empty list on any failure.
    """
    try:
        resp = requests.get(
            _TPEX_APPLICANT_URL,
            timeout=_REQUEST_TIMEOUT,
            headers={
                "Accept":     "application/json",
                "User-Agent": "AlphaStrategist/1.2 (portfolio monitor)",
            },
        )
        resp.raise_for_status()

        if not resp.content or not resp.text.strip():
            logger.info("[TW_AUCTION] tpex_esb_applicant_companies 回傳空白內容")
            return []

        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct:
            logger.warning(
                "[TW_AUCTION] tpex_esb_applicant_companies 回傳非 JSON（Content-Type: %s）", ct
            )
            return []

        try:
            rows = resp.json()
        except ValueError:
            logger.warning("[TW_AUCTION] tpex_esb_applicant_companies JSON 解析失敗")
            return []

        today   = datetime.now().date()
        lo_date = today
        hi_date = today + timedelta(days=_IPO_LOOKAHEAD_DAYS)
        events: list[dict] = []

        logger.info(
            "[TW_AUCTION][DEBUG] TPEX applicant_companies 原始筆數：%d", len(rows)
        )

        for row in rows:
            if not isinstance(row, dict):
                continue

            if (row.get("ListingDate") or "").strip():
                continue

            code  = (row.get("SecuritiesCompanyCode") or "").strip()
            label = f"{code} {(row.get('CompanyName') or '').strip()}".strip()

            price = (row.get("OfferingPrice") or "").strip()
            if not price or price in ("0", "0.00"):
                logger.info(
                    "[TW_AUCTION][DEBUG] 略過（無承銷價）：%s  price=%r", label, price
                )
                continue

            # TPExApprovedTradingDate: YYYYMMDD (Gregorian)
            raw_date = (row.get("TPExApprovedTradingDate") or "").strip()
            try:
                event_date = datetime.strptime(raw_date, "%Y%m%d").date()
                iso_date   = event_date.strftime("%Y-%m-%d")
                if not (lo_date <= event_date <= hi_date):
                    logger.info(
                        "[TW_AUCTION][DEBUG] 略過（日期超出窗口）：%s  date=%s", label, iso_date
                    )
                    continue
            except ValueError:
                logger.info(
                    "[TW_AUCTION][DEBUG] 日期解析失敗，略過：%s  raw=%r", label, raw_date
                )
                continue

            if not code:
                continue

            events.append({
                "ticker":      code,
                "name":        (row.get("CompanyName") or "").strip(),
                "kind":        "IPO 承銷",
                "exchange":    "TPEX",
                "date":        iso_date,
                "floor_price": price,
            })

        logger.info(
            "[TW_AUCTION][DEBUG] TPEX IPO 保留 %d / %d 筆（窗口：%s ~ %s）",
            len(events), len(rows), lo_date, hi_date,
        )
        return events

    except requests.exceptions.Timeout:
        logger.warning(
            "[TW_AUCTION] tpex_esb_applicant_companies 請求逾時（%ds）", _REQUEST_TIMEOUT
        )
    except requests.exceptions.ConnectionError as exc:
        logger.warning("[TW_AUCTION] tpex_esb_applicant_companies 連線失敗：%s", exc)
    except requests.exceptions.HTTPError as exc:
        logger.warning("[TW_AUCTION] tpex_esb_applicant_companies HTTP 錯誤：%s", exc)
    except Exception as exc:
        logger.warning("[TW_AUCTION] tpex_esb_applicant_companies 未預期錯誤：%s", exc)
    return []


def _fetch_twse_competitive_auctions() -> list[dict]:
    """
    Fetch competitive-bidding IPO auctions (競價拍賣) from the TWSE announcement API.

    Endpoint: https://www.twse.com.tw/zh/announcement/auction
    Coverage: TWSE mainboard, TWSE Innovation Board (創新板), TPEX OTC (櫃檯買賣).
              Convertible bonds (公司債) are excluded — equity listings only.

    Key fields used:
      證券代號           → ticker (bare code)
      證券名稱           → company name
      最低投標價格(元)    → floor price (拍賣底價)
      撥券日期(上市、上櫃日期) → listing / delivery date (used as event date)
      投標結束日         → bidding deadline (used for window filtering)
      發行市場           → determines suffix: 集中交易市場/創新板 → .TW, 櫃檯買賣 → .TWO
      取消競價拍賣       → skip if non-empty (cancelled/failed auction)

    Window: events whose 撥券日期 falls within
            [today - _IPO_LOOKBACK_DAYS, today + _IPO_LOOKAHEAD_DAYS].

    Returns:
        Standardised event dicts (kind='IPO 競拍'); empty list on any failure.
    """
    try:
        resp = requests.get(
            _TWSE_AUCTION_URL,
            timeout=_REQUEST_TIMEOUT,
            headers={
                "Accept":        "application/json",
                "User-Agent":    "Mozilla/5.0 (compatible; AlphaStrategist/1.2)",
                "Accept-Language": "zh-TW,zh;q=0.9",
            },
        )
        resp.raise_for_status()

        if not resp.content or not resp.text.strip():
            logger.info("[TW_AUCTION] TWSE auction API 回傳空白內容")
            return []

        try:
            payload = resp.json()
        except ValueError:
            logger.warning(
                "[TW_AUCTION] TWSE auction API 回傳非 JSON（Content-Type: %s）",
                resp.headers.get("Content-Type", "unknown"),
            )
            return []

        if payload.get("stat") != "OK":
            logger.warning(
                "[TW_AUCTION] TWSE auction API stat=%s", payload.get("stat")
            )
            return []

        fields  = payload.get("fields", [])
        raw_rows = payload.get("data", [])
        if not fields or not raw_rows:
            logger.info("[TW_AUCTION] TWSE auction API 無資料")
            return []

        today   = datetime.now().date()
        lo_date = today
        hi_date = today + timedelta(days=_IPO_LOOKAHEAD_DAYS)
        events: list[dict] = []

        logger.info("[TW_AUCTION][DEBUG] TWSE 競拍 原始筆數：%d", len(raw_rows))

        for raw in raw_rows:
            row = dict(zip(fields, raw))

            code  = (row.get("證券代號") or "").strip()
            label = f"{code} {(row.get('證券名稱') or '').strip()}".strip()

            # Skip cancelled / failed auctions
            if (row.get("取消競價拍賣(流標或取消)") or "").strip():
                logger.info("[TW_AUCTION][DEBUG] 略過（取消/流標）：%s", label)
                continue

            # Skip convertible bonds and other non-equity instruments
            nature = (row.get("發行性質") or "").strip()
            if "公司債" in nature:
                logger.info(
                    "[TW_AUCTION][DEBUG] 略過（非股票）：%s  性質=%s", label, nature
                )
                continue

            # Parse listing / delivery date (撥券日期) — format YYYY/MM/DD
            listing_raw = (row.get("撥券日期(上市、上櫃日期)") or "").strip()
            try:
                listing_date = datetime.strptime(listing_raw, "%Y/%m/%d").date()
                iso_date     = listing_date.strftime("%Y-%m-%d")
                if not (lo_date <= listing_date <= hi_date):
                    logger.info(
                        "[TW_AUCTION][DEBUG] 略過（撥券日超出窗口）：%s  date=%s",
                        label, iso_date,
                    )
                    continue
            except ValueError:
                logger.info(
                    "[TW_AUCTION][DEBUG] 撥券日解析失敗，略過：%s  raw=%r",
                    label, listing_raw,
                )
                continue

            # Skip if bidding deadline has already passed
            bid_deadline_raw = (row.get("投標結束日") or "").strip()
            if bid_deadline_raw:
                try:
                    bid_deadline_date = datetime.strptime(bid_deadline_raw, "%Y/%m/%d").date()
                    if bid_deadline_date < today:
                        logger.info(
                            "[TW_AUCTION][DEBUG] 略過（投標截止已過）：%s  deadline=%s",
                            label, bid_deadline_raw,
                        )
                        continue
                except ValueError:
                    pass

            price = (row.get("最低投標價格(元)") or "").strip()
            if not price or price in ("0", "0.00"):
                logger.info(
                    "[TW_AUCTION][DEBUG] 略過（無底價）：%s  price=%r", label, price
                )
                continue

            if not code:
                continue

            # Map market to exchange label and yfinance suffix
            market   = (row.get("發行市場") or "").strip()
            exchange = "TPEX" if market == "櫃檯買賣" else "TWSE"

            events.append({
                "ticker":      code,
                "name":        (row.get("證券名稱") or "").strip(),
                "kind":        "IPO 競拍",
                "exchange":    exchange,
                "date":        iso_date,   # listing / delivery date
                "floor_price": price,
                # Extra fields surfaced in the UI note
                "bid_deadline": (row.get("投標結束日") or "").strip(),
                "market_label": nature,
            })

        logger.info(
            "[TW_AUCTION][DEBUG] TWSE 競拍 保留 %d / %d 筆（窗口：%s ~ %s）",
            len(events), len(raw_rows), lo_date, hi_date,
        )
        return events

    except requests.exceptions.Timeout:
        logger.warning("[TW_AUCTION] TWSE auction API 請求逾時（%ds）", _REQUEST_TIMEOUT)
    except requests.exceptions.ConnectionError as exc:
        logger.warning("[TW_AUCTION] TWSE auction API 連線失敗：%s", exc)
    except requests.exceptions.HTTPError as exc:
        logger.warning("[TW_AUCTION] TWSE auction API HTTP 錯誤：%s", exc)
    except Exception as exc:
        logger.warning("[TW_AUCTION] TWSE auction API 未預期錯誤：%s", exc)
    return []


def _fetch_market_median_pe(exchange: str) -> float | None:
    """
    Compute the median P/E ratio of all listed companies on the given exchange.

    Uses:
      TPEX OTC: /openapi/v1/tpex_mainboard_peratio_analysis
      TWSE:     /zh/exchangeReport/BWIBBU_d (本益比 field)

    Only positive, finite P/E values are included (negatives = loss-making).

    Args:
        exchange: 'TPEX' or 'TWSE'.

    Returns:
        Median P/E as float, or None on failure.
    """
    # ── Live fetch ────────────────────────────────────────────────────────────
    pe_values: list[float] = []
    try:
        if exchange == "TPEX":
            resp = requests.get(
                _TPEX_MBOARD_PE_URL, timeout=_REQUEST_TIMEOUT,
                headers={"Accept": "application/json",
                         "User-Agent": "AlphaStrategist/1.2"},
            )
            resp.raise_for_status()
            rows = resp.json()
            for row in rows:
                pe_str = (row.get("PriceEarningRatio") or "").strip()
                try:
                    pe = float(pe_str)
                    if 0 < pe < 500:   # exclude negatives and absurd outliers
                        pe_values.append(pe)
                except ValueError:
                    pass
        else:  # TWSE
            resp = requests.get(
                _TWSE_PE_URL, timeout=_REQUEST_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0",
                         "Accept": "application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()
            fields = payload.get("fields", [])
            pe_idx = next(
                (i for i, f in enumerate(fields) if "本益比" in f), None
            )
            if pe_idx is not None:
                for row in payload.get("data", []):
                    try:
                        pe = float(row[pe_idx])
                        if 0 < pe < 500:
                            pe_values.append(pe)
                    except (ValueError, IndexError):
                        pass
    except Exception as exc:
        logger.warning("[VALUATION] %s 市場 P/E 抓取失敗：%s", exchange, exc)
        return None

    if not pe_values:
        return None

    pe_values.sort()
    mid    = len(pe_values) // 2
    median = (pe_values[mid - 1] + pe_values[mid]) / 2 if len(pe_values) % 2 == 0 else pe_values[mid]

    logger.info("[VALUATION] %s 市場中位數 P/E = %.1f（樣本 %d 檔）",
                exchange, median, len(pe_values))
    return median


def get_valuation_data(bare_code: str, exchange: str = "TPEX") -> dict:
    """
    Aggregate valuation inputs for an auction/IPO stock.

    EPS source priority:
      1. tpex_esb_eps_rank     — 興櫃 (pre-IPO ESB) filings
      2. yfinance trailingEps  — fallback for already-listed TWSE/TPEX stocks
                                 (e.g. 增資 events for main-board companies)

    Reference price source priority:
      1. tpex_esb_latest_statistics — 2-day avg for 興櫃 stocks
      2. yfinance fast_info          — fallback for listed stocks

    Market P/E:
      Median of all listed OTC/TWSE companies (_fetch_market_median_pe).

    Args:
        bare_code: Bare numeric code without exchange suffix, e.g. '6983'.
        exchange:  'TPEX' or 'TWSE' — determines which P/E median to use.

    Returns:
        Dict with keys (all may be None if data unavailable):
          eps          – TTM EPS (float)
          ref_price    – market price proxy (float)
          market_pe    – exchange median P/E (float)
          price_high   – today's session high (float)
          price_low    – today's session low (float)
          data_date    – date string of the price data
    """
    import yfinance as _yf

    result: dict = {
        "eps":        None,
        "ref_price":  None,
        "market_pe":  None,
        "price_high": None,
        "price_low":  None,
        "data_date":  None,
    }

    # ── EPS: Priority 1 — TPEX ESB filing ────────────────────────────────────
    try:
        resp = requests.get(
            _TPEX_ESB_EPS_URL, timeout=_REQUEST_TIMEOUT,
            headers={"Accept": "application/json",
                     "User-Agent": "AlphaStrategist/1.2"},
        )
        if resp.ok and "json" in resp.headers.get("Content-Type", ""):
            for row in resp.json():
                if (row.get("SecuritiesCompanyCode") or "").strip() == bare_code:
                    try:
                        result["eps"] = float(row["EPS"])
                    except (ValueError, KeyError):
                        pass
                    break
    except Exception as exc:
        logger.warning("[VALUATION] %s EPS 抓取失敗：%s", bare_code, exc)

    # ── EPS: Priority 2 — yfinance trailingEps (listed stocks) ───────────────
    if result["eps"] is None:
        suffix  = ".TW" if exchange == "TWSE" else ".TWO"
        yf_log  = logging.getLogger("yfinance")
        yf_prev = yf_log.level
        yf_log.setLevel(logging.CRITICAL)
        try:
            info = _yf.Ticker(f"{bare_code}{suffix}").info
            eps  = info.get("trailingEps")
            if eps is not None:
                result["eps"] = float(eps)
                logger.info("[VALUATION] %s EPS from yfinance: %.2f", bare_code, result["eps"])
        except Exception:
            pass
        finally:
            yf_log.setLevel(yf_prev)

    # ── Reference price (興櫃 2-day average) ──────────────────────────────────
    try:
        resp2 = requests.get(
            _TPEX_ESB_PRICE_URL, timeout=_REQUEST_TIMEOUT,
            headers={"Accept": "application/json",
                     "User-Agent": "AlphaStrategist/1.2"},
        )
        if resp2.ok and "json" in resp2.headers.get("Content-Type", ""):
            for row in resp2.json():
                if (row.get("SecuritiesCompanyCode") or "").strip() == bare_code:
                    try:
                        today_avg = float(row.get("Average") or 0)
                        prev_avg  = float(row.get("PreviousAveragePrice") or 0)
                        if today_avg > 0 and prev_avg > 0:
                            result["ref_price"] = (today_avg + prev_avg) / 2
                        elif today_avg > 0:
                            result["ref_price"] = today_avg
                        result["price_high"] = float(row.get("Highest") or 0) or None
                        result["price_low"]  = float(row.get("Lowest")  or 0) or None
                        result["data_date"]  = _roc_to_iso(row.get("Date", ""))
                    except (ValueError, KeyError):
                        pass
                    break
    except Exception as exc:
        logger.warning("[VALUATION] %s 興櫃價格抓取失敗：%s", bare_code, exc)

    # ── Reference price: Priority 2 — yfinance (listed stocks) ──────────────
    if result["ref_price"] is None:
        suffix = ".TW" if exchange == "TWSE" else ".TWO"
        px = _price_provider.get_current_price(f"{bare_code}{suffix}")
        if px and px > 0:
            result["ref_price"] = px
            logger.info("[VALUATION] %s ref_price from yfinance: %.2f",
                        bare_code, result["ref_price"])

    # ── Market median P/E ─────────────────────────────────────────────────────
    result["market_pe"] = _fetch_market_median_pe(exchange)

    return result


def _filter_expired_events(events: list[dict], today) -> list[dict]:
    """
    Remove auction events that are no longer actionable:
      - IPO 競拍: drop if bid_deadline < today
      - All others: drop if event date < today
    """
    result = []
    for evt in events:
        kind = evt.get("kind", "")
        if kind == "IPO 競拍":
            deadline_raw = (evt.get("bid_deadline") or "").strip()
            if deadline_raw:
                try:
                    deadline = datetime.strptime(deadline_raw, "%Y/%m/%d").date()
                    if deadline < today:
                        continue
                except ValueError:
                    pass
        else:
            date_raw = (evt.get("date") or "").strip()
            if date_raw and date_raw != "N/A":
                try:
                    evt_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
                    if evt_date < today:
                        continue
                except ValueError:
                    pass
        result.append(evt)
    return result


def get_tw_auctions() -> list[dict]:
    """
    Return upcoming TW 現金增資 (rights offering) events for ARBITRAGE detection.

    Priority chain:
      1. data/auctions.json       — manual override (highest priority)
           Use this to add 興櫃 events or anything not covered by live APIs.
           See data/auctions.json.example for format.
      2. data/auctions_cache.json — 24-hour live-fetch cache
      3. Live fetch from five sources (results merged, deduped by ticker+date):
           • TWSE TWT48U_ALL              — 上市股票除權除息預告表 (listed, 現金增資)
           • TPEX tpex_exright_prepost    — 上櫃股票除權息預告表 (OTC, 現金增資)
           • TWSE newlisting              — 最近上市公司 (upcoming IPO 承銷, fixed-price)
           • TPEX tpex_esb_applicant_companies — 申請上櫃公司 (upcoming OTC IPO 承銷)
           • TWSE auction API             — 競價拍賣公告 (IPO 競拍, covers TWSE+TPEX+創新板)

    IPO 承銷 events use kind='IPO 承銷', date=ApprovedListingDate (first trading day).
    IPO 競拍 events use kind='IPO 競拍', date=撥券日期 (listing/delivery date).
    Window for IPO events: [today - 30d, today + 90d].

    Coverage note: 興櫃 negotiated-price events have no public API source.
    Add them manually in data/auctions.json when needed.

    Returns:
        List of event dicts (keys: ticker, name, kind, exchange, date, floor_price).
        Empty list if all sources fail or return no data.
    """
    # ── Priority 1: Manual override ──────────────────────────────────────────
    if _AUCTIONS_FILE.exists():
        try:
            text = _AUCTIONS_FILE.read_text(encoding="utf-8").strip()
            if text:
                events = _json.loads(text)
                if isinstance(events, list):
                    valid = [
                        {
                            "ticker":      str(e.get("ticker", "")).strip(),
                            "name":        str(e.get("name",        "")),
                            "kind":        str(e.get("kind",        "競拍")),
                            "exchange":    str(e.get("exchange",    "TWSE")),
                            "date":        str(e.get("date",        "N/A")),
                            "floor_price": str(e.get("floor_price", "N/A")),
                        }
                        for e in events
                        if isinstance(e, dict) and e.get("ticker")
                    ]
                    logger.info(
                        "[TW_AUCTION] 手動覆蓋檔案 %s：%d 筆事件", _AUCTIONS_FILE, len(valid)
                    )
                    return valid
        except Exception as exc:
            logger.warning(
                "[TW_AUCTION] 手動覆蓋檔案讀取失敗：%s，降級至 Live API", exc
            )

    # ── Priority 2: 24-hour cache ─────────────────────────────────────────────
    if _AUCTIONS_CACHE_FILE.exists():
        try:
            cache = _json.loads(_AUCTIONS_CACHE_FILE.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(cache["fetched_at"])
            if datetime.now() - fetched_at < timedelta(hours=_CACHE_TTL_HOURS):
                cached_events = cache.get("events", [])
                today = datetime.now().date()
                cached_events = _filter_expired_events(cached_events, today)
                logger.info(
                    "[TW_AUCTION] 使用快取（更新：%s，%d 筆）",
                    fetched_at.strftime("%Y-%m-%d %H:%M"), len(cached_events),
                )
                return cached_events
            logger.info("[TW_AUCTION] 快取已逾 %dh，重新抓取...", _CACHE_TTL_HOURS)
        except Exception:
            logger.info("[TW_AUCTION] 快取讀取失敗，重新抓取...")

    # ── Priority 3: Live fetch (five sources, deduped) ───────────────────────
    twse_rights  = _fetch_twse_ex_rights()
    tpex_rights  = _fetch_tpex_ex_rights()
    twse_ipo     = _fetch_twse_ipo_listings()
    tpex_ipo     = _fetch_tpex_ipo_listings()
    competitive  = _fetch_twse_competitive_auctions()

    # Merge; competitive auctions win over fixed-price IPO listings for same ticker+date
    # (listed last so they overwrite via the seen-set — but we want competitive to WIN,
    #  so put them first; the seen-set keeps the first occurrence of each key)
    seen: set[tuple[str, str]] = set()
    events: list[dict] = []
    for evt in competitive + twse_rights + tpex_rights + twse_ipo + tpex_ipo:
        key = (evt["ticker"], evt["date"])
        if key not in seen:
            seen.add(key)
            events.append(evt)

    logger.info(
        "[TW_AUCTION] 合併後共 %d 筆事件"
        "（競拍: %d + TWSE 增資: %d + TPEX 增資: %d + TWSE IPO: %d + TPEX IPO: %d）",
        len(events),
        len(competitive), len(twse_rights), len(tpex_rights), len(twse_ipo), len(tpex_ipo),
    )

    # Write cache (even if empty — avoids hammering the API on repeated failures)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _AUCTIONS_CACHE_FILE.write_text(
            _json.dumps(
                {"fetched_at": datetime.now().isoformat(), "events": events},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("[TW_AUCTION] 快取已寫入 %s", _AUCTIONS_CACHE_FILE)
    except Exception as exc:
        logger.warning("[TW_AUCTION] 快取寫入失敗：%s", exc)

    return events


def get_target_news(ticker: str, max_items: int = 5) -> list[str]:
    """
    取得單一標的近期重大新聞標題列表（來源：yfinance）。

    Args:
        ticker:    股票代號（'NVDA'、'2330.TW'）。
        max_items: 最多回傳幾則新聞，預設 5。

    Returns:
        新聞標題字串列表；失敗或無資料時回傳 []。
    """
    try:
        news_items = yf.Ticker(ticker).news or []
        headlines: list[str] = []
        for item in news_items[:max_items]:
            # yfinance v0.2+ 的結構因版本而異，相容兩種格式
            title = (
                item.get("title")
                or item.get("content", {}).get("title", "")
                if isinstance(item, dict) else ""
            )
            if title:
                headlines.append(str(title))
        return headlines
    except Exception as exc:
        logger.warning("[NEWS] %s 新聞取得失敗：%s", ticker, exc)
        return []
