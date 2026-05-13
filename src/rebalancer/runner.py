"""
Alpha Strategist — V2.0 Pipeline Runner (Sprint 5)

Top-level orchestrator that wires Sprint 1-4 modules into a single callable:

    result, metrics = run("US")

Sequence per spec §3.2:
  1. PortfolioState.load()         — current holdings + cash (Sprint 1)
  2. yfinance.download()           — historical prices for covariance
  3. get_market_data()             — current prices + efficiency scores
  4. Sector matrix from CSV        — constraint input for optimizer (Sprint 1)
  5. estimate_covariance()         — Ledoit-Wolf shrinkage (Sprint 2)
  6. solve_target_weights()        — QP solve (Sprint 2)
  7. build_trades()                — Trade list + conviction (Sprint 3)
  8. archive_decision()            — append to JSONL archive (Sprint 3)
  9. compute_metrics()             — discipline dashboard (Sprint 3)
  → returns (BuildResult, DisciplineMetrics)

The runner is "pure of Slack/scheduling concerns": no push notifications,
no APScheduler, no module-level side effects. Calling run() twice is safe.

Config overrides: if config_override is None, runner checks
data/rebalance_config_override.json and applies any stored field overrides.
The /rebalance config set Slack command writes that file.

CRITICAL: Human-in-the-Loop. This function never triggers real orders.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yfinance as yf

from src.cost.profile import CostProfile
from src.data_fetcher import ScanResult, get_market_data
from src.discipline.metrics import DisciplineMetrics, EmptyActualsProvider, compute_metrics
from src.portfolio.state import PortfolioState
from src.rebalancer.config import RebalanceConfig
from src.rebalancer.cov_estimator import estimate_covariance
from src.rebalancer.optimizer import solve_target_weights
from src.rebalancer.rationale import RationaleContext
from src.rebalancer.trade_builder import BuildResult, archive_decision, build_trades
from src.risk_engine import MarketRegime
from src.strategies.scoring import efficiency_score

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class ScanData:
    """Scan results returned alongside BuildResult so callers avoid duplicate API calls."""
    portfolio_results: list[ScanResult]


# ── Default file paths ────────────────────────────────────────────────────────

_DEFAULT_STATE_PATH     = Path("data/portfolio_state.json")
_DEFAULT_DECISIONS_PATH = Path("memory/rebalance_decisions.jsonl")
_DEFAULT_ACTUALS_PATH   = Path("memory/actual_trades.jsonl")
_DEFAULT_SECTOR_PATH    = Path("data/sector_mapping.csv")
_DEFAULT_COST_PATH      = Path("data/cost_profile.json")
_CONFIG_OVERRIDE_PATH   = Path("data/rebalance_config_override.json")

_CASH_TICKER = "CASH"
_HISTORY_PERIOD = "1y"        # yfinance download window


# ── Config override helpers ───────────────────────────────────────────────────

def _load_config_override(
    market: Literal["US", "TW"],
    base: RebalanceConfig,
) -> RebalanceConfig:
    """
    Apply any stored field overrides from data/rebalance_config_override.json.

    The file may contain a "US" and/or "TW" key mapping to a dict of
    RebalanceConfig field names → values. Unknown keys are silently ignored.
    A missing file or parse error returns the base config unchanged.
    """
    if not _CONFIG_OVERRIDE_PATH.exists():
        return base

    try:
        overrides = json.loads(_CONFIG_OVERRIDE_PATH.read_text(encoding="utf-8"))
        market_overrides: dict = overrides.get(market, {})
        if not market_overrides:
            return base
        valid_fields = {f.name for f in dataclasses.fields(RebalanceConfig)}
        filtered = {k: v for k, v in market_overrides.items() if k in valid_fields}
        if filtered:
            return dataclasses.replace(base, **filtered)
    except Exception as exc:
        logger.warning("[RUNNER] 忽略 config override 解析失敗：%s", exc)

    return base


# ── Sector matrix builder ─────────────────────────────────────────────────────

def _build_sector_matrix(
    tickers: list[str],
    cash_idx: int,
    sector_path: Path,
) -> tuple[np.ndarray, list[str]]:
    """
    Build K×N sector indicator matrix and sector_names list.

    Tickers missing from sector_mapping.csv fall back to "其他" sector.
    CASH ticker is assigned to its own "現金" sector so it never hits the
    max_sector constraint.

    Returns:
        (sector_matrix: np.ndarray shape (K, N), sector_names: list[str])
    """
    ticker_to_l1: dict[str, str] = {}
    if sector_path.exists():
        try:
            df = pd.read_csv(sector_path, dtype=str)
            for _, row in df.iterrows():
                t = str(row.get("ticker", "") or row.get("Ticker", "")).strip()
                s = str(row.get("l1_sector", "") or row.get("L1_Sector", "")).strip()
                if t and s:
                    ticker_to_l1[t] = s
        except Exception as exc:
            logger.warning("[RUNNER] sector_mapping.csv 讀取失敗：%s", exc)
    else:
        logger.warning("[RUNNER] sector_mapping.csv 不存在，所有非現金標的歸入 '其他' 板塊")

    sector_names: list[str] = []
    for i, ticker in enumerate(tickers):
        if i == cash_idx:
            sec = "現金"
        else:
            sec = ticker_to_l1.get(ticker, "其他")
        if sec not in sector_names:
            sector_names.append(sec)

    n = len(tickers)
    k = len(sector_names)
    sector_matrix = np.zeros((k, n), dtype=float)
    for i, ticker in enumerate(tickers):
        if i == cash_idx:
            sec = "現金"
        else:
            sec = ticker_to_l1.get(ticker, "其他")
        j = sector_names.index(sec)
        sector_matrix[j, i] = 1.0

    return sector_matrix, sector_names


# ── Historical price fetcher ──────────────────────────────────────────────────

def _fetch_price_history(
    equity_tickers: list[str],
    config: RebalanceConfig,
) -> pd.DataFrame:
    """
    Download lookback_days_ideal bars of daily close prices from yfinance.

    CASH column is appended as a constant 1.0 series. Returns a DataFrame with
    equity_tickers + ["CASH"] as columns. NaN are forward-filled (then back-filled
    for the first rows). If download entirely fails, returns an empty DataFrame
    causing cov_estimator to use the diagonal fallback.
    """
    if not equity_tickers:
        return pd.DataFrame(columns=[_CASH_TICKER])

    try:
        raw = yf.download(
            tickers=equity_tickers,
            period=_HISTORY_PERIOD,
            auto_adjust=True,
            progress=False,
        )
        # yf.download returns MultiIndex columns when multiple tickers
        if isinstance(raw.columns, pd.MultiIndex):
            prices = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw.xs("Close", axis=1, level=0)
        else:
            prices = raw["Close"] if "Close" in raw.columns else raw

        prices = prices.ffill().bfill()
        prices[_CASH_TICKER] = 1.0
        return prices.astype(float)

    except Exception as exc:
        logger.error("[RUNNER] 歷史價格下載失敗：%s", exc)
        return pd.DataFrame(columns=equity_tickers + [_CASH_TICKER])


# ── Score normalization ───────────────────────────────────────────────────────

def _compute_z_scores(
    scan_results: list[ScanResult],
    tickers: list[str],
    cash_idx: int,
    regime: MarketRegime | None = None,
) -> np.ndarray:
    """
    Compute cross-sectional z-scores from efficiency_score() for the QP.

    CASH is fixed at 0.0. Missing / zero-score tickers (missing data) are set
    to the lowest non-zero percentile after normalization so the optimizer can
    still deprioritize them relative to high-quality tickers.

    Args:
        scan_results: ScanResult objects for all equity tickers.
        tickers:      Full ticker list including CASH at cash_idx.
        cash_idx:     Index of CASH in tickers.
        regime:       MarketRegime for beta penalty in BEAR/CRASH.

    Returns:
        Z-score array of shape (len(tickers),).
    """
    scan_map: dict[str, ScanResult] = {r.ticker: r for r in scan_results}
    n = len(tickers)
    raw = np.zeros(n)
    for i, t in enumerate(tickers):
        if i == cash_idx:
            continue
        r = scan_map.get(t)
        raw[i] = efficiency_score(r, regime=regime) if r is not None else 0.0

    equity_mask = np.array([i != cash_idx for i in range(n)])
    equity_scores = raw[equity_mask]

    if equity_scores.std() < 1e-9:
        return raw

    z = np.zeros(n)
    z[equity_mask] = (equity_scores - equity_scores.mean()) / equity_scores.std()
    return z


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(
    market: Literal["US", "TW"],
    state_path: Path | None = None,
    decisions_path: Path | None = None,
    actuals_path: Path | None = None,
    config_override: RebalanceConfig | None = None,
    regime: MarketRegime | None = None,
) -> tuple[BuildResult, DisciplineMetrics, ScanData]:
    """
    Execute the full V2.0 rebalancing pipeline for one market.

    Args:
        market:          "US" or "TW".
        state_path:      Path to portfolio_state.json. Defaults to
                         data/portfolio_state.json.
        decisions_path:  Path to rebalance_decisions.jsonl archive.
        actuals_path:    Path to actual_trades.jsonl for discipline scoring.
        config_override: RebalanceConfig to use instead of the market default
                         and any stored file-based overrides.

    Returns:
        (BuildResult, DisciplineMetrics). BuildResult.is_hold is True when
        the optimizer or post-processing determines no trade is warranted.

    Raises:
        OSError:    If state_path does not exist.
        ValueError: If PortfolioState schema is incompatible.
    """
    state_path      = state_path      or _DEFAULT_STATE_PATH
    decisions_path  = decisions_path  or _DEFAULT_DECISIONS_PATH
    actuals_path    = actuals_path    or _DEFAULT_ACTUALS_PATH

    logger.info("[RUNNER] 開始 %s 投資組合再平衡分析", market)

    # ── 1. Load portfolio state ────────────────────────────────────────────────
    state = PortfolioState.load(state_path)

    if market == "US":
        holdings   = state.us_holdings   # dict[str, float]: ticker → shares
        cash_value = state.us_cash_usd
    else:
        holdings   = state.tw_holdings
        cash_value = state.tw_cash_twd

    equity_tickers: list[str] = list(holdings.keys())

    if not equity_tickers and cash_value <= 0:
        logger.warning("[RUNNER] %s 空持倉且無現金，直接回傳 HOLD", market)
        from src.rebalancer.optimizer import OptimizeResult
        empty_result = OptimizeResult(
            w_target=np.array([1.0]),
            is_hold=True,
            hold_reason="empty_portfolio",
            infeasible=False,
        )
        empty_build = build_trades(
            optimize_result=empty_result,
            tickers=[_CASH_TICKER],
            w0=np.array([1.0]),
            scores=np.array([0.0]),
            prices={_CASH_TICKER: 1.0},
            portfolio_value=max(cash_value, 1.0),
            cost_profile=CostProfile.from_defaults(),
            config=RebalanceConfig.us_default() if market == "US" else RebalanceConfig.tw_default(),
            cash_idx=0,
            market=market,
            sector_matrix=np.array([[1.0]]),
            sector_names=["現金"],
            cov_estimate=__import__("src.rebalancer.cov_estimator", fromlist=["CovEstimate"]).CovEstimate(
                matrix=np.array([[0.0]]),
                days_per_ticker={_CASH_TICKER: 0},
                used_ledoit_wolf=False,
            ),
        )
        empty_metrics = compute_metrics(
            w_current=np.array([1.0]),
            tickers=[_CASH_TICKER],
            decisions_path=decisions_path,
            actuals_provider=EmptyActualsProvider(),
        )
        return empty_build, empty_metrics, ScanData(portfolio_results=[])

    # ── 2. Fetch current prices and efficiency scores ──────────────────────────
    scan_results: list[ScanResult] = get_market_data(equity_tickers)
    scan_map = {r.ticker: r for r in scan_results}

    prices_map: dict[str, float] = {_CASH_TICKER: 1.0}
    for r in scan_results:
        if r.current_price and not r.error:
            prices_map[r.ticker] = r.current_price

    # ── 3. Compute portfolio value and current weights ─────────────────────────
    holdings_value = sum(
        qty * prices_map.get(t, 0.0) for t, qty in holdings.items()
    )
    total_value = cash_value + holdings_value

    if total_value <= 0:
        logger.warning("[RUNNER] %s 總資產為零，直接回傳 HOLD", market)
        total_value = 1.0

    tickers: list[str] = [_CASH_TICKER] + equity_tickers
    cash_idx = 0
    n = len(tickers)

    w0 = np.zeros(n)
    w0[0] = cash_value / total_value
    for i, t in enumerate(equity_tickers, start=1):
        price = prices_map.get(t, 0.0)
        w0[i] = (holdings.get(t, 0.0) * price) / total_value

    # Normalise so weights sum to 1.0 (handles rounding + missing prices)
    if w0.sum() > 0:
        w0 = w0 / w0.sum()
    else:
        w0[0] = 1.0

    # ── 4. Load configuration ──────────────────────────────────────────────────
    if config_override is not None:
        config = config_override
    else:
        base   = RebalanceConfig.us_default() if market == "US" else RebalanceConfig.tw_default()
        config = _load_config_override(market, base)

    # ── 5. Fetch historical prices for covariance ──────────────────────────────
    prices_df = _fetch_price_history(equity_tickers, config)

    # Reorder columns to match tickers list (CASH first)
    ordered_cols = [_CASH_TICKER] + [t for t in equity_tickers if t in prices_df.columns]
    missing_from_history = [t for t in equity_tickers if t not in prices_df.columns]
    if missing_from_history:
        logger.warning("[RUNNER] 以下標的無歷史價格：%s", ", ".join(missing_from_history))
        for t in missing_from_history:
            prices_df[t] = 1.0
        ordered_cols = [_CASH_TICKER] + equity_tickers
    prices_df = prices_df[ordered_cols] if all(c in prices_df.columns for c in ordered_cols) else prices_df

    # ── 6. Estimate covariance ─────────────────────────────────────────────────
    cov_estimate = estimate_covariance(prices_df, config)

    # ── 7. Build sector matrix ─────────────────────────────────────────────────
    sector_matrix, sector_names = _build_sector_matrix(
        tickers=tickers,
        cash_idx=cash_idx,
        sector_path=_DEFAULT_SECTOR_PATH,
    )

    # ── 8. Compute z-score inputs for the QP ──────────────────────────────────
    scores = _compute_z_scores(scan_results, tickers, cash_idx, regime=regime)

    # ── 9. Solve target weights ────────────────────────────────────────────────
    optimize_result = solve_target_weights(
        w0=w0,
        scores=scores,
        cov=cov_estimate.matrix,
        sector_matrix=sector_matrix,
        config=config,
        cash_idx=cash_idx,
    )

    logger.info(
        "[RUNNER] 優化結果 — is_hold=%s  solver_status=%s  relaxations=%s",
        optimize_result.is_hold,
        optimize_result.solver_status,
        optimize_result.relaxations_applied,
    )

    # ── 10. Load cost profile ──────────────────────────────────────────────────
    try:
        cost_profile = CostProfile.load(_DEFAULT_COST_PATH)
    except (OSError, ValueError) as e:
        logger.warning("[RUNNER] cost_profile 載入失敗，使用預設值：%s", e)
        cost_profile = CostProfile.from_defaults()

    # ── 11. Build rationale contexts from ScanResults ─────────────────────────
    w_target = optimize_result.w_target
    rationale_contexts: list[RationaleContext] = []
    for i, ticker in enumerate(tickers):
        if i == cash_idx:
            continue
        delta = float(w_target[i]) - float(w0[i])
        side  = "BUY" if delta >= 0 else "SELL"
        r     = scan_map.get(ticker)
        rationale_contexts.append(
            RationaleContext(
                ticker=ticker,
                side=side,
                score_delta=abs(float(scores[i])),
                w0=float(w0[i]),
                rsi=None,
                peg_ratio=r.modified_peg if r else None,
                is_discovery=(float(w0[i]) == 0.0),
            )
        )

    # ── 12. Build trades ───────────────────────────────────────────────────────
    result = build_trades(
        optimize_result=optimize_result,
        tickers=tickers,
        w0=w0,
        scores=scores,
        prices=prices_map,
        portfolio_value=total_value,
        cost_profile=cost_profile,
        config=config,
        cash_idx=cash_idx,
        market=market,
        sector_matrix=sector_matrix,
        sector_names=sector_names,
        cov_estimate=cov_estimate,
        rationale_contexts=rationale_contexts,
        decisions_path=decisions_path,
    )

    logger.info(
        "[RUNNER] 交易建議 — is_hold=%s  trades=%d",
        result.is_hold, len(result.trades),
    )

    # ── 13. Archive decision ───────────────────────────────────────────────────
    archive_decision(
        result=result,
        w_current=w0,
        tickers=tickers,
        config=config,
        optimize_result=optimize_result,
        path=decisions_path,
    )

    # ── 14. Compute discipline metrics ─────────────────────────────────────────
    try:
        from src.slack.actuals_provider import JsonlActualsProvider
        actuals_provider = JsonlActualsProvider(actuals_path)
    except Exception:
        actuals_provider = EmptyActualsProvider()

    metrics = compute_metrics(
        w_current=w0,
        tickers=tickers,
        decisions_path=decisions_path,
        actuals_provider=actuals_provider,
    )

    logger.info(
        "[RUNNER] 紀律指標 — drift=%.1f%%  turnover_30d=%.1f%%  "
        "last_trade_days=%d  discipline_7d=%d/100",
        metrics.drift_pct,
        metrics.turnover_30d * 100,
        metrics.last_trade_days,
        metrics.discipline_score_7d,
    )

    return result, metrics, ScanData(portfolio_results=scan_results)
