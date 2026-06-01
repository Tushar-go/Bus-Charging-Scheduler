"""Hard rule: charge_duration — every charge must last exactly the configured duration."""
from __future__ import annotations

from scheduler.context import DecisionContext
from scheduler.registry import hard_rule


@hard_rule("charge_duration", reason="charge must equal the configured duration")
def charge_duration_ok(ctx: DecisionContext) -> bool:
    """Veto a charge whose length differs from the station's configured duration.

    Feasible iff ``ctx.charge_duration`` equals the duration the world reports
    for this station (``ctx.world.charge_minutes(ctx.station.id)``, which honours
    any per-station override before falling back to the physics default). The
    engine sets the charge length from exactly that source, so equality holds by
    construction; keeping it as an explicit, greppable hard rule turns that
    invariant into a checkable assertion rather than an unwritten assumption.
    """
    return ctx.charge_duration == ctx.world.charge_minutes(ctx.station.id)
