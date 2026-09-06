"""The discovery read of Story 68.7 -- the sections, proven without a database.

What is proven here is the LOGIC: the bindings section counted from the pure
68.2 groups, the graceful-partial of every dependency not yet landed
(`unavailable` with a named reason, never a fake zero), the named unattached
groups, and the AD-1 envelope the ONE read builds for BOTH doors. The doors
themselves are proven at the `build_asgi_app()` seam in
`test_entity_context_api.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
from core import object_kind_registry as registry

_CORE = Path(__file__).resolve().parents[2] / "core"
_ENVELOPE_SCHEMA = json.loads(
    (Path(__file__).parent.parent / "conformance" / "schemas" / "envelope.schema.json").read_text(
        encoding="utf-8"
    )
)

PROJECT = "proj_EXAMPLE"


def _version(designated=None, declared_untyped=(), undeclared=(), ds="ds_1", name="Stream One"):
    """A pinned mapping version whose payload carries the 68.2 declarations."""
    fields = []
    for field_id, kind in (designated or {}).items():
        fields.append({"field_id": field_id, "binding": {"designates_object_kind": kind}})
    for field_id in declared_untyped:
        fields.append({"field_id": field_id, "binding": {"designates_object_kind": None}})
    for field_id in undeclared:
        fields.append({"field_id": field_id, "profile": {"unique": True}, "binding": {}})
    return {
        "datastream_id": ds,
        "datastream_name": name,
        "mapping_version_id": f"dmv_{ds}",
        "mapping_version_number": 3,
        "mapping_payload": {"fields": fields},
    }


# ---------------------------------------------------------------------------
# The bindings section: only what the payload DECLARES, counted (68.2, AC4).
# ---------------------------------------------------------------------------


def test_designations_are_counted_per_object_kind():
    section, _unattached = registry._entity_bindings_section(
        [
            _version(designated={"video_id": "video", "venue_id": "venue"}),
            _version(designated={"clip_id": "video"}, ds="ds_2", name="Stream Two"),
        ]
    )
    assert section["status"] == "ok"
    assert section["designation_count"] == 3
    assert section["by_object_kind"] == {"venue": 1, "video": 2}
    assert {d["field_id"] for d in section["designations"]} == {
        "video_id",
        "venue_id",
        "clip_id",
    }
    assert section["designations"][0]["mapping_version_id"] == "dmv_ds_1"


def test_undeclared_key_candidates_are_a_named_group_with_counts():
    _section, unattached = registry._entity_bindings_section(
        [
            _version(undeclared=["session_id", "user_pseudo"], ds="ds_1", name="Alpha"),
            _version(designated={"video_id": "video"}, ds="ds_2", name="Beta"),
        ]
    )
    assert unattached["group"] == "undeclared_key_candidates"
    assert unattached["status"] == "ok"
    assert unattached["count"] == 2
    assert unattached["by_datastream"] == [
        {
            "datastream_id": "ds_1",
            "datastream_name": "Alpha",
            "field_ids": ["session_id", "user_pseudo"],
            "count": 2,
        }
    ]


def test_declared_untyped_is_counted_apart_from_absent():
    section, unattached = registry._entity_bindings_section(
        [_version(declared_untyped=["not_a_key"], undeclared=["maybe_a_key"])]
    )
    # An explicit null is "declared: this column designates nothing" -- it is
    # NOT an unbound candidate, and the two are never the same fact (68.2 AC4).
    assert section["declared_untyped_count"] == 1
    assert unattached["count"] == 1
    assert unattached["by_datastream"][0]["field_ids"] == ["maybe_a_key"]


def test_no_published_mapping_is_an_ok_zero_not_an_unavailable():
    section, unattached = registry._entity_bindings_section([])
    assert section["status"] == "ok"
    assert section["designation_count"] == 0
    assert section["designations"] == []
    assert unattached["status"] == "ok"
    assert unattached["count"] == 0


# ---------------------------------------------------------------------------
# The rule-sets section: the 68.6 contract, probed.
# ---------------------------------------------------------------------------


class _Conn:
    """The section patches `active_version`; the connection is never used."""

    def cursor(self):
        raise AssertionError("the rule-sets section must not query directly")


def test_rule_sets_unavailable_when_68_6_has_not_landed():
    with patch.object(registry, "_load_entity_derivation_family", return_value=None):
        section = registry._entity_rule_sets_section(
            _Conn(), project_id=PROJECT, object_kinds=["video"]
        )
    assert section["status"] == "unavailable"
    assert section["reason"]["code"] == registry.REASON_ENTITY_RULE_SETS_PENDING
    # Unavailable is not empty: no counter may be read as a measurement.
    assert "published_count" not in section
    assert "published" not in section


def test_rule_sets_names_each_published_version_and_each_type_without_one():
    head = {"id": "grs_1"}
    version = {
        "id": "grsv_9",
        "version_number": 4,
        "content_hash": "ab" * 32,
        "payload": {"derived_attributes": [{"name": "content_type"}]},
    }

    def _active(_conn, *, project_id, family, name):
        assert family == "entity_derivation"
        return (head, version) if name == "video" else None

    with (
        patch.object(registry, "_load_entity_derivation_family", return_value="entity_derivation"),
        patch("core.governance_rule_sets.active_version", side_effect=_active),
    ):
        section = registry._entity_rule_sets_section(
            _Conn(), project_id=PROJECT, object_kinds=["video", "venue"]
        )
    assert section["status"] == "ok"
    assert section["published_count"] == 1
    assert section["published"][0] == {
        "object_kind": "video",
        "rule_set_id": "grs_1",
        "version_id": "grsv_9",
        "version_number": 4,
        "content_hash": "ab" * 32,
        "derived_attribute_count": 1,
    }
    assert section["no_published_rule_set"] == ["venue"]


# ---------------------------------------------------------------------------
# The matching-coverage section: the 68.3 contract, pending.
# ---------------------------------------------------------------------------


def test_coverage_unavailable_when_68_3_has_not_landed():
    with patch.object(registry, "_load_matching_coverage_reader", return_value=None):
        section, unattached = registry._matching_coverage_section(_Conn(), project_id=PROJECT)
    assert section["status"] == "unavailable"
    assert section["reason"]["code"] == registry.REASON_ENTITY_MATCHING_PENDING
    assert unattached["group"] == "unmatched_occurrences"
    assert unattached["status"] == "unavailable"
    assert "count" not in unattached


def test_coverage_maps_the_coverage_report_contract_when_it_lands():
    report = {
        "state": "ok",
        "coverage": {
            "bound": 41,
            "eligible": 50,
            "by_state": {"resolved": 41, "unmatched": 7, "ambiguous": 2},
        },
    }
    with patch.object(
        registry, "_load_matching_coverage_reader", return_value=lambda *a, **k: report
    ):
        section, unattached = registry._matching_coverage_section(_Conn(), project_id=PROJECT)
    assert section == {
        "status": "ok",
        "resolved": 41,
        "eligible": 50,
        "by_state": {"resolved": 41, "unmatched": 7, "ambiguous": 2},
    }
    assert unattached == {"group": "unmatched_occurrences", "status": "ok", "count": 7}


def test_a_coverage_report_unavailable_stays_unavailable_never_zero():
    report = {
        "state": "unavailable",
        "unavailable_reason": {"code": "verdicts_store_down", "message": "..."},
    }
    with patch.object(
        registry, "_load_matching_coverage_reader", return_value=lambda *a, **k: report
    ):
        section, unattached = registry._matching_coverage_section(_Conn(), project_id=PROJECT)
    assert section["status"] == "unavailable"
    assert section["reason"]["code"] == "verdicts_store_down"
    assert unattached["status"] == "unavailable"


# ---------------------------------------------------------------------------
# The ONE read: assembly, envelope, and the unavailable alert.
# ---------------------------------------------------------------------------

_TYPES = [
    {
        "registry_id": "mdreg_A",
        "object_kind": "video",
        "canonical_key": "video_id",
        "display_name": "Videos",
        "lifecycle_state": "active",
        "version_scope": "node",
        "created_by": "a@example.com",
        "created_at": None,
        "updated_at": None,
        "live_source_count": 1,
        "node_count": 12,
    }
]


#: Ce que les deux sections de la story 69.4 rendent quand elles ont lu la base.
#: Stubees ici comme les autres seams de stockage : ce fichier prouve
#: l'ASSEMBLAGE, leurs requetes sont prouvees sur base dans
#: `tests/integration/test_entity_discovery_cross_pg.py`.
_CROSSABLE = {
    "status": "ok",
    "count": 2,
    "by_object_kind": [
        {
            "object_kind": "video",
            "carried": [{"attribute": "duration_seconds", "node_count": 12}],
            "derived": [
                {
                    "attribute": "content_type",
                    "rule_set_version_id": "grsv_9",
                    "node_count": 12,
                }
            ],
        }
    ],
    "without_attribute": [],
}
_FRESHNESS = {
    "status": "ok",
    "count": 1,
    "by_datastream": [
        {
            "datastream_id": "ds_1",
            "last_snapshot_at": "2026-08-20T00:00:00+00:00",
            "last_published_at": "2026-08-20T04:00:00+00:00",
            "published_import_count": 3,
        }
    ],
    "never_published": [],
}


def _assemble(
    *,
    matching_reader=None,
    derivation_family=None,
    crossable=None,
    freshness=None,
):
    """Run the ONE read with every store-level seam stubbed."""
    with (
        patch.object(registry, "list_entity_types", return_value=list(_TYPES)),
        patch.object(
            registry,
            "_current_mapping_version_payloads",
            return_value=[_version(designated={"video_id": "video"}, undeclared=["sess_id"])],
        ),
        patch.object(registry, "_load_entity_derivation_family", return_value=derivation_family),
        patch.object(registry, "_load_matching_coverage_reader", return_value=matching_reader),
        patch.object(
            registry,
            "_crossable_attributes_section",
            return_value=dict(crossable if crossable is not None else _CROSSABLE),
        ),
        patch.object(
            registry,
            "_import_freshness_section",
            return_value=dict(freshness if freshness is not None else _FRESHNESS),
        ),
    ):
        return registry.describe_entity_reconciliation_context(object(), project_id=PROJECT)


def test_the_read_is_a_conformant_ad_1_envelope():
    envelope = _assemble()
    jsonschema.validate(envelope, _ENVELOPE_SCHEMA)
    assert envelope["schema_version"] == "1"
    assert envelope["meta"]["freshness"] == "live"
    assert envelope["meta"]["provenance"]["source_field"] == "entity_reconciliation_context"


def test_every_section_and_every_unattached_group_is_named():
    data = _assemble()["data"]
    assert data["entity_types"]["status"] == "ok"
    assert data["entity_types"]["count"] == 1
    assert data["bindings"]["status"] == "ok"
    # Both dependencies pending: named unavailable, never a fake zero.
    assert data["rule_sets"]["status"] == "unavailable"
    assert data["matching_coverage"]["status"] == "unavailable"
    # Story 69.4 : les deux sections qui rendent un agent honnete PAR
    # CONSTRUCTION -- il ne peut promettre que ce que la decouverte annonce.
    assert data["crossable_attributes"]["status"] == "ok"
    assert data["crossable_attributes"]["by_object_kind"][0]["derived"][0][
        "rule_set_version_id"
    ] == "grsv_9"
    assert data["import_freshness"]["status"] == "ok"
    groups = {group["group"]: group for group in data["unattached"]}
    assert set(groups) == {"undeclared_key_candidates", "unmatched_occurrences"}
    assert groups["undeclared_key_candidates"]["count"] == 1
    assert groups["unmatched_occurrences"]["status"] == "unavailable"


def test_an_unavailable_section_raises_the_partial_alert():
    meta = _assemble()["meta"]
    assert [a["code"] for a in meta["alerts"]] == ["entity_context_partial"]
    assert "rule_sets" in meta["alerts"][0]["message"]
    assert "matching_coverage" in meta["alerts"][0]["message"]


def test_a_fully_landed_read_carries_no_alert():
    report = {"state": "ok", "coverage": {"bound": 1, "eligible": 1, "by_state": {}}}
    with patch("core.governance_rule_sets.active_version", return_value=None):
        envelope = _assemble(
            matching_reader=lambda *a, **k: report, derivation_family="entity_derivation"
        )
    assert envelope["meta"]["alerts"] == []
    assert envelope["data"]["rule_sets"]["no_published_rule_set"] == ["video"]


def test_a_project_with_nothing_declared_says_so_and_never_404s():
    with (
        patch.object(registry, "list_entity_types", return_value=[]),
        patch.object(registry, "_current_mapping_version_payloads", return_value=[]),
        patch.object(registry, "_load_entity_derivation_family", return_value=None),
        patch.object(registry, "_load_matching_coverage_reader", return_value=None),
        patch.object(
            registry,
            "_crossable_attributes_section",
            return_value={
                "status": "ok",
                "count": 0,
                "by_object_kind": [],
                "without_attribute": [],
            },
        ),
        patch.object(
            registry,
            "_import_freshness_section",
            return_value={
                "status": "ok",
                "count": 0,
                "by_datastream": [],
                "never_published": [],
            },
        ),
    ):
        data = registry.describe_entity_reconciliation_context(object(), project_id=PROJECT)[
            "data"
        ]
    assert data["entity_types"]["empty_reason"]["code"] == "no_entity_type_declared"
    assert data["bindings"]["designation_count"] == 0


# ---------------------------------------------------------------------------
# AD-2: no entity-kind literal in the new modules.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["entity_context_mcp.py", "entity_context_api.py"])
def test_no_object_kind_literal_appears_in_the_doors(module):
    source = (_CORE / module).read_text(encoding="utf-8")
    executable = source[source.index("from __future__") :]
    for suspect in ("video", "product", "venue", "country", "market"):
        assert not re.search(rf'object_kind\s*==\s*[\'"]{suspect}', executable)
        assert f'"{suspect}"' not in executable, f"{suspect!r} is a kind literal"


# ---------------------------------------------------------------------------
# Story 69.4: an agent can only promise what the discovery announces.
# ---------------------------------------------------------------------------


def test_a_type_with_nothing_to_cross_by_is_named_not_omitted():
    data = _assemble(
        crossable={
            "status": "ok",
            "count": 0,
            "by_object_kind": [{"object_kind": "video", "carried": [], "derived": []}],
            "without_attribute": ["video"],
        }
    )["data"]
    # "Cette entite ne porte rien par quoi l'analyser" est exactement ce qu'un
    # modele doit savoir AVANT de promettre une analyse. L'omettre le laisserait
    # decouvrir l'impossibilite en essayant, et la rapporter comme "pas de
    # donnees".
    assert data["crossable_attributes"]["without_attribute"] == ["video"]


def test_a_feed_that_never_published_is_named_with_a_null_date():
    data = _assemble(
        freshness={
            "status": "ok",
            "count": 1,
            "by_datastream": [
                {
                    "datastream_id": "ds_1",
                    "last_snapshot_at": None,
                    "last_published_at": None,
                    "published_import_count": 0,
                }
            ],
            "never_published": ["ds_1"],
        }
    )["data"]
    # << jamais >> et << pas dans la liste >> sont deux faits differents, et un
    # seul des deux nomme un geste.
    assert data["import_freshness"]["never_published"] == ["ds_1"]
