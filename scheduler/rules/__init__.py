"""Importing this package registers every built-in rule (decorator side effects).
To add a rule: drop a new module here and add one import line. The engine is never touched."""
from . import soft_individual, soft_operator, soft_overall          # noqa: F401  core soft objectives
from . import hard_range, hard_charger_capacity, hard_route_order, hard_charge_duration  # noqa: F401
from . import soft_priority, soft_tou_cost, soft_driver_shift        # noqa: F401  flex demo rules

# ---------------------------------------------------------------------------
# Could go further: auto-discovery. Instead of editing the import list above,
# we could walk this package and import every `soft_*` / `hard_*` module so a
# new rule file registers itself the instant it is dropped in — zero edits here.
# Left commented on purpose: the explicit list above is greppable, ordered, and
# makes the registered set obvious at a glance (and import errors surface loudly).
#
# import pkgutil
# import importlib
#
# for _mod in pkgutil.iter_modules(__path__):
#     if _mod.name.startswith(("soft_", "hard_")):
#         importlib.import_module(f"{__name__}.{_mod.name}")
# ---------------------------------------------------------------------------
