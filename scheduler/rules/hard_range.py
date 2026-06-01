"""Hard rule: range — a bus must never travel beyond its battery range between charges."""
from __future__ import annotations

from scheduler.context import DecisionContext
from scheduler.registry import hard_rule


@hard_rule("range", reason="bus would exceed battery range between charges")
def range_ok(ctx: DecisionContext) -> bool:
    """Veto a candidate that has already overshot its battery range.

    Feasible iff the candidate's live ``range_left`` is non-negative (within a
    tiny float tolerance). Starting a charge here always tops the battery back
    up, so *beginning* a charge is itself range-safe; the binding guarantee that
    a bus reaches its next charge is the station-choice policy, and this guard
    is the explicit, greppable assertion that the policy held — if a bus ever
    arrives with ``range_left < 0`` the schedule is infeasible.
    """
    return ctx.bus_states[ctx.candidate.id].range_left >= -1e-6
