"""Arm ONE Datastream end-to-end and measure whether the nightly loop enqueues it.

WHY THIS FILE EXISTS. Action item AI-75, measured in production 2026-07-30: 42
Datastreams, all ``lifecycle_state='draft'``, all ``schedule_mode='manual'``,
``current_mapping_version_id IS NULL`` on all 42, ``app.datastream_executions``
empty, ``next_run_at`` NULL everywhere. The conclusion recorded there was that
the nightly loop's conditions were false "faute de configuration terminee". This
harness stops repeating that sentence and MEASURES it: it walks a Datastream all
the way to ``active`` + published candidate + mapping version + schedule state,
on a disposable Postgres, and then runs the real dispatch function against the
real database to see whether the row is enqueued.

It uses a FILE source (``managed_feed`` / ``file_upload``), which is what makes
it runnable at all: no credential, no OAuth consent, no live account. The bytes
are fabricated in-process, the landing is a throwaway DuckDB file, the Postgres
is disposable.

WHAT IT PROVES, in order of value:
  1. the arming chain WORKS -- a file Datastream really does reach
     ``lifecycle_state='active'``, ``enabled=TRUE``, non-null plan AND mapping
     pointers, a ``published`` execution and a publication-log row, with a
     ``row_count`` that came from rows that physically landed;
  2. the nightly loop STILL does not enqueue it, and the harness names which of
     the loop's conditions is false rather than asserting a bare zero;
  3. the loop is not broken: the SAME arming, with a ``connector_pull`` source
     and a connection row, IS enqueued. The control is what turns "it does not
     work" into "exactly this condition is false".

THE TARGET IT IS JUDGED AGAINST
  * ``docs/product-architecture/file-source-ingestion.md`` (ratified surface for
    file arrivals);
  * ``docs/product-architecture/datastream-workbench-and-wizard.md``, "Exact
    content by mode", step 6: for a managed feed "Cadence is **Required** for
    Sheets or recurring feeds and ``Manual`` for one-off uploads; expected-arrival
    monitoring is **Recommended** for email/webhook."

Read the ``NOT COVERED`` section at the bottom before quoting this file as
coverage of anything it does not touch.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[3]

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- run `python scripts/disposable_postgres.py up`",
)

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: The stamped matrix-class key (mirrors file_source_producer.PLACEMENT_CLASS_KEY).
PLACEMENT_CLASS_KEY = "_placement_class"

FR_CSV = (
    "Date,Cout net,Impressions\n"
    "2026-01-01,1000.00,50000\n"
    "2026-01-02,1250.50,62000\n"
    "2026-01-03,980.25,48500\n"
).encode("utf-8")


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _mint_field() -> str:
    """Mint a FRESH canonical field id per run (the disposable DB is SHARED)."""
    return "mdm_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


def _mint_fields() -> dict[str, str]:
    return {name: _mint_field() for name in ("DATE", "COST", "IMPR")}


def _contract(fields):
    """A catalog Template: the canonical target, never derived from the layout.

    No discriminator here on purpose -- this harness is about ARMING and
    SCHEDULING, and the discriminator path already has its own coverage in
    ``test_file_source_chain_offline.py``. Keeping it out means a refusal here
    can only be an arming defect.
    """
    return {
        "kind": "catalog",
        "required_fields": [fields["DATE"], fields["COST"]],
        "optional_fields": [fields["IMPR"]],
        "grain": "daily",
        "class": "planned",
        "placement": {"metric": fields["COST"], "period": fields["DATE"]},
        "format": "csv",
        "aliases": {
            fields["DATE"]: ["Date", "media date"],
            fields["COST"]: ["Cout net", "net cost"],
            fields["IMPR"]: ["Impressions"],
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """A connection to the DISPOSABLE test database -- never production."""
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    lowered = dsn.lower()
    assert "supabase" not in lowered and "pooler" not in lowered, (
        "TEST_POSTGRES_DSN points at the managed production host; refusing to run. "
        "Use `python scripts/disposable_postgres.py up`."
    )
    c = psycopg.connect(dsn, connect_timeout=5)
    with c.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        is_super, bypasses = cur.fetchone()
    assert not is_super, "this suite must not run as a superuser: RLS would not apply"
    assert not bypasses, "the role holds BYPASSRLS: every isolation assertion is vacuous"
    missing = _missing_tables(c)
    if missing:
        c.close()
        pytest.skip(
            "the test database is missing "
            + ", ".join(missing)
            + " -- run `python scripts/disposable_postgres.py up`"
        )
    try:
        yield c
    finally:
        c.rollback()
        c.close()


_REQUIRED_TABLES = (
    "organizations",
    "projects",
    "datastreams",
    "mdm_canonical_fields",
    "file_source_templates",
    "datastream_plan_versions",
    "datastream_mapping_versions",
    "datastream_schedule_state",
    "datastream_executions",
    "datastream_publication_log",
    "connection_ref",
)


def _missing_tables(c) -> list[str]:
    with c.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'app' AND table_name = ANY(%s)",
            (list(_REQUIRED_TABLES),),
        )
        present = {r[0] for r in cur.fetchall()}
    return [t for t in _REQUIRED_TABLES if t not in present]


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """Land into a throwaway DuckDB file (BigQuery needs a project + credentials)."""
    db = tmp_path / "raw.duckdb"
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db))
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    return db


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed(c):
    org_id = _id("org_")
    project_id = _id("proj_")
    fields = _mint_fields()
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'recurring-arming-harness') ON CONFLICT DO NOTHING",
            (org_id, org_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, org_id, created_by, status) "
            "VALUES (%s, %s, %s, %s, 'recurring-arming-harness', 'active') "
            "ON CONFLICT DO NOTHING",
            (project_id, project_id, project_id, org_id),
        )
        # `value_type` is NOT NULL since migration 241 -- a canonical field carries
        # the half this table never had, because `semantic_concept_versions.value_type`
        # is NOT NULL and without it a field could never become a Concept. It has no
        # default, so an INSERT that omits it is refused. Each field states the type
        # its role implies rather than a blanket value.
        for role, kind, name, agg, value_type in (
            ("COST", "metric", "net_cost", "sum", "money"),
            ("IMPR", "metric", "impressions", "sum", "integer"),
            ("DATE", "dimension", "media_date", None, "date"),
        ):
            cur.execute(
                "INSERT INTO app.mdm_canonical_fields "
                "(id, project_id, concept_kind, canonical_name, aggregation, "
                " value_type, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'test')",
                (
                    fields[role],
                    project_id,
                    kind,
                    f"{name}_{project_id[-6:]}",
                    agg,
                    value_type,
                ),
            )
    c.commit()
    return org_id, project_id, fields


def _make_template(c, *, project_id, org_id, contract, code):
    from core.file_source_template import create_file_source_template

    row = create_file_source_template(
        c,
        project_id=project_id,
        org_id=org_id,
        template_code=code,
        contract=contract,
        created_by="owner@example.com",
    )
    c.commit()
    return row


# ---------------------------------------------------------------------------
# The reviewed intent the wizard hands to activation.
# ---------------------------------------------------------------------------


def _profile(sample):
    """A complete 12.3 field profile -- the mapping schema requires every key."""
    return {
        "nullable": False,
        "unique": False,
        "cardinality_signal": "low",
        "sample_values": [sample],
        "confidence": 0.9,
    }


def _suggestion(role, aggregation):
    """A complete field suggestion. `status` is `const: "suggested"` in the schema:
    the operator's confirmation lives on the BINDING, never on the suggestion."""
    return {
        "semantic_role": role,
        "aggregation": aggregation,
        "non_additive": False,
        "currency": "EUR",
        "sensitivity": "none",
        "status": "suggested",
        "evidence": ["observed_schema"],
    }


def _binding(canonical_target):
    return {
        "canonical_target": canonical_target,
        "mdm_target": canonical_target,
        "status": "confirmed",
        "blocking_reason": None,
        "confirmed_by": "owner@example.com",
        "confirmed_reason": "Confirmed in Datastream final review",
    }


def _confirmed_bundle(fields):
    """The `confirmed_intent_bundle` a completed final review carries."""
    return {
        "datastream_name": "Arming harness feed",
        "data_role": "Forecast & plan",
        "joint_grain": ["Date"],
        "field_mappings": [
            {
                "field_id": "Date",
                "included": True,
                "semantic_type": "date",
                "profile": _profile("2026-01-01"),
                "suggestion": _suggestion("dimension", "none"),
                "binding": _binding(fields["DATE"]),
            },
            {
                "field_id": "Cout net",
                "included": True,
                "semantic_type": "number",
                "profile": _profile("1000.00"),
                "suggestion": _suggestion("measure", "sum"),
                "binding": _binding(fields["COST"]),
            },
            {
                "field_id": "Impressions",
                "included": True,
                "semantic_type": "number",
                "profile": _profile("50000"),
                "suggestion": _suggestion("measure", "sum"),
                "binding": _binding(fields["IMPR"]),
            },
        ],
    }


def _compile(*, fields, org_id, channel, template_id, requested_mode):
    """Run the REAL `compile_materialization_contract` for a managed feed.

    This is the function that decides the cadence, so the harness must go through
    it rather than write `schedule_mode` itself: the whole question is what the
    product does with an operator who asks for a daily cadence.
    """
    from core.datastream_activation import compile_materialization_contract

    return compile_materialization_contract(
        frozen_review={
            "project_id": None,
            "confirmed_intent_bundle": _confirmed_bundle(fields),
        },
        operator_input={
            "mode": "managed_feed",
            "source": {
                "channel": channel,
                "staged_asset_ref": _id("dsa_"),
                "template_ref": template_id,
            },
            "configure": {},
            "schedule": {"mode": requested_mode},
        },
        observation={"safe_metadata": {"detected_format": "csv"}, "schema_hash": "0" * 64},
        org_id=org_id,
        actor="owner@example.com",
        timezone_name="Europe/Paris",
    )


def _insert_datastream(c, *, project_id, org_id, contract):
    """Insert the Datastream row exactly as `materialize_draft_mutation` does.

    Mirrors ``datastream_activation.materialize_draft_mutation`` lines 731-770:
    the same source-kind gating of ``module_name`` / ``connection_ref_id``, the
    same ``plan_schedule_mode -> schedule_mode`` table, the same ``source_owner``
    keys (including AI-91's dedicated ``managed_feed_template_ref``), the same
    ``lifecycle_state='draft'`` and ``enabled=FALSE`` start.
    """
    source = contract["plan_intent"]["source"]
    source_kind = source["kind"]
    plan_schedule = contract["plan_intent"].get("schedule") or {}
    plan_schedule_mode = str(plan_schedule.get("mode") or "manual")
    schedule_mode = {"daily": "nightly", "nightly": "nightly", "hourly": "hourly"}.get(
        plan_schedule_mode, "manual"
    )
    source_owner = {
        "selected_account_ref": source.get("selected_account_ref"),
        "declared_writer": (source.get("external_object") or {}).get("writer_identity"),
        "managed_feed_source_ref": (source.get("managed_feed") or {}).get("source_ref"),
        "managed_feed_template_ref": (source.get("managed_feed") or {}).get("template_ref"),
    }
    ds_id = _id("ds_")
    with c.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastreams
               (id,project_id,org_id,name,module_name,connection_ref_id,report_profile_id,
                enabled,schedule_mode,refetch_days,date_window_days,config,created_by,
                lifecycle_state,source_kind,data_role)
               VALUES (%s,%s,%s,%s,NULL,NULL,NULL,FALSE,%s,3,30,%s::jsonb,%s,'draft',%s,%s)""",
            (
                ds_id,
                project_id,
                org_id,
                contract["confirmed_intent_bundle"]["datastream_name"],
                schedule_mode,
                json.dumps({"source_owner": source_owner}, sort_keys=True),
                "owner@example.com",
                source_kind,
                contract["confirmed_intent_bundle"]["data_role"],
            ),
        )
    c.commit()
    return ds_id, schedule_mode


def _versions(c, *, ds_id, project_id, contract):
    """Append the plan + mapping versions through the production services.

    Both go through what `materialize_draft_mutation` calls:
    `save_datastream_intent` (which also writes the `datastream_schedule_state`
    row the nightly loop INNER JOINs), `save_field_mapping` and
    `compile_projection`.
    """
    from core.datastream_field_mapping import save_field_mapping
    from core.datastream_intents import save_datastream_intent
    from core.datastream_projection import compile_projection

    operation_id = _id("op_")
    plan = save_datastream_intent(
        datastream_id=ds_id,
        project_id=project_id,
        intent=contract["plan_intent"],
        identity="owner@example.com",
        idempotency_key=f"{operation_id}:plan",
        conn=c,
        commit=False,
        advance_pointer=False,
    )
    mapping = save_field_mapping(
        datastream_id=ds_id,
        project_id=project_id,
        mapping_payload=contract["mapping_payload"],
        identity="owner@example.com",
        idempotency_key=f"{operation_id}:mapping",
        conn=c,
        pinned_plan_version_id=plan["id"],
        advance_pointer=False,
        commit=False,
    )
    projection = compile_projection(mapping)
    assert projection.get("executable"), (
        "the confirmed mapping does not compile to an executable projection; "
        "activation refuses this before any candidate runs"
    )
    c.commit()
    return plan["id"], mapping["id"], projection


def _confirm_template(c, *, template, project_id, org_id, ds_id, fields, plan_id, mapping_id):
    """Pass the epic-22 human confirmation gate, THROUGH THE PRODUCT DOOR.

    `run_import` refuses an arrival whose Template + Mapping carry no matching
    human confirmation evidence (`file_source_confirmation_required`,
    csv_excel_import.py). The contract is legitimate and it is not what this
    harness measures -- but the harness pre-dates it, so all seven arming tests
    died on the refusal before reaching a single measurement (AI-187).

    The row is NOT hand-inserted. `app.file_source_template_confirmations` has an
    FK to `app.operations` and a validation trigger (migration 188), and an INSERT
    improvised here would have to reproduce both -- which is how a harness starts
    proving that its own fixture is self-consistent rather than that the product
    works. `confirm_adaptation_template` is the same function the confirmation
    surface calls: it mints the operation, records the immutable proof, and binds
    the EXISTING mapping version rather than minting a second one (which is why
    it, and not `confirm_mapping_version`, is the right door here).

    The gate verdict is COMPUTED from the real template and the real sample, not
    asserted: a fixture that hands itself `{"passed": True}` would keep arming
    green while the gate refuses every real file.
    """
    import csv
    import hashlib
    import io

    from core.file_source_gate import confirm_adaptation_template, evaluate_required_field_gate

    # {source column -> canonical field}, exactly what the operator confirms.
    mapping = {
        "Date": fields["DATE"],
        "Cout net": fields["COST"],
        "Impressions": fields["IMPR"],
    }
    sample_rows = list(csv.DictReader(io.StringIO(FR_CSV.decode("utf-8"))))
    gate_result = evaluate_required_field_gate(template["contract"], mapping, sample_rows)
    assert gate_result.get("passed"), (
        f"the required-field gate refuses this harness's own sample: {gate_result}"
    )

    # EVERY key below is re-checked by the migration-188 trigger against the
    # rows it points at (`validate_file_source_template_confirmation`): the
    # template's own content_hash, the mapping version's project/datastream/plan,
    # and `actor` against `confirmed_by`. Evidence that merely looks plausible is
    # rejected, which is the point -- the proof is bound to real rows or it is
    # not a proof.
    evidence = {
        "template_id": template["id"],
        "mapping_version_id": mapping_id,
        "plan_version_id": plan_id,
        "template_content_hash": template["content_hash"],
        "sample_content_hash": hashlib.sha256(FR_CSV).hexdigest(),
        "sample_filename": "media_plan_2026.csv",
        "actor": "owner@example.com",
        "resolutions": [],
        "accepted_warnings": [],
    }
    result = confirm_adaptation_template(
        c,
        template_id=template["id"],
        project_id=project_id,
        org_id=org_id,
        datastream_id=ds_id,
        actor="owner@example.com",
        gate_result=gate_result,
        evidence=evidence,
        idempotency_key=f"confirm:{ds_id}:{mapping_id}",
        content_hash=template["content_hash"],
    )
    c.commit()
    assert result.get("confirmed") is True, result
    return result


def _run_import(c, *, ds_id, project_id, plan_id, mapping_id, projection, execution_id=None):
    """Drive the arrival exactly as the upload and email callers do.

    ``run_import`` OPENS ITS OWN execution through ``managed_feed_ledger`` -- it
    is the 042 candidate, and the ledger row's ``execution_id`` FK points at it.
    That is why this helper does not pre-create one; passing ``execution_id``
    reproduces the activation driver's shape instead, and is used only by the
    test that measures what happens then.
    """
    from core.csv_excel_import import resolve_file_source_producer, run_import
    from core.raw_landing import candidate_execution

    producer = resolve_file_source_producer(
        c, project_id=project_id, datastream_id=ds_id, mapping_version_id=mapping_id
    )
    assert producer is not None, (
        "the Datastream's file-source Template binding did not resolve; the "
        "candidate would validate a shape the Datastream never lands."
    )

    def _call():
        return run_import(
            FR_CSV,
            datastream_id=ds_id,
            project_id=project_id,
            plan_version_id=plan_id,
            mapping_version_id=mapping_id,
            projection_plan=projection,
            actor="owner@example.com",
            idempotency_key=f"candidate:{execution_id or ds_id}",
            source_metadata={"filename": "media_plan_2026.csv", "channel": "file_upload"},
            contract={},
            conn=c,
            producer=producer,
        )

    if execution_id is None:
        outcome = _call()
    else:
        with candidate_execution(execution_id):
            outcome = _call()
    c.commit()
    return outcome


def _adapter_result(*, execution_id, outcome, plan_id, mapping_id):
    """The verified candidate evidence the driver returns, same shape and hashes."""
    import hashlib

    from core.raw_landing import landed_candidate_relations

    row_count = int(outcome.get("landed_row_count") or 0)
    # The reference the driver now publishes: the relation the landing actually
    # used, READ from the registry rather than fabricated. A hand-built double
    # that keeps the old synthetic path would assert a shape the driver no
    # longer produces -- the exact trap the connector-side repair named.
    landed = landed_candidate_relations(execution_id)
    replayed = str((outcome.get("landing") or {}).get("table") or "")
    if landed:
        artifact_ref = landed[0]
    elif execution_id in replayed:
        artifact_ref = replayed
    else:
        artifact_ref = f"execution/{execution_id}/candidate/relation"
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "execution_id": execution_id,
                "plan_version_id": plan_id,
                "mapping_version_id": mapping_id,
                "row_count": row_count,
                "rejected": outcome.get("rejected_count"),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "adapter_verified": True,
        "placeholder": False,
        "adapter_ref": "managed_feed.file.candidate.isolated.v1",
        "execution_id": execution_id,
        "artifact_ref": artifact_ref,
        "content_hash": fingerprint,
        "validated_content_hash": fingerprint,
        "artifact_hash": fingerprint,
        "row_count": row_count,
        # The shape fingerprint, distinct from the content one. Every driver
        # returns it (`inbound/adapters/datastream_activation_drivers.py`), and
        # it is the value the publication diff compares.
        "schema_hash": hashlib.sha256(
            json.dumps({"schema": "candidate", "plan_version_id": plan_id}, sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest(),
        "coverage": {"schema": "available", "values": "available" if row_count else "empty_file"},
    }


def _publish_and_activate(
    c, *, project_id, ds_id, ready, contract, plan_id, mapping_id, projection
):
    """Review the Ready candidate and run the real publish+activate mutation."""
    from core.datastream_activation import build_candidate_review, publish_activate_mutation

    review = build_candidate_review(
        {
            **ready,
            "project_id": project_id,
            "datastream_id": ds_id,
            "adapter_verified": True,
            "placeholder": False,
            "row_count": ready["row_count"],
            "content_hash": ready["content_hash"],
            "plan_version_id": plan_id,
            "mapping_version_id": mapping_id,
            # THE COMPILED PROJECTION, which is what `read_candidate_review`
            # passes (`datastream_activation.py:1633`, `"output_plan": projection`).
            # This used to be a hand-rolled `{"relation": ...}` with no grain, and
            # a fixture that carries no grain cannot see a publication that loses
            # one: both sides read empty and agreed.
            "output_plan": {**projection, "relation": f"{ds_id}.full_grain"},
            "schedule": contract["schedule"],
            "expected_current_execution_id": None,
        }
    )
    result = publish_activate_mutation(
        c, review=review, actor="owner@example.com", operation_id=_id("op_")
    )
    c.commit()
    return result


def _arm_file_datastream(c, *, requested_mode="daily", channel="file_upload"):
    """The whole arming sequence, returning everything the assertions need."""
    org_id, project_id, fields = _seed(c)
    template = _make_template(
        c,
        project_id=project_id,
        org_id=org_id,
        contract=_contract(fields),
        code=f"ARM_{project_id[-6:].upper()}",
    )
    contract = _compile(
        fields=fields,
        org_id=org_id,
        channel=channel,
        template_id=template["id"],
        requested_mode=requested_mode,
    )
    ds_id, schedule_mode = _insert_datastream(
        c, project_id=project_id, org_id=org_id, contract=contract
    )
    plan_id, mapping_id, projection = _versions(
        c, ds_id=ds_id, project_id=project_id, contract=contract
    )
    _confirm_template(
        c,
        template=template,
        project_id=project_id,
        org_id=org_id,
        ds_id=ds_id,
        fields=fields,
        plan_id=plan_id,
        mapping_id=mapping_id,
    )
    outcome = _run_import(
        c,
        ds_id=ds_id,
        project_id=project_id,
        plan_id=plan_id,
        mapping_id=mapping_id,
        projection=projection,
    )
    execution_id = outcome["execution"]["id"]
    from core.datastream_activation import complete_candidate_from_adapter

    ready = complete_candidate_from_adapter(
        c,
        project_id=project_id,
        datastream_id=ds_id,
        execution_id=execution_id,
        actor="owner@example.com",
        adapter_result=_adapter_result(
            execution_id=execution_id, outcome=outcome, plan_id=plan_id, mapping_id=mapping_id
        ),
    )
    c.commit()
    published = _publish_and_activate(
        c,
        project_id=project_id,
        ds_id=ds_id,
        ready={**ready, **_adapter_result(
            execution_id=execution_id, outcome=outcome, plan_id=plan_id, mapping_id=mapping_id
        )},
        contract=contract,
        plan_id=plan_id,
        mapping_id=mapping_id,
        projection=projection,
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "fields": fields,
        "template": template,
        "contract": contract,
        "datastream_id": ds_id,
        "schedule_mode": schedule_mode,
        "plan_version_id": plan_id,
        "mapping_version_id": mapping_id,
        "execution_id": execution_id,
        "outcome": outcome,
        "ready": ready,
        "published": published,
    }


# ---------------------------------------------------------------------------
# The nightly loop, measured condition by condition.
# ---------------------------------------------------------------------------

#: The loop's conditions, as they read in
#: ``core.scheduler._dispatch_nightly_datastreams``. Kept as SQL so the harness
#: reports WHICH one is false rather than a bare "not enqueued".
_LOOP_CONDITIONS = (
    ("project_active", "EXISTS (SELECT 1 FROM app.projects p WHERE p.id=ds.project_id"
                       " AND p.status='active')"),
    ("plan_version_joins", "EXISTS (SELECT 1 FROM app.datastream_plan_versions pv"
                           " WHERE pv.id=ds.current_plan_version_id"
                           " AND pv.datastream_id=ds.id AND pv.project_id=ds.project_id)"),
    ("mapping_version_joins", "EXISTS (SELECT 1 FROM app.datastream_mapping_versions mv"
                              " WHERE mv.id=ds.current_mapping_version_id"
                              " AND mv.datastream_id=ds.id AND mv.project_id=ds.project_id)"),
    ("schedule_state_joins", "EXISTS (SELECT 1 FROM app.datastream_schedule_state ss"
                             " WHERE ss.plan_version_id=ds.current_plan_version_id"
                             " AND ss.datastream_id=ds.id AND ss.project_id=ds.project_id)"),
    ("enabled", "ds.enabled = TRUE"),
    ("lifecycle_active", "ds.lifecycle_state = 'active'"),
    ("plan_pointer_set", "ds.current_plan_version_id IS NOT NULL"),
    ("mapping_pointer_set", "ds.current_mapping_version_id IS NOT NULL"),
    # AI-217: the dispatcher selects BOTH cadences that run once a period. A
    # mirror stuck on `= 'nightly'` would report a due weekly row as ineligible
    # while the dispatcher enqueues it -- a diagnostic that lies is worse than
    # none, because this harness is what a person reads to know WHY nothing ran.
    ("schedule_mode_once_a_period", "ds.schedule_mode IN ('nightly', 'weekly')"),
    ("not_external_bq", "COALESCE(ds.source_kind,'connector_pull') <> 'external_bq'"),
    ("module_enabled", "NOT EXISTS (SELECT 1 FROM app.project_modules pm"
                       " WHERE pm.project_id=ds.project_id AND pm.module_name=ds.module_name"
                       " AND pm.enabled = FALSE)"),
    # The two Python-side conditions, after the SQL. `_dispatch_nightly_datastreams`
    # does `if conn_id is None: continue` and then
    # `if cr_status != 'active' or not cr_enabled: continue`.
    ("has_connection_ref", "ds.connection_ref_id IS NOT NULL"),
    ("connection_active", "EXISTS (SELECT 1 FROM app.connection_ref cr"
                          " WHERE cr.id=ds.connection_ref_id AND cr.status='active'"
                          " AND cr.enabled = TRUE)"),
)


def _loop_conditions(c, ds_id: str, project_id: str) -> dict[str, bool]:
    """Evaluate every nightly-loop condition for one Datastream, in the database."""
    select = ", ".join(f"({sql}) AS {name}" for name, sql in _LOOP_CONDITIONS)
    with c.cursor() as cur:
        cur.execute(
            f"SELECT {select} FROM app.datastreams ds WHERE ds.id=%s AND ds.project_id=%s",  # noqa: S608
            (ds_id, project_id),
        )
        row = cur.fetchone()
        names = [d[0] for d in cur.description]
    assert row is not None, f"datastream {ds_id} not found"
    return dict(zip(names, row))


class _RecordingQueue:
    """Stands in for `core.queue` so dispatch is measured without a worker.

    `enqueue_pull` is a WRITE against `app.jobs` with its own connection and its
    own idempotency; recording the call is what the loop's contract actually is
    (`dispatch_nightly` "only enqueues", AD-12).
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue_pull(
        self, conn_id, date_from, date_to, *, requested_by, datastream_id=None,
        execution_id=None, module_name=None,
    ):
        # Story 63.1: the run this window belongs to. Omitting it from the double
        # makes the dispatch swallow a TypeError and enqueue nothing -- which is
        # exactly what happened when AI-301 taught the dispatch to pass
        # module_name and this double did not know the word.
        job = {
            "connection_ref_id": conn_id,
            "date_from": date_from,
            "date_to": date_to,
            "requested_by": requested_by,
            "datastream_id": datastream_id,
            "execution_id": execution_id,
            "module_name": module_name,
        }
        self.calls.append(job)
        # AND IT MUST DECLARE ITS STATE. The same trap as `module_name` above,
        # one AI-301 rule later: the dispatch now reads a queued window
        # POSITIVELY -- `state` in ('queued','running') -- so anything else is a
        # refusal that must not move the clock. A double that returned no `state`
        # was therefore classified as `access_denied`-shaped, `on_enqueued` never
        # ran, and `next_run_at` stayed NULL while the test blamed the advance.
        # The real `enqueue_pull` answers `{job_id, pull_id, state}`
        # (`core/queue.py:308,333`); so does this.
        return {"job_id": _id("job_"), "pull_id": _id("pull_"), "state": "queued", **job}


def _dispatch(dsn: str):
    """Run the REAL `_dispatch_nightly_datastreams` against the disposable DB."""
    from contextlib import contextmanager

    from core.scheduler import _dispatch_nightly_datastreams

    @contextmanager
    def get_connection():
        c = psycopg.connect(dsn, connect_timeout=5)
        try:
            yield c
        finally:
            c.close()

    queue = _RecordingQueue()
    jobs, count = _dispatch_nightly_datastreams(
        date.today(), "arming-harness", queue, get_connection
    )
    return queue, jobs, count


# ===========================================================================
# 1. THE ARMING CHAIN -- does a file Datastream reach `active` at all?
# ===========================================================================


@requires_postgres
def test_a_file_datastream_reaches_active_with_a_published_candidate(conn, warehouse):
    """AI-75's four production facts, inverted on one Datastream.

    Production 2026-07-30: 0/42 active, 0/42 with a mapping pointer, 0 executions,
    0 publication rows. This asserts the opposite on one file Datastream, and
    every value comes from rows that physically landed -- `row_count` is
    `landed_row_count`, not the parse count.
    """
    armed = _arm_file_datastream(conn)

    assert armed["outcome"]["landed_row_count"] == 3, (
        f"the candidate import did not land: {armed['outcome']}"
    )
    assert armed["ready"]["state"] == "ready", armed["ready"]
    assert armed["published"].outcome == "succeeded"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT lifecycle_state, enabled, schedule_mode, current_plan_version_id,"
            "       current_mapping_version_id, current_published_execution_id"
            "  FROM app.datastreams WHERE id=%s",
            (armed["datastream_id"],),
        )
        row = cur.fetchone()
    lifecycle, enabled, schedule_mode, plan_ptr, mapping_ptr, published_ptr = row

    assert lifecycle == "active", "AI-75 fact 1 (all 42 draft) is now false for this one"
    assert enabled is True, "AI-75 fact 2 (enabled=false on 41/42) is now false for this one"
    assert plan_ptr == armed["plan_version_id"]
    assert mapping_ptr == armed["mapping_version_id"], (
        "AI-75 fact 3: current_mapping_version_id IS NULL on all 42 -- the nightly "
        "loop INNER JOINs this pointer, so a null one is not a warning, it is exclusion."
    )
    assert published_ptr == armed["execution_id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, row_count FROM app.datastream_executions WHERE id=%s",
            (armed["execution_id"],),
        )
        state, row_count = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM app.datastream_publication_log WHERE execution_id=%s",
            (armed["execution_id"],),
        )
        log_rows = cur.fetchone()[0]
    assert state == "published"
    assert row_count == 3, "the published row_count must be what landed, not what parsed"
    assert log_rows == 1, "AI-75 fact 4: app.datastream_executions was empty in production"

    # The cadence the OPERATOR asked for was `daily`. What the product recorded is
    # the ratified answer for a one-off upload, and the harness records it rather
    # than working around it -- see the schedule test below.
    assert schedule_mode == armed["schedule_mode"]


# ===========================================================================
# 2. THE CADENCE -- what the wizard contract does with a requested daily feed.
# ===========================================================================


@requires_postgres
def test_a_file_upload_feed_is_forced_to_manual_by_the_ratified_contract(conn, warehouse):
    """`datastream-workbench-and-wizard.md`, step 6, Managed feed column:

        "Cadence is **Required** for Sheets or recurring feeds and `Manual` for
        one-off uploads; expected-arrival monitoring is **Recommended** for
        email/webhook."

    `compile_materialization_contract` implements exactly that
    (``datastream_activation.py:1412``): every managed-feed channel other than
    ``google_sheets`` is pinned to ``manual``, whatever the operator requested.

    This test exists so that nobody "repairs" the manual cadence into a nightly
    one believing it is a bug. It is the ratified target. Making a file-upload
    Datastream recur is a PRODUCT decision that is not written anywhere -- see the
    module's NOT COVERED note.
    """
    armed = _arm_file_datastream(conn, requested_mode="daily")

    assert armed["contract"]["plan_intent"]["schedule"]["mode"] == "manual"
    assert armed["contract"]["schedule"]["activation_kind"] == "manual"
    assert armed["contract"]["schedule"]["next_run_at"] is None
    assert armed["schedule_mode"] == "manual"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT next_run_at FROM app.datastream_schedule_state "
            "WHERE datastream_id=%s AND plan_version_id=%s",
            (armed["datastream_id"], armed["plan_version_id"]),
        )
        row = cur.fetchone()
    assert row is not None, (
        "save_datastream_intent must write the schedule-state row: the nightly "
        "loop INNER JOINs it, so its absence is exclusion, not a missing detail."
    )
    assert row[0] is None, "a manual activation must not arm next_run_at"


@requires_postgres
def test_an_inbound_email_feed_arms_an_arrival_monitor_not_a_schedule(conn, warehouse):
    """Same document, same row: email/webhook get expected-arrival monitoring.

    Recorded here because it is the other half of the answer to "how does a file
    Datastream recur": for email it recurs by ARRIVAL, and the contract arms a
    monitor rather than a cadence. Nothing in the nightly loop reads that monitor.
    """
    from core.datastream_activation import compile_materialization_contract

    org_id, project_id, fields = _seed(conn)
    contract = compile_materialization_contract(
        frozen_review={"confirmed_intent_bundle": _confirmed_bundle(fields)},
        operator_input={
            "mode": "managed_feed",
            "source": {"channel": "inbound_email"},
            "configure": {},
            "schedule": {"mode": "daily"},
        },
        observation={"safe_metadata": {"detected_format": "csv"}, "schema_hash": "0" * 64},
        org_id=org_id,
        actor="owner@example.com",
        timezone_name="Europe/Paris",
    )
    assert contract["schedule"]["activation_kind"] == "arrival_monitor"
    assert contract["schedule"]["schedule_mode"] == "manual"
    assert contract["schedule"]["next_run_at"] is None


# ===========================================================================
# 2b. TWO STRUCTURAL BLOCKS ON THE ARMING PATH, pinned so they cannot return.
# ===========================================================================


@requires_postgres
def test_grain_evidence_must_be_an_object_or_publication_aborts(conn, warehouse):
    """Regression guard for the defect that kept `datastream_output_versions` empty.

    `app.datastream_output_versions.grain_evidence` and
    `app.datastream_execution_stage_evidence.grain_evidence` are both declared
    ``JSONB NOT NULL DEFAULT '[]'::jsonb`` with a
    ``CHECK (app.safe_preconfiguration_evidence(...))`` (migration 138), and that
    function requires ``jsonb_typeof(value) = 'object'`` (migration 134). The
    column DEFAULT therefore violates the column's own CHECK, measured:

        SELECT app.safe_preconfiguration_evidence('[]'::jsonb);  -- false

    `publish_activate_mutation` wrote the bare grain list into both, so EVERY
    publish+activate of a Datastream carrying an ``org_id`` aborted on a
    CheckViolation before a single Output row existed. That is the mechanical
    reason production shows `app.datastream_output_versions` empty and zero
    `active` Datastreams -- not "setup was never finished".

    This test asserts the row is there AND is an object. If a later edit passes
    the list again, the publication raises before this line.
    """
    armed = _arm_file_datastream(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grain_evidence FROM app.datastream_output_versions WHERE execution_id=%s",
            (armed["execution_id"],),
        )
        rows = cur.fetchall()
        cur.execute("SELECT app.safe_preconfiguration_evidence('[]'::jsonb)")
        empty_array_is_safe = cur.fetchone()[0]
    assert empty_array_is_safe is False, (
        "the evidence-safety function now accepts an array; if a migration relaxed "
        "the CHECK, the wrapping in publish_activate_mutation is redundant, not wrong "
        "-- but this docstring is out of date."
    )
    assert len(rows) == 1, "publication produced no Output version"
    assert isinstance(rows[0][0], dict), f"grain_evidence is not an object: {rows[0][0]!r}"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.datastream_execution_stage_evidence "
            "WHERE execution_id=%s AND stage='published'",
            (armed["execution_id"],),
        )
        assert cur.fetchone()[0] == 1, (
            "the published stage evidence is missing: the second grain_evidence "
            "column would have failed two statements after the Output insert."
        )


@requires_postgres
def test_publishing_promotes_the_candidate_into_the_shared_relation(conn, warehouse):
    """Publishing a candidate MEANS promoting it, and nothing did.

    `raw_landing` states the contract itself: "Publication is then
    `promote_candidate`, which appends those rows into the shared table with
    their pull_id intact (AD-7)". Isolation per execution is what keeps a
    candidate out of the marts; promotion is the only thing that lets it in, and
    the staging models name the SHARED table only.

    `publish_activate_mutation` never called it. The two callers of
    `promote_candidate` are `csv_excel_import` under `publish_candidate=True` and
    `managed_file_dispatch`, which works from a dispatch row -- and the wizard
    path mints none. Measured on preprod 2026-08-08: a Datastream `active`, an
    execution `published`, an Output version, and zero rows outside
    `..__cand_<execution_id>`.

    Asserted on the WAREHOUSE, not on a call: the shared relation holds the rows,
    and the isolated one is gone. A guard that watched for a function name would
    stay green over a promotion that moved nothing.
    """
    import duckdb

    armed = _arm_file_datastream(conn)
    relation = _landing_relation(conn, armed["execution_id"])
    suffix = f"__cand_{armed['execution_id']}"
    shared = relation[: -len(suffix)] if relation.endswith(suffix) else relation

    con = duckdb.connect(str(warehouse))
    try:
        tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        assert shared in tables, (
            f"the shared relation {shared!r} does not exist after publication; "
            f"the warehouse holds {sorted(tables)}"
        )
        promoted = con.execute(f"SELECT count(*) FROM {shared}").fetchone()[0]  # noqa: S608
        published = int(armed["outcome"]["row_count"])
        assert promoted == published, (
            f"{promoted} rows reached the shared relation, {published} were published"
        )
        assert relation not in tables, (
            "the isolated candidate relation survived its own promotion"
        )
    finally:
        con.close()


def _landing_relation(c, execution_id: str) -> str:
    """The relation the import ACTUALLY wrote, read from the ledger row."""
    with c.cursor() as cur:
        cur.execute(
            "SELECT landing_relation FROM app.managed_feed_import_ledger "
            "WHERE execution_id=%s",
            (execution_id,),
        )
        row = cur.fetchone()
    assert row is not None, "the published execution has no import ledger row"
    return str(row[0]).rsplit(".", 1)[-1]


@requires_postgres
def test_one_publication_records_one_grain(conn, warehouse):
    """The Output version and the stage evidence of the SAME act must agree.

    `output` at the Output-version INSERT is a per-output SPEC, and when the
    projection declares no `outputs` list that spec is SYNTHESIZED from three keys
    -- kind, name, relation. It carries no `grain`, so the Output recorded `[]` on
    every publication that did not hand-roll its outputs, which is all of them,
    while the `published` stage evidence two statements later read the grain from
    the projection and recorded it correctly.

    Measured on preprod 2026-08-08, one transaction, `dse_01KZGNQ8H7R68EH0EAB9A2D670`:
    stage `{"grain": ["campaign", "date"]}` against output `{"grain": []}`. The
    Outputs tab reads the empty one.

    Asserted as the EQUALITY of the two rows AND as non-emptiness: equality alone
    would be satisfied by both being empty, which is the defect twice over.
    """
    armed = _arm_file_datastream(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grain_evidence FROM app.datastream_output_versions WHERE execution_id=%s",
            (armed["execution_id"],),
        )
        output_grain = cur.fetchone()[0]
        cur.execute(
            "SELECT grain_evidence FROM app.datastream_execution_stage_evidence "
            "WHERE execution_id=%s AND stage='published'",
            (armed["execution_id"],),
        )
        stage_grain = cur.fetchone()[0]
    assert output_grain.get("grain"), (
        "the published Output records no grain while the mapping declares one: "
        f"output {output_grain!r}, stage {stage_grain!r}"
    )
    assert output_grain["grain"] == stage_grain["grain"], (
        "one publication recorded two different grains: "
        f"output {output_grain!r} vs stage {stage_grain!r}"
    )


@requires_postgres
def test_stage_evidence_with_no_grain_still_lands(conn, warehouse):
    """The defect above was repaired at two CALL SITES; the WRITER kept it.

    `append_stage_evidence` defaulted `grain_evidence` to `[]` -- the array the
    column's own CHECK refuses. The two `publish_activate_mutation` call sites
    were repaired by wrapping their own value, so publication survived; every
    OTHER caller carries no grain at all and still handed the writer that `[]`.
    `_execute_candidate` is one: it appends `collected` / `mapped` / `processed`
    evidence for EVERY mode, and died on a raw CheckViolation after the import
    had already landed its rows and committed its ledger row.

    Asserted on the EFFECT against the real constraint: a stage evidence row with
    no grain INSERTS, and its stored value is an object. A caller that does hand
    a bare list is refused BY NAME, because a CheckViolation names the column and
    never the caller.
    """
    from core.datastream_workbench import WorkbenchValidationError, append_stage_evidence

    armed = _arm_file_datastream(conn)
    evidence_id = append_stage_evidence(
        conn,
        org_id=armed["org_id"],
        project_id=armed["project_id"],
        datastream_id=armed["datastream_id"],
        execution_id=armed["execution_id"],
        stage="collected",
        actor="test",
        plan_version_id=armed["plan_version_id"],
        mapping_version_id=armed["mapping_version_id"],
        evidence={"phase_state": "succeeded", "artifact_ref": "execution/x/stage/collected"},
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grain_evidence FROM app.datastream_execution_stage_evidence WHERE id=%s",
            (evidence_id,),
        )
        row = cur.fetchone()
    assert row is not None, "a stage evidence row with no grain did not land"
    assert isinstance(row[0], dict), f"grain_evidence is not an object: {row[0]!r}"

    with pytest.raises(WorkbenchValidationError):
        append_stage_evidence(
            conn,
            org_id=armed["org_id"],
            project_id=armed["project_id"],
            datastream_id=armed["datastream_id"],
            execution_id=armed["execution_id"],
            stage="mapped",
            actor="test",
            plan_version_id=armed["plan_version_id"],
            mapping_version_id=armed["mapping_version_id"],
            evidence={"grain_evidence": ["campaign", "date"]},
        )


@requires_postgres
def test_published_output_version_carries_a_comparable_schema_hash(conn, warehouse):
    """The publication diff could never compare two schemas.

    `app.datastream_output_versions.schema_hash` exists (migration 138) and the
    Outputs tab reads it (`datastream_workbench.py`, `v.schema_hash`) to fill the
    "Schema" line of the candidate-vs-current table. Nothing wrote it: the INSERT
    listed fifteen columns and that was not one of them, although the candidate
    had recorded the fingerprint in `candidate_evidence` at Ready. Both sides of
    the diff read "Unavailable", so the line said "Not comparable" on every
    publication that ever happened -- on the screen that decides a promotion.

    The value must also survive the column's own CHECK (`^[0-9a-f]{64}$`): a
    violation aborts the publish, which is exactly how the `grain_evidence`
    defect above kept the table empty.

    This one composes the publication WITHOUT `_run_import`: the whole arming
    harness is red on `file_source_confirmation_required` since the epic-22
    confirmation contract landed (`e929073f`), and that block is upstream of
    everything measured here. `create_execution` is the same function
    `commit_publication` uses, so the candidate is real; only the file arrival
    is left out.
    """
    from core.datastream_activation import complete_candidate_from_adapter
    from core.datastream_publication import create_execution

    org_id, project_id, fields = _seed(conn)
    template = _make_template(
        conn,
        project_id=project_id,
        org_id=org_id,
        contract=_contract(fields),
        code=f"SCH_{project_id[-6:].upper()}",
    )
    contract = _compile(
        fields=fields,
        org_id=org_id,
        channel="file_upload",
        template_id=template["id"],
        requested_mode="daily",
    )
    ds_id, _ = _insert_datastream(conn, project_id=project_id, org_id=org_id, contract=contract)
    plan_id, mapping_id, projection = _versions(
        conn, ds_id=ds_id, project_id=project_id, contract=contract
    )
    execution = create_execution(
        ds_id,
        project_id,
        plan_id,
        mapping_id,
        projection,
        "owner@example.com",
        _id("idem_"),
        conn,
    )
    conn.commit()
    execution_id = execution["id"]
    adapter = _adapter_result(
        execution_id=execution_id,
        outcome={"landed_row_count": 3, "rejected_count": 0},
        plan_id=plan_id,
        mapping_id=mapping_id,
    )
    ready = complete_candidate_from_adapter(
        conn,
        project_id=project_id,
        datastream_id=ds_id,
        execution_id=execution_id,
        actor="owner@example.com",
        adapter_result=adapter,
    )
    conn.commit()
    _publish_and_activate(
        conn,
        project_id=project_id,
        ds_id=ds_id,
        ready={**ready, **adapter},
        contract=contract,
        plan_id=plan_id,
        mapping_id=mapping_id,
        projection=projection,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT schema_hash FROM app.datastream_output_versions WHERE execution_id=%s",
            (execution_id,),
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT schema_hash FROM app.datastream_execution_stage_evidence "
            "WHERE execution_id=%s AND stage='published'",
            (execution_id,),
        )
        staged = cur.fetchall()
    assert len(rows) == 1, "publication produced no Output version"
    assert rows[0][0] == adapter["schema_hash"], (
        "schema_hash did not survive the publish: the Outputs diff reads 'Not comparable'"
    )
    assert staged and staged[0][0] == rows[0][0], (
        "the published stage evidence and the Output version disagree on the schema hash"
    )


@requires_postgres
def test_a_run_that_lands_before_its_publication_still_carries_the_schema_hash(conn, warehouse):
    """The publication is not the only writer, and after the first one it LOSES.

    The test above proves the FIRST publication of a Datastream carries a
    comparable `schema_hash`. It is the only one that ever did, and this is why.

    `app.datastream_output_versions` is UNIQUE on `(output_id, execution_id)`
    (migration 138) and has two writers, both inserting `ON CONFLICT DO NOTHING`:

      * `publish_activate_mutation`, which computes the hash;
      * `record_run_output_version`, called from `queue.py` when a pull finishes,
        which did not.

    `record_run_output_version` is a no-op until an `app.datastream_outputs` row
    exists, and only a publication creates one -- so on the first publication
    there is no conflict and the hash lands. From then on the order reverses: the
    run of a candidate finishes BEFORE that candidate is published, the run's row
    takes the key, and the publication's INSERT -- hash included -- is silently
    discarded. `trg_datastream_output_versions_immutable` raises on UPDATE, so
    nothing can fill the column afterwards, ever.

    The visible consequence was on the screen that decides a promotion:
    `CandidateReadiness` on the Outputs tab reads `schema_hash` on both sides, the
    candidate side was NULL on every publication after the first, and the "Schema"
    line printed `Not comparable` for the life of the Datastream.

    What is asserted here is the repair and the reason for it, in that order: the
    run's row carries the candidate's own fingerprint, and a later INSERT of the
    publication's exact column list changes nothing.

    THE MODULE AND PROFILE ARE SET ON PURPOSE. `record_run_output_version`
    resolves its relation through `stage_relation_resolver`, which reads the
    connector manifests; the file-source Datastream this harness builds names no
    module, so the writer would record nothing and the test would pass while
    measuring nothing. The pair below is read from a manifest, not invented.

    THE `app.datastream_outputs` ROW IS SEEDED DIRECTLY, and that is deliberate
    rather than a shortcut. The precondition this test needs is "an Output
    identity exists", which is the state a Datastream is in from its first
    publication onwards -- not "a publication succeeded through this harness".
    Going through `_publish_and_activate` would bind this measurement to
    `promote_candidate`'s governed column list, a different subject that is red
    in this file today (`test_published_output_version_carries_a_comparable_schema_hash`
    fails on `The published candidate carries no governed column list`), and a
    test that cannot run is a test that measures nothing. The INSERT below is the
    publication's own, copied from `publish_activate_mutation`.
    """
    import hashlib

    from core.datastream_activation import (
        complete_candidate_from_adapter,
        record_run_output_version,
    )
    from core.datastream_publication import create_execution
    from core.stage_relation_resolver import resolve_stage_relations
    from ulid import ULID

    connector, profile = "cm360", "standard_daily"
    declared = resolve_stage_relations(connector=connector, report_profile_id=profile)
    assert declared.get("collected_relation"), (
        f"{connector}/{profile} no longer declares a raw relation; this test needs a "
        "pair the resolver can answer, otherwise the writer records nothing"
    )

    org_id, project_id, fields = _seed(conn)
    template = _make_template(
        conn,
        project_id=project_id,
        org_id=org_id,
        contract=_contract(fields),
        code=f"PRE_{project_id[-6:].upper()}",
    )
    contract = _compile(
        fields=fields,
        org_id=org_id,
        channel="file_upload",
        template_id=template["id"],
        requested_mode="daily",
    )
    ds_id, _ = _insert_datastream(conn, project_id=project_id, org_id=org_id, contract=contract)
    plan_id, mapping_id, projection = _versions(
        conn, ds_id=ds_id, project_id=project_id, contract=contract
    )

    def _candidate(execution_id):
        adapter = _adapter_result(
            execution_id=execution_id,
            outcome={"landed_row_count": 3, "rejected_count": 0},
            plan_id=plan_id,
            mapping_id=mapping_id,
        )
        ready = complete_candidate_from_adapter(
            conn,
            project_id=project_id,
            datastream_id=ds_id,
            execution_id=execution_id,
            actor="owner@example.com",
            adapter_result=adapter,
        )
        conn.commit()
        return adapter, ready

    # --- the state a Datastream is in from its first publication onwards --------
    # The Output identity exists, so `record_run_output_version` is no longer a
    # no-op. This is the precondition of the defect, and the whole of it.
    output_id = f"dso_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastream_outputs
               (id,org_id,project_id,datastream_id,output_kind,stable_name,created_by)
               VALUES (%s,%s,%s,%s,'full_grain','orders_daily',%s)""",
            (output_id, org_id, project_id, ds_id, "owner@example.com"),
        )
        # `source_kind` moves WITH the module, because `ck_datastreams_source_kind`
        # (migration 030) refuses a `managed_feed` that names one -- and because a
        # nightly run finishing on a connector-pull Datastream is exactly the case
        # this defect lives in. The row stays coherent rather than being bent.
        # The two pointers a publication advances. `record_run_output_version`
        # reads them for the plan/mapping columns of the row it writes, and
        # returns None outright when the mapping pointer is NULL -- which is the
        # honest answer on a Datastream that has never published, and the reason
        # they belong in this seeded post-publication state.
        cur.execute(
            "UPDATE app.datastreams SET source_kind='connector_pull', module_name=%s, "
            "report_profile_id=%s, current_plan_version_id=%s, current_mapping_version_id=%s "
            "WHERE id=%s AND project_id=%s",
            (connector, profile, plan_id, mapping_id, ds_id, project_id),
        )
    conn.commit()

    # --- a run, recorded the way `queue.py` records one -------------------------
    second = create_execution(
        ds_id, project_id, plan_id, mapping_id, projection,
        "owner@example.com", _id("idem_"), conn,
    )
    conn.commit()
    adapter, _ = _candidate(second["id"])
    recorded = record_run_output_version(
        conn,
        project_id=project_id,
        datastream_id=ds_id,
        execution_id=second["id"],
        actor="owner@example.com",
    )
    conn.commit()
    assert recorded == declared["collected_relation"], (
        "the run recorded no Output version, so nothing below is being measured"
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, output_id, schema_hash FROM app.datastream_output_versions "
            "WHERE execution_id=%s",
            (second["id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, "the run wrote no Output version row for its own execution"
    version_id, written_output_id, schema_hash = rows[0]
    assert written_output_id == output_id
    assert schema_hash == adapter["schema_hash"], (
        "the run's Output version carries no schema hash, so the publication that "
        "follows it will be discarded and the Outputs diff will read 'Not comparable' "
        "for the life of this Datastream"
    )

    # --- and the publication that follows really is a no-op ---------------------
    # The exact conflict clause `publish_activate_mutation` uses. If this ever
    # started to write, the repair above would be unnecessary -- and it cannot,
    # because `DO UPDATE` would fire the immutability trigger.
    other = hashlib.sha256(b"a different shape entirely").hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastream_output_versions
               (id,output_id,org_id,project_id,datastream_id,execution_id,
                plan_version_id,mapping_version_id,relation_ref,schema_hash,
                grain_evidence,evidence,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
               ON CONFLICT (output_id,execution_id) DO NOTHING""",
            (
                f"dsov_{ULID()}", output_id, org_id, project_id, ds_id, second["id"],
                plan_id, mapping_id, "irrelevant.relation", other,
                json.dumps({"grain": []}), json.dumps({"recorded_by": "publication"}),
                "owner@example.com",
            ),
        )
        assert cur.rowcount == 0, (
            "the publication's INSERT was NOT discarded; the premise of this repair "
            "no longer holds and the run writer should stop carrying the hash"
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT schema_hash FROM app.datastream_output_versions WHERE id=%s",
            (version_id,),
        )
        assert cur.fetchone()[0] == adapter["schema_hash"], (
            "the row is not immutable after all"
        )


@requires_postgres
def test_the_activation_driver_shape_reuses_the_candidate_it_runs_inside(conn, warehouse):
    """Repaired 2026-08-08, and the guard is inverted rather than deleted.

    This test used to be RED AND LEFT RED: it pinned, as a fact, that the
    managed-feed CANDIDATE driver could not run. `materialize_draft_mutation`
    creates the candidate execution (state `created`), the driver wraps the import
    in `raw_landing.candidate_execution(execution_id)`, and `run_import` opened its
    OWN 042 candidate under a different idempotency key -- so the partial unique
    index `uq_datastream_executions_active` (migration 042) refused the second
    create and the candidate was blocked by ITSELF.

    Of the two shapes the old docstring named, the first was taken: `open_import`
    ADOPTS the execution the ambient scope names when it is genuinely this
    import's (same Datastream, same project, non-terminal, same plan and mapping
    versions). The scope stops being only "where rows land" and becomes what it
    always claimed to be -- the candidate the whole import belongs to.

    Asserted on the EFFECT: the call returns, the ledger row names THAT candidate,
    and the Datastream still has exactly ONE execution.
    """
    from core.datastream_publication import create_execution

    org_id, project_id, fields = _seed(conn)
    template = _make_template(
        conn,
        project_id=project_id,
        org_id=org_id,
        contract=_contract(fields),
        code=f"COL_{project_id[-6:].upper()}",
    )
    contract = _compile(
        fields=fields,
        org_id=org_id,
        channel="file_upload",
        template_id=template["id"],
        requested_mode="manual",
    )
    ds_id, _mode = _insert_datastream(
        conn, project_id=project_id, org_id=org_id, contract=contract
    )
    plan_id, mapping_id, projection = _versions(
        conn, ds_id=ds_id, project_id=project_id, contract=contract
    )
    _confirm_template(
        conn,
        template=template,
        project_id=project_id,
        org_id=org_id,
        ds_id=ds_id,
        fields=fields,
        plan_id=plan_id,
        mapping_id=mapping_id,
    )
    candidate = create_execution(
        ds_id,
        project_id,
        plan_id,
        mapping_id,
        projection,
        "owner@example.com",
        f"{_id('op_')}:candidate",
        conn,
    )
    conn.commit()

    outcome = _run_import(
        conn,
        ds_id=ds_id,
        project_id=project_id,
        plan_id=plan_id,
        mapping_id=mapping_id,
        projection=projection,
        execution_id=candidate["id"],
    )

    assert outcome["ledger"]["execution_id"] == candidate["id"], (
        "the import minted a second candidate instead of adopting the one whose "
        "isolation it was running inside"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.datastream_executions WHERE datastream_id=%s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


# ===========================================================================
# 3. THE NIGHTLY LOOP -- run it for real and name the false condition.
# ===========================================================================


@requires_postgres
def test_the_nightly_loop_never_enqueues_a_managed_feed_even_fully_armed(conn, warehouse):
    """The measurement AI-75 asked for, and it does NOT stop at "zero jobs".

    The Datastream is armed past what the product allows: `schedule_mode` is
    forced to `'nightly'` by direct SQL, so the four conditions AI-75 names
    (active, nightly, enabled, mapping version) are ALL true. It is still not
    enqueued, and the harness names why: `_dispatch_nightly_datastreams` skips
    every row whose `connection_ref_id` is NULL, and
    `materialize_draft_mutation` writes NULL there for every source kind that is
    not `connector_pull` (`datastream_activation.py:734`).

    So a managed_feed Datastream is unreachable by the nightly loop by
    construction -- not "not yet configured".
    """
    armed = _arm_file_datastream(conn)
    ds_id, project_id = armed["datastream_id"], armed["project_id"]

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET schedule_mode='nightly' WHERE id=%s", (ds_id,)
        )
    conn.commit()

    conditions = _loop_conditions(conn, ds_id, project_id)
    # AI-75's four, all true now.
    for name in (
        "lifecycle_active", "schedule_mode_once_a_period", "enabled", "mapping_version_joins"
    ):
        assert conditions[name] is True, f"{name} is still false: {conditions}"
    # And the ones AI-75 does not name.
    assert conditions["plan_version_joins"] is True
    assert conditions["schedule_state_joins"] is True
    assert conditions["has_connection_ref"] is False, (
        "a managed_feed Datastream carries no connection_ref_id -- "
        "materialize_draft_mutation only writes one for connector_pull"
    )

    queue, jobs, count = _dispatch(os.environ["TEST_POSTGRES_DSN"])
    mine = [job for job in queue.calls if job.get("datastream_id") == ds_id]
    assert mine == [], (
        "a managed_feed Datastream was enqueued for a connector pull; there is "
        "nothing to pull, and enqueue_pull's first argument is a connection id."
    )


@requires_postgres
def test_the_same_loop_does_enqueue_when_the_row_carries_a_connection(conn, warehouse):
    """The control. Without it, the test above only says "it did not happen".

    Same arming, same database, same dispatch call -- the ONLY difference is a
    `connection_ref` row and a `connector_pull` source kind. If this enqueues,
    the loop is not broken and the previous test's zero has exactly one cause.

    The connection carries NO credential: `enqueue_pull` writes a job row and
    never calls a provider (AD-12), so the enqueue is provable offline. What
    happens to that job afterwards is AI-96's subject, not this file's.
    """
    armed = _arm_file_datastream(conn)
    ds_id, project_id = armed["datastream_id"], armed["project_id"]

    conn_ref_id = _id("conn_")
    with conn.cursor() as cur:
        cur.execute(
            # `auth_path='google_direct'` -- NOT 'nango', which would demand a
            # `nango_connection_id` (ck_connection_ref_nango_id_required, migration
            # 128). The two are the only values chk_connection_ref_auth_path allows.
            # No credential of any kind is written: `encrypted_token_blob` stays NULL,
            # the loop only reads `status` and `enabled`, and `enqueue_pull` never
            # calls a provider (AD-12).
            "INSERT INTO app.connection_ref"
            " (id, provider, project_id, status, enabled, auth_path,"
            "  owner_org_id, owner_identity)"
            " VALUES (%s, 'example_module', %s, 'active', TRUE, 'google_direct',"
            "         %s, 'owner@example.com')",
            (conn_ref_id, project_id, armed["org_id"]),
        )
        cur.execute(
            "UPDATE app.datastreams SET schedule_mode='nightly', source_kind='connector_pull',"
            "       connection_ref_id=%s, module_name='example_module' WHERE id=%s",
            (conn_ref_id, ds_id),
        )
    conn.commit()

    conditions = _loop_conditions(conn, ds_id, project_id)
    assert all(conditions.values()), f"a condition is still false: {conditions}"

    queue, jobs, count = _dispatch(os.environ["TEST_POSTGRES_DSN"])
    mine = [job for job in queue.calls if job.get("datastream_id") == ds_id]
    assert mine, (
        "the nightly loop did not enqueue a Datastream for which every one of its "
        f"conditions is true: {conditions}"
    )
    assert mine[0]["connection_ref_id"] == conn_ref_id
    # The window is a DATE window (the date-grain invariant), anchored on yesterday.
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert mine[0]["date_to"] == yesterday


@requires_postgres
def test_next_run_at_is_written_by_activation_and_read_by_no_dispatcher(conn, warehouse):
    """`datastream_schedule_state.next_run_at` does not gate the nightly loop.

    Measured, because "next_run_at IS NULL everywhere" reads like the cause of the
    silence and is not: `_dispatch_nightly_datastreams` joins
    `datastream_schedule_state` but never FILTERS on `next_run_at`. The loop is a
    wall-clock trigger that dispatches EVERY matching row; `next_run_at` is
    posture shown in the Workbench.

    The consequence is worth stating plainly: `missed_run_count = 0` in production
    is not health, and neither is a NULL `next_run_at` a scheduling verdict.

    « ET NE L'AVANCE PAS » ETAIT FAUX, et cette ligne n'avait jamais tourne pour
    le dire : jusqu'au 2026-08-05 le test mourait bien plus haut, sur le refus de
    confirmation d'epic 22 (AI-187). Le semage de la confirmation l'a fait
    descendre jusqu'ici, et l'avance existe -- `scheduler.py`,
    `_advance_next_run`, qui traite explicitement le cas NULL
    (`COALESCE(ss.next_run_at, NOW())`) et deplace la ligne d'un jour. C'est le
    comportement voulu et non un defaut : un declencheur qui n'avancerait jamais
    ce champ redispatcherait la meme ligne a chaque tick.

    L'assertion porte donc sur ce que la boucle garantit vraiment -- NULL
    n'empeche pas le dispatch, ET le dispatch deplace la ligne -- ce qui est plus
    fort que l'immobilite qu'elle affirmait.
    """
    armed = _arm_file_datastream(conn)
    ds_id, project_id = armed["datastream_id"], armed["project_id"]

    conn_ref_id = _id("conn_")
    with conn.cursor() as cur:
        cur.execute(
            # `auth_path='google_direct'` -- NOT 'nango', which would demand a
            # `nango_connection_id` (ck_connection_ref_nango_id_required, migration
            # 128). The two are the only values chk_connection_ref_auth_path allows.
            # No credential of any kind is written: `encrypted_token_blob` stays NULL,
            # the loop only reads `status` and `enabled`, and `enqueue_pull` never
            # calls a provider (AD-12).
            "INSERT INTO app.connection_ref"
            " (id, provider, project_id, status, enabled, auth_path,"
            "  owner_org_id, owner_identity)"
            " VALUES (%s, 'example_module', %s, 'active', TRUE, 'google_direct',"
            "         %s, 'owner@example.com')",
            (conn_ref_id, project_id, armed["org_id"]),
        )
        cur.execute(
            "UPDATE app.datastreams SET schedule_mode='nightly', source_kind='connector_pull',"
            "       connection_ref_id=%s, module_name='example_module' WHERE id=%s",
            (conn_ref_id, ds_id),
        )
        # Explicitly NULL -- the production state AI-75 measured on all 42 rows.
        cur.execute(
            "UPDATE app.datastream_schedule_state SET next_run_at=NULL WHERE datastream_id=%s",
            (ds_id,),
        )
    conn.commit()

    queue, _jobs, _count = _dispatch(os.environ["TEST_POSTGRES_DSN"])
    mine = [job for job in queue.calls if job.get("datastream_id") == ds_id]
    assert mine, (
        "a NULL next_run_at suppressed the dispatch -- if this ever fails, the "
        "loop grew a due-date filter and this docstring is out of date."
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT next_run_at, missed_run_count FROM app.datastream_schedule_state"
            " WHERE datastream_id=%s AND plan_version_id=%s",
            (ds_id, armed["plan_version_id"]),
        )
        next_run_at, missed = cur.fetchone()
    # NULL n'a pas supprime le dispatch (asserte plus haut), et le dispatch a
    # bien deplace la ligne : sans cela, le meme Datastream repartirait a chaque
    # tick. Borne large a dessein -- le pas est calcule dans le fuseau du projet
    # (AI-117), donc l'instant exact depend de ce fuseau, jamais le fait.
    assert next_run_at is not None, (
        "le dispatch n'a pas avance next_run_at : la meme ligne se redispatchera "
        "a chaque tick"
    )
    assert next_run_at > datetime.now(timezone.utc), (
        f"next_run_at a ete avance dans le passe ({next_run_at})"
    )
    assert missed == 0, (
        "missed_run_count is still 0 after a real dispatch -- a zero here has "
        "never meant 'no run was missed'."
    )


# ===========================================================================
# NOT COVERED -- stated, because an unstated boundary reads as coverage.
#
#  * THE WIZARD BOOKKEEPING. This harness does not walk
#    draft -> revision -> proposal -> observation -> preview -> final review ->
#    `materialize_draft_mutation`. It builds the Datastream row with the exact
#    SQL and the exact source-kind gating `materialize_draft_mutation` uses, and
#    it runs the REAL `compile_materialization_contract`, `save_datastream_intent`,
#    `save_field_mapping`, `compile_projection`, `create_execution`,
#    `complete_candidate_from_adapter`, `build_candidate_review` and
#    `publish_activate_mutation`. What is skipped is the immutable setup evidence
#    (previews, final reviews, materialization rows), which is covered by the
#    Story 47.4 seams. Nothing skipped here influences the scheduler's conditions.
#
#  * THE STAGED-ASSET LOOKUP. `managed_feed_candidate` resolves its bytes from a
#    quarantined `app.datastream_setup_assets` row. The harness hands the bytes
#    directly to the same `run_import` inside the same
#    `raw_landing.candidate_execution`. Asset quarantine is Inbound's surface.
#
#  * WHAT THE ENQUEUED JOB DOES. `enqueue_pull` is recorded, not executed. The
#    dispatch contract is "it only enqueues" (AD-12); whether the pull then
#    succeeds is AI-96's subject and needs a live account.
#
#  * THE DAEMON THREAD AND ITS CLOCK. `_scheduler_loop` fires on wall-clock
#    hour:minute with SCHEDULER_ENABLED, and the harness calls the dispatch
#    function directly. Nothing here proves the thread starts in production.
#
#  * GOOGLE SHEETS RECURRENCE. `google_sheets` is the ONE managed-feed channel
#    `compile_materialization_contract` lets keep a cadence. It is not exercised:
#    it needs an OAuth consent. What CAN be said without one is recorded as a
#    finding, not a test: activation writes the cadence to
#    `app.datastreams.schedule_mode`, while the only code that dispatches a
#    managed feed (`scheduler._run_managed_feed_syncs`) reads a DIFFERENT table,
#    `app.managed_feed_sync_schedule`, whose sole writer is an endpoint in
#    `admin_api.py`. Activation never writes it.
#
#  * HOW A FILE DATASTREAM SHOULD RECUR. Deliberately NOT decided here. The
#    ratified contract gives a one-off upload `Manual` and an email feed an
#    arrival monitor; nothing in `docs/product-architecture/` says what dispatches
#    a recurring file feed, nor what reads
#    `app.datastream_arrival_monitors`. Choosing one would be inventing a design
#    decision and presenting it as a repair.
# ===========================================================================
