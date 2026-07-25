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
    key = default_idempotency_key("proj_1", "AXA_PLAN", h)
    assert key == f"file_source_template:proj_1:AXA_PLAN:{h}"
