"""Canonical DolphinBench fact registry access."""

from registry.facts import FactRegistryError, load_fact_registry, source_session_ids_for_test

__all__ = [
    "FactRegistryError",
    "load_fact_registry",
    "source_session_ids_for_test",
]
