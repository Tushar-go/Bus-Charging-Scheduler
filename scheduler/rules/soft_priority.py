"""Soft rule: priority — let operationally important buses jump the queue."""
from __future__ import annotations

from scheduler.context import DecisionContext
from scheduler.registry import soft_rule

# Priority tag -> benefit. "high" gets the largest pull; "low" is actively
# de-prioritised. Unknown tags fall back to neutral (0.0) so a typo is inert.
_PRIORITY_BENEFIT = {
    "high": 1.0,
    "medium": 0.5,
    "normal": 0.0,
    "low": -0.5,
}


@soft_rule("priority")
def priority(ctx: DecisionContext) -> float:
    """Prefer buses tagged with a higher ``priority`` attribute.

    Reads ``ctx.candidate.attrs.get("priority", "normal")`` (case-insensitive)
    and maps it to a benefit: ``high`` -> 1.0, ``medium`` -> 0.5,
    ``normal`` -> 0.0, ``low`` -> -0.5; any unrecognised value is treated as
    0.0 so a mistyped tag simply has no effect. With a large ``priority`` weight
    this term dominates the weighted sum, so tagged buses jump the queue ahead of
    their rivals — while every hard rule (range, capacity, route order, charge
    duration) is still enforced, so a priority bus can never charge somewhere
    infeasible. Priority lives entirely in bus DATA, never in engine code.
    """
    level = str(ctx.candidate.attrs.get("priority", "normal")).lower()
    return _PRIORITY_BENEFIT.get(level, 0.0)
