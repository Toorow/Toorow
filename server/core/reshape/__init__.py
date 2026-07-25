"""toorow -- Shared reshape engine (Story 22.9, AD-5).

ONE Excel reshape engine, exposed as vetted mechanical primitives, consumed by
both the media-plan import (Story 22.1) and the file-source ingestion producer
(Epic 22 Phase B). Extracted with NO behaviour change from
``core.mediaplan_import`` / ``core.mediaplan_store``:

  * merged-cell resolution -- :func:`resolve_merges`, :func:`cell_resolved`
  * header-row column resolution -- :func:`resolve_column_index`
  * date-range -> daily spread -- :func:`compute_spread` (largest-remainder at
    the cent, sum-preserving, deterministic)

A ``.py`` adaptation and the catalog producer CALL these primitives; they never
reimplement them (AD-5: no second Excel engine, no reinvented daily spread).

Dependency direction is one-way: the media-plan domain imports from ``reshape``,
never the reverse.
"""

from __future__ import annotations

from core.reshape.headers import resolve_column_index
from core.reshape.merges import cell_resolved, resolve_merges
from core.reshape.spread import (
    CENT,
    ReshapeError,
    ReshapeValidationError,
    compute_spread,
)

__all__ = [
    "CENT",
    "ReshapeError",
    "ReshapeValidationError",
    "cell_resolved",
    "compute_spread",
    "resolve_column_index",
    "resolve_merges",
]
