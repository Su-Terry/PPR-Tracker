"""
Alpha Strategist — Slack Warden (V1.2 — Block Kit + Interactive Buttons)

職責：
  - 將 Block Kit 看板推播至指定 Slack 頻道（每日掃描觸發）。
  - 監聽頻道訊息：!analyze / /analyze [代號] → Thread 深度分析。
  - 監聽 Block Kit 按鈕互動：
      analyze_ticker_*    → 點擊 🔍 Analyze 按鈕 → Thread 深度分析
      global_refresh_all  → 點擊 🔄 Refresh All → 重新執行全量掃描
      global_check_auctions → 點擊 🔶 Check Auctions → Thread 顯示競拍事件

環境變數（三者均必填）：
  SLACK_BOT_TOKEN  : Bot OAuth Token（xoxb-...）
  SLACK_APP_TOKEN  : App-Level Token（xapp-...）
  SLACK_CHANNEL_ID : 推播目標頻道 ID（C...）

Slack App 設定要求：
  - Bot Token Scopes：chat:write, channels:history, groups:history, im:history
  - Socket Mode 啟用
  - Interactivity & Shortcuts：啟用（Button actions 必須）

Threading：
  - send_report()：同步，Slack SDK WebClient 執行緒安全，可從 APScheduler 呼叫。
  - Action handlers：Bolt 自動在背景執行緒分派，必須先呼叫 ack()。
  - run()：阻塞式，應在主執行緒最後呼叫。

CRITICAL: 純通知與互動層，禁止任何下單或市場預測邏輯。Human-in-the-Loop。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.portfolio.state import PortfolioState
from src.slack.dashboard import render_dashboard

logger = logging.getLogger(__name__)

_MAX_SLACK_LEN   = 3000   # conservative split length for mrkdwn messages
_ANALYZE_PATTERN = re.compile(r"[!/]analyze\s+(\S+)", re.IGNORECASE)
_ACTION_ANALYZE  = re.compile(r"^analyze_ticker_.*")

# ── Sprint 4 message patterns ─────────────────────────────────────────────────
_REBALANCE_PREVIEW_PATTERN = re.compile(
    r"[!/]rebalance\s+preview(?:\s+(us|tw|all))?", re.IGNORECASE
)
_HOLDINGS_SHOW_PATTERN = re.compile(
    r"[!/]holdings\s+show(?:\s+(us|tw))?", re.IGNORECASE
)
_WHY_PATTERN         = re.compile(r"[!/]why\s+(\S+)", re.IGNORECASE)
_TRADE_ADD_PATTERN   = re.compile(r"[!/]trade\s+add\b", re.IGNORECASE)

# Stretch stub patterns (Sprint 5 wires business logic)
_CASH_PATTERN            = re.compile(r"[!/]cash\s+(?:show|set|adjust)\b", re.IGNORECASE)
_HOLDINGS_SYNC_PATTERN   = re.compile(r"[!/]holdings\s+sync\b", re.IGNORECASE)
_IPO_PATTERN             = re.compile(r"[!/]ipo\s+(?:apply|release|list)\b", re.IGNORECASE)
_FX_PATTERN              = re.compile(r"[!/]fx\s+record\b", re.IGNORECASE)
_COST_PATTERN            = re.compile(r"[!/]cost\s+(?:show|set|reset)\b", re.IGNORECASE)
_RECONCILE_PATTERN       = re.compile(r"[!/]reconcile\b", re.IGNORECASE)
_REBALANCE_CONFIG_PATTERN = re.compile(r"[!/]rebalance\s+config\b", re.IGNORECASE)

# ── Sprint 4 modal definition ─────────────────────────────────────────────────
_TRADE_ADD_MODAL: dict = {
    "type": "modal",
    "callback_id": "trade_add_submit",
    "title": {"type": "plain_text", "text": "登記成交記錄"},
    "submit": {"type": "plain_text", "text": "確認"},
    "close": {"type": "plain_text", "text": "取消"},
    "blocks": [
        {
            "type": "input",
            "block_id": "market",
            "label": {"type": "plain_text", "text": "市場"},
            "element": {
                "type": "static_select",
                "action_id": "market_select",
                "options": [
                    {"text": {"type": "plain_text", "text": "🇺🇸 US"}, "value": "US"},
                    {"text": {"type": "plain_text", "text": "🇹🇼 TW"}, "value": "TW"},
                ],
            },
        },
        {
            "type": "input",
            "block_id": "ticker",
            "label": {"type": "plain_text", "text": "代號 (Ticker)"},
            "element": {"type": "plain_text_input", "action_id": "ticker_input"},
        },
        {
            "type": "input",
            "block_id": "side",
            "label": {"type": "plain_text", "text": "方向"},
            "element": {
                "type": "static_select",
                "action_id": "side_select",
                "options": [
                    {"text": {"type": "plain_text", "text": "BUY"}, "value": "BUY"},
                    {"text": {"type": "plain_text", "text": "SELL"}, "value": "SELL"},
                ],
            },
        },
        {
            "type": "input",
            "block_id": "quantity",
            "label": {"type": "plain_text", "text": "數量 (股)"},
            "element": {"type": "plain_text_input", "action_id": "quantity_input"},
        },
        {
            "type": "input",
            "block_id": "filled_price",
            "label": {"type": "plain_text", "text": "成交價"},
            "element": {"type": "plain_text_input", "action_id": "filled_price_input"},
        },
        {
            "type": "input",
            "block_id": "commission",
            "label": {"type": "plain_text", "text": "手續費"},
            "element": {
                "type": "plain_text_input",
                "action_id": "commission_input",
                "initial_value": "0",
            },
            "optional": True,
        },
        {
            "type": "input",
            "block_id": "tax",
            "label": {"type": "plain_text", "text": "交易稅"},
            "element": {
                "type": "plain_text_input",
                "action_id": "tax_input",
                "initial_value": "0",
            },
            "optional": True,
        },
        {
            "type": "input",
            "block_id": "fx",
            "label": {"type": "plain_text", "text": "匯率 (預設 1.0)"},
            "element": {
                "type": "plain_text_input",
                "action_id": "fx_input",
                "initial_value": "1.0",
            },
            "optional": True,
        },
        {
            "type": "input",
            "block_id": "system_suggested",
            "label": {"type": "plain_text", "text": "系統建議?"},
            "element": {
                "type": "checkboxes",
                "action_id": "system_suggested_check",
                "options": [
                    {
                        "text": {"type": "plain_text", "text": "是系統建議的交易"},
                        "value": "yes",
                    }
                ],
            },
            "optional": True,
        },
    ],
}


# ── Module-level helpers ──────────────────────────────────────────────────────

def _now_taipei() -> str:
    """Return current Asia/Taipei time as 'YYYY-MM-DD HH:MM'."""
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M")


def _today_taipei() -> str:
    """Return today's date in Asia/Taipei as YYYY-MM-DD."""
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


def _find_latest_decision(path: Path, ticker: str) -> dict | None:
    """Find the most recent archive entry containing `ticker` in trades."""
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if any(t.get("ticker") == ticker for t in rec.get("trades", [])):
                return rec
        except json.JSONDecodeError:
            continue
    return None


def _find_latest_active_decision(path: Path, market: str) -> dict | None:
    """Find the most recent non-HOLD archive entry for the given market."""
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("market") == market and not rec.get("is_hold", True):
                return rec
        except json.JSONDecodeError:
            continue
    return None


def _append_actual_trade(path: Path, record: dict) -> None:
    """Append one record to actual_trades.jsonl atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _tier_emoji(tier: str) -> str:
    return {"Execute": "✅", "Watch": "⏸", "Skip": "❌"}.get(tier, "•")


class SlackWarden:
    """
    Slack Socket Mode Bot — Block Kit 推播與互動指令層。

    Daemon Mode:
        warden = SlackWarden.from_env()
        scheduler.start()
        warden.run(
            adhoc_analysis_fn=llm_compiler.generate_adhoc_analysis,
            refresh_fn=lambda: daily_portfolio_scan(market="ALL"),
            check_auctions_fn=data_fetcher.get_tw_auctions,
        )

    One-shot (--now mode or any thread):
        warden = SlackWarden.from_env()
        warden.send_report(blocks)   # synchronous, no Socket Mode needed
    """

    def __init__(
        self,
        bot_token:        str,
        app_token:        str,
        channel_id:       str,
        state_path:       Optional[Path] = None,
        decision_archive: Optional[Path] = None,
        actual_trades:    Optional[Path] = None,
    ) -> None:
        self._app_token   = app_token
        self._channel_id  = channel_id

        # V1.1 callbacks
        self._adhoc_fn:           Optional[Callable[[str], str]]     = None
        self._refresh_fn:         Optional[Callable[[], None]]       = None
        self._check_auctions_fn:  Optional[Callable[[], list[dict]]] = None

        # Sprint 4 pipeline callback (returns BuildResult + DisciplineMetrics)
        self._rebalance_fn: Optional[Callable[[str], tuple]] = None

        # Sprint 4 data paths (defaults match V2.0 module constants)
        self._state_path       = state_path       or Path("data/portfolio_state.json")
        self._decision_archive = decision_archive or Path("memory/rebalance_decisions.jsonl")
        self._actual_trades_path = actual_trades  or Path("memory/actual_trades.jsonl")

        self.app = App(token=bot_token)
        self._register_handlers()

    @classmethod
    def from_env(cls) -> "SlackWarden":
        """
        Build a SlackWarden from environment variables.

        Raises:
            ValueError: if any of the three required env vars is missing.
        """
        bot_token  = os.getenv("SLACK_BOT_TOKEN",  "")
        app_token  = os.getenv("SLACK_APP_TOKEN",  "")
        channel_id = os.getenv("SLACK_CHANNEL_ID", "")

        missing = [
            name for name, val in [
                ("SLACK_BOT_TOKEN",  bot_token),
                ("SLACK_APP_TOKEN",  app_token),
                ("SLACK_CHANNEL_ID", channel_id),
            ]
            if not val
        ]
        if missing:
            raise ValueError(f"Slack 環境變數未設定：{', '.join(missing)}")

        return cls(bot_token=bot_token, app_token=app_token, channel_id=channel_id)

    # ── Handler registration ──────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        """Register all message listeners and action handlers with the Bolt app."""
        # V1.1: text command !analyze / /analyze TICKER
        self.app.message(_ANALYZE_PATTERN)(self._on_analyze_text)

        # V1.1: Block Kit buttons
        self.app.action(_ACTION_ANALYZE)(self._on_analyze_button)
        self.app.action("global_refresh_all")(self._on_refresh_all)
        self.app.action("global_check_auctions")(self._on_check_auctions)

        # Sprint 4: core text commands
        self.app.message(_REBALANCE_PREVIEW_PATTERN)(self._on_rebalance_preview)
        self.app.message(_HOLDINGS_SHOW_PATTERN)(self._on_holdings_show)
        self.app.message(_WHY_PATTERN)(self._on_why)
        self.app.message(_TRADE_ADD_PATTERN)(self._on_trade_add_msg)

        # Sprint 4: dashboard action buttons
        self.app.action("rebalance_approve")(self._on_rebalance_approve)
        self.app.action("rebalance_why_summary")(self._on_rebalance_why_summary)
        self.app.action("rebalance_skip_plan")(self._on_rebalance_skip_plan)
        self.app.action("rebalance_expand")(self._on_rebalance_expand)

        # Sprint 4: /trade add modal flow
        self.app.action("trade_open_modal")(self._on_trade_open_modal)
        self.app.view("trade_add_submit")(self._on_trade_add_submit)

        # Sprint 4: stretch command stubs (Sprint 5 wires business logic)
        self.app.message(_CASH_PATTERN)(self._on_stub_cash)
        self.app.message(_HOLDINGS_SYNC_PATTERN)(self._on_stub_holdings_sync)
        self.app.message(_IPO_PATTERN)(self._on_stub_ipo)
        self.app.message(_FX_PATTERN)(self._on_stub_fx)
        self.app.message(_COST_PATTERN)(self._on_stub_cost)
        self.app.message(_RECONCILE_PATTERN)(self._on_stub_reconcile)
        self.app.message(_REBALANCE_CONFIG_PATTERN)(self._on_stub_rebalance_config)

        # Silence Slack's message_changed / message_deleted subtype events.
        self.app.event({"type": "message", "subtype": "message_changed"})(lambda body: None)
        self.app.event({"type": "message", "subtype": "message_deleted"})(lambda body: None)

    # ── Sprint 4: core text command handlers ─────────────────────────────────

    def _on_rebalance_preview(
        self, message: dict, say: Callable, context: dict
    ) -> None:
        """Handle !rebalance preview [us|tw|all] — render Block Kit dashboard."""
        matches = context.get("matches", ())
        raw_market = str(matches[0]).upper() if matches and matches[0] else "US"
        markets = ["US", "TW"] if raw_market == "ALL" else [
            raw_market if raw_market in ("US", "TW") else "US"
        ]
        thread_ts = message.get("ts", "")

        if self._rebalance_fn is None:
            say(
                text="⚙️ 策略管道尚未連接 — Sprint 5 整合後可用。",
                thread_ts=thread_ts,
            )
            return

        for mkt in markets:
            try:
                result, metrics = self._rebalance_fn(mkt)
                blocks = render_dashboard(result, metrics, mkt, _now_taipei())
                self.app.client.chat_postMessage(
                    channel=self._channel_id,
                    blocks=blocks,
                    text=f"[REBALANCE PREVIEW {mkt}]",
                    thread_ts=thread_ts,
                )
            except Exception as exc:
                logger.error("[SLACK] rebalance preview %s 失敗：%s", mkt, exc)
                say(text=f"[ERROR] Preview {mkt} 失敗：{exc}", thread_ts=thread_ts)

    def _on_holdings_show(
        self, message: dict, say: Callable, context: dict
    ) -> None:
        """Handle !holdings show [us|tw] — print current PortfolioState."""
        matches = context.get("matches", ())
        raw_market = str(matches[0]).upper() if matches and matches[0] else "ALL"
        markets_to_show = (
            ["US", "TW"] if raw_market not in ("US", "TW") else [raw_market]
        )
        thread_ts = message.get("ts", "")

        try:
            state = PortfolioState.load(self._state_path)
        except OSError:
            say(
                text="❌ 狀態檔不存在。請先建立 portfolio_state.json。",
                thread_ts=thread_ts,
            )
            return
        except Exception as exc:
            say(text=f"[ERROR] 讀取持倉失敗：{exc}", thread_ts=thread_ts)
            return

        lines = ["*📊 目前持倉*"]
        for mkt in markets_to_show:
            snap = state.market_snapshot(mkt)
            if mkt == "US":
                lines.append(f"\n🇺🇸 *US*  現金: ${snap['cash_usd']:.2f}")
                for ticker, qty in snap["holdings"].items():
                    lines.append(f"  • {ticker}: {qty:.2f} 股")
                if not snap["holdings"]:
                    lines.append("  (無持倉)")
            else:
                lines.append(f"\n🇹🇼 *TW*  現金: NT${snap['cash_twd']:.0f}")
                for ticker, qty in snap["holdings"].items():
                    lines.append(f"  • {ticker}: {qty:.0f} 股")
                if not snap["holdings"]:
                    lines.append("  (無持倉)")

        say(text="\n".join(lines), thread_ts=thread_ts)

    def _on_why(self, message: dict, say: Callable, context: dict) -> None:
        """Handle !why <TICKER> — show conviction breakdown from archive."""
        matches = context.get("matches", ())
        if not matches:
            return
        ticker = str(matches[0]).strip().upper()
        thread_ts = message.get("ts", "")

        rec = _find_latest_decision(self._decision_archive, ticker)
        if rec is None:
            say(
                text=f"❌ 找不到 `{ticker}` 的決策記錄。",
                thread_ts=thread_ts,
            )
            return

        trade_dict = next(
            (t for t in rec.get("trades", []) if t.get("ticker") == ticker), None
        )
        if trade_dict is None:
            say(
                text=f"❌ `{ticker}` 不在最新決策的 trade 列表中。",
                thread_ts=thread_ts,
            )
            return

        ts_str = rec.get("timestamp", "N/A")[:16]
        tier = trade_dict.get("execution_tier", "")
        lines = [
            f"━━ */why {ticker}* ━━",
            f"決策時間: {ts_str}",
            (
                f"方向: {trade_dict['side']}  |  "
                f"Conviction: {trade_dict['conviction']:.1f}/10  "
                f"{_tier_emoji(tier)} {tier}"
            ),
            f"理由: `{trade_dict.get('rationale', 'N/A')}`",
        ]

        bindings = trade_dict.get("bindings", [])
        lines.append(f"約束: {', '.join(bindings)}" if bindings else "約束: 無")

        components = trade_dict.get("conviction_components")
        if components:
            lines.extend([
                "",
                "*Conviction 分解:*",
                f"  Score delta:    {components['score_delta_pct']:.2f} × 0.40 = {components['score_delta_pct'] * 0.40:.2f}",
                f"  Binding:        {components['constraint_binding']:.2f} × 0.20 = {components['constraint_binding'] * 0.20:.2f}",
                f"  Cov certainty:  {components['cov_certainty']:.2f} × 0.15 = {components['cov_certainty'] * 0.15:.2f}",
                f"  Consistency:    {components['consistency']:.2f} × 0.15 = {components['consistency'] * 0.15:.2f}",
                f"  Timing:         {components['timing']:.2f} × 0.10 = {components['timing'] * 0.10:.2f}",
            ])
        else:
            lines.append("\n_Sub-components not archived — only summary available._")

        say(text="\n".join(lines), thread_ts=thread_ts)

    def _on_trade_add_msg(
        self, message: dict, say: Callable, context: dict
    ) -> None:
        """Handle !trade add — post a button that opens the trade modal."""
        thread_ts = message.get("ts", "")
        say(
            blocks=[
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "📝 Open Trade Form",
                                "emoji": True,
                            },
                            "style": "primary",
                            "action_id": "trade_open_modal",
                            "value": "open",
                        }
                    ],
                }
            ],
            text="請點擊按鈕開啟交易登記表單。",
            thread_ts=thread_ts,
        )

    # ── Sprint 4: modal handlers ──────────────────────────────────────────────

    def _on_trade_open_modal(self, ack: Callable, body: dict, client) -> None:
        """Open /trade add modal when button is clicked."""
        ack()
        trigger_id = body.get("trigger_id", "")
        if not trigger_id:
            return
        try:
            client.views_open(trigger_id=trigger_id, view=_TRADE_ADD_MODAL)
        except Exception as exc:
            logger.error("[SLACK] trade_open_modal 失敗：%s", exc)

    def _on_trade_add_submit(self, ack: Callable, body: dict, client) -> None:
        """Process /trade add modal submission — write actual_trades.jsonl + update state."""
        ack()
        values = body.get("view", {}).get("state", {}).get("values", {})
        user_id = body.get("user", {}).get("id", "")

        try:
            market = values["market"]["market_select"]["selected_option"]["value"]
            ticker = values["ticker"]["ticker_input"]["value"].strip().upper()
            side = values["side"]["side_select"]["selected_option"]["value"]
            quantity = float(values["quantity"]["quantity_input"]["value"])
            filled_price = float(values["filled_price"]["filled_price_input"]["value"])
            commission = float(
                (values.get("commission", {}).get("commission_input", {}).get("value") or "0")
            )
            tax = float(
                (values.get("tax", {}).get("tax_input", {}).get("value") or "0")
            )
            fx = float(
                (values.get("fx", {}).get("fx_input", {}).get("value") or "1.0")
            )
            sys_opts = (
                values.get("system_suggested", {})
                .get("system_suggested_check", {})
                .get("selected_options") or []
            )
            system_suggested = any(o.get("value") == "yes" for o in sys_opts)
        except (KeyError, ValueError, TypeError) as exc:
            logger.error("[SLACK] trade_add_submit 解析失敗：%s", exc)
            if user_id:
                client.chat_postMessage(
                    channel=user_id, text=f"[ERROR] 表單解析失敗：{exc}"
                )
            return

        if quantity <= 0 or filled_price <= 0:
            if user_id:
                client.chat_postMessage(
                    channel=user_id, text="❌ 數量與成交價必須大於 0。"
                )
            return

        today = _today_taipei()
        record = {
            "ticker": ticker,
            "side": side,
            "market": market,
            "date": today,
            "system_suggested": system_suggested,
            "quantity": quantity,
            "filled_price": filled_price,
            "commission": commission,
            "tax": tax,
            "fx": fx,
            "status": "reconciled",
        }

        try:
            _append_actual_trade(self._actual_trades_path, record)
        except OSError as exc:
            logger.error("[SLACK] actual_trades.jsonl 寫入失敗：%s", exc)

        try:
            state = PortfolioState.load(self._state_path)
            cash_delta = (
                -(quantity * filled_price + commission + tax)
                if side == "BUY"
                else quantity * filled_price - commission - tax
            )
            state.apply_trade(market, ticker, side, quantity, cash_delta)
            state.save(self._state_path)
        except (OSError, ValueError) as exc:
            logger.warning("[SLACK] /trade add: PortfolioState 更新失敗：%s", exc)

        if user_id:
            client.chat_postMessage(
                channel=user_id,
                text=f"✅ 已登記：{side} {ticker} × {quantity:.0f} @ {filled_price:.2f}",
            )

    # ── Sprint 4: dashboard action button handlers ────────────────────────────

    def _on_rebalance_approve(self, ack: Callable, body: dict, client) -> None:
        """Approve all Execute-tier trades: write actual_trades.jsonl + update state."""
        ack()
        market = body["actions"][0].get("value", "US").upper()
        channel = body["channel"]["id"]
        msg_ts = body["message"]["ts"]

        rec = _find_latest_active_decision(self._decision_archive, market)
        if rec is None:
            client.chat_postMessage(
                channel=channel,
                text=f"❌ 找不到 {market} 的有效決策記錄。",
                thread_ts=msg_ts,
            )
            return

        execute_trades = [
            t for t in rec.get("trades", []) if t.get("execution_tier") == "Execute"
        ]
        if not execute_trades:
            client.chat_postMessage(
                channel=channel,
                text="ℹ️ 本次決策無 Execute-tier trade，Approve 無效果。",
                thread_ts=msg_ts,
            )
            return

        today = _today_taipei()
        written: list[str] = []
        errors: list[str] = []

        for t in execute_trades:
            ticker = t["ticker"]
            side = t["side"]
            quantity = abs(float(t.get("quantity", 0)))
            est_price = float(t.get("est_price", 0))
            breakdown = t.get("est_cost_breakdown", {})
            commission = float(breakdown.get("commission", 0))
            tax = float(breakdown.get("tax", 0))

            act_record = {
                "ticker": ticker,
                "side": side,
                "market": market,
                "date": today,
                "system_suggested": True,
                "quantity": quantity,
                "filled_price": est_price,
                "commission": commission,
                "tax": tax,
                "fx": 1.0,
                "status": "pending_confirmation",
            }
            try:
                _append_actual_trade(self._actual_trades_path, act_record)
            except OSError as exc:
                errors.append(f"{ticker}: {exc}")
                continue

            try:
                state = PortfolioState.load(self._state_path)
                cash_delta = (
                    -(quantity * est_price + commission + tax)
                    if side == "BUY"
                    else quantity * est_price - commission - tax
                )
                state.apply_trade(market, ticker, side, quantity, cash_delta)
                state.save(self._state_path)
            except Exception as exc:
                logger.warning(
                    "[SLACK] Approve: PortfolioState 更新失敗 %s：%s", ticker, exc
                )

            logger.info(
                "[BROKER_STUB] Approve: %s %s %s qty=%.4f", side, ticker, market, quantity
            )
            written.append(f"{side} {ticker} × {quantity:.0f}")

        if written:
            client.chat_postMessage(
                channel=channel,
                text=(
                    f"✅ 已記錄 {len(written)} 筆 Execute-tier 交易 "
                    f"(status: pending_confirmation)：\n"
                    + "\n".join(f"  • {w}" for w in written)
                ),
                thread_ts=msg_ts,
            )
        if errors:
            client.chat_postMessage(
                channel=channel,
                text=f"⚠️ {len(errors)} 筆寫入失敗：\n" + "\n".join(errors),
                thread_ts=msg_ts,
            )

    def _on_rebalance_why_summary(self, ack: Callable, body: dict, client) -> None:
        """Show a summary of all trades in the latest decision for the market."""
        ack()
        market = body["actions"][0].get("value", "US").upper()
        channel = body["channel"]["id"]
        msg_ts = body["message"]["ts"]

        rec = _find_latest_active_decision(self._decision_archive, market)
        if rec is None:
            client.chat_postMessage(
                channel=channel, text="❌ 找不到決策記錄。", thread_ts=msg_ts
            )
            return

        lines = [f"━━ *Why — {market}* ({rec.get('timestamp', '')[:16]}) ━━"]
        for t in rec.get("trades", []):
            emoji = _tier_emoji(t.get("execution_tier", ""))
            lines.append(
                f"{emoji} {t['side']} *{t['ticker']}*  "
                f"{t['conviction']:.1f}/10  `{t.get('rationale', 'N/A')}`"
            )

        client.chat_postMessage(
            channel=channel, text="\n".join(lines), thread_ts=msg_ts
        )

    def _on_rebalance_skip_plan(self, ack: Callable, body: dict, client) -> None:
        """Acknowledge Skip plan — no trades recorded."""
        ack()
        channel = body["channel"]["id"]
        msg_ts = body["message"]["ts"]
        client.chat_postMessage(
            channel=channel,
            text="⏭ 本次計劃已跳過，不記錄任何交易。",
            thread_ts=msg_ts,
        )

    def _on_rebalance_expand(self, ack: Callable, body: dict, client) -> None:
        """Show full report for a HOLD decision."""
        ack()
        market = body["actions"][0].get("value", "US").upper()
        channel = body["channel"]["id"]
        msg_ts = body["message"]["ts"]

        if not self._decision_archive.exists():
            client.chat_postMessage(
                channel=channel, text="❌ 找不到決策記錄。", thread_ts=msg_ts
            )
            return

        try:
            lines_raw = self._decision_archive.read_text(encoding="utf-8").splitlines()
        except OSError:
            client.chat_postMessage(
                channel=channel, text="❌ 無法讀取決策存檔。", thread_ts=msg_ts
            )
            return

        rec = None
        for line in reversed(lines_raw):
            line = line.strip()
            if not line:
                continue
            try:
                candidate = json.loads(line)
                if candidate.get("market") == market:
                    rec = candidate
                    break
            except json.JSONDecodeError:
                continue

        if rec is None:
            client.chat_postMessage(
                channel=channel,
                text=f"❌ 找不到 {market} 的決策記錄。",
                thread_ts=msg_ts,
            )
            return

        hold_str = "🟢 HOLD" if rec.get("is_hold") else f"🟡 Active ({len(rec.get('trades', []))} trades)"
        trades_text = "\n".join(
            f"  {t['side']} {t['ticker']}: conviction {t['conviction']:.1f}, {t.get('rationale', 'N/A')}"
            for t in rec.get("trades", [])
        ) or "  (無交易記錄)"

        client.chat_postMessage(
            channel=channel,
            text=f"*📋 完整報告 — {market}*  {hold_str}\n{trades_text}",
            thread_ts=msg_ts,
        )

    # ── Sprint 4: stretch command stubs ───────────────────────────────────────

    def _on_stub_cash(self, message: dict, say: Callable, context: dict) -> None:
        say(text="TODO: Sprint 5 — /cash 命令 (show/set/adjust)")

    def _on_stub_holdings_sync(self, message: dict, say: Callable, context: dict) -> None:
        say(text="TODO: Sprint 5 — /holdings sync (CSV 上傳)")

    def _on_stub_ipo(self, message: dict, say: Callable, context: dict) -> None:
        say(text="TODO: Sprint 5 — /ipo 命令 (apply/release/list)")

    def _on_stub_fx(self, message: dict, say: Callable, context: dict) -> None:
        say(text="TODO: Sprint 5 — /fx record")

    def _on_stub_cost(self, message: dict, say: Callable, context: dict) -> None:
        say(text="TODO: Sprint 5 — /cost 命令 (show/set/reset)")

    def _on_stub_reconcile(self, message: dict, say: Callable, context: dict) -> None:
        say(text="TODO: Sprint 5 — /reconcile 命令")

    def _on_stub_rebalance_config(self, message: dict, say: Callable, context: dict) -> None:
        say(text="TODO: Sprint 5 — /rebalance config 命令")

    # ── V1.1: Text command handler ────────────────────────────────────────────

    def _on_analyze_text(self, message: dict, say: Callable, context: dict) -> None:
        """
        Handle !analyze [TICKER] or /analyze [TICKER] text commands.
        Replies in the originating message's thread.
        """
        matches   = context.get("matches", ())
        if not matches:
            return

        ticker    = str(matches[0]).strip().upper()
        thread_ts = message.get("ts", "")

        say(
            text=f":hourglass_flowing_sand: 正在分析 `{ticker}`，請稍候...",
            thread_ts=thread_ts,
        )

        if self._adhoc_fn is None:
            say(text="[ERROR] 分析模組未載入，請檢查系統設定。", thread_ts=thread_ts)
            return

        try:
            report = self._adhoc_fn(ticker)
            self._post_chunks(report, thread_ts=thread_ts)
        except Exception as exc:
            logger.error("[SLACK] !analyze %s 失敗：%s", ticker, exc)
            say(text=f"[ERROR] `{ticker}` 分析失敗：{exc}", thread_ts=thread_ts)

    # ── Button action handlers ────────────────────────────────────────────────

    def _on_analyze_button(self, ack: Callable, body: dict, client) -> None:
        """
        Handle 🔍 Analyze button clicks from the Block Kit dashboard.

        Flow:
          1. ack() immediately (Slack requires acknowledgment within 3 s).
          2. Extract the original ticker from action["value"] (unsanitized).
          3. Post "analyzing..." in the source message's thread.
          4. Run generate_adhoc_analysis and post the LLM report in the thread.

        Args:
            ack:    Slack Bolt acknowledgment callable — must be called first.
            body:   Full interaction payload from Slack.
            client: Slack WebClient bound to the bot token.
        """
        ack()

        action    = body["actions"][0]
        ticker    = action.get("value", "").strip().upper()
        channel   = body["channel"]["id"]
        msg_ts    = body["message"]["ts"]

        if not ticker:
            return

        client.chat_postMessage(
            channel=channel,
            text=f":hourglass_flowing_sand: 正在分析 `{ticker}`，請稍候...",
            thread_ts=msg_ts,
        )

        if self._adhoc_fn is None:
            client.chat_postMessage(
                channel=channel,
                text="[ERROR] 分析模組未載入，請檢查系統設定。",
                thread_ts=msg_ts,
            )
            return

        try:
            report = self._adhoc_fn(ticker)
            self._post_chunks(report, channel=channel, thread_ts=msg_ts, client=client)
        except Exception as exc:
            logger.error("[SLACK] analyze_button %s 失敗：%s", ticker, exc)
            client.chat_postMessage(
                channel=channel,
                text=f"[ERROR] `{ticker}` 分析失敗：{exc}",
                thread_ts=msg_ts,
            )

    def _on_refresh_all(self, ack: Callable, body: dict, client) -> None:
        """
        Handle 🔄 Refresh All button click.

        Triggers a full portfolio scan (calls refresh_fn) and posts the
        new Block Kit dashboard to the channel. Status messages go to the
        thread of the clicked message.

        Args:
            ack:    Slack Bolt acknowledgment callable.
            body:   Interaction payload.
            client: Slack WebClient.
        """
        ack()

        channel = body["channel"]["id"]
        msg_ts  = body["message"]["ts"]

        client.chat_postMessage(
            channel=channel,
            text=":arrows_counterclockwise: 正在執行全量掃描，請稍候（約 30–60 秒）...",
            thread_ts=msg_ts,
        )

        if self._refresh_fn is None:
            client.chat_postMessage(
                channel=channel,
                text="[ERROR] Refresh 功能未設定，請檢查 main.py 的 run() 呼叫。",
                thread_ts=msg_ts,
            )
            return

        try:
            self._refresh_fn()
            client.chat_postMessage(
                channel=channel,
                text=":white_check_mark: 掃描完成，新報告已發布至頻道。",
                thread_ts=msg_ts,
            )
        except Exception as exc:
            logger.error("[SLACK] global_refresh_all 失敗：%s", exc)
            client.chat_postMessage(
                channel=channel,
                text=f"[ERROR] Refresh 失敗：{exc}",
                thread_ts=msg_ts,
            )

    def _on_check_auctions(self, ack: Callable, body: dict, client) -> None:
        """
        Handle 🔶 Check Auctions button click.

        Calls check_auctions_fn (get_tw_auctions) and posts the results
        as a formatted mrkdwn message in the thread.

        Args:
            ack:    Slack Bolt acknowledgment callable.
            body:   Interaction payload.
            client: Slack WebClient.
        """
        ack()

        channel = body["channel"]["id"]
        msg_ts  = body["message"]["ts"]

        if self._check_auctions_fn is None:
            client.chat_postMessage(
                channel=channel,
                text="[ERROR] Check Auctions 功能未設定。",
                thread_ts=msg_ts,
            )
            return

        try:
            auctions = self._check_auctions_fn()

            if not auctions:
                client.chat_postMessage(
                    channel=channel,
                    text="🔶 目前無近期競拍 / 增資事件（TWSE + TPEX）。",
                    thread_ts=msg_ts,
                )
                return

            lines = ["*🔶 近期台股競拍 / 增資事件*", ""]
            for a in auctions:
                lines.append(
                    f"`{a.get('ticker', '')}` {a.get('name', '')} — "
                    f"[{a.get('kind', '')}]  "
                    f"Date: {a.get('date', 'N/A')}  |  "
                    f"Floor/Issue: NTD {a.get('floor_price', 'N/A')}  |  "
                    f"{a.get('exchange', '')}"
                )

            client.chat_postMessage(
                channel=channel,
                text="\n".join(lines),
                mrkdwn=True,
                thread_ts=msg_ts,
            )
        except Exception as exc:
            logger.error("[SLACK] global_check_auctions 失敗：%s", exc)
            client.chat_postMessage(
                channel=channel,
                text=f"[ERROR] Check Auctions 失敗：{exc}",
                thread_ts=msg_ts,
            )

    # ── Message sending ───────────────────────────────────────────────────────

    def _post_chunks(
        self,
        text:      str,
        channel:   str = "",
        thread_ts: str = "",
        client=None,
    ) -> None:
        """
        Split long mrkdwn text and post each chunk to the channel.

        Args:
            text:      Full mrkdwn message body.
            channel:   Target channel ID; defaults to self._channel_id.
            thread_ts: If set, posts in this message's thread.
            client:    Slack WebClient to use; defaults to self.app.client.
        """
        if not text:
            return
        _client  = client or self.app.client
        _channel = channel or self._channel_id
        chunks   = [text[i : i + _MAX_SLACK_LEN] for i in range(0, len(text), _MAX_SLACK_LEN)]

        for chunk in chunks:
            try:
                kwargs: dict = {"channel": _channel, "text": chunk, "mrkdwn": True}
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                _client.chat_postMessage(**kwargs)
            except Exception as exc:
                logger.error("[SLACK] chat_postMessage 失敗：%s", exc)

    def send_report(self, blocks: list[dict]) -> str | None:
        """
        Send the Block Kit dashboard to the main channel.

        Uses chat_postMessage with blocks= for rich UI.
        The text= parameter provides an accessible fallback for
        mobile push notifications and screen readers.

        Thread-safe: Slack SDK WebClient is synchronous and can be
        called from APScheduler background threads without bridges.

        Args:
            blocks: Block Kit block list from generate_daily_report().

        Returns:
            The message timestamp (ts) of the posted dashboard, or None on
            failure.  Callers should pass this ts to send_text(..., thread_ts=ts)
            to attach follow-up research reports as threaded replies.
        """
        logger.info(
            "[SLACK] 發送 Block Kit 看板至頻道 %s（%d blocks）...",
            self._channel_id, len(blocks),
        )
        try:
            resp = self.app.client.chat_postMessage(
                channel=self._channel_id,
                blocks=blocks,
                text="[WARDEN SCAN REPORT] — 請在 Slack 中查看互動式看板。",
            )
            ts = resp.get("ts")
            logger.info("[SLACK] 看板發送完成（ts=%s）。", ts)
            return ts
        except Exception as exc:
            logger.error("[SLACK] send_report 失敗：%s", exc)
            return None

    def send_text(self, text: str, thread_ts: str | None = None) -> str | None:
        """
        Post a plain mrkdwn text message to the main channel.

        Used for research reports and other non-Block-Kit payloads.
        Long text is automatically split into ≤3,000-char chunks via
        the existing _post_chunks helper.

        Args:
            text:      Slack mrkdwn string to send.
            thread_ts: Optional thread timestamp to reply in-thread.

        Returns:
            Timestamp (ts) of the posted message, or None on failure.
            The ts can be used to thread follow-up messages.
        """
        logger.info(
            "[SLACK] send_text → 頻道 %s（%d chars%s）",
            self._channel_id, len(text),
            ", in-thread" if thread_ts else "",
        )
        try:
            resp = self.app.client.chat_postMessage(
                channel=self._channel_id,
                text=text,
                mrkdwn=True,
                **({"thread_ts": thread_ts} if thread_ts else {}),
            )
            ts = resp.get("ts")
            logger.info("[SLACK] send_text 完成（ts=%s）", ts)
            return ts
        except Exception as exc:
            logger.error("[SLACK] send_text 失敗：%s", exc)
            return None

    def upload_charts(self, chart_paths: list) -> None:
        """
        Upload diagnostic PNG charts to the main channel via files_upload_v2.

        Each chart is uploaded as a separate file message so Slack renders
        them inline.  Failures are logged but do not raise — a missing chart
        must never abort the main reporting pipeline.

        Args:
            chart_paths: List of pathlib.Path objects pointing to PNG files.
        """
        if not chart_paths:
            return

        titles = {
            "ipo_valuation_gap": "IPO Value Gap — Auction Price Levels",
            "alpha_quadrant":    "Alpha Quadrant — Valuation vs. Momentum",
        }
        comments = {
            "ipo_valuation_gap": "📊 IPO Value Gap: Floor · Fair Value · Ceiling · Market Price",
            "alpha_quadrant":    "📍 Alpha Quadrant: Modified PEG vs. 50-day MA Distance",
        }

        for path in chart_paths:
            stem  = path.stem
            title = titles.get(stem, stem.replace("_", " ").title())
            comment = comments.get(stem, "")
            try:
                with open(path, "rb") as fh:
                    self.app.client.files_upload_v2(
                        channel=self._channel_id,
                        file=fh,
                        filename=path.name,
                        title=title,
                        initial_comment=comment,
                    )
                logger.info("[SLACK] 圖表已上傳：%s", path.name)
            except Exception as exc:
                logger.error("[SLACK] 圖表上傳失敗 %s：%s", path.name, exc)

    # ── Start ─────────────────────────────────────────────────────────────────

    def run(
        self,
        adhoc_analysis_fn:  Optional[Callable[[str], str]]     = None,
        refresh_fn:         Optional[Callable[[], None]]       = None,
        check_auctions_fn:  Optional[Callable[[], list[dict]]] = None,
    ) -> None:
        """
        Start the Socket Mode handler (blocking — call last in main thread).

        Socket Mode Handler maintains the WebSocket in a background thread
        and dispatches events; the main thread sleeps until SIGINT/SIGTERM.

        APScheduler jobs call send_report() from background threads;
        Slack SDK WebClient is thread-safe — no extra synchronization needed.

        Args:
            adhoc_analysis_fn:  Callable(ticker) → mrkdwn str (LLM deep analysis).
            refresh_fn:         Callable() → None; triggers a fresh portfolio scan.
            check_auctions_fn:  Callable() → list[dict]; fetches TW auction events.
        """
        self._adhoc_fn          = adhoc_analysis_fn
        self._refresh_fn        = refresh_fn
        self._check_auctions_fn = check_auctions_fn

        handler = SocketModeHandler(self.app, self._app_token)
        handler.start()  # launches WebSocket in background thread, returns immediately

        logger.info(
            "[SLACK] Socket Mode 啟動，監聽頻道 %s 的訊息與 Block Kit 互動...",
            self._channel_id,
        )

        while True:
            time.sleep(1)
