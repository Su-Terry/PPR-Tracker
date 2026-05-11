"""
Alpha Strategist — System-wide configuration constants.

Separation of concerns: keep tunable parameters here so they are never
scattered as magic strings across business logic modules.
"""

from __future__ import annotations

# ── Safe Haven Pool ───────────────────────────────────────────────────────────
# BOXX is the only NRA-friendly cash-equivalent ETF here: it distributes
# returns as capital gains (options spread), NOT dividends.
# SGOV / USFR are excluded — they pay monthly dividends subject to the 30 %
# US withholding tax for Non-Resident Aliens, making them net-negative for
# short parking windows after friction.
SAFE_HAVENS: list[str] = ["BOXX"]

# ── Broker Microstructure (Cathay 國泰 複委託, USD leg) ────────────────────────
STOCK_FEE_RATE       = 0.001    # 0.10 % per leg (buy or sell) for stocks
ETF_FLAT_FEE         = 3.0      # USD flat fee per ETF trade leg

# ── BOXX Yield Assumption ─────────────────────────────────────────────────────
# Annualised BOXX yield (options-spread capital gains, no dividend withholding).
# Update when Fed funds rate changes materially.
BOXX_YIELD           = 0.052    # ~5.2 % p.a.

# ── Safe Haven Routing Parameters ────────────────────────────────────────────
# Used by _compute_haven_route() to decide BOXX vs pure USD Cash.
DEFAULT_CAPITAL_BLOCK = 5_000   # USD — assumed position block when size is unknown
EXPECTED_PARK_DAYS    = 14      # days capital is expected to sit before reinvesting
