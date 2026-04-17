"""
SPC (Statistical Process Control) for personal biomarkers.

X-mR chart (Individuals + Moving Range) per biomarker.
Western Electric rules for trend/shift detection.
All math in Python — no LLM arithmetic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


@dataclass
class SPCPoint:
    date: date
    value: float
    unit: str
    ref_low: float | None = None
    ref_high: float | None = None


@dataclass
class SPCResult:
    biomarker: str
    n: int
    mean: float  # X-bar (process center)
    ucl: float   # Upper Control Limit (X + 2.66 * mR-bar)
    lcl: float   # Lower Control Limit (X - 2.66 * mR-bar)
    mr_bar: float  # Average Moving Range
    unit: str
    ref_low: float | None = None
    ref_high: float | None = None
    points: list[SPCPoint] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    trend: str = "stable"  # stable/rising/declining/volatile/shift


# X-mR constants
# For individuals chart: D4=3.267, d2=1.128 (subgroup size=2 for moving range)
# UCL = X-bar + 2.66 * mR-bar  (2.66 = 3/d2 = 3/1.128)
# LCL = X-bar - 2.66 * mR-bar
_E2 = 2.66  # 3 / 1.128


def compute_xmr(biomarker: str, points: list[SPCPoint]) -> SPCResult | None:
    """Compute X-mR chart for a single biomarker."""
    if len(points) < 2:
        return None

    values = [p.value for p in points]
    n = len(values)

    # X-bar (mean of individuals)
    x_bar = sum(values) / n

    # Moving ranges
    mrs = [abs(values[i] - values[i - 1]) for i in range(1, n)]
    mr_bar = sum(mrs) / len(mrs) if mrs else 0.0

    # Control limits
    ucl = x_bar + _E2 * mr_bar
    lcl = x_bar - _E2 * mr_bar

    # Use last point's ref and unit
    last = points[-1]

    result = SPCResult(
        biomarker=biomarker,
        n=n,
        mean=round(x_bar, 2),
        ucl=round(ucl, 2),
        lcl=round(lcl, 2),
        mr_bar=round(mr_bar, 2),
        unit=last.unit,
        ref_low=last.ref_low,
        ref_high=last.ref_high,
        points=points,
    )

    # ── Western Electric rules ──
    result.alerts = _check_western_electric(values, x_bar, ucl, lcl, mr_bar)
    result.trend = _classify_trend(values, x_bar, ucl, lcl)

    return result


def _check_western_electric(
    values: list[float], x_bar: float, ucl: float, lcl: float, mr_bar: float,
) -> list[str]:
    """Apply Western Electric / Nelson rules for anomaly detection."""
    alerts: list[str] = []
    n = len(values)
    sigma = mr_bar / 1.128 if mr_bar > 0 else 0.0
    zone_b_upper = x_bar + 2 * sigma
    zone_b_lower = x_bar - 2 * sigma
    zone_c_upper = x_bar + sigma
    zone_c_lower = x_bar - sigma

    # Rule 1: Point beyond UCL/LCL (3σ)
    for i, v in enumerate(values):
        if v > ucl:
            alerts.append(f"Точка {i + 1} выше UCL ({v:.1f} > {ucl:.1f})")
        elif v < lcl:
            alerts.append(f"Точка {i + 1} ниже LCL ({v:.1f} < {lcl:.1f})")

    # Rule 2: 7+ points on same side of center (trend/shift)
    if n >= 7:
        above = 0
        below = 0
        for v in values[-7:]:
            if v > x_bar:
                above += 1
            elif v < x_bar:
                below += 1
        if above >= 7:
            alerts.append(f"7 точек подряд выше среднего — вероятный сдвиг вверх")
        elif below >= 7:
            alerts.append(f"7 точек подряд ниже среднего — вероятный сдвиг вниз")

    # Rule 3: 6+ points trending in one direction
    if n >= 6:
        rising = all(values[i] > values[i - 1] for i in range(n - 5, n))
        falling = all(values[i] < values[i - 1] for i in range(n - 5, n))
        if rising:
            alerts.append(f"6 точек подряд растут — устойчивый восходящий тренд")
        elif falling:
            alerts.append(f"6 точек подряд падают — устойчивый нисходящий тренд")

    # Rule 4: 2 out of 3 beyond 2σ (same side)
    if n >= 3:
        last3 = values[-3:]
        above_2s = sum(1 for v in last3 if v > zone_b_upper)
        below_2s = sum(1 for v in last3 if v < zone_b_lower)
        if above_2s >= 2:
            alerts.append(f"2 из 3 последних точек выше 2σ — возможный выброс")
        if below_2s >= 2:
            alerts.append(f"2 из 3 последних точек ниже 2σ — возможный выброс")

    # Rule 5: Last point vs previous — big jump
    if n >= 2 and mr_bar > 0:
        last_mr = abs(values[-1] - values[-2])
        if last_mr > 3.27 * mr_bar:  # D4 * mR-bar
            alerts.append(
                f"Резкое изменение: {values[-2]:.1f} → {values[-1]:.1f} "
                f"(скачок {last_mr:.1f}, лимит {3.27 * mr_bar:.1f})")

    return alerts


def _classify_trend(
    values: list[float], x_bar: float, ucl: float, lcl: float,
) -> str:
    """Classify overall trend."""
    n = len(values)
    if n < 3:
        return "insufficient_data"

    # Simple linear direction of last points
    last_half = values[n // 2:]
    first_half = values[:n // 2] or values[:1]
    avg_first = sum(first_half) / len(first_half)
    avg_last = sum(last_half) / len(last_half)

    diff_pct = ((avg_last - avg_first) / avg_first * 100) if avg_first != 0 else 0

    # Check if last point is outside control limits
    last_v = values[-1]
    if last_v > ucl or last_v < lcl:
        if diff_pct > 5:
            return "rising_alert"
        elif diff_pct < -5:
            return "declining_alert"
        return "volatile"

    if diff_pct > 10:
        return "rising"
    elif diff_pct < -10:
        return "declining"
    elif abs(diff_pct) <= 5:
        return "stable"
    elif diff_pct > 0:
        return "slightly_rising"
    else:
        return "slightly_declining"


def format_spc_report(results: list[SPCResult]) -> str:
    """Format SPC results for Telegram."""
    if not results:
        return "Недостаточно данных для SPC-анализа (нужно ≥2 измерения одного показателя)."

    trend_icons = {
        "stable": "✅", "rising": "📈", "declining": "📉",
        "rising_alert": "🔴📈", "declining_alert": "🔴📉",
        "volatile": "⚡", "slightly_rising": "↗️",
        "slightly_declining": "↘️", "insufficient_data": "❓",
    }

    lines = ["<b>SPC-анализ биомаркеров</b>\n"]

    # Group: with alerts first
    with_alerts = [r for r in results if r.alerts]
    without_alerts = [r for r in results if not r.alerts]

    if with_alerts:
        lines.append("<b>Требуют внимания:</b>\n")
        for r in with_alerts:
            icon = trend_icons.get(r.trend, "•")
            lines.append(f"{icon} <b>{r.biomarker}</b> ({r.n} точек)")
            lines.append(f"  Среднее: {r.mean} {r.unit}")
            lines.append(f"  Границы: LCL={r.lcl} — UCL={r.ucl}")
            if r.ref_low is not None or r.ref_high is not None:
                lines.append(f"  Лаб.норма: {r.ref_low or '?'}–{r.ref_high or '?'}")
            vals = " → ".join(f"{p.value}" for p in r.points)
            lines.append(f"  Динамика: {vals}")
            for a in r.alerts:
                lines.append(f"  ⚠️ {a}")
            lines.append("")

    if without_alerts:
        lines.append("<b>Стабильные:</b>\n")
        for r in without_alerts:
            icon = trend_icons.get(r.trend, "•")
            vals = " → ".join(f"{p.value}" for p in r.points)
            lines.append(f"{icon} {r.biomarker}: {vals} {r.unit} (mean={r.mean})")

    return "\n".join(lines)


def format_spc_for_profile(results: list[SPCResult]) -> str:
    """Compact SPC summary for Health Profile prompt."""
    if not results:
        return ""

    lines = ["SPC ANALYSIS (Statistical Process Control):"]
    for r in results:
        line = (f"  {r.biomarker}: n={r.n}, mean={r.mean}, "
                f"UCL={r.ucl}, LCL={r.lcl}, trend={r.trend}")
        if r.alerts:
            line += f" ALERTS: {'; '.join(r.alerts)}"
        lines.append(line)
    return "\n".join(lines)
