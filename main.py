"""
Alpha Strategist — Main Entry Point (V1.1)

Zero-Touch VLM Pipeline 執行順序：
  1. APScheduler 在背景啟動，依排程呼叫 daily_portfolio_scan()。
  2. daily_portfolio_scan() 完成後：
       a. LLM Compiler 將掃描結果格式化為 Warden Report（generate_daily_report）。
       b. Slack Warden 將 LLM 報告推播至 Slack 頻道（SlackWarden.send_report）。
  3. Slack Bot 在主執行緒阻塞式運行（Socket Mode），監聽指令。

排程時間（Asia/Taipei）：
  - 14:30 週一至週五 → 台股收盤前掃描
  - 05:00 週一至週五 → 美股開盤前掃描

Slack 指令（Socket Mode）：
  !analyze [代號]  → 單一標的臨時深度分析（LLM 生成，Thread 回覆）
  /analyze [代號]  → 同上

CRITICAL: 所有輸出僅供通知，禁止觸發任何下單行為。Human-in-the-Loop。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from src.data_fetcher import (
    DATA_DIR,
    ScanResult,
    get_market_data,
    get_macro_regime,
    get_portfolio,
    get_tw_auctions,
)
from src.config import SAFE_HAVENS
from src.llm_compiler import find_arbitrage_matches, generate_adhoc_analysis, generate_daily_report, get_optimal_swaps
from src.visualizer import generate_alpha_quadrant, generate_ipo_value_gap
from src.discovery import scan_market_for_alpha
from src.logger import archive_scan_context
from src.agent_researcher import conduct_deep_research, RESEARCH_THRESHOLD
from src.price_trigger import intraday_scan_job, format_drop_alert
from src.performance_audit import run_weekly_audit
from src.mcp_servers.portfolio_gateway import mcp
from src.slack_bot import SlackWarden

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("alpha-strategist")

# ── 模組級 Slack Warden 實例（main() 初始化後設定）──────────────────────────
_slack_warden: Optional[SlackWarden] = None


# ── 摘要表格（Console 輸出）──────────────────────────────────────────────────

def _print_summary(results: list[ScanResult], market: str) -> None:
    """
    將本次掃描結果以 ASCII 表格印至 console。

    Args:
        results: 所有 ScanResult 的列表。
        market:  本次掃描的市場標籤。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"\n{'=' * 80}\n"
        f" WARDEN SCAN REPORT | Market: {market} | {now}\n"
        f"{'=' * 80}"
    )
    print(
        f"{'TICKER':<14} {'PRICE':>8} {'50MA':>8} {'SCORE':>9} "
        f"{'MODEL':<10} {'SIGNALS'}"
    )
    print("-" * 80)

    for r in results:
        price = f"{r.current_price:.2f}" if r.current_price else "N/A"
        ma50  = f"{r.ma50:.2f}"          if r.ma50          else "N/A"

        if r.valuation_model == "PEG" and r.modified_peg is not None:
            score = "inf" if r.modified_peg == float("inf") else f"{r.modified_peg:.3f}"
        elif r.valuation_model == "PS" and r.ps_growth_ratio is not None:
            score = "inf" if r.ps_growth_ratio == float("inf") else f"{r.ps_growth_ratio:.3f}"
        else:
            score = "N/A"

        model   = r.valuation_model or ("ERROR" if r.error else "N/A")
        sig_str = ", ".join(r.signals) if r.signals else ("ERROR" if r.error else "WATCH")
        print(
            f"{r.ticker:<14} {price:>8} {ma50:>8} {score:>9} "
            f"{model:<10}  {sig_str}"
        )

    alerts = sum(1 for r in results if r.signals)
    errors = sum(1 for r in results if r.error)
    print("-" * 80)
    print(f" Total: {len(results)} | Alerts: {alerts} | Errors: {errors}")
    print("=" * 80 + "\n")


# ── Warden Scan Job ───────────────────────────────────────────────────────────

def daily_portfolio_scan(market: str = "ALL") -> list[ScanResult]:
    """
    每日持倉掃描作業（V1.1 Zero-Touch Pipeline）。

    流程：
      a. 讀取 data/ CSV，取得完整持倉（get_portfolio）。
      b. 批次評估所有標的（get_market_data → quant_engine）。
      c. 印出 ASCII 摘要表格 + Discovery Log（Console）。
      d. 呼叫 LLM Compiler 生成 Markdown 報告（generate_daily_report）。
      e. 若 Slack 已設定：SlackWarden.send_report 推播 LLM 報告。

    Args:
        market: 掃描目標市場標籤，僅用於 console log 顯示。

    Returns:
        所有標的的 ScanResult 列表。
    """
    # ── Heartbeat ─────────────────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(
        "[HEARTBEAT] Warden Clock 啟動 | Market: %s | %s | System Alive",
        market, now,
    )

    # ── Macro Regime — fetch once, propagate to all downstream modules ────────
    # get_macro_regime() defaults to BULL on any data failure so swap logic is
    # never silently suppressed due to a network hiccup.
    macro = get_macro_regime()
    regime = macro["regime"]
    if macro.get("error"):
        logger.warning("[MACRO] 宏觀數據取得部分失敗（已降級至 BULL）：%s", macro["error"])

    # ── Step 1: 雙檔自動探索 + 合併 ─────────────────────────────────────────
    portfolio    = get_portfolio(DATA_DIR)
    df_all       = portfolio["df"]
    foreign_file = portfolio["files"]["foreign"]
    tw_file      = portfolio["files"]["tw"]

    logger.info(
        "[DATA] 複委託庫存: %s | 證券未實現彙總: %s",
        foreign_file or "NOT FOUND",
        tw_file      or "NOT FOUND",
    )
    if not foreign_file:
        logger.warning("[DATA] 找不到複委託庫存 CSV，美股部位將缺席本次掃描")
    if not tw_file:
        logger.warning("[DATA] 找不到證券未實現彙總 CSV，台股部位將缺席本次掃描")
    for w in portfolio["warnings"]:
        logger.warning("[DATA] %s", w)

    logger.info(
        "[DATA] 載入完成 — US/Foreign: %d 檔 | TW: %d 檔 | 合計: %d 檔",
        portfolio["us_count"], portfolio["tw_count"], len(df_all),
    )

    if df_all.empty:
        logger.warning("[WARDEN] 無有效持倉，掃描中止。")
        return []

    tickers = list(dict.fromkeys(df_all["Ticker"].tolist()))  # 去重，保持順序
    logger.info("[WARDEN] 共 %d 個唯一持倉，開始逐一評估...", len(tickers))

    # ── Step 2: 批次評估 ─────────────────────────────────────────────────────
    results: list[ScanResult] = get_market_data(tickers)

    # ── Step 3: Console 摘要表格 ─────────────────────────────────────────────
    _print_summary(results, market)

    # ── Step 4: Discovery Log ─────────────────────────────────────────────────
    by_model: dict[str, list[str]] = {"PEG": [], "PS": [], "TECHNICAL": [], "ERROR": []}
    for r in results:
        key = "ERROR" if r.error else (r.valuation_model or "ERROR")
        by_model.setdefault(key, []).append(r.ticker)

    print("DISCOVERY LOG")
    print("-" * 48)
    for model, model_tickers in by_model.items():
        if model_tickers:
            print(f"  {model:<12}: {', '.join(model_tickers)}")
    print()

    # ── Step 5: Block Kit Dashboard ──────────────────────────────────────────
    auctions    = get_tw_auctions()
    arb_matches = find_arbitrage_matches(results, auctions)

    # ── Step 5a: Discovery scan (universe minus current holdings) ─────────────
    discovery = scan_market_for_alpha(portfolio_tickers=tickers)
    if discovery:
        logger.info(
            "[DISCOVERY] Top %d Alpha Discovery targets: %s",
            len(discovery),
            ", ".join(r.ticker for r in discovery),
        )
    else:
        logger.info("[DISCOVERY] No Discovery targets passed all filters.")

    # ── Step 5b: Rotation swaps — compute ONCE, reused by report / archive / researcher ──
    swaps: list[dict] = get_optimal_swaps(results, discovery, portfolio_df=df_all, regime=regime) if discovery else []
    if swaps:
        logger.info(
            "[ROTATION] %d 組換倉建議：%s",
            len(swaps),
            "  |  ".join(
                f"{s['source_ticker'].ticker}"
                f"→{s['target_ticker'].ticker if s['target_ticker'] else 'CASH'}"
                f" Δ{s['score_delta']:.2f}"
                for s in swaps
            ),
        )

    # ── Step 5c: Pre-compute research eligibility ─────────────────────────────
    # Criteria: high score_delta + NOT a cash-flight + target is NOT a safe haven.
    # Safe-haven routing (BOXX/SGOV/USFR) is a one-line action — no AI moat
    # analysis needed, and posting Gemini output for a T-bill ETF is noise.
    research_swaps = [
        s for s in swaps
        if not s.get("is_cash_flight")
        and s.get("target_ticker") is not None
        and s["target_ticker"].ticker not in SAFE_HAVENS
        and s["score_delta"] > RESEARCH_THRESHOLD
    ]
    has_research = bool(research_swaps) and _slack_warden is not None

    dashboard_blocks = generate_daily_report(
        results, auctions,
        arb_matches=arb_matches, discovery=discovery, swaps=swaps,
        has_research=has_research,
        regime=regime, macro_data=macro,
    )

    # ── Step 6: Slack 推播 ────────────────────────────────────────────────────
    dashboard_ts: str | None = None
    if _slack_warden is not None:
        dashboard_ts = _slack_warden.send_report(dashboard_blocks)
        # ── Step 7: Diagnostic charts ─────────────────────────────────────────
        chart_paths = []
        try:
            p1 = generate_ipo_value_gap(arb_matches)
            if p1:
                chart_paths.append(p1)
            p2 = generate_alpha_quadrant(results, discovery=discovery)
            if p2:
                chart_paths.append(p2)
        except Exception as exc:
            logger.error("[VISUALIZER] 圖表生成失敗：%s", exc)
        if chart_paths:
            _slack_warden.upload_charts(chart_paths)
    else:
        logger.info("[WARDEN] Slack 未設定，報告僅輸出至 console。")
        print("\n" + "─" * 80)
        print("[DASHBOARD — CONSOLE FALLBACK]")
        print("─" * 80)
        for block in dashboard_blocks:
            btype = block.get("type", "")
            if btype == "header":
                print("\n" + block["text"]["text"])
            elif btype == "section" and "text" in block:
                print(block["text"]["text"].replace("*", "").replace("`", ""))
        print("─" * 80 + "\n")

    # ── Step 8: Decision Memory — archive for V2.0 backtesting ──────────────
    try:
        archive_scan_context(
            portfolio_results=results,
            discovery_targets=discovery,
            swap_advice=swaps,
            market=market,
        )
    except Exception as exc:
        logger.error("[MEMORY] 非阻塞存檔異常（主流程不受影響）：%s", exc)

    # ── Step 9: Agentic Researcher — threaded deep reports ───────────────────
    # Only fires for high-conviction equity swaps (not safe-haven, not cash-flight).
    # Reports are posted as thread replies to the dashboard message so the main
    # channel stays clean — one Block Kit card, details in the thread.
    if research_swaps and _slack_warden is not None:
        logger.info(
            "[RESEARCHER] %d 組高信度換倉（delta > %.1f）觸發深度研究，將存入 Thread...",
            len(research_swaps), RESEARCH_THRESHOLD,
        )
        for swap in research_swaps:
            try:
                report = conduct_deep_research(
                    source_ticker=swap["source_ticker"],
                    target_ticker=swap["target_ticker"],
                    score_delta=swap["score_delta"],
                )
                _slack_warden.send_text(report, thread_ts=dashboard_ts)
            except Exception as exc:
                logger.error(
                    "[RESEARCHER] 深度研究推播失敗（%s → %s）：%s",
                    swap["source_ticker"].ticker,
                    swap["target_ticker"].ticker,
                    exc,
                )

    logger.info(
        "[WARDEN] 掃描結束。Human-in-the-Loop — 等待操盤人確認後執行。"
    )
    return results


# ── Scheduler jobs ────────────────────────────────────────────────────────────

def _run_intraday_alert() -> None:
    """
    15-minute APScheduler job — fires a Slack alert if any holding drops ≥ 5 %.
    Silently exits when markets are closed or no drops are detected.
    """
    try:
        alerts = intraday_scan_job(threshold_pct=5.0)
        if alerts and _slack_warden is not None:
            msg = format_drop_alert(alerts, threshold_pct=5.0)
            _slack_warden.send_text(msg)
    except Exception as exc:
        logger.error("[TRIGGER] 盤中警報作業失敗：%s", exc)


def _run_weekly_audit() -> None:
    """
    Weekly APScheduler job (Sunday 09:00 Taipei) — posts a performance
    audit comparing swap advice vs realised price returns.
    """
    try:
        report = run_weekly_audit(lookback_days=7)
        if _slack_warden is not None:
            _slack_warden.send_text(report)
        else:
            print(report)
    except Exception as exc:
        logger.error("[AUDIT] 週報作業失敗：%s", exc)


# ── Scheduler ────────────────────────────────────────────────────────────────

def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")

    # 台股：週一至週五 14:30
    scheduler.add_job(
        func=daily_portfolio_scan,
        trigger=CronTrigger(day_of_week="mon-fri", hour=14, minute=30),
        kwargs={"market": "TW"},
        id="tw_scan",
        name="台股收盤前掃描",
        replace_existing=True,
    )

    # 美股：週一至週五 05:00
    scheduler.add_job(
        func=daily_portfolio_scan,
        trigger=CronTrigger(day_of_week="mon-fri", hour=5, minute=0),
        kwargs={"market": "US"},
        id="us_scan",
        name="美股開盤前掃描",
        replace_existing=True,
    )

    # 盤中跌幅警報：每 15 分鐘（全天候執行，內部自動判斷是否市場開盤）
    scheduler.add_job(
        func=_run_intraday_alert,
        trigger=CronTrigger(day_of_week="mon-fri", minute="*/15"),
        id="intraday_alert",
        name="盤中跌幅警報",
        replace_existing=True,
    )

    # 週報績效對帳：每週日 09:00
    scheduler.add_job(
        func=_run_weekly_audit,
        trigger=CronTrigger(day_of_week="sun", hour=9, minute=0),
        id="weekly_audit",
        name="週報績效對帳",
        replace_existing=True,
    )

    return scheduler


# ── Argument Parser ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="alpha-strategist",
        description="Alpha Strategist — CFO Warden System (V1.1)",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help=(
            "掃描後立即退出（適合外部 cron job）。"
            " 預設行為（無此旗標）：啟動時掃描一次，然後保持 Socket Mode 持久監聽。"
        ),
    )
    parser.add_argument(
        "--market",
        default="ALL",
        choices=["ALL", "TW", "US"],
        help="搭配 --now 使用，指定掃描市場（預設 ALL）。",
    )
    return parser.parse_args()


# ── Entry Point ───────────────────────────────────────────────────────────────

def main() -> None:
    global _slack_warden

    args = _parse_args()

    # ── 初始化 Slack Warden（選用）──────────────────────────────────────────
    # send_report() 使用同步 WebClient，--now 與 Daemon Mode 均適用，無需區分。
    try:
        _slack_warden = SlackWarden.from_env()
        logger.info("[SLACK] Warden 初始化成功（頻道：%s）。", _slack_warden._channel_id)
    except ValueError as exc:
        logger.info("[SLACK] 未設定或設定不完整，已略過 Slack 功能：%s", exc)
        _slack_warden = None

    # ── Manual Run Mode（--now）──────────────────────────────────────────────
    if args.now:
        logger.info("[MANUAL RUN] --now 模式啟動，立即執行掃描後退出。")
        # daily_portfolio_scan 內部已處理 LLM + Slack 推播
        daily_portfolio_scan(market=args.market)
        logger.info("[MANUAL RUN] 掃描完成，程序退出。")
        sys.exit(0)

    # ── Daemon Mode（APScheduler + Slack Socket Mode）────────────────────────
    scheduler = build_scheduler()
    scheduler.start()

    for job in scheduler.get_jobs():
        logger.info(
            "[SCHEDULER] 排程已載入：[%s] %s → 下次執行：%s",
            job.id, job.name, job.next_run_time,
        )

    def _shutdown(signum, frame):  # type: ignore[type-arg]
        logger.info("[SHUTDOWN] 收到終止信號，正在停止 Warden...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Startup scan（立即推播，再進入持久監聽）────────────────────────────────
    # Run once at boot so the dashboard is visible the moment the bot comes online.
    # APScheduler handles subsequent scheduled runs (14:30 TW / 05:00 US).
    logger.info("[STARTUP] 執行啟動掃描...")
    daily_portfolio_scan(market="ALL")
    logger.info("[STARTUP] 初始看板已發送。進入持久監聽模式...")

    if _slack_warden:
        # Socket Mode 阻塞式啟動；APScheduler 在背景定時觸發 daily_portfolio_scan
        logger.info("[STARTUP] Alpha Strategist V1.1 啟動完成（Slack Socket Mode）。")
        _slack_warden.run(
            adhoc_analysis_fn=generate_adhoc_analysis,
            refresh_fn=lambda: daily_portfolio_scan(market="ALL"),
            check_auctions_fn=get_tw_auctions,
        )
    else:
        # 無 Slack：退回 FastMCP Server 模式
        logger.info("[STARTUP] Alpha Strategist V1.1 啟動完成（FastMCP 模式，無 Slack Bot）。")
        mcp.run()


if __name__ == "__main__":
    main()
