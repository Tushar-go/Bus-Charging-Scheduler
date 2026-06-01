"""Hard rule: route_order — charges must progress along the route, never backtrack."""
from __future__ import annotations

from scheduler.context import DecisionContext
from scheduler.registry import hard_rule


@hard_rule("route_order", reason="charging here would backtrack")
def route_order_ok(ctx: DecisionContext) -> bool:
    """Veto a charge that would sit behind the candidate's previous charge.

    Feasible iff the station being scored is at-or-ahead, in travel order, of
    the last station this bus charged at. We compare the candidate's
    distance-from-origin to the chosen station
    (``ctx.world.distance(candidate.origin, ctx.station.id)``) against
    ``last_station_pos`` (the distance-from-origin of the previous charge, which
    starts at ``-1.0`` so the first charge always passes), allowing a tiny float
    tolerance. Because position is a single signed corridor axis, this works for
    forward and reverse buses alike and forbids charging at a station already
    behind the bus.
    """
    candidate = ctx.candidate
    here = ctx.world.distance(candidate.origin, ctx.station.id)
    last = ctx.bus_states[candidate.id].last_station_pos
    return here >= last - 1e-6
