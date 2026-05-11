"""
Alpha Strategist — Performance Audit (V1.0)

Retrospectively evaluates the quality of swap recommendations by comparing
the price change of "SELL" tickers vs "BUY" tickers since the day the
advice was recorded in the JSONL archive.

Runs as a weekly APScheduler job (Sunday 09:00 Asia/Taipei) and pushes a
concise Slack report that answers two questions:

  1. Were the swap calls directionally correct?
     (Did the recommended BUY outperform the recommended SELL since the
     advice date?)

  2. Are our efficiency score weights (0.7 growth / 0.3 stability) effective?
     (Is a higher score at advice time correlated with better subsequent
     realised returns?)

This is the V2.0 "feedback loop" — the data that will eventually drive
automated threshold calibration.

Limitations (V1.0)
────────────────────
• Uses simple price-change % as the return proxy (no dividends, no FX).
• Minimum lookback: 1 day. Results < 5 days are flagged as "too early".
• Score-return correlation is Pearson; with small N it's directional only.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

from src.data_fetcher import DATA_DIR

logger = logging.getLogger(__name__)

_SCAN_HISTORY_DIR    = DATA_DIR / "scan_history"
_MIN_DAYS_FOR_SIGNAL = 5      # fewer days → flagged as "too early to judge"
_TOO_EARLY_LABEL     = "⏳ _資料不足（< 5 交易日）_"


# ── Price fetching ────────────────────────────────────────────────────────────

def _price_change_pct(ticker: str, since_date: str) -> float | None:
    """
    Compute the price return (%) for a ticker from `since_date` to today.

    Args:
        ticker:     Full ticker string (e.g. "NVDA", "2330.TW").
        since_date: ISO date string ("2026-05-01").

    Returns:
        Return in percent (e.g. 7.3 for +7.3 %), or None on failure.
    """
    try:
        hist = yf.Ticker(ticker).history(start=since_date, auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return None
        price_then = float(hist["Close"].iloc[0])
        price_now  = float(hist["Close"].iloc[-1])
        if price_then == 0:
            return None
        return (price_now - price_then) / price_then * 100
    except Exception as exc:
        logger.debug("[AUDIT] %s price_change 失敗：%s", ticker, exc)
        return None


# ── JSONL loader ──────────────────────────────────────────────────────────────

def _load_recent_swaps(lookback_days: int) -> list[dict]:
    """
    Read swap records from the last N days of JSONL archive files.

    Each returned dict contains:
      date, market, from_ticker, to_ticker,
      sell_score, buy_score, score_delta

    Args:
        lookback_days: Number of past days to scan.

    Returns:
        List of swap record dicts.
    """
    today     = datetime.now(timezone.utc).date()
    all_swaps: list[dict] = []

    for day_offset in range(lookback_days):
        from datetime import timedelta
        date      = today - timedelta(days=day_offset)
        jsonl_path = _SCAN_HISTORY_DIR / f"{date}.jsonl"
        if not jsonl_path.exists():
            continue
        try:
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                for swap in rec.get("swaps", []):
                    all_swaps.append({
                        "date":        str(date),
                        "market":      rec.get("market", "?"),
                        "from_ticker": swap["from_ticker"],
                        "to_ticker":   swap["to_ticker"],
                        "sell_score":  swap.get("sell_score"),
                        "buy_score":   swap.get("buy_score"),
                        "score_delta": swap.get("score_delta"),
                    })
        except Exception as exc:
            logger.warning("[AUDIT] %s 讀取失敗：%s", jsonl_path, exc)

    logger.info("[AUDIT] 讀取 %d 筆換倉建議（過去 %d 天）", len(all_swaps), lookback_days)
    return all_swaps


# ── Correlation helper ────────────────────────────────────────────────────────

def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation coefficient, or None if undefined."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num    = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denom  = math.sqrt(
        sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)
    )
    return num / denom if denom else None


# ── Public API ────────────────────────────────────────────────────────────────

def run_weekly_audit(lookback_days: int = 7) -> str:
    """
    Generate a weekly performance audit report as a Slack mrkdwn string.

    For each swap suggestion recorded in the past N days:
      • Fetch price change for SELL and BUY tickers since advice date.
      • Determine if BUY outperformed SELL (correct call).
      • Compute avg outperformance and score-return correlation.

    Args:
        lookback_days: Days of history to review (default 7).

    Returns:
        Formatted Slack mrkdwn string. Returns a brief "no data" message
        if the archive contains no swaps for the period.
    """
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
    swaps    = _load_recent_swaps(lookback_days)

    if not swaps:
        return (
            f"*📊 WARDEN WEEKLY AUDIT — {now_str}*\n"
            f"過去 {lookback_days} 天內無換倉建議紀錄。"
            f"（Archive 尚在累積中，或本期無高效差標的）"
        )

    # ── Evaluate each swap ───────────────────────────────────────────────────
    results: list[dict] = []
    for s in swaps:
        days_since = (datetime.now(timezone.utc).date() -
                      datetime.fromisoformat(s["date"]).date()).days

        sell_ret = _price_change_pct(s["from_ticker"], s["date"])
        buy_ret  = _price_change_pct(s["to_ticker"],   s["date"])

        if sell_ret is None or buy_ret is None:
            verdict = "⚠️ _資料不完整_"
            outperform: float | None = None
        elif days_since < _MIN_DAYS_FOR_SIGNAL:
            verdict    = _TOO_EARLY_LABEL
            outperform = buy_ret - sell_ret
        else:
            outperform = buy_ret - sell_ret
            verdict    = "✅ 正確" if outperform > 0 else "❌ 反向"

        results.append({**s, "sell_ret": sell_ret, "buy_ret": buy_ret,
                        "outperform": outperform, "verdict": verdict,
                        "days_since": days_since})

    # ── Aggregate stats ──────────────────────────────────────────────────────
    judged   = [r for r in results if r["outperform"] is not None
                and r["days_since"] >= _MIN_DAYS_FOR_SIGNAL]
    correct  = sum(1 for r in judged if r["outperform"] > 0)
    hit_rate = correct / len(judged) if judged else None
    avg_out  = (sum(r["outperform"] for r in judged) / len(judged)) if judged else None

    # Score-return correlation
    score_xs = [r["score_delta"] for r in judged
                if r["score_delta"] is not None and r["outperform"] is not None]
    ret_ys   = [r["outperform"] for r in judged
                if r["score_delta"] is not None and r["outperform"] is not None]
    corr     = _pearson(score_xs, ret_ys)

    # ── Build report ─────────────────────────────────────────────────────────
    lines: list[str] = [
        f"*📊 WARDEN WEEKLY AUDIT — {now_str}*",
        f"回顧期間：過去 *{lookback_days}* 天  |  換倉建議筆數：*{len(swaps)}*  |  可評估：*{len(judged)}*",
        "",
    ]

    if judged:
        hr_str  = f"{hit_rate:.0%}" if hit_rate is not None else "N/A"
        avg_str = f"{avg_out:+.1f}%" if avg_out is not None else "N/A"
        cr_str  = f"{corr:.2f}" if corr is not None else "N/A（樣本數不足）"

        lines += [
            f"*📈 勝率（BUY 跑贏 SELL）：{hr_str}*  |  平均超額報酬：*{avg_str}*",
            f"*🔢 效率分數 ↔ 報酬相關性（Pearson r）：{cr_str}*",
            "_r > 0.5 表示評分系統有效；r < 0 表示權重需要重新校正_",
            "",
            "*換倉逐筆回顧：*",
        ]

        for r in results:
            sell_r_str = f"{r['sell_ret']:+.1f}%" if r["sell_ret"] is not None else "N/A"
            buy_r_str  = f"{r['buy_ret']:+.1f}%"  if r["buy_ret"]  is not None else "N/A"
            out_str    = f"{r['outperform']:+.1f}%" if r["outperform"] is not None else "—"
            lines.append(
                f"• {r['verdict']}  `{r['from_ticker']}` {sell_r_str} → `{r['to_ticker']}` {buy_r_str}"
                f"  超額 *{out_str}*  _(Δscore={r['score_delta']:.2f}, {r['date']})_"
            )
    else:
        lines.append("_目前可評估筆數為 0（建議需至少 5 個交易日後才能判斷）。_")

    lines += ["", "⚠️ _Human-in-the-Loop — 歷史績效不代表未來表現。_"]
    return "\n".join(lines)
