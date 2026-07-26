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
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import dimension_conformance as dc  # noqa: E402
from core import dimension_lineage as dl  # noqa: E402

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


def _apply_migration(conn, path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _ensure_set_updated_at(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS app")
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS trigger AS $$
            BEGIN NEW.updated_at = now(); RETURN NEW; END;
            $$ LANGUAGE plpgsql
            """
        )
    conn.commit()


def _prepare(conn) -> None:
    """Ensure set_updated_at + 049 (shared audit) + 103 applied idempotently."""
    _ensure_set_updated_at(conn)
    _apply_migration(conn, _MIGRATION_049)
    _apply_migration(conn, _MIGRATION_106)


def _seed_org(conn, suffix: str) -> str:
    org_id = f"dl_org_{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s,%s,%s,'system')",
            (org_id, f"DLOrg-{suffix}", f"dl-org-{suffix}"),
        )
    conn.commit()
    return org_id


def _drop_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as clean:
        with clean.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        clean.commit()


@pg_available
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
                        (f"dlb_{uuid.uuid4().hex}", DIM, org_id),
                    )
            conn.rollback()
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO app.dimension_labels "
                        "(id, canonical_dimension, display_label, scope_level, org_id,"
                        " created_by) VALUES (%s,%s,'   ','ORG',%s,'system')",
                        (f"dlb_{uuid.uuid4().hex}", DIM, org_id),
                    )
            conn.rollback()
        finally:
            _drop_org(org_id)


@pg_available
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
                    (f"dlb_{uuid.uuid4().hex}", DIM, org_id),
                )
            conn.commit()
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.UniqueViolation):
                    cur.execute(
                        "INSERT INTO app.dimension_labels "
                        "(id, canonical_dimension, display_label, scope_level, org_id,"
                        " created_by) VALUES (%s,%s,'second','ORG',%s,'system')",
                        (f"dlb_{uuid.uuid4().hex}", DIM, org_id),
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
        org_id = _seed_org(conn, suffix)
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
