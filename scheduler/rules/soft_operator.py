"""Soft rule: operator — each operator's fleet should run smoothly as a group."""
from __future__ import annotations

from scheduler.context import DecisionContext
from scheduler.registry import soft_rule


@soft_rule("operator")
def operator(ctx: DecisionContext) -> float:
    """Balance charging service across operators (round-robin fairness).

    "Each operator's fleet should run smoothly as a group" — we read that as:
    no operator should be systematically served before the others. So we
    prioritise the candidate whose operator has so far received the **least**
    charging service across the whole network, relative to the busiest operator.

    Crucially this signal is computed from *global, fleet-level* state (total
    charges granted per operator so far), **not** from the candidate's own wait.
    That decouples it from the ``individual`` rule, so the two genuinely
    disagree on a mixed-operator queue — which is exactly what makes tuning the
    operator weight produce a *visibly different* schedule (the Scenario 4 test:
    with KPN dominating the fleet, a higher operator weight pulls the schedule
    toward evening out service across KPN / Freshbus / Flixbus rather than
    strict longest-wait-first).

    Returns a benefit in [0, 1]: the least-served operator scores ~1.0 (serve
    next), the busiest scores ~0.0. Before any charge has happened every
    operator is equal, so the rule is neutral (0.0) and ties fall through to the
    deterministic tie-break.
    """
    served: dict[str, int] = {}
    for state in ctx.bus_states.values():
        served[state.bus.operator] = served.get(state.bus.operator, 0) + state.num_charges
    busiest = max(served.values(), default=0)
    if busiest <= 0:
        return 0.0  # no service granted yet -> neutral, let tie-breaks decide
    mine = served.get(ctx.candidate.operator, 0)
    return 1.0 - mine / busiest
