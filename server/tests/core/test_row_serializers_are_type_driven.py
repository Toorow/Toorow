"""No serialiser decides by column NAME -- AI-219, generalising story 63.1.

WHAT BREAKS, AND WHY NOBODY SEES IT. `JSONResponse` serialises inside
``render()``, which Starlette calls after the handler has returned. A `datetime`
that reached the payload raises `TypeError` THERE -- outside the handler's
``try/except``, so the 500 the caller gets carries no code, the log line the
handler would have written is never written, and the 503 branch never runs. 63.1
measured exactly this on `datastream_executions` when migration 218 added
`started_at` and `progress_updated_at` to a serialiser that knew three names.

WHAT IS UNDER TEST HERE. Not "does this row serialise" -- that passes today with
the names hardcoded. What is tested is a column the serialiser has NEVER HEARD
OF, which is the only shape the next migration can take. Each test feeds one
extra date-typed column and asserts the value came out as a string.

The eleventh member of the family, `_row_to_execution`, already holds the rule
and has its own tests; it is the reference, not a subject.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from unittest.mock import MagicMock

import pytest

#: The column no serialiser has a name for. Every one of them must still render.
TOMORROW = "settled_at"
MOMENT = datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)


def _cursor(columns: list[str], rows: list[tuple]) -> MagicMock:
    cur = MagicMock()
    cur.description = [(name,) for name in columns]
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    return cur


def _is_json(payload: dict) -> None:
    """The property that actually matters: `JSONResponse.render` would not raise."""
    json.dumps(payload)


# ---------------------------------------------------------------------------
# The seam itself.
# ---------------------------------------------------------------------------


def test_the_shared_scalar_converts_every_temporal_type() -> None:
    from core.row_json import json_scalar

    assert json_scalar(MOMENT) == MOMENT.isoformat()
    assert json_scalar(date(2026, 8, 6)) == "2026-08-06"
    assert json_scalar(time(9, 30)) == "09:30:00"


def test_the_shared_scalar_leaves_everything_else_alone() -> None:
    """A `Decimal` that reaches here is a missing mapping layer, not an encoding."""
    from decimal import Decimal

    from core.row_json import json_scalar

    assert json_scalar(None) is None
    assert json_scalar(True) is True
    assert json_scalar(3) == 3
    assert json_scalar(Decimal("1.5")) == Decimal("1.5")


def test_the_shared_row_reader_takes_names_or_a_cursor_description() -> None:
    from core.row_json import row_to_json

    assert row_to_json(["a", "b"], (1, MOMENT))["b"] == MOMENT.isoformat()
    assert row_to_json([("a",), ("b",)], (1, MOMENT))["b"] == MOMENT.isoformat()


# ---------------------------------------------------------------------------
# The ten sisters, one test each, fed a column they have never heard of.
# ---------------------------------------------------------------------------


def test_authorization_serializer_survives_a_new_date_column() -> None:
    from core.me_api import _serialize_authorization  # noqa: PLC0415

    ref = {
        "id": "cref_1", "nango_connection_id": "nc_1", "provider": "generic",
        "project_id": "proj_EXAMPLE", "created_at": MOMENT, "status": "active",
        TOMORROW: MOMENT,
    }
    _serialize_authorization(ref, "owner@example.com", True)

    assert ref[TOMORROW] == MOMENT.isoformat()
    assert ref["created_at"] == MOMENT.isoformat()


def test_project_row_serializer_survives_a_new_date_column() -> None:
    from core.projects_api import _project_row_to_dict  # noqa: PLC0415

    record = _project_row_to_dict(
        ["id", "created_at", TOMORROW], ("proj_EXAMPLE", MOMENT, MOMENT)
    )

    assert record[TOMORROW] == MOMENT.isoformat()
    _is_json(record)


def test_context_topic_serializer_survives_a_new_date_column() -> None:
    from core.context_store import _row_to_topic

    record = _row_to_topic(("t_1", MOMENT, MOMENT), ["id", "created_at", TOMORROW])

    assert record[TOMORROW] == MOMENT.isoformat()
    _is_json(record)


def test_daily_insight_serializer_survives_a_new_date_column() -> None:
    from core.daily_insights import _row_to_dict

    cur = _cursor(["id", "insight_date", TOMORROW], [("ins_1", date(2026, 8, 6), MOMENT)])
    record = _row_to_dict(cur, cur.fetchone.return_value)

    assert record[TOMORROW] == MOMENT.isoformat()
    assert record["insight_date"] == "2026-08-06"
    _is_json(record)


def test_daily_insight_serializer_still_parses_its_json_columns() -> None:
    """The date rule replaced the name list; it must not have eaten the rest."""
    from core.daily_insights import _row_to_dict

    cur = _cursor(["id", "payload"], [("ins_1", '{"delta": 3}')])
    record = _row_to_dict(cur, cur.fetchone.return_value, json_cols=("payload",))

    assert record["payload"] == {"delta": 3}


def test_managed_file_dispatch_serializer_survives_a_new_date_column() -> None:
    from core.managed_file_dispatch import _row

    cur = _cursor(["id", "created_at", TOMORROW], [("disp_1", MOMENT, MOMENT)])
    record = _row(cur, cur.fetchone.return_value)

    assert record[TOMORROW] == MOMENT.isoformat()
    _is_json(record)


def test_managed_file_dispatch_serializer_still_parses_its_json_columns() -> None:
    from core.managed_file_dispatch import _row

    cur = _cursor(["id", "bundle"], [("disp_1", '{"files": 2}')])
    record = _row(cur, cur.fetchone.return_value)

    assert record["bundle"] == {"files": 2}


def test_alert_definition_create_and_update_share_one_serializer() -> None:
    """Two copies of the same name list, twenty lines apart, are now one call."""
    from core.alert_definitions_api import _alert_definition_row  # noqa: PLC0415

    cur = _cursor(
        ["id", "threshold", "created_at", TOMORROW],
        [("alert_1", 12.5, MOMENT, MOMENT)],
    )
    record = _alert_definition_row(cur, cur.fetchone.return_value)

    assert record[TOMORROW] == MOMENT.isoformat()
    assert record["threshold"] == 12.5
    assert isinstance(record["threshold"], float)
    _is_json(record)


@pytest.mark.parametrize("function_name", ["_list_org_mappings", "_get_mapping_by_id"])
def test_metric_semantics_serializers_are_type_driven(function_name) -> None:
    """Both copies live in one module; neither may keep its own name list."""
    import ast
    import inspect

    from core import metric_semantics_api

    source = inspect.getsource(metric_semantics_api)
    node = next(
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef) and n.name == function_name
    )
    code = ast.unparse(node)

    assert "row_to_json" in code, (
        f"{function_name} still decides by column name; the next migration on "
        "app.source_metric_mappings renders a mute 500"
    )
    assert "'created_at', 'updated_at'" not in code
