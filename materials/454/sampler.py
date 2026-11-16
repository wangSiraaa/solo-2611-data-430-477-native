"""Compatibility surface for the sampling entry points.

The real implementation lives in :mod:`design` (frozen selection) and
:mod:`collect` (worker-side whole-page reads).  This module only re-exports it
so older imports keep working, and it is deliberately *not* the place where any
new selection logic is defined.
"""
from __future__ import annotations

from collect import collect, validate_part
from design import build_plan, validate_plan


def select(catalog, design):
    """Return the frozen selection for a design, without reading page bodies."""
    plan = build_plan(catalog, "select-only", design)
    return list(plan["selected"])


__all__ = ["select", "collect", "build_plan", "validate_plan", "validate_part"]
