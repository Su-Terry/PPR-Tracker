"""
Alpha Strategist — Block Kit Dashboard Renderer (V2.0 Sprint 4)

Pure function: BuildResult + DisciplineMetrics → list[Block]

Two layouts:
  Trade plan: full dashboard with trade list and Approve/Why/Skip buttons.
  HOLD:       compact view with discipline metrics and "Show full report" button.

Skip-tier trades are rendered in a context block with ~strikethrough~ mrkdwn
and are excluded from the Approve action scope (spec D-S4-1).
"""

from __future__ import annotations

from typing import Literal

from src.discipline.metrics import DisciplineMetrics
from src.rebalancer.trade_builder import BuildResult, Trade

_MARKET_FLAG: dict[str, str] = {"US": "🇺🇸", "TW": "🇹🇼"}
_TIER_EMOJI: dict[str, str] = {"Execute": "✅", "Watch": "⏸", "Skip": "❌"}
_HOLD_REASON_TEXT: dict[str, str] = {
    "infeasible": "optimizer infeasible — constraints too tight",
    "min_turnover": "drift within tolerance",
    "low_conviction": "all conviction scores < 4.0",
    "low_notional": "all trade sizes below minimum",
}


def render_dashboard(
    result: BuildResult,
    metrics: DisciplineMetrics,
    market: Literal["US", "TW"],
    timestamp: str,
) -> list[dict]:
    """
    Render a Slack Block Kit dashboard for one market's rebalance result.

    Parameters
    ----------
    result:
        BuildResult from build_trades(). May be HOLD or active plan.
    metrics:
        DisciplineMetrics computed from the actuals provider.
    market:
        "US" or "TW" — used for market flag and action IDs.
    timestamp:
        Human-readable timestamp string (e.g. "2026-05-12 08:30").

    Returns
    -------
    list[dict]
        Slack Block Kit block list. Always ≤ 50 blocks.
    """
    flag = _MARKET_FLAG.get(market, market)
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Alpha Strategist | {timestamp}  {flag} {market}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    if result.is_hold:
        blocks.extend(_hold_conclusion(result))
    else:
        blocks.extend(_trade_conclusion(result))

    blocks.append({"type": "divider"})
    blocks.extend(_discipline_blocks(metrics))

    if result.is_hold:
        blocks.extend(_hold_actions(market))
    else:
        blocks.extend(_trade_detail_blocks(result, market))

    return blocks


# ── Private block builders ────────────────────────────────────────────────────

def _hold_conclusion(result: BuildResult) -> list[dict]:
    reason = result.hold_reasons[0] if result.hold_reasons else "min_turnover"
    reason_text = _HOLD_REASON_TEXT.get(reason, reason)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"━━ *今日結論* ━━\n🟢 HOLD all — {reason_text}",
            },
        }
    ]


def _trade_conclusion(result: BuildResult) -> list[dict]:
    execute = [t for t in result.trades if t.execution_tier == "Execute"]
    watch = [t for t in result.trades if t.execution_tier == "Watch"]
    skip = [t for t in result.trades if t.execution_tier == "Skip"]

    if execute:
        summary = (
            f"🟡 建議調整 {len(execute)} 筆  "
            f"(✅ Execute: {len(execute)} | ⏸ Watch: {len(watch)} | ❌ Skip: {len(skip)})"
        )
    else:
        summary = f"🔴 無 Execute 建議  (⏸ Watch: {len(watch)} | ❌ Skip: {len(skip)})"

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"━━ *今日結論* ━━\n{summary}"},
        }
    ]


def _discipline_blocks(metrics: DisciplineMetrics) -> list[dict]:
    drift_emoji = "🔴" if metrics.drift_pct > 10 else "🟢"
    turnover_emoji = (
        "🔴" if metrics.turnover_30d > 20
        else "🟡" if metrics.turnover_30d > 10
        else "🟢"
    )
    last_trade_text = (
        f"{metrics.last_trade_days} days" if metrics.last_trade_days >= 0 else "never"
    )
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "━━ *紀律儀表* ━━"},
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Drift:* {metrics.drift_pct:.1f}% {drift_emoji}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*30d Turnover:* {metrics.turnover_30d:.1f}% {turnover_emoji}",
                },
                {"type": "mrkdwn", "text": f"*Last trade:* {last_trade_text}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Discipline (7d):* {metrics.discipline_score_7d}/100",
                },
            ],
        },
    ]


def _hold_actions(market: str) -> list[dict]:
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Show full report",
                        "emoji": True,
                    },
                    "action_id": "rebalance_expand",
                    "value": market,
                }
            ],
        }
    ]


def _trade_detail_blocks(result: BuildResult, market: str) -> list[dict]:
    execute_trades = [t for t in result.trades if t.execution_tier == "Execute"]
    watch_trades = [t for t in result.trades if t.execution_tier == "Watch"]
    skip_trades = [t for t in result.trades if t.execution_tier == "Skip"]

    currency = "$" if market == "US" else "NT$"
    total_cost = sum(t.est_cost for t in result.trades)
    total_turnover = sum(abs(t.delta_weight) for t in result.trades) * 50

    blocks: list[dict] = [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"━━ *詳情* ━━\n"
                    f"*Turnover: {total_turnover:.1f}%* | "
                    f"*Est Cost: {currency}{total_cost:.2f}*"
                ),
            },
        },
    ]

    active_trades = execute_trades + watch_trades
    if active_trades:
        lines = [_format_trade_line(t, currency) for t in active_trades]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        )

    if skip_trades:
        skip_lines = [_format_skip_line(t, currency) for t in skip_trades]
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "\n".join(skip_lines)
                            + "\n_Skip-tier: conviction below threshold_"
                        ),
                    }
                ],
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "rebalance_approve",
                    "value": market,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔍 Why?", "emoji": True},
                    "action_id": "rebalance_why_summary",
                    "value": market,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "❌ Skip plan",
                        "emoji": True,
                    },
                    "style": "danger",
                    "action_id": "rebalance_skip_plan",
                    "value": market,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Skip this plan?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": "No trades will be recorded.",
                        },
                        "confirm": {"type": "plain_text", "text": "Yes, skip"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            ],
        }
    )

    return blocks


def _format_trade_line(t: Trade, currency: str) -> str:
    emoji = _TIER_EMOJI.get(t.execution_tier, "•")
    side_str = "BUY " if t.side == "BUY" else "SELL"
    qty_sign = "+" if t.side == "BUY" else "−"
    return (
        f"{emoji} {side_str} *{t.ticker}*  {t.conviction:.1f}/10  "
        f"`{t.rationale}`   qty {qty_sign}{abs(t.quantity):.0f}  "
        f"Est: {currency}{t.est_cost:.2f}"
    )


def _format_skip_line(t: Trade, currency: str) -> str:
    side_str = "BUY " if t.side == "BUY" else "SELL"
    return (
        f"~❌ {side_str} {t.ticker}  {t.conviction:.1f}/10  "
        f"`{t.rationale}`  qty {abs(t.quantity):.0f}  "
        f"Est: {currency}{t.est_cost:.2f}~"
    )
