"""Hard rule: charger_capacity — never assign a charge when no charger slot is free."""
from __future__ import annotations

from scheduler.context import DecisionContext
from scheduler.registry import hard_rule


@hard_rule("charger_capacity", reason="no free charger slot")
def charger_capacity_ok(ctx: DecisionContext) -> bool:
    """Veto a candidate when the station has no free charger slot right now.

    Feasible iff ``ctx.free_slots >= 1``. Capacity is plain data on the node
    (``charger_capacity``), so this rule generalises the one-charger case to any
    number of chargers N without special-casing: doubling the chargers is a
    single number edit in the world, and this guard reads the live free-slot
    count the engine derived from that capacity.
    """
    return ctx.free_slots >= 1
