from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Story 59.2 -- the seeded fleet, its monitor and its issue live in the shared
# conftest: the Workbench header suite reads the same rows, and two fixtures
# would be two worlds for one fact.
from tests.core.conftest import open_dq_issue, publish_dq_monitor

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "infra" / "nango" / "migrations" / "133_data_surface_identities.sql"


def _cursor(*rows):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.side_effect = list(rows)
    cursor.fetchall.return_value = []
    cursor.rowcount = 1
    return cursor


def test_migration_adds_distinct_data_object_identities_and_immutable_versions():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "source_account_id" in sql
    # The constraint is asserted by NAME, not by its DDL spelling. `f9fa928` had
    # to promote it in place -- `UNIQUE (source_account_id)` raises 42P07 against
    # the index this migration already created -- and this assertion went red for
    # a repair that was correct. A later migration may reshape the DDL again; the
    # name is what the foreign keys downstream depend on.
    assert "uq_credential_accounts_source_account_id" in sql
    assert "UNIQUE" in sql
    assert "app.event_configurations" in sql
    assert "app.event_configuration_versions" in sql
    assert "app.connector_contract_versions" in sql
    assert "connector_fingerprint" in sql
    assert "context_events" in sql and "event_configuration_version_id" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "immutable" in sql.lower()


def test_source_account_id_is_opaque_and_reused_during_rediscovery(monkeypatch):
    from core import account_topology

    minted = iter(["sacct_01ARZ3NDEKTSV4RRFFQ69G5FAV"])
    monkeypatch.setattr(account_topology, "_mint_source_account_id", lambda: next(minted))
    cursor = _cursor()
    conn = MagicMock()
    conn.cursor.return_value = cursor

    account_topology.reconcile_discovered_accounts(
        "cred_1", [{"id": "provider-account-raw", "label": "Visible label"}], conn
    )

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    params = [call.args[1] for call in cursor.execute.call_args_list if len(call.args) > 1]
    insert_index = next(
        i
        for i, statement in enumerate(statements)
        if "INSERT INTO app.credential_accounts" in statement
    )
    assert "source_account_id" in statements[insert_index]
    assert (
        "source_account_id = app.credential_accounts.source_account_id" in statements[insert_index]
    )
    assert params[insert_index][0] == "sacct_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert "provider-account-raw" not in params[insert_index][0]


def test_connector_contract_fingerprint_is_canonical_and_secret_free():
    from core.data_identities import connector_contract_snapshot

    left = connector_contract_snapshot(
        {
            "name": "generic",
            "display_name": "Generic",
            "auth_type": "none",
            "report_profiles": [{"id": "daily", "grain": ["date"]}],
            "source_capabilities": {"reports": []},
            "ignored_runtime_secret": "must-not-land",
        },
        validation_evidence={"status": "validated", "issues": []},
    )
    right = connector_contract_snapshot(
        {
            "source_capabilities": {"reports": []},
            "report_profiles": [{"grain": ["date"], "id": "daily"}],
            "auth_type": "none",
            "display_name": "Generic",
            "name": "generic",
        },
        validation_evidence={"issues": [], "status": "validated"},
    )

    assert left["connector_fingerprint"] == right["connector_fingerprint"]
    assert len(left["connector_fingerprint"]) == 64
    serialized = json.dumps(left, sort_keys=True)
    assert "ignored_runtime_secret" not in serialized
    assert "must-not-land" not in serialized


def test_event_configuration_version_binds_datastream_and_contract_evidence():
    from core.data_identities import build_event_configuration_version

    version = build_event_configuration_version(
        event_configuration_id="ecfg_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        datastream_id="ds_1",
        version_number=1,
        connector_contract_version_id="ccv_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        connector_fingerprint="a" * 64,
        source_mapping={"source_ref": "report:event"},
        collection_policy={"cadence": "daily", "timezone": "UTC"},
        actor="operator@example.com",
    )

    assert version["datastream_id"] == "ds_1"
    assert version["event_configuration_id"].startswith("ecfg_")
    assert len(version["normalized_payload_hash"]) == 64
    assert version["source_mapping"] == {"source_ref": "report:event"}
    assert version["collection_policy"]["timezone"] == "UTC"


def test_event_configuration_version_rejects_unpinned_contract():
    from core.data_identities import build_event_configuration_version

    with pytest.raises(ValueError, match="connector contract"):
        build_event_configuration_version(
            event_configuration_id="ecfg_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            datastream_id="ds_1",
            version_number=1,
            connector_contract_version_id="",
            connector_fingerprint="",
            source_mapping={},
            collection_policy={},
            actor="operator@example.com",
        )


def test_data_collection_envelope_is_versioned_and_evidence_explicit():
    from core.data_surface import build_collection_envelope

    envelope = build_collection_envelope(
        project_id="proj-1",
        lens="sources",
        items=[],
        evidence_as_of=None,
        unavailable_reasons=[{"code": "source_inventory_empty", "message": "No source evidence"}],
        allowed_actions=["source-account.connect"],
        generated_at="2026-07-29T10:00:00Z",
    )

    assert set(envelope) == {
        "schema_version",
        "project_ref",
        "generated_at",
        "evidence_as_of",
        "items",
        "unavailable_reasons",
        "allowed_actions",
    }
    assert envelope["schema_version"] == "data-sources.v1"
    assert envelope["project_ref"] == {
        "object_type": "project",
        "id": "proj-1",
        "href": "/api/projects/proj-1/data-overview",
    }
    assert envelope["evidence_as_of"] is None
    assert envelope["unavailable_reasons"][0]["code"] == "source_inventory_empty"


def test_source_account_projection_never_exposes_provider_or_credential_ids():
    from core.data_surface import project_source_account

    item = project_source_account(
        {
            "source_account_id": "sacct_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "label": "North America account",
            "connector_id": "generic-analytics",
            "available": True,
            "discovered_at": "2026-07-28T10:00:00Z",
            "last_seen_at": "2026-07-29T10:00:00Z",
            "used_by_count": 2,
            "external_account_id": "raw-provider-account",
            "credential_id": "conn-secret-owner-link",
            "nango_connection_id": "nango-raw-id",
        },
        project_id="proj-1",
    )

    serialized = json.dumps(item, sort_keys=True)
    assert item["object_ref"]["object_type"] == "source-account"
    assert item["connector_ref"]["id"] == "generic-analytics"
    assert item["object_ref"]["href"] == (
        "/api/projects/proj-1/source-accounts/"
        "sacct_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    )
    assert "raw-provider-account" not in serialized
    assert "conn-secret-owner-link" not in serialized
    assert "nango-raw-id" not in serialized
    assert item["states"] == {
        "availability": "available",
        # `ok|stale|revoked`, the real state machine of `app.connection_health`
        # (migrations 005 and 007). Sources used to report only a boolean, so a
        # REVOKED credential and a merely stale one looked identical -- on the
        # one page whose function names "authorization health".
        "authorization": "unavailable",
        "freshness": "observed",
        "usage": "used",
    }


def test_source_account_carries_ownership_and_the_real_authorization_health():
    """The two halves of this page's function that were computed and undrawn.

    `authorization_scope` is derived inside the query (`data_surface.py`) and was
    already projected; nothing rendered it. The health state was one join away
    and the join was never taken. Four review lenses found both on 2026-08-03."""
    from core.data_surface import project_source_account

    item = project_source_account(
        {
            "source_account_id": "sacct_EXAMPLE",
            "connector_id": "generic-analytics",
            "available": True,
            "authorization_scope": "delegated",
            "connection_health_state": "revoked",
            "used_by_count": 0,
        },
        project_id="proj-1",
    )

    # Ownership: an account this Project may USE but cannot REPAIR.
    assert item["authorization_ref"]["owner_scope"] == "delegated"
    # Health: the exact state, not "unavailable" standing in for three outcomes.
    assert item["states"]["authorization"] == "revoked"
    # And the boolean stays what it always was -- the two are different facts.
    assert item["states"]["availability"] == "available"


def test_source_account_names_the_authorization_it_belongs_to():
    """Story 57.11 -- what makes question 2 of step 1 askable, and nothing held it.

    The ratified document said the question "which authorization" could not be
    asked "until the source-options payload carries an authorization reference
    per account". Measured 2026-08-10, the payload already did: the query selects
    `cr.id AS connection_ref_id` and this projection puts it on every account.

    What was missing was the pin. Not one test read `connection_ref`, so a
    projection rewrite could have dropped it in silence and the only symptom
    would have been a wizard step that quietly stopped being answerable -- the
    class of regression nothing on this screen would name.

    `authorization_ref` beside it is what the authorization is CALLED (scope,
    kind, owning organization) and carries no id; the two are not redundant, and
    a reader must not mistake one for the other.

    The QUERY half of the same fact -- `cr.id` selected AND grouped, without which
    the whole lens answers a 42803 -- is two tests further down; this one is the
    projection, and neither implies the other.
    """
    from core.data_surface import project_source_account

    item = project_source_account(
        {
            "source_account_id": "sacct_EXAMPLE",
            "connector_id": "google",
            "connection_ref_id": "conn_EXAMPLE",
            "available": True,
            "used_by_count": 0,
        },
        project_id="proj-1",
    )

    # THE ADDRESS: the same id `POST /api/connections/{id}/backfill` and
    # `GET /api/connections/{id}/accounts` take in their path.
    assert item["connection_ref"] == {"object_type": "connection", "id": "conn_EXAMPLE"}
    # THE NAME, and it holds no id -- which is why both travel.
    assert "id" not in item["authorization_ref"]
    # One Google consent opens ten Connectors, so `connector_ref` here is the
    # authorization's PROVIDER and answers a different question than
    # `connection_ref` does. Collapsing the two is what made step 1 ask one
    # question where there are two.
    assert item["connector_ref"]["id"] == "google"


def test_data_surface_queries_use_owned_evidence_not_legacy_presentations():
    from core.data_surface import DATA_SURFACE_QUERIES

    query_text = "\n".join(DATA_SURFACE_QUERIES.values()).lower()
    assert "app.managed_feed_import_ledger" in query_text
    assert "app.inbound_receipts" in query_text
    assert "app.datastream_publication_log" in query_text
    assert "app.datastream_schedule_state" in query_text
    assert "app.connector_contract_versions" in query_text
    assert "app.event_configurations" in query_text
    assert "app.project_flux" in query_text
    assert "project_modules" not in query_text
    assert "media_plans" not in query_text


def test_import_projection_keeps_receipt_candidate_and_current_publication_distinct():
    from core.data_surface import project_import

    item = project_import(
        {
            "id": "mfl_1",
            "datastream_id": "ds_1",
            "datastream_name": "Daily feed",
            "execution_id": "dse_candidate",
            "current_published_execution_id": "dse_current",
            "candidate_state": "failed",
            "receipt_id": "inbrx_1",
            "receipt_channel": "email",
            "receipt_state": "LANDED",
            "attachment_count": 1,
            "total_bytes": 42,
            "outcome": "failed",
            "rejected_row_count": 3,
        },
        project_id="proj-1",
    )

    assert item["candidate_ref"] == {"object_type": "datastream-execution", "id": "dse_candidate"}
    assert item["publication_ref"] is None
    assert item["receipt_ref"] == {"object_type": "inbound-receipt", "id": "inbrx_1"}
    assert item["states"]["candidate"] == "failed"
    assert item["states"]["publication"] == "unavailable"
    assert item["states"]["receipt"] == "LANDED"


def test_datastream_projection_preserves_candidate_and_last_known_good_evidence():
    from core.data_surface import project_datastream

    item = project_datastream(
        {
            "id": "ds_1",
            "enabled": True,
            "executable": True,
            "current_published_execution_id": "dse_current",
            "last_known_good_execution_id": "dse_current",
            "latest_execution_id": "dse_failed",
            "latest_execution_state": "failed",
            "latest_error_code": "schema_drift",
            "next_run_at": "2026-07-30T02:00:00Z",
        },
        project_id="proj-1",
    )

    assert item["published_execution_ref"]["object_type"] == "publication"
    assert item["last_known_good_publication_ref"]["id"] == "dse_current"
    assert item["candidate_ref"]["id"] == "dse_failed"
    assert item["evidence"]["latest_exception"] == "schema_drift"
    assert item["evidence"]["next_run_at"] == "2026-07-30T02:00:00Z"


# ---------------------------------------------------------------------------
# Story 58.8 -- what the fleet list needs, and what it must NOT be given
# ---------------------------------------------------------------------------
#
# The five facts below were in the database and stopped at the SQL, so no front
# end could have drawn them however it was written. What these tests hold is not
# that they are emitted -- that is one line each -- but the SHAPE of their
# absence: `None`, never `0` and never `""`. A `0` metric count on a Datastream
# with no plan is indistinguishable from a plan that selected nothing, and the
# screen owes two different sentences for those two states.


def test_datastream_projection_carries_the_collection_identity_of_a_current_plan():
    from core.data_surface import project_datastream

    item = project_datastream(
        {
            "id": "ds_1",
            "enabled": True,
            "data_role": "Performance",
            "source_account_id": "sacct_EXAMPLE",
            "source_account_label": "Example brand account",
            "source_account_external_id": "act-000",
            "active_plan_version": 4,
            "plan_grain": ["Date", "Campaign"],
            "plan_metric_count": 2,
            "plan_dimension_count": 3,
            "latest_execution_state": "published",
            "latest_execution_at": "2026-08-06T23:10:00Z",
        },
        project_id="proj-1",
    )

    evidence = item["evidence"]
    assert evidence["data_role"] == "Performance"
    assert evidence["source_account_id"] == "sacct_EXAMPLE"
    assert evidence["source_account_label"] == "Example brand account"
    assert evidence["plan_grain"] == ["Date", "Campaign"]
    assert evidence["plan_metric_count"] == 2
    assert evidence["plan_dimension_count"] == 3
    # The instant of the LATEST run, which is not the last publication: a failed
    # run has no publication and the `Published` column already carries that one.
    assert evidence["latest_run_at"] == "2026-08-06T23:10:00Z"


def test_a_stream_without_a_plan_or_an_account_emits_none_rather_than_zero():
    from core.data_surface import project_datastream

    evidence = project_datastream({"id": "ds_2", "enabled": True}, project_id="proj-1")["evidence"]

    for key in (
        "data_role",
        "source_account_id",
        "source_account_label",
        "latest_run_at",
        "plan_grain",
        "plan_metric_count",
        "plan_dimension_count",
    ):
        assert evidence[key] is None, f"{key} must be None when the fact does not exist"
        # Said twice on purpose: `0` and `""` are both falsey and both would let
        # a screen print a measurement nobody took.
        assert evidence[key] != 0
        assert evidence[key] != ""


def test_an_account_without_a_label_falls_back_to_its_external_id_like_the_sources_lens():
    from core.data_surface import project_datastream

    named = project_datastream(
        {
            "id": "ds_3",
            "enabled": True,
            "source_account_id": "sacct_EXAMPLE",
            "source_account_label": None,
            "source_account_external_id": "act-000",
        },
        project_id="proj-1",
    )
    # A constant fallback is not a name -- the `sources` lens learned that when
    # five Search Console properties all rendered as one repeated string.
    assert named["evidence"]["source_account_label"] == "act-000"

    unnamed = project_datastream(
        {
            "id": "ds_4",
            "enabled": True,
            "source_account_id": "sacct_EXAMPLE",
            "source_account_label": None,
            "source_account_external_id": None,
        },
        project_id="proj-1",
    )
    # The stream NAMES an account and the authorization exposes nothing for it.
    # That is a different repair from "this stream names no account", and the
    # two must stay distinguishable on the wire.
    assert unnamed["evidence"]["source_account_id"] == "sacct_EXAMPLE"
    assert unnamed["evidence"]["source_account_label"] is None


def test_the_datastreams_query_reads_the_five_facts_the_fleet_list_draws():
    import re

    from core.data_surface import DATA_SURFACE_QUERIES

    query = re.sub(r"--[^\n]*", "", DATA_SURFACE_QUERIES["datastreams"])

    assert "d.data_role" in query
    assert "d.source_account_id" in query
    assert "app.credential_accounts" in query
    # `state_changed_at`, and NOT `started_at`: the latter does not exist on
    # `app.datastream_executions` in the deployed schema and the query would
    # raise UndefinedColumn -- a whole lens returning 503 instead of a list.
    assert "latest_execution.state_changed_at" in query
    # The grain lives under `source.selection`, measured on every plan version:
    # there is no `selection` key at the root of the normalized payload.
    assert "'{source,selection,grain}'" in query
    assert "'{source,selection,metrics}'" in query
    assert "'{source,selection,dimensions}'" in query
    # No COALESCE to zero on the counts: a plan that selected nothing and a
    # Datastream with no plan would then send the same number.
    assert "COALESCE(pv.normalized_payload" not in query


def test_connector_projection_redacts_legacy_free_form_blocking_cause():
    from core.data_surface import project_connector

    item = project_connector(
        {
            "connector_id": "generic",
            "installation_state": "DEGRADED",
            "blocking_cause": "provider token secret leaked in legacy prose",
        },
        project_id="proj-1",
    )

    assert item["evidence"]["blocking_cause"] == "dependency_unavailable"


def test_data_surface_rejects_unknown_lens_without_guessing():
    from core.data_surface import compose_data_surface

    with pytest.raises(ValueError, match="unsupported Data lens"):
        compose_data_surface("proj-1", "mystery", MagicMock())


def test_import_collection_filters_and_pages_before_building_the_envelope(monkeypatch):
    from core import data_surface

    items = [
        {
            "object_ref": {"id": "imp-1"},
            "label": "Alpha daily",
            "states": {"outcome": "ready"},
            "evidence_as_of": "2026-08-13T08:00:00Z",
        },
        {
            "object_ref": {"id": "imp-2"},
            "label": "Alpha archive",
            "states": {"outcome": "ready"},
            "evidence_as_of": "2026-08-13T09:00:00Z",
        },
        {
            "object_ref": {"id": "imp-3"},
            "label": "Beta failed",
            "states": {"outcome": "failed"},
            "evidence_as_of": "2026-08-13T10:00:00Z",
        },
    ]
    monkeypatch.setattr(data_surface, "_project_org", lambda *_: "org-1")
    monkeypatch.setattr(data_surface, "_fetch_rows", lambda *_: list(items))
    monkeypatch.setitem(data_surface._PROJECTORS, "imports", lambda row, **_: row)

    envelope = data_surface._compose_collection(
        "proj-1",
        "imports",
        MagicMock(),
        object_id=None,
        can_edit=True,
        filters={
            "q": "alpha",
            "state": "ready",
            "from": "2026-08-13",
            "to": "2026-08-13",
        },
        limit=1,
        cursor=1,
    )

    assert [item["object_ref"]["id"] for item in envelope["items"]] == ["imp-2"]
    assert envelope["total"] == 2
    assert envelope["bound"] == 1
    assert envelope["next_cursor"] is None
    assert envelope["applied_filters"] == {
        "q": "alpha",
        "state": "ready",
        "from": "2026-08-13",
        "to": "2026-08-13",
    }
    assert envelope["filter_options"] == {
        "states": ["failed", "ready"],
        "datastreams": [],
    }


def test_import_date_filter_does_not_treat_missing_evidence_as_old(monkeypatch):
    from core import data_surface

    items = [
        {
            "object_ref": {"id": "dated"},
            "states": {},
            "evidence_as_of": "2026-08-12T10:00:00Z",
        },
        {"object_ref": {"id": "undated"}, "states": {}, "evidence_as_of": None},
    ]
    monkeypatch.setattr(data_surface, "_project_org", lambda *_: "org-1")
    monkeypatch.setattr(data_surface, "_fetch_rows", lambda *_: list(items))
    monkeypatch.setitem(data_surface._PROJECTORS, "imports", lambda row, **_: row)

    envelope = data_surface._compose_collection(
        "proj-1",
        "imports",
        MagicMock(),
        object_id=None,
        can_edit=True,
        filters={"to": "2026-08-13"},
    )

    assert [item["object_ref"]["id"] for item in envelope["items"]] == ["dated"]


def test_import_collection_rejects_a_stale_cursor(monkeypatch):
    from core import data_surface

    item = {"object_ref": {"id": "imp-1"}, "states": {}, "evidence_as_of": None}
    monkeypatch.setattr(data_surface, "_project_org", lambda *_: "org-1")
    monkeypatch.setattr(data_surface, "_fetch_rows", lambda *_: [item])
    monkeypatch.setitem(data_surface._PROJECTORS, "imports", lambda row, **_: row)

    with pytest.raises(ValueError, match="cursor does not reference"):
        data_surface._compose_collection(
            "proj-1",
            "imports",
            MagicMock(),
            object_id=None,
            can_edit=True,
            cursor=25,
        )


#: One page's worth of facts per lens, for the two collections that carried a
#: pager the envelope could not light. Each entry is (lens, rows, expectations)
#: and the rows are shaped exactly as their projector emits them -- `sources`
#: names its scope with `label` and its Connector through `connector_ref`,
#: `events` names itself with `name` and its Datastream through `datastream_ref`.
_SOURCE_ROWS = [
    {
        "object_ref": {"id": "sacct_1"},
        "label": "Alpha property",
        "connector_ref": {"object_type": "connector", "id": "google"},
        "states": {"availability": "available", "authorization": "ok", "usage": "used"},
        "evidence_as_of": "2026-08-16T08:00:00Z",
    },
    {
        "object_ref": {"id": "sacct_2"},
        "label": "Alpha archive",
        "connector_ref": {"object_type": "connector", "id": "google"},
        "states": {"availability": "available", "authorization": "stale", "usage": "unused"},
        "evidence_as_of": "2026-08-16T09:00:00Z",
    },
    {
        "object_ref": {"id": "sacct_3"},
        "label": "Beta account",
        "connector_ref": {"object_type": "connector", "id": "meta-ads"},
        "states": {"availability": "unavailable", "authorization": "revoked", "usage": "unused"},
        "evidence_as_of": "2026-08-16T10:00:00Z",
    },
]

_EVENT_ROWS = [
    {
        "object_ref": {"id": "ecfg_1"},
        "name": "Alpha launch",
        "datastream_ref": {"object_type": "datastream", "id": "ds_1"},
        "states": {"lifecycle": "active", "review": "confirmed", "collection": "observed"},
        "evidence_as_of": "2026-08-16T08:00:00Z",
    },
    {
        "object_ref": {"id": "ecfg_2"},
        "name": "Alpha promo",
        "datastream_ref": {"object_type": "datastream", "id": "ds_2"},
        "states": {"lifecycle": "active", "review": "draft", "collection": "unavailable"},
        "evidence_as_of": "2026-08-16T09:00:00Z",
    },
    {
        "object_ref": {"id": "ecfg_3"},
        "name": "Beta rollout",
        "datastream_ref": {"object_type": "datastream", "id": "ds_2"},
        "states": {"lifecycle": "draft", "review": "draft", "collection": "unavailable"},
        "evidence_as_of": "2026-08-16T10:00:00Z",
    },
]


@pytest.mark.parametrize(
    ("lens", "rows", "expected_states", "expected_datastreams"),
    [
        (
            "sources",
            _SOURCE_ROWS,
            ["available", "ok", "revoked", "stale", "unavailable", "unused", "used"],
            [],
        ),
        (
            "events",
            _EVENT_ROWS,
            ["active", "confirmed", "draft", "observed", "unavailable"],
            ["ds_1", "ds_2"],
        ),
    ],
)
def test_sources_and_events_carry_the_page_facts_their_pager_needs(
    monkeypatch, lens, rows, expected_states, expected_datastreams
):
    """The two collections whose footer was drawn against an envelope without one.

    `Sources.tsx:163` and `BusinessContextPanel.tsx:92` both send a cursor and
    both render `DataCollectionLayout`'s pager, which appears only when the
    envelope carries `total` or `next_cursor`. `_compose_collection` composed
    them for `imports` and `connectors` alone, so on those two screens the
    controls were mounted against facts that never arrived.

    Every number below is COUNTED from the rows the lens read -- the whole
    authorized snapshot is fetched before the page is cut, so `total` is the
    size of the filtered collection and not an estimate of it.
    """
    from core import data_surface

    monkeypatch.setattr(data_surface, "_project_org", lambda *_: "org-1")
    monkeypatch.setattr(data_surface, "_fetch_rows", lambda *_: [dict(row) for row in rows])
    monkeypatch.setitem(data_surface._PROJECTORS, lens, lambda row, **_: row)

    envelope = data_surface._compose_collection(
        "proj-1", lens, MagicMock(), object_id=None, can_edit=True, limit=2, cursor=0
    )

    assert len(envelope["items"]) == 2
    assert envelope["total"] == 3
    assert envelope["bound"] == 2
    # The third row exists and is not on this page: the cursor that reaches it.
    assert envelope["next_cursor"] == "2"
    assert envelope["applied_filters"] == {}
    # Declared from what was actually read, never from a list written here.
    assert envelope["filter_options"] == {
        "states": expected_states,
        "datastreams": expected_datastreams,
    }

    last = data_surface._compose_collection(
        "proj-1", lens, MagicMock(), object_id=None, can_edit=True, limit=2, cursor=2
    )
    assert [item["object_ref"]["id"] for item in last["items"]] == [rows[2]["object_ref"]["id"]]
    assert last["total"] == 3
    # `null`, not a cursor that would answer with an empty page forever.
    assert last["next_cursor"] is None


@pytest.mark.parametrize(
    ("lens", "rows", "query", "expected"),
    [
        # A Source Account is searchable by its scope name AND by the Connector
        # its column shows: `connector_ref` is how every lens but `connectors`
        # names one, and the haystack read `connector_id` only.
        ("sources", _SOURCE_ROWS, "alpha", ["sacct_1", "sacct_2"]),
        ("sources", _SOURCE_ROWS, "meta-ads", ["sacct_3"]),
        ("events", _EVENT_ROWS, "alpha", ["ecfg_1", "ecfg_2"]),
    ],
)
def test_sources_and_events_narrow_on_what_their_rows_actually_name(
    monkeypatch, lens, rows, query, expected
):
    from core import data_surface

    monkeypatch.setattr(data_surface, "_project_org", lambda *_: "org-1")
    monkeypatch.setattr(data_surface, "_fetch_rows", lambda *_: [dict(row) for row in rows])
    monkeypatch.setitem(data_surface._PROJECTORS, lens, lambda row, **_: row)

    envelope = data_surface._compose_collection(
        "proj-1", lens, MagicMock(), object_id=None, can_edit=True, filters={"q": query}
    )

    assert [item["object_ref"]["id"] for item in envelope["items"]] == expected
    assert envelope["total"] == len(expected)
    assert envelope["applied_filters"] == {"q": query}


def test_one_source_account_read_alone_carries_no_page_facts(monkeypatch):
    """A detail read is not a page of one -- it is a single object, and a footer
    counting it would invite a reader to page through a collection of one."""
    from core import data_surface

    monkeypatch.setattr(data_surface, "_project_org", lambda *_: "org-1")
    monkeypatch.setattr(data_surface, "_fetch_rows", lambda *_: [dict(row) for row in _SOURCE_ROWS])
    monkeypatch.setitem(data_surface._PROJECTORS, "sources", lambda row, **_: row)

    envelope = data_surface._compose_collection(
        "proj-1", "sources", MagicMock(), object_id="sacct_2", can_edit=False
    )

    assert [item["object_ref"]["id"] for item in envelope["items"]] == ["sacct_2"]
    for key in ("total", "bound", "next_cursor", "applied_filters", "filter_options"):
        assert key not in envelope


def test_event_configuration_creation_is_datastream_owned_and_operation_backed(monkeypatch):
    from core import event_configurations
    from core.operations import OperationResult

    cursor = _cursor(("ds_1", "proj-1", "org-1"))
    conn = MagicMock()
    conn.cursor.return_value = cursor
    captured = {}

    def execute(conn_arg, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(conn_arg, "op_1")
        captured["changed"] = changed
        return OperationResult("op_1", "succeeded", changed.result, "audit_1", "outbox_1", False)

    monkeypatch.setattr(event_configurations, "execute_operation", execute)
    monkeypatch.setattr(event_configurations, "_mint_id", lambda prefix: f"{prefix}_fixed")

    result = event_configurations.create_event_configuration(
        conn,
        project_id="proj-1",
        datastream_id="ds_1",
        org_id="org-1",
        name="Launch events",
        actor="person-1",
        idempotency_key="idem-1",
    )

    sql = "\n".join(call.args[0] for call in cursor.execute.call_args_list)
    assert "FROM app.datastreams" in sql
    assert "project_id = %s" in sql
    assert "INSERT INTO app.event_configurations" in sql
    assert captured["spec"].resource_path == (
        "organization:org-1",
        "project:proj-1",
        "flux:ds_1",
        "event-configuration:ecfg_fixed",
    )
    assert result["operation_id"] == "op_1"


def test_event_activation_rejects_stale_connector_contract(monkeypatch):
    from core import event_configurations

    cursor = _cursor(
        ("ds_1",),
        (
            "ecfg_1",
            "ds_1",
            "proj-1",
            "org-1",
            "ecv_1",
            "confirmed",
            "ccv_old",
            "a" * 64,
            "generic",
        ),
        ("ccv_new", "b" * 64),
    )
    conn = MagicMock()
    conn.cursor.return_value = cursor

    def execute(conn_arg, spec, *, mutation):
        return mutation(conn_arg, "op_1")

    monkeypatch.setattr(event_configurations, "execute_operation", execute)

    with pytest.raises(event_configurations.EventConfigurationStale, match="contract"):
        event_configurations.activate_event_configuration_version(
            conn,
            project_id="proj-1",
            event_configuration_id="ecfg_1",
            version_id="ecv_1",
            org_id="org-1",
            actor="person-1",
            idempotency_key="idem-activate",
        )


def test_console_links_are_rooted_on_the_organization_and_parse_as_object_tabs():
    """Every console address this surface emits must be one the client resolves.

    It was not. `_console_link` built `/project/{p}/data/{section}/{type}/{id}/
    {tab}`, and the client refuses anything whose first segment is not `org`
    (`ui/admin/src/shell/router.tsx:129`), so all 17 call sites across five
    lenses produced a link that landed on the unknown-route screen. The Datastream
    fleet's Mapping badge was the visible symptom; the defect was class-wide.

    This suite could not have caught it: nothing here asserted the SHAPE of a
    link, only that evidence fields survived projection. So the grammar is
    asserted literally, segment by segment, exactly as `router.tsx` parses it:

        /org/{org}/project/{project}/{workspace}/{section}/object/{type}/{id}/tab/{tab}
    """
    from core.data_surface import (
        project_connector,
        project_datastream,
        project_import,
        project_source_account,
    )

    cases = [
        (project_datastream, {"id": "ds_1"}, "datastreams", "datastream", "ds_1"),
        (
            project_source_account,
            {"source_account_id": "sacc_1"},
            "sources",
            "source-account",
            "sacc_1",
        ),
        (project_import, {"id": "imp_1", "datastream_id": "ds_1"}, "imports", "import", "imp_1"),
        (project_connector, {"connector_id": "generic"}, "connectors", "connector", "generic"),
    ]
    for projector, row, section, object_type, object_id in cases:
        item = projector(row, project_id="proj-1", org_id="org-1")
        overview = item["links"]["overview"]
        assert overview.split("/") == [
            "", "org", "org-1", "project", "proj-1", "data", section,
            "object", object_type, object_id, "tab", "overview",
        ], f"{section}: {overview}"


def test_a_project_without_an_organization_emits_no_console_link_at_all():
    """`app.projects.org_id` was added NULLable and backfilled (migration 035).

    A link that cannot resolve is worse than no link: the screen renders it as a
    way forward and the click lands on the unknown-route screen. So the absence
    is total -- the key is dropped, not emitted empty."""
    from core.data_surface import project_datastream

    item = project_datastream({"id": "ds_1"}, project_id="proj-1", org_id=None)

    assert item["links"] == {}


def test_sources_query_can_address_the_connection_it_selects():
    """The Sources lens carries the id every connection action is keyed by.

    This query has joined `app.connection_ref` since it was written and read
    four of its columns without ever selecting its id. So a Source Account
    could not name the connection it IS, and `POST /api/connections/{id}/backfill`
    -- which `execution-substrate.md:88-97` cites while describing "a human
    waiting for the answer in the console" -- was unreachable from the one
    screen that owns authorizations. No front-end change could have surfaced it.
    """
    from core.data_surface import DATA_SURFACE_QUERIES

    query = DATA_SURFACE_QUERIES["sources"]
    assert "cr.id AS connection_ref_id" in query


def test_sources_query_groups_by_every_column_it_projects():
    """A bare `cr.id` in this SELECT raises 42803, and no component test sees it.

    The query aggregates (`COUNT(DISTINCT pf.flux_id)`) and groups by
    `cr.provider`, not by `cr.id`, so adding the id to the projection without
    adding it to the GROUP BY makes the WHOLE Sources lens answer an error
    instead of a list. Checked syntactically because that is exactly the level
    at which Postgres refuses it.
    """
    import re

    from core.data_surface import DATA_SURFACE_QUERIES

    query = re.sub(r"--[^\n]*", "", DATA_SURFACE_QUERIES["sources"])
    projection = query.split("FROM")[0].replace("SELECT", "", 1)
    grouped = query.split("GROUP BY")[1].split("ORDER BY")[0]
    grouped_refs = set(re.findall(r"\b([a-z_]+\.[a-z_]+)\b", grouped))

    ungrouped: list[str] = []
    for column in re.split(r",(?![^()]*\))", projection):
        if not column.strip() or re.search(r"\b(COUNT|SUM|MAX|MIN|AVG)\s*\(", column, re.I):
            continue
        expression = re.split(r"\s+AS\s+", column, flags=re.I)[0].strip()
        refs = set(re.findall(r"\b([a-z_]+\.[a-z_]+)\b", expression))
        if refs and not refs <= grouped_refs:
            ungrouped.append(expression)

    assert ungrouped == []


# ---------------------------------------------------------------------------
# Story 59.2 -- the open-issue count the fleet's `Issues` column draws
# ---------------------------------------------------------------------------
#
# WHY IT IS SEEDED. `app.dq_issues` is 0 on the disposable base and 0 on preprod,
# and `app.dq_monitors` is 0 / 2. So the "monitored, nothing open" case is
# reachable on preprod as it stands and every other case has to be written by the
# real writers -- `ensure_monitor`, `publish_version`, `open_issue`,
# `transition_issue`. A hand-rolled INSERT can satisfy a read the writer would
# never produce, which is how a green test outlives the path it claims to cover.
#
# AND THE LENS ITSELF NEEDS `app.project_flux`: the fleet query INNER JOINs it,
# so a Datastream nobody linked to a Project is invisible here -- which is why
# 1421 Datastreams of the disposable base project to at most one row.
#
# THE SEED, THE MONITOR AND THE ISSUE COME FROM `tests/core/conftest.py`: the
# Workbench header suite reads the same rows, and two fixtures would be two
# worlds for one fact.


def _fleet_items(conn, fleet) -> dict[str, dict]:
    from core.data_surface import compose_data_surface

    envelope = compose_data_surface(fleet["project_id"], "datastreams", conn)
    return {item["object_ref"]["id"]: item for item in envelope["items"]}


def test_the_fleet_counts_an_issue_whose_run_was_never_named(pg_conn, dq_fleet):
    """`execution_id IS NULL` is the MAJORITY case, and it must reach the badge.

    `app.pull_jobs.execution_id` was NULL on 130 rows of 130 (disposable) and 6 of
    6 (preprod), so an issue written by the nightly sweep names no run. A count
    derived from `_run_anomalies` -- which filters `execution_id IS NOT NULL` --
    would read zero for exactly those.
    """
    from ulid import ULID

    execution_id = f"dse_{ULID()}"
    plan_id = f"dpv_{uuid.uuid4().hex[:12]}"
    mapping_id = f"dmv_{uuid.uuid4().hex[:12]}"
    with pg_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
                 (id, datastream_id, project_id, version_number, contract_version,
                  source_kind, writer_kind, destination_policy, normalized_payload,
                  content_hash, idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, 'v1', 'connector_pull', 'toorow', 'managed_raw',
                       '{}'::jsonb, %s, %s, 'system')""",
            (plan_id, dq_fleet["watched"], dq_fleet["project_id"], "a" * 64, "b" * 64),
        )
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
                 (id, datastream_id, project_id, version_number, mapping_contract_version,
                  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                  toorow_extension_version, executable, mapping_payload, ossie_projection,
                  idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, 'v1', %s, %s, %s, '1.0', '1.0', TRUE,
                       '{}'::jsonb, '{}'::jsonb, %s, 'system')""",
            (
                mapping_id,
                dq_fleet["watched"],
                dq_fleet["project_id"],
                "c" * 64,
                plan_id,
                "d" * 64,
                "e" * 64,
            ),
        )
        cur.execute(
            "INSERT INTO app.datastream_executions "
            "(id, datastream_id, project_id, plan_version_id, mapping_version_id, "
            " state, created_by) "
            "VALUES (%s, %s, %s, %s, %s, 'published', 'system')",
            (execution_id, dq_fleet["watched"], dq_fleet["project_id"], plan_id, mapping_id),
        )
    monitor_id = publish_dq_monitor(pg_conn, dq_fleet, dq_fleet["watched"])
    open_dq_issue(pg_conn, dq_fleet, monitor_id, severity="blocking", seed="no-run")
    open_dq_issue(
        pg_conn,
        dq_fleet,
        monitor_id,
        severity="degrading",
        seed="with-run",
        execution_id=execution_id,
    )

    items = _fleet_items(pg_conn, dq_fleet)
    summary = items[dq_fleet["watched"]]["evidence"]["open_issues"]

    assert summary["count"] == 2, "the issue naming no run is counted like the other"
    assert summary["by_severity"] == {"blocking": 1, "degrading": 1, "informational": 0}
    # Worst first, and it is the SERVER that decides which word that is.
    assert summary["highest_severity"] == "blocking"
    assert summary["monitored"] is True


def test_a_flux_nobody_watches_is_not_a_flux_that_was_found_clean(pg_conn, dq_fleet):
    """The two sentences `epic-59:114` refuses to collapse, plus the third one.

    Preprod carries 2 published monitors and 0 issues on a real Datastream since
    2026-08-08, so "no monitor has run" became a measurably false claim on an
    existing row: the middle state is instantiated and cannot be folded into
    either neighbour.
    """
    publish_dq_monitor(pg_conn, dq_fleet, dq_fleet["watched"])

    items = _fleet_items(pg_conn, dq_fleet)
    watched = items[dq_fleet["watched"]]["evidence"]["open_issues"]
    bare = items[dq_fleet["bare"]]["evidence"]["open_issues"]

    # Something looked and found nothing -- and no run is to blame for nothing.
    assert watched == {
        "monitored": True,
        "count": 0,
        "by_severity": {"blocking": 0, "degrading": 0, "informational": 0},
        "highest_severity": None,
        "faulty_execution_id": None,
    }
    # Nothing has looked at all -- a different statement, and a MEASURED one.
    assert bare["monitored"] is False
    assert bare["count"] == 0
    assert bare["highest_severity"] is None


def test_resolved_closed_and_live_suppressions_leave_the_badge(pg_conn, dq_fleet):
    """The predicate's own three exclusions, and the expiry that undoes one."""
    from datetime import date, timedelta

    from core.dq_governance import transition_issue

    monitor_id = publish_dq_monitor(pg_conn, dq_fleet, dq_fleet["watched"])
    resolved = open_dq_issue(pg_conn, dq_fleet, monitor_id, severity="blocking", seed="resolved")
    closed = open_dq_issue(pg_conn, dq_fleet, monitor_id, severity="blocking", seed="closed")
    suppressed = open_dq_issue(
        pg_conn, dq_fleet, monitor_id, severity="degrading", seed="suppressed"
    )
    open_dq_issue(pg_conn, dq_fleet, monitor_id, severity="informational", seed="left-open")

    for issue_id, event in ((resolved, "resolved"), (closed, "closed")):
        transition_issue(
            pg_conn,
            project_id=dq_fleet["project_id"],
            issue_id=issue_id,
            event_kind=event,
            actor="owner@example.com",
            reason="settled by the test",
        )
    transition_issue(
        pg_conn,
        project_id=dq_fleet["project_id"],
        issue_id=suppressed,
        event_kind="suppressed",
        actor="owner@example.com",
        reason="silenced until tomorrow",
        suppressed_until=date.today() + timedelta(days=1),
    )

    summary = _fleet_items(pg_conn, dq_fleet)[dq_fleet["watched"]]["evidence"]["open_issues"]
    # `informational` is still counted: `MATERIAL_SEVERITIES` is the filter of the
    # Attention lens, whose question is "what deserves getting up for", not
    # "what is there".
    assert summary["count"] == 1
    assert summary["highest_severity"] == "informational"

    # An EXPIRED suppression is not a suppression: it comes back on its own.
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE app.dq_issues SET suppressed_until = %s WHERE id = %s",
            (date.today() - timedelta(days=1), suppressed),
        )
    back = _fleet_items(pg_conn, dq_fleet)[dq_fleet["watched"]]["evidence"]["open_issues"]
    assert back["count"] == 2
    assert back["highest_severity"] == "degrading"


def test_marking_an_issue_reviewed_moves_the_badge_it_is_shown_on(pg_conn, dq_fleet):
    """Arbitrage 1: acknowledged is REVIEWED for this badge, and only for it.

    A badge that does not move when someone clicks « Mark as reviewed » -- the
    gesture story 59.1 shipped -- lies about the action. The Governance reading is
    the other one and stays available on the SAME predicate, through the
    parameter: an acknowledged issue is still unresolved there.
    """
    from core.dq_governance import open_issue_counts_by_datastream, transition_issue

    monitor_id = publish_dq_monitor(pg_conn, dq_fleet, dq_fleet["watched"])
    issue_id = open_dq_issue(pg_conn, dq_fleet, monitor_id, severity="blocking", seed="ack")

    transition_issue(
        pg_conn,
        project_id=dq_fleet["project_id"],
        issue_id=issue_id,
        event_kind="acknowledged",
        actor="owner@example.com",
        reason="seen, being worked on",
    )

    summary = _fleet_items(pg_conn, dq_fleet)[dq_fleet["watched"]]["evidence"]["open_issues"]
    assert summary["count"] == 0
    assert summary["highest_severity"] is None
    # Still monitored -- acknowledging an issue does not un-watch the flux.
    assert summary["monitored"] is True

    governance = open_issue_counts_by_datastream(
        pg_conn, project_id=dq_fleet["project_id"], include_acknowledged=True
    )
    assert governance[dq_fleet["watched"]]["count"] == 1


#: Every spelling of "this row is not closed" a writer could reach for. The
#: first version of this guard knew ONE of them -- `NOT IN ('closed'` -- so the
#: two `<> 'closed'` clauses in `control_cases.py` were invisible to it, and a
#: rewrite to `!= 'closed'` would have been invisible too.
_CLOSED_EXCLUSION = r"(?:NOT\s+IN\s*\([^)]*'closed'|(?:<>|!=)\s*'closed')"

#: The acknowledged half of the anomaly predicate, in every spelling.
_ACKNOWLEDGED_EXCLUSION = r"(?:NOT\s+IN\s*\([^)]*'acknowledged'|(?:<>|!=)\s*'acknowledged')"

#: The suppression half. `[<>]=?` and not `<`: a rewrite that flipped the
#: comparison would silently re-open every expired suppression, which is the
#: exact hole the `<` was chosen to close.
_SUPPRESSION_WINDOW = r"suppressed_until\s*[<>]=?\s*\w"

#: `app.alert_firings` has no `status` column at all -- its whole notion of open
#: is one nullable timestamp, and both directions of the test are the notion.
_FIRING_OPENNESS = r"acknowledged_at\s+IS\s+(?:NOT\s+)?NULL"


def _core_sources_without_comments() -> dict[str, str]:
    """Every `server/core` module, RECURSIVELY, with its comments removed.

    Two corrections, both from the 2026-08-21 review of this guard:

    * `glob("*.py")` read the package ROOT only. `core/reshape`,
      `core/schemas` and `core/scratch_profiles` were outside every assertion
      below, so a module moved one directory down escaped the whole guard;
    * a spelling written in a COMMENT is prose about the notion, not a second
      definition of it. Comments are stripped so that explaining the predicate
      next to a consumer does not read as respelling it. Strings are NOT
      stripped: that is where SQL lives.
    """
    import io
    import tokenize

    core = REPO_ROOT / "server" / "core"
    sources: dict[str, str] = {}
    for path in sorted(core.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            stripped = "".join(
                "" if token.type == tokenize.COMMENT else token.string
                for token in tokenize.generate_tokens(io.StringIO(text).readline)
            )
        except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
            stripped = text
        sources[path.relative_to(core).as_posix()] = stripped
    return sources


def test_what_open_means_is_spelled_once_and_readers_consume_it():
    """AI-235: one spelling of "open" PER OBJECT, and the guard on all of them.

    Story 59.2 introduced `open_issue_predicate` and this test (then named
    `test_the_acknowledged_and_suppression_clauses_each_live_in_one_place`)
    COUNTED the four hand-spelled copies so the gap stayed visible. AI-235
    routed `controls_attention._issue_items` and the Governance monitor card
    (`governance_read_model._GOVERNED_DQ_MONITORS`) onto the predicate -- before
    that, a live-suppressed anomaly counted as open on the card and in change-set
    impact while `dq_governance.open_issues` excluded it, two readers disagreeing
    on the side that alarms.

    WHAT THE REVIEW OF 2026-08-21 FOUND, AND WHAT THIS NOW HOLDS. The guard
    proved a branch, not a scope, in three separate ways:

    * it read `server/core/*.py` NON-RECURSIVELY;
    * it knew ONE spelling of "closed" and so never saw `control_cases.py`'s two
      `<> 'closed'` clauses, nor would it have seen a rewrite to `!= 'closed'`;
    * it guarded ONE object. The same word survived on two others --
      `app.control_cases`, whose "open" was three hand-written clauses in two
      files that DISAGREED about `resolved`, and `app.alert_firings`, whose
      "open" was four copies of a null test in two files and which `dq_api`
      renders to a person as "open issues", the very words
      `open_issue_predicate` answers for a different table.

    The rule is now the one the defect always implied: **each object that has a
    notion of open owns exactly ONE sentence for it, and every reader consumes
    that sentence.** Three objects, three owners, and this test names them.
    """
    import re

    sources = _core_sources_without_comments()

    # -- `app.dq_issues`: the anomaly predicate, owned by `dq_governance` -----
    acknowledged = {
        name for name, text in sources.items() if re.search(_ACKNOWLEDGED_EXCLUSION, text, re.I)
    }
    assert acknowledged == {"dq_governance.py"}, acknowledged

    suppression = {
        name for name, text in sources.items() if re.search(_SUPPRESSION_WINDOW, text, re.I)
    }
    assert suppression == {"dq_governance.py"}, suppression

    # -- "this row is not closed": the RECURRENCE notion, one owner per object -
    #
    # Not the same question as "is it still alarming", and that is why it has
    # its own name on each side. Migration 145 wrote it into the schema as two
    # partial unique indexes (`uq_dq_issues_open_root_cause` and
    # `uq_control_cases_open_root_cause`, both `WHERE status <> 'closed'`); a
    # reader that dedupes against anything else collides with the index. The
    # index is applied and is never re-edited -- what changed is that the
    # sentence it enforces now has a NAME on the Python side instead of being
    # three characters away from a predicate meaning something else.
    not_closed = {
        name for name, text in sources.items() if re.search(_CLOSED_EXCLUSION, text, re.I)
    }
    assert not_closed == {
        # `app.dq_issues` -- `unclosed_issue_predicate`, and the anomaly
        # predicate's own "not settled" half.
        "dq_governance.py",
        # `app.control_cases` -- `open_case_predicate`, both readings.
        "control_cases.py",
    }, not_closed

    # -- `app.alert_firings`: open means unacknowledged, owned by `infra_alerts`
    firings = {name for name, text in sources.items() if re.search(_FIRING_OPENNESS, text, re.I)}
    assert firings == {"infra_alerts.py"}, firings

    # -- and the readers really CONSUME, rather than merely not respelling -----
    #
    # A set that shrank because a reader deleted its filter would pass every
    # assertion above. Each consumer is named with the sentence it now asks.
    for module, consumed in (
        ("controls_attention.py", "open_issue_predicate"),
        ("controls_attention.py", "open_case_predicate"),
        ("controls_change_sets.py", "open_issue_predicate"),
        ("governance_read_model.py", "open_issue_predicate"),
        ("dq_api.py", "firing_status_predicate"),
        ("dq_api.py", "FIRING_STATUSES"),
        ("project_readiness.py", "firing_status_predicate"),
    ):
        assert consumed in sources[module], (module, consumed)

    # And the monitor card's SQL -- built at import -- actually carries the
    # shared sentence, suppression half included.
    from core.governance_read_model import _GOVERNED_DQ_MONITORS

    assert "suppressed_until < CURRENT_DATE" in _GOVERNED_DQ_MONITORS


def test_the_guard_on_open_reads_below_the_package_root():
    """`glob` read one directory; a module moved down one escaped every rule.

    Named as its own test because the failure it prevents is invisible: a guard
    whose reading shrinks keeps passing, and the assertions above would all have
    gone green on a `server/core` with nothing in it.
    """
    sources = _core_sources_without_comments()
    assert any("/" in name for name in sources), (
        "the scan is flat again -- server/core has sub-packages and they hold code"
    )
    assert "dq_governance.py" in sources


def test_a_rewritten_spelling_of_closed_does_not_escape_the_guard():
    """The attack the first version of this guard did not survive.

    `NOT IN ('closed'` was the ONE form it looked for. Each line below is a
    rewrite that changes nothing about what the database does and everything
    about what the guard sees, and all of them are seen now.
    """
    import re

    for spelling in (
        "WHERE status NOT IN ('closed', 'resolved')",
        "WHERE status <> 'closed'",
        "WHERE status != 'closed'",
        "WHERE i.status  <>  'closed'",
        "where status not in ('closed')",
    ):
        assert re.search(_CLOSED_EXCLUSION, spelling, re.I), spelling


def test_the_recurrence_predicate_is_not_the_open_predicate():
    """Two sentences about `app.dq_issues`, and neither may quietly become the other.

    `unclosed_issue_predicate` matches migration 145's partial unique index and
    counts a `resolved` issue and a live-suppressed one as still occupying the
    slot. `open_issue_predicate` counts neither as open. Collapsing them would
    either make `open_issue` collide with the index or make the console alarm on
    anomalies somebody already resolved.
    """
    from core.dq_governance import (
        OPEN_ISSUE_PREDICATE,
        UNCLOSED_ISSUE_PREDICATE,
        open_issue_predicate,
        unclosed_issue_predicate,
    )

    assert unclosed_issue_predicate() == UNCLOSED_ISSUE_PREDICATE
    assert unclosed_issue_predicate() == "status <> 'closed'"
    assert unclosed_issue_predicate(alias="i") == "i.status <> 'closed'"
    assert unclosed_issue_predicate() != OPEN_ISSUE_PREDICATE
    # The recurrence reading is the WIDER one -- it is what the unique index
    # enforces, and `open_issue_predicate` narrows it twice over.
    assert "resolved" not in unclosed_issue_predicate()
    assert "resolved" in open_issue_predicate()


def test_a_control_case_has_one_open_and_the_caller_names_its_reading():
    """`app.control_cases` had THREE hand-written clauses that disagreed.

    `open_cases` and the recurrence lookup said `status <> 'closed'`; the
    attention list said `NOT IN ('closed', 'resolved')`. Both readings are
    legitimate and both survive -- what does not survive is either of them being
    typed at a call site, where the next reader cannot tell a deliberate
    difference from a dropped clause.
    """
    import re

    from core.control_cases import OPEN_CASE_PREDICATE, open_case_predicate

    assert open_case_predicate() == OPEN_CASE_PREDICATE
    assert open_case_predicate() == "status <> 'closed'"
    assert open_case_predicate(include_resolved=False) == "status NOT IN ('closed', 'resolved')"
    assert open_case_predicate(alias="c") == "c.status <> 'closed'"
    assert (
        open_case_predicate(include_resolved=False, alias="c")
        == "c.status NOT IN ('closed', 'resolved')"
    )
    # Every column reference carries the prefix, so the fragment is safe under
    # the JOIN `_case_items` puts it in.
    aliased = open_case_predicate(include_resolved=False, alias="c")
    assert not re.search(r"(?<!\.)\bstatus\b", aliased)


def test_an_alert_firing_has_one_open_and_it_lives_with_its_writer():
    """Same word, third object -- and the reading a person is shown.

    `dq_api` prints these rows under the heading "open issues". If that phrase
    resolved to a hand-typed null test on one screen and to a predicate on
    another, the product would answer one question two ways under one word.
    """
    from core.infra_alerts import FIRING_STATUSES, OPEN_FIRING_PREDICATE, firing_status_predicate

    assert FIRING_STATUSES == ("open", "acknowledged")
    assert firing_status_predicate("open") == OPEN_FIRING_PREDICATE
    assert firing_status_predicate("open") == "acknowledged_at IS NULL"
    assert firing_status_predicate("acknowledged") == "acknowledged_at IS NOT NULL"
    assert firing_status_predicate("open", alias="af") == "af.acknowledged_at IS NULL"
    # A third reading is refused at the door rather than invented in SQL.
    with pytest.raises(ValueError):
        firing_status_predicate("resolved")


def test_the_open_predicate_alias_reaches_every_column():
    """`alias="i"` must prefix EVERY column reference, or a JOIN reads wrong.

    One missed prefix under a JOIN whose other table carries a `status` column
    would be an ambiguity error at best and a silent read of the wrong table at
    worst -- the exact failure sharing the predicate exists to prevent.
    """
    import re

    from core.dq_governance import OPEN_ISSUE_PREDICATE, open_issue_predicate

    # The constant is DERIVED from the builder -- one text, never respelled.
    assert open_issue_predicate() == OPEN_ISSUE_PREDICATE

    aliased = open_issue_predicate(alias="i", include_acknowledged=False)
    assert "i.status NOT IN ('closed', 'resolved')" in aliased
    assert "(i.status <> 'suppressed' OR i.suppressed_until < CURRENT_DATE)" in aliased
    assert "i.status <> 'acknowledged'" in aliased
    # No column escaped the prefix: every `status` / `suppressed_until` is
    # qualified, so the fragment stays safe under any JOIN.
    assert not re.search(r"(?<!\.)\b(status|suppressed_until)\b", aliased)

    # The acknowledged clause answers the caller's question, never leaks into
    # the default Governance reading.
    assert "acknowledged" not in open_issue_predicate(alias="i")


class _RecordingCursor:
    """A cursor that says what was asked of the database, and asks it anyway."""

    def __init__(self, cursor, log):
        self._cursor = cursor
        self._log = log

    def execute(self, query, params=None):
        self._log.append(str(query))
        if params is None:
            return self._cursor.execute(query)
        return self._cursor.execute(query, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)


class _RecordingConnection:
    def __init__(self, conn, log):
        self._conn = conn
        self._log = log

    def cursor(self, *args, **kwargs):
        return _RecordingCursor(self._conn.cursor(*args, **kwargs), self._log)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_the_whole_lens_costs_one_aggregate_read_and_not_one_per_row(pg_conn, dq_fleet):
    """COUNTED, not supposed -- the statements are recorded as they are issued.

    The fleet is two rows here and the biggest real one anywhere is two, so a
    per-row subquery would not be visible in a timing. What is asserted is the
    SHAPE: one statement reads the issue store for the whole lens, whatever the
    number of Datastreams.
    """
    monitor_id = publish_dq_monitor(pg_conn, dq_fleet, dq_fleet["watched"])
    open_dq_issue(pg_conn, dq_fleet, monitor_id, severity="degrading", seed="one")

    log: list[str] = []
    _fleet_items(_RecordingConnection(pg_conn, log), dq_fleet)

    touching_issues = [statement for statement in log if "app.dq_issues" in statement]
    assert len(touching_issues) == 1, log
    # And it is an AGGREGATE, not a list the caller counts in Python.
    assert "GROUP BY datastream_id" in touching_issues[0]
    # The monitors travel in the SAME statement: "nobody is watching" and
    # "watching, nothing found" are two states of one read.
    assert "app.dq_monitors" in touching_issues[0]
    assert sum("app.dq_monitors" in statement for statement in log) == 1


def test_the_fleet_row_carries_no_issue_count_when_it_could_not_be_read():
    """The FOURTH state: an absent key, and never a zero.

    `WorkbenchOverviewPage` already makes this distinction for `dq_monitors`; a
    `0` here would be the console asserting a clean bill of health nobody
    measured, which is the whole reason this column carried a sentence until
    today.
    """
    from core.data_surface import project_datastream

    unread = project_datastream({"id": "ds_1", "enabled": True}, project_id="proj_EXAMPLE")
    assert "open_issues" not in unread["evidence"]

    measured = project_datastream(
        {
            "id": "ds_1",
            "enabled": True,
            "open_issues": {
                "monitored": True,
                "count": 0,
                "by_severity": {"blocking": 0, "degrading": 0, "informational": 0},
                "highest_severity": None,
            },
        },
        project_id="proj_EXAMPLE",
    )
    assert measured["evidence"]["open_issues"]["count"] == 0
    assert measured["evidence"]["open_issues"]["monitored"] is True


def _break_the_aggregate(monkeypatch):
    """Make the real aggregate fail the way a database really fails it.

    The predicate is replaced by a reference to a column that does not exist, so
    `open_issue_counts_by_datastream` issues its own statement, unchanged, against
    the real connection and PostgreSQL answers `UndefinedColumn` INSIDE the open
    transaction. That is the shape of every failure this path can actually meet --
    a column dropped by a migration, a table not yet created, a permission
    refused -- and it is the shape that poisons the transaction.

    The first version of this test raised `RuntimeError` from a `MagicMock`
    cursor: a failure mode production cannot produce, and precisely the one that
    dodges the transaction-poisoning it was written to cover.
    """
    from core import dq_governance

    monkeypatch.setattr(
        dq_governance,
        "open_issue_predicate",
        lambda **_kwargs: "no_such_column_59_2 IS NULL",
    )


def test_an_unreadable_issue_store_leaves_the_key_off_rather_than_writing_zero(
    pg_conn, dq_fleet, monkeypatch
):
    """Fail-soft, and the absence is the message.

    Twelve other columns of this row were read perfectly well; taking the fleet
    down for the thirteenth would hide them. What must NOT happen is the quiet
    substitution of a zero for a reading that never returned.
    """
    _break_the_aggregate(monkeypatch)

    items = _fleet_items(pg_conn, dq_fleet)

    assert set(items) == {dq_fleet["watched"], dq_fleet["bare"]}
    for item in items.values():
        # The key is ABSENT. Not `0`, not `null`, not an empty object: the count
        # could not be read, and that is a fourth state.
        assert "open_issues" not in item["evidence"]
        # And the twelve columns that WERE read are still there.
        assert item["evidence"]["cadence"] is None or item["evidence"]["cadence"]
        assert "mapping_version_id" in item["evidence"]


def test_a_failed_aggregate_leaves_the_transaction_usable_for_the_other_lenses(
    pg_conn, dq_fleet, monkeypatch
):
    """THE FAILURE MODE THE SWALLOW MUST SURVIVE, and the one it used to create.

    A statement that fails aborts its transaction; catching the Python exception
    does not un-abort it, so every later statement on the same connection raises
    `InFailedSqlTransaction`. With a bare `try/except` the `datastreams` lens
    degraded exactly as intended and `compose_data_surface(project_id,
    "overview")` -- which reads the SIX lenses on one connection -- died on the
    next one. The fail-soft protected the lens that would have survived anyway and
    took down the screen that composes all of them.

    `dq_governance.read_open_issue_counts` marks a SAVEPOINT and rolls back to it,
    which is what makes the rest of this test possible at all.
    """
    from core.data_surface import compose_data_surface

    _break_the_aggregate(monkeypatch)

    # 1. The lens that owns the count degrades, on the same connection.
    fleet = compose_data_surface(dq_fleet["project_id"], "datastreams", pg_conn)
    assert fleet["items"]
    assert all("open_issues" not in item["evidence"] for item in fleet["items"])

    # 2. THE CONNECTION IS STILL USABLE. Without the rollback to the savepoint
    #    this raises `InFailedSqlTransaction` and nothing below runs.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1

    # 3. And the screen that composes the six lenses still composes.
    overview = compose_data_surface(dq_fleet["project_id"], "overview", pg_conn)
    assert {summary["lens"] for summary in overview["items"]} == {
        "datastreams",
        "sources",
        "imports",
        "events",
        "connectors",
    }
    datastreams = next(s for s in overview["items"] if s["lens"] == "datastreams")
    # The Overview counts the fleet it could read; the issue count it could not
    # read is simply not part of what it reports.
    assert datastreams["object_count"] == 2


def test_the_workbench_header_survives_the_same_failure_on_the_same_connection(
    pg_conn, dq_fleet, monkeypatch
):
    """The header reads three things after the aggregate. All three must still run."""
    from core.datastream_workbench import read_workbench

    _break_the_aggregate(monkeypatch)

    header = read_workbench(
        pg_conn, project_id=dq_fleet["project_id"], datastream_id=dq_fleet["watched"]
    )
    assert "open_issues" not in header
    # The capability tabs are read from the same connection and are still there.
    assert header["capability_tabs"]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
