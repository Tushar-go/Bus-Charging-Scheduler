"""Domain model — every concrete fact about the world is data, not code.

Design rules baked in here (see ARCHITECTURE.md for the full rationale):

* A node is a charging **station** iff it carries ``charger_capacity > 0``.
  Endpoints are simply nodes without a charger. No ``is_endpoint`` boolean
  drives logic, so "the new node is also an endpoint" can never break us.
* A charger is a **resource with capacity N**. ``capacity == 1`` is not
  special-cased, so "double the chargers" is a one-number data edit.
* Station **positions** and segment **travel times** are *derived* from
  segment distances + speed — never stored — so changing a distance or a
  speed needs zero code.
* A bus carries ``origin``/``destination`` node ids; **direction is derived**
  (forward iff origin precedes destination in route order). Reverse buses
  traverse the *same* shared stations backwards. This generalises to more
  than two endpoints and mid-corridor trips for free.
* Free-form ``attrs`` (bus/node) and ``data`` (world/scenario) dicts let any
  future field or table attach with no schema change.
* Time is integer-friendly minutes-from-midnight internally (exact ordering);
  the UI formats back to ``HH:MM``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple


# --------------------------------------------------------------------------- #
# World (the physical universe — shared by every scenario)                    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Segment:
    frm: str
    to: str
    distance_km: float


@dataclass(frozen=True)
class Node:
    id: str
    sequence: int
    role: str = "station"
    charger_capacity: int = 0          # 0 => not a scheduling station (endpoint / pass-through)
    charge_duration_min: Optional[float] = None   # per-station override of physics default
    attrs: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_station(self) -> bool:
        return self.charger_capacity > 0


@dataclass(frozen=True)
class Physics:
    battery_range_km: float = 240.0
    speed_kmph: float = 60.0
    charge_duration_min: float = 25.0
    charge_target: str = "full"

    def travel_min(self, distance_km: float) -> float:
        """Travel time is DERIVED from distance and speed (never stored)."""
        return distance_km * 60.0 / self.speed_kmph


@dataclass(frozen=True)
class Operator:
    id: str
    name: str = ""


class World:
    """The route graph + physics + operators. Positions/times are derived.

    Not a frozen dataclass because it precomputes lookup caches on construction;
    it is treated as read-only everywhere (the simulation never mutates it).
    """

    def __init__(
        self,
        nodes: List[Node],
        segments: List[Segment],
        physics: Physics,
        operators: List[Operator],
        data: Optional[Dict[str, Any]] = None,
        world_id: str = "world",
    ) -> None:
        self.world_id = world_id
        self.nodes: Tuple[Node, ...] = tuple(sorted(nodes, key=lambda n: n.sequence))
        self.segments: Tuple[Segment, ...] = tuple(segments)
        self.physics = physics
        self.operators: Tuple[Operator, ...] = tuple(operators)
        self.data: Dict[str, Any] = dict(data or {})

        self._by_id: Dict[str, Node] = {n.id: n for n in self.nodes}
        # distance between any two adjacent-by-sequence nodes
        self._seg_dist: Dict[frozenset, float] = {
            frozenset((s.frm, s.to)): float(s.distance_km) for s in self.segments
        }
        # cumulative position from the first node (sequence 0)
        self._pos: Dict[str, float] = {}
        running = 0.0
        ordered = list(self.nodes)
        if ordered:
            self._pos[ordered[0].id] = 0.0
            for prev, cur in zip(ordered, ordered[1:]):
                key = frozenset((prev.id, cur.id))
                if key not in self._seg_dist:
                    raise ValueError(
                        f"world: no segment connects consecutive nodes "
                        f"{prev.id!r} and {cur.id!r}"
                    )
                running += self._seg_dist[key]
                self._pos[cur.id] = running
        self.total_km = running

    # -- lookups ----------------------------------------------------------- #
    def node(self, node_id: str) -> Node:
        return self._by_id[node_id]

    def has_node(self, node_id: str) -> bool:
        return node_id in self._by_id

    def station_ids(self) -> List[str]:
        return [n.id for n in self.nodes if n.is_station]

    def stations(self) -> List[Node]:
        return [n for n in self.nodes if n.is_station]

    def position(self, node_id: str) -> float:
        return self._pos[node_id]

    def distance(self, a: str, b: str) -> float:
        return abs(self._pos[a] - self._pos[b])

    def travel_min(self, a: str, b: str) -> float:
        return self.physics.travel_min(self.distance(a, b))

    def charge_minutes(self, node_id: str) -> float:
        node = self._by_id[node_id]
        if node.charge_duration_min is not None:
            return float(node.charge_duration_min)
        return float(self.physics.charge_duration_min)

    def capacity(self, node_id: str) -> int:
        return self._by_id[node_id].charger_capacity

    def stations_in_travel_order(self, origin: str, destination: str) -> List[Tuple[str, float]]:
        """Stations strictly between origin and destination, in the order the
        bus reaches them, each paired with its distance-from-origin.

        Works for both directions because position is a single corridor axis
        and we sort by signed progress away from the origin.
        """
        o_seq = self._by_id[origin].sequence
        d_seq = self._by_id[destination].sequence
        forward = o_seq < d_seq
        lo, hi = (o_seq, d_seq) if forward else (d_seq, o_seq)
        between = [n for n in self.nodes if lo < n.sequence < hi and n.is_station]
        between.sort(key=lambda n: n.sequence, reverse=not forward)
        return [(n.id, self.distance(origin, n.id)) for n in between]

    def direction(self, origin: str, destination: str) -> str:
        return "forward" if self._by_id[origin].sequence < self._by_id[destination].sequence else "reverse"


# --------------------------------------------------------------------------- #
# Scenario (the tunable inputs — weights + fleet)                             #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bus:
    id: str
    operator: str
    origin: str
    destination: str
    departure: int                      # minutes from midnight
    attrs: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    name: str
    weights: Dict[str, float]           # THE single source of truth for weights
    buses: List[Bus]
    world: World
    station_policy: str = "max_reach"
    data: Dict[str, Any] = field(default_factory=dict)
    scenario_id: str = ""


# --------------------------------------------------------------------------- #
# Runtime state (mutable, owned by the engine; exposed read-only to rules)    #
# --------------------------------------------------------------------------- #
@dataclass
class BusState:
    bus: Bus
    path_stops: List[str]               # charge node ids (in order) then the destination
    charge_nodes: List[str]             # just the charge node ids
    total_trip_km: float
    stop_index: int = 0                 # index into path_stops of the current/next stop
    pos_km: float = 0.0                 # distance from origin already covered
    range_left: float = 0.0
    status: str = "enroute"             # enroute | queued | charging | finished
    queue_arrival_t: Optional[float] = None   # when it joined the current station's queue
    last_station_pos: float = -1.0      # distance-from-origin of the last station it charged at
    departure_t: float = 0.0
    arrival_t: Optional[float] = None
    total_wait: float = 0.0
    num_charges: int = 0
    events: List["Event"] = field(default_factory=list)

    @property
    def charges_remaining(self) -> int:
        """Charges still to do, including the one at the current stop."""
        return max(0, len(self.charge_nodes) - self.stop_index)


# --------------------------------------------------------------------------- #
# Output model                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    type: str                           # depart|travel|arrive_station|wait|charge_start|charge_end|arrive_destination
    bus_id: str
    node: Optional[str] = None
    segment: Optional[str] = None
    t_start: float = 0.0
    t_end: float = 0.0
    battery_km_in: Optional[float] = None
    battery_km_out: Optional[float] = None
    charger_index: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChargeRecord:
    charger_index: int
    bus_id: str
    operator: str
    charge_start: float
    charge_end: float
    wait_min: float


@dataclass
class StationQueue:
    node: str
    capacity: int
    records: List[ChargeRecord] = field(default_factory=list)   # in charge-start order


@dataclass
class PerBusPlan:
    bus_id: str
    operator: str
    origin: str
    destination: str
    direction: str
    departure: float
    arrival: Optional[float]
    total_wait_min: float
    num_charges: int
    charge_nodes: List[str]
    events: List[Event]


@dataclass
class Schedule:
    scenario_id: str
    scenario_name: str
    weights: Dict[str, float]
    buses: List[PerBusPlan]
    stations: List[StationQueue]
    objective: Dict[str, Any]
    feasible: bool = True
    violations: List[str] = field(default_factory=list)

    def plan_for(self, bus_id: str) -> PerBusPlan:
        for p in self.buses:
            if p.bus_id == bus_id:
                return p
        raise KeyError(f"no bus {bus_id!r} in this schedule")

    def station(self, node_id: str) -> StationQueue:
        for s in self.stations:
            if s.node == node_id:
                return s
        raise KeyError(f"no station {node_id!r} in this schedule")
