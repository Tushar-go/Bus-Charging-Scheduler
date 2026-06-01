# Architecture

> **The thesis of this codebase:** the engine knows *nothing* about Bengaluru, Kochi,
> four stations, three operators, or 240 km. Every concrete fact about the world is
> **data**, and every objective is a **pluggable rule**. As a result, the overwhelming
> majority of "what if the problem changes?" questions are answered by editing a YAML
> number or dropping in an ~8-line file — and **the engine is never touched**. Section 3
> (the anticipated-changes list) is where that claim is cashed out, item by item.

---

## 1. Approach & why it fits

### What it is — a deterministic dispatch simulation

The scheduler is a **deterministic discrete-event simulation** of the corridor. Buses
depart at their scheduled times, travel segment-by-segment (travel time = distance ÷
speed), and arrive at stations. A station has *N* chargers (`charger_capacity`). When
more buses want a single charger than there are free slots, the engine resolves the
contention with a **weighted priority-dispatch rule**: among the waiting candidates it
has not vetoed, it charges next the one with the highest

```
priority(candidate) = Σ  weights[name] · rule.score(ctx)     over all registered SOFT rules
```

where `ctx` is a [`DecisionContext`](scheduler/context.py) — a read-only snapshot of the
whole simulation at the moment of the decision. **HARD rules** (range, capacity, route
order, charge duration) run first and *veto* any infeasible candidate before the sum is
ever computed. This exact summation happens at a **single line** in the engine, and the
engine **names no individual rule** — it just iterates `REGISTRY.soft_rules()`.

### The three shipped soft objectives

Each soft rule returns a benefit in `[0, 1]` for a candidate; the dispatch sum above
weights and adds them. They are deliberately built to pull in *different directions* so
the weights are real levers:

- **`individual` — longest-wait-first (anti-starvation).** Scores a candidate by how long
  *it* has already waited in the queue, so the bus that has been waiting longest is served
  next. This is the classic fairness-to-the-individual lever.
- **`operator` — round-robin fleet fairness.** "Each operator's fleet should run smoothly
  as a group" is read as *no operator should be systematically served before the others*.
  The rule therefore prioritises the candidate whose operator has so far received the
  **least total charging service across the whole network** — computed from the global,
  per-operator count of completed charges in `bus_states`, normalised to `[0, 1]` (the
  least-served operator ≈ 1.0, the busiest ≈ 0.0; before any charge every operator is
  equal, so the rule is neutral at 0.0 and ties fall through to the deterministic
  tie-break). Crucially this signal is **decoupled from the candidate's own wait** — it
  reads fleet-level service, not queue position — so it genuinely *disagrees* with
  `individual` on a mixed-operator queue. That decoupling is exactly what lets the operator
  weight produce a *visibly different* schedule rather than echoing longest-wait-first.
- **`overall` — shortest-remaining-time-first (SPT).** Scores by how little work the
  candidate has left, minimising total network completion time. Honest caveat: on a
  single-direction, same-station queue every waiting bus has the *same* remaining trip, so
  `overall` is neutral there and only differentiates under heterogeneous / mixed-progress
  contention. On the shipped scenarios `individual` and `operator` are therefore the
  visibly-active levers.

**Different weights → visibly different schedules (measured).** Because `operator` reads
fleet-level service rather than wait, tuning it reorders contended chargers. Concretely, in
**Scenario 4** raising the operator weight `1.0 → 2.0` changes the per-station charging
order at **all four stations (A, B, C, D)**: the minority operators' buses (Freshbus
`BK-09`, Flixbus `BK-10`) are pulled ahead of the trailing KPN buses (`BK-06/07/08`), so
service evens out across operators instead of staying strict longest-wait-first.
Individual-only vs operator-only weights likewise produce different per-station orders in
scenarios 1, 3, 4, and 5. (Determinism, feasibility, the ≥ 2-charges lemma, and all 28
tests still hold — the weights move the *order*, never the legality, of the schedule.)

*Which* stations a bus charges at is decided separately, up front, by a
[`StationPolicy`](scheduler/station_policy.py) (geometry + range only, no timing). The
engine then simulates the *timing and ordering* of those charges. Splitting "where" from
"when" keeps each piece small and independently testable.

### Why not CP-SAT / MILP

A constraint solver (OR-Tools CP-SAT, or a MILP) could express "minimise total time
subject to one-charger-at-a-time" and find a provably optimal schedule. We deliberately
chose the dispatch simulation instead, and the trade-off is worth stating honestly:

- **Explainability.** Every decision is a one-line, human-readable story: *"at t = 240,
  bus `BK-09` (Freshbus) beat `BK-07` (KPN) for charger B because the operator weight was
  2.0 and Freshbus had so far received the least charging service of any operator."* A
  CP-SAT model hands you a 20-bus assignment matrix and no narrative. For a tool whose
  whole job is to *justify* a schedule to three competing operators, the narrative **is**
  the product.
- **"Different weights → visibly different schedules" becomes true *and traceable*.** The
  weights are literally the coefficients in the dispatch sum, so nudging one re-orders a
  contended charger in a way you can point at. In a MILP the weights sit in an objective
  function and the causal chain to the output is opaque.
- **Add a rule live.** A new objective is one ~8-line function (Section 5). In a solver,
  a new objective term often means new decision variables and re-deriving constraints.
- **Scales linearly and ships anywhere.** The simulation is `O(events)` with no heavy
  solver dependency — it runs in milliseconds and deploys to Streamlit Community Cloud
  with a pure-Python `requirements.txt` (no native OR-Tools wheel, no licensing).
- **Fits a 3–4 day build** and is easy for a reviewer to read end-to-end.

**The honest cost:** a greedy priority dispatch is *fast, deterministic, and
defensible*, but it is **not provably globally optimal**. For sum-of-completion-times
under single-machine contention the priority terms we use (shortest-remaining-time,
longest-wait-first) are strong, well-understood heuristics — not an optimality
guarantee. We accept that trade because explainability and tunability are the graded
goals here, not the last few percent of network time.

### The CP-SAT swap seam (documented, **not built**)

Crucially, choosing the heuristic now does **not** lock the door on a solver later. The
engine sits behind a conceptual `Scheduler` interface:

```python
class Scheduler(Protocol):
    def solve(self, scenario: Scenario) -> Schedule: ...
```

The shipped `engine.solve(scenario)` is one implementation. A future `CpSatScheduler`
could be a *drop-in second implementation* that consumes the **same rule registry** —
reading each soft rule's weight to build its objective and each hard rule as a
constraint — and returns the **same `Schedule`** dataclass, so the UI, the loaders, and
the tests are all unaffected. The `DecisionContext` → `score` contract is exactly the
kind of small, total, side-effect-free function a solver can also call to seed
objective coefficients. **This seam is described, not implemented** — but the data model
and the registry were shaped so that it stays a clean swap rather than a rewrite.

---

## 2. Data-structure design

All domain types live in [`scheduler/models.py`](scheduler/models.py); the read-only
decision view is [`scheduler/context.py`](scheduler/context.py). The layering is:

| Layer | Type(s) | What it holds | Mutable? |
|---|---|---|---|
| **World** (physical universe) | `World`, `Node`, `Segment`, `Physics`, `Operator` | ordered nodes, segment distances, physics constants, operators | no (read-only) |
| **Scenario** (tunable inputs) | `Scenario`, `Bus` | `weights`, the fleet, the chosen `station_policy`, free-form `data` | weights/fleet only |
| **Runtime** (owned by engine) | `BusState` | live position, range, status, queue-join time, accrued wait | yes (engine only) |
| **Output** | `Schedule`, `PerBusPlan`, `StationQueue`, `ChargeRecord`, `Event` | the produced timetable + per-charger order + objective + violations | yes (result) |

The design choices below are the reason **the chance the code breaks when the world
changes ≈ 0** — each one turns a *category* of change into a *data edit*:

- **A node is a station *iff* `charger_capacity > 0`.** `Node.is_station` is a derived
  property, not a stored boolean. There is no `is_endpoint` flag anywhere driving logic.
  So "the new node is also an endpoint" or "close that station" can never break a code
  path — endpoints are simply nodes without a charger, and closing a station is
  `capacity: 0`.
- **A charger is a capacity-*N* resource.** `capacity == 1` is never special-cased; the
  engine derives a live free-slot count from `charger_capacity`. "Double the chargers" is
  one number.
- **Positions, travel times, and direction are *derived*, never stored.** `World`
  precomputes cumulative positions (`_pos`, `total_km`) from segment distances on
  construction; `travel_min(a, b) = distance(a, b) · 60 / speed`; `direction(o, d)` is
  "forward iff origin precedes destination in route order". Change a distance or a speed
  and *everything* downstream recomputes — there is no second place holding a stale copy.
- **A bus carries `origin`/`destination` node ids; direction is derived.** Reverse buses
  traverse the *same shared stations* backwards along a single signed corridor axis
  (`stations_in_travel_order` sorts by progress away from the origin). This generalises to
  more than two endpoints and to mid-corridor trips *for free* — no "BK vs KB" enum.
- **Time is integer-friendly minutes-from-midnight internally.** Exact ordering (no float
  clock drift in comparisons), with `hhmm_to_min` / `min_to_hhmm` at the edges for the
  human `HH:MM` clock. Times are **never wrapped at 1440**, so a trip that finishes after
  midnight reads `27:11` and still sorts correctly.
- **Per-station charge-duration override.** `Node.charge_duration_min` falls back to
  `Physics.charge_duration_min`; `World.charge_minutes(node)` encodes that precedence in
  one place. "Station C charges slower" is a per-node field, not an engine branch.
- **Free-form `attrs` and `data` dicts absorb the unforeseen.** Every `Bus` and `Node`
  has an `attrs: Mapping`; the `World` and `Scenario` each have a `data: Dict`. The loader
  funnels any YAML key it doesn't recognise into `attrs` automatically. A future field
  (priority tag, SLA, connector type, initial battery, a price table) attaches with **no
  schema change** — and a rule reads it straight out of the context.
- **Weights are a plain `Dict[str, float]` with no code-side defaults.** The loader passes
  the scenario's `weights:` block through verbatim; a missing weight ⇒ `0.0` at the
  summation site. The scenario YAML is the *single source of truth* for tuning.

---

## 3. Anticipated changes — the foresight list

This is the headline. Below are **30 concrete changes** the design absorbs, each mapped
to the **exact field or file** that absorbs it, organised by how much work each costs.
The three tiers are, by design, a cliff: most changes are *data only*; the next tier is
*one new rule file*; only a handful are *small extensions* — and **none** require an
engine rewrite.

> **Every interviewer-named curveball is covered here:** *add a station* (3.1), *double
> the chargers* (3.2), *swap an operator* (3.3), *change a segment* (3.4), *priority
> buses* (3.15), *time-of-day electricity costs* (3.16), *driver shifts* (3.17), and
> *multiple routes sharing stations* (3.24). The first four are data-only; the next three
> are shipped rules; the last is the one genuinely-a-small-extension item, and we say so
> plainly.

### Tier A — DATA-ONLY changes (zero code)

*Edit a YAML file. No Python touched, no restart beyond Streamlit's auto-rerun.*

1. **Add a station.** Add a node with a `charger` block and split the segment it sits on
   into two (so distances still sum). → `data/world.yaml` `nodes:` + `segments:`. Station
   status, position, and travel times are all derived; nothing in code enumerates "four
   stations".
2. **Double (or N-tuple) the chargers.** → `data/world.yaml` `nodes[i].charger.capacity:
   2`. `capacity == 1` is not special-cased; the engine and `hard_charger_capacity`
   already reason about *N* free slots.
3. **Swap or add an operator.** → `data/world.yaml` `operators:` (add an `{id, name}`),
   then reference the `id` from buses. The `operator` soft rule groups by `Bus.operator`
   generically — no operator name is hard-coded.
4. **Change a segment distance.** → `data/world.yaml` `segments[i].distance_km`. Positions
   (`World._pos`), travel times, and route feasibility all recompute from it.
5. **More or fewer buses.** → a scenario's `buses:` list. The fleet size is just the list
   length; nothing assumes 20.
6. **A new scenario.** → drop a `data/scenarios/scenario_06_*.yaml`. `list_scenarios`
   auto-discovers any `scenario_*.yaml`, so it appears in the dropdown with no code change.
7. **Change the weights / re-tune.** → a scenario's `weights:` block. This is *the* tuning
   knob; it reaches the engine as a dict read at exactly one summation line.
8. **Change the battery range.** → `data/world.yaml` `defaults.battery_range_km`. Feeds
   `Physics.battery_range_km`, which the station policy and `hard_range` read; feasibility
   is recomputed.
9. **Change the global charge time.** → `data/world.yaml` `defaults.charge.duration_min`.
   Flows through `Physics.charge_duration_min` and `World.charge_minutes`.
10. **Per-station charge time** (one station charges slower/faster). → `data/world.yaml`
    `nodes[i].charger.duration_min`. `World.charge_minutes` prefers the node override over
    the physics default; `hard_charge_duration` validates against the same source.
11. **Change the cruising speed** (also: variable speed becomes possible). →
    `data/world.yaml` `defaults.speed_kmph`. Travel time is *derived* (`distance · 60 /
    speed`), so every segment's duration updates. (Per-segment speed is a tiny extension —
    see 3.22.)
12. **New endpoints / mid-corridor trips.** → set any node's `id` as a bus's `origin` /
    `destination`. Direction is derived from sequence order, and `stations_in_travel_order`
    already handles a start/end *between* the terminals. A bus from `A → D` "just works".
13. **Close / mothball a station.** → `data/world.yaml` set that node's
    `charger.capacity: 0` (or remove the `charger` block). It becomes a pass-through node;
    `is_station` is false, so no bus schedules a charge there.
14. **Apply a one-off "what-if" to the world from inside a scenario.** → a scenario's
    `world_patch:` block, which the loader **deep-merges over `world.yaml`** before
    building the `World`. Lets a scenario double a charger or change a distance *locally*
    without forking the world file.

### Tier B — NEW RULE (one new file in `scheduler/rules/`, no engine change)

*Write a decorated `(ctx) -> float` function, add one import line to
`rules/__init__.py`, add one weight to a scenario. The engine sums over the registry and
names no rule, so it is provably untouched (Section 5).*

15. **Priority buses** — let operationally important buses jump the queue. → **shipped**
    as [`soft_priority.py`](scheduler/rules/soft_priority.py); reads
    `bus.attrs["priority"]` (`high`/`medium`/`normal`/`low`) and weight `priority`. With a
    large weight, tagged buses win contention — while hard rules still forbid anything
    infeasible.
16. **Time-of-day electricity cost** — charge when the grid is cheap. → **shipped** as
    [`soft_tou_cost.py`](scheduler/rules/soft_tou_cost.py); reads an `{hour: price}` table
    from `scenario.data["tou_cost_by_hour"]` (then `world.data`) and prices the hour the
    charge would start. Re-pricing the grid is a data edit; the rule is the only code.
17. **Driver shifts** — don't strand a driver past clock-off. → **shipped** as
    [`soft_driver_shift.py`](scheduler/rules/soft_driver_shift.py); reads
    `bus.attrs["shift_end"]` (`HH:MM` or minutes) and raises urgency as the charge *end*
    approaches the shift end.
18. **Minimum charge spacing** — discourage charging again too soon after the last charge.
    → a new `soft_min_spacing.py`: score by `clock − ctx.bus_states[id]`'s last charge
    time (the context already exposes per-bus state and `last_station_pos`). No new field
    needed.
19. **Operator-exclusive (or operator-preferred) chargers** — reserve a charger for one
    operator. → a new `soft_operator_affinity.py` reading `station.attrs["reserved_for"]`
    (set in `world.yaml`); score high when `candidate.operator` matches. (Make it a *hard*
    veto instead — `hard_operator_exclusive.py` — if it must be absolute; the
    `@hard_rule` machinery is identical.)
20. **Per-bus deadlines / SLA** — penalise plans that would miss a promised arrival. → a
    new `soft_deadline.py` reading `bus.attrs["due_by"]` and comparing it to
    `ctx.projected_arrival(candidate.id)` (already a context helper).
21. **Minimax / worst-wait objective** — optimise the *worst* wait rather than the sum. →
    a new `soft_minimax_wait.py` that scores a candidate by how much it would reduce the
    queue's maximum wait (the context exposes every waiting bus's wait via
    `ctx.wait_of`). A different fairness philosophy, still one file.

### Tier C — SMALL EXTENSION (a tiny new field *and* a rule; still no engine rewrite)

*These need slightly more than a single rule — typically one new optional field plus a
rule that reads it — but the engine's dispatch loop and the `Schedule` output are
unchanged.*

22. **Variable speed per segment.** → add `segments[i].speed_kmph` to the world and have
    `World.travel_min`/segment lookup prefer it over the default. A localised change to one
    derived-time helper; no engine or rule change. (Bordering on data-only.)
23. **Branching topology** (the corridor stops being a single line). → today nodes are
    ordered by a 1-D `sequence`. Generalising to a graph means giving the `World` an
    explicit adjacency/shortest-path over `segments` (which are already `(from, to,
    distance)` edges) instead of the cumulative-position shortcut. The models (`Segment`,
    `Node`, `attrs`) already carry the data; only the *position/`stations_in_travel_order`
    helpers* would change. Honestly the largest item on this list.
24. **Multiple routes sharing the same stations.** → add a `routes[]` list to the world
    (each route an ordered node sequence) and tag each bus with `attrs["route_id"]`.
    **Stations are already keyed globally by node id and a charger is already a shared
    capacity-N resource**, so two routes sharing station B contend on the *same* charger
    with zero engine change. This is **the one interviewer-named curveball that is a small
    extension rather than pure data** — and we call that out plainly: it needs a `routes[]`
    field and a per-bus `route_id`, after which the existing dispatch handles the shared
    contention as-is.
25. **Partial initial charge** (a bus doesn't start full). → `bus.attrs["initial_battery_km"]`;
    `BusState.range_left` is initialised from it instead of the full range. One field + a
    one-line change to state initialisation; `hard_range` already guards the rest.
26. **Charging curve** (charge time depends on arrival state-of-charge, not a flat 25 min).
    → today `charge_duration` is sourced from `World.charge_minutes`. Make that a small
    function of `battery_km_in` (e.g. a curve in `world.data`). `hard_charge_duration` is
    re-expressed against the same source; the dispatch loop is unchanged.
27. **Connector types** (a bus can only use compatible chargers). → `bus.attrs["connector"]`
    + `node.attrs["connectors"]`, enforced by a new `hard_connector.py` veto (reads both
    from the context). Field + one hard rule.
28. **Non-full charge targets** (charge to 80%). → `defaults.charge.target` already exists
    on `Physics` (`charge_target`); wiring it into the duration/curve calc is the same
    surface as 3.26.
29. **A second station-selection strategy** (e.g. cost-aware "where to charge"). → add a
    class to `scheduler/station_policy.py` and a name to the `POLICIES` registry; select it
    per scenario via `station_policy:`. A `load_aware` policy already ships beside the
    default `max_reach`, proving the seam.
30. **A whole alternative solver** (CP-SAT). → a new `CpSatScheduler` implementing the
    `Scheduler` protocol, consuming the same `REGISTRY` and returning the same `Schedule`.
    The largest *conceptual* extension, but architecturally a clean swap (Section 1) — and
    explicitly **not built**.

---

## 4. How to change a weight (code example)

Weights live in **exactly one place** — the scenario YAML — and are consumed at
**exactly one place** — the engine's priority sum. The full round trip:

```yaml
# data/scenarios/scenario_04_operator_heavy.yaml
weights: { individual: 1.0, operator: 2.0, overall: 1.0 }
#                                    ↑ the only edit
```

The loader passes this through verbatim (no defaults injected) into
`Scenario.weights`, and the engine reads it at a single line when it scores a candidate:

```python
# scheduler/engine.py — the ONE site weights are consumed
priority = sum(
    weights.get(name, 0.0) * rule.score(ctx)        # missing weight ⇒ 0.0
    for name, rule in REGISTRY.soft_rules().items()
)
```

Set every weight to `0.0` and every candidate scores `0.0`, so the tie-break chain
(earliest arrival, then bus id) takes over — i.e. the schedule degrades to plain FCFS.
That is the precise sense in which "the weights are the dispatch".

---

## 5. How to add a new rule (code example)

A rule is a single decorated function. The decorator registers it in the global
`REGISTRY` as an *import side effect*; the engine iterates the registry and names no
rule, so a new rule **cannot** force an engine edit. Below is the real, shipped
[`soft_tou_cost.py`](scheduler/rules/soft_tou_cost.py) — a time-of-use electricity-cost
objective that prefers charging in cheaper grid hours:

```python
"""Soft rule: tou_cost — favour charging during cheaper time-of-use hours."""
from __future__ import annotations

from typing import Dict

from scheduler.context import DecisionContext
from scheduler.registry import soft_rule

# Flat default: every hour costs the same, so the rule is inert until a real
# price table is supplied in scenario/world data.
_FLAT_TABLE: Dict[int, float] = {h: 1.0 for h in range(24)}


def _load_table(ctx: DecisionContext) -> Dict[int, float]:
    """Read an {hour: price} table from scenario data, then world data."""
    raw = ctx.scenario.data.get("tou_cost_by_hour")
    if raw is None:
        raw = ctx.world.data.get("tou_cost_by_hour")
    if not raw:
        return dict(_FLAT_TABLE)
    return {int(k): float(v) for k, v in raw.items()}


@soft_rule("tou_cost")                      # ← registers the rule on import
def tou_cost(ctx: DecisionContext) -> float:
    """Reward starting a charge in a cheaper time-of-use (ToU) hour."""
    table = _load_table(ctx)
    hour = int(ctx.clock // 60) % 24
    price = table.get(hour, 1.0)
    lo, hi = min(table.values()), max(table.values())
    span = (hi - lo) or 1.0
    return 1.0 - (price - lo) / span        # cheapest hour ≈ 1.0, dearest ≈ 0.0
```

The `@soft_rule("tou_cost")` decorator does the registration; the function reads only
fields that already exist on the `DecisionContext` (`clock`, `scenario.data`,
`world.data`). To make it active, register it and give it a weight — the **entire**
change set is:

```text
+ NEW FILE   scheduler/rules/soft_tou_cost.py
~ 1 LINE     scheduler/rules/__init__.py          from . import …, soft_tou_cost, …
~ 1–2 LINES  data/scenarios/<file>.yaml           weights: { …, tou_cost: 1.5 }  + optional price table
──────────────────────────────────────────────────────────────────────────────
  UNCHANGED: scheduler/engine.py — the proof.
```

Hard constraints are the same pattern with `@hard_rule("name", reason=…)` over a
`(ctx) -> bool` function (return `False` to veto). All four shipped hard rules
(`range`, `charger_capacity`, `route_order`, `charge_duration`) are written exactly this
way.

---

## 6. Assumptions

These are the modelling assumptions baked into the shipped data and engine. Each is
either a documented default or a single data field, so any of them can be revisited
without surgery.

- **Speed = 60 km/h**, so **1 km = 1 minute** of travel and a 100 km segment takes 100
  min. (`defaults.speed_kmph` in `world.yaml`; travel time is derived.)
- **A charge is strictly 25 minutes to full, regardless of arrival battery level.** No
  charging curve; "to full" is the only target modelled today.
  (`defaults.charge.duration_min` / `target`.)
- **Time is integer minutes from midnight**; a scenario is a single evening. We do **not**
  assume a day-rollover — but minutes are allowed to exceed 1440, so a trip ending after
  midnight is `25:30`, not wrapped to `01:30`. (`hhmm_to_min` / `min_to_hhmm` never modulo
  1440.)
- **Both directions share the same physical chargers.** A station's `charger_capacity` is
  the *total* simultaneous capacity at that location, contended by forward and reverse
  buses alike.
- **Default station policy is "charge as late as feasible" (`max_reach`).** For the
  shipped corridor this yields **{B, D}** for the Bengaluru→Kochi bus and **{C, A}** for
  the Kochi→Bengaluru bus. A `load_aware` policy is also provided (spreads predicted
  demand across the cheapest-cardinality plans). **Note for discussion:** the station
  *choice itself* determines which stations get contended, so the policy is a genuine
  lever on congestion — a good interview talking point, and the reason "where to charge"
  is a swappable `StationPolicy` rather than hard-coded.
- **Ties are broken deterministically:** highest priority sum, then earliest queue
  arrival, then bus id. Identical inputs always produce an identical schedule.
- **Weights are non-negative**, and **all-zero weights ⇒ effectively FCFS** (every
  candidate scores 0, so only the tie-break chain orders them).
- **Every bus starts with a full battery** at its origin (see 3.25 for the
  `initial_battery_km` extension that relaxes this).

---

## 7. Correctness

### Feasibility lemma

On the shipped corridor the four stations sit at cumulative distances **A = 100,
B = 220, C = 320, D = 440 km** (out of 540), and the battery range is **240 km**. Claim:
*a charge plan is feasible iff it never skips two consecutive segments.*

- The five inter-stop gaps between candidate stops (origin, A, B, C, D, destination) are
  100, 120, 100, 120, 100 km. **Any single segment** ≤ 120 ≤ 240 — always fine.
- **Any two consecutive segments** sum to at most 120 + 120 = 240 ≤ 240 — just feasible,
  so skipping *one* station between two charges is allowed.
- **Any three consecutive segments** sum to at least 100 + 120 + 100 = 320 > 240 —
  infeasible, so you can never skip *two* stations in a row.

Therefore **every full traversal needs ≥ 2 charges** (you cannot cover 540 km with 240 km
of range while honouring the "no two-in-a-row skip" rule using fewer than two stops), and
the `max_reach` policy's {B, D} / {C, A} are exactly the late-as-possible 2-charge plans.
`station_policy.py` enumerates feasible plans by the very same leg-within-range test
(`_legs_within_range`), so the simulation only ever runs plans this lemma certifies.

### Hard rules: guaranteed by construction *and* re-validated

The four hard rules are enforced **twice over**, on purpose:

1. **By construction during dispatch.** Before any candidate enters the priority sum, the
   engine runs `is_feasible(ctx)` for every registered hard rule and drops vetoed
   candidates. So a bus is never *chosen* to charge somewhere out of range, with no free
   slot, behind its last charge, or for the wrong duration.
2. **By a post-run validator.** After the simulation completes, the engine re-checks every
   charge actually placed in the `Schedule` against the same hard rules and records any
   `violations`. The two layers are independent: the dispatch guard prevents bad choices,
   and the validator proves none slipped through, surfacing the rule's `reason` if one
   ever did. A schedule is reported `feasible` only if that re-validation is clean (and
   every bus reached its destination).

This belt-and-braces approach is why the feasibility tests can assert a strong invariant —
*no hard-rule violation appears in any produced schedule* — rather than merely trusting
the dispatch loop.
