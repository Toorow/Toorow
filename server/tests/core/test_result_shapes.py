"""Story 41.6 -- the server-authored ``waterfall_v1`` Result contract."""

from __future__ import annotations

import pytest
from core.result_shapes import ResultShapeRefused, serialize_waterfall_v1

COMPONENTS = [
    {
        "component": "net_media",
        "label": "Net media",
        "waterfall_role": "base",
        "member_id": "m_net",
        "tax_basis": "HT",
    },
    {
        "component": "platform_fee",
        "label": "Platform fees",
        "waterfall_role": "delta",
        "member_id": "m_platform",
        "tax_basis": "HT",
    },
    {
        "component": "regulatory_tax",
        "label": "Regulatory tax",
        "waterfall_role": "delta",
        "member_id": "m_regulatory",
        "tax_basis": "HT",
    },
    {
        "component": "withholding_gross_up",
        "label": "Withholding gross-up",
        "waterfall_role": "delta",
        "member_id": "m_wht",
        "tax_basis": "HT",
    },
    {
        "component": "agency_fee",
        "label": "Agency fee",
        "waterfall_role": "delta",
        "member_id": "m_agency",
        "tax_basis": "HT",
    },
    {
        "component": "total_cost_ht",
        "label": "Total cost HT",
        "waterfall_role": "subtotal",
        "member_id": "m_ht",
        "tax_basis": "HT",
    },
    {
        "component": "vat_sales_tax",
        "label": "VAT / sales tax",
        "waterfall_role": "delta",
        "member_id": "m_vat",
        "tax_basis": "TTC",
    },
    {
        "component": "invoice_ttc",
        "label": "Invoice TTC",
        "waterfall_role": "total",
        "member_id": "m_ttc",
        "tax_basis": "TTC",
    },
]


def _descriptor(**overrides):
    value = {
        "id": "waterfall_v1",
        "components": COMPONENTS,
        "currency": {"value": "EUR"},
        "covered_row_count": {"member_id": "m_covered"},
        "total_row_count": {"member_id": "m_total"},
        "gap_codes": {"member_id": "m_gaps"},
        "non_additive_grains": ["plan_line"],
        "allowed_basis_transition": ["HT", "TTC"],
        "verification": {
            "keep_separate": True,
            "rows": [
                {"provider": "DoubleVerify", "value_micros": None, "gap_code": "NO_RULE_DECLARED"}
            ],
        },
    }
    value.update(overrides)
    return value


def _source(**overrides):
    row = {
        "m_net": 1_000_000,
        "m_platform": 100_000,
        "m_regulatory": 50_000,
        "m_wht": 25_000,
        "m_agency": 75_000,
        "m_ht": 1_250_000,
        "m_vat": 250_000,
        "m_ttc": 1_500_000,
        "m_covered": 9,
        "m_total": 10,
        "m_gaps": [],
    }
    row.update(overrides)
    return row


def test_waterfall_v1_is_an_eight_datum_server_authored_shape():
    shaped = serialize_waterfall_v1(
        result_id="qr_01",
        source_rows=[_source()],
        descriptor=_descriptor(),
        grain="project",
        evidence_by_member={member["member_id"]: f"ev_{i}" for i, member in enumerate(COMPONENTS)},
    )
    assert shaped["manifest"]["result_shape"] == "waterfall_v1"
    assert [r["component"] for r in shaped["rows"]] == [c["component"] for c in COMPONENTS]
    assert [r["sequence"] for r in shaped["rows"]] == list(range(1, 9))
    assert [r["value_micros"] for r in shaped["rows"]] == [
        1_000_000,
        100_000,
        50_000,
        25_000,
        75_000,
        1_250_000,
        250_000,
        1_500_000,
    ]
    assert [r["running_total_micros"] for r in shaped["rows"]] == [
        1_000_000,
        1_100_000,
        1_150_000,
        1_175_000,
        1_250_000,
        1_250_000,
        1_500_000,
        1_500_000,
    ]
    assert shaped["rows"][1]["start_total_micros"] == 1_000_000
    assert shaped["rows"][6]["start_total_micros"] == 1_250_000
    assert all(r["datum_key"].startswith("qr_01:") for r in shaped["rows"])
    assert all(r["evidence_key"].startswith("ev_") for r in shaped["rows"])
    assert shaped["manifest"]["coverage"] == {"covered_row_count": 9, "total_row_count": 10}
    assert all(row["covered_row_count"] == 9 for row in shaped["rows"])
    assert shaped["manifest"]["verification"]["keep_separate"] is True
    assert shaped["manifest"]["verification"]["rows"][0]["value_micros"] is None


def test_a_required_null_propagates_without_becoming_zero_or_a_complete_total():
    shaped = serialize_waterfall_v1(
        result_id="qr_02",
        source_rows=[_source(m_regulatory=None, m_gaps=["REGULATORY_INPUT_UNRESOLVED"])],
        descriptor=_descriptor(),
        grain="project",
        evidence_by_member={},
    )
    regulatory = shaped["rows"][2]
    assert regulatory["value_micros"] is None
    assert regulatory["running_total_micros"] is None
    assert regulatory["is_complete"] is False
    assert regulatory["gap_codes"] == ["REGULATORY_INPUT_UNRESOLVED"]
    assert all(r["running_total_micros"] is None for r in shaped["rows"][2:])
    assert shaped["rows"][-1]["value_micros"] is None


def test_known_zero_stays_complete_and_distinct_from_a_null():
    shaped = serialize_waterfall_v1(
        result_id="qr_03",
        source_rows=[_source(m_platform=0, m_ht=1_150_000, m_ttc=1_400_000)],
        descriptor=_descriptor(),
        grain="project",
        evidence_by_member={},
    )
    assert shaped["rows"][1]["value_micros"] == 0
    assert shaped["rows"][1]["is_complete"] is True
    assert shaped["rows"][1]["running_total_micros"] == 1_000_000


def test_plan_line_and_other_declared_non_additive_grains_are_refused():
    with pytest.raises(ResultShapeRefused, match="non-additive") as exc:
        serialize_waterfall_v1(
            result_id="qr_04",
            source_rows=[_source()],
            descriptor=_descriptor(),
            grain="plan_line",
            evidence_by_member={},
        )
    assert exc.value.code == "non_additive_grain"


def test_only_the_declared_ht_to_ttc_transition_is_allowed():
    invalid = [dict(item) for item in COMPONENTS]
    invalid[3]["tax_basis"] = "TTC"
    invalid[4]["tax_basis"] = "HT"
    with pytest.raises(ResultShapeRefused, match="tax basis") as exc:
        serialize_waterfall_v1(
            result_id="qr_05",
            source_rows=[_source()],
            descriptor=_descriptor(components=invalid),
            grain="project",
            evidence_by_member={},
        )
    assert exc.value.code == "incompatible_tax_basis"


def test_the_shape_refuses_multiple_summary_rows_instead_of_summing_them():
    with pytest.raises(ResultShapeRefused) as exc:
        serialize_waterfall_v1(
            result_id="qr_06",
            source_rows=[_source(), _source()],
            descriptor=_descriptor(),
            grain="project",
            evidence_by_member={},
        )
    assert exc.value.code == "ambiguous_waterfall_rows"


def test_schema_uses_stable_field_ids_and_never_overloads_semantic_roles():
    shaped = serialize_waterfall_v1(
        result_id="qr_07",
        source_rows=[_source()],
        descriptor=_descriptor(),
        grain="project",
        evidence_by_member={},
    )
    fields = shaped["schema"]["fields"]
    assert all(set(field) == {"id", "name"} for field in fields)
    assert {field["id"] for field in fields} >= {
        "wf_datum_key",
        "wf_waterfall_role",
        "wf_value_micros",
        "wf_running_total_micros",
        "wf_evidence_key",
    }
    assert not ({"base", "delta", "subtotal", "total"} & {field.get("role") for field in fields})
