"""Weekly-last-session rebalance schedule."""
from __future__ import annotations

from collections import defaultdict


def weekly_last_decisions(decisions: list) -> list:
    weeks = defaultdict(list)
    for decision in decisions:
        year, week, _ = decision.isocalendar()
        weeks[(year, week)].append(decision)
    return [max(values) for _, values in sorted(weeks.items())]
