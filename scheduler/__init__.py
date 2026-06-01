"""Bus Charging Scheduler — a data-driven, pluggable scheduling engine.

Public surface:
    from scheduler.loader import load_world, load_scenario, list_scenarios
    from scheduler.engine import solve
    import scheduler.rules   # importing registers all built-in rules

The engine knows nothing about Bengaluru, Kochi, "BK/KB", four stations, or
240 km. Every concrete fact about the world lives in data (``data/*.yaml``).
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
