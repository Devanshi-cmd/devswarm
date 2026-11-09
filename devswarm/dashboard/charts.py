"""Compute SVG geometry for bar charts, so templates only draw what is decided here."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

CHART_WIDTH = 960  # Drawn at about 1:1 on a desktop card, so bars stay 20px thick.
_LABEL_WIDTH = 92
_VALUE_WIDTH = 92
_ROW_HEIGHT = 36
_BAR_HEIGHT = 20
_SEGMENT_GAP = 2
_CORNER_RADIUS = 4
_MIN_VISIBLE_WIDTH = 1.0  # Thinner slivers are skipped; the table view keeps their values.


@dataclass(frozen=True)
class Series:
    key: str
    label: str
    css_class: str


@dataclass(frozen=True)
class Segment:
    css_class: str
    path: str
    tooltip: str


@dataclass(frozen=True)
class Row:
    label: str
    value: str
    top: int
    middle: int
    value_x: float
    tooltip: str
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class BarChart:
    title: str
    width: int
    height: int
    label_right: int
    baseline_x: int
    row_height: int
    legend: tuple[Series, ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class Datum:
    label: str
    values: dict[str, float]


def _bar_path(x: float, y: float, width: float, height: float, *, round_end: bool) -> str:
    """A bar that is square at its start, with rounded corners only on the data end."""
    radius = min(_CORNER_RADIUS, width, height / 2) if round_end else 0
    right = x + width
    bottom = y + height
    if radius == 0:
        return f"M{x:.2f} {y:.2f}H{right:.2f}V{bottom:.2f}H{x:.2f}Z"
    return (
        f"M{x:.2f} {y:.2f}H{right - radius:.2f}"
        f"Q{right:.2f} {y:.2f} {right:.2f} {y + radius:.2f}"
        f"V{bottom - radius:.2f}"
        f"Q{right:.2f} {bottom:.2f} {right - radius:.2f} {bottom:.2f}"
        f"H{x:.2f}Z"
    )


def horizontal_bars(
    title: str,
    series: Sequence[Series],
    data: Sequence[Datum],
    format_value: Callable[[float], str],
) -> BarChart | None:
    """Build a horizontal bar chart, stacked when there is more than one series."""
    totals = [sum(datum.values.get(item.key, 0) for item in series) for datum in data]
    largest = max(totals, default=0)
    if largest <= 0:
        return None

    plot_width = CHART_WIDTH - _LABEL_WIDTH - _VALUE_WIDTH
    rows = []
    for index, (datum, total) in enumerate(zip(data, totals, strict=True)):
        top = index * _ROW_HEIGHT
        bar_y = top + (_ROW_HEIGHT - _BAR_HEIGHT) // 2
        cursor = float(_LABEL_WIDTH)
        drawn: list[tuple[Series, float, float, float]] = []
        for item in series:
            value = datum.values.get(item.key, 0)
            width = value / largest * plot_width
            start = cursor + (_SEGMENT_GAP if drawn else 0)
            visible = width - (_SEGMENT_GAP if drawn else 0)
            cursor += width
            if value > 0 and visible >= _MIN_VISIBLE_WIDTH:
                drawn.append((item, start, visible, value))

        segments = tuple(
            Segment(
                css_class=item.css_class,
                path=_bar_path(start, bar_y, width, _BAR_HEIGHT, round_end=i == len(drawn) - 1),
                tooltip=f"{datum.label}, {item.label}: {format_value(value)}",
            )
            for i, (item, start, width, value) in enumerate(drawn)
        )
        rows.append(
            Row(
                label=datum.label,
                value=format_value(total),
                top=top,
                middle=top + _ROW_HEIGHT // 2,
                value_x=cursor + 8,
                tooltip=f"{datum.label}: {format_value(total)}",
                segments=segments,
            )
        )
    return BarChart(
        title=title,
        width=CHART_WIDTH,
        height=len(rows) * _ROW_HEIGHT,
        label_right=_LABEL_WIDTH - 10,
        baseline_x=_LABEL_WIDTH,
        row_height=_ROW_HEIGHT,
        # One series needs no legend: the chart title already names it.
        legend=tuple(series) if len(series) > 1 else (),
        rows=tuple(rows),
    )
