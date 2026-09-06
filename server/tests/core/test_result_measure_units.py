"""A Result says what its numbers MEAN, or nobody can read the money in it.

Canonical money is micros with the currency attached (`core/money.py`, E39-AD1)
and the `/1e6` back to display units happens exactly once, at read. Console
table, pivot matrix and MCP App are three reads -- and until the plan froze the
governed `value_type`/`unit` beside each measure, all three printed the stored
integer, so 124 EUR reached three screens as `124000000`.

What is proven here is the CARRIAGE, end to end and offline: the plan freezes it,
the Result schema carries it, the pivot projection echoes it. The division itself
is a display transform and is proven where it happens, in
`ui/cards/shell/src/viz/__tests__/canonicalAmount.test.ts`.
"""

from __future__ import annotations

from core.multi_source_execution import result_schema
from core.multi_source_plan import _compile_derived, _compile_member
from core.pivot_projection import project

VOCABULARY = {
    "mdm_spend": {"value_type": "money", "unit": "EUR"},
    "mdm_conversions": {"value_type": "integer", "unit": None},
    "mdm_cpa": {"value_type": "money", "unit": "EUR"},
}

PUBLISHED = {
    "datastream_id": "ds_left",
    "name": "Paid media",
    "mapping_version_id": "dmv_left",
    "mapping_version_number": 3,
    "output_version_id": "dov_left",
    "output_id": "dso_left",
    "published_execution_id": "dse_left",
    "publication_log_id": "dpl_left",
    "plan_version_id": "dpv_left",
    "relation_ref": "dataset.table",
    "schema_hash": "a" * 64,
    "mapping_payload": {
        "fields": [
            {
                "field_id": "cost_micros",
                "binding": {"mdm_target": "mdm_spend", "status": "confirmed"},
                "suggestion": {"semantic_role": "measure", "aggregation": "sum"},
            },
            {
                "field_id": "conversions",
                "binding": {"mdm_target": "mdm_conversions", "status": "confirmed"},
                "suggestion": {"semantic_role": "measure", "aggregation": "sum"},
            },
        ]
    },
}


def _member() -> dict:
    return _compile_member(
        {
            "datastream_id": "ds_left",
            "measures": [
                {"canonical_field_id": "mdm_spend"},
                {"canonical_field_id": "mdm_conversions"},
            ],
        },
        PUBLISHED,
        VOCABULARY,
    )


def test_the_plan_freezes_the_governed_unit_of_every_measure():
    measures = {m["canonical_field_id"]: m for m in _member()["measures"]}

    assert measures["mdm_spend"]["value_type"] == "money"
    assert measures["mdm_spend"]["unit"] == "EUR"
    # An integer count has no unit, and inventing one would be as wrong as
    # dropping a currency: the field says nothing, so the plan says nothing.
    assert measures["mdm_conversions"]["value_type"] == "integer"
    assert measures["mdm_conversions"]["unit"] is None


def test_a_ratio_states_its_own_type_and_never_inherits_it_from_its_components():
    derived = _compile_derived(
        [
            {
                "canonical_field_id": "mdm_cpa",
                "numerator_field_id": "mdm_spend",
                "denominator_field_id": "mdm_conversions",
            }
        ],
        [_member()],
        VOCABULARY,
    )

    assert derived[0]["value_type"] == "money"
    assert derived[0]["unit"] == "EUR"


def test_an_unreadable_or_silent_vocabulary_freezes_no_unit_at_all():
    """No vocabulary, no currency. Silence is not EUR."""
    measures = _compile_member(
        {"datastream_id": "ds_left", "measures": [{"canonical_field_id": "mdm_spend"}]},
        PUBLISHED,
        None,
    )["measures"]

    assert measures[0]["value_type"] is None
    assert measures[0]["unit"] is None


def test_the_result_schema_and_the_pivot_carry_what_the_plan_froze():
    plan = {
        "members": [_member()],
        "derived_measures": _compile_derived(
            [
                {
                    "canonical_field_id": "mdm_cpa",
                    "numerator_field_id": "mdm_spend",
                    "denominator_field_id": "mdm_conversions",
                }
            ],
            [_member()],
            VOCABULARY,
        ),
        "edges": [
            {"components": [{"canonical_field_id": "mdm_day", "canonical_name": "Day"}]}
        ],
        "dimensions": [{"canonical_field_id": "mdm_day"}],
    }
    schema = result_schema(plan)
    by_name = {field["name"]: field for field in schema["fields"]}

    assert by_name["m_mdm_spend"]["value_type"] == "money"
    assert by_name["m_mdm_spend"]["unit"] == "EUR"
    assert by_name["r_mdm_cpa"]["unit"] == "EUR"

    matrix = project(
        result_id="qr_1",
        content_hash="b" * 64,
        schema=schema,
        rows=[
            {
                "k_mdm_day": "2026-08-11",
                "m_mdm_spend": 124_000_000,
                "m_mdm_conversions": 42,
                "r_mdm_cpa": 2_952_380,
            }
        ],
        request={
            "rows": ["k_mdm_day"],
            "columns": [],
            "values": ["m_mdm_spend", "m_mdm_conversions"],
        },
    )
    value_fields = {field["name"]: field for field in matrix["value_fields"]}

    assert value_fields["m_mdm_spend"]["value_type"] == "money"
    assert value_fields["m_mdm_spend"]["unit"] == "EUR"
    assert value_fields["m_mdm_conversions"]["unit"] is None
    # The projection rearranges; it never converts. The stored micros stay micros
    # on the wire -- the division belongs to the read, once, in the renderer.
    assert matrix["cells"][0]["values"]["m_mdm_spend"]["value"] == 124_000_000
