"""Generate trend charts as PNG for Telegram."""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def render_trend_chart(
    biomarker: str,
    dates: list[date],
    values: list[float],
    unit: str,
    ref_low: float | None = None,
    ref_high: float | None = None,
    ucl: float | None = None,
    lcl: float | None = None,
    mean: float | None = None,
) -> bytes:
    """Render a trend chart and return PNG bytes."""
    fig, ax = plt.subplots(figsize=(8, 4))

    # Reference range (lab norms) — green zone
    if ref_low is not None and ref_high is not None:
        ax.axhspan(ref_low, ref_high, alpha=0.15, color="green", label="Норма")
    elif ref_high is not None:
        ax.axhline(ref_high, color="green", linestyle="--", alpha=0.5, label=f"Верхняя норма ({ref_high})")
    elif ref_low is not None:
        ax.axhline(ref_low, color="green", linestyle="--", alpha=0.5, label=f"Нижняя норма ({ref_low})")

    # SPC control limits — dashed orange
    if ucl is not None:
        ax.axhline(ucl, color="orange", linestyle=":", alpha=0.6, label=f"UCL ({ucl:.1f})")
    if lcl is not None and lcl > 0:
        ax.axhline(lcl, color="orange", linestyle=":", alpha=0.6, label=f"LCL ({lcl:.1f})")
    if mean is not None:
        ax.axhline(mean, color="gray", linestyle="-", alpha=0.3, label=f"Среднее ({mean:.1f})")

    # Data points
    colors = []
    for v in values:
        if ref_low is not None and v < ref_low:
            colors.append("red")
        elif ref_high is not None and v > ref_high:
            colors.append("red")
        else:
            colors.append("#2196F3")

    ax.plot(dates, values, "-o", color="#2196F3", linewidth=2, markersize=8, zorder=5)

    # Color abnormal points red
    for d, v, c in zip(dates, values, colors):
        if c == "red":
            ax.plot(d, v, "o", color="red", markersize=10, zorder=6)

    # Value labels
    for d, v in zip(dates, values):
        ax.annotate(f"{v:.1f}", (d, v), textcoords="offset points",
                    xytext=(0, 12), ha="center", fontsize=9, fontweight="bold")

    ax.set_title(f"{biomarker} ({unit})", fontsize=14, fontweight="bold")
    ax.set_ylabel(unit)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.%y"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
