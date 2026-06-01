"""Bus Charging Scheduler — minimal Streamlit UI.

The brief is explicit about scope: *no metrics dashboards, no maps, no
animations*. A reviewer should land on the scenario dropdown immediately, pick a
scenario, see the input, and see what the scheduler decided. This file does
exactly that and nothing more.

Three tabs:
    1. Scenario input      — the fleet table + raw YAML + the world's stations.
    2. Per-bus timetable   — each bus's charging stops, waits and final arrival.
    3. Per-station order    — who charged at A / B / C / D, and in what order.

Everything is driven off the typed objects returned by the engine
(``scheduler.engine.solve``) and the loader (``scheduler.loader``); this file
never reaches into engine internals.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

import scheduler.rules  # noqa: F401  — importing registers all built-in rules
from scheduler.engine import solve
from scheduler.loader import (
    list_scenarios,
    load_scenario,
    min_to_hhmm,
)

st.set_page_config(page_title="Bus Charging Scheduler", layout="wide")

DATA = Path(__file__).parent / "data"


def _hhmm(m) -> str:
    """Format minutes-from-midnight as HH:MM, tolerating ``None``/empty."""
    if m is None:
        return "—"
    return min_to_hhmm(m)


def _direction_label(plan_or_bus) -> str:
    """A readable BK/KB-style direction tag plus the endpoints.

    ``forward`` is Bengaluru->Kochi (BK); ``reverse`` is Kochi->Bengaluru (KB).
    We show the corridor direction word alongside ``origin -> destination`` so
    the table is self-explanatory regardless of node naming.
    """
    direction = getattr(plan_or_bus, "direction", None)
    tag = {"forward": "BK", "reverse": "KB"}.get(direction, direction or "?")
    origin = getattr(plan_or_bus, "origin", "?")
    destination = getattr(plan_or_bus, "destination", "?")
    return f"{tag} ({origin}→{destination})"


def _charge_rows_from_events(events) -> list[dict]:
    """Collapse a bus's event stream into one row per charge stop.

    We walk the events in order and, per visited node, gather the
    ``arrive_station`` time, the ``wait`` duration, and the ``charge_start`` /
    ``charge_end`` clock values. A node only becomes a row once it has a
    ``charge_start`` (a bus may pass a station without charging there).
    """
    by_node: dict[str, dict] = {}
    order: list[str] = []

    def slot(node: str) -> dict:
        if node not in by_node:
            by_node[node] = {
                "node": node,
                "arrive": None,
                "charge_start": None,
                "charge_end": None,
                "wait": 0.0,
            }
            order.append(node)
        return by_node[node]

    for ev in events:
        node = ev.node
        if node is None:
            continue
        if ev.type == "arrive_station":
            row = slot(node)
            if row["arrive"] is None:
                row["arrive"] = ev.t_start
        elif ev.type == "wait":
            row = slot(node)
            row["wait"] += max(0.0, ev.t_end - ev.t_start)
        elif ev.type == "charge_start":
            row = slot(node)
            row["charge_start"] = ev.t_start
            if row["arrive"] is None:
                row["arrive"] = ev.t_start
        elif ev.type == "charge_end":
            row = slot(node)
            row["charge_end"] = ev.t_end

    rows: list[dict] = []
    for node in order:
        row = by_node[node]
        if row["charge_start"] is None:
            continue  # passed through without charging
        rows.append(
            {
                "Station": node,
                "Arrive": _hhmm(row["arrive"]),
                "Charge start": _hhmm(row["charge_start"]),
                "Charge end": _hhmm(row["charge_end"]),
                "Wait (min)": int(round(row["wait"])),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Scenario picker (the very first thing the reviewer sees).                    #
# --------------------------------------------------------------------------- #
st.title("Bus Charging Scheduler")

scenarios = list_scenarios(DATA / "scenarios")
if not scenarios:
    st.error(f"No scenarios found in {DATA / 'scenarios'}.")
    st.stop()

labels = [label for label, _ in scenarios]
choice = st.selectbox("Scenario", labels)
chosen_path = dict(scenarios)[choice]

scenario = load_scenario(chosen_path)
schedule = solve(scenario.world, scenario)

# Active weights, surfaced so a live weight edit is visible immediately.
weights_txt = "  ·  ".join(f"{k} = {v:g}" for k, v in schedule.weights.items())
st.caption(f"Active weights:  {weights_txt}")

if not schedule.feasible:
    st.error(
        "Schedule is INFEASIBLE. Violations:\n\n"
        + "\n".join(f"- {v}" for v in schedule.violations)
    )

tab_input, tab_bus, tab_station = st.tabs(
    ["Scenario input", "Per-bus timetable", "Per-station order"]
)


# --------------------------------------------------------------------------- #
# Tab 1 — Scenario input                                                      #
# --------------------------------------------------------------------------- #
with tab_input:
    st.subheader("Fleet")
    fleet_rows = []
    for bus in scenario.buses:
        direction = scenario.world.direction(bus.origin, bus.destination)
        tag = {"forward": "BK", "reverse": "KB"}.get(direction, direction)
        fleet_rows.append(
            {
                "Bus": bus.id,
                "Operator": bus.operator,
                "Direction": f"{tag} ({bus.origin}→{bus.destination})",
                "Departure": _hhmm(bus.departure),
            }
        )
    st.dataframe(
        pd.DataFrame(fleet_rows),
        width="stretch",
        hide_index=True,
    )

    st.subheader("World stations")
    station_rows = []
    for node_id in scenario.world.station_ids():
        station_rows.append(
            {
                "Station": node_id,
                "km from origin": scenario.world.position(node_id),
                "Chargers": scenario.world.capacity(node_id),
            }
        )
    st.dataframe(
        pd.DataFrame(station_rows),
        width="stretch",
        hide_index=True,
    )

    with st.expander("Raw scenario YAML"):
        st.code(Path(chosen_path).read_text(encoding="utf-8"), language="yaml")


# --------------------------------------------------------------------------- #
# Tab 2 — Per-bus timetable                                                   #
# --------------------------------------------------------------------------- #
with tab_bus:
    for bus in scenario.buses:
        plan = schedule.plan_for(bus.id)
        header = (
            f"**{plan.bus_id}** · {plan.operator} · {_direction_label(plan)} · "
            f"{_hhmm(plan.departure)} → {_hhmm(plan.arrival)}"
        )
        st.markdown(header)

        rows = _charge_rows_from_events(plan.events)
        if rows:
            st.dataframe(
                pd.DataFrame(rows),
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No charging stops (reached destination on the initial charge).")
        st.divider()


# --------------------------------------------------------------------------- #
# Tab 3 — Per-station order                                                   #
# --------------------------------------------------------------------------- #
with tab_station:
    stations = schedule.stations
    if not stations:
        st.caption("This world has no charging stations.")
    else:
        columns = st.columns(len(stations))
        for col, station in zip(columns, stations):
            with col:
                n = station.capacity
                st.markdown(f"### {station.node} ({n} charger{'s' if n != 1 else ''})")
                records = sorted(station.records, key=lambda r: r.charge_start)
                if not records:
                    st.caption("No buses charged here.")
                    continue
                rows = [
                    {
                        "#": i,
                        "Bus": rec.bus_id,
                        "Operator": rec.operator,
                        "Start": _hhmm(rec.charge_start),
                        "End": _hhmm(rec.charge_end),
                        "Wait": int(round(rec.wait_min)),
                    }
                    for i, rec in enumerate(records, start=1)
                ]
                st.dataframe(
                    pd.DataFrame(rows),
                    width="stretch",
                    hide_index=True,
                )
