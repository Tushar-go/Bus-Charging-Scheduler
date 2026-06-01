"""Station policies — *which* stations a bus charges at (not *when*).

A :class:`StationPolicy` answers two questions for a single bus:

* ``feasible_plans`` — every charge-station-id set (in travel order) that lets
  the bus complete its trip without any single leg exceeding the battery range.
* ``choose`` — the one plan the engine will actually run.

This module is deliberately ignorant of *time* and *queues*: it only reasons
about geometry (distances) and range. The discrete-event engine consumes the
chosen plan and decides the timing/ordering of charges.

Two policies ship today:

``MaxReachStationPolicy`` (the default)
    A "charge as late as feasible" greedy: from the origin, repeatedly hop to
    the *farthest* station still within battery range, stopping once the
    destination is reachable. Minimises the number of charges and pushes them
    as far down the corridor as possible. For the Bengaluru->Kochi corridor
    (segments 100/120/100/120/100 km, range 240 km) this yields ``['B', 'D']``
    for the forward bus and ``['C', 'A']`` for the reverse bus.

``LoadAwareStationPolicy``
    Among the *minimum-cardinality* feasible plans it picks the one that
    spreads predicted demand best, where each station's predicted demand is the
    number of fleet buses whose greedy (max-reach) plan would use that station.
    With no scenario it degrades gracefully to max-reach behaviour.

Determinism is a hard requirement everywhere: enumeration order, tie-breaks and
the returned lists are all stable for identical inputs.
"""
from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from scheduler.models import Bus, Scenario, World


# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #
class InfeasibleRouteError(Exception):
    """Raised when a bus cannot reach its destination under the battery range.

    No combination of the corridor's stations produces a route whose every leg
    is within ``world.physics.battery_range_km``.
    """


# --------------------------------------------------------------------------- #
# Protocol                                                                    #
# --------------------------------------------------------------------------- #
@runtime_checkable
class StationPolicy(Protocol):
    """Structural contract for a station-selection policy.

    Any object exposing these two methods is a valid policy — no base class to
    inherit (matches the rule-protocol style used elsewhere in the package).
    """

    def feasible_plans(self, bus: Bus, world: World) -> List[List[str]]:
        """All feasible charge-station-id sets, each in travel order."""
        ...

    def choose(self, bus: Bus, world: World) -> List[str]:
        """The single chosen charge-station-id set, in travel order."""
        ...


# --------------------------------------------------------------------------- #
# Shared geometry helpers (pure functions over the World)                     #
# --------------------------------------------------------------------------- #
def _legs_within_range(
    charge_dists: List[float], trip_km: float, battery_range_km: float
) -> bool:
    """True iff every leg of a route is within the battery range.

    ``charge_dists`` are the distances-from-origin of the chosen charge stops,
    already sorted ascending. The legs are::

        origin -> first stop, stop_i -> stop_{i+1}, ..., last stop -> destination

    With an empty ``charge_dists`` the single leg is ``origin -> destination``.
    """
    prev = 0.0
    for d in charge_dists:
        if d - prev > battery_range_km:
            return False
        prev = d
    return (trip_km - prev) <= battery_range_km


def enumerate_feasible_plans(bus: Bus, world: World) -> List[List[str]]:
    """Every feasible charge-station-id set for ``bus``, in travel order.

    Backtracking over the corridor's stations (a handful at most). A plan is
    feasible iff every leg is within ``world.physics.battery_range_km``. Results
    are ordered deterministically: by plan length, then by the tuple of
    distances (which equals travel order), so the smallest/earliest plans come
    first.
    """
    origin, destination = bus.origin, bus.destination
    trip_km = world.distance(origin, destination)
    battery = float(world.physics.battery_range_km)

    # Stations between origin and destination, in the order the bus reaches
    # them, each with its distance-from-origin. This is the only ordering we
    # ever use, so plans are intrinsically in travel order.
    stations = world.stations_in_travel_order(origin, destination)

    plans: List[List[str]] = []

    def backtrack(start_idx: int, chosen_ids: List[str], chosen_dists: List[float]) -> None:
        # Record this prefix as a plan if the *remaining* leg to the destination
        # is within range (every earlier leg was checked before we recursed).
        last = chosen_dists[-1] if chosen_dists else 0.0
        if (trip_km - last) <= battery:
            plans.append(list(chosen_ids))
        # Try appending each later station as the next charge stop, but only if
        # the leg from the current position to it is within range.
        for i in range(start_idx, len(stations)):
            sid, dist = stations[i]
            if dist - last <= battery:
                chosen_ids.append(sid)
                chosen_dists.append(dist)
                backtrack(i + 1, chosen_ids, chosen_dists)
                chosen_ids.pop()
                chosen_dists.pop()

    backtrack(0, [], [])

    # Deterministic order: fewer charges first, then by travel-order distances.
    dist_of = {sid: d for sid, d in stations}
    plans.sort(key=lambda p: (len(p), tuple(dist_of[s] for s in p)))
    return plans


def greedy_max_reach(bus: Bus, world: World) -> List[str]:
    """The "charge as late as feasible" greedy plan, in travel order.

    From the origin, repeatedly select the farthest station still within
    battery range of the current position; stop once the destination is within
    range. Raises :class:`InfeasibleRouteError` if a gap cannot be bridged.
    """
    origin, destination = bus.origin, bus.destination
    trip_km = world.distance(origin, destination)
    battery = float(world.physics.battery_range_km)
    stations = world.stations_in_travel_order(origin, destination)

    chosen: List[str] = []
    cur = 0.0  # distance-from-origin of the current position
    while (trip_km - cur) > battery:
        # Stations strictly ahead of us and within one charge-free hop.
        reachable = [(sid, d) for sid, d in stations if d > cur and (d - cur) <= battery]
        if not reachable:
            raise InfeasibleRouteError(
                f"bus {bus.id!r} cannot reach {destination!r} from position "
                f"{cur:.1f} km: no station within range {battery:.1f} km "
                f"(remaining {trip_km - cur:.1f} km)"
            )
        # Farthest reachable station == "charge as late as feasible".
        sid, d = reachable[-1]
        chosen.append(sid)
        cur = d
    return chosen


# --------------------------------------------------------------------------- #
# Policies                                                                    #
# --------------------------------------------------------------------------- #
class MaxReachStationPolicy:
    """Default policy: minimise charges by charging as late as feasible."""

    name = "max_reach"

    def feasible_plans(self, bus: Bus, world: World) -> List[List[str]]:
        plans = enumerate_feasible_plans(bus, world)
        if not plans:
            raise InfeasibleRouteError(
                f"bus {bus.id!r}: no feasible charge plan from {bus.origin!r} "
                f"to {bus.destination!r} under range "
                f"{world.physics.battery_range_km:.1f} km"
            )
        return plans

    def choose(self, bus: Bus, world: World) -> List[str]:
        # Use the greedy directly (it also raises InfeasibleRouteError on a gap).
        return greedy_max_reach(bus, world)


class LoadAwareStationPolicy:
    """Spread demand across stations among the cheapest feasible plans.

    Selection procedure for a bus:

    1. Enumerate all feasible plans; keep only those of minimum cardinality
       (fewest charges — same cost dimension max-reach optimises).
    2. Score each candidate by predicted load: the sum, over its stations, of a
       fleet-wide demand estimate. Demand for a station = the number of buses in
       the scenario whose *max-reach* plan uses that station.
    3. Pick the lowest-scoring plan; break ties by the plan's station-id tuple
       (lexicographic) for determinism.

    With no scenario (``scenario is None``) there is no fleet to estimate demand
    from, so it falls back to the plain max-reach choice.
    """

    name = "load_aware"

    def __init__(self, scenario: Optional[Scenario] = None) -> None:
        self.scenario = scenario
        # Cache of station_id -> predicted demand, computed lazily per world so
        # that repeated ``choose`` calls during a solve stay O(1).
        self._demand: Optional[dict] = None
        self._demand_world_id: Optional[str] = None

    # -- demand model ------------------------------------------------------ #
    def _demand_for(self, world: World) -> dict:
        """Predicted per-station demand from the scenario's fleet (cached).

        Each bus contributes +1 to every station on its max-reach plan. Buses
        whose route is infeasible contribute nothing (they cannot run).
        """
        if self._demand is not None and self._demand_world_id == world.world_id:
            return self._demand
        demand: dict = {sid: 0 for sid in world.station_ids()}
        if self.scenario is not None:
            for other in self.scenario.buses:
                try:
                    for sid in greedy_max_reach(other, world):
                        demand[sid] = demand.get(sid, 0) + 1
                except InfeasibleRouteError:
                    # An infeasible bus adds no load; the engine surfaces its
                    # infeasibility separately.
                    continue
        self._demand = demand
        self._demand_world_id = world.world_id
        return demand

    # -- protocol ---------------------------------------------------------- #
    def feasible_plans(self, bus: Bus, world: World) -> List[List[str]]:
        plans = enumerate_feasible_plans(bus, world)
        if not plans:
            raise InfeasibleRouteError(
                f"bus {bus.id!r}: no feasible charge plan from {bus.origin!r} "
                f"to {bus.destination!r} under range "
                f"{world.physics.battery_range_km:.1f} km"
            )
        return plans

    def choose(self, bus: Bus, world: World) -> List[str]:
        # No fleet context -> behave exactly like the default policy.
        if self.scenario is None:
            return greedy_max_reach(bus, world)

        plans = self.feasible_plans(bus, world)
        fewest = min(len(p) for p in plans)
        candidates = [p for p in plans if len(p) == fewest]
        demand = self._demand_for(world)

        def load(plan: List[str]) -> float:
            return sum(demand.get(sid, 0) for sid in plan)

        # Lowest predicted load wins; deterministic tie-break by station-id tuple.
        candidates.sort(key=lambda p: (load(p), tuple(p)))
        return candidates[0]


# --------------------------------------------------------------------------- #
# Registry / factory                                                          #
# --------------------------------------------------------------------------- #
POLICIES = {
    "max_reach": MaxReachStationPolicy,
    "load_aware": LoadAwareStationPolicy,
}


def get_policy(name: str, scenario: Optional[Scenario] = None) -> StationPolicy:
    """Instantiate the named policy.

    ``scenario`` is forwarded to policies that accept it (currently
    ``load_aware``); policies that don't need it ignore the argument. An unknown
    name raises :class:`ValueError` listing the valid names.
    """
    try:
        cls = POLICIES[name]
    except KeyError:
        valid = ", ".join(sorted(POLICIES))
        raise ValueError(f"unknown station policy {name!r}; valid policies: {valid}")
    if cls is LoadAwareStationPolicy:
        return cls(scenario=scenario)
    return cls()
