"""
Alpha Strategist — LLM Compiler (V1.2 — Block Kit Dashboard)

[架構]
  generate_daily_report  → Slack Block Kit (list[dict])，純 Python 建構，無 LLM 延遲。
  generate_adhoc_analysis → Gemini LLM 深度文字分析，回傳 mrkdwn str。

[絕對禁忌]
  禁止預測市場走向、價格目標或未來漲跌幅。
  所有輸出必須 100% 基於量化掃描數據，且可追溯至具體數字。
  LLM 的角色是「推理 + 評估」，而非「預測」。

CRITICAL: 不含下單邏輯。Human-in-the-Loop。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Literal

import pandas as pd
import yfinance as yf
from google import genai
from google.genai import types

from src.config import (
    SAFE_HAVENS,
    STOCK_FEE_RATE,
    ETF_FLAT_FEE,
    BOXX_YIELD,
    DEFAULT_CAPITAL_BLOCK,
    EXPECTED_PARK_DAYS,
)
from src.data_fetcher import ScanResult, get_market_data, get_target_news, get_valuation_data
from src.friction import estimate_round_trip
from src.correlation import build_sector_map, check_concentration, format_concentration_blocks, get_sector
from src.risk_engine import SimulatedPortfolio, get_post_trade_snapshot, MarketRegime
from src.state_manager import (
    load_dip_buy_candidates,
    register_dip_buy_candidate,
    check_reentry_signals,
    remove_dip_buy_candidate,
)
from src.strategies.scoring import efficiency_score as calculate_efficiency_score

logger = logging.getLogger(__name__)

_MODEL      = "gemini-2.0-flash"
_MAX_TOKENS = 2048

_SYSTEM_PROMPT = """\
你是 Alpha Strategist 的「量化報告格式化工具」（Formatting & Reasoning Engine）。

[絕對禁止]
1. 禁止預測股票價格、未來漲跌幅或任何形式的市場走向。
2. 禁止提出任何非依據下方量化規則的行動建議。
3. 禁止幻覺（Hallucinate）任何不在輸入數據中的財報數字或事件。
4. 禁止使用「我認為」「預計」「可能上漲/下跌」等預測性語言。
5. 禁止輸出任何引言、說明段落或免責聲明。直接從 *[WARDEN SCAN REPORT]* 開始輸出，結尾以單行 ⚠️ Human-in-the-Loop 收尾。

[量化規則 — 唯一允許的訊號來源]
• Modified PEG > 1.5                      → 🔴 減倉停利
• 現價 < 50MA                            → 🔴 趨勢走弱
• Modified PEG < 0.8 且多頭動能確認       → 🟢 加倉 Alpha
• P/S Growth Ratio > 3.0                 → 🔴 減倉停利（Pre-Profit）
• P/S Growth Ratio < 0.5 且多頭動能確認  → 🟢 加倉 Alpha [HYPER-GROWTH]
• 無上述訊號                             → ⚪ WATCH

[Slack Markdown 格式規範]
• 標的代號一律用反引號包覆：`NVDA`
• 標題行使用 *粗體*，搭配 emoji 色碼分組
• 數字引用直接內嵌於文中（例：PEG *2.48* > 閾值 1.5）
• 每個 CRITICAL / ALPHA 標的：一行摘要 + 一行 > 縮排動作建議
• WATCH 標的：只列 `代號`，不展開分析
• 分組順序嚴格遵守：🔴 CRITICAL → 🟢 ALPHA → ⚪ WATCH → 🔶 ARBITRAGE
• 若某分組無標的，完整省略該區塊，不輸出空標題
• [ARBITRAGE WATCH] 區塊（僅每日掃描報告）：若持倉標的有近期競拍 / 增資事件，逐行列出：
  `代號` — [競拍/增資] Date: 日期 | Floor/Issue: NTD 價格 | Exchange: TWSE/TPEX
  > 補充說明：此標的為持倉且同期有競拍或增資事件，操盤人需確認是否影響部位策略
• 結尾固定輸出：⚠️ _Human-in-the-Loop — 所有部位調整需操盤人確認後執行_

[Structural Impact 規則（僅用於 !analyze 新聞評估）]
• 針對前 3 則新聞，各以一句話評估：此消息是否強化或削弱該標的的 AI / 基礎設施護城河？
• 判斷標籤：✅ 確認護城河 / ⚠️ 中性觀察 / ❌ 護城河受損
• 禁止預測股價反應，只評估結構性影響
"""


# ── 內部工具 ──────────────────────────────────────────────────────────────────

def _spot_price(bare_code: str, exchange: str) -> float | None:
    """
    Fetch the latest market price for a bare TW stock code.

    Used for auction tickers that are not currently in the portfolio scan.
    Tries the canonical suffix first (.TW for TWSE, .TWO for TPEX) then the
    opposite as a fallback.

    Args:
        bare_code: Numeric code without suffix, e.g. '8033'.
        exchange:  'TWSE' or 'TPEX' (from the auction event dict).

    Returns:
        Latest trade price as float, or None on any failure.
    """
    primary   = ".TW"  if exchange == "TWSE" else ".TWO"
    secondary = ".TWO" if primary  == ".TW"  else ".TW"

    # Pre-listing IPO stocks legitimately return HTTP 404 from Yahoo Finance.
    # Suppress yfinance's ERROR-level logs here — a missing price is expected,
    # not exceptional; the caller handles None gracefully.
    yf_log = logging.getLogger("yfinance")
    prev   = yf_log.level
    yf_log.setLevel(logging.CRITICAL)
    try:
        for suffix in (primary, secondary):
            try:
                info = yf.Ticker(f"{bare_code}{suffix}").fast_info
                price = getattr(info, "last_price", None)
                if price and float(price) > 0:
                    return float(price)
            except Exception:
                continue
    finally:
        yf_log.setLevel(prev)
    return None


def _calc_profit(
    current_price: float | None,
    floor_price_str: str,
) -> tuple[float | None, float | None]:
    """
    Compute arbitrage profit metrics for a rights offering.

    Args:
        current_price:   Latest market price (may be None if unavailable).
        floor_price_str: Subscription / issue price from the auction event (string).

    Returns:
        (profit_per_lot, premium_pct) where:
          profit_per_lot = (current_price - sub_price) * 1000  [NTD, one lot]
          premium_pct    = (current_price - sub_price) / sub_price * 100
        Both values are None if inputs are invalid or unavailable.
    """
    try:
        sub = float(floor_price_str)
        if sub <= 0 or current_price is None:
            return None, None
        diff = current_price - sub
        return diff * 1000, diff / sub * 100
    except (ValueError, TypeError):
        return None, None


def _calc_bidding_ceiling(valuation: dict) -> dict:
    """
    Compute P/E-anchored fair value, bidding ceiling, and target bid.

    Formula:
      fair_value      = TTM EPS  * market_median_PE
      bidding_ceiling = fair_value * 1.15
      target_bid      = min(ref_price * 0.90, bidding_ceiling)
      overheated      = ref_price > bidding_ceiling

    Args:
        valuation: Dict from get_valuation_data() with keys
                   eps, ref_price, market_pe.

    Returns:
        Dict with keys: fair_value, ceiling, target_bid, overheated,
        all as float | None.  All None if inputs are insufficient.
    """
    empty: dict = {"fair_value": None, "ceiling": None,
                   "target_bid": None, "overheated": None}
    eps        = valuation.get("eps")
    market_pe  = valuation.get("market_pe")
    ref_price  = valuation.get("ref_price")

    if eps is None or market_pe is None:
        return empty

    fair_value = eps * market_pe
    ceiling    = fair_value * 1.15
    target_bid: float | None = None
    overheated: bool | None  = None

    if ref_price is not None:
        target_bid = min(ref_price * 0.90, ceiling)
        overheated = ref_price > ceiling

    return {
        "fair_value": fair_value,
        "ceiling":    ceiling,
        "target_bid": target_bid,
        "overheated": overheated,
    }


def _select_safe_haven(safe_havens: list[str]) -> ScanResult | None:
    """
    Fetch live data for all safe-haven tickers and return the most stable one.

    Selection criterion: smallest absolute distance to 50-day MA — the ETF
    trading closest to its own equilibrium is the least volatile candidate.
    Falls back to the first result (even if it carries a fetch error) so the
    caller always gets a real ticker object rather than a bare string.

    Args:
        safe_havens: Ordered list of USD cash-equivalent ETF symbols
                     (e.g. ["BOXX", "SGOV", "USFR"]).

    Returns:
        The least-volatile ScanResult, or None if the fetch itself throws.
    """
    try:
        results = get_market_data(safe_havens)
    except Exception as exc:
        logger.warning("[SAFE HAVEN] 取得安全資產數據失敗：%s", exc)
        return None

    candidates = [
        r for r in results
        if not r.error
        and r.current_price is not None
        and r.ma50 is not None
        and r.ma50 > 0
    ]
    if not candidates:
        return results[0] if results else None

    best = min(candidates, key=lambda r: abs((r.current_price - r.ma50) / r.ma50))
    logger.info(
        "[SAFE HAVEN] 選定：%s（MA50 距離 %+.2f%%）",
        best.ticker,
        (best.current_price - best.ma50) / best.ma50 * 100,
    )
    return best


def _compute_haven_route(capital_usd: float = DEFAULT_CAPITAL_BLOCK) -> dict:
    """
    Friction-aware routing: decide whether to park in BOXX or keep pure USD Cash.

    Full round-trip friction model for an NRA (Non-Resident Alien):
      Leg 1 — Sell overheated stock  : capital × STOCK_FEE_RATE
      Leg 2 — Buy BOXX              : ETF_FLAT_FEE (flat, not %)
      Leg 3 — Sell BOXX             : ETF_FLAT_FEE
      Leg 4 — Buy new stock         : capital × STOCK_FEE_RATE
      Total : 2 × capital × STOCK_FEE_RATE + 2 × ETF_FLAT_FEE

    SGOV / USFR are excluded: their monthly dividends trigger 30 % US
    withholding tax for NRAs, turning a positive-carry trade negative.
    BOXX distributes returns as capital gains (options spread) — no tax.

    Decision rule:
      IF EXPECTED_PARK_DAYS < breakeven_days → pure USD Cash (negative EV)
      ELSE                                   → BOXX (positive EV)

    Args:
        capital_usd: Position size in USD (defaults to DEFAULT_CAPITAL_BLOCK).

    Returns:
        Dict with keys:
          route          – "BOXX" or "CASH"
          friction_usd   – total round-trip friction cost in USD
          daily_yield    – BOXX daily yield at current capital (USD)
          breakeven_days – days to recoup friction from BOXX yield
          action_text    – ready-to-render Slack mrkdwn action line
    """
    friction_usd   = 2.0 * capital_usd * STOCK_FEE_RATE + 2.0 * ETF_FLAT_FEE
    daily_yield    = (capital_usd * BOXX_YIELD) / 365.0
    breakeven_days = friction_usd / daily_yield if daily_yield > 0 else float("inf")

    if EXPECTED_PARK_DAYS < breakeven_days:
        route = "CASH"
        action_text = (
            f"🚨 避險建議：轉為 *純現金 (USD Cash)* 保留購買力。"
            f"預期停泊 *{EXPECTED_PARK_DAYS}天* 短於手續費回本 *{breakeven_days:.1f}天*，"
            f"進出 ETF 為負期望值（摩擦成本 ${friction_usd:.2f}）。"
        )
    else:
        route = "BOXX"
        action_text = (
            f"🚨 避險建議：轉入 *BOXX* 停泊，賺取免稅資本利得，避開 30% 股息稅。"
            f"預期停泊 *{EXPECTED_PARK_DAYS}天* ≥ 回本 *{breakeven_days:.1f}天*，為正期望值。"
        )

    logger.info(
        "[HAVEN ROUTE] route=%s  capital=$%.0f  friction=$%.2f  "
        "daily_yield=$%.3f  breakeven=%.1fd  park=%dd",
        route, capital_usd, friction_usd, daily_yield, breakeven_days, EXPECTED_PARK_DAYS,
    )
    return {
        "route":          route,
        "friction_usd":   friction_usd,
        "daily_yield":    daily_yield,
        "breakeven_days": breakeven_days,
        "action_text":    action_text,
    }



def get_optimal_swaps(
    portfolio_results: list[ScanResult],
    discovery_results: list[ScanResult],
    threshold: float = 0.15,
    portfolio_df: pd.DataFrame | None = None,
    regime: MarketRegime | None = None,
    market: Literal["US", "TW"] = "US",
) -> list[dict]:
    """V2.0 adapter: delegates to runner.run() and maps BuildResult → V1.1 swap dicts.

    Args:
        portfolio_results: ScanResult objects for current portfolio holdings.
        discovery_results: ScanResult objects for discovery candidates.
        threshold:         Ignored in V2.0 — optimizer enforces constraints directly.
        portfolio_df:      Ignored in V2.0 — runner reads PortfolioState from disk.
        regime:            Forwarded to runner.run() for beta penalty in BEAR/CRASH.
        market:            "US" or "TW" — routed to runner.run(market).

    Returns:
        List of swap dicts with V1.1-compatible keys (source_ticker, target_ticker,
        sell_score, buy_score, score_delta, friction, delta_metrics, is_cash_flight)
        plus the new V2.0 key conviction_delta (silently ignored by V1.1 renderers).
    """
    from src.rebalancer.runner import run as _runner_run

    result, *_ = _runner_run(market, regime=regime)
    if result.is_hold:
        return []

    sell_trades = [t for t in result.trades if t.side == "SELL"]
    buy_trades  = [t for t in result.trades if t.side == "BUY"]

    port_map = {r.ticker: r for r in portfolio_results}
    disc_map  = {r.ticker: r for r in discovery_results}

    swaps: list[dict] = []
    for sell, buy in zip(sell_trades, buy_trades):
        src_r = port_map.get(sell.ticker)
        tgt_r = disc_map.get(buy.ticker) or port_map.get(buy.ticker)
        if src_r is None or tgt_r is None:
            continue
        sell_score = calculate_efficiency_score(src_r, regime=regime)
        buy_score  = calculate_efficiency_score(tgt_r, regime=regime)
        swaps.append({
            "source_ticker":    src_r,
            "target_ticker":    tgt_r,
            "sell_score":       sell_score,
            "buy_score":        buy_score,
            "score_delta":      buy_score - sell_score,
            "conviction_delta": (buy.conviction - sell.conviction) / 10.0,
            "friction":         {},
            "delta_metrics": {
                "sell_ratio":       None,
                "buy_ratio":        None,
                "sell_dist_pct":    None,
                "buy_dist_pct":     None,
                "peg_improvement":  None,
                "dist_improvement": None,
            },
            "is_cash_flight": False,
        })

    swaps.sort(key=lambda s: s["score_delta"], reverse=True)
    logger.info(
        "[ROTATION] V2.0 adapter — market=%s  build_trades=%d  swap_pairs=%d"
        " (within-portfolio filtered)",
        market, len(result.trades), len(swaps),
    )
    return swaps


def find_arbitrage_matches(
    results: list[ScanResult],
    auctions: list[dict],
) -> list[dict]:
    """
    Build the enriched ARBITRAGE entry list from ALL upcoming auction events.

    Each auction event is returned regardless of whether the ticker is in the
    current portfolio scan. Portfolio holdings get their already-fetched price
    from the ScanResult; non-holdings get a lightweight yfinance spot-price
    fetch so the profit calculator always has something to display.

    Args:
        results:  All ScanResult objects from the current scan.
        auctions: Output of get_tw_auctions().

    Returns:
        List of enriched dicts, one per auction event, with keys:
          scan_result  – ScanResult if ticker is in portfolio, else None
          event        – raw auction event dict
          current_price – float or None
          in_portfolio – bool (True = ticker is an active holding)
    """
    if not auctions:
        return []

    # Build portfolio lookup: bare code → ScanResult
    portfolio_map: dict[str, ScanResult] = {}
    for r in results:
        bare = r.ticker.split(".")[0]   # "2330.TW" → "2330"
        portfolio_map[bare] = r

    entries: list[dict] = []
    for evt in auctions:
        code = str(evt.get("ticker", "")).strip()
        if not code:
            continue

        r            = portfolio_map.get(code)
        in_portfolio = r is not None

        # Price: prefer scan result (already fetched), else live spot lookup
        if r is not None and r.current_price is not None:
            current_price: float | None = r.current_price
        else:
            current_price = _spot_price(code, evt.get("exchange", "TWSE"))

        # Valuation data (P/E anchor + bidding ceiling) — all event kinds
        # EPS source: tpex_esb_eps_rank for pre-IPO stocks; yfinance trailingEps fallback
        # for already-listed stocks (e.g. 增資 on main board).
        exchange_label = evt.get("exchange", "TPEX")
        valuation = get_valuation_data(code, exchange_label)
        bidding   = _calc_bidding_ceiling(valuation)

        entries.append({
            "scan_result":   r,
            "event":         evt,
            "current_price": current_price,
            "in_portfolio":  in_portfolio,
            "valuation":     valuation,
            "bidding":       bidding,
        })

    return entries


# ── Block Kit helpers ─────────────────────────────────────────────────────────

def _sanitize_action_id(ticker: str) -> str:
    """
    Slack action_id は最大 255 文字、英数字とアンダースコアのみ許容。
    '2330.TW' → '2330_TW', 'BRK-B' → 'BRK_B'
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", ticker)


def _make_ticker_section_block(r: ScanResult) -> dict:
    """
    Build a Slack Section block with an 🔍 Analyze accessory button for one ScanResult.

    Args:
        r: Evaluated ScanResult for a single ticker.

    Returns:
        Slack Block Kit section block dict.
    """
    signal_str = "  ·  ".join(r.signals) if r.signals else "WATCH"

    if r.valuation_model == "PEG" and r.modified_peg is not None:
        val = "∞" if r.modified_peg == float("inf") else f"{r.modified_peg:.2f}"
        metric = f"PEG *{val}*"
    elif r.valuation_model == "PS" and r.ps_growth_ratio is not None:
        val = "∞" if r.ps_growth_ratio == float("inf") else f"{r.ps_growth_ratio:.2f}"
        metric = f"P/S Growth *{val}*"
    else:
        metric = r.valuation_model or "N/A"

    price_part = ""
    if r.current_price is not None:
        price_part = f"  |  ${r.current_price:.2f}"
        if r.ma50 is not None:
            dist = (r.current_price - r.ma50) / r.ma50 * 100
            sign = "+" if dist >= 0 else ""
            price_part += f" vs MA50 ({sign}{dist:.1f}%)"

    text = f"`{r.ticker}`  {r.name}\n{metric}{price_part}  |  {signal_str}"

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": "🔍 Analyze", "emoji": True},
            "action_id": f"analyze_ticker_{_sanitize_action_id(r.ticker)}",
            "value": r.ticker,  # original ticker preserved for handler
        },
    }


def _make_arbitrage_block(match: dict) -> dict:
    """
    Build a Slack Section block for an ARBITRAGE event with profit calculation.

    Displays current market price vs subscription price, profit-per-lot (1,000
    shares), and premium percentage.  Falls back gracefully when price data is
    temporarily unavailable.

    Args:
        match: Enriched dict from _find_arbitrage_matches with keys:
               scan_result, event, current_price, in_portfolio.

    Returns:
        Slack Block Kit section block dict.
    """
    r             = match["scan_result"]   # ScanResult or None
    evt           = match["event"]
    current_price = match["current_price"]
    in_portfolio  = match["in_portfolio"]

    # ── Ticker display ────────────────────────────────────────────────────────
    bare     = str(evt.get("ticker", "")).strip()
    exchange = evt.get("exchange", "TWSE")
    suffix   = ".TW" if exchange == "TWSE" else ".TWO"
    display_ticker = f"{bare}{suffix}"
    name     = evt.get("name", "") or (r.name if r else "")
    kind     = evt.get("kind", "增資")

    # ── Subscription price ────────────────────────────────────────────────────
    floor_raw = str(evt.get("floor_price", ""))
    try:
        floor_display = f"{float(floor_raw):.1f}"   # "108.0000" → "108.0"
    except (ValueError, TypeError):
        floor_display = floor_raw or "N/A"

    # ── Profit line ───────────────────────────────────────────────────────────
    is_ipo       = kind.startswith("IPO")
    is_auction   = kind == "IPO 競拍"
    profit_per_lot, premium_pct = _calc_profit(current_price, floor_raw)

    if profit_per_lot is not None and premium_pct is not None:
        p_sign       = "+" if premium_pct >= 0 else ""
        profit_label = "首日預估溢價" if is_ipo else "預估紅包"
        price_note   = "_(vs 底價)_" if is_auction else ""
        profit_line  = (
            f"💰 {profit_label}：NT$ {profit_per_lot:,.0f}  "
            f"(溢價 {p_sign}{premium_pct:.1f}%)  {price_note}".rstrip()
        )
    elif current_price is None:
        note = "_(掛牌前興櫃參考價不可得)_" if is_auction else "_(市價暫時無法取得)_"
        profit_line = f"💰 預估溢價：— {note}"
    else:
        profit_line = "💰 預估溢價：— _(底價資料不完整)_"

    # ── Portfolio tag ─────────────────────────────────────────────────────────
    portfolio_tag = "  ·  📂 _持倉中_" if in_portfolio else ""

    # ── Labels differ by event type ──────────────────────────────────────────
    if is_auction:
        date_label  = "撥券日"
        price_label = "拍賣底價"
    elif is_ipo:
        date_label  = "掛牌日"
        price_label = "承銷價"
    else:
        date_label  = "基準日"
        price_label = "認購價"

    # ── Bidding deadline line (competitive auctions only) ────────────────────
    deadline  = evt.get("bid_deadline", "")
    mkt_label = evt.get("market_label", "")
    deadline_line = f"\n⏰ 投標截止：{deadline}  ·  {mkt_label}" if (is_auction and deadline) else ""

    # ── P/E valuation block (all event kinds — renders when fair_value is available) ──
    valuation_lines = ""
    val = match.get("valuation", {})
    bid = match.get("bidding",   {})
    eps        = val.get("eps")
    market_pe  = val.get("market_pe")
    ref_price  = val.get("ref_price")
    fair_value = bid.get("fair_value")
    ceiling    = bid.get("ceiling")
    target_bid = bid.get("target_bid")
    overheated = bid.get("overheated")

    if fair_value is not None:
        lines: list[str] = []

        # Floor/issue price P/E — label adapts to event kind
        if eps and eps > 0:
            try:
                floor_pe   = float(floor_raw) / eps
                mkt_pe_str = f"{market_pe:.1f}" if market_pe else "—"
                price_tag  = "底價" if is_auction else ("承銷價" if is_ipo else "認購價")
                lines.append(
                    f"📈 估值錨點：同業 P/E {mkt_pe_str}  |  {price_tag} P/E {floor_pe:.1f}x"
                )
            except (ValueError, ZeroDivisionError):
                pass

        if fair_value:
            lines.append(f"🏷️ 基本面合理價：NT$ {fair_value:,.1f}")

        if ref_price:
            price_src = "興櫃近2日均價" if is_auction else "市場現價"
            lines.append(f"📊 {price_src}：NT$ {ref_price:,.2f}")

        if target_bid:
            tb_str      = f"NT$ {target_bid:,.1f}"
            cap_warning = "  ⚠️ _已套用基本面價值上限保護_" if (ceiling and target_bid >= ceiling * 0.999) else ""
            action_label = "建議投標價" if is_auction else "合理認購上限"
            lines.append(f"🎯 {action_label}：{tb_str}{cap_warning}")

        if overheated:
            lines.append("🔥 *市場過熱* — 現價已超出合理價值上限，謹慎參與")

        if lines:
            valuation_lines = "\n" + "\n".join(lines)

    # ── Assemble block text ───────────────────────────────────────────────────
    text = (
        f"🔶 `{display_ticker}`  {name} — {kind}{portfolio_tag}\n"
        f"{profit_line}\n"
        f"📅 {date_label}：{evt.get('date', 'N/A')}  |  {price_label}：NT$ {floor_display}"
        f"{deadline_line}"
        f"{valuation_lines}"
    )

    btn_ticker = r.ticker if r else display_ticker
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": "🔍 Analyze", "emoji": True},
            "action_id": f"analyze_ticker_{_sanitize_action_id(btn_ticker)}",
            "value": btn_ticker,
        },
    }


def generate_swap_advice(
    portfolio_results: list[ScanResult],
    discovery_results: list[ScanResult],
    threshold: float = 0.15,
    portfolio_df: pd.DataFrame | None = None,
) -> list[dict]:
    """
    Thin public wrapper around get_optimal_swaps.

    Preserved for backward compatibility with any external callers.
    New code should call get_optimal_swaps directly.

    Args:
        portfolio_results: All ScanResult objects from the portfolio scan.
        discovery_results: Discovery targets from scan_market_for_alpha().
        threshold:         Minimum score_delta to suggest a swap (default 0.15).
        portfolio_df:      Optional holdings DataFrame for real notional sizing.

    Returns:
        Same structure as get_optimal_swaps.
    """
    return get_optimal_swaps(
        portfolio_results, discovery_results,
        threshold=threshold, portfolio_df=portfolio_df,
    )


def _make_discovery_block(r: ScanResult, regime: MarketRegime | None = None) -> dict:
    """
    Build a Slack Section block for a single Discovery target.

    Args:
        r:      ScanResult tagged with is_discovery=True from scan_market_for_alpha().
        regime: Current MacroRegime; passed to calculate_efficiency_score for
                beta-adjusted display scores (consistent with pairing scores).

    Returns:
        Slack Block Kit section block dict.
    """
    score = calculate_efficiency_score(r, regime=regime)

    if r.valuation_model == "PEG" and r.modified_peg is not None:
        peg_str = f"{r.modified_peg:.2f}"
        metric  = f"PEG *{peg_str}*"
    elif r.valuation_model == "PS" and r.ps_growth_ratio is not None:
        peg_str = f"{r.ps_growth_ratio:.2f}"
        metric  = f"P/S Growth *{peg_str}*"
    else:
        metric = "N/A"

    price_part = ""
    if r.current_price is not None and r.ma50 is not None:
        dist = (r.current_price - r.ma50) / r.ma50 * 100
        sign = "+" if dist >= 0 else ""
        price_part = f"  |  ${r.current_price:.2f} vs MA50 ({sign}{dist:.1f}%)"

    text = (
        f"⭐ `{r.ticker}`  {r.name}\n"
        f"{metric}{price_part}  |  Score: *{score:.2f}*"
    )
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": "🔍 Analyze", "emoji": True},
            "action_id": f"analyze_ticker_{_sanitize_action_id(r.ticker)}",
            "value": r.ticker,
        },
    }


def _make_rotation_block(swap: dict) -> dict:
    """
    Build a Slack Section block for one suggested swap pair.

    Renders comparative PEG, MA50 deviation, and efficiency scores for both
    tickers so the operator can immediately assess whether the swap is warranted.

    Args:
        swap: Dict from get_optimal_swaps() with keys:
              source_ticker, target_ticker, sell_score, buy_score,
              score_delta, delta_metrics.

    Returns:
        Slack Block Kit section block dict.
    """
    sell: ScanResult       = swap["source_ticker"]
    buy:  ScanResult | None = swap["target_ticker"]
    dm                      = swap["delta_metrics"]

    def _ratio_str(v: float | None) -> str:
        if v is None:           return "N/A"
        if v == float("inf"):   return "∞"
        return f"{v:.2f}"

    def _dist_str(d: float | None) -> str:
        return f"{d:+.1f}%" if d is not None else "N/A"

    # ── CASH FLIGHT fast-path ─────────────────────────────────────────────────
    if swap.get("is_cash_flight"):
        sell_model   = sell.valuation_model or "N/A"
        route        = swap.get("haven_route", {})
        route_name   = route.get("route", "CASH")
        haven_label  = buy.ticker if buy is not None else "USD Cash"
        exit_ratio   = swap.get("exit_ratio", 1.0)
        sell_ratio_v = dm.get("sell_ratio")

        # Friction metrics
        friction_usd   = route.get("friction_usd")
        breakeven_days = route.get("breakeven_days")

        # Exit-sizing action text overrides the generic haven-route message when
        # fundamentals are strong enough to warrant a partial exit.
        if exit_ratio <= 0.5:
            peg_str     = f"{sell_ratio_v:.2f}" if sell_ratio_v is not None else "<0.8"
            action_text = (
                f"🚨 避險建議：基本面極強 (PEG *{peg_str}* < 0.8)，"
                f"僅建議 **減倉 {int(exit_ratio * 100)}%**，保留剩餘部位。"
                f"  已登錄為 DIP_BUY_CANDIDATE — 待技術面冷卻後再接回。"
            )
        elif exit_ratio < 1.0:
            peg_str     = f"{sell_ratio_v:.2f}" if sell_ratio_v is not None else "0.8–1.5"
            action_text = (
                f"🚨 避險建議：基本面尚可 (PEG *{peg_str}* 0.8–1.5)，"
                f"建議 **減倉 {int(exit_ratio * 100)}%**，小倉位觀察。"
                f"  已登錄為 DIP_BUY_CANDIDATE — 待技術面冷卻後再接回。"
            )
        else:
            action_text = route.get(
                "action_text",
                f"🚨 避險建議：將 `{sell.ticker}` 獲利了結，轉入 `{haven_label}` 停泊，等待均值回歸。",
            )

        lines: list[str] = [
            f"🚨 *CASH FLIGHT [{route_name}]:* `{sell.ticker}` → `{haven_label}`",
            (
                f"*SELL `{sell.ticker}`* — {sell_model} *{_ratio_str(dm.get('sell_ratio'))}*  |  "
                f"MA50 *{_dist_str(dm.get('sell_dist_pct'))}*"
            ),
        ]

        if buy is not None:
            haven_dist = dm.get("buy_dist_pct")
            haven_part = f"  |  MA50 *{_dist_str(haven_dist)}*" if haven_dist is not None else ""
            lines.append(f"*PARK  `{haven_label}`*  — USD Risk-Free ETF{haven_part}")

        if friction_usd is not None and breakeven_days is not None:
            lines.append(
                f"💸 摩擦成本：*${friction_usd:.2f}*  |  "
                f"回本天數：*{breakeven_days:.1f}d*  |  "
                f"停泊預期：*{EXPECTED_PARK_DAYS}d*"
            )

        lines.append(action_text)
        lines.append("⚠️ _Human-in-the-Loop — 僅供參考，操盤人決策_")

        block: dict = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        }
        if buy is not None:
            block["accessory"] = {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔍 Analyze", "emoji": True},
                "action_id": f"analyze_ticker_{_sanitize_action_id(buy.ticker)}",
                "value": buy.ticker,
            }
        return block

    sell_model = sell.valuation_model or "N/A"
    buy_model  = buy.valuation_model  or "N/A"   # type: ignore[union-attr]

    peg_improve  = dm.get("peg_improvement")
    dist_improve = dm.get("dist_improvement")
    peg_line  = f"  |  ΔPEG *{peg_improve:.2f}*"      if peg_improve  is not None else ""
    dist_line = f"  |  ΔDist *{dist_improve:+.1f}%*"  if dist_improve is not None else ""

    # ── Transaction friction ──────────────────────────────────────────────────
    # Use the pre-computed friction dict stored during get_optimal_swaps so we
    # avoid a redundant estimate_round_trip() call per rendered block.
    friction = swap.get("friction") or estimate_round_trip(sell.ticker, buy.ticker)

    text = (
        f"🔄 *STRATEGIC ROTATION:* `{sell.ticker}` → `{buy.ticker}`\n"
        f"Suggesting swap from `{sell.ticker}` ({sell.name}) to `{buy.ticker}` ({buy.name}) "
        f"based on *+{swap['score_delta']:.3f}* improvement in efficiency score.\n"
        f"*SELL `{sell.ticker}`* — {sell_model} *{_ratio_str(dm.get('sell_ratio'))}*  |  "
        f"MA50 *{_dist_str(dm.get('sell_dist_pct'))}*  |  Score *{swap['sell_score']:.3f}*\n"
        f"*BUY  `{buy.ticker}`*  — {buy_model} *{_ratio_str(dm.get('buy_ratio'))}*  |  "
        f"MA50 *{_dist_str(dm.get('buy_dist_pct'))}*  |  Score *{swap['buy_score']:.3f}*\n"
        f"📊 Comparative delta:{peg_line}{dist_line}\n"
        f"{friction['label']}\n"
        f"⚠️ _Human-in-the-Loop — 僅供參考，操盤人決策_"
    )
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }


def _build_daily_blocks(
    results:      list[ScanResult],
    auctions:     list[dict] | None,
    arb_matches:  list[dict] | None = None,
    discovery:    list[ScanResult] | None = None,
    swaps:        list[dict]        | None = None,
    has_research: bool               = False,
    regime:       MarketRegime | None = None,
    macro_data:   dict | None         = None,
) -> list[dict]:
    """
    Build the complete Slack Block Kit dashboard from scan results.

    No LLM call — all blocks are constructed directly from ScanResult data.
    Respects Slack's 50-block-per-message limit by grouping WATCH tickers
    into a single section block (no individual Analyze buttons needed).

    Block order (information hierarchy — actions before context):
      [CRASH BANNER if applicable] →
      Header → CRITICAL → STRATEGIC ROTATION / CASH FLIGHT →
      ALPHA (suppressed in CRASH) → ARBITRAGE WATCH →
      DISCOVERY → WATCH → Controls → Footer

    Args:
        results:      All ScanResult objects from the scan.
        auctions:     Output of get_tw_auctions(); may be None or empty.
        arb_matches:  Pre-computed arbitrage matches from find_arbitrage_matches().
                      If None, will be computed from results + auctions.
        discovery:    Discovery targets from scan_market_for_alpha(). Optional.
        swaps:        Pre-computed swap pairs from get_optimal_swaps(). If None,
                      computed internally from results + discovery.
        has_research: When True, appends a thread-note to the Rotation header
                      indicating that AI deep research is waiting in the thread.
        regime:       Current MarketRegime; enables CRASH banner and ALPHA suppression.
        macro_data:   Raw macro dict from get_macro_regime(); used for banner metrics.

    Returns:
        Slack Block Kit block list ready for chat_postMessage(blocks=...).
    """
    blocks: list[dict] = []
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 🚨 CRASH BANNER (inserted first — highest visual priority) ────────────
    if regime == MarketRegime.CRASH:
        vix_str     = f"{macro_data['vix_close']:.1f}"    if macro_data and macro_data.get("vix_close")    else "≥30"
        ma_dist_str = f"{macro_data['spy_ma_dist']*100:+.1f}%" if macro_data and macro_data.get("spy_ma_dist") else "< −5%"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "🚨 *MACRO REGIME: CRASH — Equity Swaps Suspended*\n"
                    f"VIX *{vix_str}* ≥ 30  ·  SPY vs MA50 *{ma_dist_str}* ≤ −5%\n"
                    "_所有股票換倉建議已暫停。系統僅允許 CASH FLIGHT 避險操作。_"
                ),
            },
        })
        blocks.append({"type": "divider"})

    # ── Categorise ────────────────────────────────────────────────────────────
    _CRITICAL_SIGNALS = ("減倉停利", "趨勢走弱", "嚴重技術面過熱：強制減倉停利", "技術面嚴重破線：需停損")
    critical = [
        r for r in results
        if not r.error and any(s in _CRITICAL_SIGNALS for s in r.signals)
    ]
    critical_tickers = {r.ticker for r in critical}
    alpha = [
        r for r in results
        if not r.error
        and any("加倉 Alpha" in s for s in r.signals)
        and r.ticker not in critical_tickers
    ]
    watch  = [r for r in results if not r.error and not r.signals]
    errors = [r for r in results if r.error]
    arb           = arb_matches if arb_matches is not None else find_arbitrage_matches(results, auctions or [])
    disc          = discovery or []
    resolved_swaps: list[dict] = (
        swaps if swaps is not None
        else (get_optimal_swaps(results, disc) if disc else [])
    )

    # ── Tactical re-entry radar ───────────────────────────────────────────────
    dip_candidates  = load_dip_buy_candidates()
    reentry_signals = check_reentry_signals(results, dip_candidates)

    arb_suffix   = f"  ·  🔶 {len(arb)}"              if arb            else ""
    disc_suffix  = f"  ·  ⭐ {len(disc)}"             if disc           else ""
    swap_suffix  = f"  ·  🔄 {len(resolved_swaps)}"   if resolved_swaps else ""

    # ── Header (plain_text only, max 150 chars) ───────────────────────────────
    header = (
        f"WARDEN SCAN REPORT  {scan_time}  |  {len(results)} 標的  |  "
        f"🔴 {len(critical)}  ·  🟢 {len(alpha)}{arb_suffix}{disc_suffix}{swap_suffix}"
    )
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": header[:150], "emoji": True},
    })
    blocks.append({"type": "divider"})

    # ── 🔴 CRITICAL ──────────────────────────────────────────────────────────
    if critical:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🔴 CRITICAL — 需立即確認*"},
        })
        for r in critical:
            blocks.append(_make_ticker_section_block(r))
        blocks.append({"type": "divider"})

    # ── 🔄 STRATEGIC ROTATION / 🚨 CASH FLIGHT ───────────────────────────────
    # Moved directly after CRITICAL so action items are visible before context.
    # In CRASH regime only cash-flight swaps exist; header changes accordingly.
    if resolved_swaps and len(blocks) < 44:
        if regime == MarketRegime.CRASH:
            rotation_header = "*🚨 CASH FLIGHT ONLY — 避險操作（宏觀崩盤模式）*"
        else:
            thread_note = (
                "\n_💡 詳細 AI 深度研報已存放於此訊息的 Thread 中_"
                if has_research else ""
            )
            rotation_header = f"*🔄 STRATEGIC ROTATION — 建議換倉（量化分析）*{thread_note}"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": rotation_header,
            },
        })
        for swap in resolved_swaps[: max(0, 43 - len(blocks))]:
            blocks.append(_make_rotation_block(swap))
        blocks.append({"type": "divider"})

    # ── 🎯 TACTICAL RE-ENTER — prior partial-exit tickers that cooled down ────
    # Fires when a DIP_BUY_CANDIDATE's MA50 distance has re-entered [0%, +10%].
    # Placed above ALPHA so it is never missed; cleared from state after display.
    if reentry_signals and len(blocks) < 44:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*🎯 TACTICAL RE-ENTER (短期回踩接回)*",
            },
        })
        for sig in reentry_signals[: max(0, 43 - len(blocks))]:
            r    = sig["scan_result"]
            rec        = sig["candidate_record"]
            ma50_dist  = sig["ma50_dist"]
            ema10_dist = sig["ema10_dist"]
            ema21_dist = sig["ema21_dist"]
            peg        = rec.get("peg")
            peg_str      = f"{peg:.2f}" if peg is not None else "N/A"
            exit_pct     = int(rec.get("exit_ratio", 1.0) * 100)
            exit_dist    = rec.get("ma50_dist_at_exit")
            exit_dist_str = f"{exit_dist * 100:+.1f}%" if exit_dist is not None else "N/A"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*`{r.ticker}`* — 先前減倉 *{exit_pct}%*（MA50 過熱 *{exit_dist_str}*）\n"
                        f"MA50 *{ma50_dist * 100:+.1f}%*  |  "
                        f"EMA10 *{ema10_dist * 100:+.2f}%*  |  "
                        f"EMA21 *{ema21_dist * 100:+.2f}%*  |  "
                        f"PEG *{peg_str}*\n"
                        "機構級動能回踩確認：股價已落入 EMA10 與 EMA21 的「動能價值區間 (Value Zone)」。"
                        "趨勢極強且基本面優異，建議立即戰術性接回。"
                    ),
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔍 Analyze", "emoji": True},
                    "action_id": f"analyze_ticker_{_sanitize_action_id(r.ticker)}",
                    "value": r.ticker,
                },
            })
            # Remove from registry — signal has been surfaced to the operator.
            remove_dip_buy_candidate(r.ticker)
        blocks.append({"type": "divider"})

    # ── 🟢 ALPHA — suppressed in CRASH regime ────────────────────────────────
    if alpha and len(blocks) < 44 and regime != MarketRegime.CRASH:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🟢 ALPHA — 加倉機會*"},
        })
        for r in alpha[: max(0, 43 - len(blocks))]:
            blocks.append(_make_ticker_section_block(r))
        blocks.append({"type": "divider"})

    # ── 🔶 ARBITRAGE ─────────────────────────────────────────────────────────
    if arb and len(blocks) < 44:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🔶 ARBITRAGE WATCH — 近期競拍 / 增資事件*"},
        })
        for m in arb[: max(0, 43 - len(blocks))]:
            blocks.append(_make_arbitrage_block(m))
        blocks.append({"type": "divider"})

    # ── ⭐ DISCOVERY ──────────────────────────────────────────────────────────
    # Slack 50-block hard limit; reserve 4 for WATCH + controls + footer.
    if disc and len(blocks) < 44:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*⭐ DISCOVERY — Alpha Universe Candidates*"},
        })
        for r in disc[: max(0, 43 - len(blocks))]:
            blocks.append(_make_discovery_block(r, regime=regime))
        blocks.append({"type": "divider"})

    # ── ⚪ WATCH (grouped, bottom — no individual buttons) ────────────────────
    if watch:
        ticker_list = "  ".join(f"`{r.ticker}`" for r in watch)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*⚪ WATCH*\n{ticker_list}"},
        })
        blocks.append({"type": "divider"})

    # ── 🗺️ SECTOR CONCENTRATION — Post-Trade Simulation ─────────────────────
    # Always shown — ✅ on pass, ❌ on breach — so the operator never wonders
    # whether the block was silently omitted vs. genuinely clean.
    if len(blocks) < 48:
        try:
            post_trade_results = get_post_trade_snapshot(results, resolved_swaps)
            sector_map  = build_sector_map([r for r in post_trade_results if not r.error])
            conc_warns  = check_concentration(sector_map)
            conc_blocks = format_concentration_blocks(
                conc_warns,
                title="*🗺️ SECTOR CONCENTRATION — Post-Trade Simulation*",
            )
            if conc_blocks:
                blocks.extend(conc_blocks)
            else:
                total_count = sum(len(v) for v in sector_map.values())
                named = {s: v for s, v in sector_map.items() if s != "Unknown"}
                if named and total_count > 0:
                    top_s, top_t = max(named.items(), key=lambda kv: len(kv[1]))
                    top_w = len(top_t) / total_count
                    approval_text = (
                        f"*🗺️ SECTOR CONCENTRATION — Post-Trade Simulation*\n"
                        f"✅ *Approved* — `{top_s}` (highest exposure) at "
                        f"*{top_w:.0%}* — within 40% limit. Swaps approved."
                    )
                else:
                    approval_text = (
                        "*🗺️ SECTOR CONCENTRATION — Post-Trade Simulation*\n"
                        "✅ *Approved* — No sector breaches detected. Swaps approved."
                    )
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": approval_text},
                })
            blocks.append({"type": "divider"})
        except Exception as exc:
            logger.warning("[DASHBOARD] 板塊集中度計算失敗（非阻塞）：%s", exc)

    # ── Errors (context strip, no button) ────────────────────────────────────
    if errors:
        error_list = "  ".join(f"`{r.ticker}`" for r in errors)
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"⚠️ 資料取得失敗：{error_list}"}],
        })

    # ── Global control buttons ────────────────────────────────────────────────
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔄 Refresh All", "emoji": True},
                "action_id": "global_refresh_all",
                "style": "primary",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔶 Check Auctions", "emoji": True},
                "action_id": "global_check_auctions",
            },
        ],
    })

    # ── Footer ────────────────────────────────────────────────────────────────
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "⚠️ _Human-in-the-Loop — 所有部位調整需操盤人確認後執行_",
        }],
    })

    return blocks


def _build_scan_context(
    results: list[ScanResult],
    auctions: list[dict] | None = None,
) -> str:
    """將 ScanResult 列表序列化為 LLM 可讀的結構化純文字。"""
    lines: list[str] = [
        f"SCAN_TIME: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"TOTAL_TICKERS: {len(results)}",
        "",
    ]

    for r in results:
        lines.append(f"[{r.ticker}] {r.name}")
        lines.append(f"  MODEL: {r.valuation_model or 'N/A'}")

        if r.valuation_model == "PEG":
            peg_str = (
                "inf" if r.modified_peg == float("inf")
                else f"{r.modified_peg:.4f}" if r.modified_peg is not None
                else "N/A"
            )
            lines.append(f"  Modified_PEG: {peg_str}")
            if r.trailing_pe is not None:
                lines.append(f"  Trailing_PE: {r.trailing_pe:.1f}")
            if r.earnings_growth is not None:
                lines.append(f"  EPS_Growth: {r.earnings_growth * 100:.1f}%")
            if r.capex_to_rev is not None:
                lines.append(f"  Capex/Rev: {r.capex_to_rev * 100:.1f}%")

        elif r.valuation_model == "PS":
            psr_str = (
                "inf" if r.ps_growth_ratio == float("inf")
                else f"{r.ps_growth_ratio:.4f}" if r.ps_growth_ratio is not None
                else "N/A"
            )
            lines.append(f"  PS_Growth_Ratio: {psr_str}")
            if r.ps_ratio is not None:
                lines.append(f"  PS_Ratio: {r.ps_ratio:.2f}x")
            if r.revenue_growth is not None:
                lines.append(f"  Revenue_Growth: {r.revenue_growth * 100:.1f}%")

        if r.current_price is not None:
            lines.append(f"  Price: ${r.current_price:.2f}")
        if r.ma50 is not None and r.current_price is not None:
            dist = (r.current_price - r.ma50) / r.ma50 * 100
            sign = "+" if dist >= 0 else ""
            lines.append(f"  MA50: ${r.ma50:.2f} ({sign}{dist:.1f}%)")
        if r.week_52_high is not None:
            lines.append(f"  52W_High: ${r.week_52_high:.2f}")
        lines.append(f"  Momentum: {'YES' if r.momentum else 'NO'}")
        lines.append(f"  Signals: {', '.join(r.signals) if r.signals else 'WATCH'}")
        if r.error:
            lines.append(f"  ERROR: {r.error}")
        lines.append("")

    if auctions:
        lines += ["TW_AUCTIONS:"]
        for a in auctions:
            lines.append(
                f"  {a.get('name', 'N/A')} ({a.get('ticker', '')}) |"
                f" Date: {a.get('date', 'N/A')} | Floor: {a.get('floor_price', 'N/A')}"
            )
    elif auctions is not None:
        lines += ["TW_AUCTIONS: none"]

    return "\n".join(lines)


def _call_gemini(user_prompt: str) -> str:
    """
    呼叫 Gemini API，回傳生成的文字。

    SDK 預設使用 v1beta（AI Studio key）。System Prompt 以 few-shot turn 方式
    注入 contents，相容 v1 / v1beta 兩個端點（v1 不支援 systemInstruction 欄位）。

    Args:
        user_prompt: 注入量化數據後的使用者提示。

    Returns:
        格式化後的 Slack Markdown 報告；失敗時回傳以 [ERROR] 開頭的錯誤訊息。
    """
    import traceback

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("[LLM] GEMINI_API_KEY 未設定。")
        return "[ERROR] GEMINI_API_KEY 未設定，已略過 LLM 報告生成。"

    contents = [
        types.Content(role="user",  parts=[types.Part(text=_SYSTEM_PROMPT)]),
        types.Content(role="model", parts=[types.Part(text="已確認角色限制與格式規範。請提供掃描數據。")]),
        types.Content(role="user",  parts=[types.Part(text=user_prompt)]),
    ]

    try:
        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model    = _MODEL,
            contents = contents,
            config   = types.GenerateContentConfig(
                max_output_tokens = _MAX_TOKENS,
            ),
        )
        report = response.text
        logger.info("[LLM] Gemini API 呼叫成功（%d chars）", len(report))
        return report
    except Exception as exc:
        logger.error("[LLM] Gemini API 呼叫失敗：%s\n%s", exc, traceback.format_exc())
        return f"[ERROR] LLM 報告生成失敗：{exc}"


# ── Public API ────────────────────────────────────────────────────────────────

def generate_daily_report(
    results:      list[ScanResult],
    auctions:     list[dict] | None = None,
    arb_matches:  list[dict] | None = None,
    discovery:    list[ScanResult] | None = None,
    swaps:        list[dict]        | None = None,
    has_research: bool               = False,
    regime:       MarketRegime | None = None,
    macro_data:   dict | None         = None,
) -> list[dict]:
    """
    Build the Slack Block Kit dashboard for the daily scan.

    Pure Python — no LLM call. Blocks are constructed directly from ScanResult
    data, eliminating Gemini latency from the scheduled scan pipeline.
    LLM (Gemini) is reserved for on-demand generate_adhoc_analysis calls.

    Args:
        results:      All ScanResult objects from get_market_data().
        auctions:     Output of get_tw_auctions(); None skips ARBITRAGE section.
        arb_matches:  Pre-computed arbitrage matches (from find_arbitrage_matches).
        discovery:    Discovery targets from scan_market_for_alpha(). Optional.
        swaps:        Pre-computed swap pairs from get_optimal_swaps(). If None,
                      computed internally. Pass explicitly to avoid double computation.
        has_research: When True, adds a thread pointer note to the Rotation header.
        regime:       Current MarketRegime for CRASH banner and ALPHA suppression.
        macro_data:   Raw dict from get_macro_regime() for banner metrics display.

    Returns:
        Slack Block Kit block list (list[dict]) for chat_postMessage(blocks=...).
    """
    logger.info(
        "[DASHBOARD] 建構 Block Kit 看板（%d 標的，%d Discovery，%d 換倉建議  Regime=%s）",
        len(results), len(discovery or []), len(swaps or []),
        regime.value if regime else "N/A",
    )
    blocks = _build_daily_blocks(
        results, auctions,
        arb_matches=arb_matches, discovery=discovery, swaps=swaps,
        has_research=has_research, regime=regime, macro_data=macro_data,
    )
    logger.info("[DASHBOARD] 完成，共 %d blocks", len(blocks))
    return blocks


def generate_adhoc_analysis(ticker: str) -> str:
    """
    對單一標的執行臨時深度分析（對應 Slack !analyze 指令）。

    流程：即時抓取市場數據 → 取得近期新聞 → Gemini 生成分析報告。
    新聞區塊包含前 3 則的 Structural Impact 評估（AI / 基礎設施護城河判斷）。

    Args:
        ticker: 股票代號（'NVDA'、'2330.TW'）。

    Returns:
        格式化後的 Slack Markdown 分析報告字串。
    """
    logger.info("[LLM] 開始臨時分析：%s", ticker)

    results = get_market_data([ticker])
    r       = results[0]
    # Use the normalized ticker (e.g., "2330.TW") so yfinance news lookup works
    news    = get_target_news(r.ticker, max_items=5)

    context = _build_scan_context([r])

    # 新聞區塊：前 3 則附 Structural Impact 指示，其餘僅列標題
    if news:
        news_lines = ["", "NEWS (source: yfinance):"]
        for i, headline in enumerate(news):
            if i < 3:
                news_lines.append(
                    f"  [{i+1}] {headline}"
                    f"  ← [請評估此消息對 AI / 基礎設施護城河的 Structural Impact]"
                )
            else:
                news_lines.append(f"  [{i+1}] {headline}")
        news_section = "\n".join(news_lines)
    else:
        news_section = "\nNEWS: none"

    user_prompt = (
        f"請對 `{ticker}` 進行深度分析，直接輸出 Slack Markdown 報告。\n\n"
        "輸出結構（嚴格依序，無引言）：\n"
        f"1. 標題行：*[ANALYZE: {ticker}]* 名稱 | 模型 | 現價\n"
        "2. *估值現況* — 訊號 + 具體數字引用（例：PEG *0.35* < 閾值 0.8）\n"
        "3. *技術面* — 現價 vs 50MA 距離、多頭動能判斷\n"
        "4. *新聞 Structural Impact（前 3 則）* — 每則一行，格式：\n"
        "   `標題摘要` → [✅/⚠️/❌] 一句話評估是否強化或削弱 AI / 基礎設施護城河\n"
        "5. *綜合結論* — 依量化規則輸出行動訊號（🔴/🟢/⚪），不含市場預測\n"
        "6. 結尾：⚠️ _Human-in-the-Loop — 所有部位調整需操盤人確認後執行_\n\n"
        f"--- DATA ---\n{context}{news_section}"
    )

    report = _call_gemini(user_prompt)
    logger.info("[LLM] 臨時分析完成：%s（%d chars）", ticker, len(report))
    return report
