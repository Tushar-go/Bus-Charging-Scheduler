"""DecisionContext — the read-only snapshot every rule sees.

This is the linchpin of the architecture. A rule's only job is: *given a
decision the engine is about to make, return a number*. The art is exposing
**enough state that an unforeseen rule needs zero engine changes**.

We therefore hand a rule a read-only view over the *entire* simulation state
(the world, the clock, every station's queue and history, every bus's live
state, the scenario data) plus the specific candidate being scored. If a rule
can see all of that, then priority buses, time-of-day pricing, driver shifts,
and operator fairness are all *just arithmetic over fields that already exist*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping

from scheduler.models import Bus, BusState, ChargeRecord, Node, Scenario, World


@dataclass(frozen=True)
class DecisionContext:
    # --- the decision being scored --------------------------------------- #
    candidate: Bus                      # the bus we might let charge next
    station: Node                       # where (carries .charger_capacity, .id, attrs)
    decision_kind: str                  # "charge_order" today; future kinds reuse this context

    # --- time ------------------------------------------------------------- #
    clock: float                        # current simulation time (minutes from midnight)
    candidate_ready_at: float           # when the candidate joined this station's queue
    candidate_wait_so_far: float        # clock - candidate_ready_at (its wait if chosen now)
    charge_duration: float              # minutes this charge will take

    # --- local station state --------------------------------------------- #
    queue: List[Bus]                    # all buses currently waiting here (incl. candidate)
    station_history: List[ChargeRecord] # buses already charged here, in order
    free_slots: int                     # chargers free right now at this station

    # --- global state ----------------------------------------------------- #
    world: World
    scenario: Scenario
    bus_states: Mapping[str, BusState]  # every bus's live state
    weights: Mapping[str, float]        # exposed so a meta-rule could read it; normal rules don't

    # --- convenience derived helpers (pure functions over the above) ------ #
    def fleet_of(self, operator: str) -> List[Bus]:
        return [b for b in self.scenario.buses if b.operator == operator]

    def waiting_same_operator(self) -> List[Bus]:
        return [b for b in self.queue if b.operator == self.candidate.operator]

    def wait_of(self, bus_id: str) -> float:
        st = self.bus_states[bus_id]
        if st.queue_arrival_t is None:
            return 0.0
        return max(0.0, self.clock - st.queue_arrival_t)

    def projected_arrival(self, bus_id: str) -> float:
        """Optimistic remaining arrival if this bus left now and hit no further
        waits: remaining travel + remaining mandatory charges. A clean SPT proxy."""
        st = self.bus_states[bus_id]
        remaining_km = max(0.0, st.total_trip_km - st.pos_km)
        travel = self.world.physics.travel_min(remaining_km)
        charges = st.charges_remaining * self.charge_duration
        return self.clock + travel + charges
