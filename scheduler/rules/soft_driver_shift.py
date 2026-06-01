"""Soft rule: driver_shift — prioritise buses at risk of finishing after the driver's shift ends."""
from __future__ import annotations

from typing import Optional

from scheduler.context import DecisionContext
from scheduler.registry import soft_rule

# How early before the shift end urgency starts ramping up, and over how many
# minutes it ramps from 0 to 1. A bus due to finish within this lead window of
# (or past) its shift end is treated as fully urgent.
_LEAD_MIN = 60.0
_RAMP_MIN = 60.0


def _to_minutes(value) -> Optional[float]:
    """Parse a shift-end into minutes-from-midnight.

    Accepts an ``"HH:MM"`` string (-> H*60 + M) or a numeric minutes value.
    Returns ``None`` if the value is missing or unparseable, leaving the rule
    inert for that bus.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if ":" in text:
        hh, _, mm = text.partition(":")
        try:
            return int(hh) * 60 + int(mm)
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@soft_rule("driver_shift")
def driver_shift(ctx: DecisionContext) -> float:
    """Prioritise buses whose charge would finish near or past the driver's shift end.

    Reads ``ctx.candidate.attrs.get("shift_end")`` — an ``"HH:MM"`` string or a
    minutes integer. If it is absent (or unparseable) the rule returns 0.0 and is
    completely inert for that bus.

    Otherwise it penalises plans that run a driver past clocking-off time by
    raising the benefit for at-risk buses: it compares the charge END
    (``ctx.clock + ctx.charge_duration``) against the shift end and returns
    ``clamp((charge_end - (shift_end - 60)) / 60, 0, 1)``. Urgency therefore
    starts climbing an hour before the shift end and saturates at 1.0 once the
    charge would finish at (or after) it, so buses about to strand their drivers
    are served first — without ever overriding the hard feasibility rules.
    """
    shift_end = _to_minutes(ctx.candidate.attrs.get("shift_end"))
    if shift_end is None:
        return 0.0
    charge_end = ctx.clock + ctx.charge_duration
    return _clamp((charge_end - (shift_end - _LEAD_MIN)) / _RAMP_MIN, 0.0, 1.0)
