"""A human confirmation on a catalog-bound Datastream is recorded -- AI-321.

G9, 2026-08-29: once the catalog Templates declared their class (migration
319) the confirm door recomputed a passing preview and then REFUSED the
confirmation twice over -- the mapping schema knew only the `fst_` spelling of
a Template id, and the confirmation table binds a client artifact (migration
188 checks `app.file_source_templates` by id and hash), which a catalog
Template is not. Two pins: the schema accepts the catalog reference, and the
gate mints the mapping version without minting a client-confirmation row.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _confirmation(template_id: str, content_hash: str) -> dict:
    return {
        "template_id": template_id,
        "template_content_hash": content_hash,
        "sample_content_hash": "a" * 64,
        "sample_filename": "qa-e2e.csv",
        "actor": "owner@example.com",
        "confirmed_at": "2026-08-29T09:00:00+00:00",
        "resolutions": [],
        "accepted_warnings": [],
        "warning_reason": None,
    }


def test_the_mapping_schema_accepts_both_spellings_of_a_template_id():
    import jsonschema
    from core.datastream_field_mapping import _mapping_schema_validator

    validator = _mapping_schema_validator()
    sub = validator.schema["$defs"]["file_source_confirmation"]
    check = jsonschema.Draft202012Validator(sub)
    client_id = "fst_" + "0123456789ABCDEFGHJKMNPQRS"
    assert not list(check.iter_errors(_confirmation("template:OFFLINE_OOH_V1:2", "")))
    assert not list(check.iter_errors(_confirmation(client_id, "b" * 64)))
    # Not anything: a bare code is not a reference, and a half hash is not a hash.
    assert list(check.iter_errors(_confirmation("OFFLINE_OOH_V1", "")))
    assert list(check.iter_errors(_confirmation("template:OFFLINE_OOH_V1:2", "abc")))


@pytest.mark.parametrize(
    ("template_id", "rows_expected"),
    [("template:OFFLINE_OOH_V1:2", 0), ("fst_0123456789ABCDEFGHJKMNPQRS", 1)],
)
def test_a_catalog_template_mints_the_mapping_version_and_no_client_confirmation_row(
    template_id, rows_expected
):
    from core import file_source_gate as gate

    recorded: list[str] = []

    def _execute(conn, spec, *, mutation):
        result = mutation(conn, "op_1")

        class _Op:
            operation_id = "op_1"
            replayed = False

        _Op.result = result.result
        return _Op()

    with (
        patch.object(
            gate,
            "_record_confirmation",
            side_effect=lambda *a, **k: recorded.append(k["template_id"]),
        ),
        patch(
            "core.datastream_field_mapping.save_field_mapping",
            return_value={"id": "dmap_1", "plan_version_id": "dsp_1", "executable": True,
                          "blocking_count": 0},
        ),
        patch("core.operations.execute_operation", side_effect=_execute),
    ):
        result = gate.confirm_mapping_version(
            object(),
            template_id=template_id,
            project_id="proj_EXAMPLE",
            org_id="org_EXAMPLE",
            datastream_id="ds_1",
            actor="owner@example.com",
            gate_result={"passed": True},
            mapping_payload={"fields": []},
            evidence={"sample_content_hash": "a" * 64, "resolutions": []},
            idempotency_key="k1",
            content_hash="",
            pinned_plan_version_id="dsp_1",
        )

    assert result["mapping_version_id"] == "dmap_1"
    assert result["confirmed"] is True
    assert len(recorded) == rows_expected
