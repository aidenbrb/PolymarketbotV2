"""Pure markout-based performance aggregation by market "family" -- no
network calls, no client, no side effects. See live/RUNBOOK.md's most recent
risk-tightening section.

Groups fills.json records by a coarse market-type-family heuristic (corners,
home-run props, first-half props, ...) so realized behavior can be compared
across families -- a bot can look profitable in aggregate while one specific
family (e.g. stat props on in-play events) is quietly bleeding.

Uses markout_1m_cents/markout_5m_cents (mark-to-market at a fixed post-fill
delay), NOT realized P&L -- fills.json has no per-fill realized P&L field,
and attributing realized gains/losses back to individual fills would need
new FIFO/weighted-average cost-lot tracking that doesn't exist anywhere in
this codebase today. Markout is already the bot's own trusted adverse-
selection proxy (see toxicity_tracker.py), so reusing it here needs no new
plumbing and is directionally the right signal: a family with consistently
negative markout is systematically giving back edge after the fill, which is
exactly the "bleeding" this report exists to surface.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Optional

# Ordered, most-specific-first. Exact hyphen-token matches against the
# lowercased slug (not raw substring search) to avoid false positives from
# unrelated tokens. Independently checked against every real slug this bot
# has actually quoted (144 fills.json / 117 orders.json records, 31/117+
# distinct slugs) -- see live/RUNBOOK.md. Coarse and best-effort, same
# spirit as event_exposure.py::derive_event_bucket_key: a slug that matches
# no rule here falls back to its market-type-code prefix token, or "other"
# if unparseable -- never raises, never blocks anything.
_COMPOSITE_RULES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"fh", "exact"}), "half_exact_score_prop"),
    (frozenset({"sh", "exact"}), "half_exact_score_prop"),
)
_TOKEN_RULES: tuple[tuple[str, str], ...] = (
    ("temp", "weather"),
    ("hr", "home_run_prop"),
    ("exact", "exact_score_prop"),
    ("cor", "corners"),
    ("yrfi", "first_inning_run_prop"),
    ("nrfi", "first_inning_run_prop"),
    ("f5", "first_five_innings"),
    ("ttg", "time_to_goal_prop"),
    ("fh", "team_to_score_prop"),
    ("sh", "team_to_score_prop"),
    ("ftts", "team_to_score_prop"),
    ("cy", "award_futures"),
    ("mvp", "award_futures"),
    ("champ", "championship_futures"),
    ("quarterq", "qualifier"),
    ("stgelim", "qualifier"),
    ("qual", "qualifier"),
    ("w", "futures_winner"),
)


def classify_family(slug: str) -> str:
    """Coarse, best-effort market-family classifier -- see module docstring.
    Empty/unparseable slug -> "other", never raises."""
    if not slug:
        return "other"
    tokens = slug.lower().split("-")
    token_set = frozenset(tokens)

    for required_tokens, label in _COMPOSITE_RULES:
        if required_tokens <= token_set:
            return label
    for token, label in _TOKEN_RULES:
        if token in token_set:
            return label
    return tokens[0] if tokens[0] else "other"


@dataclass
class FamilyPerformance:
    family: str
    fill_count: int
    avg_markout_1m_cents: Optional[float]
    median_markout_1m_cents: Optional[float]
    avg_markout_5m_cents: Optional[float]
    median_markout_5m_cents: Optional[float]
    favorable_count: int
    adverse_count: int
    neutral_count: int
    unresolved_count: int


def _resolved_values(fills: list[dict[str, Any]], key: str) -> list[float]:
    """Only real numeric markout values -- excludes both "not due yet"
    (value and its _computed_at sibling both None) and "resolved to stale"
    (value None, _computed_at set), which fills.py's own convention
    distinguishes but which are equally uninformative for this report."""
    values = []
    for fill in fills:
        value = fill.get(key)
        if value is not None:
            values.append(float(value))
    return values


def compute_family_performance(fills: list[dict[str, Any]]) -> list[FamilyPerformance]:
    """Groups fills by classify_family(market_slug). Empty input -> []."""
    by_family: dict[str, list[dict[str, Any]]] = {}
    for fill in fills:
        family = classify_family(fill.get("market_slug") or "")
        by_family.setdefault(family, []).append(fill)

    performances = []
    for family, family_fills in by_family.items():
        markout_1m = _resolved_values(family_fills, "markout_1m_cents")
        markout_5m = _resolved_values(family_fills, "markout_5m_cents")
        quality_counts = {"favorable": 0, "adverse": 0, "neutral": 0}
        unresolved_count = 0
        for fill in family_fills:
            quality = fill.get("fill_quality")
            if quality in quality_counts:
                quality_counts[quality] += 1
            else:
                unresolved_count += 1

        performances.append(FamilyPerformance(
            family=family,
            fill_count=len(family_fills),
            avg_markout_1m_cents=statistics.mean(markout_1m) if markout_1m else None,
            median_markout_1m_cents=statistics.median(markout_1m) if markout_1m else None,
            avg_markout_5m_cents=statistics.mean(markout_5m) if markout_5m else None,
            median_markout_5m_cents=statistics.median(markout_5m) if markout_5m else None,
            favorable_count=quality_counts["favorable"],
            adverse_count=quality_counts["adverse"],
            neutral_count=quality_counts["neutral"],
            unresolved_count=unresolved_count,
        ))
    return performances
