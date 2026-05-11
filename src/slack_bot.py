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

import logging
import os
import re
import time
from typing import Callable, Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logger = logging.getLogger(__name__)

_MAX_SLACK_LEN   = 3000   # conservative split length for mrkdwn messages
_ANALYZE_PATTERN = re.compile(r"[!/]analyze\s+(\S+)", re.IGNORECASE)
_ACTION_ANALYZE  = re.compile(r"^analyze_ticker_.*")


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
        bot_token:  str,
        app_token:  str,
        channel_id: str,
    ) -> None:
        self._app_token   = app_token
        self._channel_id  = channel_id

        self._adhoc_fn:           Optional[Callable[[str], str]]     = None
        self._refresh_fn:         Optional[Callable[[], None]]       = None
        self._check_auctions_fn:  Optional[Callable[[], list[dict]]] = None

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
        # Text command: !analyze TICKER or /analyze TICKER
        self.app.message(_ANALYZE_PATTERN)(self._on_analyze_text)

        # Block Kit button: 🔍 Analyze (action_id = "analyze_ticker_<SAFE_TICKER>")
        self.app.action(_ACTION_ANALYZE)(self._on_analyze_button)

        # Block Kit button: 🔄 Refresh All
        self.app.action("global_refresh_all")(self._on_refresh_all)

        # Block Kit button: 🔶 Check Auctions
        self.app.action("global_check_auctions")(self._on_check_auctions)

        # Silence Slack's message_changed / message_deleted subtype events.
        # Bolt emits a 404 warning for every edit/delete in the channel unless
        # these subtypes are explicitly acknowledged with a no-op handler.
        self.app.event({"type": "message", "subtype": "message_changed"})(lambda body: None)
        self.app.event({"type": "message", "subtype": "message_deleted"})(lambda body: None)

    # ── Text command handler ──────────────────────────────────────────────────

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
