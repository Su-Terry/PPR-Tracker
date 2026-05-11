"""
Alpha Strategist — Quantitative Strategy Engine

所有函式為純函式（Pure Functions），無副作用、無 I/O。

估值模型：
  PEG Model  → calculate_modified_peg()  適用有獲利、有 EPS 成長的股票
  P/S Model  → calculate_ps_growth_ratio() 適用超高成長但尚未獲利（Pre-Profit）的股票
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


def calculate_modified_peg(
    pe: float,
    eps_growth: float,
    capex_to_rev: float,
) -> float:
    """
    計算修正版林區公式（Modified PEG Ratio）。

    **百分比參數一律使用小數格式（Decimal Form）**：
      - 0.20 代表 20%，來源為 yfinance info["earningsGrowth"]，直接傳入。

    **公式**：PE / ((eps_growth + capex_to_rev) × 100)
      - 內部乘以 100 將小數還原為百分比尺度，與傳統 PEG 閾值對齊。
      - 觸發閾值：> 1.5 減倉停利，< 0.8 加倉 Alpha。

    Args:
        pe:           本益比，例如 25.0。
        eps_growth:   EPS 年化成長率（小數），例如 0.20 代表 20%。
        capex_to_rev: 資本支出佔營收比（小數），例如 0.05 代表 5%。

    Returns:
        Modified PEG；分母 <= 0 時回傳 float("inf")。
    """
    raw = eps_growth + capex_to_rev
    if raw <= 0.0:
        return float("inf")
    return round(pe / (raw * 100), 4)


def calculate_ps_growth_ratio(
    ps_ratio: float,
    revenue_growth: float,
) -> float:
    """
    計算 P/S 成長比（Pre-Profit Hyper-Growth 估值模型）。

    適用對象：淨利 < 0 但營收高速成長的公司（如 CRWV）。
    傳統 PEG 在此失效，改用市銷率相對營收成長率衡量估值。

    **公式**：P/S Ratio / (revenue_growth × 100)
      - 與 Modified PEG 使用相同的百分比尺度換算。
      - 參考閾值：< 0.5 視為 Hyper-Growth Alpha 候選；> 3.0 視為過熱。

    Args:
        ps_ratio:       市銷率（Price-to-Sales），例如 12.4。
                        來源：yfinance info["priceToSalesTrailing12Months"]。
        revenue_growth: 營收年化成長率（小數），例如 1.85 代表 185%。
                        來源：yfinance info["revenueGrowth"]。

    Returns:
        P/S Growth Ratio；revenue_growth <= 0 時回傳 float("inf")。
    """
    if revenue_growth <= 0.0:
        return float("inf")
    return round(ps_ratio / (revenue_growth * 100), 4)


def check_momentum_trend(
    prices: pd.Series,
    week_52_high: Optional[float] = None,
) -> bool:
    """
    判斷股票是否處於健康的多頭動能結構。

    條件 1（必要）：最新收盤價 > 50MA > 200MA（三線多頭排列）。
    條件 2（選填）：若提供 week_52_high，現價須在 52 週高點 90% 以上。
      - 距 52 週高點超過 10% → 視為趨勢衰竭或落刀風險，回傳 False。
      - 此守衛防止在基本面便宜但技術面已破壞時誤觸加倉訊號。

    需至少 200 根 K 棒；資料不足時保守回傳 False。

    Args:
        prices:       按時間升冪排列的收盤價 pd.Series。
        week_52_high: 52 週最高價（選填）。提供時啟用近高點守衛。
                      來源：yfinance info["fiftyTwoWeekHigh"]。

    Returns:
        True  → 三線多頭排列，且（若提供 52 週高）現價在高點 90% 以上。
        False → 任一條件不滿足，或資料不足。
    """
    if len(prices) < 200:
        return False

    ma50   = prices.rolling(50).mean().iloc[-1]
    ma200  = prices.rolling(200).mean().iloc[-1]
    latest = prices.iloc[-1]

    if not (latest > ma50 > ma200):
        return False

    # 52 週近高點守衛
    if week_52_high is not None and week_52_high > 0:
        if latest / week_52_high < 0.90:
            return False

    return True
