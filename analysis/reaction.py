"""Plot the market maker's implied-volatility convergence after news events."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping


def _observations(records: Iterable[Mapping[str, Any]]) -> dict[int, list[float]]:
    """Group mean absolute fair-IV gaps by ticks since the newest news.

    Each decision log contains the fair remaining volatility and the midpoint
    implied volatility for every valid listed option.  The gap is a proxy for
    how much public volatility information the market maker has not yet priced.

    :param records: Decoded JSONL decision records.
    :returns: Absolute implied-volatility gaps grouped by news age in ticks.
    """

    grouped: dict[int, list[float]] = defaultdict(list)
    for record in records:
        forecast = record.get("forecast")
        age = record.get("time_since_latest_news")
        if not isinstance(forecast, Mapping) or not isinstance(age, int):
            continue
        sigma = forecast.get("sigma")
        if not isinstance(sigma, (int, float)):
            continue
        gaps = [abs(float(option["market_iv"]) - float(sigma)) for option in record.get("options", [])
                if isinstance(option, Mapping) and isinstance(option.get("market_iv"), (int, float))]
        if gaps:
            grouped[age].append(mean(gaps))
    return dict(grouped)


def trade_observations(records: Iterable[Mapping[str, Any]]) -> dict[int, list[float]]:
    """Group fair-IV gaps by ticks elapsed since the latest straddle signal.

    A decision containing an ``ATM volatility straddle`` desired trade starts a
    new measurement window. Later snapshots reveal whether market IV converges
    after the strategy acted. It is observational: a change cannot prove that
    our trade, rather than other participants or news, caused the response.

    :param records: Decoded JSONL decision records in chronological order.
    :returns: Absolute implied-volatility gaps grouped by elapsed trade ticks.
    """

    grouped: dict[int, list[float]] = defaultdict(list)
    latest_trade_tick: int | None = None
    for record in sorted(records, key=lambda item: int(item.get("tick", -1))):
        tick = record.get("tick")
        if not isinstance(tick, int):
            continue
        selected = record.get("desired_trades", [])
        if any(isinstance(trade, Mapping) and trade.get("reason") == "ATM volatility straddle" for trade in selected):
            latest_trade_tick = tick
        forecast = record.get("forecast")
        if latest_trade_tick is None or not isinstance(forecast, Mapping):
            continue
        sigma = forecast.get("sigma")
        if not isinstance(sigma, (int, float)):
            continue
        gaps = [abs(float(option["market_iv"]) - float(sigma)) for option in record.get("options", [])
                if isinstance(option, Mapping) and isinstance(option.get("market_iv"), (int, float))]
        if gaps:
            grouped[tick - latest_trade_tick].append(mean(gaps))
    return dict(grouped)


def read_decisions(path: str | Path) -> list[dict[str, Any]]:
    """Read only valid volatility-decision records from a JSONL log.

    :param path: Decision-log path written by ``--decision-log``.
    :returns: Decoded volatility-decision records.
    :raises ValueError: If a non-empty line is not valid JSON.
    """

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON on line {line_number}") from error
        if record.get("event") == "volatility_decision":
            records.append(record)
    return records


def _svg_point(index: int, value: float, count: int, maximum: float, width: int, height: int) -> tuple[float, float]:
    """Map one chart coordinate into an SVG plotting rectangle.

    :param index: Horizontal observation index.
    :param value: Vertical value.
    :param count: Number of observation buckets.
    :param maximum: Maximum plotted value.
    :param width: Plotting width in pixels.
    :param height: Plotting height in pixels.
    :returns: SVG x and y coordinates.
    """

    x = 70.0 + (width - 100.0) * index / max(1, count - 1)
    y = 30.0 + (height - 80.0) * (1.0 - value / max(maximum, 0.000001))
    return x, y


def write_svg(grouped: Mapping[int, list[float]], output: str | Path,
              after_trade: Mapping[int, list[float]] | None = None) -> None:
    """Write a standalone convergence chart without third-party dependencies.

    :param grouped: Fair-IV-gap observations grouped by news age.
    :param output: SVG destination path.
    :param after_trade: Optional observations aligned to the latest straddle signal.
    :raises ValueError: If the log has no usable implied-volatility observations.
    """

    if not grouped:
        raise ValueError("No market-IV observations with a news age were found")
    ages = sorted(grouped)
    values = [mean(grouped[age]) for age in ages]
    width, height = 960, 480
    trade_ages = sorted(after_trade or {})
    trade_values = [mean(after_trade[age]) for age in trade_ages] if after_trade else []
    maximum = max([*values, *trade_values]) * 1.1
    all_ages = sorted(set(ages) | set(trade_ages))
    positions = {age: index for index, age in enumerate(all_ages)}
    points = [_svg_point(positions[age], value, len(all_ages), maximum, width, height)
              for age, value in zip(ages, values)]
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    labels = "".join(f'<text x="{_svg_point(index, 0, len(all_ages), maximum, width, height)[0]:.1f}" y="455" text-anchor="middle">{age}</text>'
                     for index, age in enumerate(all_ages))
    circles = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4"/>' for x, y in points)
    trade_points = [_svg_point(positions[age], value, len(all_ages), maximum, width, height)
                    for age, value in zip(trade_ages, trade_values)]
    trade_polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in trade_points)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<style>text{{font:14px sans-serif;fill:#1f2937}} .axis{{stroke:#64748b}} .line{{fill:none;stroke:#2563eb;stroke-width:3}} .trade{{fill:none;stroke:#9333ea;stroke-width:3}} circle{{fill:#2563eb}}</style>
<rect width="100%" height="100%" fill="white"/><text x="70" y="22" font-weight="bold">Market-maker IV convergence after analyst news</text>
<line class="axis" x1="70" y1="400" x2="930" y2="400"/><line class="axis" x1="70" y1="30" x2="70" y2="400"/>
<text x="8" y="42">{maximum:.3f}</text><text x="8" y="403">0.000</text>
<polyline class="line" points="{polyline}"/>{circles}<polyline class="trade" points="{trade_polyline}"/>{labels}
<text x="480" y="475" text-anchor="middle">ticks since each series' anchor event</text>
<text x="76" y="50">mean |market IV − fair remaining volatility|</text>
<text x="680" y="22" fill="#2563eb">blue: after news</text><text x="810" y="22" fill="#9333ea">purple: after straddle signal</text>
</svg>'''
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(svg, encoding="utf-8")


def main() -> None:
    """Render an offline SVG convergence chart from a decision JSONL file."""

    parser = argparse.ArgumentParser(description="Plot market-IV convergence from volatility decision logs.")
    parser.add_argument("input", help="JSONL file created with --decision-log")
    parser.add_argument("--output", default="data/market-maker-reaction.svg", help="SVG chart destination")
    args = parser.parse_args()
    records = read_decisions(args.input)
    write_svg(_observations(records), args.output, trade_observations(records))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
