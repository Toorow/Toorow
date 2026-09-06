"""toorow -- tests for the file-source template artifact (Story 22.11, AD-2).

OFFLINE (pure, no DB): contract validation + normalisation, content-hash
determinism, template_code guard, idempotency-key derivation.

The DB behaviour (create through execute_operation, idempotent same-version /
new-version, immutability trigger, canonical-field registry check) lives in the
pg-gated companion ``server/tests/integration/test_file_source_template_pg.py``.
"""

from __future__ import annotations

import pytest
from core.file_source_template import (
    FileSourceTemplateValidationError,
    compute_content_hash,
    default_idempotency_key,
    validate_template_contract,
)


def _catalog_contract(**over):
    base = {
        "kind": "catalog",
        "required_fields": ["mdm_net_cost", "mdm_media_date"],
        "optional_fields": ["mdm_impressions"],
        "grain": "daily",
        "class": "planned",
        "placement": {"metric": "mdm_net_cost", "period": "mdm_media_date",
                      "dimension": ["mdm_channel"]},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Contract validation + normalisation
# ---------------------------------------------------------------------------


def test_validate_normalises_and_sorts():
    norm = validate_template_contract(_catalog_contract(
        required_fields=["mdm_b", "mdm_a"], optional_fields=["mdm_z"],
    ))
    assert norm["required_fields"] == ["mdm_a", "mdm_b"]  # sorted -> stable hash
    assert norm["optional_fields"] == ["mdm_z"]
    assert norm["class"] == "planned"
    assert norm["placement"] == {
        "metric": "mdm_net_cost", "period": "mdm_media_date", "dimension": ["mdm_channel"],
    }


def test_validate_rejects_unknown_kind():
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(_catalog_contract(kind="whatever"))


def test_validate_refuses_missing_class_ad6():
    c = _catalog_contract()
    del c["class"]
    with pytest.raises(FileSourceTemplateValidationError) as exc:
        validate_template_contract(c)
    assert "class" in str(exc.value)


def test_validate_rejects_empty_required_fields():
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(_catalog_contract(required_fields=[]))


def test_validate_rejects_required_optional_overlap():
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(_catalog_contract(
            required_fields=["mdm_x"], optional_fields=["mdm_x"],
        ))


def test_validate_requires_placement_metric_and_period():
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(_catalog_contract(placement={"metric": "mdm_net_cost"}))


def test_validate_adaptation_requires_py_content_hash():
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(_catalog_contract(kind="adaptation"))
    # A valid adaptation carries the locked .py SHA-256.
    norm = validate_template_contract(_catalog_contract(
        kind="adaptation", py_content_hash="a" * 64,
    ))
    assert norm["py_content_hash"] == "a" * 64


# ---------------------------------------------------------------------------
# Story 22.14 regression: the discriminator + layout keys must SURVIVE
# normalisation.
#
# ``create_file_source_template`` persists ``json.dumps(normalised)``: whatever
# the validator drops is UNREACHABLE on every persisted template. And the
# consumers read exactly those keys -- ``stamp_placement`` /
# ``evaluate_variant_discriminator`` read ``contract['discriminator']``,
# ``produce`` / ``read_source_columns`` read format / header_row / sheet_name /
# date_format. A closed key set that omits them disarms the whole variant
# mechanism the moment the template is stored.
# ---------------------------------------------------------------------------


def test_validate_preserves_discriminator_and_layout_keys():
    norm = validate_template_contract(_catalog_contract(
        discriminator={"dimension": "mdm_market", "source": "filename",
                       "pattern": r"_(?P<value>[A-Z]{2})_"},
        format="csv",
        header_row=6,
        sheet_name="DV360",
        date_format="%d.%m.%Y",
    ))
    assert norm["discriminator"] == {
        "dimension": "mdm_market", "source": "filename",
        "pattern": r"_(?P<value>[A-Z]{2})_",
    }
    assert norm["format"] == "csv"
    assert norm["header_row"] == 6
    assert norm["sheet_name"] == "DV360"
    assert norm["date_format"] == "%d.%m.%Y"


def test_content_hash_changes_on_discriminator_change():
    # Two templates that differ ONLY by which cell carries the market must be two
    # DIFFERENT versions; a dropped discriminator collapses them onto one hash.
    a = compute_content_hash(validate_template_contract(_catalog_contract(
        discriminator={"dimension": "mdm_market", "source": "cell", "cell": "Market"})))
    b = compute_content_hash(validate_template_contract(_catalog_contract(
        discriminator={"dimension": "mdm_market", "source": "cell", "cell": "Pays"})))
    assert a != b


def test_validate_is_idempotent_on_an_already_normalised_contract():
    # The persisted contract IS the normalised one; re-validating it must be a
    # no-op, otherwise the stored content_hash can never be re-derived (the seal
    # the replay lock checks in file_source_producer).
    once = validate_template_contract(_catalog_contract(
        discriminator={"dimension": "mdm_market", "source": "cell", "cell": "Market"},
        format="csv", header_row=6, sheet_name="DV360", date_format="%d.%m.%Y",
    ))
    twice = validate_template_contract(once)
    assert twice == once
    assert compute_content_hash(twice) == compute_content_hash(once)


def test_validate_rejects_a_malformed_discriminator():
    # An unusable discriminator must be refused at creation, not silently kept and
    # resolved to None at landing time (that mislabels or nulls the dimension).
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(_catalog_contract(
            discriminator={"dimension": "mdm_market", "source": "telepathy"}))
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(_catalog_contract(
            discriminator={"dimension": "mdm_market", "source": "cell"}))  # no cell
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(_catalog_contract(
            discriminator={"dimension": "mdm_market", "source": "filename",
                           "pattern": "_(?P<value>[A-Z"}))  # uncompilable


# ---------------------------------------------------------------------------
# Content hash: deterministic + sensitive to real changes
# ---------------------------------------------------------------------------


def test_content_hash_is_order_insensitive_and_deterministic():
    a = compute_content_hash(validate_template_contract(_catalog_contract(
        required_fields=["mdm_a", "mdm_b"])))
    b = compute_content_hash(validate_template_contract(_catalog_contract(
        required_fields=["mdm_b", "mdm_a"])))  # different input order, same content
    assert a == b
    assert len(a) == 64


def test_content_hash_changes_on_class_change():
    p = compute_content_hash(validate_template_contract(_catalog_contract(**{"class": "planned"})))
    a = compute_content_hash(validate_template_contract(_catalog_contract(**{"class": "actual"})))
    assert p != a


def test_content_hash_changes_on_placement_change():
    base = compute_content_hash(validate_template_contract(_catalog_contract()))
    moved = compute_content_hash(validate_template_contract(_catalog_contract(
        placement={"metric": "mdm_net_cost", "period": "mdm_media_date",
                   "dimension": ["mdm_market"]})))
    assert base != moved


# ---------------------------------------------------------------------------
# Idempotency key derivation
# ---------------------------------------------------------------------------


def test_default_idempotency_key_binds_scope_and_content():
    h = "f" * 64
    key = default_idempotency_key("proj_1", "EXAMPLE_PLAN", h)
    assert key == f"file_source_template:proj_1:EXAMPLE_PLAN:{h}"


# ---------------------------------------------------------------------------
# The landing target + the plan-line capabilities (chantier 67-25b).
#
# `file-source-ingestion.md` ratified ONE ingestion engine and required each
# missing capability to become a KEY OF THE TEMPLATE CONTRACT, validated here.
# A key the executor cannot read is a key nobody can rely on -- that is how the
# declared discriminator became unreachable once already.
# ---------------------------------------------------------------------------


def _plan_contract(**over):
    """A contract landing in the plan store (grain 'line' on both levels)."""
    base = _catalog_contract(
        grain="line",
        landing_target="plan_store",
        reshape={
            "fields": {"label": "Line", "start_date": "Start",
                       "end_date": "End", "budget": "Budget"},
            "amount_field": "budget",
            "start_field": "start_date",
            "end_field": "end_date",
            "grain": "line",
        },
    )
    base.update(over)
    return base


def test_landing_target_defaults_to_the_warehouse_relation():
    norm = validate_template_contract(_catalog_contract())
    assert norm["landing_target"] == "warehouse_relation"


def test_landing_target_rejects_an_unknown_value():
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(_catalog_contract(landing_target="bigquery"))


def test_a_plan_store_target_is_kept_and_changes_the_content_hash():
    norm = validate_template_contract(_plan_contract())
    assert norm["landing_target"] == "plan_store"
    # The target is part of the seal: the same fields landing somewhere else is
    # a DIFFERENT contract, and must version a different row.
    warehouse = dict(_plan_contract())
    warehouse["landing_target"] = "warehouse_relation"
    assert compute_content_hash(norm) != compute_content_hash(
        validate_template_contract(warehouse)
    )


def test_a_plan_store_target_refuses_the_daily_grain():
    """The plan store's model is the LINE, and it spreads to daily at publish.

    Landing daily rows there would change the plan's data model and duplicate the
    spread -- the two would then disagree about what a plan line is.
    """
    with pytest.raises(FileSourceTemplateValidationError) as exc:
        validate_template_contract(_plan_contract(grain="daily"))
    assert "plan_store" in str(exc.value)


def test_several_sheets_require_the_line_grain():
    c = _plan_contract()
    c["reshape"] = {**c["reshape"], "grain": "daily", "sheets": ["France", "Germany"]}
    c["grain"] = "daily"
    c["landing_target"] = "warehouse_relation"
    with pytest.raises(FileSourceTemplateValidationError) as exc:
        validate_template_contract(c)
    assert "sheets" in str(exc.value)


def test_the_sheets_capability_is_kept_and_refuses_a_repeat():
    c = _plan_contract()
    c["reshape"] = {**c["reshape"], "sheets": ["France", "Germany"]}
    assert validate_template_contract(c)["reshape"]["sheets"] == ["France", "Germany"]

    c["reshape"] = {**c["reshape"], "sheets": ["France", "France"]}
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(c)


def test_an_explode_key_must_be_a_physical_a1_range():
    """The split binds to the merge's PHYSICAL coordinates, never a nickname.

    That is what makes a shifted merge stop applying instead of splitting on a
    stale key.
    """
    c = _plan_contract()
    c["reshape"] = {
        **c["reshape"],
        "merged_amount": {"explode": {"the-display-block": ["500", "700"]}},
    }
    with pytest.raises(FileSourceTemplateValidationError) as exc:
        validate_template_contract(c)
    assert "A1 range" in str(exc.value)


def test_an_explode_refuses_a_zero_or_negative_weight():
    c = _plan_contract()
    c["reshape"] = {
        **c["reshape"],
        "merged_amount": {"explode": {"D2:D4": ["500", "0"]}},
    }
    with pytest.raises(FileSourceTemplateValidationError):
        validate_template_contract(c)


def test_a_valid_explode_survives_normalisation():
    c = _plan_contract()
    c["reshape"] = {
        **c["reshape"],
        "line_key": "auto",
        "merged_amount": {"explode": {"D2:D4": ["500.00", "400.00", "300.00"]}},
    }
    norm = validate_template_contract(c)
    assert norm["reshape"]["merged_amount"]["explode"]["D2:D4"] == [
        "500.00", "400.00", "300.00",
    ]
