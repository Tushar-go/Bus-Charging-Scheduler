"""The rule registry and the @soft_rule / @hard_rule decorators.

Adding a rule = write a function (or class) + decorate it. The decorator
registers it as a side effect of import. The engine iterates over the registry
and **never names an individual rule** — so adding one never touches the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict

from scheduler.context import DecisionContext

if TYPE_CHECKING:
    # Imported for type hints only. A runtime import here would create a cycle:
    # rules import the registry, so the registry must not import the rules package.
    from scheduler.rules.base import HardRule, SoftRule


@dataclass
class _FuncSoftRule:
    name: str
    _fn: Callable[[DecisionContext], float]

    def score(self, ctx: DecisionContext) -> float:
        return self._fn(ctx)


@dataclass
class _FuncHardRule:
    name: str
    _fn: Callable[[DecisionContext], bool]
    _reason: str = ""

    def is_feasible(self, ctx: DecisionContext) -> bool:
        return self._fn(ctx)

    def reason(self, ctx: DecisionContext) -> str:
        return self._reason or f"{self.name} violated"


@dataclass
class Registry:
    _soft: Dict[str, SoftRule] = field(default_factory=dict)
    _hard: Dict[str, HardRule] = field(default_factory=dict)

    def add_soft(self, rule: SoftRule) -> None:
        if rule.name in self._soft:
            raise ValueError(f"duplicate soft rule {rule.name!r}")
        self._soft[rule.name] = rule

    def add_hard(self, rule: HardRule) -> None:
        if rule.name in self._hard:
            raise ValueError(f"duplicate hard rule {rule.name!r}")
        self._hard[rule.name] = rule

    def soft_rules(self) -> Dict[str, SoftRule]:
        return dict(self._soft)          # copy: callers can't mutate the registry

    def hard_rules(self) -> Dict[str, HardRule]:
        return dict(self._hard)


# The single global registry.
REGISTRY = Registry()


def soft_rule(name: str):
    """Decorate a ``(ctx) -> float`` function OR a class implementing SoftRule.

    Higher score == this candidate is more preferred to charge next.
    """
    def deco(obj):
        if isinstance(obj, type):                       # class form
            inst = obj()
            if not getattr(inst, "name", None):
                inst.name = name
            REGISTRY.add_soft(inst)
            return obj
        REGISTRY.add_soft(_FuncSoftRule(name, obj))     # function form
        return obj

    return deco


def hard_rule(name: str, reason: str = ""):
    """Decorate a ``(ctx) -> bool`` function (True == feasible) OR a HardRule class."""
    def deco(obj):
        if isinstance(obj, type):
            inst = obj()
            if not getattr(inst, "name", None):
                inst.name = name
            REGISTRY.add_hard(inst)
            return obj
        REGISTRY.add_hard(_FuncHardRule(name, obj, reason))
        return obj

    return deco
