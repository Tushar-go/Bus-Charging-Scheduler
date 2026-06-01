# Bus Charging Scheduler

A small Streamlit app that schedules charging for a fleet of electric buses on the
**Bengaluru → A → B → C → D → Kochi** corridor (540 km, five segments of
100 / 120 / 100 / 120 / 100 km). Buses run in both directions and share four
single-charger stations (A, B, C, D). Each bus has a 240 km battery and every charge
takes a fixed 25 minutes to full. The scheduler decides **which stations each bus
charges at** and **the order buses use each charger**, optimising three *tunable* soft
objectives — keep any one bus from waiting too long (**individual**), keep each
operator's fleet running smoothly (**operator**), and keep total network time low
(**overall**).

The whole point of the design is that the *rules are pluggable* and the *world is
data*: you change behaviour by editing a YAML number or dropping in an ~8-line rule
file — never by rewriting the engine. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the
full rationale and the (heavily-detailed) list of changes the design absorbs.

**Live app:** `https://bus-charging-scheduler-bctva8hpcnz6epvjkna88q.streamlit.app/`

---

## Run locally

Requires **Python 3.11+** (3.12 recommended).

```bash
# 1. create & activate a virtual environment
python -m venv .venv
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 2. install dependencies
pip install -r requirements.txt

# 3. run the app
streamlit run app.py
```

Streamlit prints a local URL (default `http://localhost:8501`); open it in a browser.

---

## Using the app

1. **Pick a scenario** from the dropdown in the sidebar. The five scenarios live in
   `data/scenarios/` and are discovered automatically (any `scenario_*.yaml` file
   shows up — add one and it appears in the list).
2. The schedule is computed instantly and shown across three tabs:
   - **Scenario input** — the fleet you fed in: every bus's operator, origin →
     destination, direction, departure time, and the active `weights`. This is the
     "what did I ask for" view.
   - **Per-bus timetable** — one row per bus: its chosen charge stations, departure
     and arrival (`HH:MM`), total waiting time, and the full event trace
     (depart → travel → arrive → wait → charge → … → arrive destination).
   - **Per-station order** — for each charger (A, B, C, D), the exact sequence buses
     used it: who charged when, which operator, and how long each waited. This is the
     view that makes "different weights → different order" visible.

---

## How to change a weight

Weights are the **only** tuning knob, and they live in **exactly one place**: the
`weights:` block of the scenario YAML. There are no weight defaults baked into the
code — the scenario is the single source of truth (a missing weight is simply treated
as `0.0`).

Open `data/scenarios/<file>.yaml` and edit one number. For example, to make the
operator-fairness objective twice as strong:

```diff
 # data/scenarios/scenario_04_operator_heavy.yaml
-weights: { individual: 1.0, operator: 1.0, overall: 1.0 }
+weights: { individual: 1.0, operator: 2.0, overall: 1.0 }
```

Save the file, then **re-pick the scenario** in the dropdown (or refresh). The
per-station order visibly re-shuffles toward **round-robin fleet fairness** — the
buses of the least-served operators are pulled ahead so charging service evens out
across operators instead of strict longest-wait-first. On Scenario 4 this single
edit (operator `1.0 → 2.0`) reorders the charging order at **all four stations**
(A, B, C, D): the minority operators' buses (Freshbus `BK-09`, Flixbus `BK-10`) are
served ahead of the trailing KPN buses (`BK-06/07/08`) — a clean thing to show a
reviewer. That is the entire workflow — no code, no restart of anything but the
re-run Streamlit triggers for you.

---

## How to add a new rule in 60 seconds

A rule is a single decorated function that, given the decision the engine is about to
make, returns a number. The engine sums `weight × score` over **every registered soft
rule** and never names any rule individually — so adding one **cannot** require an
engine change. Here is a complete, real example: a *time-of-use electricity cost* rule
that prefers charging during cheaper grid hours.

**Step 1 — create the file** `scheduler/rules/soft_tou_cost.py`:

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


@soft_rule("tou_cost")
def tou_cost(ctx: DecisionContext) -> float:
    """Reward starting a charge in a cheaper time-of-use (ToU) hour."""
    table = _load_table(ctx)
    hour = int(ctx.clock // 60) % 24
    price = table.get(hour, 1.0)
    lo, hi = min(table.values()), max(table.values())
    span = (hi - lo) or 1.0
    return 1.0 - (price - lo) / span        # cheapest hour ≈ 1.0, dearest ≈ 0.0
```

**Step 2 — register it** with one import line in `scheduler/rules/__init__.py`:

```diff
 from . import soft_individual, soft_operator, soft_overall          # core soft objectives
 from . import hard_range, hard_charger_capacity, hard_route_order, hard_charge_duration
-from . import soft_priority, soft_driver_shift                      # flex demo rules
+from . import soft_priority, soft_tou_cost, soft_driver_shift       # flex demo rules
```

**Step 3 — turn it on** by adding one weight (and, optionally, a price table) to a
scenario:

```diff
 # data/scenarios/scenario_05_peak.yaml
-weights: { individual: 1.0, operator: 1.0, overall: 1.0 }
+weights: { individual: 1.0, operator: 1.0, overall: 1.0, tou_cost: 1.5 }
+data:
+  tou_cost_by_hour: { 19: 8.0, 20: 8.0, 21: 5.0, 22: 3.0, 23: 3.0 }
```

That's it. The annotated diff in full:

```text
+ NEW FILE   scheduler/rules/soft_tou_cost.py        (the rule — ~25 lines)
~ 1 LINE     scheduler/rules/__init__.py             (one import, registers on load)
~ 2 LINES    data/scenarios/scenario_05_peak.yaml    (one weight + a price table)
──────────────────────────────────────────────────────────────────────────────
  UNCHANGED: scheduler/engine.py — the proof.
```

The engine is untouched because it only ever does `sum(weights[name] * rule.score(ctx))`
over whatever the registry holds. The `DecisionContext` already exposes the clock, the
scenario data, the world, and the candidate bus — so a brand-new objective is *just
arithmetic over fields that already exist*. (This rule already ships in the repo; the
walkthrough above mirrors the real file.)

---

## How to grow the world

The physical universe lives in `data/world.yaml`. All of these are pure data edits —
**no code changes**:

- **Add a station** — add a node (with a `charger` block) and split the segment it
  sits on into two so distances still sum correctly. A node is a station *iff* it has a
  charger with `capacity > 0`; positions and travel times are derived from segment
  distances, so the new station "just works".
  ```yaml
  nodes:
    - { id: E, sequence: 6, role: station, charger: { capacity: 1 } }
  segments:
    - { from: D, to: E,     distance_km: 60 }   # was D -> KOCHI: 100
    - { from: E, to: KOCHI, distance_km: 40 }   # 60 + 40 = the original 100
  ```
- **Double the chargers at a station** — change one number:
  ```yaml
  - { id: B, sequence: 2, role: station, charger: { capacity: 2 } }
  ```
  Capacity-1 is never special-cased, so `capacity: 2` (or `4`) means two (or four)
  buses charge in parallel there.
- **Add or swap an operator** — add a line under `operators:`; reference its `id` from
  any bus.
  ```yaml
  operators:
    - { id: greenline, name: GreenLine }
  ```
- **Change a segment distance** — edit `distance_km`. Station positions, travel times,
  and feasibility are all recomputed from it.
- **Close a station** — set `charger.capacity: 0` (or drop the `charger` block); the
  node becomes a pass-through and no bus schedules a charge there.

For *fleet*-level changes (more/fewer buses, new departure times, priority tags) edit a
scenario file in `data/scenarios/`. For a one-off "what if" you can tweak the world from
*inside* a scenario via a `world_patch:` block (deep-merged over `world.yaml`) without
forking the world file.

---

## Project layout

```text
assignment/
├── app.py                          Streamlit UI: scenario dropdown + 3 result tabs
├── requirements.txt                streamlit, pyyaml, pytest
├── README.md                       this file
├── ARCHITECTURE.md                 design rationale + anticipated-changes list
│
├── scheduler/                      the engine package (knows nothing about Bengaluru)
│   ├── __init__.py                 public surface + version
│   ├── models.py                   World / Scenario / Bus / Schedule dataclasses
│   ├── context.py                  DecisionContext — the read-only snapshot rules see
│   ├── registry.py                 REGISTRY + @soft_rule / @hard_rule decorators
│   ├── loader.py                   YAML → models (the only place that knows the YAML shape)
│   ├── station_policy.py           which stations a bus charges at (max_reach / load_aware)
│   ├── engine.py                   discrete-event simulation + weighted priority dispatch
│   └── rules/
│       ├── __init__.py             imports every rule (one line per rule registers it)
│       ├── base.py                 SoftRule / HardRule typing protocols
│       ├── soft_individual.py      no single bus waits too long (anti-starvation)
│       ├── soft_operator.py        round-robin fairness: serve the least-served operator next
│       ├── soft_overall.py         shortest-remaining-time-first (low total time)
│       ├── soft_priority.py        let high-priority buses jump the queue
│       ├── soft_tou_cost.py        prefer cheaper time-of-use electricity hours
│       ├── soft_driver_shift.py    serve buses at risk of running past a driver's shift
│       ├── hard_range.py           veto: never exceed battery range between charges
│       ├── hard_charger_capacity.py veto: never charge with no free charger slot
│       ├── hard_route_order.py     veto: charges must progress, never backtrack
│       └── hard_charge_duration.py veto: a charge must equal the configured duration
│
├── data/
│   ├── world.yaml                  the shared corridor: nodes, segments, physics, operators
│   └── scenarios/
│       ├── scenario_01_even.yaml   even 15-min spacing both ways (gentle baseline)
│       ├── scenario_02_*.yaml      … four more scenarios (peaks, operator-heavy, etc.)
│       └── …
│
└── 
```

---


The suites assert the properties the design promises:

- **Feasibility** — every bus in every shipped scenario reaches its destination, and no
  hard rule (range, charger capacity, route order, charge duration) is ever violated in
  the produced schedule (the engine also re-validates every charge after the run).
- **Determinism** — solving the same scenario twice yields byte-identical schedules
  (stable tie-breaks: priority, then earliest arrival, then bus id).
- **Weights actually matter** — raising the `operator` weight measurably changes the
  per-station charge order versus the balanced baseline, proving the dispatch is driven
  by the YAML weights and not hard-coded.
- **Data-only growth** — loading a world with `charger.capacity: 2`, an added station,
  a changed distance, or a `world_patch` produces a valid schedule with **no code
  change** (positions/times/feasibility all derive from the data).
- **Loader validation** — malformed YAML (unknown operator, unknown node, duplicate id,
  bad `HH:MM`, missing required field) fails loudly at load time with a clear error.
- **Rule plumbing** — adding a rule module registers it in `REGISTRY`; a rule with a
  missing weight contributes `0.0`; an all-zero `weights` block degrades to FCFS.

---

## Deploying (Streamlit Community Cloud)

1. Push this repository to a **public** GitHub repo.
2. Go to **share.streamlit.io → New app**.
3. Pick the **repo**, **branch**, and set the main file path to **`app.py`**.
4. Under **Advanced settings**, set the **Python version to 3.12**.
5. Deploy. Streamlit Cloud auto-installs `requirements.txt` (no solver or system
   packages needed — it's pure Python + Streamlit + PyYAML), then serves the app.
6. Copy the resulting public URL into the **Live app** placeholder at the top of this
   README.
