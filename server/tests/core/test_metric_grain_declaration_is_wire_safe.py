"""A metric-grain declaration must survive `json.dumps` -- measured 2026-09-01.

The first `GET .../governance/metric-grain/{concept}` ever made on a project
that HAD declared an authority answered 500, and so did the first
`PUT .../total`: `get_declaration` returned the rows as psycopg handed them,
with `created_at` / `updated_at` as datetimes and `tolerance_ratio` as a
Decimal, and `JSONResponse` refused them AFTER the write had committed. The
person was told the gesture failed; the database said it had succeeded; and
every project with a declaration could no longer read it. `_wire_safe` is the
one place that difference is folded, and this pins it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from core.metric_grain import _wire_safe


def test_datetimes_and_decimals_become_json_values():
    row = {
        "id": "mgd_EXAMPLE",
        "created_at": datetime(2026, 9, 1, 13, 55, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 1, 13, 56, tzinfo=timezone.utc),
        "tolerance_ratio": Decimal("0.0500"),
        "note": "kept as is",
    }

    safe = _wire_safe(row)

    assert json.dumps(safe)  # the whole point: it serialises
    assert safe["created_at"] == "2026-09-01T13:55:00+00:00"
    assert safe["updated_at"] == "2026-09-01T13:56:00+00:00"
    assert safe["tolerance_ratio"] == 0.05
    assert safe["note"] == "kept as is"
    assert row["created_at"].tzinfo is not None, "the caller's row is not mutated"


def test_absent_and_null_fields_pass_through():
    safe = _wire_safe({"id": "mgd_EXAMPLE", "created_at": None})
    assert safe == {"id": "mgd_EXAMPLE", "created_at": None}
    assert json.dumps(safe)
