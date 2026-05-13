# 專案代號：Alpha Strategist (CFO Warden System)

## 🎯 系統定位與核心守則 (System Persona)

- 你是本專案的「首席系統架構師」。你的任務是協助開發一個跨台美股的 Agentic AI 監控系統。
- 專案核心邏輯：成長動能強、修正版林區公式 (Modified PEG)、結構性調整分析、維持低換手率。
- 系統角色（V2.0 起明確化）：**紀律錨點**，不是決策輔助、不是省時 agent。目的是提供穩定、可審查、可量化的決策基準，當使用者想偏離時，系統的存在讓他停下來自問「是系統說的還是我想動？」。

- **絕對禁忌 (CRITICAL)**：
1. **禁止 strong forecasting**：個股或市場的點估計報酬（如 "NVDA 下月 +8%"）、擇時宣稱（"現在該進場"）、總經預測（"FED 12 月降息"）一律禁止。**允許 weak forecasting**：橫斷面相對評分（"A 比 B 好"）、條件機率聲明（"歷史上 high momentum 組在 macro=bull 時表現較好"）、二階矩估計（波動度、相關性）。Score 與 covariance 屬於 weak forecasting，是合法決策輸入。
2. 絕對禁止在沒有呼叫 API 的情況下「幻覺 (Hallucinate)」任何股票報價或財報數據。
3. 系統定位為「Data Gateway」與「推播引擎」，絕對禁止撰寫任何會直接觸發真實下單的程式碼。人類必須留在迴圈內 (Human-in-the-Loop)。

## 🛠️ 環境與工具管理 (Strict Dependency Management)

本專案**強制且唯一**使用 **`uv`** 作為 Python 環境與套件管理工具。禁止使用傳統的 `pip`、`poetry` 或 `conda`。

- **執行 Python 腳本**：一律使用 `uv run <script_name.py>`
- **新增依賴套件**：一律使用 `uv add <package_name>`
- **管理開發工具**：使用 `uv tool run`
- **虛擬環境**：依賴 `uv` 自動管理的 `.venv`，不要手動干預。

## 🧱 核心技術棧 (Tech Stack)

- **語言**：Python 3.12+
- **Agent 通訊協定**：`fastmcp` (Model Context Protocol)
- **數據獲取**：`yfinance` (台/美股報價與基本面)
- **數據處理**：`pandas`
- **自動化排程**：`apscheduler`
- **最佳化求解（V2.0 起）**：`cvxpy` + CLARABEL solver
- **共變異數估計（V2.0 起）**：`scikit-learn` Ledoit-Wolf shrinkage

## 💻 程式碼架構規範 (Coding Guidelines)

1. **微服務架構 (Microservices)**：將「資料獲取 (Data Ingestion)」、「策略運算 (Strategy Engine)」與「推播介面 (Push Notification)」徹底解耦。
2. **型別提示 (Type Hinting)**：所有的 Python 函式必須包含嚴格的 Type Hints，並提供完整的 Docstrings 說明其物理意義。
3. **錯誤處理 (Error Handling)**：在處理 API 請求（如讀取 CSV、呼叫 yfinance）時，必須實作完整的 `try-except` 區塊，若發生斷線或缺漏資料，應回傳乾淨的錯誤訊息，不可導致系統崩潰。
4. **無狀態設計 (Stateless)**：MCP 工具函式必須是無狀態的，每一次呼叫都應該是獨立且可預期的 (Deterministic)。

## 📁 目錄結構規劃參考 (Project Structure)

- `/data/`：存放從券商（如國泰）匯出的靜態 CSV 檔案。
- `/src/mcp_servers/`：存放 FastMCP 的 API Gateway 程式碼。
- `/src/strategies/`：存放量化邏輯（如修正版林區公式）的純 Python 腳本。
- `/src/notifications/`：存放 Slack/Discord Webhook 推播邏輯。
- `/src/portfolio/`（V2.0 新增）：portfolio state、sector mapping 等資料層。
- `/src/cost/`（V2.0 新增）：交易成本估計與 profile 管理。
- `/src/rebalancer/`（V2.0 Sprint 2+）：全域再平衡 optimizer。
- `/docs/`：設計文件，目前包含 `ALPHA_STRATEGIST_V2_SPEC.md` (V2.0 spec v0.3)。

## V2.0 Sprint 1 — Foundation Data Layer (2026-05-12)

Three new modules were added by `feat/v2-rebalancer-foundation`. They are standalone and not yet wired into `main.py` — V1.1 behavior is unchanged.

- `src/portfolio/state.py` — JSON-backed `PortfolioState` dataclass (cash, holdings, IPO subscriptions, lockup tickers per spec §7.1–7.2). State file lives at `data/portfolio_state.json` (not committed; create with `PortfolioState.create_empty().save(path)`).
- `data/sector_mapping.csv` — L1/L2 sector classification for optimizer `max_sector` constraints (spec §7.3). Missing tickers fall back to `("其他", "unmapped")` at runtime and are logged.
- `src/cost/profile.py` — `CostProfile` dataclass with §6.1 textbook defaults, `/cost set` manual override, and data collection stub for V2.1 statistical learning (spec §6.2, §7.6). Default file `data/cost_profile.json` is committed with IBKR-like US rates and Cathay pre-discount TW rates.

## V2.0 Sprint 2 — Optimizer Core (2026-05-12)

Four new modules were added by `feat/v2-optimizer-core`. They are standalone and not yet wired into `main.py` — V1.1 behavior is unchanged. New runtime deps: `cvxpy`, `clarabel`, `scikit-learn`, `numpy` (direct).

- `src/rebalancer/config.py` — `RebalanceConfig` frozen dataclass (spec §4.3 + §7.4). `us_default()` and `tw_default()` class methods carry all constraint and lambda parameters. `min_total_turnover` uses the L1 norm (buy + sell summed separately), so the default 0.02 equals ~1% one-way equivalent.
- `src/rebalancer/cov_estimator.py` — `estimate_covariance()` returns a `CovEstimate` with an annualised N×N matrix via Ledoit-Wolf shrinkage (scikit-learn). Falls back to a diagonal matrix when the common-window row count is below `lookback_days_min` (spec §11 Risk row 4). Cash/safe-haven proxies (BOXX, SGOV) do not cause singularity — Ledoit-Wolf's shrinkage target guarantees PSD; the diagonal fallback uses a 1%-vol prior for tickers with < 5 data points.
- `src/rebalancer/optimizer.py` — `solve_target_weights()` runs the §4.2 QP via cvxpy + CLARABEL. On infeasibility, auto-relaxes `max_turnover → max_sector → cash_floor` (+5 pp each, max 3 attempts), then returns `w0` with `infeasible=True` (spec deviation D2: returns instead of raising, to enable Sprint 4 dashboard HOLD display). Outputs an `OptimizeResult` with `relaxations_applied` for `/why` debugging.
- `src/rebalancer/conviction.py` — `compute_convictions()` scores each trade 0–10 using five pure-rule components: `score_delta` percentile rank (40%), max constraint binding ratio (20%), cov certainty linear scale (15%), consistency neutral stub pending Sprint 3 (15%), timing days-since-last-trade (10%). `execution_tier()` maps scores to Execute/Watch/Skip per spec §4.5.

## V2.0 Sprint 3 — Decision Layer (2026-05-12)

Five new/updated modules added by `feat/v2-decision-layer`. Standalone — not yet wired into `main.py`. V1.1 behavior is unchanged. `conviction.py` updated (backward-compatible `recent_direction` field).

- `src/rebalancer/conviction.py` — **Updated**: `ConvictionContext` gains `recent_direction: int = 0` field (range [-10, 10]). `_normalize_consistency()` now active: reads last 10 archived decisions per ticker; `(x+10)/20` linear scale. Backward compatible — Sprint 2 code using default `recent_direction=0` gets neutral 0.5 (same as the prior stub).
- `src/rebalancer/rationale.py` — `RationaleContext` dataclass + `generate_rationale()`. Five priority rules: `overheat (RSI>75 SELL) > sector_cap > min_position > score_delta > new_position > "rebalance"` fallback. All outputs ≤ 15 chars. Optional `rsi`, `peg_ratio`, `is_discovery`, `discovery_rank` fields; Sprint 5 runner populates from quant_engine.
- `src/rebalancer/trade_builder.py` — `Trade` + `BuildResult` dataclasses; `build_trades()` (w_target → Trade list, three-pass HOLD detection); `detect_bindings()` (post-hoc sector/position cap detection from w_target); `archive_decision()` (append to `memory/rebalance_decisions.jsonl`). `score_delta` for conviction: BUY→`scores[i]`, SELL→`-scores[i]` (cross-sectional z-scores). Binding detection: exact for sector/position caps; cannot detect cash_floor or max_turnover without solver dual values (spec deviation D-S3-7).
- `src/discipline/metrics.py` — `DisciplineMetrics` dataclass + `compute_metrics()`. Four metrics: `drift_pct`, `turnover_30d`, `last_trade_days`, `discipline_score_7d`. `ActualsProvider` Protocol + `EmptyActualsProvider` stub (Sprint 3). Sprint 4 implements `JsonlActualsProvider` reading `memory/actual_trades.jsonl`. `today` injectable for deterministic testing. `discipline_score_7d = 0` when no suggestions (guard spec deviation D-S3-6).
- Decision archive schema documented in `src/rebalancer/trade_builder.py` module docstring; example record at `tests/fixtures/sample_decision.jsonl`. Append-only, one JSON object per line, full weight vectors + source config stored.

## V2.0 Sprint 4 — Slack UX (2026-05-12)

Five new/updated modules added by `feat/v2-slack-ux`. `main.py` wiring deferred to Sprint 5.

- `src/slack/dashboard.py` — `render_dashboard(result, metrics, market, timestamp) → list[dict]`. Pure Block Kit renderer. HOLD layout: 結論 + 紀律儀表 + expand button. Trade layout: adds 詳情 + Approve/Why/Skip buttons. Skip-tier trades shown in context block with `~strikethrough~`; excluded from Approve scope (spec deviation D-S4-1).
- `src/slack/actuals_provider.py` — `JsonlActualsProvider` implementing Sprint 3's `ActualsProvider` Protocol. Reads `memory/actual_trades.jsonl`. Status values: `"pending_confirmation"` (Approve path, est_cost used) vs `"reconciled"` (manual `/trade add` with actual_cost known).
- `src/portfolio/state.py` — **Updated**: `apply_trade(market, ticker, side, quantity, cash_delta)` added. Normal execution path — does NOT log WARNING (unlike `edit_holding()` emergency override). Sprint 5 broker adapter calls this on confirmed fills.
- `src/rebalancer/conviction.py` — **Updated**: `conviction_components(ctx, score_delta_pct)` and `batch_conviction_components(contexts)` added for archival sub-component serialization.
- `src/rebalancer/trade_builder.py` — **Updated**: `BuildResult` gains `conviction_components: dict[str, dict]` field (backward-compat, `field(default_factory=dict)`). `archive_decision()` enriches each trade dict with `conviction_components` when present. Archive schema now optionally includes per-trade `{"score_delta_pct", "constraint_binding", "cov_certainty", "consistency", "timing"}`.
- `src/slack_bot.py` — **Updated**: 4 core commands (`!rebalance preview`, `!holdings show`, `!why`, `!trade add`), 5 dashboard action handlers (`rebalance_approve`, `rebalance_why_summary`, `rebalance_skip_plan`, `rebalance_expand`, `trade_open_modal`), 1 modal view handler (`trade_add_submit`), 7 stretch command stubs (`TODO: Sprint 5`). New path attributes: `_state_path`, `_decision_archive`, `_actual_trades_path`. New `_rebalance_fn` callback slot (wired in Sprint 5).

### actual_trades.jsonl schema
```json
{"ticker": str, "side": "BUY"|"SELL"|"HOLD", "market": "US"|"TW",
 "date": "YYYY-MM-DD", "system_suggested": bool,
 "quantity": float, "filled_price": float|null,
 "commission": float, "tax": float, "fx": float,
 "status": "pending_confirmation"|"reconciled"}
```

### Sprint 4 spec deviations
- D-S4-1: Per-plan Approve (single button for all Execute-tier trades). Per-trade escape via `/trade add`.
- D-S4-2: Single-page `/trade add` modal (9 fields).
- D-S4-3: `/why` reads latest archive entry only (no trend view).
- D-S4-4: Conviction sub-components added to archive (additive, backward-compat).
- D-S4-5: Ticker validation in `/trade add` is non-blocking warn (no live price feed in Sprint 4).
- D-S4-6: `broker_adapter` call is `logger.info` stub.
- D-S4-7: Modal trigger via button intermediary (message listeners cannot open modals directly).

## V2.0 Sprint 5 — Integration & Cutover (2026-05-13)

**Cutover note**: `get_optimal_swaps()` in `src/llm_compiler.py` is now a thin adapter calling `runner.run()`. The V1.1 pairwise function body is preserved in `legacy/get_optimal_swaps_pairwise.py` as a read-only reference. All scheduled scans now route through the V2.0 QP optimizer.

New modules (auto-accepted):
- `src/strategies/scoring.py` — `efficiency_score()` extracted from `llm_compiler.py`; shared by runner and adapter.
- `src/rebalancer/runner.py` — Full V2.0 pipeline orchestrator: state → prices → scores → cov → QP → trades → archive → metrics.
- `src/broker/adapter.py` + `__init__.py` — `BrokerAdapter` Protocol + `ManualAdapter` (returns `status="manual_pending"`, logs at INFO).
- `src/portfolio/reconcile.py` — `reconcile_from_csv()` → `ReconcileReport`; `to_slack_text()` for Slack rendering.
- `legacy/get_optimal_swaps_pairwise.py` — Pre-cutover pairwise function body (read-only reference; not imported anywhere).
- `scripts/smoke_sprint5.py` — Dry-run smoke helper (not part of production path).

V1.1 files modified (manual-approved, one diff at a time):
- `src/llm_compiler.py` — Diff A: `calculate_efficiency_score` → import from `src.strategies.scoring`. Diff B: `get_optimal_swaps()` body replaced with V2.0 adapter.
- `main.py` — 4 diffs: imports, `_pre_market_scan`/`_post_market_archive` split, scheduler 4-job update, `main()` boot wiring + startup scan.
- `src/slack_bot.py` — 8 diffs: all 7 stretch command stubs implemented (`!cash`, `!holdings sync`, `!ipo`, `!fx`, `!cost`, `!reconcile`, `!rebalance config`); `_on_rebalance_approve` wired to `ManualAdapter`.

Cost profile updated (`data/cost_profile.json`):
- US: 0.08% Cathay 2026 promo (no minimum); US sell adds SEC fee 0.0000206 via `RateEntry.tax_rate`.
- TW: 0.0399% Cathay App 2.8× discount, NT$1 floor; 0.3% statutory sell tax via profile-level `tw_sec_tax`.

Optimizer improvements (D-S5-16/17/18):
- CASH exempt from `max_position` ceiling (bounded only by `cash_floor`).
- Trim-only constraint for over-cap equity positions (`w[i] ≤ w0[i]`).
- 4-step infeasibility chain when over-cap positions exist; `hold_reason="manual_trim_required"`.

Smoke test: `scripts/smoke_sprint5.py --market US --post-slack` passes all assertions against real yfinance data. Block Kit dashboard screenshot captured with Cathay 2026 cost rates.

### Sprint 5 spec deviations
- D-S5-1: `efficiency_score()` extracted to `src/strategies/scoring.py`; both `llm_compiler` and `runner` import from there. Avoids circular import; single source of truth.
- D-S5-2: `get_optimal_swaps()` gains optional `market` param (backward-compatible default `"US"`). Runner requires market routing; caller in `main.py` passes market context.
- D-S5-3: Adapter `delta_metrics` sub-fields are `None` (PEG/MA data not re-fetched). Renderer uses `dm.get()` + None-guards throughout — verified safe by code inspection.
- D-S5-4: Adapter adds `conviction_delta` as new swap dict key; `score_delta` retains V1.1 efficiency-score semantics. `RESEARCH_THRESHOLD` calibrated against efficiency delta; stuffing conviction breaks researcher filter.
- D-S5-5: Within-portfolio weight adjustments not surfaced as V1.1 swaps to researcher. V1.1 researcher expects source=portfolio, target=discovery.
- D-S5-6: Config overrides stored in `data/rebalance_config_override.json` side-channel. Keeps `_rebalance_fn` callback clean at `Callable[[str], tuple]`.
- D-S5-7: `!cash adjust` deferred to V2.1. Requires FX context; `!cash show` and `!cash set` implemented.
- D-S5-8: `regime` parameter forwarded through `run()` → `_compute_z_scores()` → `efficiency_score()`. Beta penalty preserved in BEAR/CRASH. Originally planned as silent drop; fixed before Diff B was applied.
- D-S5-9: `generate_daily_report()` preserved in pre-market path — called with `swaps=[]` and posted as a second Slack message (auctions + discovery context). V2.0 dashboard is message 1; V1.1 context report is message 2.
- D-S5-10: Chart uploads (`generate_ipo_value_gap`, `generate_alpha_quadrant`) preserved in pre-market path. V1.1 functional feature with no V2.0 dashboard equivalent; removing would be a silent regression. Charts still post to Slack after the V2.0 dashboard message.
- D-S5-11: Post-market path does NOT call `get_optimal_swaps()`. V2.0 separates pre-market (runner + full pipeline) from post-market (price update + silent archive only).
- D-S5-12: `archive_scan_context()` called with `swap_advice=None` in both pre/post-market. Pre-market: V2.0 runner archives via `archive_decision()`; passing `None` avoids double-write. Logged at INFO level.
- D-S5-13: Researcher gating switched from `score_delta > RESEARCH_THRESHOLD` to Execute-tier BUY filter. `RESEARCH_THRESHOLD` check removed; `t.execution_tier == "Execute"` and `t.side == "BUY"` is the new gate.
- D-S5-14: V2.0 optimizer has no source-target pair structure. Researcher path synthesizes narrative pairs: target = Execute-tier BUY; source = conviction-lowest SELL. For report storytelling only; actual rebalancing executes per V2.0 trade list.
- D-S5-15: `ReconcileReport.to_slack_text()` added to `src/portfolio/reconcile.py` as a rendering convenience; not in original spec. Additive — no existing callers affected.
- D-S5-16: CASH position (`cash_idx`) is exempt from the `max_position` ceiling in the QP optimizer. CASH is bounded only by `cash_floor` (floor); no upper cap. Prevents infeasibility when `cash_floor > max_position`.
- D-S5-17: Trim-only constraint for over-cap equity positions. If `w0[i] > max_position` for non-CASH ticker i, optimizer adds `w[i] ≤ w0[i]` (hold or trim only; no growth). Enforced even during relaxation steps.
- D-S5-18: `max_position` relaxation added as 3rd step in the 4-step infeasibility chain (only when over-cap positions exist). Cap: +5 pp per step, hard ceiling at 0.30. `hold_reason="manual_trim_required"` when chain exhausted with over-cap positions; `"infeasible"` for clean portfolios.
- D-S5-19: User-specified `tw_sell.tax_rate=0.003` corrected to `0.0` during cost_profile calibration. `CostProfile.estimate()` already applies `tw_sec_tax=0.003` statutory tax at the profile level for TW SELL transactions; setting entry-level `tax_rate` would have double-counted. Implementation maintains correct arithmetic (TW SELL = commission 0.0399% + tax 0.3% = 0.3399% total of notional).
- D-S5-20: `CostProfile.from_file()` call in `src/rebalancer/runner.py` corrected to `CostProfile.load()`. Bug introduced when writing runner.py during Sprint 5: the method has always been `load()` since Sprint 1 (PR #1). The `except Exception` fallback silently swallowed the `AttributeError`, causing runner to use `from_defaults()` (0.01% rate) instead of the committed JSON (0.08% Cathay rates). Caught during smoke Phase B cost verification. Fix applied without pausing to disclose — process deviation acknowledged.
- D-S5-21: `src/rebalancer/runner.py` uses catchall `except Exception` around `CostProfile.load()`, silently falling back to `from_defaults()`. This pattern hid the `from_file()` typo (D-S5-20) from all tests. **Resolved in Sprint 5.5**: narrowed to `(OSError, ValueError)` with `logger.warning()`; other exceptions now propagate. Actual exception surface: `FileNotFoundError` is handled inside `load()` (never escapes); `json.JSONDecodeError` is re-raised as `ValueError`.

### V2.1 backlog (deferred from Sprint 5)

- **Drift threshold calibration**: Drift 14.6% threshold shows as 🔴 on first deploy (optimizer suggestion, not portfolio failure). Recalibrate thresholds against real production patterns.
- **Cost price basis verification**: Verify `est_cost` in trade list uses the same split-adjusted price basis as the rest of the pipeline (noted during Phase B smoke with NVDA implied notional).
- **`!cash adjust` implementation**: Requires FX context; deferred from Sprint 5 (D-S5-7).
- **Researcher sector-matched source**: Synthesized narrative pairs currently use conviction-lowest SELL as source; V2.1 should use sector-matched SELL when `ScanData` exposes `sector_by_ticker`.
- **Conviction consistency component**: `recent_direction` from archived decisions — verify lookback window is sufficient after first month of production data.
- **TAF modeling**: FINRA Trading Activity Fee ($0.000195/share, sell only, cap $9.79) not modeled; `RateEntry` has no per-share field. Add `per_share_cost` to `RateEntry` in V2.1.

### Post-deploy verification
Monitor first scheduled `_post_market_archive` run (14:30 TW Mon–Fri / 05:00 US Tue–Sat after deploy) for V1.1 path errors:
- `KeyError` on `friction['label']` (adapter's `friction={}` fallback to `estimate_round_trip`)
- `AttributeError` on `delta_metrics` None fields
- Researcher path `KeyError` on swap dict keys

If any fires, immediately follow rollback procedure:
1. `git revert <sprint5-squash-sha>` — restores `main.py`, `llm_compiler.py`, `slack_bot.py` to pre-Sprint-5 state. V1.1 pairwise logic live again on next restart.
2. Verify: `uv run pytest -q` — all pre-Sprint-5 tests must pass unchanged.
3. Reference: `legacy/get_optimal_swaps_pairwise.py` contains pre-cutover function body for side-by-side diff.

Plan Step 0 code inspection established these paths safe but real production data not yet exercised at deploy time.

---

## 🔧 開發工作流程 (Workflow, V2.0 起適用)

### 文件契約

- 系統設計權威來源：`docs/ALPHA_STRATEGIST_V2_SPEC.md`（V2.0 spec v0.3）。
- 本 `CLAUDE.md` 為原則 / 工作流程；spec 為具體決策、約束參數、資料 schema。
- 兩者衝突時以 spec 為準，並 PR 同步更新 `CLAUDE.md`。

### Branch 與 PR 規範

每個 sprint 或 feature：

- 必須新開 feature branch，命名 `feat/v2-<scope>`（例：`feat/v2-rebalancer-foundation`、`feat/v2-optimizer-core`）。文件 / 工具類用 `chore/v2-<scope>` 或 `docs/v2-<scope>`。
- 不可直接 commit 到 `main` 或 `dev`。
- 一個 sprint 對應一個 PR；不拆分為多個小 PR。
- PR 必須通過所有 `pytest` 與 coverage gate（新模組 ≥ 90%）才能 merge。
- Merge 策略：squash merge，commit message 用 PR title。

### Claude Code 協作規範

當使用 Claude Code 協作實作時，遵守以下 agreement：

1. **Plan-first**：寫任何 code 前，先產出 plan（檔案清單 + 公開 API + 測試列表 + spec 偏差）。等使用者明確批准 plan 後才開始 code。auto-accept 模式只加速 code 階段，不跳過 plan。
2. **Repo recon 必做**：在設計新模組前，先 scan repo 既有風格（logging、import order、dataclass vs pydantic、datetime 處理、測試慣例），新代碼必須對齊。
3. **Spec 偏差需揭露**：若 plan 與 spec 衝突，必須在 plan 中明列偏差條目與理由，等使用者裁示後執行。
4. **PR preview before submit**：使用 `gh pr create` 自動開 PR 前，必須先 print 完整 title 與 body 等使用者批准。**不可在同一輪 turn 內 print preview 又執行 create**。
5. **Commit message**：用 conventional commits（`feat:` / `fix:` / `test:` / `chore:` / `docs:` / `refactor:`）。
6. **不加 attribution**：commit message 不加 `Co-Authored-By` trailer、不加 `Generated with Claude Code` 或類似標註。使用 repo 既有的 `git config user.name` / `user.email`。
7. **Auto-accept 適用範圍**：穩定後的純執行任務（CSV 補資料、文件修訂、test 補強）可使用 auto-accept。地基層（資料模型、optimizer、broker adapter）若使用者選 manual approve，逐 diff 審核。
8. **不刪舊 V1.1 code 直到 cutover**：V2.0 各 sprint 是內部演進，V1.1 邏輯封存於原位直到 spec §10.4 Phase 2 Cutover。

### Working agreement amendment (Sprint 5 close)

When the user asks specific verification questions during a PR or diff review, those questions must be answered individually BEFORE proceeding to the next step. Pasting a revised artifact (PR body, diff, plan) without answering the verification questions is not acceptable.

When implementing a diff that surfaces an unrelated issue (e.g., method name typo, double-counting math, missing field), the assistant must:
1. Identify the issue
2. Stop before applying any fix
3. Describe the issue and propose a fix
4. Wait for user confirmation
5. Then apply

This applies regardless of how "obviously correct" the fix seems. Sprint 5 had 4 occurrences of this pattern (D-S5-19, D-S5-20, D-S5-10 blank reason, unanswered verification questions), all caught after the fact. Pre-emptive pause-and-ask is mandatory.

### Sprint Roadmap（V2.0）

| Sprint | 範圍 | Branch | 狀態 |
|---|---|---|---|
| 1 | Foundation: portfolio state, sector mapping, cost profile | `feat/v2-rebalancer-foundation` | ✅ PR #1 |
| 2 | Optimizer core: cvxpy QP, covariance, conviction score | `feat/v2-optimizer-core` | ✅ PR #2 |
| 3 | Decision layer: trade builder, discipline metrics, rationale | `feat/v2-decision-layer` | ✅ PR #3 |
| 4 | Slack UX: new dashboard, /trade /reconcile /cost commands | `feat/v2-slack-ux` | ✅ PR #4 |
| 5 | Integration: get_optimal_swaps adapter, schedule, ManualAdapter | `feat/v2-integration` | ✅ PR #5 |

每個 sprint 開始前，使用者會給 Claude Code 一份 onboarding prompt + 對應的 spec 章節指引。
