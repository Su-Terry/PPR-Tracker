# Alpha Strategist V2.0 — Global Rebalancer with Discipline Anchoring

**Status:** Draft v0.3
**Author:** Spec drafted with Claude, owned by @Su-Terry
**Supersedes:** V1.1 local heuristic pairing (`get_optimal_swaps`)

**Changes from v0.2:**
- §1 系統定位重寫：紀律錨點，非省時 agent，非決策輔助
- §4 新增 Conviction Score（每筆 trade 的執行強度）
- §5 trade list 加 `execution_tier` 與雙欄位成本
- §6 成本模型完全改寫：雙軌制 + 學習 profile
- §7.5 新增 State Reconciliation 四層防護
- §7.6 新增 Cost Profile 收集與覆寫
- §8 Slack UX 改為「結論先行 + 紀律儀表」，新增 trade 補登、reconcile、cost 指令
- §8.5 排程重設計：盤前推、盤後 silent log
- §9.3 新增 Broker Adapter 抽象（漸進實作）
- §10 評估改用 Discipline Score 為主指標
- §11 新增「狀態漂移」與「成本失準」兩條核心風險

---

## 0. TL;DR

V1.1 使用 `get_optimal_swaps()` 做 **pairwise heuristic pairing**：產出局部換倉建議。

V2.0 改為 **global optimization with discipline anchoring**：
- 全域目標權重最佳化（QP + L1 turnover penalty），TW / US 各自獨立
- 每筆 trade 帶 **Conviction Score**（0–10），明確分級 Execute / Watch / Skip
- 每日 dashboard 頂部固定有**紀律儀表**（drift / turnover / stillness / discipline）
- 「HOLD」是合法且明確的結論，不硬擠假動作
- State 透過四層防護維持與真實持倉同步
- 成本透過雙欄位（est_cost / actual_cost）漸進學習

**保留**：所有資料層、Slack Bot、IPO 整合、研究員、週報、memory archiving。
**替換**：`get_optimal_swaps()` 一個函式。
**新增**：`src/rebalancer/`、`src/portfolio/state.py`、`src/portfolio/reconcile.py`、`src/broker/`、`data/sector_mapping.csv`、`data/portfolio_state.json`、`data/cost_profile.json`。

V2.0 是 PPR-Tracker 的內部演進，不另開 repo。

---

## 1. 系統定位（核心重寫）

### 1.1 系統角色：紀律錨點

V2.0 的系統角色**不是**：
- ❌ 決策輔助工具（你不需要每天 review 30 分鐘）
- ❌ 省時 agent（你不要它幫你避開市場）
- ❌ 自動下單機器人（V2.0 仍 Human-in-the-Loop）

V2.0 的系統角色**是**：
- ✅ **紀律錨點**：當使用者想動而市場情緒在主導時，系統的存在讓他停下來問「是系統說的還是我想動？」
- ✅ **外部化的判斷力**：提供穩定、可審查、可量化的決策基準（Conviction、Drift、Discipline）
- ✅ **行為對帳工具**：週報量化「執行 vs 建議」的對齊度，揭示自身的失控時刻

### 1.2 預測原則：strong vs weak forecasting

- **禁止 strong forecasting**：個股或市場點估計報酬、擇時宣稱、總經預測
- **允許 weak forecasting**：橫斷面相對評分、條件機率聲明、波動度估計
- Score 與 covariance 都屬於 weak forecasting，是 V2.0 optimizer 的合法輸入

> 此條已偏離 CLAUDE.md 原本「絕對禁止預測市場走向」的措辭。建議同步更新 CLAUDE.md（PR pending）。

### 1.3 其他原則（沿用 V1.1）

- **禁止幻覺**：所有輸入必須有來源；缺資料就退化，不補值
- **Human-in-the-Loop**：optimizer 輸出建議，不執行
- **微服務解耦**：rebalancer 是純函式模組，不直接呼叫 data fetcher 或 slack

---

## 2. Scope & Non-Goals

### In scope (V2.0)
- 全域目標權重最佳化（QP），TW / US 各自獨立
- L1 換手懲罰
- 板塊 / 單檔 / 現金緩衝 / 最大換手率約束
- TW IPO 預扣現金銜接
- Conviction Score 計算（純規則，非 LLM）
- 紀律儀表（drift / turnover / stillness / discipline）
- JSON-based portfolio state
- 自建 sector mapping
- 四層 state reconciliation（auto-record + 補登 modal + 週日 CSV 上傳）
- 雙欄位成本記錄（est_cost / actual_cost）
- Broker Adapter 抽象（MVP 只實作 `ManualAdapter`）
- 排程：盤前推 / 盤後 silent log
- 與 IQC-Stage1 auto-alpha 的整合介面（介面定義 only）

### Out of scope (V2.1+)
- Factor model covariance（V2.0 用 Ledoit-Wolf shrinkage）
- Integer programming（V2.0 允許小數股）
- 跨市場最佳化（嚴格分開）
- 跨幣別 FX 模擬（V2.0 只記錄 FX 事件，不參與 optimizer）
- Tax-aware rebalancing（wash sale, taxlot harvesting）
- 離線歷史回測（樣本不足，等資料累積）
- 自動 broker reconciliation（V2.0 為手動 CSV 上傳）
- Cost profile 自動學習（V2.0 收集資料，V2.1 啟用）
- 自動下單（adapter 已抽象，未來才實作）

---

## 3. Architecture

### V1.1 現況

```
data_fetcher → quant_engine (score) → llm_compiler.get_optimal_swaps (pairwise)
                                    ↓
                              generate_daily_report (Block Kit)
                                    ↓
                              SlackWarden.send_report
```

### V2.0 目標

```
data_fetcher → quant_engine (score) ─┐
discovery.scan_market_for_alpha ─────┤
portfolio.state.load() ──────────────┤
sector_mapping.load() ───────────────┤
cost_profile.load() ─────────────────┤
                                     ▼
                  ┌──────────────────────────────────┐
                  │  rebalancer.run(market="US")     │ ──→ trades_us (+ conviction)
                  │  rebalancer.run(market="TW")     │ ──→ trades_tw (+ conviction)
                  └──────────────────────────────────┘
                                     │
                                     ▼
                  discipline.compute_metrics() ──→ drift, turnover, stillness, discipline_7d
                                     │
                                     ▼
                  llm_compiler.format_rebalance_advice (REFACTORED)
                                     ▼
                          generate_daily_report (UNCHANGED interface)
```

### 3.1 為什麼 TW / US 嚴格分開

- 流動性結構不同（TW 證交稅 0.3%、漲跌幅 10%；US 0 佣金、無漲跌幅）
- Covariance 估計不穩（跨市場受 FX 干擾）
- 時區與營運週期不同（不同 scan 時點）
- 配置邏輯不同（TW 集中、US 分散）
- FX 為人工事件，不參與 optimizer

實作：兩個 `RebalanceConfig` 實例 → 兩次獨立 `solve_target_weights()` → 結果合併為單一 Slack section。

### 3.2 `get_optimal_swaps()` 變 thin adapter

下游有 4 個 consumer：`generate_daily_report`、`archive_scan_context`、`agent_researcher`、`run_weekly_audit`。介面不變 = 零下游改動。

---

## 4. The Optimization Problem & Conviction Score

### 4.1 變數與符號（每個市場各跑一次）

- `N`：該市場 universe 大小（current holdings + discovery + cash + safe haven proxy），通常 30–80
- `w ∈ R^N`：目標權重
- `w0 ∈ R^N`：當前權重
- `s ∈ R^N`：score 向量（z-score 標準化）
- `Σ ∈ R^{N×N}`：return covariance（Ledoit-Wolf shrinkage, 252-day）
- `B ∈ R^{K×N}`：sector indicator matrix

### 4.2 目標函數

$$
\min_w \quad -\lambda_s \cdot s^T w \;+\; \lambda_v \cdot w^T \Sigma w \;+\; \lambda_t \cdot \|w - w_0\|_1
$$

### 4.3 約束條件（TW / US 各自配置）

| 參數 | US 預設 | TW 預設 |
|---|---|---|
| `max_position` | 0.20 | 0.30 |
| `max_sector` | 0.40 | 0.50 |
| `cash_floor` | 0.05 | 0.10 |
| `max_turnover` | 0.40 | 0.30 |
| `lambda_score` | 1.0 | 1.0 |
| `lambda_var` | 0.5 | 0.5 |
| `lambda_turnover` | 2.0 | 3.0 |
| `min_position` | 0.02 | 0.05 |
| `min_trade_amount` | $100 USD | NT$3,000 |

**TW cash floor 動態**：

```
cash_floor_tw_effective = max(
    config.cash_floor,
    pending_ipo_subscription_twd / portfolio_value_twd
)
```

### 4.4 Conviction Score（每筆 trade 0–10）

純規則計算，**不使用 LLM**，可被檢驗。

```python
def compute_conviction(trade: Trade, context: ConvictionContext) -> float:
    # 各成分歸一化到 [0, 1]
    c_score    = normalize_score_delta(context.score_delta)        # 40%
    c_binding  = normalize_constraint_binding(context.bindings)    # 20%
    c_cov      = normalize_cov_certainty(context.cov_certainty)    # 15%
    c_consist  = normalize_consistency(context.recent_decisions)   # 15%
    c_timing   = normalize_timing(context.days_since_last_trade)   # 10%

    raw = 0.40*c_score + 0.20*c_binding + 0.15*c_cov \
        + 0.15*c_consist + 0.10*c_timing

    return round(raw * 10, 1)  # 0.0 – 10.0
```

| 成分 | 意義 | 高分情況 | 低分情況 |
|---|---|---|---|
| score_delta | 換到更高分的程度 | source score 8 → target score 2 | source 5 → target 4.5 |
| constraint binding | 約束觸頂強度 | sector cap 觸頂 95% | 各約束都有空間 |
| cov certainty | 共變異數估計可靠度 | 標的有 252 日資料 | 新上市標的、< 60 日 |
| consistency | 與近期決策一致 | 連續建議同方向 | 上週才反向操作 |
| timing | 距上次同檔交易 | 上次 30+ 天前 | 上次 3 天前（避免來回鞭） |

### 4.5 Execution Tier

| Conviction | Tier | Slack 標示 | 行為提示 |
|---|---|---|---|
| ≥ 6.0 | **Execute** | ✅ | 系統建議執行 |
| 4.0 – 5.9 | **Watch** | ⏸ | 邊緣案例，紀律上不施壓 |
| < 4.0 | **Skip** | ❌ | 訊號太弱，不該動 |

門檻 6.0 為初始值，shadow mode 跑 2 週後依分布調整。

### 4.6 Optimizer 實作

```python
import cvxpy as cp

def solve_target_weights(
    w0, scores, cov, sector_matrix, config
) -> np.ndarray:
    n = len(w0)
    w = cp.Variable(n, nonneg=True)

    objective = cp.Minimize(
        -config.lambda_score * scores @ w
        + config.lambda_var * cp.quad_form(w, cp.psd_wrap(cov))
        + config.lambda_turnover * cp.norm(w - w0, 1)
    )

    constraints = [
        cp.sum(w) == 1,
        w <= config.max_position,
        sector_matrix @ w <= config.max_sector,
        w[config.cash_idx] >= config.effective_cash_floor,
        cp.norm(w - w0, 1) <= config.max_turnover,
    ]

    prob = cp.Problem(objective, constraints)
    prob.solve(solver=cp.CLARABEL)

    if prob.status != cp.OPTIMAL:
        raise OptimizerInfeasibleError(f"Solver status: {prob.status}")

    return w.value
```

**`min_position` 處理**：先解 QP，將 `w < min_position` 設為 0，re-normalize，必要時 warm-start 重解一次。

### 4.7 「HOLD」是合法結論

當以下任一條件滿足，輸出明確 HOLD：
- `‖w* − w0‖₁ < min_total_turnover`（例如 < 2%）
- 所有 trade 的 conviction 都 < 4.0
- 所有 trade 的 notional 都 < `min_trade_amount`

HOLD 不是「沒結論」，是「系統審慎評估後認為今日無需動作」的明確聲明。dashboard 仍會推送，但結論行寫 `🟢 HOLD all`，詳情區塊縮短。

---

## 5. Trade List 與雙欄位成本

```python
@dataclass
class Trade:
    market: Literal["US", "TW"]
    ticker: str
    side: Literal["BUY", "SELL"]
    delta_weight: float
    target_weight: float
    quantity: float            # 股數
    est_price: float           # 系統估算用價格
    notional: float            # USD or TWD

    # 雙欄位成本
    est_cost: float                      # 來自 cost_profile + §6 規則
    est_cost_breakdown: dict             # {commission, tax, fx_spread}
    actual_cost: float | None = None     # 使用者回報實際成本
    actual_cost_breakdown: dict | None = None

    # 決策資訊
    conviction: float                    # 0–10
    execution_tier: Literal["Execute", "Watch", "Skip"]
    rationale: str                       # ≤15 字
    bindings: list[str]                  # 觸發的 constraints
```

`rationale` 由純規則組合（非 LLM）：

- `"PEG 0.8, Discovery #1"`
- `"RSI 82 過熱"`
- `"sector cap 觸發"`
- `"換倉至更高 score (+2.1)"`

---

## 6. 成本模型（雙軌制）

### 6.1 教科書 prior（V2.0 啟用）

| 市場 | 單邊成本（prior） |
|---|---|
| US 買 / 賣 | `max($0.01, notional * 0.0001)` — IBKR 等級 |
| TW 買 | `notional * 0.001425` — 原價未折 |
| TW 賣 | `notional * 0.001425 + notional * 0.003` |

### 6.2 個人 cost profile（V2.0 收集，V2.1 啟用學習）

```json
// data/cost_profile.json
{
  "version": 1,
  "last_updated": "2026-05-12T08:30:00+08:00",
  "us_buy":   {"rate": 0.0024, "min": 35.0, "source": "manual_override"},
  "us_sell":  {"rate": 0.0024, "min": 35.0, "source": "manual_override"},
  "tw_buy":   {"rate": 0.00057, "min": 0,  "source": "learned", "n_samples": 47},
  "tw_sell":  {"rate": 0.00057, "min": 0,  "source": "learned", "n_samples": 39},
  "tw_sec_tax": 0.003,
  "fx_twd_usd_spread_bps": 45
}
```

- `source: "default"` — 使用 §6.1 教科書值
- `source: "manual_override"` — 使用者 `/cost set` 覆寫
- `source: "learned"` — V2.1 從 `actual_cost` 統計（V2.0 階段不啟用）

**TW 證交稅 `tw_sec_tax = 0.003` 寫死**，是法定值不走學習。

### 6.3 Cost 雙欄位邏輯

```python
# Step 1: Approve trade 時，用 est_cost 預先更新 state
state.cash -= qty * price + est_cost  
trade.actual_cost = None  # outstanding

# Step 2: 使用者補登實際成本
delta = actual_cost - est_cost
state.cash -= delta
trade.actual_cost = actual_cost
trade.actual_cost_breakdown = {...}

# Step 3 (V2.1): 累積 30+ 筆後更新 cost_profile.learned
```

### 6.4 FX 為手動事件

V2.0 **不模擬** FX。TW cash 與 US cash 各自獨立，optimizer 不會跨幣別建議。

當使用者實際做 FX 換匯，手動記錄：

```
/fx record twd_to_usd 50000 1580
↳ TW cash: -50,000  |  US cash: +1,580
↳ Implied rate: 31.65  |  Spread vs market mid: 0.42%
```

FX 事件寫入 `memory/fx_events.jsonl`，週報統計實際 spread，未來用於更新 `fx_twd_usd_spread_bps`。

### 6.5 成本 outstanding 追蹤

- Trade 預設 `actual_cost = None`，狀態 `outstanding`
- 48 小時後仍 outstanding → 週報「成本回報待補」清單
- 連續 5 筆同類交易都 outstanding → Slack reminder「最近成本未補登，估計值可能失準」

---

## 7. 資料模型增量

### 7.1 `data/portfolio_state.json`

```json
{
  "version": 1,
  "last_updated": "2026-05-12T14:32:00+08:00",
  "us": {
    "cash_usd": 12500.00,
    "holdings": {"NVDA": 50, "AAPL": 30, "GOOGL": 15}
  },
  "tw": {
    "cash_twd": 380000,
    "holdings": {"2330.TW": 1000, "2454.TW": 200},
    "pending_ipo_subscription_twd": 35000,
    "pending_ipo_details": [
      {
        "ticker": "6488.TW",
        "amount_twd": 35000,
        "subscribed_date": "2026-05-10",
        "release_date": "2026-05-22",
        "kind": "subscription"
      }
    ],
    "ipo_lockup_holdings": []
  }
}
```

### 7.2 `src/portfolio/state.py`

```python
class PortfolioState:
    @classmethod
    def load(cls, path: Path) -> "PortfolioState": ...
    def save(self, path: Path) -> None:
        """Atomic write via os.replace."""
    def market_snapshot(self, market): ...
    def update_cash(self, market, amount, reason=""): ...
    def add_ipo_subscription(self, ticker, amount, release_date): ...
    def release_ipo(self, ticker): ...
    def sync_holdings_from_csv(self, csv_path): ...
    def edit_holding(self, ticker, qty, reason: str): 
        """Emergency override. Log + warn in weekly report."""
```

Atomic write：

```python
def save(self, path):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))
    os.replace(tmp, path)
```

### 7.3 `data/sector_mapping.csv`

L1（用於 `max_sector` 約束）：

1. 半導體
2. 軟體_雲端
3. 消費電子
4. 傳統製造
5. 金融
6. 生醫
7. 能源_原物料
8. 消費
9. AI_基礎設施
10. 其他

L2 自由分類，留作報表用。缺失 ticker fallback `("其他", "unmapped")` 並 log。

### 7.4 `RebalanceConfig`

```python
@dataclass(frozen=True)
class RebalanceConfig:
    market: Literal["US", "TW"]
    lambda_score: float
    lambda_var: float
    lambda_turnover: float
    max_position: float
    max_sector: float
    cash_floor: float
    max_turnover: float
    min_position: float
    min_trade_amount: float

    @classmethod
    def us_default(cls) -> "RebalanceConfig": ...
    @classmethod
    def tw_default(cls) -> "RebalanceConfig": ...
```

### 7.5 State Reconciliation（四層防護）

**第一層：Auto-record on Approve（MVP）**

按 `[ Approve ]` 後，立刻彈出 modal：

```
✅ Approve this rebalance plan?

Trades to record (using est_cost):
  SELL  AAPL  30 shares  @ $185  est_cost $7
  BUY   NVDA  20 shares  @ $920  est_cost $18

[ Confirm — auto-record ]  [ I'll update manually later ]
```

`Confirm` → JSON 預先更新（用 est_cost），trade 標記 `actual_cost=None`。

`Manual later` → JSON 不變，trade 標記 `pending execution`，當天結束推 reminder。

**第二層：補登 Modal（MVP）**

`/trade add` 開啟：

```
Add Trade
─────────────
Market:   ○ US  ● TW
Ticker:   [2330.TW]
Side:     ● BUY  ○ SELL
Quantity: [1000]
Filled price: [880.00]

— Costs (optional) —
手續費:   [377]
證交稅:   [0]
FX:       [—]

[ ] This was system-suggested
─────────────
[ Save ]
```

留空 = 用 est_cost，標記 `cost_estimated=True`。

**第三層：自動 Broker Reconciliation（V2.1 範圍，V2.0 不實作）**

預留介面，富邦 API 接通後啟用。

**第四層：週日 CSV 上傳（MVP）**

```
🗓 Weekly Reconciliation

Last full reconcile: 7 days ago

[ Upload US CSV (國泰) ]  [ Upload TW CSV (國泰) ]
[ Skip — trust state ]
```

CSV 解析後比對 JSON state：

| 差異類型 | 處理 |
|---|---|
| 持股數量不一致 | Slack 列差異，使用者確認 / 修正 |
| 現金差異 vs 預期（trade 有 actual_cost）| 任何差異 > $1 都推 |
| 現金差異 vs 預期（trade 無 actual_cost）| 容忍 ±30% 估計成本，或 < $5 |
| 未知 ticker（可能配股 / IPO 撥券）| Slack 推「請確認新部位來源」 |
| 缺失 ticker（賣光未回報） | Slack 推「偵測到部位歸零」 |

### 7.6 Cost Profile 收集與覆寫

```python
# src/cost/profile.py
class CostProfile:
    @classmethod
    def load(cls, path: Path) -> "CostProfile": ...
    def estimate(self, market, side, notional) -> float:
        """Apply rate + min, fallback to default §6.1."""
    def manual_set(self, key, rate, min_val, source="manual_override"): ...
    def record_actual(self, trade: Trade): 
        """V2.0: 只存資料；V2.1: 觸發學習。"""
```

`/cost set us_buy rate 0.0024 min 35` 立刻覆寫。

---

## 8. Slack UX

### 8.1 Dashboard 結構：結論先行 + 紀律儀表

```
📊 Alpha Strategist Daily | 2026-05-12 08:30

━━ 今日結論 ━━
🟡 建議調整 1 筆  (conviction 7.2 — ✅ Execute)
   其餘部位 HOLD

━━ 紀律儀表 ━━
Drift:           4.2% 🟢
30-day turnover: 12%  🟢 (target ≤ 20%)
Last trade:      9 days ago
Discipline (7d): 78/100

━━ 詳情 ━━
🇺🇸 *US*  | Turnover: 5.4% | Est Cost: $25
  SELL AAPL → BUY NVDA  conviction 7.2/10  ✅
  · AAPL: RSI 82 過熱, score -1.3
  · NVDA: Discovery #1, PEG 0.8

🇹🇼 *TW*
  🟢 HOLD all (drift within tolerance)

[ Approve ]   [ Why? ]   [ Skip ]

━━ 折疊：score table / IPO 行事曆 ━━
```

HOLD 日的精簡版：

```
📊 Alpha Strategist Daily | 2026-05-12 08:30

━━ 今日結論 ━━
🟢 HOLD all  (no trades suggested)

━━ 紀律儀表 ━━
Drift:           2.1% 🟢
30-day turnover: 8%   🟢
Last trade:      14 days ago
Discipline (7d): 92/100

━━ 詳情 ━━
🇺🇸 *US*  · Top holdings within target weights
🇹🇼 *TW*  · 半導體 sector 47% (cap 50%, 安全)

[ Show full report ]
```

### 8.2 紀律儀表詳細定義

```python
@dataclass
class DisciplineMetrics:
    drift_pct: float           # ‖w_current − w_target_last‖₁ / 2
    turnover_30d: float        # 過去 30 日累計 turnover
    last_trade_days: int       # 距上次任何 trade 的天數
    discipline_score_7d: int   # 0–100，最近 7 日對齊率
```

**Discipline Score 計算**：

```python
def discipline_score_7d(history) -> int:
    suggestions = history.suggested_actions(days=7)  # 含 HOLD
    actuals = history.actual_actions(days=7)
    aligned = count_aligned(suggestions, actuals)
    return round(aligned / len(suggestions) * 100)
```

**Override 對帳**（週報用，非每日）：

```
本週你 override 系統建議 2 次：
  - 11/10 BUY NVDA (系統建議 HOLD)
    ↳ 5 天後 NVDA +1.2%, SPY +0.8% (略勝)
    ↳ 當時 RSI 78 + 5 日漲幅 12% — 符合 FOMO 訊號特徵
  - 11/12 SELL AAPL (系統建議 BUY)
    ↳ 5 天後 AAPL -2.1% (避開損失)
```

### 8.3 Slack 指令完整清單

**Rebalancer：**
- `/rebalance preview [us|tw|all]`
- `/rebalance config show [us|tw]`
- `/rebalance config set us lambda_turnover 3.0` — in-memory
- `/why <TICKER>` — 完整 score 分解 + binding constraints + conviction 細項

**Portfolio state：**
- `/cash show`
- `/cash set us 12500`
- `/cash adjust us -13 "tax"` — 微調並註記
- `/holdings show [us|tw]`
- `/holdings edit NVDA 80 "stock split"` — 緊急覆寫，會 log + 週報列出
- `/holdings sync` — 從 CSV 重匯入

**Trade 回報：**
- `/trade add` — 開 modal
- `/trade list [today|week]`
- `/trade undo <id>`
- `/trade complete <id>` — 補登 actual_cost

**Reconciliation：**
- `/reconcile` — 手動觸發
- `/reconcile upload us` — 引導上傳 CSV
- `/reconcile status` — 上次差異報告

**IPO：**
- `/ipo apply 6488.TW 35000 2026-05-22`
- `/ipo release 6488.TW`
- `/ipo list`

**FX：**
- `/fx record twd_to_usd 50000 1580`
- `/fx history`

**Cost profile：**
- `/cost show`
- `/cost set us_buy rate 0.0024 min 35`
- `/cost reset us_buy` — 退回 default

### 8.4 排程設計

| 排程 | Taipei | 推 Slack | 用途 |
|---|---|---|---|
| **TW 盤前** | 08:30 (Mon–Fri) | ✅ | 今日 TW 建議 |
| **US 盤前** | 21:00 (Sun–Thu) | ✅ | 今夜 US 建議 |
| **TW 盤後** | 14:30 (Mon–Fri) | ❌ silent log | 寫入 memory，回測用 |
| **US 盤後** | 05:00 (Tue–Sat) | ❌ silent log | 同上 |
| **盤中監控** | 每 15 分鐘 | ⚠️ 僅警報 | 閃崩警報，> 5% 跌幅 |
| **週日 review** | 09:00 | ✅ 週報 | Discipline 對帳 + reconcile 提醒 |

**設計原則**：盤前能執行才推；盤後資料保留供隔日盤前用，但不打擾。

### 8.5 `[ Approve ]` 行為

1. 彈出 confirmation modal（§7.5 第一層）
2. 確認後 → 寫入 `memory/rebalance_decisions.jsonl`
3. JSON state 預先更新（auto-record）
4. 觸發 agent_researcher 對 conviction ≥ 7 的 trade 做深度研究（thread）
5. 48 小時後若 actual_cost 仍 None，加入 outstanding list

**仍然不下單**。下單動作由使用者於券商 App 手動執行。

---

## 9. Auto-Alpha & Broker Adapter

### 9.1 Alpha Protocol（與 IQC-Stage1 介面對齊）

```python
class Alpha(Protocol):
    name: str
    universe: Literal["US", "TW", "ALL"]

    def compute(self, date: pd.Timestamp, prices: pd.DataFrame) -> pd.Series:
        """Returns: pd.Series(Index=ticker, Value=alpha score)"""
```

### 9.2 Combiner

```python
final_score = w_classic * classic_score + w_auto * auto_alpha_score
```

V2.0 預設 `w_auto = 0.0`（shadow），V2.1 才開放。

### 9.3 Broker Adapter（為未來自動下單鋪路）

```python
class BrokerAdapter(Protocol):
    def fetch_holdings(self, market) -> dict[str, float]: ...
    def fetch_cash(self, market) -> float: ...
    def place_order(self, trade: Trade) -> OrderResult: ...

class ManualAdapter(BrokerAdapter):
    """V2.0 預設。fetch 從 CSV，place_order 為 no-op。"""

class FubonAdapter(BrokerAdapter):
    """V2.1+ 實作。"""
```

`[ Approve ]` 永遠呼叫 `adapter.place_order()`。V2.0 階段 `ManualAdapter.place_order` 只回傳 `OrderResult(status="manual_pending")`，未來換 adapter 不用改 caller。

---

## 10. 評估與 Rollout

### 10.1 主要指標：Discipline Score，不是賺多少

V2.0 上線時市場為 AI 單邊狂熱，**任何「賺多少」評估都不可信**。改用：

- **Discipline Score 趨勢**：是否上升 / 穩定 / 下降
- **Override 後果統計**：你 override 的決定平均 5 日後表現
- **Drift 控制度**：是否長期維持 < 5%
- **Conviction 分布**：是否合理（不過度集中在 6.0–7.0 之間）

賺多少留給 V2.1 + 累積 6 個月以上資料後再評估。

### 10.2 Rollout

**Phase 0 — Shadow mode（6 週）**
- V2.0 與 V1.1 並行
- Slack 只發 V1.1 建議
- V2.0 結果寫入 `memory/v2_shadow.jsonl`
- 週日對比週報：trade list 差異、turnover 差異、HHI 差異（不評估收益）

**Phase 1 — Soft launch（2 週）**
- Slack 同時顯示兩者
- 觀察 conviction 分布、HOLD 比例、紀律儀表表現
- 出現 infeasibility / 反直覺 → 加 unit test

**Phase 2 — Cutover**
- `get_optimal_swaps()` 內部改用 V2.0
- V1.1 邏輯封存於 `legacy/`，不刪除
- 開放 V2.1 範圍（auto-alpha、cost learning、broker API）

### 10.3 監控指標（live）

| 指標 | 期望值 | 警報閾值 |
|---|---|---|
| Discipline Score 7d | > 70 | < 50 連續 2 週 |
| Weekly turnover | < 10% | > 20% 連續 2 週 |
| Optimizer infeasibility rate | < 5% | > 10% |
| Cash floor 觸發頻率 | 偶發 | 連續觸發 IPO 預扣以外的情況 |
| Sector cap binding 頻率 | < 50% | 連續 binding 表示 universe 太集中 |
| **State drift rate** | **< 2 筆/週** | **> 5 筆/週** |
| **Outstanding actual_cost** | **< 5 筆** | **> 10 筆** |
| **`/holdings edit` 使用頻率** | **< 1 次/月** | **> 3 次/月（前三層設計有問題）** |

---

## 11. 風險與已知 caveats

| 風險 | 嚴重度 | 緩解 |
|---|---|---|
| **State drift 是隱性風險之首** — 失準後所有 metrics 失效 | 🔴 高 | 四層防護（§7.5）；`/holdings edit` 用量監控 |
| **成本估計失準** → optimizer 決策偏差 + cash 累積誤差 | 🔴 高 | 雙欄位、48h outstanding 追蹤、`/cost set` 隨時覆寫 |
| 當前 AI 單邊市為樣本偏差，risk-off 切換是真壓力測試 | 🟡 中 | §10.5 監控；regime 切換時手動調 `lambda_score` / `cash_floor` |
| Covariance 在 small portfolio 不穩 | 🟡 中 | Ledoit-Wolf + 60 日 lookback；不夠資料退化對角矩陣 |
| Optimizer infeasible | 🟡 中 | 自動放寬：`max_turnover → max_sector → cash_floor`，每次 5pp，最多 3 次 |
| TW IPO 撥券立刻被賣 | 🟢 低 | `ipo_lockup_holdings` 不進 universe |
| 系統變成「自我合理化工具」（使用者常 override） | 🟡 中 | 週報直白指出 override 模式 + FOMO 訊號標記 |
| Sector mapping 漏更新 | 🟢 低 | Fallback `("其他", "unmapped")` + 週報列 unmapped |
| JSON state 損壞 | 🟢 低 | Atomic write + version + schema 驗證 |

---

## 12. 實作工作分解

| Task | 預估時間 | 依賴 |
|---|---|---|
| `src/portfolio/state.py` + JSON schema | 1 day | - |
| `src/portfolio/sectors.py` + `sector_mapping.csv` 初版 | 0.75 day | - |
| `src/cost/profile.py` + `cost_profile.json` | 0.5 day | - |
| `src/rebalancer/config.py` + 兩個 default | 0.25 day | - |
| `src/rebalancer/cov_estimator.py` (Ledoit-Wolf) | 0.5 day | - |
| `src/rebalancer/optimizer.py` (cvxpy QP) | 1.5 day | cov_estimator, config |
| `src/rebalancer/conviction.py` (5 成分計算) | 0.75 day | optimizer |
| `src/rebalancer/trade_builder.py` (含 rationale) | 0.75 day | optimizer, cost |
| `src/rebalancer/runner.py` (TW/US 分別執行) | 0.5 day | trade_builder |
| `src/discipline/metrics.py` (drift/turnover/stillness/discipline) | 1 day | state, runner |
| `src/portfolio/reconcile.py` (CSV 解析 + 比對) | 1 day | state |
| `src/broker/adapter.py` + `ManualAdapter` | 0.5 day | state |
| `get_optimal_swaps()` adapter | 0.5 day | runner |
| Slack `/rebalance *` 指令 | 0.75 day | runner |
| Slack `/cash *` / `/holdings *` 指令 | 1 day | state |
| Slack `/trade *` 補登 modal | 1 day | state, cost |
| Slack `/reconcile *` 指令 + CSV 上傳 | 1 day | reconcile |
| Slack `/ipo *` / `/fx *` / `/cost *` 指令 | 0.75 day | state, cost |
| Slack Block Kit 新 dashboard (結論 + 儀表 + 詳情) | 1 day | discipline, runner |
| 排程改寫 (08:30/21:00/silent 14:30/05:00) | 0.5 day | - |
| Shadow mode logging + 週報對比 | 0.75 day | runner |
| Override 對帳 + FOMO 訊號偵測 | 0.75 day | discipline |
| 單元測試 (約束、邊界、reconcile、cost) | 2 day | 全部 |
| 文件 + CLAUDE.md 更新 | 0.5 day | - |
| **總計** | **約 18 工作天** | |

---

## 13. 確認清單

- [x] 1b — 自建 sector mapping
- [x] 2 — TW / US 嚴格分開
- [x] 3 — JSON state 管理持倉與現金
- [x] 4 — IQC-Stage1 alpha 介面 `pd.Series` 對齊
- [x] 5 — 6 週 shadow mode，捨棄離線回測
- [x] 6 — Constraint 參數 TW / US 脫鉤
- [x] 7 — Rationale 內嵌於 trade
- [x] 8 — vectorbt (V2.1)
- [x] 9 — 系統定位為紀律錨點（非省時 agent）
- [x] 10 — Conviction Score (≥6 = Execute) + Execution Tier
- [x] 11 — 排程 08:30/21:00 推、14:30/05:00 silent
- [x] 12 — 「明確 HOLD 結論」是合法且必要的輸出
- [x] 13 — FOMO 偵測為事後 review（週報）
- [x] 14 — Broker Adapter 抽象，V2.0 ManualAdapter，V2.1 富邦
- [x] 15 — MVP reconciliation：第 1+2+4 層
- [x] 16 — `/holdings edit` 逃生口，週報列出
- [x] 17 — 雙欄位成本 (est_cost / actual_cost)
- [x] 18 — `cost_profile` manual override 開放
- [x] 19 — TW 證交稅 0.3% 寫死
- [x] 20 — actual_cost 48 小時 outstanding 追蹤
- [ ] **預測原則重寫** — 是否同步更新 `CLAUDE.md`？等待授權

---

## Appendix A — 為什麼這樣切

### 為什麼系統定位是「紀律錨點」

使用者每天會看盤，但每天從零思考容易被市場情緒帶偏。系統的價值在於提供**穩定、可審查、可量化的決策基準**，當使用者想偏離時，系統的存在會讓他停下來自問「是系統說的還是我想動？」

### 為什麼 Conviction Score 是純規則

LLM 生成的 conviction 不可審查、難測試、會 drift。純規則計算可以：
- Unit test 每個成分
- 使用者 `/why` 看到完整分解
- 跨版本可比較（同樣輸入產出同樣分數）

### 為什麼 HOLD 是平等結論

「沒事找事做」是個人投資者最大的成本來源。系統明確輸出 HOLD，等同於**主動阻止過度交易**。這比任何個別 trade 建議更重要。

### 為什麼 State Reconciliation 四層

State 失準是系統最大隱性風險。任何一層都不夠：
- 第 1 層（auto-record）解決 80% 但不處理 override
- 第 2 層（補登 modal）處理 override 但人會忘
- 第 3 層（broker API）解決根本但 V2.0 還沒接
- 第 4 層（週日 CSV）冗餘檢查兜底

### 為什麼雙欄位成本

成本是 portfolio cash 的主要消耗，估計失準會累積成大誤差。但要求每次精確回報摩擦力太大，所以：
- est_cost 讓系統可以即時運作
- actual_cost 讓系統長期校準
- 漸進收斂到使用者真實成本結構

### 為什麼 cvxpy + CLARABEL

開源、純 Python、QP/MIQP 通吃、cvxpy 介面接近數學表達式。CLARABEL 是 2024 後新興 solver，比 OSQP 快、比 ECOS 穩。