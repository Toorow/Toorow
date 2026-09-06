"""Tests for Story 27.9 -- inverse lineage ("fed-by") + client-owned dimension labels.

Offline (no DB): the manifest reader over a temporary modules tree, the PLAN x MANIFEST
join (report grain preserved, every un-joinable piece reported as a gap and never
guessed), the per-connector value-mapping summary (status EXPOSED, never bypassed), the
assembled read model, the label cascade reducer + fallback, the render-envelope
projection, and the AD-2 "no provider/dimension name in the module" grep.

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of migration 106
(scope CHECK, COALESCE unicity, blank guard, FK CASCADE), the label cascade over real
rows and the audit reuse of app.metric_semantics_audit (049). Pattern calque sur
test_dimension_conformance.py.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import pytest

from tests.conftest import purge_fixture_org

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import dimension_conformance as dc  # noqa: E402
from core import dimension_lineage as dl  # noqa: E402

from tests.support.updated_at_trigger import ensure_set_updated_at

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_049 = _REPO_ROOT / "infra" / "nango" / "migrations" / "049_metric_semantics.sql"
_MIGRATION_106 = _REPO_ROOT / "infra" / "nango" / "migrations" / "106_dimension_labels.sql"


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


# ===========================================================================
# Helpers -- opaque fixtures (AD-2: no real provider/dimension name anywhere)
# ===========================================================================

DIM = "dim_alpha"          # the conformed dimension under test (client identifier)
CONN_A = "connector-a"
CONN_B = "connector-b"


def _plan_row(
    *,
    datastream_id="ds_1",
    name="stream one",
    connector=CONN_A,
    report_id="report_one",
    dimensions=("field_x",),
    payload=None,
    plan_version_id="dsp_1",
    version_number=3,
    enabled=True,
    archived=False,
):
    if payload is None and report_id is not None:
        payload = {
            "source": {
                "kind": "connector_pull",
                "report_id": report_id,
                "selection": {"dimensions": list(dimensions), "metrics": ["m"]},
            }
        }
    return {
        "datastream_id": datastream_id,
        "datastream_name": name,
        "connector": connector,
        "plan_version_id": plan_version_id,
        "version_number": version_number,
        "payload": payload,
        "enabled": enabled,
        "archived": archived,
    }


def _mapping_row(
    connector, source_value, canonical_value, scope, status=dc.STATUS_CONFIRMED,
    org_id=None, project_id=None,
):
    return {
        "connector": connector,
        "source_value": source_value,
        "canonical_value": canonical_value,
        "scope_level": scope,
        "status": status,
        "org_id": org_id,
        "project_id": project_id,
    }


# ===========================================================================
# A. Manifest reader (schema level)
# ===========================================================================


def _write_manifest(base: Path, connector: str, mapping) -> None:
    directory = base / connector
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"name": connector}
    if mapping is not None:
        payload["canonical_dimension_mapping"] = mapping
    (directory / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_manifest_index_reads_dimension_mappings(tmp_path):
    """The index is keyed by module DIRECTORY name (the connector identity)."""
    _write_manifest(tmp_path, CONN_A, {"field_x": DIM, "field_y": "dim_beta"})
    index = dl.read_manifest_dimension_mappings(tmp_path)
    assert index == {CONN_A: {"field_x": DIM, "field_y": "dim_beta"}}


def test_manifest_index_accepts_dict_targets(tmp_path):
    """A qualified target ({'canonical': ...}) is accepted; junk shapes are skipped."""
    _write_manifest(
        tmp_path, CONN_A, {"field_x": {"canonical": DIM}, "field_z": {"other": 1}}
    )
    assert dl.read_manifest_dimension_mappings(tmp_path) == {CONN_A: {"field_x": DIM}}


def test_manifest_index_skips_unusable_directories(tmp_path):
    """No manifest / no mapping key / invalid JSON -> ABSENT from the index, not guessed."""
    (tmp_path / "no-manifest").mkdir()
    _write_manifest(tmp_path, "no-mapping", None)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    _write_manifest(tmp_path, CONN_A, {"field_x": DIM})
    assert set(dl.read_manifest_dimension_mappings(tmp_path)) == {CONN_A}


def test_manifest_index_missing_dir_is_empty(tmp_path):
    """An unreadable modules dir yields {} (fail-soft), never an exception."""
    assert dl.read_manifest_dimension_mappings(tmp_path / "absent") == {}


# ===========================================================================
# B. PLAN x MANIFEST join -- the report grain is the point of the story
# ===========================================================================


def test_same_field_on_two_reports_stays_two_rows():
    """THE story: one field pulled from two reports must NOT collapse to one connector row."""
    index = {CONN_A: {"field_x": DIM}}
    rows = [
        _plan_row(datastream_id="ds_1", report_id="report_one"),
        _plan_row(datastream_id="ds_2", report_id="report_two"),
    ]
    uses, gaps = dl.extract_plan_field_uses(
        rows, manifest_index=index, canonical_dimension=DIM
    )
    assert [(u.report_id, u.source_field) for u in uses] == [
        ("report_one", "field_x"),
        ("report_two", "field_x"),
    ]
    assert gaps == []


def test_only_fields_targeting_the_dimension_are_kept():
    """A selected field mapped to ANOTHER dimension is not evidence for this one."""
    index = {CONN_A: {"field_x": DIM, "field_y": "dim_beta"}}
    uses, _ = dl.extract_plan_field_uses(
        [_plan_row(dimensions=("field_x", "field_y"))],
        manifest_index=index,
        canonical_dimension=DIM,
    )
    assert [u.source_field for u in uses] == ["field_x"]


def test_missing_report_id_is_unknown_not_invented():
    """A payload without report_id still proves the field is pulled -- report = 'unknown'."""
    payload = {"source": {"kind": "managed_feed", "selection": {"dimensions": ["field_x"]}}}
    uses, _ = dl.extract_plan_field_uses(
        [_plan_row(payload=payload, report_id=None)],
        manifest_index={CONN_A: {"field_x": DIM}},
        canonical_dimension=DIM,
    )
    assert [u.report_id for u in uses] == [dl.UNKNOWN]


def test_gap_when_no_plan_version():
    """A datastream without a current plan version is REPORTED, never dropped silently."""
    uses, gaps = dl.extract_plan_field_uses(
        [_plan_row(payload=None, report_id=None)],
        manifest_index={CONN_A: {"field_x": DIM}},
        canonical_dimension=DIM,
    )
    assert uses == []
    assert [g["reason"] for g in gaps] == [dl.GAP_NO_PLAN_VERSION]


def test_gap_when_connector_unknown():
    """module_name absent -> we cannot name the connector; say so."""
    _, gaps = dl.extract_plan_field_uses(
        [_plan_row(connector=None)], manifest_index={}, canonical_dimension=DIM
    )
    assert [g["reason"] for g in gaps] == [dl.GAP_CONNECTOR_UNKNOWN]
    assert gaps[0]["connector"] == dl.UNKNOWN


def test_gap_when_manifest_mapping_unavailable():
    """No schema-level mapping for that connector -> gap, and NO invented field."""
    uses, gaps = dl.extract_plan_field_uses(
        [_plan_row()], manifest_index={}, canonical_dimension=DIM
    )
    assert uses == []
    assert [g["reason"] for g in gaps] == [dl.GAP_SCHEMA_MAPPING_UNAVAILABLE]
    assert gaps[0]["report_id"] == "report_one"


def test_gap_when_plan_declares_no_dimensions():
    """A plan kind that declares no dimensions cannot feed anything -- reported as such."""
    payload = {"source": {"kind": "external_bq", "report_id": "report_one"}}
    uses, gaps = dl.extract_plan_field_uses(
        [_plan_row(payload=payload)],
        manifest_index={CONN_A: {"field_x": DIM}},
        canonical_dimension=DIM,
    )
    assert uses == []
    assert [g["reason"] for g in gaps] == [dl.GAP_NO_DECLARED_DIMENSIONS]


def test_payload_accepted_as_json_string():
    """A payload handed over as a JSON string joins exactly like a dict."""
    payload = json.dumps(
        {"source": {"report_id": "report_one", "selection": {"dimensions": ["field_x"]}}}
    )
    uses, _ = dl.extract_plan_field_uses(
        [_plan_row(payload=payload)],
        manifest_index={CONN_A: {"field_x": DIM}},
        canonical_dimension=DIM,
    )
    assert len(uses) == 1


def test_extract_is_deterministic():
    """Same input -> byte-identical ordered output (stable sort)."""
    index = {CONN_A: {"field_x": DIM}, CONN_B: {"field_w": DIM}}
    rows = [
        _plan_row(datastream_id="ds_2", connector=CONN_B, dimensions=("field_w",)),
        _plan_row(datastream_id="ds_1"),
    ]
    first, _ = dl.extract_plan_field_uses(
        rows, manifest_index=index, canonical_dimension=DIM
    )
    second, _ = dl.extract_plan_field_uses(
        rows, manifest_index=index, canonical_dimension=DIM
    )
    assert first == second
    assert [u.connector for u in first] == [CONN_A, CONN_B]


# ===========================================================================
# C. Value-level summary -- status exposed, never bypassed
# ===========================================================================


def test_summary_counts_and_status_partial():
    rows = [
        _mapping_row(CONN_A, "v1", "CANON", dc.SCOPE_ORG, org_id="org_1"),
        _mapping_row(
            CONN_A, "v2", "CANON", dc.SCOPE_ORG, status=dc.STATUS_PROPOSED, org_id="org_1"
        ),
    ]
    summary = dl.summarize_mappings_by_connector(rows)[CONN_A]
    assert summary["status"] == dl.MAPPING_STATUS_PARTIAL
    assert summary["counts"][dc.STATUS_CONFIRMED] == 1
    assert summary["counts"][dc.STATUS_PROPOSED] == 1
    assert summary["resolved_value_count"] == 1
    assert summary["grain"] == dl.MAPPING_GRAIN_CONNECTOR


def test_summary_proposed_only_resolves_nothing():
    """AD-9 held: proposed rows are SHOWN but resolve zero values."""
    rows = [
        _mapping_row(
            CONN_A, "v1", "CANON", dc.SCOPE_ORG, status=dc.STATUS_PROPOSED, org_id="org_1"
        )
    ]
    summary = dl.summarize_mappings_by_connector(rows)[CONN_A]
    assert summary["status"] == dl.MAPPING_STATUS_PROPOSED
    assert summary["resolved_value_count"] == 0
    assert summary["confirmed_scopes"] == []


def test_summary_rejected_only():
    rows = [
        _mapping_row(
            CONN_A, "v1", "CANON", dc.SCOPE_ORG, status=dc.STATUS_REJECTED, org_id="org_1"
        )
    ]
    assert (
        dl.summarize_mappings_by_connector(rows)[CONN_A]["status"]
        == dl.MAPPING_STATUS_REJECTED
    )


def test_summary_reports_the_scope_that_won():
    """The PROJECT override wins the pair -- and that is the scope reported."""
    rows = [
        _mapping_row(CONN_A, "v1", "PLAT", dc.SCOPE_PLATFORM),
        _mapping_row(CONN_A, "v1", "PROJ", dc.SCOPE_PROJECT, project_id="proj_1"),
    ]
    summary = dl.summarize_mappings_by_connector(rows)[CONN_A]
    assert summary["confirmed_scopes"] == [dc.SCOPE_PROJECT]
    assert summary["resolved_value_count"] == 1


# ===========================================================================
# D. The assembled read model
# ===========================================================================


def _label(display=None, scope=None):
    if display is None:
        return {
            "display_label": DIM,
            "scope_level": None,
            "label_source": dc.LABEL_SOURCE_FALLBACK,
        }
    return {
        "display_label": display,
        "scope_level": scope,
        "label_source": dc.LABEL_SOURCE_CLIENT,
    }


def test_fed_by_row_shape_carries_the_three_levels():
    """One row = connector + report + field + mapping status + confirmed scope."""
    uses, gaps = dl.extract_plan_field_uses(
        [_plan_row()],
        manifest_index={CONN_A: {"field_x": DIM}},
        canonical_dimension=DIM,
    )
    result = dl.build_fed_by(
        canonical_dimension=DIM,
        label=_label("Client word", dc.SCOPE_ORG),
        plan_uses=uses,
        gaps=gaps,
        mapping_rows=[_mapping_row(CONN_A, "v1", "CANON", dc.SCOPE_ORG, org_id="org_1")],
        project_id="proj_1",
        org_id="org_1",
    )
    assert result["canonical_dimension"] == DIM
    assert result["display_label"] == "Client word"
    assert result["label_source"] == dc.LABEL_SOURCE_CLIENT
    (entry,) = result["fed_by"]
    assert entry["connector"] == CONN_A
    assert entry["report_id"] == "report_one"
    assert entry["source_field"] == "field_x"
    assert entry["schema_link"] == dl.SCHEMA_LINK_MANIFEST
    assert entry["mapping"]["status"] == dl.MAPPING_STATUS_CONFIRMED
    assert entry["mapping"]["confirmed_scopes"] == [dc.SCOPE_ORG]
    assert entry["datastreams"][0]["datastream_id"] == "ds_1"
    assert entry["datastreams"][0]["plan_version_number"] == 3


def test_fed_by_keeps_report_grain_across_datastreams():
    """Two reports of the SAME connector stay two rows; same report groups datastreams."""
    uses, gaps = dl.extract_plan_field_uses(
        [
            _plan_row(datastream_id="ds_1", report_id="report_one"),
            _plan_row(datastream_id="ds_2", report_id="report_two"),
            _plan_row(datastream_id="ds_3", report_id="report_one"),
        ],
        manifest_index={CONN_A: {"field_x": DIM}},
        canonical_dimension=DIM,
    )
    result = dl.build_fed_by(
        canonical_dimension=DIM, label=_label(), plan_uses=uses, gaps=gaps,
        mapping_rows=[],
    )
    reports = [(e["report_id"], len(e["datastreams"])) for e in result["fed_by"]]
    assert reports == [("report_one", 2), ("report_two", 1)]


def test_fed_by_without_mappings_says_none():
    """A source feeds the dimension but nothing is mapped yet -- status 'none', honest."""
    uses, gaps = dl.extract_plan_field_uses(
        [_plan_row()], manifest_index={CONN_A: {"field_x": DIM}}, canonical_dimension=DIM
    )
    result = dl.build_fed_by(
        canonical_dimension=DIM, label=_label(), plan_uses=uses, gaps=gaps, mapping_rows=[]
    )
    assert result["fed_by"][0]["mapping"]["status"] == dl.MAPPING_STATUS_NONE
    assert result["fed_by"][0]["mapping"]["resolved_value_count"] == 0


def test_fed_by_mapping_without_plan_evidence_is_listed_and_flagged():
    """Value mappings on a connector no live plan feeds: shown with 'unknown' + a gap."""
    result = dl.build_fed_by(
        canonical_dimension=DIM,
        label=_label(),
        plan_uses=[],
        gaps=[],
        mapping_rows=[_mapping_row(CONN_B, "v1", "CANON", dc.SCOPE_ORG, org_id="org_1")],
    )
    (entry,) = result["fed_by"]
    assert entry["connector"] == CONN_B
    assert entry["report_id"] == dl.UNKNOWN
    assert entry["source_field"] == dl.UNKNOWN
    assert entry["schema_link"] == dl.SCHEMA_LINK_UNAVAILABLE
    assert entry["datastreams"] == []
    assert {g["reason"] for g in result["gaps"]} == {dl.GAP_NO_PLAN_EVIDENCE}


def test_fed_by_falls_back_to_identifier_when_unlabelled():
    """No client label -> the identifier is shown AND the fallback is declared."""
    result = dl.build_fed_by(
        canonical_dimension=DIM, label=_label(), plan_uses=[], gaps=[], mapping_rows=[]
    )
    assert result["display_label"] == DIM
    assert result["label_source"] == dc.LABEL_SOURCE_FALLBACK
    assert result["label_scope"] is None


def test_fed_by_is_deterministic():
    uses, gaps = dl.extract_plan_field_uses(
        [
            _plan_row(datastream_id="ds_2", connector=CONN_B, dimensions=("field_w",)),
            _plan_row(datastream_id="ds_1"),
        ],
        manifest_index={CONN_A: {"field_x": DIM}, CONN_B: {"field_w": DIM}},
        canonical_dimension=DIM,
    )
    args = dict(
        canonical_dimension=DIM, label=_label(), plan_uses=uses, gaps=gaps,
        mapping_rows=[_mapping_row(CONN_A, "v1", "CANON", dc.SCOPE_PLATFORM)],
    )
    assert dl.build_fed_by(**args) == dl.build_fed_by(**args)


# ===========================================================================
# E. The client label -- cascade, fallback, and carrying it to the surfaces
# ===========================================================================


def _label_row(dimension, label, scope, org_id=None, project_id=None):
    return {
        "canonical_dimension": dimension,
        "display_label": label,
        "description": None,
        "scope_level": scope,
        "org_id": org_id,
        "project_id": project_id,
    }


def test_label_cascade_project_beats_org_beats_platform():
    rows = [
        _label_row(DIM, "platform word", dc.SCOPE_PLATFORM),
        _label_row(DIM, "org word", dc.SCOPE_ORG, org_id="org_1"),
        _label_row(DIM, "project word", dc.SCOPE_PROJECT, project_id="proj_1"),
    ]
    winner = dc.reduce_labels_by_specificity(rows)[DIM]
    assert winner["display_label"] == "project word"
    assert winner["scope_level"] == dc.SCOPE_PROJECT


def test_label_reducer_ignores_blank_labels():
    """A blank label would show the user nothing -- it never wins the cascade."""
    rows = [
        _label_row(DIM, "org word", dc.SCOPE_ORG, org_id="org_1"),
        _label_row(DIM, "   ", dc.SCOPE_PROJECT, project_id="proj_1"),
    ]
    assert dc.reduce_labels_by_specificity(rows)[DIM]["display_label"] == "org word"


def test_label_reducer_deterministic():
    rows = [
        _label_row(DIM, "platform word", dc.SCOPE_PLATFORM),
        _label_row(DIM, "org word", dc.SCOPE_ORG, org_id="org_1"),
    ]
    assert dc.reduce_labels_by_specificity(rows) == dc.reduce_labels_by_specificity(rows)


def test_build_label_map_always_answers_for_every_dimension():
    """A reading surface must always find something to print -- and know what it is."""
    resolved = {DIM: {"display_label": "Client word", "description": None,
                      "scope_level": dc.SCOPE_ORG}}
    label_map = dl.build_label_map([DIM, "dim_beta"], resolved)
    assert label_map[DIM]["display_label"] == "Client word"
    assert label_map[DIM]["label_source"] == dc.LABEL_SOURCE_CLIENT
    assert label_map["dim_beta"]["display_label"] == "dim_beta"
    assert label_map["dim_beta"]["label_source"] == dc.LABEL_SOURCE_FALLBACK


def test_decorate_envelope_is_additive_and_non_mutating():
    """meta.dimension_labels is an additive AD-1 key; the input envelope is untouched."""
    envelope = {"schema_version": "1", "meta": {"alerts": []}, "data": {}}
    label_map = dl.build_label_map([DIM], {})
    decorated = dl.decorate_envelope_with_labels(envelope, label_map)
    assert decorated["meta"]["dimension_labels"][DIM]["display_label"] == DIM
    assert decorated["meta"]["alerts"] == []
    assert "dimension_labels" not in envelope["meta"]


def test_decorate_envelope_omits_the_key_when_empty():
    """Never a null key: an empty map leaves the envelope exactly as it was."""
    envelope = {"schema_version": "1", "meta": {"alerts": []}, "data": {}}
    assert "dimension_labels" not in dl.decorate_envelope_with_labels(envelope, {})["meta"]


# ===========================================================================
# F. AD-2 -- no provider / dimension vocabulary in the new modules
# ===========================================================================


@pytest.mark.parametrize("module", [dl, dc])
def test_no_provider_or_dimension_name_in_module(module):
    """AD-2: connector and dimension names come from data, never from the code."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    forbidden = [
        "google-analytics", "google_analytics", "meta-ads", "meta_ads",
        "tiktok", "linkedin", "shopify", "stripe", "adjust",
        "facebook", "pinterest", "amazon", "microsoft", "snapchat",
        "audience_language", "targeting_language", "content_language",
        "'country'", "breakdown_dimension",
    ]
    hits = [name for name in forbidden if name in lowered]
    assert not hits, f"provider/dimension name(s) hard-coded: {hits}"


def test_migration_103_is_replayable_and_scoped():
    """Migration 106 is additive/idempotent and repeats the 049/052 scope contract."""
    sql = _MIGRATION_106.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.dimension_labels" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_dimension_labels_scope_key" in sql
    assert "COALESCE(org_id, ''), COALESCE(project_id, '')" in sql
    assert "ck_dimension_labels_scope_cols" in sql
    assert "DROP TABLE" not in sql.upper()


# ===========================================================================
# Live Postgres -- the real DDL of migration 106 + the label store
# ===========================================================================


def _label_id() -> str:
    """A conforming `dlb_<ULID>` — the shape migration 301 enforces.

    This used to be `f"dlb_{uuid.uuid4().hex}"`, 32 hex characters. Two of the
    tests below assert a `CheckViolation` on the SCOPE triplet, so once 301 posed
    `ck_dimension_labels_id` they would have kept passing on the id shape instead
    — green for a reason they do not name. The production writer mints exactly
    this (`dimension_conformance._mint_id('dlb_')`), so the fixture now mints what
    production mints.
    """
    from ulid import ULID

    return f"dlb_{ULID()}"


def _apply_migration(conn, path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _ensure_set_updated_at(conn) -> None:
    """See `tests.support.updated_at_trigger`: ask before replacing."""
    ensure_set_updated_at(conn)


def _prepare(conn) -> None:
    """Ensure set_updated_at + 049 (shared audit) + 103 applied idempotently."""
    _ensure_set_updated_at(conn)
    _apply_migration(conn, _MIGRATION_049)
    _apply_migration(conn, _MIGRATION_106)


def _seed_org(conn, suffix: str, *, owner: str | None = None) -> str:
    """Seed an organization, and optionally the OWNER who will write in it.

    THE MEMBER ROW IS NOT DECORATION -- 2026-08-21. `set_dimension_label` and
    `delete_dimension_label` acquire by `core.db.request_connection(identity)`
    since the org branch of the MCP door was closed, so the RLS policy of
    `274_a_row_without_an_org_belongs_to_no_tenant.sql:50-55` now governs the
    UPSERT as well as the read. An identity with no `app.org_members` row is
    refused by the floor -- which is the floor doing its job, and exactly what a
    caller reaching this function has already proven to the application guard.
    Seeding the membership makes the fixture say the same thing the guard says.
    """
    org_id = f"dl_org_{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s,%s,%s,'system')",
            (org_id, f"DLOrg-{suffix}", f"dl-org-{suffix}"),
        )
        if owner is not None:
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status) "
                "VALUES (%s,%s,%s,'owner','active')",
                (f"om_{suffix}", org_id, owner),
            )
    conn.commit()
    return org_id


def _drop_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as clean:
        # Through the graph the production purge walks: `mdm_business_domains`
        # holds an org by ON DELETE RESTRICT, and the next governed table will too.
        purge_fixture_org(clean, org_id)
        clean.commit()


@pg_available
@pytest.mark.pg_owner
def test_live_ddl_replayable():
    """Migration 106 creates the table + unique index, and replaying it is a no-op."""
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        _apply_migration(conn, _MIGRATION_106)  # replay must not error
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='app' AND table_name='dimension_labels'"
            )
            assert cur.fetchone() is not None


@pg_available
@pytest.mark.pg_owner
def test_live_scope_check_and_blank_guard():
    """The scope triplet CHECK and the not-blank CHECK are enforced by the DDL."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    with get_connection() as conn:
        _prepare(conn)
        org_id = _seed_org(conn, suffix)
        try:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO app.dimension_labels "
                        "(id, canonical_dimension, display_label, scope_level, org_id,"
                        " created_by) VALUES (%s,%s,'x','PLATFORM',%s,'system')",
                        (_label_id(), DIM, org_id),
                    )
            conn.rollback()
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO app.dimension_labels "
                        "(id, canonical_dimension, display_label, scope_level, org_id,"
                        " created_by) VALUES (%s,%s,'   ','ORG',%s,'system')",
                        (_label_id(), DIM, org_id),
                    )
            conn.rollback()
        finally:
            _drop_org(org_id)



@pg_available
@pytest.mark.pg_owner
def test_live_the_identity_shape_is_enforced_and_not_only_documented():
    """Migration 301. The column said `-- ULID prefixe : 'dlb_'` and nothing held it.

    Migration 105 seeded three PLATFORM rows under deterministic keys
    (`dlb_27_8_<dimension>`) and 174, repairing 105's no-op, repeated exactly the
    same three. Measured in preprod on 2026-08-23: every row in the table broke the
    contract its own column documents. An identifier that names the story that
    seeded it reads as data, invites being rebuilt by concatenation elsewhere, and
    collides on a primary key the day two seeds pick the same convention.
    """
    import psycopg
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO app.dimension_labels "
                    "(id, canonical_dimension, display_label, scope_level, created_by) "
                    "VALUES (%s,%s,'x','PLATFORM','system')",
                    ("dlb_27_8_audience_language", DIM),
                )
        conn.rollback()
        # And the excluded letters are excluded: `I`, `L`, `O` and `U` read as
        # 1, 1, 0 and V, and an identity nobody can dictate is not an identity.
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO app.dimension_labels "
                    "(id, canonical_dimension, display_label, scope_level, created_by) "
                    "VALUES (%s,%s,'x','PLATFORM','system')",
                    ("dlb_0000000000000000000000000I", DIM),
                )
        conn.rollback()
        # What the seeds now carry, and what the production writer mints, pass.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM app.dimension_labels WHERE scope_level = 'PLATFORM'"
            )
            seeded = [row[0] for row in cur.fetchall()]
        assert seeded, "the platform seeds of migration 174 are gone"
        assert all(re.fullmatch(r"dlb_[0-9A-HJKMNP-TV-Z]{26}", value) for value in seeded), (
            f"a seeded identity still breaks the shape: {seeded}"
        )


@pg_available
@pytest.mark.pg_owner
def test_live_the_production_writer_mints_the_shape_the_column_demands():
    """The guard is only worth what the WRITER produces: a constraint nothing feeds
    correctly would turn the first client rename into a 500."""
    from core.dimension_conformance import _LABEL_ID_PREFIX, _mint_id

    minted = _mint_id(_LABEL_ID_PREFIX)
    assert re.fullmatch(r"dlb_[0-9A-HJKMNP-TV-Z]{26}", minted), minted

@pg_available
@pytest.mark.pg_owner
def test_live_unicity_per_scope_and_org_cascade():
    """One label per (scope, dimension); deleting the org takes its labels with it."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    with get_connection() as conn:
        _prepare(conn)
        org_id = _seed_org(conn, suffix)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.dimension_labels "
                    "(id, canonical_dimension, display_label, scope_level, org_id,"
                    " created_by) VALUES (%s,%s,'first','ORG',%s,'system')",
                    (_label_id(), DIM, org_id),
                )
            conn.commit()
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.UniqueViolation):
                    cur.execute(
                        "INSERT INTO app.dimension_labels "
                        "(id, canonical_dimension, display_label, scope_level, org_id,"
                        " created_by) VALUES (%s,%s,'second','ORG',%s,'system')",
                        (_label_id(), DIM, org_id),
                    )
            conn.rollback()
        finally:
            _drop_org(org_id)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.dimension_labels WHERE org_id = %s", (org_id,)
            )
            assert cur.fetchone()[0] == 0


@pg_available
@pytest.mark.pg_owner
def test_live_label_cascade_and_audit():
    """set_dimension_label writes an audited row; the ORG label wins over PLATFORM."""
    from core.db import get_connection
    from core.dimension_conformance import (
        delete_dimension_label,
        resolve_dimension_label,
        set_dimension_label,
    )

    suffix = uuid.uuid4().hex[:8]
    dimension = f"dim_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        org_id = _seed_org(conn, suffix, owner="alice")
    try:
        set_dimension_label(
            canonical_dimension=dimension,
            display_label="org word",
            scope_level=dc.SCOPE_ORG,
            org_id=org_id,
            project_id=None,
            identity="alice",
        )
        resolved = resolve_dimension_label(dimension, org_id=org_id)
        assert resolved["display_label"] == "org word"
        assert resolved["label_source"] == dc.LABEL_SOURCE_CLIENT
        assert resolved["scope_level"] == dc.SCOPE_ORG

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type = %s AND org_id = %s",
                    (dc.ENTITY_TYPE_LABEL, org_id),
                )
                assert cur.fetchone()[0] >= 1

        assert delete_dimension_label(
            canonical_dimension=dimension,
            scope_level=dc.SCOPE_ORG,
            org_id=org_id,
            project_id=None,
            identity="alice",
        )
        after = resolve_dimension_label(dimension, org_id=org_id)
        assert after["display_label"] == dimension
        assert after["label_source"] == dc.LABEL_SOURCE_FALLBACK
    finally:
        _drop_org(org_id)


# ===========================================================================
# Story 27.8 -- a CONFIRMED client binding is executable, and it wins.
#
# `resolve_field_bindings` shipped on 2026-08-01 and the repair note said so in
# as many words: "RESTE, NOMME : resolve_field_bindings n'a toujours aucun
# appelant de production". Two REST routes and an MCP tool let a client confirm a
# binding, the row landed in `app.dimension_field_bindings` -- and every read went
# on answering from the manifest alone. A confirmation that changes nothing
# downstream is a form filled in, not a decision taken.
#
# The rule is written in docs/product-architecture/governance.md, "A manifest
# binding is shipped evidence; an ambiguous field needs a person".
# ===========================================================================


def test_a_confirmed_binding_feeds_a_dimension_the_manifest_never_mapped():
    """The case the whole binding flow exists for: a `pending` field, decided by a person.

    The manifest deliberately says nothing about this field -- binding it there on name
    similarity is what 27.8 forbids -- so without the client's confirmed row the field
    is invisible to the lineage.
    """
    index = {CONN_A: {"field_other": "dim_beta"}}
    rows = [_plan_row(dimensions=("field_undecided",))]

    silent, _ = dl.extract_plan_field_uses(
        rows, manifest_index=index, canonical_dimension=DIM
    )
    assert silent == [], "the manifest must not settle an undecided field"

    decided, gaps = dl.extract_plan_field_uses(
        rows,
        manifest_index=index,
        canonical_dimension=DIM,
        field_bindings={(CONN_A, "report_one", "field_undecided"): DIM},
    )
    assert [(u.report_id, u.source_field) for u in decided] == [
        ("report_one", "field_undecided")
    ]
    assert gaps == []


def test_a_confirmed_binding_wins_over_the_manifest_for_the_field_it_names():
    """A person who decided outranks a shipped default -- for THAT field, and no other."""
    index = {CONN_A: {"field_x": DIM, "field_y": "dim_beta"}}
    uses, _ = dl.extract_plan_field_uses(
        [_plan_row(dimensions=("field_x", "field_y"))],
        manifest_index=index,
        canonical_dimension=DIM,
        field_bindings={(CONN_A, "report_one", "field_y"): DIM},
    )
    assert [u.source_field for u in uses] == ["field_x", "field_y"]

    # And the reverse: a client who moved a field AWAY from this dimension removes it.
    moved, _ = dl.extract_plan_field_uses(
        [_plan_row(dimensions=("field_x",))],
        manifest_index=index,
        canonical_dimension=DIM,
        field_bindings={(CONN_A, "report_one", "field_x"): "dim_beta"},
    )
    assert moved == []


def test_a_binding_is_scoped_to_the_report_it_names_and_no_other():
    """The key carries the report BECAUSE one field can mean two things in two reports.

    That is 27.9's whole claim about grain, exercised on the binding side: a decision
    taken for `report_one` says nothing about `report_two`.
    """
    index = {CONN_A: {"field_other": "dim_beta"}}
    uses, _ = dl.extract_plan_field_uses(
        [
            _plan_row(datastream_id="ds_1", report_id="report_one", dimensions=("field_z",)),
            _plan_row(datastream_id="ds_2", report_id="report_two", dimensions=("field_z",)),
        ],
        manifest_index=index,
        canonical_dimension=DIM,
        field_bindings={(CONN_A, "report_one", "field_z"): DIM},
    )
    assert [(u.report_id, u.source_field) for u in uses] == [("report_one", "field_z")]


def test_no_binding_at_all_answers_exactly_as_before():
    """Additive: a project with no confirmed row reads what it always read."""
    index = {CONN_A: {"field_x": DIM}}
    rows = [_plan_row()]
    before, _ = dl.extract_plan_field_uses(
        rows, manifest_index=index, canonical_dimension=DIM
    )
    after, _ = dl.extract_plan_field_uses(
        rows, manifest_index=index, canonical_dimension=DIM, field_bindings={}
    )
    assert [u.source_field for u in before] == [u.source_field for u in after] == ["field_x"]


def test_a_binding_store_that_cannot_be_read_serves_the_manifest_answer(monkeypatch):
    """Fail-soft, like the value and plan levels: never a refusal, never an invention."""
    def _explode(**_kwargs):
        raise RuntimeError("the binding store is unreachable")

    monkeypatch.setattr("core.language_dimensions.resolve_field_bindings", _explode)
    assert dl._confirmed_field_bindings(org_id="org_EXAMPLE", project_id="proj_EXAMPLE") == {}


# ===========================================================================
# F. EACH SCOPE'S OWN WORD -- not only the one that wins (2026-08-31).
#
# `get_fed_by` served the WINNING label and nothing else, and the console panel
# beside it pre-fills the word field from that answer. So the moment a PROJECT
# label overrode an ORG one, a person choosing "your whole organization" was
# offered the word DERIVED from the identifier -- over their organization's own
# chosen name. Pressing the button would have renamed it to a proposal.
# `governance.md`: *"a scope that already carries a name pre-fills anything but
# that name"* is on the Incomplete-if list this surface owns.
#
# Not one test below names a scope that is not in `dl.LABEL_SCOPES`, and the
# non-winning case is the one the old read could not express.
# ===========================================================================


def test_every_declared_scope_is_a_key_even_when_nothing_is_stored_there():
    """`None` and "not looked at" are not the same answer."""
    by_scope = dl.labels_by_scope([])
    assert set(by_scope) == set(dl.LABEL_SCOPES)
    assert all(value is None for value in by_scope.values())


def test_a_non_winning_scope_keeps_its_own_word():
    """The case the old read could not express: ORG named it, PROJECT overrode it."""
    by_scope = dl.labels_by_scope(
        [
            _label_row(DIM, "Terminal", dc.SCOPE_ORG),
            _label_row(DIM, "Appareil", dc.SCOPE_PROJECT),
        ]
    )
    assert by_scope[dc.SCOPE_ORG]["display_label"] == "Terminal"
    assert by_scope[dc.SCOPE_PROJECT]["display_label"] == "Appareil"
    assert by_scope[dc.SCOPE_PLATFORM] is None


def test_a_blank_word_is_no_word():
    """The DB CHECK already refuses one; an offline caller cannot inject one either."""
    assert dl.labels_by_scope([_label_row(DIM, "   ", dc.SCOPE_ORG)])[dc.SCOPE_ORG] is None
    assert dl.labels_by_scope([_label_row(DIM, None, dc.SCOPE_ORG)])[dc.SCOPE_ORG] is None


def test_the_word_in_force_is_derived_from_the_per_scope_answer():
    """One reduction, one answer. Two would be free to disagree on one screen."""
    by_scope = dl.labels_by_scope(
        [
            _label_row(DIM, "Device category", dc.SCOPE_PLATFORM),
            _label_row(DIM, "Terminal", dc.SCOPE_ORG),
            _label_row(DIM, "Appareil", dc.SCOPE_PROJECT),
        ]
    )
    resolved = dl.resolve_label_from_scopes(DIM, by_scope)
    assert resolved["display_label"] == "Appareil"
    assert resolved["scope_level"] == dc.SCOPE_PROJECT
    assert resolved["label_source"] == dc.LABEL_SOURCE_CLIENT
    # Drop the most specific one and the next takes over, in order.
    by_scope[dc.SCOPE_PROJECT] = None
    assert dl.resolve_label_from_scopes(DIM, by_scope)["display_label"] == "Terminal"
    by_scope[dc.SCOPE_ORG] = None
    assert dl.resolve_label_from_scopes(DIM, by_scope)["display_label"] == "Device category"


def test_nothing_stored_anywhere_falls_back_and_says_so():
    resolved = dl.resolve_label_from_scopes(DIM, dl.labels_by_scope([]))
    assert resolved["display_label"] == DIM
    assert resolved["scope_level"] is None
    assert resolved["label_source"] == dc.LABEL_SOURCE_FALLBACK


def test_the_derived_cascade_agrees_with_the_stores_own_reducer():
    """Two spellings of one cascade is how a screen comes to say two things.

    `reduce_labels_by_specificity` is what every READ path uses. This asserts the
    projection above cannot drift from it.
    """
    rows = [
        _label_row(DIM, "Device category", dc.SCOPE_PLATFORM),
        _label_row(DIM, "Terminal", dc.SCOPE_ORG),
    ]
    for extra in ([], [_label_row(DIM, "Appareil", dc.SCOPE_PROJECT)]):
        candidate = rows + extra
        winner = dc.reduce_labels_by_specificity(candidate)[DIM]
        derived = dl.resolve_label_from_scopes(DIM, dl.labels_by_scope(candidate))
        assert derived["display_label"] == winner["display_label"]
        assert derived["scope_level"] == winner["scope_level"]


def test_the_envelope_carries_every_scope_beside_the_winner():
    scope_labels = dl.labels_by_scope(
        [_label_row(DIM, "Terminal", dc.SCOPE_ORG), _label_row(DIM, "Appareil", dc.SCOPE_PROJECT)]
    )
    result = dl.build_fed_by(
        canonical_dimension=DIM,
        label=dl.resolve_label_from_scopes(DIM, scope_labels),
        plan_uses=[],
        gaps=[],
        mapping_rows=[],
        scope_labels=scope_labels,
    )
    assert result["display_label"] == "Appareil"
    assert result["scope_labels"][dc.SCOPE_ORG]["display_label"] == "Terminal"
    assert result["scope_labels"][dc.SCOPE_PROJECT]["display_label"] == "Appareil"
    assert result["scope_labels"][dc.SCOPE_PLATFORM] is None


def test_the_envelope_declares_the_scopes_even_when_the_caller_passed_nothing():
    """An absent key teaches the panel to fall back to a derived proposal, which is
    the defect wearing a missing field."""
    result = dl.build_fed_by(
        canonical_dimension=DIM, label=_label(), plan_uses=[], gaps=[], mapping_rows=[]
    )
    assert set(result["scope_labels"]) == set(dl.LABEL_SCOPES)
    assert all(value is None for value in result["scope_labels"].values())
