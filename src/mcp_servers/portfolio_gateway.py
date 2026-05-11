"""
Alpha Strategist — Portfolio Gateway MCP Server

Tools:
  sync_portfolio    : 自動探索 data/ 目錄，合併國泰雙格式 CSV，回傳持倉摘要。
  fetch_market_data : 透過 yfinance 取得 PE、EPS成長率、Capex/Rev、現價。

雙格式支援：
  複委託庫存*     → 美股/外股（有代號欄）
  證券未實現彙總* → 台股（無代號欄，以名稱暫代，.TW 自動補加不適用）

CRITICAL: 純 Data Gateway，禁止任何下單或市場預測邏輯。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

TW_TICKER_MAP_PATH = Path("data/tw_ticker_map.json")

import pandas as pd
import yfinance as yf
from fastmcp import FastMCP

mcp = FastMCP("Alpha_Strategist")
logger = logging.getLogger(__name__)

DATA_DIR = Path("data")

# ── 欄位別名對照表 ─────────────────────────────────────────────────────────────
# 格式：標準欄位 → 候選欄位名稱列表（越前越優先）

_FOREIGN_ALIASES: dict[str, list[str]] = {
    "Ticker":     ["代號", "股票代號", "商品代號", "證券代號", "Ticker"],
    "Shares":     ["目前庫存", "持倉股數", "庫存股數", "股數", "Shares"],
    "Cost_Basis": ["均價", "單位成本", "成本", "成交均價", "成交價", "Cost_Basis"],
}

_TW_ALIASES: dict[str, list[str]] = {
    # TW 格式無代號欄，以股票名稱暫代；yfinance 查詢將 gracefully fail
    "Ticker":     ["股票名稱", "證券名稱", "名稱", "Ticker"],
    "Shares":     ["股數", "庫存股數", "持倉股數", "目前庫存", "Shares"],
    "Cost_Basis": ["成交均價", "均價", "單位成本", "成本", "付出成本", "Cost_Basis"],
}

_TW_VALID_CURRENCIES = {"台幣", "美元", "港幣"}


# ── 內部工具函式 ───────────────────────────────────────────────────────────────

def _find_latest(prefix: str, data_dir: Path = DATA_DIR) -> Optional[Path]:
    """
    在 data_dir 中找出檔名以 prefix 開頭且最新修改的 CSV。

    Args:
        prefix:   檔名前綴，例如 '複委託庫存' 或 '證券未實現彙總'。
        data_dir: 搜尋目錄，預設為專案 data/。

    Returns:
        最新 Path；找不到則回傳 None。
    """
    candidates = sorted(
        data_dir.glob(f"{prefix}*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _resolve_col(df: pd.DataFrame, aliases: list[str]) -> Optional[str]:
    """從 DataFrame 中找出第一個符合候選列表的欄位名稱。"""
    for alias in aliases:
        if alias in df.columns:
            return alias
    return None


def _clean_numeric(series: pd.Series) -> pd.Series:
    """移除千分位逗號、百分號、引號，轉為 float；無法解析者設為 NaN。"""
    return (
        series.astype(str)
        .str.strip()
        .str.strip('"')
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False)
        .pipe(pd.to_numeric, errors="coerce")
    )


def _normalize_ticker(raw: str) -> str:
    """4-6 位純數字代號補加 '.TW'（台股格式）。"""
    t = raw.strip().upper()
    return f"{t}.TW" if t.isdigit() and 4 <= len(t) <= 6 else t


def _parse_csv(
    path: Path,
    aliases: dict[str, list[str]],
    source_label: str,
    currency_filter: Optional[set[str]] = None,
    currency_col: Optional[str] = None,
    apply_tw_suffix: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """
    通用 CSV 解析器：彈性欄位匹配 → 標準化 → 過濾彙總列。

    Args:
        path:            CSV 路徑。
        aliases:         欄位別名對照表。
        source_label:    來源標籤（用於 'source' 欄與錯誤訊息）。
        currency_filter: 若指定，只保留 currency_col 值在此集合內的列。
        currency_col:    貨幣欄位名稱（搭配 currency_filter 使用）。
        apply_tw_suffix: 若 True，對純數字代號補加 '.TW'。

    Returns:
        (DataFrame with [Ticker, Shares, Cost_Basis, source], warnings list)
    """
    warnings: list[str] = []

    try:
        df_raw = pd.read_csv(path, encoding="utf-8-sig", thousands=",")
    except Exception as exc:
        return pd.DataFrame(), [f"讀取 {path.name} 失敗：{exc}"]

    if df_raw.empty:
        return pd.DataFrame(), [f"{path.name} 為空檔案"]

    # 欄位匹配
    col_map: dict[str, str] = {}
    missing: list[str] = []
    for std, candidates in aliases.items():
        actual = _resolve_col(df_raw, candidates)
        if actual:
            col_map[actual] = std
        else:
            missing.append(std)

    if missing:
        warnings.append(
            f"{path.name} 缺少欄位 {missing}（CSV 欄位：{list(df_raw.columns)}）"
        )
        return pd.DataFrame(), warnings

    df = df_raw[list(col_map.keys())].rename(columns=col_map).copy()

    # 貨幣過濾（移除彙總列）
    if currency_filter and currency_col:
        actual_cur_col = _resolve_col(df_raw, [currency_col])
        if actual_cur_col:
            mask = df_raw[actual_cur_col].isin(currency_filter)
            df   = df[mask.values].copy()
        else:
            warnings.append(f"{path.name} 找不到幣別欄 '{currency_col}'，跳過彙總列過濾")

    # 清理 Ticker
    df = df[df["Ticker"].notna()].copy()
    df["Ticker"] = df["Ticker"].astype(str).str.strip()
    df = df[df["Ticker"].str.len() > 0]

    if apply_tw_suffix:
        df["Ticker"] = df["Ticker"].apply(_normalize_ticker)

    # 清理數字欄
    for col in ("Shares", "Cost_Basis"):
        df[col] = _clean_numeric(df[col])

    df = df.dropna(subset=["Shares", "Cost_Basis"])
    df = df[df["Shares"] > 0].copy()

    if df.empty:
        warnings.append(f"{path.name} 過濾後無有效持倉")
        return pd.DataFrame(), warnings

    df["source"] = source_label
    return df[["Ticker", "Shares", "Cost_Basis", "source"]].reset_index(drop=True), warnings


def _load_tw_ticker_map(map_path: Path = TW_TICKER_MAP_PATH) -> dict[str, str]:
    """
    讀取 tw_ticker_map.json，回傳「股票名稱 → yfinance 代號」對照表。
    忽略空值與以 '_' 開頭的 key（註解用途）。
    """
    if not map_path.exists():
        return {}
    try:
        raw = json.loads(map_path.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_") and v}
    except Exception as exc:
        logger.warning("tw_ticker_map.json 讀取失敗：%s", exc)
        return {}


def load_portfolio(data_dir: Path = DATA_DIR) -> dict:
    """
    自動探索 data_dir，解析最新的複委託 + 台股 CSV，回傳合併結果。

    供 `sync_portfolio` MCP tool 與 `daily_portfolio_scan` 共用。

    Returns:
        {
          "df":       pd.DataFrame  合併持倉（欄位：Ticker, Shares, Cost_Basis, source）
          "us_count": int
          "tw_count": int
          "files":    {"foreign": str|None, "tw": str|None}
          "warnings": list[str]
        }
    """
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    files: dict[str, Optional[str]] = {"foreign": None, "tw": None}

    # ── 複委託庫存（美股）────────────────────────────────────────────────────
    foreign_path = _find_latest("複委託庫存", data_dir)
    if foreign_path:
        files["foreign"] = foreign_path.name
        df_f, w = _parse_csv(
            path=foreign_path,
            aliases=_FOREIGN_ALIASES,
            source_label="foreign",
            apply_tw_suffix=False,   # 複委託代號為美股，不補 .TW
        )
        warnings.extend(w)
        if not df_f.empty:
            frames.append(df_f)
    else:
        warnings.append(f"找不到 複委託庫存*.csv（搜尋：{data_dir}）")

    # ── 證券未實現彙總（台股）───────────────────────────────────────────────
    tw_path = _find_latest("證券未實現彙總", data_dir)
    if tw_path:
        files["tw"] = tw_path.name
        df_t, w = _parse_csv(
            path=tw_path,
            aliases=_TW_ALIASES,
            source_label="tw_securities",
            currency_filter=_TW_VALID_CURRENCIES,
            currency_col="幣別",
            apply_tw_suffix=True,   # 純數字名稱補 .TW（名稱欄無法保證，但嘗試）
        )
        warnings.extend(w)
        if not df_t.empty:
            frames.append(df_t)
    else:
        warnings.append(f"找不到 證券未實現彙總*.csv（搜尋：{data_dir}）")

    if not frames:
        return {"df": pd.DataFrame(), "us_count": 0, "tw_count": 0,
                "files": files, "warnings": warnings}

    merged = pd.concat(frames, ignore_index=True)

    # 套用 TW 名稱 → 代號對照表（僅對 tw_securities 列）
    tw_map = _load_tw_ticker_map(data_dir / "tw_ticker_map.json")
    if tw_map:
        mask = merged["source"] == "tw_securities"
        merged.loc[mask, "Ticker"] = merged.loc[mask, "Ticker"].map(
            lambda name: tw_map.get(name, name)
        )
        mapped   = mask.sum()
        resolved = int(merged.loc[mask, "Ticker"].isin(tw_map.values()).sum())
        if resolved < mapped:
            unmapped = merged.loc[mask & ~merged["Ticker"].isin(tw_map.values()), "Ticker"].tolist()
            warnings.append(
                f"tw_ticker_map.json 缺少對應：{unmapped}。"
                " 請補充 data/tw_ticker_map.json 以啟用這些標的的 yfinance 查詢。"
            )

    us_count = int((merged["source"] == "foreign").sum())
    tw_count = int((merged["source"] == "tw_securities").sum())

    return {
        "df":       merged,
        "us_count": us_count,
        "tw_count": tw_count,
        "files":    files,
        "warnings": warnings,
    }


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def sync_portfolio(data_dir: str = "data") -> str:
    """
    自動探索 data_dir，合併國泰雙格式 CSV，回傳持倉摘要表格。

    自動識別：
      複委託庫存*     → 美股（代號: 代號/股票代號, 股數: 目前庫存, 成本: 均價）
      證券未實現彙總* → 台股（代號: 股票名稱, 股數: 股數, 成本: 成交均價）

    Args:
        data_dir: CSV 所在目錄，預設 'data/'。

    Returns:
        格式化持倉摘要字串；失敗時回傳 ERROR。
    """
    result = load_portfolio(Path(data_dir))
    df     = result["df"]

    lines = [
        f"[FILES]   Foreign: {result['files']['foreign'] or 'NOT FOUND'}"
        f"  |  TW: {result['files']['tw'] or 'NOT FOUND'}",
        f"[COUNTS]  US/Foreign: {result['us_count']}  |  TW: {result['tw_count']}"
        f"  |  Total: {len(df)}",
    ]
    if result["warnings"]:
        lines.append(f"[WARNINGS] {'; '.join(result['warnings'])}")

    if df.empty:
        lines.append("[ERROR] 無有效持倉資料")
        return "\n".join(lines)

    df = df.copy()
    df["Total_Cost"] = df["Shares"] * df["Cost_Basis"]
    total_cost       = df["Total_Cost"].sum()

    lines += [
        "",
        f"{'TICKER':<14} {'SRC':<14} {'SHARES':>8} {'COST_BASIS':>12} {'TOTAL_COST':>14}",
        "-" * 66,
    ]
    for _, row in df.iterrows():
        lines.append(
            f"{row['Ticker']:<14} {row['source']:<14} {row['Shares']:>8.0f}"
            f" {row['Cost_Basis']:>12.2f} {row['Total_Cost']:>14.2f}"
        )
    lines += [
        "-" * 66,
        f"{'TOTAL':<14} {'':<14} {len(df):>8} {'':>12} {total_cost:>14.2f}",
    ]
    return "\n".join(lines)


@mcp.tool()
def fetch_market_data(ticker: str) -> str:
    """
    透過 yfinance 取得股票的關鍵估值與成長指標。

    台股格式：'2330.TW'；美股格式：'NVDA'。

    Returns:
        JSON 字串；取得失敗時對應欄位為 null 並附帶 error。
    """
    result: dict = {
        "ticker":          ticker,
        "current_price":   None,
        "trailing_pe":     None,
        "earnings_growth": None,
        "revenue_growth":  None,
        "ps_ratio":        None,
        "week_52_high":    None,
        "capex_to_rev":    None,
        "currency":        None,
        "error":           None,
    }
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        result["current_price"]   = info.get("currentPrice") or info.get("regularMarketPrice")
        result["trailing_pe"]     = info.get("trailingPE")
        result["earnings_growth"] = info.get("earningsGrowth")
        result["revenue_growth"]  = info.get("revenueGrowth")
        result["ps_ratio"]        = info.get("priceToSalesTrailing12Months")
        result["week_52_high"]    = info.get("fiftyTwoWeekHigh")
        result["currency"]        = info.get("currency")

        try:
            cf            = t.cashflow
            total_revenue = info.get("totalRevenue") or 0
            if "Capital Expenditure" in cf.index and total_revenue > 0:
                capex               = abs(float(cf.loc["Capital Expenditure"].iloc[0]))
                result["capex_to_rev"] = round(capex / total_revenue, 4)
        except Exception:
            pass

        if result["current_price"] is None:
            result["error"] = "currentPrice 為空，請確認代號是否正確"

    except Exception as exc:
        result["error"] = str(exc)

    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
