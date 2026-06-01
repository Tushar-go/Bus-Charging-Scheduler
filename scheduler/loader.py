"""YAML → domain-model loaders for the bus-charging scheduler.

This module is the *only* place that knows the on-disk YAML shape. It turns

    data/world.yaml          ->  World
    data/scenarios/*.yaml    ->  Scenario  (each referencing a World)

into the strongly-typed dataclasses defined in :mod:`scheduler.models`, and it
validates aggressively so a malformed data file fails loudly at load time
rather than silently mis-scheduling later.

Key design choices (mirroring the model's "everything is data" philosophy):

* A node becomes a charging **station** purely by carrying a ``charger`` block
  with ``capacity > 0``. No boolean flags drive scheduling logic.
* A scenario may carry an optional ``world_patch`` mapping that is **deep-merged
  over the world's raw dict before the World is constructed**. This lets a
  scenario tweak the physical world (e.g. double a charger's capacity, change a
  distance) without forking ``world.yaml``. To make that robust we parse the
  world YAML into a plain dict first and build the World from the (possibly
  patched) dict via :func:`_world_from_dict`.
* ``weights`` are passed through **verbatim** — the loader injects no code-side
  defaults, because the scenario is the single source of truth for weights.
* Time is stored as integer minutes-from-midnight. ``hhmm_to_min`` /
  ``min_to_hhmm`` convert to/from the human ``HH:MM`` clock string. Times may
  exceed 24h (a bus can finish after midnight), so we never modulo 1440.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

from scheduler.models import Bus, Node, Operator, Physics, Scenario, Segment, World


# --------------------------------------------------------------------------- #
# Time helpers                                                                #
# --------------------------------------------------------------------------- #
def hhmm_to_min(s: str) -> int:
    """Parse a ``"HH:MM"`` clock string into integer minutes from midnight.

    >>> hhmm_to_min("19:00")
    1140
    >>> hhmm_to_min("00:00")
    0

    Hours may exceed 23 (e.g. ``"25:30"`` for half-past one the next morning).
    """
    text = str(s).strip()
    if ":" not in text:
        raise ValueError(f"time {s!r} is not in HH:MM form")
    hh_str, _, mm_str = text.partition(":")
    try:
        hh = int(hh_str)
        mm = int(mm_str)
    except ValueError as exc:
        raise ValueError(f"time {s!r} is not in HH:MM form") from exc
    if hh < 0 or mm < 0 or mm > 59:
        raise ValueError(f"time {s!r} has out-of-range components")
    return hh * 60 + mm


def min_to_hhmm(m: float) -> str:
    """Format minutes-from-midnight back to ``"HH:MM"`` (zero-padded).

    Hours are NOT wrapped at 24h, so a trip that ends past midnight reads e.g.
    ``"25:11"`` rather than ``"01:11"`` — exact ordering is preserved.

    >>> min_to_hhmm(1140)
    '19:00'
    >>> min_to_hhmm(1631)
    '27:11'
    """
    total = int(round(float(m)))
    if total < 0:
        raise ValueError(f"cannot format negative time {m!r}")
    hours, mins = divmod(total, 60)
    return f"{hours:02d}:{mins:02d}"


# --------------------------------------------------------------------------- #
# Low-level YAML helpers                                                      #
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML file into a dict (always UTF-8); error on non-mapping roots."""
    with open(path, "r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, Mapping):
        raise ValueError(f"{path}: top-level YAML must be a mapping, got {type(loaded).__name__}")
    return dict(loaded)


def _deep_merge(base: Dict[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``patch`` over a deep copy of ``base`` and return it.

    Mappings merge key-by-key; any non-mapping value (including lists) in the
    patch replaces the corresponding base value outright.
    """
    out: Dict[str, Any] = {
        k: (dict(v) if isinstance(v, Mapping) else v) for k, v in base.items()
    }
    for key, pval in patch.items():
        bval = out.get(key)
        if isinstance(bval, Mapping) and isinstance(pval, Mapping):
            out[key] = _deep_merge(dict(bval), pval)
        else:
            out[key] = dict(pval) if isinstance(pval, Mapping) else pval
    return out


# --------------------------------------------------------------------------- #
# World construction                                                          #
# --------------------------------------------------------------------------- #
def _world_from_dict(d: Mapping[str, Any]) -> World:
    """Build a :class:`World` from an already-parsed (and possibly patched) dict.

    Kept separate from :func:`load_world` so scenarios can deep-merge a
    ``world_patch`` over the raw world dict and reuse the exact same builder.
    """
    defaults = dict(d.get("defaults") or {})
    charge_defaults = dict(defaults.get("charge") or {})

    battery_range_km = float(defaults.get("battery_range_km", 240.0))
    speed_kmph = float(defaults.get("speed_kmph", 60.0))
    default_charge_min = float(charge_defaults.get("duration_min", 25.0))
    charge_target = str(charge_defaults.get("target", "full"))
    default_capacity = int(defaults.get("charger_capacity", 1))

    physics = Physics(
        battery_range_km=battery_range_km,
        speed_kmph=speed_kmph,
        charge_duration_min=default_charge_min,
        charge_target=charge_target,
    )

    # -- nodes -------------------------------------------------------------- #
    raw_nodes = d.get("nodes")
    if not raw_nodes:
        raise ValueError("world: 'nodes' is required and must be a non-empty list")

    nodes: List[Node] = []
    seen_ids: set[str] = set()
    seen_seq: Dict[int, str] = {}
    for raw in raw_nodes:
        if "id" not in raw or "sequence" not in raw:
            raise ValueError(f"world: every node needs 'id' and 'sequence' (got {raw!r})")
        node_id = str(raw["id"])
        sequence = int(raw["sequence"])

        if node_id in seen_ids:
            raise ValueError(f"world: duplicate node id {node_id!r}")
        if sequence in seen_seq:
            raise ValueError(
                f"world: duplicate sequence {sequence} on nodes "
                f"{seen_seq[sequence]!r} and {node_id!r} (sequences must be unique)"
            )
        seen_ids.add(node_id)
        seen_seq[sequence] = node_id

        charger = raw.get("charger")
        if charger:
            charger_capacity = int(charger.get("capacity", default_capacity))
            per_node_dur = charger.get("duration_min")
            charge_duration_min = None if per_node_dur is None else float(per_node_dur)
        else:
            charger_capacity = 0
            charge_duration_min = None

        # Known keys consumed above; everything else flows into free-form attrs.
        attrs = {
            k: v for k, v in raw.items()
            if k not in ("id", "sequence", "role", "charger")
        }

        nodes.append(
            Node(
                id=node_id,
                sequence=sequence,
                role=str(raw.get("role", "station")),
                charger_capacity=charger_capacity,
                charge_duration_min=charge_duration_min,
                attrs=attrs,
            )
        )

    node_ids = {n.id for n in nodes}

    # -- segments ----------------------------------------------------------- #
    raw_segments = d.get("segments") or []
    segments: List[Segment] = []
    for raw in raw_segments:
        if "from" not in raw or "to" not in raw or "distance_km" not in raw:
            raise ValueError(
                f"world: every segment needs 'from', 'to', 'distance_km' (got {raw!r})"
            )
        frm = str(raw["from"])
        to = str(raw["to"])
        if frm not in node_ids:
            raise ValueError(f"world: segment endpoint {frm!r} is not a known node")
        if to not in node_ids:
            raise ValueError(f"world: segment endpoint {to!r} is not a known node")
        segments.append(Segment(frm=frm, to=to, distance_km=float(raw["distance_km"])))

    if not segments:
        raise ValueError("world: 'segments' is required to connect the nodes")

    # -- operators ---------------------------------------------------------- #
    raw_operators = d.get("operators") or []
    operators: List[Operator] = []
    seen_ops: set[str] = set()
    for raw in raw_operators:
        if "id" not in raw:
            raise ValueError(f"world: every operator needs an 'id' (got {raw!r})")
        op_id = str(raw["id"])
        if op_id in seen_ops:
            raise ValueError(f"world: duplicate operator id {op_id!r}")
        seen_ops.add(op_id)
        operators.append(Operator(id=op_id, name=str(raw.get("name", ""))))

    data = d.get("data")
    world_id = str(d.get("world_id", "world"))

    # World.__init__ performs the final check that consecutive-by-sequence
    # nodes are actually connected by a segment (and computes positions).
    return World(
        nodes=nodes,
        segments=segments,
        physics=physics,
        operators=operators,
        data=dict(data) if isinstance(data, Mapping) else None,
        world_id=world_id,
    )


def load_world(path) -> World:
    """Parse ``world.yaml`` at ``path`` into a validated :class:`World`."""
    return _world_from_dict(_read_yaml(Path(path)))


# --------------------------------------------------------------------------- #
# Scenario construction                                                       #
# --------------------------------------------------------------------------- #
def load_scenario(path, world: Optional[World] = None) -> Scenario:
    """Parse a scenario file into a validated :class:`Scenario`.

    Resolution of the World:
      * If ``world`` is given, it is used as-is (no patching).
      * Otherwise the scenario's ``world_ref`` (a path *relative to the scenario
        file's directory*) is loaded. If the scenario carries a ``world_patch``
        mapping, it is deep-merged over the world's raw dict *before* the World
        is built, so a scenario can tweak the physical world locally.

    Validation: every bus's ``origin``/``destination`` must be known nodes and
    its ``operator`` must be a known operator id; otherwise ``ValueError``.
    """
    scenario_path = Path(path)
    raw = _read_yaml(scenario_path)

    # --- resolve the World ------------------------------------------------- #
    if world is None:
        world_ref = raw.get("world_ref")
        if not world_ref:
            raise ValueError(f"{scenario_path}: missing 'world_ref' and no world supplied")
        world_path = (scenario_path.parent / str(world_ref)).resolve()
        world_dict = _read_yaml(world_path)

        patch = raw.get("world_patch")
        if patch:
            if not isinstance(patch, Mapping):
                raise ValueError(f"{scenario_path}: 'world_patch' must be a mapping")
            world_dict = _deep_merge(world_dict, patch)

        world = _world_from_dict(world_dict)

    # --- weights (verbatim — no code-side defaults injected) --------------- #
    raw_weights = raw.get("weights") or {}
    if not isinstance(raw_weights, Mapping):
        raise ValueError(f"{scenario_path}: 'weights' must be a mapping")
    weights: Dict[str, float] = {str(k): float(v) for k, v in raw_weights.items()}

    station_policy = str(raw.get("station_policy", "max_reach"))

    raw_data = raw.get("data") or {}
    if not isinstance(raw_data, Mapping):
        raise ValueError(f"{scenario_path}: 'data' must be a mapping")
    data: Dict[str, Any] = dict(raw_data)

    # --- buses ------------------------------------------------------------- #
    known_ops = {op.id for op in world.operators}
    raw_buses = raw.get("buses") or []
    buses: List[Bus] = []
    seen_bus_ids: set[str] = set()
    for rb in raw_buses:
        for req in ("id", "operator", "origin", "destination", "departure"):
            if req not in rb:
                raise ValueError(f"{scenario_path}: bus {rb!r} is missing required field {req!r}")

        bus_id = str(rb["id"])
        if bus_id in seen_bus_ids:
            raise ValueError(f"{scenario_path}: duplicate bus id {bus_id!r}")
        seen_bus_ids.add(bus_id)

        operator = str(rb["operator"])
        origin = str(rb["origin"])
        destination = str(rb["destination"])

        if operator not in known_ops:
            raise ValueError(
                f"{scenario_path}: bus {bus_id!r} has unknown operator {operator!r} "
                f"(known: {sorted(known_ops)})"
            )
        if not world.has_node(origin):
            raise ValueError(f"{scenario_path}: bus {bus_id!r} has unknown origin {origin!r}")
        if not world.has_node(destination):
            raise ValueError(
                f"{scenario_path}: bus {bus_id!r} has unknown destination {destination!r}"
            )
        if origin == destination:
            raise ValueError(
                f"{scenario_path}: bus {bus_id!r} has identical origin and destination {origin!r}"
            )

        departure = hhmm_to_min(rb["departure"])
        attrs = dict(rb.get("attrs") or {})

        buses.append(
            Bus(
                id=bus_id,
                operator=operator,
                origin=origin,
                destination=destination,
                departure=departure,
                attrs=attrs,
            )
        )

    scenario_id = str(raw.get("scenario_id", "") or scenario_path.stem)
    name = str(raw.get("name", "") or scenario_path.stem)

    return Scenario(
        name=name,
        weights=weights,
        buses=buses,
        world=world,
        station_policy=station_policy,
        data=data,
        scenario_id=scenario_id,
    )


# --------------------------------------------------------------------------- #
# Discovery                                                                   #
# --------------------------------------------------------------------------- #
def list_scenarios(directory) -> List[Tuple[str, str]]:
    """List ``scenario_*.yaml`` files in ``directory``, sorted by filename.

    Returns ``[(display_label, absolute_path_str), ...]`` where ``display_label``
    is the scenario's ``name`` field (falling back to the filename stem if the
    file has no ``name`` or cannot be parsed).
    """
    base = Path(directory)
    results: List[Tuple[str, str]] = []
    for path in sorted(base.glob("scenario_*.yaml"), key=lambda p: p.name):
        try:
            raw = _read_yaml(path)
            label = str(raw.get("name") or path.stem)
        except Exception:
            label = path.stem
        results.append((label, str(path.resolve())))
    return results
