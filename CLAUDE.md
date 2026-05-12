# 專案代號：Alpha Strategist (CFO Warden System)

## 🎯 系統定位與核心守則 (System Persona)
- 你是本專案的「首席系統架構師」。你的任務是協助開發一個跨台美股的 Agentic AI 監控系統。
- 專案核心邏輯：成長動能強、修正版林區公式 (Modified PEG)、結構性調整分析、維持低換手率。
- **絕對禁忌 (CRITICAL)**：
  1. 絕對禁止預測市場走向。
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

## V2.0 Sprint 1 — Foundation Data Layer (2026-05-12)

Three new modules were added by `feat/v2-rebalancer-foundation`. They are standalone and not yet wired into `main.py` — V1.1 behavior is unchanged.
- `src/portfolio/state.py` — JSON-backed `PortfolioState` dataclass (cash, holdings, IPO subscriptions, lockup tickers per spec §7.1–7.2). State file lives at `data/portfolio_state.json` (not committed; create with `PortfolioState.create_empty().save(path)`).
- `data/sector_mapping.csv` — L1/L2 sector classification for optimizer `max_sector` constraints (spec §7.3). Missing tickers fall back to `("其他", "unmapped")` at runtime and are logged.
- `src/cost/profile.py` — `CostProfile` dataclass with §6.1 textbook defaults, `/cost set` manual override, and data collection stub for V2.1 statistical learning (spec §6.2, §7.6). Default file `data/cost_profile.json` is committed with IBKR-like US rates and Cathay pre-discount TW rates.