"""Soft rule: tou_cost — favour charging during cheaper time-of-use hours."""
from __future__ import annotations

from typing import Dict

from scheduler.context import DecisionContext
from scheduler.registry import soft_rule

# Flat default: every hour costs the same, so the rule is inert (returns ~1.0
# for every hour) until a real price table is supplied in scenario/world data.
_FLAT_TABLE: Dict[int, float] = {h: 1.0 for h in range(24)}


def _load_table(ctx: DecisionContext) -> Dict[int, float]:
    """Read an {hour: price} table from scenario data, then world data.

    Keys may arrive as ints or strings (e.g. from JSON), so we coerce every key
    to ``int`` and every value to ``float``. Falls back to a flat 24-hour table.
    """
    raw = ctx.scenario.data.get("tou_cost_by_hour")
    if raw is None:
        raw = ctx.world.data.get("tou_cost_by_hour")
    if not raw:
        return dict(_FLAT_TABLE)
    return {int(k): float(v) for k, v in raw.items()}


@soft_rule("tou_cost")
def tou_cost(ctx: DecisionContext) -> float:
    """Reward starting a charge in a cheaper time-of-use (ToU) hour.

    Prices come from an ``{hour: price}`` table looked up in
    ``ctx.scenario.data["tou_cost_by_hour"]`` first, then
    ``ctx.world.data["tou_cost_by_hour"]`` (hour keys may be ints or strings;
    both are handled). With no table the default is a flat 1.0 for all 24 hours.

    We price the hour the charge would START
    (``hour = int(ctx.clock // 60) % 24``) and normalise across the table's own
    values: ``1.0 - (price - lo) / span`` where ``lo``/``hi`` are the cheapest
    and dearest prices and ``span = hi - lo``. So the cheapest hour scores ~1.0,
    the dearest ~0.0, and a flat table scores 1.0 everywhere (inert). All prices
    live in DATA, never in code, so re-pricing the grid needs no code change.
    """
    table = _load_table(ctx)
    hour = int(ctx.clock // 60) % 24
    price = table.get(hour, 1.0)
    lo = min(table.values())
    hi = max(table.values())
    span = (hi - lo) or 1.0
    return 1.0 - (price - lo) / span
