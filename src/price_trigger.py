"""
Alpha Strategist — Intraday Price Trigger (V1.0)

Lightweight real-time monitor that fires a Slack alert when any portfolio
holding drops more than a configurable threshold in a single session.

Designed to run as a 15-minute APScheduler job during market hours so the
operator is notified of crashes before the next scheduled daily scan.

Market hours (Asia/Taipei)
───────────────────────────
  TW (TWSE/TPEX) : 09:00 – 13:30
  US (NYSE/NASDAQ): 21:30 – 04:00+1 (EDT offset; DST-adjusted by yfinance)

Price source: yfinance fast_info (last_price vs previous_close).
Note: yfinance data may have a 15-minute delay for US stocks on free tier.
This is acceptable for a ≥5 % drop alert — precision to the minute is
not required for this use case.
"""

from __future__ import annotations

import logging
from datetime import datetime, time

import pytz
import yfinance as yf

from src.data_fetcher import DATA_DIR, ScanResult, get_market_data, get_portfolio

logger = logging.getLogger(__name__)

_DEFAULT_DROP_THRESHOLD = 5.0     # percent
_TW_TZ   = pytz.timezone("Asia/Taipei")
_US_TZ   = pytz.timezone("America/New_York")
_TW_OPEN = time(9, 0)
_TW_CLOSE = time(13, 30)
_US_OPEN  = time(9, 30)
_US_CLOSE = time(16, 0)


# ── Market hours detection ────────────────────────────────────────────────────

def _tw_market_open() -> bool:
    now = datetime.now(_TW_TZ)
    return now.weekday() < 5 and _TW_OPEN <= now.time() <= _TW_CLOSE


def _us_market_open() -> bool:
    now = datetime.now(_US_TZ)
    return now.weekday() < 5 and _US_OPEN <= now.time() <= _US_CLOSE


def any_market_open() -> bool:
    """Return True if TW or US market is currently in session."""
    return _tw_market_open() or _us_market_open()


# ── Price check ───────────────────────────────────────────────────────────────

def scan_intraday_drops(
    tickers:       list[str],
    threshold_pct: float = _DEFAULT_DROP_THRESHOLD,
) -> list[dict]:
    """
    Fetch latest price vs previous close for each ticker and flag drops.

    Only checks tickers whose exchange is currently open (TW or US).
    Tickers that fail to fetch are silently skipped.

    Args:
        tickers:       List of ticker strings (e.g. ["NVDA", "2330.TW"]).
        threshold_pct: Single-session drop that triggers an alert (default 5 %).

    Returns:
        List of alert dicts sorted by change_pct ascending (worst first):
          ticker, current_price, prev_close, change_pct, drop_str
    """
    tw_open = _tw_market_open()
    us_open = _us_market_open()

    if not tw_open and not us_open:
        logger.debug("[TRIGGER] 目前非市場交易時段，跳過盤中掃描。")
        return []

    alerts: list[dict] = []

    yf_log = logging.getLogger("yfinance")
    prev_level = yf_log.level
    yf_log.setLevel(logging.CRITICAL)  # suppress yfinance noise

    try:
        for ticker in tickers:
            is_tw = ticker.upper().endswith((".TW", ".TWO"))

            # Skip if that market isn't open
            if is_tw and not tw_open:
                continue
            if not is_tw and not us_open:
                continue

            try:
                fi          = yf.Ticker(ticker).fast_info
                current     = getattr(fi, "last_price",      None)
                prev_close  = getattr(fi, "previous_close",  None)

                if current is None or prev_close is None or prev_close == 0:
                    continue

                current    = float(current)
                prev_close = float(prev_close)
                change_pct = (current - prev_close) / prev_close * 100

                if change_pct <= -threshold_pct:
                    alerts.append({
                        "ticker":        ticker,
                        "current_price": current,
                        "prev_close":    prev_close,
                        "change_pct":    change_pct,
                        "drop_str":      f"{change_pct:.1f}%",
                    })
            except Exception as exc:
                logger.debug("[TRIGGER] %s 價格取得失敗：%s", ticker, exc)
    finally:
        yf_log.setLevel(prev_level)

    alerts.sort(key=lambda a: a["change_pct"])
    logger.info(
        "[TRIGGER] 盤中掃描完成 — %d 標的，%d 觸發（閾值 −%.1f%%）",
        len(tickers), len(alerts), threshold_pct,
    )
    return alerts


# ── Formatting ────────────────────────────────────────────────────────────────

def format_drop_alert(alerts: list[dict], threshold_pct: float = _DEFAULT_DROP_THRESHOLD) -> str:
    """
    Format intraday drop alerts as a Slack mrkdwn string.

    Args:
        alerts:        Output of scan_intraday_drops().
        threshold_pct: Threshold used (shown in header).

    Returns:
        Slack mrkdwn string. Empty string if alerts is empty.
    """
    if not alerts:
        return ""

    now_str = datetime.now(_TW_TZ).strftime("%H:%M")
    lines   = [
        f"🚨 *INTRADAY ALERT — {now_str}*  (跌幅 ≥ {threshold_pct:.0f}%)",
        "",
    ]
    for a in alerts:
        lines.append(
            f"• `{a['ticker']}`  *{a['drop_str']}*  "
            f"現價 {a['current_price']:.2f}  ←  昨收 {a['prev_close']:.2f}"
        )
    lines += [
        "",
        "⚠️ _Human-in-the-Loop — 請確認是否需要提前操作。_",
    ]
    return "\n".join(lines)


# ── Scheduler job ─────────────────────────────────────────────────────────────

def intraday_scan_job(
    threshold_pct: float = _DEFAULT_DROP_THRESHOLD,
) -> list[dict]:
    """
    Top-level function called by APScheduler every 15 minutes.

    Loads current portfolio, scans for intraday drops, and returns
    alert list. Caller (main.py) handles Slack delivery.

    Returns:
        List of alert dicts from scan_intraday_drops, or [] if no market open
        or no drops found.
    """
    try:
        portfolio = get_portfolio(DATA_DIR)
        df        = portfolio["df"]
        if df.empty:
            return []
        tickers = list(dict.fromkeys(df["Ticker"].tolist()))
        return scan_intraday_drops(tickers, threshold_pct=threshold_pct)
    except Exception as exc:
        logger.error("[TRIGGER] intraday_scan_job 異常：%s", exc)
        return []
