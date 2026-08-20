#!/usr/bin/env python
"""Print the measured reliability metrics.

Reads the durable telemetry tables and renders them as a table. Nothing is
estimated and nothing is cached: run it again after more traffic and the
numbers move.

    python -m scripts.report_metrics            # human readable
    python -m scripts.report_metrics --json     # machine readable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.infrastructure.database import dispose_engine, session_scope
from app.observability.metrics import collect_metrics


def _format(report) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("HUDDLE - MEASURED AGENT RELIABILITY METRICS")
    lines.append("=" * 78)

    if not report.counts.get("turns_total"):
        lines.append("")
        lines.append("No recorded turns yet. Drive some traffic through /chat first:")
        lines.append("  docker compose up -d && python -m scripts.simulate")
        lines.append("")

    lines.append("")
    lines.append(f"{'METRIC':<38} {'VALUE':>14}  UNIT")
    lines.append("-" * 78)
    for metric in report.metrics:
        value = (
            f"{metric.value:,.1f}"
            if metric.unit in ("milliseconds", "tokens", "calls")
            else f"{metric.value:.4f}"
        )
        support = (
            f"  ({metric.numerator}/{metric.denominator})"
            if metric.numerator is not None and metric.denominator
            else ""
        )
        lines.append(f"{metric.name:<38} {value:>14}  {metric.unit}{support}")

    lines.append("")
    lines.append("COUNTS")
    lines.append("-" * 78)
    for key, value in sorted(report.counts.items()):
        lines.append(f"{key:<38} {value:>14,}")

    lines.append("")
    lines.append("DEFINITIONS")
    lines.append("-" * 78)
    for metric in report.metrics:
        lines.append(f"{metric.name}:")
        for chunk in _wrap(metric.definition, 74):
            lines.append(f"  {chunk}")
    lines.append("=" * 78)
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    arguments = parser.parse_args()

    try:
        async with session_scope() as session:
            report = await collect_metrics(session)
    except Exception as error:
        print(f"Could not read metrics: {error}", file=sys.stderr)
        print("Is the database up? `docker compose up -d db`", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    if arguments.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(_format(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
