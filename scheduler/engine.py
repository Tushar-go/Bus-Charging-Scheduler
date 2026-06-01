"""The scheduling engine — a deterministic discrete-event simulation.

This is the heart of the system, and yet it knows *nothing* about any concrete
rule. Its single point of contact with the rule system is :func:`_priority`,
which sums ``weight * rule.score(ctx)`` over whatever soft rules happen to be in
the registry. Adding, removing, or reweighting a rule therefore never touches
this file — that is the whole point of the architecture.

How it works
============

1. **Routing** (``station_policy``) decides *which* stations each bus charges
   at. The engine never second-guesses that choice; it only schedules timing.

2. **Simulation** is event-driven. Two event kinds drive everything:

   * ``ARRIVE`` — a bus reaches its next stop (a station, or its destination).
   * ``CHARGE_DONE`` — a charge finishes; the bus departs that station.

   Events are popped from a min-heap keyed by ``(time, seq, ...)``. ``seq`` is a
   monotonically increasing integer assigned at push time, giving a fully
   deterministic order even when two events share a timestamp.

3. **Batch fairness.** At each distinct timestamp we pop *every* event sharing
   that time and apply all their state changes *before* making any dispatch
   decision. Only then, for each affected station (in sorted id order), do we
   run :func:`maybe_start_charging`. This guarantees that a bus arriving at the
   exact instant a slot frees is considered alongside the buses already waiting
   — nobody is unfairly skipped because of pop order.

4. **Dispatch.** When a slot is free and buses are waiting,
   :func:`pick_next_to_charge` filters out any candidate vetoed by a hard rule,
   then picks the highest-priority survivor (highest weighted soft score), with
   deterministic tie-breaks.

5. **Validation.** Hard constraints are guaranteed by construction, but a
   post-run :func:`validate` independently re-checks every invariant over the
   produced timelines and records any violation. Belt *and* braces.

Determinism is mandatory: solving the same ``(world, scenario)`` twice yields
byte-identical schedules. Every ordering decision (heap keys, station iteration,
tie-breaks, output sorting) is therefore total and input-derived.
"""
from __future__ import annotations

import heapq
import itertools
import statistics
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# Importing the rules package registers every built-in rule via its
# ``@soft_rule`` / ``@hard_rule`` decorators (decorator side effects). It MUST
# precede ``import scheduler.registry`` below: ``registry`` imports
# ``scheduler.rules.base``, which initialises the ``scheduler.rules`` package;
# if ``registry`` were imported first, the package's eager rule imports would
# re-enter ``registry`` before ``soft_rule`` is defined (a circular import).
# Importing the package here first makes ``registry`` fully initialise as a
# nested step, after which every later ``from scheduler.registry import ...``
# resolves cleanly. ``scheduler.rules`` never imports this module, so there is
# no cycle back to the engine.
from scheduler import rules as _rules  # noqa: F401 — ensure built-in rules are registered

from scheduler.context import DecisionContext
from scheduler.models import (
    Bus,
    BusState,
    ChargeRecord,
    Event,
    PerBusPlan,
    Scenario,
    Schedule,
    StationQueue,
    World,
)
from scheduler.registry import REGISTRY
from scheduler.station_policy import InfeasibleRouteError, get_policy

# Event-kind tags (module constants keep the heap payloads self-documenting).
ARRIVE = "ARRIVE"
CHARGE_DONE = "CHARGE_DONE"


# --------------------------------------------------------------------------- #
# The integration point — the ONLY place soft-rule weights are read for       #
# dispatch. It names no individual rule.                                      #
# --------------------------------------------------------------------------- #
def _priority(ctx: DecisionContext, weights) -> float:
    """Weighted sum of every registered soft rule's score for ``ctx``.

    Higher == this candidate is more preferred to charge next. A rule with no
    entry in ``weights`` contributes zero (``weights.get(name, 0.0)``).
    """
    return sum(
        weights.get(name, 0.0) * rule.score(ctx)
        for name, rule in REGISTRY.soft_rules().items()
    )


def _id_key(bus_id: str) -> Tuple[int, ...]:
    """A reversible key that makes the *lexicographically smallest* bus id win.

    ``pick_next_to_charge`` selects the candidate with the **maximum** sort key.
    To make the final tie-break prefer the smallest id under that ``max``, we
    negate each character's code point: the smallest id then has the largest
    negated tuple. Length is part of the natural tuple ordering, so equal
    prefixes order by length correctly too. The mapping is total and
    input-derived, so it is fully deterministic.
    """
    return tuple(-ord(c) for c in bus_id)


def pick_next_to_charge(
    candidates: List[Bus],
    make_ctx: Callable[[Bus], DecisionContext],
    weights,
) -> Optional[Bus]:
    """Choose which waiting bus charges next at a now-free slot.

    1. Drop any candidate vetoed by a hard rule (``is_feasible`` False).
    2. Among the survivors, return the one maximising the tuple
       ``(priority, -ready_at, _id_key(id))``:

       * highest weighted soft-rule priority, then
       * earliest queue-arrival time (``-ready_at`` so earlier wins under max),
         then
       * lexicographically smallest bus id (via :func:`_id_key`).

    Returns ``None`` when no candidate is feasible.
    """
    hard = REGISTRY.hard_rules()
    # Build each candidate's context ONCE (it is a pure read-only snapshot) and
    # reuse it for both the hard-rule veto and the priority/tie-break key.
    scored = []
    for b in candidates:
        ctx = make_ctx(b)
        if all(r.is_feasible(ctx) for r in hard.values()):
            scored.append((b, ctx))
    if not scored:
        return None
    return max(
        scored,
        key=lambda bc: (
            _priority(bc[1], weights),
            -bc[1].candidate_ready_at,
            _id_key(bc[0].id),
        ),
    )[0]


# --------------------------------------------------------------------------- #
# Per-station runtime resource                                                #
# --------------------------------------------------------------------------- #
@dataclass
class _Slot:
    """One charger bay. ``free_at`` is the time it next becomes available."""

    free_at: float = float("-inf")


@dataclass
class _Station:
    """Live state of one charging station during the simulation."""

    node_id: str
    capacity: int
    slots: List[_Slot] = field(default_factory=list)
    # Waiting buses as (bus_id, join_time), kept in FIFO/join order for stable
    # iteration; the actual dispatch order is decided by pick_next_to_charge.
    waiting: List[Tuple[str, float]] = field(default_factory=list)
    history: List[ChargeRecord] = field(default_factory=list)

    def free_slot_indices(self, t: float) -> List[int]:
        """Indices of slots free at time ``t`` (``free_at <= t``)."""
        return [i for i, s in enumerate(self.slots) if s.free_at <= t]

    def free_slot_count(self, t: float) -> int:
        return sum(1 for s in self.slots if s.free_at <= t)


# --------------------------------------------------------------------------- #
# The simulation                                                              #
# --------------------------------------------------------------------------- #
class _Simulation:
    """Owns all mutable state for one ``solve`` call.

    Kept as a class purely to thread state cleanly between the event handlers;
    it is created, run, and discarded inside :func:`solve`.
    """

    def __init__(self, world: World, scenario: Scenario) -> None:
        self.world = world
        self.scenario = scenario
        self.weights = scenario.weights

        # Resolve the station policy once. load_aware needs the fleet, so we
        # always pass the scenario; policies that ignore it are unaffected.
        self.policy = get_policy(scenario.station_policy, scenario=scenario)

        self.states: Dict[str, BusState] = {}
        self.stations: Dict[str, _Station] = {}
        self.infeasible: List[str] = []  # bus ids the policy could not route

        # Event heap of (time, seq, kind, payload); seq breaks time ties.
        self._heap: List[Tuple[float, int, str, dict]] = []
        self._seq = itertools.count()

        self._build_states()
        self._build_stations()

    # -- setup ------------------------------------------------------------- #
    def _build_states(self) -> None:
        """Create a :class:`BusState` per bus and seed its first ARRIVE.

        Buses are processed in scenario order so that seq/state construction is
        deterministic. A bus the policy cannot route is recorded as infeasible
        and excluded from the simulation (it produces an arrival of ``None``).
        """
        full = float(self.world.physics.battery_range_km)
        for bus in self.scenario.buses:
            try:
                charge_nodes = self.policy.choose(bus, self.world)
            except InfeasibleRouteError:
                self.infeasible.append(bus.id)
                # Still create a state so the bus appears in output (unfinished).
                self.states[bus.id] = BusState(
                    bus=bus,
                    path_stops=[bus.destination],
                    charge_nodes=[],
                    total_trip_km=self.world.distance(bus.origin, bus.destination),
                    range_left=full,
                    departure_t=float(bus.departure),
                    status="infeasible",
                )
                continue

            path_stops = list(charge_nodes) + [bus.destination]
            st = BusState(
                bus=bus,
                path_stops=path_stops,
                charge_nodes=list(charge_nodes),
                total_trip_km=self.world.distance(bus.origin, bus.destination),
                range_left=full,
                departure_t=float(bus.departure),
                pos_km=0.0,
                status="enroute",
            )
            self.states[bus.id] = st
            # Record the departure event and schedule arrival at the first stop.
            st.events.append(
                Event(
                    type="depart",
                    bus_id=bus.id,
                    node=bus.origin,
                    t_start=st.departure_t,
                    t_end=st.departure_t,
                    battery_km_out=st.range_left,
                )
            )
            self._schedule_arrival(st, depart_time=st.departure_t)

    def _build_stations(self) -> None:
        """Create a :class:`_Station` resource per charging node in the world."""
        for node in self.world.stations():
            cap = int(node.charger_capacity)
            self.stations[node.id] = _Station(
                node_id=node.id,
                capacity=cap,
                slots=[_Slot() for _ in range(cap)],
            )

    # -- heap helpers ------------------------------------------------------ #
    def _push(self, time: float, kind: str, payload: dict) -> None:
        heapq.heappush(self._heap, (float(time), next(self._seq), kind, payload))

    def _schedule_arrival(self, st: BusState, depart_time: float) -> None:
        """Schedule the bus's ARRIVE at ``path_stops[stop_index]``.

        ``stop_index`` points at the current/next stop. The origin of the leg is
        the previous stop (or the bus origin for the first leg). ``depart_time``
        is when the bus leaves its current location (its scheduled departure for
        the first leg, or the charge-end time for later legs); arrival is that
        plus the leg's travel time.
        """
        bus = st.bus
        idx = st.stop_index
        next_stop = st.path_stops[idx]
        from_node = bus.origin if idx == 0 else st.path_stops[idx - 1]
        leg_km = self.world.distance(from_node, next_stop)
        arrive_t = depart_time + self.world.physics.travel_min(leg_km)
        # Record the travel leg for the timeline.
        st.events.append(
            Event(
                type="travel",
                bus_id=bus.id,
                segment=f"{from_node}->{next_stop}",
                t_start=depart_time,
                t_end=arrive_t,
                battery_km_in=st.range_left,
                battery_km_out=st.range_left - leg_km,
            )
        )
        self._push(
            arrive_t,
            ARRIVE,
            {"bus_id": bus.id, "from": from_node, "to": next_stop, "leg_km": leg_km},
        )

    # -- main loop --------------------------------------------------------- #
    def run(self) -> None:
        """Process events in batches sharing the smallest timestamp."""
        while self._heap:
            t = self._heap[0][0]
            affected: set[str] = set()
            # Pop and apply every event at this exact time before dispatching.
            while self._heap and self._heap[0][0] == t:
                _, _, kind, payload = heapq.heappop(self._heap)
                if kind == ARRIVE:
                    self._on_arrive(t, payload, affected)
                elif kind == CHARGE_DONE:
                    self._on_charge_done(t, payload, affected)
            # Now make dispatch decisions, stations in sorted id order.
            for sid in sorted(affected):
                self.maybe_start_charging(self.stations[sid], t)

    def _on_arrive(self, t: float, payload: dict, affected: set) -> None:
        """Handle a bus reaching its next stop (station or destination)."""
        st = self.states[payload["bus_id"]]
        leg_km = payload["leg_km"]
        # Deduct the traveled leg from range and advance position.
        st.range_left -= leg_km
        st.pos_km += leg_km

        if payload["to"] == st.bus.destination:
            # Trip complete.
            st.status = "finished"
            st.arrival_t = t
            st.events.append(
                Event(
                    type="arrive_destination",
                    bus_id=st.bus.id,
                    node=st.bus.destination,
                    t_start=t,
                    t_end=t,
                    battery_km_in=st.range_left,
                )
            )
            return

        # Arrived at a charging station: enqueue.
        sid = payload["to"]
        station = self.stations[sid]
        st.status = "queued"
        st.queue_arrival_t = t
        station.waiting.append((st.bus.id, t))
        st.events.append(
            Event(
                type="arrive_station",
                bus_id=st.bus.id,
                node=sid,
                t_start=t,
                t_end=t,
                battery_km_in=st.range_left,
            )
        )
        affected.add(sid)

    def _on_charge_done(self, t: float, payload: dict, affected: set) -> None:
        """Handle a charge finishing: the bus refills and departs the station."""
        st = self.states[payload["bus_id"]]
        sid = payload["station"]
        # Battery is full at charge end; the bus departs the station now (t).
        st.range_left = float(self.world.physics.battery_range_km)
        st.status = "enroute"
        st.last_station_pos = self.world.distance(st.bus.origin, sid)
        # Advance to the next stop and schedule its arrival, departing at t.
        st.stop_index += 1
        self._schedule_arrival(st, depart_time=t)
        # The slot was freed (free_at set to this end time) when charging began,
        # so the station may now be able to start another waiting bus.
        affected.add(sid)

    # -- dispatch ---------------------------------------------------------- #
    def maybe_start_charging(self, station: _Station, t: float) -> None:
        """Start charging waiting buses while free slots remain.

        Each iteration: build a :class:`DecisionContext` for every waiting bus,
        ask :func:`pick_next_to_charge` for the winner, and assign it to a free
        slot. Repeats until no free slot or no waiting bus (or all candidates
        are vetoed by a hard rule).
        """
        while station.free_slot_count(t) >= 1 and station.waiting:
            waiting_buses = [self.states[bid].bus for bid, _ in station.waiting]

            def make_ctx(bus: Bus) -> DecisionContext:
                return self._make_ctx(bus, station, t)

            winner = pick_next_to_charge(waiting_buses, make_ctx, self.weights)
            if winner is None:
                # Every waiting candidate is currently infeasible; stop trying.
                break

            self._start_charge(station, winner, t)

    def _start_charge(self, station: _Station, bus: Bus, t: float) -> None:
        """Commit ``bus`` to a free slot at ``station`` starting at ``t``."""
        st = self.states[bus.id]
        # Lowest free slot index for deterministic charger assignment.
        free_idx = station.free_slot_indices(t)
        charger_index = min(free_idx)
        slot = station.slots[charger_index]

        duration = self.world.charge_minutes(station.node_id)
        start = t
        end = t + duration
        slot.free_at = end  # occupy the slot until the charge ends

        # Remove the winner from the waiting list (first matching id).
        for i, (bid, _) in enumerate(station.waiting):
            if bid == bus.id:
                station.waiting.pop(i)
                break

        wait = max(0.0, t - (st.queue_arrival_t if st.queue_arrival_t is not None else t))
        st.total_wait += wait
        st.num_charges += 1
        st.status = "charging"

        record = ChargeRecord(
            charger_index=charger_index,
            bus_id=bus.id,
            operator=bus.operator,
            charge_start=start,
            charge_end=end,
            wait_min=wait,
        )
        station.history.append(record)

        # Timeline events: the wait (if any), then the charge window.
        if wait > 0:
            st.events.append(
                Event(
                    type="wait",
                    bus_id=bus.id,
                    node=station.node_id,
                    t_start=st.queue_arrival_t if st.queue_arrival_t is not None else start,
                    t_end=start,
                )
            )
        st.events.append(
            Event(
                type="charge_start",
                bus_id=bus.id,
                node=station.node_id,
                t_start=start,
                t_end=start,
                charger_index=charger_index,
                battery_km_in=st.range_left,
            )
        )
        st.events.append(
            Event(
                type="charge_end",
                bus_id=bus.id,
                node=station.node_id,
                t_start=start,
                t_end=end,
                charger_index=charger_index,
                battery_km_in=st.range_left,
                battery_km_out=float(self.world.physics.battery_range_km),
            )
        )

        # The candidate is no longer waiting; clear its queue marker now that the
        # wait is locked in (so its DecisionContext wait for later stations resets
        # cleanly when it re-queues).
        st.queue_arrival_t = None
        self._push(end, CHARGE_DONE, {"bus_id": bus.id, "station": station.node_id,
                                       "charger_index": charger_index})

    def _make_ctx(self, bus: Bus, station: _Station, t: float) -> DecisionContext:
        """Snapshot the current world/queue state for scoring ``bus``."""
        st = self.states[bus.id]
        ready_at = st.queue_arrival_t if st.queue_arrival_t is not None else t
        queue_buses = [self.states[bid].bus for bid, _ in station.waiting]
        return DecisionContext(
            candidate=bus,
            station=self.world.node(station.node_id),
            decision_kind="charge_order",
            clock=t,
            candidate_ready_at=ready_at,
            candidate_wait_so_far=max(0.0, t - ready_at),
            charge_duration=self.world.charge_minutes(station.node_id),
            queue=queue_buses,
            station_history=station.history,
            free_slots=station.free_slot_count(t),
            world=self.world,
            scenario=self.scenario,
            bus_states=self.states,
            weights=self.weights,
        )


# --------------------------------------------------------------------------- #
# Metrics                                                                     #
# --------------------------------------------------------------------------- #
def _metrics(world: World, scenario: Scenario, states: Dict[str, BusState]) -> Dict:
    """Compute the objective dict from finished bus states.

    Only finished buses contribute to costs (an infeasible/unfinished bus has no
    arrival). Weights are read **exclusively** from ``scenario.weights``.

    Cost definitions:
      * ``Cost_individual = max_b wait_b + 0.25 * mean_b wait_b``
      * ``Cost_operator   = sum_op (mean_{b in op} wait_b + pstdev_{b in op} wait_b)``
      * ``Cost_overall     = sum_b trip_b``
    where ``wait_b`` is total wait minutes and ``trip_b = arrival - departure``.
    """
    finished = [s for s in states.values() if s.arrival_t is not None]

    waits = [s.total_wait for s in finished]
    trips = [s.arrival_t - s.departure_t for s in finished]

    # --- individual --------------------------------------------------------
    if waits:
        cost_individual = max(waits) + 0.25 * statistics.fmean(waits)
    else:
        cost_individual = 0.0

    # --- operator ----------------------------------------------------------
    # Group finished buses by operator, preserving world operator order for a
    # deterministic, readable by_operator section.
    by_op_buses: Dict[str, List[BusState]] = {}
    for s in finished:
        by_op_buses.setdefault(s.bus.operator, []).append(s)

    cost_operator = 0.0
    for op_states in by_op_buses.values():
        op_waits = [s.total_wait for s in op_states]
        mean_w = statistics.fmean(op_waits)
        std_w = statistics.pstdev(op_waits) if len(op_waits) > 1 else 0.0
        cost_operator += mean_w + 1.0 * std_w

    # --- overall -----------------------------------------------------------
    cost_overall = sum(trips)

    # --- weighted total ----------------------------------------------------
    w_ind = scenario.weights.get("individual", 0.0)
    w_op = scenario.weights.get("operator", 0.0)
    w_ov = scenario.weights.get("overall", 0.0)
    total = w_ind * cost_individual + w_op * cost_operator + w_ov * cost_overall

    makespan = max((s.arrival_t for s in finished), default=0.0)

    # --- per-operator breakdown (ordered by world operator declaration) ----
    by_operator: Dict[str, Dict[str, float]] = {}
    for op in world.operators:
        op_states = by_op_buses.get(op.id, [])
        if not op_states:
            continue
        op_waits = [s.total_wait for s in op_states]
        op_trips = [s.arrival_t - s.departure_t for s in op_states]
        by_operator[op.id] = {
            "count": len(op_states),
            "mean_wait": statistics.fmean(op_waits),
            "max_wait": max(op_waits),
            "std_wait": statistics.pstdev(op_waits) if len(op_waits) > 1 else 0.0,
            "mean_trip": statistics.fmean(op_trips),
        }
    # Include any operator that appears on a bus but not in world.operators
    # (defensive: keeps every contributing operator visible). Sorted for
    # determinism.
    for op_id in sorted(by_op_buses):
        if op_id in by_operator:
            continue
        op_states = by_op_buses[op_id]
        op_waits = [s.total_wait for s in op_states]
        op_trips = [s.arrival_t - s.departure_t for s in op_states]
        by_operator[op_id] = {
            "count": len(op_states),
            "mean_wait": statistics.fmean(op_waits),
            "max_wait": max(op_waits),
            "std_wait": statistics.pstdev(op_waits) if len(op_waits) > 1 else 0.0,
            "mean_trip": statistics.fmean(op_trips),
        }

    return {
        "total": float(total),
        "makespan": float(makespan),
        "by_rule": {
            "individual": {
                "raw": float(cost_individual),
                "weight": float(w_ind),
                "weighted": float(w_ind * cost_individual),
            },
            "operator": {
                "raw": float(cost_operator),
                "weight": float(w_op),
                "weighted": float(w_op * cost_operator),
            },
            "overall": {
                "raw": float(cost_overall),
                "weight": float(w_ov),
                "weighted": float(w_ov * cost_overall),
            },
        },
        "by_operator": by_operator,
    }


# --------------------------------------------------------------------------- #
# Post-run validation — independently re-checks every hard invariant          #
# --------------------------------------------------------------------------- #
def validate(world: World, schedule: Schedule) -> None:
    """Re-derive and re-check every hard constraint over the produced timelines.

    Mutates ``schedule.violations`` / ``schedule.feasible`` in place. The checks
    are independent of how the engine *built* the schedule, so they catch any
    construction bug as well as genuinely infeasible inputs.

    Invariants:
      * **Completion** — every bus has an arrival time.
      * **Range** — every realized leg (between consecutive stops) is within
        ``battery_range_km``.
      * **Charge duration** — each charge lasts exactly ``charge_minutes`` for
        its station.
      * **Route order** — a bus's charged stations are strictly increasing in
        distance-from-origin (forward progress only).
      * **Capacity** — no two charge records on the same (station, charger)
        overlap in time.
    """
    violations: List[str] = []
    battery = float(world.physics.battery_range_km)

    # -- per-bus checks ----------------------------------------------------- #
    for plan in schedule.buses:
        if plan.arrival is None:
            violations.append(f"bus {plan.bus_id}: did not reach destination")
            continue

        # Realized stop sequence: origin, each charge node, destination.
        stops = [plan.origin] + list(plan.charge_nodes) + [plan.destination]
        prev = stops[0]
        for nxt in stops[1:]:
            leg = world.distance(prev, nxt)
            if leg > battery + 1e-6:
                violations.append(
                    f"bus {plan.bus_id}: leg {prev}->{nxt} = {leg:.1f} km "
                    f"exceeds range {battery:.1f} km"
                )
            prev = nxt

        # Route order: charged-station distance-from-origin strictly increasing.
        dists = [world.distance(plan.origin, sid) for sid in plan.charge_nodes]
        for a, b in zip(dists, dists[1:]):
            if not (b > a):
                violations.append(
                    f"bus {plan.bus_id}: charge stations not in forward order "
                    f"({a:.1f} km then {b:.1f} km)"
                )

        # Charge duration: every charge_end event lasts exactly charge_minutes.
        for ev in plan.events:
            if ev.type == "charge_end":
                expected = world.charge_minutes(ev.node)
                actual = ev.t_end - ev.t_start
                if abs(actual - expected) > 1e-6:
                    violations.append(
                        f"bus {plan.bus_id}: charge at {ev.node} lasted "
                        f"{actual:.1f} min, expected {expected:.1f} min"
                    )

    # -- per-station capacity (no overlap per charger) ---------------------- #
    for sq in schedule.stations:
        per_charger: Dict[int, List[ChargeRecord]] = {}
        for rec in sq.records:
            per_charger.setdefault(rec.charger_index, []).append(rec)
        for idx, recs in per_charger.items():
            recs_sorted = sorted(recs, key=lambda r: r.charge_start)
            for a, b in zip(recs_sorted, recs_sorted[1:]):
                if b.charge_start < a.charge_end - 1e-6:
                    violations.append(
                        f"station {sq.node} charger {idx}: {a.bus_id} "
                        f"[{a.charge_start:.1f},{a.charge_end:.1f}] overlaps "
                        f"{b.bus_id} [{b.charge_start:.1f},{b.charge_end:.1f}]"
                    )

    schedule.violations = violations
    schedule.feasible = not violations


# --------------------------------------------------------------------------- #
# Output assembly                                                             #
# --------------------------------------------------------------------------- #
def _build_schedule(sim: _Simulation) -> Schedule:
    """Assemble the immutable-ish :class:`Schedule` output from sim state."""
    world, scenario = sim.world, sim.scenario

    # Per-bus plans, in scenario order (deterministic, matches input).
    bus_plans: List[PerBusPlan] = []
    for bus in scenario.buses:
        st = sim.states[bus.id]
        bus_plans.append(
            PerBusPlan(
                bus_id=bus.id,
                operator=bus.operator,
                origin=bus.origin,
                destination=bus.destination,
                direction=world.direction(bus.origin, bus.destination),
                departure=st.departure_t,
                arrival=st.arrival_t,
                total_wait_min=st.total_wait,
                num_charges=st.num_charges,
                charge_nodes=list(st.charge_nodes),
                events=st.events,
            )
        )

    # Per-station queues, stations in world (sequence) order, records by start.
    station_queues: List[StationQueue] = []
    for node in world.stations():
        s = sim.stations[node.id]
        records = sorted(
            s.history, key=lambda r: (r.charge_start, r.charger_index, r.bus_id)
        )
        station_queues.append(
            StationQueue(node=node.id, capacity=s.capacity, records=records)
        )

    objective = _metrics(world, scenario, sim.states)

    schedule = Schedule(
        scenario_id=scenario.scenario_id,
        scenario_name=scenario.name,
        weights=dict(scenario.weights),
        buses=bus_plans,
        stations=station_queues,
        objective=objective,
    )
    validate(world, schedule)
    return schedule


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #
def solve(world: World, scenario: Scenario) -> Schedule:
    """Run the deterministic simulation and return a :class:`Schedule`.

    Solving the same ``(world, scenario)`` twice produces byte-identical output.
    """
    sim = _Simulation(world, scenario)
    sim.run()
    return _build_schedule(sim)


# Rule registration is performed by the ``from scheduler import rules`` import
# at the TOP of this module (it must run before ``import scheduler.registry`` to
# break the registry<->rules import cycle present in the foundation). Keeping it
# at the top — rather than the bottom — is the only deviation from the spec's
# suggested placement, and it is required for the package to import at all.
