"""Rule protocols. Structural typing (typing.Protocol) means a rule author
writes a plain function + decorator — no base class to inherit, no ceremony.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from scheduler.context import DecisionContext


@runtime_checkable
class SoftRule(Protocol):
    """A weighted preference.

    ``score`` returns a number where HIGHER == more preferred for letting
    ``ctx.candidate`` charge next. The engine multiplies by ``weights[name]``
    and sums across all soft rules. Built-in rules return a benefit in ~[0, 1]
    so weights are commensurable.
    """

    name: str

    def score(self, ctx: DecisionContext) -> float: ...


@runtime_checkable
class HardRule(Protocol):
    """A feasibility constraint.

    ``is_feasible`` returns False to VETO a candidate. Hard rules are never
    weighted; any single False removes the candidate from consideration.
    ``reason`` powers diagnostics in tests and the post-run validator.
    """

    name: str

    def is_feasible(self, ctx: DecisionContext) -> bool: ...

    def reason(self, ctx: DecisionContext) -> str: ...
