"""The file-source chain is reached from the ingresses a person actually uses.

AI-88: every link of the Epic 22 file-source framework was a seam armed nowhere.
`run_import` accepted a `producer` since Story 22.12 and none of its three real
callers ever passed one, so `produce`, the required-field gate, the recognizer
and the drift detector had zero production callers while sixteen stories claimed
the capability.

These tests start from the TWO REAL INGRESSES -- the upload route
(`POST /api/datastreams/{id}/imports`) and the email path
(`inbound_ingest.ingest_inbound_file`) -- and assert they converge. That
distinction is the whole point: the parity test that already existed
(`test_inbound_lands_like_upload.py::test_upload_and_email_land_identically`)
calls ONE function twice with identical arguments, which is `f(x) == f(x)` and
is satisfied by two transports that both do nothing.

The routing rule under test is not invented here; it is read off two persisted
artifacts that already exist:

  * WHICH template  -- `app.datastreams.config -> source_owner ->
    managed_feed_source_ref`, discriminated by the `fst_` prefix that migration
    097 enforces as a CHECK constraint. The wizard writes it:
    datastream_setup_observations.py:337 offers active templates as
    `template_ref`, datastream_activation.py:1377 collapses it into
    `managed_feed.source_ref`, :743 persists it on the Datastream.
  * WHICH mapping   -- `app.datastream_mapping_versions.mapping_payload`, the
    versioned content-hashed artifact whose `binding` carries `confirmed_by`.
    `run_import` already receives its id as `mapping_version_id`. Replaying it
    is what AD-8 means by "never a silent re-map"; re-running the recognizer at
    arrival would be the silent re-map AD-8 forbids.
"""

from __future__ import annotations

import base64
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from core.datastream_activation import compile_materialization_contract

from tests.core.test_datastream_activation import _frozen_review
from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# A well-formed but obviously synthetic template id: 'fst_' + 26 Crockford
# base32 chars, exactly what migration 097's ck_file_source_templates_id CHECK
# constraint admits. Deliberately NOT shaped like any real ULID -- this repo has
# to stay shareable, and a plausible-looking id is how a production identifier
# ends up committed.
TEMPLATE_ID = "fst_0123456789ABCDEFGH0123456J"
PROJECT_ID = "proj_EXAMPLE"
DATASTREAM_ID = "ds_EXAMPLE"
PLAN_VERSION_ID = "dsp_EXAMPLE"
MAPPING_VERSION_ID = "dmap_EXAMPLE"

# The source file: French headers, which is the entire reason a template exists
# -- the canonical target is `day`/`clicks`, the file says something else.
CSV_BYTES = (
    b"Date,Clics\n"
    b"2026-07-01,5\n"
    b"2026-07-02,7\n"
    b"2026-07-03,9\n"
    b"2026-07-04,11\n"
)

#: La classe se declare sous la cle `class`, PAS `placement_class`.
#:
#: Cette fixture disait `placement_class` -- qui est le nom de la COLONNE en base
#: (`file_source_template.py:59`), jamais une cle du contrat. Consequence mesuree
#: le 2026-08-01 : `file_source_producer` lit `contract.get("class")`, ne trouve
#: rien, et refuse l'import en `no_placement_class` (AD-6, story 22.14) AVANT
#: d'atteindre le gate de champs requis -- de sorte que les deux tests de 22.15
#: mouraient en amont et que le gate qu'ils sont censes prouver n'etait plus
#: atteint par personne.
#:
#: La fixture encodait donc un contrat qui ne peut pas exister : le validateur de
#: production le refuse (`file_source_template.py:167`). C'est pourquoi le test
#: `test_the_template_fixture_is_a_contract_production_would_accept` existe plus
#: bas -- une fixture qu'aucun validateur ne relit derive jusqu'a ne plus decrire
#: le produit, en silence, et emmene ses tests avec elle.
TEMPLATE_CONTRACT = {
    "kind": "catalog",
    "format": "csv",
    "header_row": 1,
    "grain": "daily",
    "class": "actual",
    "placement": {"metric": "clicks", "period": "day", "dimension": []},
    "required_fields": ["day", "clicks"],
    "optional_fields": [],
    # LES ALIAS VIVENT SOUS `aliases`, ET NULLE PART AILLEURS.
    #
    # Cette fixture les declarait sous `fields[].names`. `normalise_contract`
    # (file_source_template.py:188-207) construit le contrat depuis
    # `required_fields`, `optional_fields`, `grain`, `class`, `placement` et trois
    # passthroughs -- `aliases`, `field_types`, `reshape`. `fields` n'en fait pas
    # partie : la cle etait JETEE, et `_candidate_names` ne lit que `aliases`.
    #
    # La consequence se mesurait : `Date` marquait 0.5714 contre `day` -- un
    # difflib sur deux chaines proches -- au lieu du 1.0 d'un alias exact, donc
    # `unmatched`. Un alias declare dans une forme que le produit ne porte pas est
    # decoratif, et la fixture faisait echouer la reconnaissance qu'elle croyait
    # decrire.
    "aliases": {"day": ["date"], "clicks": ["clics"]},
}


def _binding(canonical: str) -> dict:
    """A CONFIRMED binding -- the human confirmation is a schema-required field."""
    return {
        "canonical_target": canonical,
        "mdm_target": None,
        "status": "confirmed",
        "blocking_reason": None,
        "confirmed_by": "owner@example.com",
        "confirmed_reason": "onboarding preview confirmed",
    }


MAPPING_PAYLOAD = {
    "fields": [
        {"field_id": "Date", "physical_type": "date", "binding": _binding("day")},
        {"field_id": "Clics", "physical_type": "decimal", "binding": _binding("clicks")},
    ]
}

# The same mapping with `clicks` never confirmed: a REQUIRED canonical field that
# no source column reaches. This must stop the import before it lands.
MAPPING_PAYLOAD_MISSING_REQUIRED = {
    "fields": [
        {"field_id": "Date", "physical_type": "date", "binding": _binding("day")},
    ]
}


# ---------------------------------------------------------------------------
# One fake connection, shared by both ingresses, dispatching on the SQL text.
# ---------------------------------------------------------------------------


# EVERY STATEMENT THE TWO INGRESSES ISSUE, NAMED ONCE (AI-317). The chain this
# replaces ended in an `else` that answered `_default = None` with a
# `description` of `[]`: a statement no branch recognised came back as "no row",
# which is an ANSWER -- and `resolve_file_source_producer` reads exactly that
# answer as "this Datastream carries no binding, run the ordinary CSV path". A
# read added to these paths tomorrow would have been measured as an absence, in
# silence, and this file would have stayed green.
#
# Declaration order is the order an `if/elif` would test in: first match wins.
# The three reads of `app.datastreams` are separated by their PROJECTION, not by
# their relation, because all three name the same table.
_STATEMENTS = StatementInventory(
    "test_file_source_wiring._FakeCursor",
    # admin_api.py#require_datastream_in_project -- `_datastream_in_project`, the
    # membership proof the
    # upload route takes before it hands the bytes on. NEVER MODELLED: it fell
    # into the catch-all `FROM app.datastreams` branch and was answered with the
    # five-tuple `_load_ingestable_datastream` reads. The product only asks
    # `fetchone() is not None`, so a row of the wrong shape passed for a proof.
    datastream_membership="select 1 from app.datastreams",
    # file_source_resolution.py:443 -- which Template this Datastream binds.
    datastream_config="select config from app.datastreams",
    # inbound_ingest.py:117 -- `_load_ingestable_datastream` (email + upload).
    ingestable_datastream="select source_kind, enabled, config",
    # file_source_resolution.py:489 -- the client-saved Template and its seal,
    # LEFT JOINed onto the per-project confirmation of this mapping version.
    template="from app.file_source_templates",
    # inbound_ingest.py:275 -- `_fetch_mapping_bundle`: the pinned plan AND
    # mapping in one scoped read, with the fingerprint evidence.
    mapping_bundle=(
        "from app.datastream_mapping_versions m",
        "join app.datastream_plan_versions p",
    ),
    # file_source_resolution.py:402 and :539 -- the mapping version replayed,
    # ONE column. NEVER MODELLED APART: it shared the branch above and was
    # answered with the seven-tuple bundle row. The product reads `row[0]`, so
    # the six extra columns were invisible -- and a fake that answers a
    # seven-column row to a one-column SELECT has stopped describing the query.
    mapping_payload="select mapping_payload from app.datastream_mapping_versions",
    # inbound_ingest.py:164 -- `_fetch_projection_plan`, the last executable
    # projection recorded for the pinned plan version.
    projection_plan="from app.datastream_executions",
    # import_preview.py -- `_unit_report` reads SEVERAL canonical fields at once
    # (`fetchall`), which is why this one answers rows and not a row. Issued from
    # `build_file_source_preview`, which drives this same fake through
    # `test_file_source_preview.py`.
    canonical_fields="from app.mdm_canonical_fields",
    # inbound_ingest.py:981 -- the empty-publication preference, read by both
    # ingresses so both apply the same policy.
    allow_empty_publication="from app.project_preferences",
)


class _FakeCursor:
    """Answers the statements above, and REFUSES every other one.

    `description` is DERIVED from the statement rather than spelled out: a
    hand-written column tuple is a second copy of the projection, and it is the
    copy that goes stale first, because nothing reads it.
    """

    def __init__(self, responses: dict):
        self._responses = responses
        self._current = None
        self._rows: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._rows = []
        self._current = None
        statement = _STATEMENTS.match(sql)
        # Every statement in the inventory is a SELECT with a flat projection,
        # so psycopg's `description` follows from the text of the query itself.
        self.description = describe(sql)
        match statement:
            case "datastream_membership":
                # `SELECT 1`: the row exists. The product asks nothing else.
                self._current = (1,)
            case "datastream_config":
                self._current = self._responses.get("ds_config")
            case "ingestable_datastream":
                self._current = self._responses.get("datastream")
            case "template":
                self._current = self._responses.get("template")
            case "mapping_bundle":
                self._current = self._responses.get("mapping_bundle")
            case "mapping_payload":
                self._current = self._responses.get("mapping_payload")
            case "projection_plan":
                self._current = self._responses.get("projection")
            case "canonical_fields":
                # `fetchall`, pas `fetchone` : le rapport d'unites lit PLUSIEURS
                # champs canoniques d'un coup.
                self._rows = self._responses.get("canonical_fields") or []
            case "allow_empty_publication":
                self._current = self._responses.get("preference")
            case _:  # pragma: no cover - a name in the inventory, unanswered
                raise _STATEMENTS.unknown(sql)

    def fetchone(self):
        return self._current

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows


class _FakeConn:
    def __init__(self, responses: dict):
        self._responses = responses
        self.committed = False

    def cursor(self):
        return _FakeCursor(self._responses)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def _responses(
    *, mapping_payload=MAPPING_PAYLOAD, source_ref=TEMPLATE_ID, template_contract=None
) -> dict:
    return {
        # The Datastream carries its file-source binding (wizard-written).
        "ds_config": ({"source_owner": {"managed_feed_source_ref": source_ref}},),
        # 097 stores the contract document itself in the `contract` JSONB column,
        # so the SELECT yields the contract bare -- not a row wrapping one.
        "template": (TEMPLATE_CONTRACT if template_contract is None else template_contract,),
        # TWO STATEMENTS READ THIS ARTIFACT, and they do not read the same
        # columns. `_fetch_mapping_bundle` joins the plan version and reads the
        # fingerprint evidence; `resolve_file_source_producer` reads the payload
        # alone. Both rows are built from the SAME `mapping_payload` here, so
        # the two answers cannot drift apart.
        "mapping_bundle": (
            mapping_payload,
            "mapping-content-hash",
            "source-schema-hash",
            "mapping-capability-fingerprint",
            "plan-content-hash",
            "plan-capability-fingerprint",
            "universal-datastream-plan-v1",
        ),
        "mapping_payload": (mapping_payload,),
        # The five-tuple `_load_ingestable_datastream` reads (email path).
        "datastream": (
            "managed_feed",
            True,
            {
                "channels": ["upload", "email"],
                "source_owner": {"managed_feed_source_ref": source_ref},
            },
            PLAN_VERSION_ID,
            MAPPING_VERSION_ID,
        ),
        "projection": ({"executable": True},),
        "preference": (True,),
    }


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an unrecognised statement must NAME itself, not answer no rows.

    The old `else` returned `_default = None`, and `None` is not a refusal here
    -- it is the exact answer `resolve_file_source_producer` reads as "this
    Datastream carries no file-source binding, run the ordinary CSV path"
    (`file_source_resolution.py:447`). A query that moved, or a read added to
    either ingress, would have been answered "absent" and every assertion below
    would have kept passing about a branch the product never took.
    """
    cursor = _FakeCursor(_responses())
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT landing_target FROM app.file_source_template_confirmations "
            "WHERE datastream_id = %s AND project_id = %s"
        )
    message = str(raised.value)
    # The statement that moved, and a neighbour to compare it against.
    assert "app.file_source_template_confirmations" in message
    assert "template" in message

    # The three reads of `app.datastreams` really are told apart, and in the
    # order the product issues them.
    assert (
        _STATEMENTS.find("SELECT 1 FROM app.datastreams WHERE id = %s AND project_id = %s")
        == "datastream_membership"
    )
    assert (
        _STATEMENTS.find("SELECT config FROM app.datastreams WHERE id = %s")
        == "datastream_config"
    )


class _RunSpy:
    """Captures every run_import call, uniformly, from either ingress."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, data, **kwargs):
        self.calls.append({"data": data, **kwargs})
        return {
            "ledger": {"id": "mfl_EXAMPLE"},
            "execution": {"id": "dse_EXAMPLE"},
            "outcome": "written_pending_publication",
            "published": False,
            "blocked": False,
            "no_op": False,
            "row_count": 4,
            "rejected_count": 0,
        }


# ---------------------------------------------------------------------------
# Entry point 1 -- the upload route.
# ---------------------------------------------------------------------------


def _upload(conn, spy):
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    @contextmanager
    def _get_connection():
        yield conn

    body = {
        "project_id": PROJECT_ID,
        "filename": "plan.csv",
        "idempotency_key": "idem-upload-1",
        "file_base64": base64.b64encode(CSV_BYTES).decode(),
    }

    def process_upload(db_conn, **kwargs):
        from core.inbound_ingest import ingest_inbound_file

        result = ingest_inbound_file(
            db_conn,
            datastream_id=DATASTREAM_ID,
            project_id=PROJECT_ID,
            file_bytes=kwargs["file_bytes"],
            filename=kwargs.get("filename"),
            channel="upload",
            message_id="upload-test",
            actor=kwargs["actor"],
        )
        return {
            "status": "landed",
            "receipt_id": "inr_EXAMPLE",
            "attachments": [{"raw_import_id": "iri_EXAMPLE"}],
            "dispatch_result": result,
        }
    with (
        patch(
            "core.admin_api._check_auth",
            new=AsyncMock(return_value=(True, "owner@example.com")),
        ),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", new=_get_connection),
        patch("core.csv_excel_import.run_import", new=spy),
        patch(
            "core.inbound_processing.process_authorized_upload",
            new=process_upload,
        ),
    ):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        return client.post(
            f"/api/datastreams/{DATASTREAM_ID}/imports",
            headers={"Authorization": "Bearer test-secret"},
            json=body,
        )


# ---------------------------------------------------------------------------
# Entry point 2 -- the email path.
# ---------------------------------------------------------------------------


def _email(conn, spy):
    from core.inbound_ingest import ingest_inbound_file

    with patch("core.csv_excel_import.run_import", new=spy):
        return ingest_inbound_file(
            conn,
            datastream_id=DATASTREAM_ID,
            project_id=PROJECT_ID,
            file_bytes=CSV_BYTES,
            filename="plan.csv",
            channel="email",
            message_id="msg-1",
            actor="inbound-worker",
        )


# ---------------------------------------------------------------------------
# The wiring, from each ingress.
# ---------------------------------------------------------------------------


def test_upload_arms_the_file_source_producer():
    """A Datastream bound to a template must not reach run_import bare."""
    spy = _RunSpy()
    resp = _upload(_FakeConn(_responses()), spy)

    assert resp.status_code == 200, resp.text
    assert len(spy.calls) == 1
    assert spy.calls[0].get("producer") is not None, (
        "the upload route reached run_import with no producer: the Datastream's "
        "file-source template was never consulted"
    )


def test_email_arms_the_file_source_producer():
    """Ingress parity is not parity if only one ingress resolves the template."""
    spy = _RunSpy()
    _email(_FakeConn(_responses()), spy)

    assert len(spy.calls) == 1
    assert spy.calls[0].get("producer") is not None, (
        "the email path reached run_import with no producer -- the exact defect "
        "Story 22.24 claimed to have closed"
    )


def test_upload_and_email_replay_the_same_producer_on_the_same_bytes():
    """The parity, taken from the two real ingresses rather than one function twice.

    Same template id, same mapping, and -- the part that actually matters --
    the same canonical rows out of the same bytes. Comparing only the fact that
    a producer is present would pass for two producers built from different
    templates.
    """
    upload_spy, email_spy = _RunSpy(), _RunSpy()
    resp = _upload(_FakeConn(_responses()), upload_spy)
    assert resp.status_code == 200, resp.text
    _email(_FakeConn(_responses()), email_spy)

    up = upload_spy.calls[0]["producer"]
    em = email_spy.calls[0]["producer"]
    assert up is not None and em is not None

    assert up.template_id == em.template_id == TEMPLATE_ID
    assert up.mapping == em.mapping == {"Date": "day", "Clics": "clicks"}

    # The bytes both ingresses forwarded are the same bytes.
    assert upload_spy.calls[0]["data"] == email_spy.calls[0]["data"] == CSV_BYTES

    # And the producers agree on what those bytes MEAN -- canonical rows, not
    # the source's French headers.
    up_rows = up(CSV_BYTES).rows
    em_rows = em(CSV_BYTES).rows
    assert up_rows == em_rows
    assert up_rows[0]["day"].isoformat() == "2026-07-01"
    assert up_rows[0]["clicks"] == 5


# ---------------------------------------------------------------------------
# The gate is consulted BEFORE the landing, not journalled beside it.
# ---------------------------------------------------------------------------


def _run_import_with(mapping_payload, template_contract=None):
    """Drive the real run_import with a resolved producer and a stubbed ledger."""
    from core.csv_excel_import import resolve_file_source_producer, run_import

    conn = _FakeConn(
        _responses(mapping_payload=mapping_payload, template_contract=template_contract)
    )
    producer = resolve_file_source_producer(
        conn,
        project_id=PROJECT_ID,
        datastream_id=DATASTREAM_ID,
        mapping_version_id=MAPPING_VERSION_ID,
    )
    assert producer is not None

    opened: list = []

    def _open_import(**kwargs):
        opened.append(kwargs)
        return {
            "no_op": False,
            "ledger": {"id": "mfl_EXAMPLE", "datastream_id": DATASTREAM_ID},
            "execution": {"id": "dse_EXAMPLE"},
        }

    with (
        patch("core.managed_feed_ledger.open_import", new=_open_import),
        patch("core.managed_feed_ledger.record_rows", return_value={"rejected": 0}),
        patch(
            "core.managed_feed_ledger.allocate_landing_relation",
            return_value="raw_project.managed_feed_ds",
        ),
        patch("core.raw_landing.land_raw_rows", return_value={"rows": 4}),
        # The 12.5 rejection gate is a different gate on a written ledger row; it
        # is not the subject here and needs a real ledger to read.
        patch(
            "core.managed_feed_ledger.evaluate_rejection_gate_for_ledger",
            return_value=None,
        ),
        patch("core.managed_feed_ledger.mark_outcome", return_value=None),
    ):
        result = run_import(
            CSV_BYTES,
            datastream_id=DATASTREAM_ID,
            project_id=PROJECT_ID,
            plan_version_id=PLAN_VERSION_ID,
            mapping_version_id=MAPPING_VERSION_ID,
            projection_plan={"executable": True},
            actor="owner@example.com",
            idempotency_key="idem-gate-1",
            source_metadata={"filename": "plan.csv", "format": "csv"},
            contract={},
            conn=conn,
            producer=producer,
        )
    return result, opened


def test_a_missing_required_field_stops_the_import_before_it_lands():
    """A refusal must PREVENT the landing, not be logged next to one.

    `clicks` is required by the template and no confirmed binding reaches it.
    The proof that the gate is a gate and not a log line is that the 12.8 ledger
    is never opened -- no row, no candidate, nothing to publish later.
    """
    result, opened = _run_import_with(MAPPING_PAYLOAD_MISSING_REQUIRED)

    assert result["blocked"] is True
    assert result["published"] is False
    assert result["ledger"] is None
    assert "clicks" in result["gate"]["missing_required"]
    assert opened == [], "the gate failed and the import opened a ledger row anyway"


def test_a_passing_gate_still_lands():
    """The control: without it the test above passes by blocking everything."""
    result, opened = _run_import_with(MAPPING_PAYLOAD)

    assert result["blocked"] is False
    assert len(opened) == 1


# ---------------------------------------------------------------------------
# The CSV/Excel path must stay exactly as it was.
# ---------------------------------------------------------------------------


def test_a_datastream_with_no_template_binding_keeps_the_plain_csv_path():
    """The resolver is additive: no `fst_` binding means no producer at all."""
    from core.csv_excel_import import resolve_file_source_producer

    conn = _FakeConn(_responses(source_ref="file_upload"))
    assert (
        resolve_file_source_producer(
            conn,
            project_id=PROJECT_ID,
            datastream_id=DATASTREAM_ID,
            mapping_version_id=MAPPING_VERSION_ID,
        )
        is None
    )


@pytest.mark.parametrize("source_ref", ["", None, "dsa_0123456789ABCDEFGH0123456J"])
def test_only_an_fst_reference_routes_to_a_template(source_ref):
    """`managed_feed_source_ref` also carries staged assets and sheet refs."""
    from core.csv_excel_import import resolve_file_source_producer

    conn = _FakeConn(_responses(source_ref=source_ref))
    assert (
        resolve_file_source_producer(
            conn,
            project_id=PROJECT_ID,
            datastream_id=DATASTREAM_ID,
            mapping_version_id=MAPPING_VERSION_ID,
        )
        is None
    )


# ---------------------------------------------------------------------------
# AI-91 -- the staged preview asset must not shadow the Template binding.
#
# Ratified by Jean, 2026-07-31: the Template reference and the staged preview
# asset are SEPARATE KEYS. They answer two different questions -- which contract
# do I replay, and which file was used to preview it -- so collapsing them into
# one `or` chain loses the binding whenever both are present, which is exactly
# what the onboarding wizard produces (it offers `template_ref` on the same
# `file_upload` channel that carries a staged asset).
#
# Measured before the repair: `staged_asset_ref` won, `managed_feed.source_ref`
# carried no `fst_` prefix, and `resolve_file_source_producer` found nothing to
# resolve -- so the whole Story 22.12 wiring stayed inert on the one journey the
# wizard actually produces.
# ---------------------------------------------------------------------------

STAGED_ASSET_ID = "asset_EXAMPLE_PREVIEW"


def _managed_feed_operator_input(**source_extra) -> dict:
    """A managed_feed activation whose draft picked a Template."""
    return {
        "mode": "managed_feed",
        "source": {"channel": "file_upload", **source_extra},
        "configure": {"cadence_intent": "manual"},
    }


def _compile(**source_extra) -> dict:
    return compile_materialization_contract(
        frozen_review=_frozen_review(),
        operator_input=_managed_feed_operator_input(**source_extra),
        observation={"schema_hash": "d" * 64},
        org_id="org_1",
        actor="person_1",
        timezone_name="Europe/Paris",
    )


def test_a_staged_preview_asset_does_not_shadow_the_template_binding():
    """The wizard's own output: a draft that previewed a file AND chose a Template."""
    feed = _compile(
        staged_asset_ref=STAGED_ASSET_ID,
        template_ref=TEMPLATE_ID,
    )["plan_intent"]["source"]["managed_feed"]

    assert feed.get("template_ref") == TEMPLATE_ID, (
        "the Template binding was lost at activation: the staged preview asset "
        f"shadowed it. managed_feed = {feed!r}"
    )


def test_the_staged_asset_survives_too_it_is_not_a_swap():
    """Reordering the chain would have lost the asset instead. Both are kept."""
    feed = _compile(
        staged_asset_ref=STAGED_ASSET_ID,
        template_ref=TEMPLATE_ID,
    )["plan_intent"]["source"]["managed_feed"]

    assert feed.get("staged_asset_ref") == STAGED_ASSET_ID
    assert feed["source_ref"] == STAGED_ASSET_ID, (
        "source_ref still names the input the plan pinned for this channel"
    )


def test_a_template_only_draft_still_binds():
    """No staged asset: the Template is still reachable under its own key."""
    feed = _compile(template_ref=TEMPLATE_ID)["plan_intent"]["source"]["managed_feed"]

    assert feed.get("template_ref") == TEMPLATE_ID


def test_the_resolver_reads_the_dedicated_key_not_a_prefix_in_a_catch_all():
    """`resolve_file_source_producer` must no longer guess a prefix."""
    from core.csv_excel_import import resolve_file_source_producer

    responses = _responses()
    responses["ds_config"] = (
        {
            "source_owner": {
                "managed_feed_source_ref": STAGED_ASSET_ID,  # NOT an fst_ id
                "managed_feed_template_ref": TEMPLATE_ID,
            }
        },
    )
    producer = resolve_file_source_producer(
        _FakeConn(responses),
        project_id=PROJECT_ID,
        datastream_id=DATASTREAM_ID,
        mapping_version_id=MAPPING_VERSION_ID,
    )
    assert producer is not None, (
        "the Template is bound under managed_feed_template_ref and the resolver "
        "did not find it"
    )


# ---------------------------------------------------------------------------
# Le garde de la fixture elle-meme.
# ---------------------------------------------------------------------------


def test_the_template_fixture_is_a_contract_production_would_accept():
    """Une fixture qu'aucun validateur ne relit derive, en silence, avec ses tests.

    Celle-ci declarait `placement_class` -- le nom de la COLONNE en base -- la ou
    le contrat attend `class`. Le validateur de production
    (`file_source_template.validate_template_contract`) refuse ce contrat, mais la
    fixture ne passait jamais devant lui : elle allait directement au producteur.
    Resultat mesure le 2026-08-01 : les deux tests du gate de champs requis
    (story 22.15) etaient refuses en amont par le gate de placement (22.14, AD-6)
    et le gate qu'ils prouvent n'etait plus atteint par personne. Ils etaient
    rouges sur main, et enregistres nulle part.

    Ce test relie la fixture au validateur reel. Il ne prouve pas que le contrat
    est le BON pour ce test -- il prouve qu'il est POSSIBLE.
    """
    from core.file_source_template import validate_template_contract

    normalised = validate_template_contract(dict(TEMPLATE_CONTRACT))

    # Ce que le producteur lira ensuite, aux memes cles.
    assert normalised["class"] == "actual"
    assert normalised["placement"]["metric"] == "clicks"
    assert normalised["placement"]["period"] == "day"
    assert normalised["required_fields"] == ["clicks", "day"]


def test_a_template_declaring_no_class_is_refused_before_landing():
    """AD-6 / story 22.14, epingle depuis le chemin d'import lui-meme.

    Le refus existait et fonctionnait -- c'est lui qui a fait rougir les deux
    tests du gate de champs requis, parce que la fixture declarait la classe sous
    une cle que le contrat n'a pas. Il n'avait AUCUN test sur ce chemin : sans
    celui-ci, corriger la fixture pourrait desarmer le refus sans que rien ne le
    dise. Il est verifie la ou il agit -- `run_import`, avant l'ouverture du
    registre -- et pas sur le producteur, qui ne stampe pas.
    """
    contract = {k: v for k, v in TEMPLATE_CONTRACT.items() if k != "class"}
    result, opened = _run_import_with(MAPPING_PAYLOAD, template_contract=contract)

    assert result["blocked"] is True
    assert result["reason"] == "no_placement_class"
    assert result["outcome"] == "placement_blocked"
    assert result["published"] is False
    assert opened == [], "un import refuse a quand meme ouvert une ligne de registre"


# ---------------------------------------------------------------------------
# The SECOND ratified realization of a Template (walk G9, 2026-08-08)
# ---------------------------------------------------------------------------


def test_a_catalog_bound_datastream_resolves_its_template_too():
    """`file-source-ingestion.md` binds a Template by TWO paths -- "a reused
    catalog or client-saved Template" -- and this resolver understood only the
    client-saved one.

    A Datastream created through the inbound/upload endpoint pins
    `{template_code, template_version}` (`import_templates.
    create_inbound_datastream`, "the SOLE binding record"). It names a Template,
    in the shape that path writes. With no branch for it the producer came back
    None, the import fell through to the ordinary CSV path, and everything the
    Template governs went with it.

    MEASURED on the deployed QA walk (G9, 2026-08-08): five of its six failures
    -- no column recognition, mapping gate letting everything through, a refusal
    that could not name the missing column, no adaptation lock, no re-analysis
    on a changed layout -- had this ONE cause.
    """
    from unittest.mock import patch as _patch

    from core.csv_excel_import import resolve_file_source_producer

    responses = _responses(source_ref="")
    responses["ds_config"] = (
        {
            "connector_name": "managed_feed",
            "template_code": "OFFLINE_OOH_V1",
            "template_version": 1,
            "channels": ["upload"],
            "publishable": True,
        },
    )
    conn = _FakeConn(responses)

    with _patch(
        "core.import_templates.get_template",
        return_value={"template_code": "OFFLINE_OOH_V1", "version": 1,
                      "contract": TEMPLATE_CONTRACT},
    ):
        producer = resolve_file_source_producer(
            conn,
            project_id=PROJECT_ID,
            datastream_id=DATASTREAM_ID,
            mapping_version_id=MAPPING_VERSION_ID,
        )

    assert producer is not None, "a catalog-bound Datastream resolved no producer"
    # The reference SAYS what it is. `fst_...` would claim a client artifact
    # that does not exist.
    assert producer.template_id == "template:OFFLINE_OOH_V1:1"
    assert producer.template["contract"] == TEMPLATE_CONTRACT
    # No content hash is invented: there is no client artifact to seal.
    assert producer.template["content_hash"] is None


def test_a_wizard_bound_datastream_resolves_the_catalog_template_by_its_code():
    """The assistant binds a catalog Template by CODE under
    `source_owner.managed_feed_template_ref`, never as the `{template_code,
    template_version}` pair the upload door writes (AI-321, G9 2026-08-28: T02
    `file_source` null, the confirm 404, the import ungated -- one binding the
    resolver could not read, again). The version is the catalog's latest for
    that code, and the reference says which one was used."""
    from unittest.mock import patch as _patch

    from core.csv_excel_import import resolve_file_source_producer

    responses = _responses(source_ref="")
    responses["ds_config"] = (
        {
            "source_owner": {
                "managed_feed_source_ref": "dsa_01EXAMPLE",
                "managed_feed_template_ref": "OFFLINE_OOH_V1",
            },
            "channels": ["upload"],
            "connector_name": "managed_feed",
        },
    )
    conn = _FakeConn(responses)

    with (
        _patch("core.import_templates.latest_version", return_value=1) as latest,
        _patch(
            "core.import_templates.get_template",
            return_value={"template_code": "OFFLINE_OOH_V1", "version": 1,
                          "contract": TEMPLATE_CONTRACT},
        ) as get,
    ):
        producer = resolve_file_source_producer(
            conn,
            project_id=PROJECT_ID,
            datastream_id=DATASTREAM_ID,
            mapping_version_id=MAPPING_VERSION_ID,
        )

    assert producer is not None, "a wizard-bound Datastream resolved no producer"
    assert producer.template_id == "template:OFFLINE_OOH_V1:1"
    latest.assert_called_once_with(conn, "OFFLINE_OOH_V1")
    get.assert_called_once_with(conn, "OFFLINE_OOH_V1", 1)


def test_a_wizard_binding_to_a_code_the_catalog_never_had_resolves_nothing():
    from unittest.mock import patch as _patch

    from core.csv_excel_import import resolve_file_source_producer

    responses = _responses(source_ref="")
    responses["ds_config"] = (
        {"source_owner": {"managed_feed_template_ref": "NOT_A_TEMPLATE"}},
    )
    with _patch("core.import_templates.latest_version", return_value=None):
        producer = resolve_file_source_producer(
            _FakeConn(responses),
            project_id=PROJECT_ID,
            datastream_id=DATASTREAM_ID,
            mapping_version_id=MAPPING_VERSION_ID,
        )
    assert producer is None


def test_a_catalog_template_needs_no_per_project_confirmation_row():
    """And that is not a hole in the gate.

    A client-saved `fst_` is an artifact someone assembled; it can drift, so the
    replay is refused until a human confirmation is sealed against its
    `content_hash`. A catalog Template is platform-governed and IMMUTABLE per
    (code, version): there is no second artifact, and the record
    `file-source-ingestion.md` demands -- "proven from the Template's own
    record" -- is the catalog row itself.

    Requiring a confirmation row here does not add safety. It makes the
    inbound/upload path unusable, which is exactly what happened when this
    branch first landed without the distinction.
    """
    from core.csv_excel_import import FileSourceProducer

    catalog = FileSourceProducer(
        {"contract": TEMPLATE_CONTRACT, "content_hash": None},
        {},
        "template:OFFLINE_OOH_V1:1",
        catalog_governed=True,
    )
    assert catalog.confirmed_for(MAPPING_VERSION_ID) is True

    # The `fst_` gate is UNCHANGED: no confirmation row, no replay.
    client_saved = FileSourceProducer(
        {"contract": TEMPLATE_CONTRACT, "content_hash": "sha256:abc"},
        {},
        TEMPLATE_ID,
    )
    assert client_saved.confirmed_for(MAPPING_VERSION_ID) is False


# ---------------------------------------------------------------------------
# 38.16 AC4 -- la derive de Template etait INOBSERVABLE
# ---------------------------------------------------------------------------


def _producer(**kwargs):
    from core.csv_excel_import import FileSourceProducer

    base = dict(
        confirmation_operation_id="op_1",
        confirmation_evidence={
            "mapping_version_id": MAPPING_VERSION_ID,
            "template_content_hash": "hash_v1",
        },
    )
    base.update(kwargs)
    return FileSourceProducer(
        {"contract": TEMPLATE_CONTRACT, "content_hash": base.pop("content_hash", "hash_v1")},
        {},
        TEMPLATE_ID,
        **base,
    )


def test_the_three_refusals_are_told_apart_instead_of_collapsing_into_one_false():
    """AC4 : << template-version drift invalidates the candidate and requires
    rebase >>. L invalidation marchait ; elle etait INVISIBLE.

    `confirmed_for` rendait `False` pour trois situations qui appellent trois
    actes DIFFERENTS -- confirmer, rebaser le mapping, rebaser le Template. Un
    seul mot envoyait deux personnes sur trois faire la mauvaise chose.
    """
    from core import csv_excel_import as mod

    assert _producer().confirmation_state(MAPPING_VERSION_ID) == mod.CONFIRMATION_CONFIRMED

    assert (
        _producer(confirmation_operation_id=None).confirmation_state(MAPPING_VERSION_ID)
        == mod.CONFIRMATION_NEVER
    )

    assert (
        _producer(
            confirmation_evidence={
                "mapping_version_id": "dmap_AUTRE",
                "template_content_hash": "hash_v1",
            }
        ).confirmation_state(MAPPING_VERSION_ID)
        == mod.CONFIRMATION_MAPPING_MOVED
    )

    # LE CAS D AC4 : la confirmation etait valable hier, le Template a bouge.
    assert (
        _producer(content_hash="hash_v2").confirmation_state(MAPPING_VERSION_ID)
        == mod.CONFIRMATION_TEMPLATE_MOVED
    )


def test_the_refusal_itself_is_unchanged():
    """Nommer la raison ne doit RIEN relacher.

    `confirmed_for` est la garde qui empeche un rejeu non confirme d atterrir.
    Elle rend exactement ce qu elle rendait : seule la raison est desormais
    lisible a cote.
    """
    assert _producer().confirmed_for(MAPPING_VERSION_ID) is True
    assert _producer(confirmation_operation_id=None).confirmed_for(MAPPING_VERSION_ID) is False
    assert _producer(content_hash="hash_v2").confirmed_for(MAPPING_VERSION_ID) is False
    assert (
        _producer(
            confirmation_evidence={
                "mapping_version_id": "dmap_AUTRE",
                "template_content_hash": "hash_v1",
            }
        ).confirmed_for(MAPPING_VERSION_ID)
        is False
    )


def test_a_catalog_template_answers_confirmed_without_a_row():
    """Le Template de CATALOGUE est immuable par (code, version) : il n a pas de
    second artefact qui puisse deriver, donc pas de rebase a demander."""
    from core import csv_excel_import as mod

    catalog = _producer(confirmation_operation_id=None)
    catalog.catalog_governed = True
    assert catalog.confirmation_state(MAPPING_VERSION_ID) == mod.CONFIRMATION_CONFIRMED
