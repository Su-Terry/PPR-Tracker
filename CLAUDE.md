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

### Sprint Roadmap（V2.0）

| Sprint | 範圍 | Branch | 狀態 |
|---|---|---|---|
| 1 | Foundation: portfolio state, sector mapping, cost profile | `feat/v2-rebalancer-foundation` | ✅ PR #1 |
| 2 | Optimizer core: cvxpy QP, covariance, conviction score | `feat/v2-optimizer-core` | ✅ PR #2 |
| 3 | Decision layer: trade builder, discipline metrics, rationale | `feat/v2-decision-layer` | ✅ PR #3 |
| 4 | Slack UX: new dashboard, /trade /reconcile /cost commands | `feat/v2-slack-ux` | Planned |
| 5 | Integration: get_optimal_swaps adapter, schedule, ManualAdapter | `feat/v2-integration` | Planned |

每個 sprint 開始前，使用者會給 Claude Code 一份 onboarding prompt + 對應的 spec 章節指引。
