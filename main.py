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
import json
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
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
from src.rebalancer.runner import run as rebalancer_run
from src.discipline.metrics import DisciplineMetrics
from src.rebalancer.trade_builder import BuildResult
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

def daily_portfolio_scan(market: str = "ALL", push_to_slack: bool = True, decisions_path: Path | None = None) -> list[ScanResult]:
    """
    Daily portfolio scan.

    Pre-market (push_to_slack=True, default):
      Runs V2.0 rebalancer pipeline per market → Block Kit dashboard (message 1).
      Posts V1.1 auctions + discovery context report (message 2, D-S5-9).
      Uploads charts (D-S5-10). Archives via archive_scan_context(swap_advice=None, D-S5-12).
      Triggers researcher for Execute-tier BUY trades (D-S5-13).
      Narrative SELL/BUY pair for researcher: conviction-lowest SELL; sector match V2.1 (D-S5-14).

    Post-market (push_to_slack=False):
      Runs V1.1 get_market_data → archives scan context (silent, no Slack push, D-S5-11).
      swap_advice=None: V2.0 runner already archived decisions pre-market (D-S5-12).
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(
        "[HEARTBEAT] Warden Clock 啟動 | Market: %s | push_to_slack: %s | %s | System Alive",
        market, push_to_slack, now,
    )

    # ── Macro Regime ──────────────────────────────────────────────────────────
    macro = get_macro_regime()
    regime = macro["regime"]
    if macro.get("error"):
        logger.warning("[MACRO] 宏觀數據取得部分失敗（已降級至 BULL）：%s", macro["error"])

    # ── Portfolio CSV ─────────────────────────────────────────────────────────
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

    tickers = list(dict.fromkeys(df_all["Ticker"].tolist()))
    logger.info("[WARDEN] 共 %d 個唯一持倉，開始逐一評估...", len(tickers))

    # ── Pre-market: V2.0 pipeline + V1.1 context message ─────────────────────
    if push_to_slack:
        markets_to_run = ["US", "TW"] if market == "ALL" else [market]
        all_results: list[ScanResult] = []

        for mkt in markets_to_run:
            # V2.0 runner — single get_market_data call per market (BLOCKER #2 fix)
            try:
                r_result, r_metrics, r_scan_data = rebalancer_run(mkt, regime=regime, decisions_path=decisions_path)
            except Exception as exc:
                logger.error("[RUNNER] V2.0 runner 失敗（%s）：%s", mkt, exc)
                continue

            mkt_results = r_scan_data.portfolio_results
            all_results.extend(mkt_results)
            _print_summary(mkt_results, mkt)

            # ── Message 1: V2.0 Block Kit dashboard ──────────────────────────
            from src.slack.dashboard import render_dashboard
            v2_blocks = render_dashboard(r_result, r_metrics, market=mkt, timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"))

            dashboard_ts: str | None = None
            if _slack_warden is not None:
                dashboard_ts = _slack_warden.send_report(v2_blocks)
                logger.info("[WARDEN] V2.0 Dashboard 已推播（%s）", mkt)
            else:
                logger.info("[WARDEN] Slack 未設定（%s），Dashboard 僅輸出 console", mkt)
                for block in v2_blocks:
                    if block.get("type") in ("header", "section") and "text" in block:
                        print(block["text"]["text"].replace("*", "").replace("`", ""))

            # ── Discovery (runner fetches portfolio; discovery is a separate universe scan) ──
            discovery = scan_market_for_alpha(portfolio_tickers=tickers)
            if discovery:
                logger.info(
                    "[DISCOVERY] Top %d Alpha Discovery targets: %s",
                    len(discovery), ", ".join(r.ticker for r in discovery),
                )
            else:
                logger.info("[DISCOVERY] No Discovery targets passed all filters.")

            # ── Auctions + arb ────────────────────────────────────────────────
            auctions    = get_tw_auctions()
            arb_matches = find_arbitrage_matches(mkt_results, auctions)

            # ── Research gating: Execute-tier BUY trades only (D-S5-13) ──────
            research_trades = [
                t for t in r_result.trades
                if t.side == "BUY"
                and t.execution_tier == "Execute"
                and t.ticker not in SAFE_HAVENS
            ]

            # ── Message 2: V1.1 context report — auctions + discovery (D-S5-9) ──
            # swaps=[] — V2.0 dashboard already shows trade decisions.
            context_blocks = generate_daily_report(
                mkt_results, auctions,
                arb_matches=arb_matches, discovery=discovery, swaps=[],
                has_research=bool(research_trades) and _slack_warden is not None,
                regime=regime, macro_data=macro,
            )
            if _slack_warden is not None and context_blocks:
                _slack_warden.send_report(context_blocks)

            # ── Charts upload (D-S5-10) ───────────────────────────────────────
            if _slack_warden is not None:
                chart_paths = []
                try:
                    p1 = generate_ipo_value_gap(arb_matches)
                    if p1:
                        chart_paths.append(p1)
                    p2 = generate_alpha_quadrant(mkt_results, discovery=discovery)
                    if p2:
                        chart_paths.append(p2)
                except Exception as exc:
                    logger.error("[VISUALIZER] 圖表生成失敗（%s）：%s", mkt, exc)
                if chart_paths:
                    _slack_warden.upload_charts(chart_paths)

            # ── Archive scan context (swap_advice=None — runner archived via archive_decision) ──
            try:
                archive_scan_context(
                    portfolio_results=mkt_results,
                    discovery_targets=discovery,
                    swap_advice=None,
                    market=mkt,
                )
            except Exception as exc:
                logger.error("[MEMORY] 非阻塞存檔異常（%s）：%s", mkt, exc)

            # ── Researcher: Execute-tier BUY, narrative SELL/BUY pair (D-S5-13, D-S5-14) ──
            if research_trades and _slack_warden is not None:
                port_map = {r.ticker: r for r in mkt_results}
                disc_map  = {r.ticker: r for r in discovery}
                # V2.0 optimizer has no source-target pair concept (D-S5-14).
                # Narrative source = conviction-lowest SELL. Sector-matched source: V2.1.
                sell_sorted_by_conv = sorted(
                    [t for t in r_result.trades if t.side == "SELL"],
                    key=lambda t: t.conviction,
                )
                logger.info(
                    "[RESEARCHER] %d Execute-tier BUY（%s）觸發深度研究...",
                    len(research_trades), mkt,
                )
                for buy in research_trades:
                    tgt_r = disc_map.get(buy.ticker) or port_map.get(buy.ticker)
                    if tgt_r is None:
                        logger.warning(
                            "[RESEARCHER] ScanResult 查無目標 %s，略過。", buy.ticker
                        )
                        continue
                    src_t = sell_sorted_by_conv[0] if sell_sorted_by_conv else None
                    src_r = port_map.get(src_t.ticker) if src_t else None
                    if src_r is None:
                        logger.warning(
                            "[RESEARCHER] 無 SELL 可作敘事配對，略過 BUY %s。", buy.ticker
                        )
                        continue
                    try:
                        report = conduct_deep_research(
                            source_ticker=src_r,  # narrative pair, not algorithmic (D-S5-14)
                            target_ticker=tgt_r,
                            score_delta=buy.conviction / 10.0,
                        )
                        _slack_warden.send_text(report, thread_ts=dashboard_ts)
                    except Exception as exc:
                        logger.error(
                            "[RESEARCHER] 深度研究失敗（%s → %s）：%s",
                            src_r.ticker, tgt_r.ticker, exc,
                        )

        logger.info("[WARDEN] 掃描結束。Human-in-the-Loop — 等待操盤人確認後執行。")
        return all_results

    # ── Post-market: silent V1.1 price update + archive (D-S5-11) ─────────────
    results: list[ScanResult] = get_market_data(tickers)
    _print_summary(results, market)
    logger.info(
        "[POST-MARKET] swap_advice=None — V2.0 pre-market runner already archived "
        "rebalancing decisions via archive_decision(); this pass updates price context only."
    )
    try:
        archive_scan_context(
            portfolio_results=results,
            discovery_targets=[],
            swap_advice=None,
            market=market,
        )
    except Exception as exc:
        logger.error("[MEMORY] 非阻塞存檔異常：%s", exc)

    logger.info("[WARDEN] 掃描結束（post-market silent archive 完成）。")
    return results


def _pre_market_scan(market: str, decisions_path: Path | None = None) -> None:
    """Thin wrapper: pre-market push → V2.0 Block Kit dashboard + V1.1 context message."""
    daily_portfolio_scan(market=market, push_to_slack=True, decisions_path=decisions_path)


def _post_market_archive(market: str) -> None:
    """Thin wrapper: post-market silent archive → price update, no Slack push."""
    daily_portfolio_scan(market=market, push_to_slack=False)


def _refresh_both_markets() -> None:
    """refresh_fn callback for SlackWarden.run() — fires pre-market pipeline for both markets."""
    for m in ("US", "TW"):
        _pre_market_scan(m)


def _rebalance_callback(market: str) -> tuple[BuildResult, DisciplineMetrics]:
    """_rebalance_fn callback: strips ScanData from 3-tuple to match RebalanceFn contract."""
    result, metrics, _scan_data = rebalancer_run(market)
    return result, metrics


def _recent_archive_exists(path: Path, market: str, minutes: int) -> bool:
    """Return True if the JSONL archive has an entry for market within minutes of now."""
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("market") != market:
                continue
            ts_str = record.get("timestamp")
            if ts_str is None:
                continue
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            return (datetime.now() - ts).total_seconds() < minutes * 60
    except Exception:
        pass
    return False


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

    # ── Pre-market: V2.0 dashboard push ──────────────────────────────────────
    # TW: 30 min before TWSE open (09:00 Taipei), Mon–Fri
    scheduler.add_job(
        func=_pre_market_scan,
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=30),
        kwargs={"market": "TW"},
        id="tw_premarket",
        name="台股開盤前掃描（V2.0 推播）",
        replace_existing=True,
    )

    # US: before NYSE open (09:30 NY = 21:00 Taipei prev day), Sun–Thu
    scheduler.add_job(
        func=_pre_market_scan,
        trigger=CronTrigger(day_of_week="sun-thu", hour=21, minute=0),
        kwargs={"market": "US"},
        id="us_premarket",
        name="美股開盤前掃描（V2.0 推播）",
        replace_existing=True,
    )

    # ── Post-market: silent price update + archive ─────────────────────────────
    # TW: after TWSE close (13:30 Taipei), Mon–Fri
    scheduler.add_job(
        func=_post_market_archive,
        trigger=CronTrigger(day_of_week="mon-fri", hour=14, minute=30),
        kwargs={"market": "TW"},
        id="tw_postmarket",
        name="台股收盤後歸檔（靜默）",
        replace_existing=True,
    )

    # US: after NYSE close (16:00 NY = 05:00 Taipei next day), Tue–Sat
    scheduler.add_job(
        func=_post_market_archive,
        trigger=CronTrigger(day_of_week="tue-sat", hour=5, minute=0),
        kwargs={"market": "US"},
        id="us_postmarket",
        name="美股收盤後歸檔（靜默）",
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

    # Wire V2.0 rebalance callback for !rebalance preview command
    if _slack_warden is not None:
        _slack_warden._rebalance_fn = _rebalance_callback

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
    # Dedupes against recent archive entries to avoid double-push at scheduled boundaries.
    # DRY-RUN: set V2_BOOT_DRY_RUN=true to route archive to sprint5_smoke.jsonl
    # and Slack posts to V2_TEST_CHANNEL_ID (if set).
    logger.info("[STARTUP] 執行啟動掃描...")
    dry_run = os.getenv("V2_BOOT_DRY_RUN", "").lower() in ("true", "1", "yes")
    boot_decisions_path = (
        Path("memory/sprint5_smoke.jsonl") if dry_run
        else Path("memory/rebalance_decisions.jsonl")
    )
    if dry_run:
        logger.info("[STARTUP] DRY-RUN 模式啟動（V2_BOOT_DRY_RUN=true）")

    _original_channel: str | None = None
    if dry_run and _slack_warden is not None:
        test_channel = os.getenv("V2_TEST_CHANNEL_ID")
        if test_channel:
            _original_channel = _slack_warden._channel_id
            _slack_warden._channel_id = test_channel
            logger.info("[STARTUP] DRY-RUN: 推播至測試頻道 %s", test_channel)

    try:
        for _mkt in ("US", "TW"):
            if not dry_run and _recent_archive_exists(boot_decisions_path, _mkt, minutes=30):
                logger.info(
                    "[STARTUP] %s 略過 — 近期已有 archive 記錄（< 30 min）", _mkt,
                )
                continue
            _pre_market_scan(_mkt, decisions_path=boot_decisions_path)
    finally:
        if _original_channel is not None and _slack_warden is not None:
            _slack_warden._channel_id = _original_channel
            logger.info("[STARTUP] DRY-RUN: 已還原至正式頻道")

    logger.info("[STARTUP] 初始看板已發送。進入持久監聽模式...")

    if _slack_warden:
        # Socket Mode 阻塞式啟動；APScheduler 在背景定時觸發 daily_portfolio_scan
        logger.info("[STARTUP] Alpha Strategist V1.1 啟動完成（Slack Socket Mode）。")
        _slack_warden.run(
            adhoc_analysis_fn=generate_adhoc_analysis,
            refresh_fn=_refresh_both_markets,
            check_auctions_fn=get_tw_auctions,
        )
    else:
        # 無 Slack：退回 FastMCP Server 模式
        logger.info("[STARTUP] Alpha Strategist V1.1 啟動完成（FastMCP 模式，無 Slack Bot）。")
        mcp.run()


if __name__ == "__main__":
    main()
