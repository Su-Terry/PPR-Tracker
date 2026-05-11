"""
Alpha Strategist — Visual Analytics (V1.3 — Bloomberg Dark)

Chart A: IPO Value Gap  — bullet/marker style; single axis per ticker, normalized
         to "% premium above floor price" so all tickers share one scale.
Chart B: Alpha Quadrant — high-contrast scatter; bubble size encodes signal strength;
         auto-arrow to most dangerous ticker; no-overlap label placement.

Bloomberg aesthetic: pitch-black background, neon green / electric red accents.
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from src.data_fetcher import ScanResult

logger    = logging.getLogger(__name__)
PLOTS_DIR = Path("data/plots")

# ── Bloomberg palette ─────────────────────────────────────────────────────────
_BLACK   = "#000000"
_PANEL   = "#0a0a0a"
_GRID    = "#1a1a1a"
_DIM     = "#333333"

_NEON_GREEN  = "#00FF41"
_NEON_RED    = "#FF0033"
_NEON_BLUE   = "#00BFFF"
_NEON_ORANGE = "#FF8C00"
_SILVER      = "#888888"
_WHITE       = "#F0F0F0"
_GOLD        = "#FFD700"   # Discovery targets

_ZONE_GREEN = "#003300"   # value zone fill
_ZONE_RED   = "#330000"   # bubble zone fill
_ZONE_SPEC  = "#1a0a00"   # speculative / valuation-below-floor fill

_Q_BUY  = "#001a00"       # quadrant fills (chart B)
_Q_MOM  = "#001200"
_Q_EXIT = "#1a0000"
_Q_HOLD = "#121200"

_INF_CAP = 5.0


def _ensure_plots_dir() -> None:
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)


def _bloomberg_axes(ax: plt.Axes) -> None:
    """Apply Bloomberg-style dark theme to a single Axes instance."""
    ax.set_facecolor(_PANEL)
    for spine in ax.spines.values():
        spine.set_color(_DIM)
    ax.tick_params(colors=_WHITE, labelsize=9)
    ax.xaxis.label.set_color(_WHITE)
    ax.yaxis.label.set_color(_WHITE)
    ax.title.set_color(_WHITE)
    ax.grid(False)


# ── Chart A: IPO Value Gap ─────────────────────────────────────────────────────

def generate_ipo_value_gap(arb_matches: list[dict]) -> Path | None:
    """
    Bullet-chart showing where the current market price sits relative to the
    Value Zone (Floor → Ceiling) for each active auction event.

    X-axis is normalised to "% above floor price" so all tickers share one scale.

    Visual encoding:
      ◆ Gray diamond  — Floor Price (0 %, the reference anchor)
      | Blue vline    — Fundamental Fair Value
      | Orange vline  — Bidding Ceiling (Fair × 1.15)
      ● Green dot     — Current market price is inside the Value Zone
      ● Red dot       — Current market price has pierced the Ceiling (overheated)
      Green fill      — Value Zone  (Floor → Ceiling)
      Red fill        — Bubble Zone (Ceiling → x-max)
    """
    if not arb_matches:
        logger.info("[VISUALIZER] No auction data — skipping value gap chart.")
        return None

    _ensure_plots_dir()

    rows: list[dict] = []
    for m in arb_matches:
        evt  = m.get("event", {})
        bid  = m.get("bidding", {})
        val  = m.get("valuation", {})
        bare = evt.get("ticker", "")
        if not bare:
            continue

        suffix = ".TW" if evt.get("exchange", "TWSE") == "TWSE" else ".TWO"
        label  = f"{bare}{suffix}"

        try:
            floor = float(evt.get("floor_price") or 0)
        except (ValueError, TypeError):
            floor = 0.0
        if floor <= 0:
            continue

        def _safe(v: object) -> float | None:
            try:
                return float(v) if v is not None else None  # type: ignore[arg-type]
            except (ValueError, TypeError):
                return None

        eps  = _safe(val.get("eps"))
        fair = _safe(bid.get("fair_value"))
        ceil = _safe(bid.get("ceiling"))
        mkt  = _safe(m.get("current_price") or val.get("ref_price"))

        # Normalise to % above floor (x-axis origin = floor = 0 %)
        def _pct(price: float | None) -> float | None:
            return (price - floor) / floor * 100 if price is not None else None

        norm_fair = _pct(fair)
        norm_ceil = _pct(ceil)
        norm_mkt  = _pct(mkt)

        # Speculative: fair value sits below the floor price
        # → P/E anchor is irrelevant; the market is pricing in growth/narrative premium
        is_speculative = (norm_fair is not None and norm_fair < 0) or \
                         (norm_ceil is not None and norm_ceil < 0)

        # ⚠️ 投機溢價 tag: low EPS (<0.5) while market trades >30% above floor
        spec_tag = (
            eps is not None and eps < 0.5
            and norm_mkt is not None and norm_mkt > 30
        )

        rows.append({
            "label":          label,
            "floor":          floor,
            "mkt":            mkt,
            "norm_fair":      norm_fair,
            "norm_ceil":      norm_ceil,
            "norm_mkt":       norm_mkt,
            "overheated":     (ceil is not None and mkt is not None and mkt > ceil),
            "is_speculative": is_speculative,
            "spec_tag":       spec_tag,
            "eps":            eps,
        })

    if not rows:
        logger.info("[VISUALIZER] No plottable auction rows — skipping value gap chart.")
        return None

    n = len(rows)

    # x-axis: always start at or left of 0%; never let negative fair/ceil values
    # drag the axis left and create a confusing negative-premium region.
    all_x_positive = [0.0]
    for r in rows:
        for v in (r["norm_ceil"], r["norm_mkt"]):
            if v is not None and v > 0:
                all_x_positive.append(v)
        # Include norm_fair only when it's above floor (valid value zone)
        if r["norm_fair"] is not None and r["norm_fair"] > 0:
            all_x_positive.append(r["norm_fair"])
    x_lo = -4.0   # small left margin so floor diamond isn't clipped
    x_hi = max(all_x_positive) + 15

    fig, ax = plt.subplots(figsize=(12, max(2.8, n * 0.85 + 1.6)))
    fig.patch.set_facecolor(_BLACK)
    _bloomberg_axes(ax)

    BAR_H = 0.42

    for i, row in enumerate(rows):
        y            = float(i)
        nc           = row["norm_ceil"]
        nm           = row["norm_mkt"]
        overheated   = row["overheated"]
        speculative  = row["is_speculative"]

        # ── Shaded band ───────────────────────────────────────────────────────
        if speculative:
            # Fair value is below floor → entire visible range is "speculative territory"
            ax.barh(y, x_hi - x_lo, left=x_lo, height=BAR_H,
                    color=_ZONE_SPEC, alpha=0.9, zorder=1)
        elif nc is not None:
            # Normal case: green value zone (0 → ceiling), red bubble zone (ceiling → x_hi)
            ax.barh(y, nc, left=0.0, height=BAR_H,
                    color=_ZONE_GREEN, alpha=0.85, zorder=1)
            if x_hi > nc:
                ax.barh(y, x_hi - nc, left=nc, height=BAR_H,
                        color=_ZONE_RED, alpha=0.70, zorder=1)
        else:
            # No valuation data at all
            ax.barh(y, x_hi - x_lo, left=x_lo, height=BAR_H,
                    color=_GRID, alpha=0.6, zorder=1)

        # Centre axis line
        ax.plot([x_lo, x_hi], [y, y], color=_DIM, lw=0.6, zorder=2)

        # ── Floor marker ─────────────────────────────────────────────────────
        ax.scatter(0, y, s=100, c=_SILVER, marker="D", zorder=5, linewidths=0)

        # ── Fair Value tick (only when above floor) ───────────────────────────
        if row["norm_fair"] is not None and not speculative:
            ax.vlines(row["norm_fair"], y - BAR_H * 0.65, y + BAR_H * 0.65,
                      colors=_NEON_BLUE, linewidth=2.5, zorder=6)

        # ── Ceiling tick (only when above floor) ─────────────────────────────
        if nc is not None and not speculative:
            ax.vlines(nc, y - BAR_H * 0.75, y + BAR_H * 0.75,
                      colors=_NEON_ORANGE, linewidth=2.0, zorder=6)

        # ── Speculative: "Valuation Below Floor" label + optional tag ─────────
        if speculative:
            ax.text(1.0, y, "Valuation Below Floor",
                    va="center", ha="left", fontsize=7.5,
                    color=_SILVER, alpha=0.75, style="italic", zorder=6)

        # ── Current price dot ─────────────────────────────────────────────────
        if nm is not None:
            # In speculative zone: dot always red (market is above any rational anchor)
            dot_c = _NEON_RED if (overheated or speculative) else _NEON_GREEN
            ax.scatter(nm, y, s=160, c=dot_c, zorder=7,
                       edgecolors=_WHITE, linewidths=0.8)
            price_label = f"NT$ {row['floor'] * (1 + nm / 100):,.0f}"
            if row["spec_tag"]:
                price_label += "  ⚠ 投機溢價"
            ax.text(nm + 1.0, y, price_label,
                    va="center", ha="left",
                    fontsize=8, color=dot_c, fontweight="bold")

    # ── Reference line at 0 % (Floor) ────────────────────────────────────────
    ax.axvline(0, color=_SILVER, lw=0.8, linestyle="--", alpha=0.5, zorder=3)
    ax.text(0.4, n - 0.08, "Floor", fontsize=7, color=_SILVER,
            ha="left", va="top", alpha=0.7)

    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(-0.7, n - 0.3)
    ax.set_yticks(range(n))
    ax.set_yticklabels([r["label"] for r in rows], fontsize=10, color=_WHITE)
    ax.set_xlabel("Premium above Floor Price  (%)", color=_WHITE, fontsize=10)
    ax.set_title("IPO / Auction Value Gap", color=_WHITE, fontsize=13,
                 pad=10, fontweight="bold")
    ax.xaxis.set_major_formatter(mticker.FormatStrFormatter("%+.0f%%"))

    legend_items = [
        mpatches.Patch(facecolor=_ZONE_GREEN, edgecolor="none", label="Value Zone  (Floor → Ceiling)"),
        mpatches.Patch(facecolor=_ZONE_RED,   edgecolor="none", label="Bubble Zone  (Above Ceiling)"),
        mpatches.Patch(facecolor=_ZONE_SPEC,  edgecolor="none", label="Speculative  (Valuation Below Floor)"),
        plt.Line2D([0], [0], color=_NEON_BLUE,   lw=2.5, label="Fair Value"),
        plt.Line2D([0], [0], color=_NEON_ORANGE, lw=2.0, label="Ceiling  (Fair × 1.15)"),
        plt.scatter([], [], s=120, c=_NEON_GREEN, edgecolors=_WHITE, lw=0.8, label="Market Price  ≤ Ceiling"),
        plt.scatter([], [], s=120, c=_NEON_RED,   edgecolors=_WHITE, lw=0.8, label="Market Price  > Ceiling / Speculative"),
    ]
    ax.legend(
        handles=legend_items, loc="lower right", fontsize=8,
        facecolor=_BLACK, labelcolor=_WHITE, edgecolor=_DIM, framealpha=0.9,
    )

    plt.tight_layout(pad=1.2)
    out = PLOTS_DIR / "ipo_valuation_gap.png"
    try:
        fig.savefig(out, dpi=140, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        logger.info("[VISUALIZER] Value gap chart → %s  (%d rows)", out, n)
    except Exception as exc:
        logger.error("[VISUALIZER] Failed to save value gap chart: %s", exc)
        out = None  # type: ignore[assignment]
    finally:
        plt.close(fig)
    return out


# ── Chart B: Alpha Quadrant ────────────────────────────────────────────────────

def generate_alpha_quadrant(
    results:   list[ScanResult],
    discovery: list[ScanResult] | None = None,
) -> Path | None:
    """
    High-contrast scatter plot on a Bloomberg-black canvas.

    X-axis: Distance to 50-day MA (%)
    Y-axis: Modified PEG / P/S Growth Ratio  (inf capped at 5×, shown as △)
    Bubble: size ∝ |ratio × MA-distance|  (bigger = stronger signal)
    Arrow:  points to the single most "critical" ticker
            (highest ratio among those above MA, or highest overall).
    Labels: auto-placed with quadrant-aware offsets; no overlap.
    Stars:  Gold ★ markers for Discovery targets (from scan_market_for_alpha).
    """
    pts: list[dict] = []

    for r in results:
        if r.error or r.current_price is None or r.ma50 is None:
            continue

        if r.valuation_model == "PEG" and r.modified_peg is not None:
            raw = r.modified_peg
        elif r.valuation_model == "PS" and r.ps_growth_ratio is not None:
            raw = r.ps_growth_ratio
        else:
            continue

        if raw < 0:
            continue

        is_inf  = raw == float("inf")
        ratio   = _INF_CAP if is_inf else min(raw, _INF_CAP)
        ma_dist = (r.current_price - r.ma50) / r.ma50 * 100

        _CB_SIGNALS = {"減倉停利", "趨勢走弱", "嚴重技術面過熱：強制減倉停利", "技術面嚴重破線：需停損"}
        is_critical = any(s in _CB_SIGNALS for s in r.signals)
        is_alpha    = any("加倉 Alpha" in s for s in r.signals)
        color = _NEON_RED if is_critical else (_NEON_GREEN if is_alpha else _SILVER)

        # Bubble size: proportional to |ratio × MA_dist|, with floor so tiny signals are visible
        strength = abs(ratio * ma_dist)
        size     = max(50, min(600, strength * 10 + 50))

        pts.append({
            "x":           ma_dist,
            "y":           ratio,
            "color":       color,
            "label":       r.ticker.split(".")[0],
            "size":        size,
            "is_inf":      is_inf,
            "is_critical": is_critical,
            "strength":    strength,
        })

    if not pts:
        logger.info("[VISUALIZER] No plottable data — skipping alpha quadrant.")
        return None

    _ensure_plots_dir()

    xs     = [p["x"]     for p in pts]
    ys     = [p["y"]     for p in pts]
    colors = [p["color"] for p in pts]
    sizes  = [p["size"]  for p in pts]
    infs   = [p["is_inf"] for p in pts]

    x_pad = max(abs(min(xs)), abs(max(xs))) * 0.18 + 4
    y_pad = 0.35
    # Always extend limits to include circuit-breaker thresholds so zones are visible
    x_lo = min(min(xs) - x_pad, -23.0)
    x_hi = max(max(xs) + x_pad,  60.0)
    y_lo, y_hi = max(0.0, min(ys) - 0.2), _INF_CAP + y_pad

    fig, ax = plt.subplots(figsize=(11, 7.5))
    fig.patch.set_facecolor(_BLACK)
    _bloomberg_axes(ax)

    # ── Quadrant fills ────────────────────────────────────────────────────────
    ax.fill_betweenx([y_lo, 1.0], x_lo, 0.0, color=_Q_BUY,  zorder=0)   # BL — Buy
    ax.fill_betweenx([y_lo, 1.0], 0.0, x_hi, color=_Q_MOM,  zorder=0)   # BR — Momentum
    ax.fill_betweenx([1.0, y_hi], 0.0, x_hi, color=_Q_EXIT, zorder=0)   # TR — Exit
    ax.fill_betweenx([1.0, y_hi], x_lo, 0.0, color=_Q_HOLD, zorder=0)   # TL — Hold

    # ── Quadrant divider lines ────────────────────────────────────────────────
    ax.axvline(0.0, color=_DIM, lw=1.0, zorder=2)
    ax.axhline(1.0, color=_DIM, lw=1.0, zorder=2)

    # ── Circuit Breaker danger zones ──────────────────────────────────────────
    ax.axvspan(x_lo, -20.0, alpha=0.15, color="blue", zorder=1.5,
               label="BROKEN TREND: <-20% MA50")
    ax.axvspan(25.0, x_hi,  alpha=0.15, color="red",  zorder=1.5,
               label="CRITICAL: >25% MA50")
    # Threshold boundary markers
    ax.axvline(-20.0, color=_NEON_BLUE, lw=0.9, linestyle="--", alpha=0.45, zorder=2)
    ax.axvline( 25.0, color=_NEON_RED,  lw=0.9, linestyle="--", alpha=0.45, zorder=2)
    # Zone labels
    ax.text(-20.5, y_hi - 0.08, "CB −20%", ha="right", va="top",
            fontsize=7, color=_NEON_BLUE, alpha=0.7, zorder=3)
    ax.text( 25.5, y_hi - 0.08, "CB +25%", ha="left",  va="top",
            fontsize=7, color=_NEON_RED,  alpha=0.7, zorder=3)

    # ── Cap-line annotation ───────────────────────────────────────────────────
    ax.axhline(_INF_CAP, color=_DIM, lw=0.6, linestyle=":", zorder=2)
    ax.text(x_hi - 0.3, _INF_CAP + 0.06, "∞ cap",
            fontsize=7, color=_SILVER, ha="right", va="bottom")

    # ── Quadrant labels ───────────────────────────────────────────────────────
    q_kw = dict(fontsize=9, fontweight="bold", alpha=0.35, color=_WHITE,
                ha="center", va="center", zorder=1)
    x_ml = (x_lo + 0) / 2
    x_mr = (0 + x_hi) / 2
    y_mb = (y_lo + 1.0) / 2
    y_mt = (1.0 + y_hi) / 2
    ax.text(x_ml, y_mb, "BUY ZONE",         **q_kw)
    ax.text(x_mr, y_mb, "STRONG\nMOMENTUM", **q_kw)
    ax.text(x_mr, y_mt, "EXIT / TRIM",      **q_kw)
    ax.text(x_ml, y_mt, "WATCH / HOLD",     **q_kw)

    # ── Scatter (circles for normal, triangles for inf-capped) ───────────────
    reg_idx = [i for i, f in enumerate(infs) if not f]
    inf_idx = [i for i, f in enumerate(infs) if f]

    if reg_idx:
        ax.scatter(
            [xs[i] for i in reg_idx], [ys[i] for i in reg_idx],
            s=[sizes[i] for i in reg_idx],
            c=[colors[i] for i in reg_idx],
            marker="o", zorder=4,
            edgecolors=_BLACK, linewidths=0.8,
        )
    if inf_idx:
        ax.scatter(
            [xs[i] for i in inf_idx], [ys[i] for i in inf_idx],
            s=[sizes[i] for i in inf_idx],
            c=[colors[i] for i in inf_idx],
            marker="^", zorder=4,
            edgecolors=_BLACK, linewidths=0.8,
        )

    # ── Discovery targets — gold stars ────────────────────────────────────────
    disc_pts: list[dict] = []
    for r in (discovery or []):
        if r.current_price is None or r.ma50 is None or r.ma50 == 0:
            continue
        if r.valuation_model == "PEG" and r.modified_peg is not None:
            raw = r.modified_peg
        elif r.valuation_model == "PS" and r.ps_growth_ratio is not None:
            raw = r.ps_growth_ratio
        else:
            continue
        if raw < 0:
            continue
        ratio   = _INF_CAP if raw == float("inf") else min(raw, _INF_CAP)
        ma_dist = (r.current_price - r.ma50) / r.ma50 * 100
        disc_pts.append({"x": ma_dist, "y": ratio, "label": r.ticker.split(".")[0]})

    if disc_pts:
        ax.scatter(
            [p["x"] for p in disc_pts], [p["y"] for p in disc_pts],
            s=300, c=_GOLD, marker="*", zorder=6,
            edgecolors=_BLACK, linewidths=0.6,
        )
        for p in disc_pts:
            ax.annotate(
                f"⭐ {p['label']}",
                (p["x"], p["y"]),
                xytext=(8, 6), textcoords="offset points",
                fontsize=8, color=_GOLD, fontweight="bold", zorder=7,
            )

    # ── Arrow to most critical ticker ─────────────────────────────────────────
    # Prefer: highest (ratio, ma_dist > 0) pair — overvalued AND above MA
    arrow_pt = max(
        (p for p in pts if p["is_critical"]),
        key=lambda p: p["strength"],
        default=None,
    )
    if arrow_pt is None:
        arrow_pt = max(pts, key=lambda p: p["strength"])

    ax.annotate(
        f"  ← {arrow_pt['label']}",
        xy=(arrow_pt["x"], arrow_pt["y"]),
        xytext=(
            arrow_pt["x"] + (8 if arrow_pt["x"] < 0 else -8),
            arrow_pt["y"] + 0.55,
        ),
        fontsize=9, color=_NEON_RED, fontweight="bold",
        arrowprops=dict(
            arrowstyle="-|>",
            color=_NEON_RED,
            lw=1.4,
            mutation_scale=12,
        ),
        zorder=8,
    )

    # ── Ticker labels (quadrant-aware offset, simple overlap avoidance) ───────
    # Track placed label y-positions to avoid stacking
    placed: list[tuple[float, float]] = []
    MIN_DIST = 0.32   # minimum y-gap between adjacent labels

    for p in sorted(pts, key=lambda q: -q["y"]):
        x, y = p["x"], p["y"]
        # Skip the arrow-annotated ticker (already labelled)
        if p is arrow_pt:
            continue

        dx = 8  if x >= 0 else -50   # right half → label right; left half → label left
        dy = 5

        # Bump down if another label is too close vertically
        for _, py in placed:
            if abs(y + dy / 72 - py) < MIN_DIST:
                dy -= 14

        lbl = f"{p['label']} ∞" if p["is_inf"] else p["label"]
        ax.annotate(
            lbl, (x, y),
            xytext=(dx, dy), textcoords="offset points",
            fontsize=9, color=_WHITE, alpha=0.85,
            zorder=7,
        )
        placed.append((x, y + dy / 72))

    # ── Axes styling ──────────────────────────────────────────────────────────
    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(y_lo, y_hi)
    ax.set_xlabel("Distance to 50-day MA  (%)", color=_WHITE, fontsize=10)
    ax.set_ylabel(
        f"Modified PEG  /  P/S Growth Ratio  (cap {_INF_CAP:.0f}×)",
        color=_WHITE, fontsize=10,
    )
    ax.set_title("Alpha Quadrant — Valuation vs. Momentum",
                 color=_WHITE, fontsize=13, pad=10, fontweight="bold")
    ax.xaxis.set_major_formatter(mticker.FormatStrFormatter("%+.0f%%"))

    legend_items = [
        mpatches.Patch(color=_NEON_RED,   label="CRITICAL  (reduce / trend break)"),
        mpatches.Patch(color=_NEON_GREEN, label="ALPHA  (add on momentum)"),
        mpatches.Patch(color=_SILVER,     label="WATCH"),
        plt.Line2D([0], [0], marker="^", color="none",
                   markerfacecolor=_WHITE, markersize=8, label="∞ PEG  (no earnings growth)"),
        mpatches.Patch(facecolor="red",  alpha=0.25, edgecolor="none",
                       label="CRITICAL: >25% MA50"),
        mpatches.Patch(facecolor="blue", alpha=0.25, edgecolor="none",
                       label="BROKEN TREND: <-20% MA50"),
    ]
    if disc_pts:
        legend_items.append(
            plt.Line2D([0], [0], marker="*", color="none",
                       markerfacecolor=_GOLD, markersize=12, label="⭐ DISCOVERY  (alpha candidate)"),
        )
    ax.legend(
        handles=legend_items, fontsize=8,
        facecolor=_BLACK, labelcolor=_WHITE, edgecolor=_DIM, framealpha=0.9,
    )

    plt.tight_layout(pad=1.2)
    out = PLOTS_DIR / "alpha_quadrant.png"
    try:
        fig.savefig(out, dpi=140, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        logger.info("[VISUALIZER] Alpha quadrant → %s  (%d tickers)", out, len(pts))
    except Exception as exc:
        logger.error("[VISUALIZER] Failed to save alpha quadrant: %s", exc)
        out = None  # type: ignore[assignment]
    finally:
        plt.close(fig)
    return out
