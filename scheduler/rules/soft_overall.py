"""Soft rule: overall — total time across the whole network should be low."""
from __future__ import annotations

from scheduler.context import DecisionContext
from scheduler.registry import soft_rule


@soft_rule("overall")
def overall(ctx: DecisionContext) -> float:
    """Shortest-remaining-time-first: serve the bus closest to finishing.

    Dispatching near-finishers first lowers total network completion time (a
    classic SPT heuristic for sum-of-completion-times). We rank by each waiting
    bus's optimistic remaining arrival and give the earliest-finisher the
    highest benefit, normalised across the current queue to [0, 1].
    """
    arrivals = {b.id: ctx.projected_arrival(b.id) for b in ctx.queue}
    lo, hi = min(arrivals.values()), max(arrivals.values())
    span = (hi - lo) or 1.0
    return 1.0 - (arrivals[ctx.candidate.id] - lo) / span
