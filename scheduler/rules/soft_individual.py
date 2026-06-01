"""Soft rule: individual — no single bus should wait too long."""
from __future__ import annotations

from scheduler.context import DecisionContext
from scheduler.registry import soft_rule

# A wait we treat as "fully bad". 60 min ~= longer than two back-to-back charges.
WAIT_CAP_MIN = 60.0


@soft_rule("individual")
def individual(ctx: DecisionContext) -> float:
    """Prefer the bus that has already waited longest (longest-wait-first).

    Returns a benefit in [0, 1] that rises with the candidate's accrued wait.
    Because it grows monotonically with waiting time, any waiting bus will
    eventually out-score its rivals as long as ``individual`` has a positive
    weight — i.e. this term provides anti-starvation.
    """
    return min(ctx.candidate_wait_so_far, WAIT_CAP_MIN) / WAIT_CAP_MIN
