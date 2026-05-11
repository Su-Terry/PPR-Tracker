"""
Alpha Strategist — Agentic Researcher (V2.0)

Generates a minimalist deep-research report for high-conviction rotation
candidates (Efficiency Score Delta > 0.5).

Pipeline per swap pair (source → target):
  A. 48h news pulse      — yfinance headlines for both tickers
  B. Sector fingerprint  — detect Tech / AI / Robotics via yfinance sector data
  C. Domain cross-ref    — map findings to user's research focus areas
  D. Bull vs Bear synth  — structured Gemini reasoning across MOAT / FINANCIAL /
                           INDUSTRY dimensions

Output: Slack mrkdwn string (~800–1400 chars, Attachment-ready).
Trigger: called only when score_delta > _RESEARCH_THRESHOLD (0.5) so LLM
         latency is incurred only for truly high-conviction swap advice.

CRITICAL: All output is advisory. No order triggers. Human-in-the-Loop.
"""

from __future__ import annotations

import logging
import os
import traceback

import yfinance as yf
from google import genai
from google.genai import types

from src.data_fetcher import ScanResult, get_target_news

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_MODEL            = "gemini-2.0-flash"
_MAX_TOKENS       = 1536
RESEARCH_THRESHOLD = 0.5      # score_delta must exceed this to trigger research

# Sectors / keywords that unlock the "Technical Barriers / R&D" section (Step B)
_TECH_SECTORS: frozenset[str] = frozenset({
    "Technology",
    "Communication Services",
    "Industrials",          # drones, defence, robotics
})
_TECH_KEYWORDS: frozenset[str] = frozenset({
    "semiconductor", "artificial intelligence", "machine learning",
    "robotics", "autonomous", "software", "cloud", "cybersecurity",
    "drone", "defense", "defence", "optical", "photonics", "gpu",
})

# User's active research domains — injected into "Personal Relevance" section
_USER_RESEARCH_DOMAINS = """\
- VLM Agentic Systems (Vision-Language Model pipelines, tool-calling agents)
- Semantic Drone Navigation (autonomous UAV path planning, obstacle avoidance)
- AI / Robotics Infrastructure (edge inference, compute scaling, sensor fusion)\
"""

# System persona for research — narrower than the daily-report system prompt
_RESEARCH_SYSTEM_PROMPT = """\
你是 Alpha Strategist 的「換倉研究助理」（Rotation Research Assistant）。

[核心限制]
1. 禁止預測股價漲跌幅或給出價格目標。
2. 禁止幻覺（Hallucinate）任何不在輸入數據中的財報數字。
3. 所有結論必須可追溯至輸入資料或公開已知事實。
4. 輸出必須是極簡條列式 Slack mrkdwn。不含引言、免責聲明、段落說明。

[格式規範]
• 使用 *粗體* 做小節標題，不使用 #/## 標題
• 每個 bullet 以 • 開頭（Unicode U+2022），不使用 Markdown 清單符號（-/*/+）
• 代號一律反引號包覆：`NVDA`
• 數字直接內嵌，不另起一行
• 全文目標長度：800–1400 字元
"""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_sector_info(ticker: str) -> dict:
    """
    Retrieve sector, industry, and key financial summary from yfinance.

    Returns a dict with keys: sector, industry, is_tech, fcf, total_debt,
    operating_cash_flow, revenue_growth, gross_margins, analyst_target_mean.
    All values default to None / False on failure.
    """
    result: dict = {
        "sector":               None,
        "industry":             None,
        "is_tech":              False,
        "fcf":                  None,
        "total_debt":           None,
        "operating_cash_flow":  None,
        "revenue_growth":       None,
        "gross_margins":        None,
        "analyst_target_mean":  None,
        "recommendation":       None,
    }
    try:
        info = yf.Ticker(ticker).info
        sector   = info.get("sector",   "") or ""
        industry = info.get("industry", "") or ""

        result["sector"]   = sector   or None
        result["industry"] = industry or None

        # Classify as Tech/AI/Robotics
        combined = (sector + " " + industry).lower()
        result["is_tech"] = (
            sector in _TECH_SECTORS
            or any(kw in combined for kw in _TECH_KEYWORDS)
        )

        # Financial quality signals
        result["fcf"]                 = info.get("freeCashflow")
        result["total_debt"]          = info.get("totalDebt")
        result["operating_cash_flow"] = info.get("operatingCashflow")
        result["revenue_growth"]      = info.get("revenueGrowth")
        result["gross_margins"]       = info.get("grossMargins")
        result["analyst_target_mean"] = info.get("targetMeanPrice")
        result["recommendation"]      = info.get("recommendationKey")

    except Exception as exc:
        logger.warning("[RESEARCHER] %s sector info 取得失敗：%s", ticker, exc)

    return result


def _fmt_usd(v: float | None) -> str:
    """Format a raw dollar value (e.g. freeCashflow) into a compact string."""
    if v is None:
        return "N/A"
    if abs(v) >= 1e9:
        return f"${v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:.1f}%"


def _build_research_prompt(
    src: ScanResult,
    tgt: ScanResult,
    score_delta: float,
    src_news: list[str],
    tgt_news: list[str],
    src_info: dict,
    tgt_info: dict,
) -> str:
    """Assemble the full structured prompt for the Gemini research call."""

    def _ratio_str(r: ScanResult) -> str:
        if r.valuation_model == "PEG" and r.modified_peg is not None:
            v = r.modified_peg
            return "∞" if v == float("inf") else f"{v:.2f}"
        if r.valuation_model == "PS" and r.ps_growth_ratio is not None:
            v = r.ps_growth_ratio
            return "∞" if v == float("inf") else f"{v:.2f}"
        return "N/A"

    def _dist_str(r: ScanResult) -> str:
        if r.current_price and r.ma50:
            d = (r.current_price - r.ma50) / r.ma50 * 100
            return f"{d:+.1f}%"
        return "N/A"

    def _news_block(ticker: str, headlines: list[str]) -> str:
        if not headlines:
            return f"  `{ticker}`: 無近期新聞"
        return "\n".join(f"  `{ticker}` [{i+1}]: {h}" for i, h in enumerate(headlines[:4]))

    def _fin_block(ticker: str, info: dict) -> str:
        lines = [
            f"  `{ticker}` Sector: {info['sector'] or 'N/A'} / {info['industry'] or 'N/A'}",
            f"  `{ticker}` FCF: {_fmt_usd(info['fcf'])}  |  Debt: {_fmt_usd(info['total_debt'])}",
            f"  `{ticker}` OpCF: {_fmt_usd(info['operating_cash_flow'])}  |  Gross Margin: {_fmt_pct(info['gross_margins'])}",
            f"  `{ticker}` Rev Growth: {_fmt_pct(info['revenue_growth'])}  |  Analyst Target: {_fmt_usd(info['analyst_target_mean'])}",
            f"  `{ticker}` Analyst Rec: {(info['recommendation'] or 'N/A').upper()}",
        ]
        return "\n".join(lines)

    is_tech = src_info["is_tech"] or tgt_info["is_tech"]
    tech_note = (
        "（至少一檔屬 Tech / AI / Robotics 板塊 — 請在 MOAT 中分析技術壁壘與 R&D 效率）"
        if is_tech else
        "（非 Tech 板塊 — MOAT 分析側重品牌護城河、規模經濟或監管壁壘）"
    )

    prompt = f"""\
請對以下換倉建議進行深度研究，輸出極簡條列式 Slack mrkdwn 報告。

--- SWAP CONTEXT ---
SELL: `{src.ticker}` ({src.name}) | {src.valuation_model} {_ratio_str(src)} | MA50 {_dist_str(src)} | Score {score_delta:.3f}
BUY:  `{tgt.ticker}` ({tgt.name}) | {tgt.valuation_model} {_ratio_str(tgt)} | MA50 {_dist_str(tgt)}
Score Delta: +{score_delta:.3f}  [觸發閾值 > 0.5 ← 高信度換倉]

--- 48H NEWS ---
{_news_block(src.ticker, src_news)}
{_news_block(tgt.ticker, tgt_news)}

--- FINANCIAL QUALITY ---
{_fin_block(src.ticker, src_info)}
{_fin_block(tgt.ticker, tgt_info)}

--- SECTOR NOTE ---
{tech_note}

--- USER RESEARCH DOMAINS (Personal Relevance 必須對應以下方向) ---
{_USER_RESEARCH_DOMAINS}

--- OUTPUT FORMAT (嚴格依序，無引言) ---
*🔬 DEEP RESEARCH — `{src.ticker}` → `{tgt.ticker}`*
Score Delta *+{score_delta:.3f}* | High-Conviction Rotation

*📰 48H NEWS PULSE*
（各 1–2 bullet，每則加上 ✅/⚠️/❌ 影響標籤）

*🏰 MOAT*
• R&D Efficiency: ...
• Technical Barriers: ...  {tech_note.split("—")[0].strip()}

*💰 FINANCIAL QUALITY*
• FCF Quality: （正/負現金流，槓桿率）
• Debt Safety: （債務可控程度）

*🏭 INDUSTRY POSITION*
• Supply Chain: （上下游地位）
• Analyst Consensus: （分析師評級 + 目標價）

*⚔️ BULL vs BEAR*
• Bull: （最強多方論點，1 句）
• Bear: （最大風險，1 句）

*🧬 PERSONAL RELEVANCE*
（說明此換倉如何影響 VLM Agentic、語義無人機導航、AI 基礎設施投資組合的曝險）

⚠️ _Human-in-the-Loop — 研究報告僅供參考，操盤人決策_\
"""
    return prompt


def _call_gemini_research(prompt: str) -> str:
    """
    Call Gemini with the research system persona and return the mrkdwn report.

    Separate from llm_compiler._call_gemini to use a dedicated system prompt
    and a larger token budget appropriate for structured research output.

    Returns an [ERROR] string on any failure so the caller can log and continue.
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return "[ERROR] GEMINI_API_KEY 未設定，已略過深度研究。"

    contents = [
        types.Content(role="user",  parts=[types.Part(text=_RESEARCH_SYSTEM_PROMPT)]),
        types.Content(role="model", parts=[types.Part(text="已確認研究助理角色。請提供換倉資料。")]),
        types.Content(role="user",  parts=[types.Part(text=prompt)]),
    ]

    try:
        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model    = _MODEL,
            contents = contents,
            config   = types.GenerateContentConfig(
                max_output_tokens=_MAX_TOKENS,
            ),
        )
        return response.text
    except Exception as exc:
        logger.error("[RESEARCHER] Gemini 呼叫失敗：%s\n%s", exc, traceback.format_exc())
        return f"[ERROR] 深度研究生成失敗：{exc}"


# ── Public API ────────────────────────────────────────────────────────────────

def conduct_deep_research(
    source_ticker: ScanResult,
    target_ticker: ScanResult,
    score_delta:   float,
) -> str:
    """
    Run the 4-step agentic research pipeline for one high-conviction swap pair.

    Steps:
      A. Fetch latest 48h news headlines for both tickers via yfinance.
      B. Fetch sector / industry data; classify as Tech/AI/Robotics.
      C. Cross-reference findings with user's research domains
         (VLM Agentic, semantic drone navigation, AI infrastructure).
      D. Gemini Bull vs Bear synthesis across MOAT / FINANCIAL / INDUSTRY.

    Only call this function when score_delta > RESEARCH_THRESHOLD (0.5).
    The function does NOT enforce the threshold itself — the caller is
    responsible for filtering.

    Args:
        source_ticker: ScanResult of the portfolio holding to sell.
        target_ticker: ScanResult of the Discovery target to buy.
        score_delta:   Efficiency score improvement (buy − sell).

    Returns:
        Slack mrkdwn research report string (~800–1400 chars).
        Returns an [ERROR] prefixed string on failure — never raises.
    """
    src_tick = source_ticker.ticker
    tgt_tick = target_ticker.ticker

    logger.info(
        "[RESEARCHER] 啟動深度研究：%s → %s  (delta=+%.3f)",
        src_tick, tgt_tick, score_delta,
    )

    # ── Step A: 48h news ──────────────────────────────────────────────────────
    src_news = get_target_news(src_tick, max_items=4)
    tgt_news = get_target_news(tgt_tick, max_items=4)
    logger.info(
        "[RESEARCHER] 新聞抓取完成 — %s: %d則, %s: %d則",
        src_tick, len(src_news), tgt_tick, len(tgt_news),
    )

    # ── Step B: Sector / financial fingerprint ────────────────────────────────
    src_info = _fetch_sector_info(src_tick)
    tgt_info = _fetch_sector_info(tgt_tick)
    logger.info(
        "[RESEARCHER] 板塊識別 — %s: %s (is_tech=%s) | %s: %s (is_tech=%s)",
        src_tick, src_info["sector"], src_info["is_tech"],
        tgt_tick, tgt_info["sector"], tgt_info["is_tech"],
    )

    # ── Steps C + D: Cross-ref user domains + Gemini Bull/Bear synthesis ──────
    prompt = _build_research_prompt(
        source_ticker, target_ticker, score_delta,
        src_news, tgt_news, src_info, tgt_info,
    )
    report = _call_gemini_research(prompt)

    logger.info(
        "[RESEARCHER] 深度研究完成：%s → %s（%d chars）",
        src_tick, tgt_tick, len(report),
    )
    return report
