#!/usr/bin/env python3
"""
Sprint 5 smoke test — dry-run the V2.0 pipeline against real yfinance prices.

Usage:
    uv run python scripts/smoke_sprint5.py --market US
    uv run python scripts/smoke_sprint5.py --market US --post-slack

Procedure (Q4 from plan):
  1. Seeds a minimal portfolio_state.json with 2-3 real US tickers
  2. Calls runner.run("US") with decisions_path → memory/sprint5_smoke.jsonl
  3. Prints assertions to stdout
  4. Optionally posts the Block Kit dashboard to SLACK_SMOKE_CHANNEL_ID

This script is a throw-away helper — not tested, not part of the production path.
Call with V2_BOOT_DRY_RUN=true to route the full main() boot through the smoke channel.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path when run via `uv run python scripts/...`
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.portfolio.state import PortfolioState
from src.rebalancer.runner import run as runner_run

_SMOKE_DECISIONS = Path("memory/sprint5_smoke.jsonl")
_STATE_PATH      = Path("data/portfolio_state.json")
_BACKUP_PATH     = Path("data/portfolio_state.backup.json")


# ── Seed portfolio ─────────────────────────────────────────────────────────────

def _seed_us_portfolio() -> None:
    """Write a minimal 3-ticker US portfolio for smoke purposes."""
    state = PortfolioState.create_empty()
    state.us_cash_usd  = 5_000.0
    state.us_holdings  = {
        "NVDA": 3.0,
        "AAPL": 10.0,
        "MSFT": 5.0,
    }
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state.save(_STATE_PATH)
    print(f"[SMOKE] Seeded portfolio → {_STATE_PATH}")
    print(f"        us_cash_usd={state.us_cash_usd}  holdings={state.us_holdings}")


# ── Assertions ────────────────────────────────────────────────────────────────

def _assert_result(result, metrics, scan_data) -> None:
    import numpy as np

    print("\n── Smoke assertions ──────────────────────────────────────────")

    # is_hold
    print(f"  is_hold:          {result.is_hold}")

    # conviction range
    convictions = [t.conviction for t in result.trades] if result.trades else []
    if convictions:
        lo, hi = min(convictions), max(convictions)
        assert all(0.0 <= c <= 10.0 for c in convictions), (
            f"FAIL: conviction out of [0,10]: {convictions}"
        )
        print(f"  conviction_range: [{lo:.2f}, {hi:.2f}]  ✅")
    else:
        print("  conviction_range: [] (HOLD — no trades)")

    # NaN / inf in weights
    weights = [t.target_weight for t in result.trades]
    nan_in  = any(np.isnan(w) for w in weights)
    inf_in  = any(np.isinf(w) for w in weights)
    print(f"  nan_in_weights:   {nan_in}")
    print(f"  inf_in_weights:   {inf_in}")
    assert not nan_in, "FAIL: NaN in target_weight"
    assert not inf_in, "FAIL: inf in target_weight"
    if weights:
        print("  weights OK        ✅")

    # archive written
    assert _SMOKE_DECISIONS.exists(), "FAIL: smoke archive not written"
    lines = [l for l in _SMOKE_DECISIONS.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1, "FAIL: archive is empty"
    last = json.loads(lines[-1])
    assert last.get("market") == "US", f"FAIL: last record market={last.get('market')}"
    print(f"  archive_records:  {len(lines)}  ✅")

    print("── All assertions passed ─────────────────────────────────────\n")


# ── Block Kit post ────────────────────────────────────────────────────────────

def _post_to_slack(result, metrics, market: str = "US") -> None:
    from datetime import datetime, timezone, timedelta
    from src.slack.dashboard import render_dashboard

    _TZ_TAIPEI = timezone(timedelta(hours=8))
    ts = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")

    blocks = render_dashboard(result, metrics, market=market, timestamp=ts)
    block_count = len(blocks)
    print(f"  block_count:      {block_count}")
    assert block_count >= 5, f"FAIL: only {block_count} blocks (expected ≥ 5)"
    print(f"  Block Kit JSON valid ✅ ({block_count} blocks)")

    smoke_channel = os.environ.get("SLACK_SMOKE_CHANNEL_ID") or os.environ.get("SLACK_CHANNEL_ID")
    if not smoke_channel:
        print("\n[SMOKE] --post-slack skipped: SLACK_SMOKE_CHANNEL_ID and SLACK_CHANNEL_ID both unset")
        print("        Block Kit JSON (first 2 blocks):")
        print(json.dumps(blocks[:2], ensure_ascii=False, indent=2))
        return

    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    if not slack_token:
        print("\n[SMOKE] --post-slack skipped: SLACK_BOT_TOKEN not set")
        return

    from slack_sdk import WebClient
    client = WebClient(token=slack_token)
    resp = client.chat_postMessage(
        channel=smoke_channel,
        text=f"[SMOKE] Sprint 5 dry-run — {market}",
        blocks=blocks,
    )
    print(f"\n[SMOKE] Posted to {smoke_channel}  ts={resp['ts']}")
    print("        Screenshot the rendered message and paste into the PR description.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint 5 smoke test")
    parser.add_argument("--market", default="US", choices=["US", "TW"])
    parser.add_argument("--post-slack", action="store_true",
                        help="Post Block Kit dashboard to SLACK_SMOKE_CHANNEL_ID")
    args = parser.parse_args()

    # Step 1 — backup existing state if present
    if _STATE_PATH.exists():
        import shutil
        shutil.copy2(_STATE_PATH, _BACKUP_PATH)
        print(f"[SMOKE] Backed up existing state → {_BACKUP_PATH}")

    try:
        # Step 1 — seed
        _seed_us_portfolio()

        # Step 2 — run pipeline
        print(f"\n[SMOKE] Running runner.run('{args.market}') against real yfinance …")
        result, metrics, scan_data = runner_run(
            args.market,
            state_path=_STATE_PATH,
            decisions_path=_SMOKE_DECISIONS,
        )
        print(f"[SMOKE] run() complete — is_hold={result.is_hold}  trades={len(result.trades)}")

        # Steps 3-5 — stdout assertions
        _assert_result(result, metrics, scan_data)

        # Step 5 — validate archive JSON
        records = [json.loads(l) for l in _SMOKE_DECISIONS.read_text().splitlines() if l.strip()]
        print(f"[SMOKE] Archive JSONL parseable — {len(records)} record(s)  ✅")

        # Steps 6-7 — optional Slack post
        if args.post_slack:
            _post_to_slack(result, metrics, market=args.market)

    finally:
        # Step 9 — restore original state
        if _BACKUP_PATH.exists():
            import shutil
            shutil.copy2(_BACKUP_PATH, _STATE_PATH)
            _BACKUP_PATH.unlink()
            print(f"\n[SMOKE] Restored original state from backup.")
        elif _STATE_PATH.exists():
            _STATE_PATH.unlink()
            print(f"\n[SMOKE] Removed seeded state (no original to restore).")


if __name__ == "__main__":
    main()
