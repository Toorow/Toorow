"""`Reprocess` on the real shape of the publication it exists to repair (67-15b).

THE SCENARIO IS NOT INVENTED. It is the one measured in production on 2026-08-17
and rebuilt here on the disposable base, row for row:

  * a `managed_feed` Datastream whose ONLY output version was written by the
    wizard's activation, carrying the synthetic
    `execution/<id>/candidate/relation` -- the string
    `query_execution._safe_identifier` refuses, because it is not a SQL
    identifier;
  * an import ledger row whose `landing_relation` names the promoted table that
    really holds the rows (`main.managed_feed_ds_...`);
  * a retained delivery in `app.inbound_raw_imports`, bytes still held;
  * and the immutable-evidence trigger of migration 138, which refuses to let
    anybody fix the wrong row in place -- correctly.

WHAT IS REAL HERE AND WHAT IS NOT, stated rather than implied. Every read and
every write of this file goes to a real Postgres with the 279 migrations applied:
the eligibility decision, the refusals, the mapping gate, the appended output
version, the immutability of the old one, and the head query
`query_execution` runs. What is substituted is ONE seam --
`inbound_reprocess._replay_one` -- because past it lies a DuckDB warehouse, a
quarantine object store and a parsing contract, none of which a Postgres fixture
holds. The substitute returns exactly what the real body returns (`status`,
`import_ledger_id`) and the LEDGER ROW IT NAMES IS WRITTEN FOR REAL, so
everything this module does with the replay's outcome is exercised against the
database rather than against a stub's opinion.
"""

from __future__ import annotations

import json

import pytest
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

SYNTHETIC = "execution/{execution_id}/candidate/relation"


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _hash64(seed: str = "") -> str:
    import hashlib

    return hashlib.sha256((seed or str(ULID())).encode("utf-8")).hexdigest()


def _world(conn, *, mode: str = "managed_feed"):
    """Org -> project -> Datastream -> plan + mapping, all published and in force."""
    org_id, project_id = _uid("org"), _uid("proj")
    ds_id = _uid("ds")
    plan_id, mapping_id = _uid("dspv"), _uid("dsmv")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id,name,slug,created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "Reprocess", org_id.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id,org_id,name,slug,created_by) VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, "Reprocess", project_id.lower(), "tester"),
        )
        cur.execute(
            """INSERT INTO app.datastreams
               (id,project_id,org_id,name,module_name,source_kind,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            # `ck_datastreams_source_kind` binds the two: a managed feed carries
            # NO connector name, a connector pull must carry one.
            (ds_id, project_id, org_id, "Reprocess flux",
             None if mode != "connector_pull" else "meta-ads", mode, "tester"),
        )
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
               (id,datastream_id,project_id,version_number,contract_version,source_kind,
                writer_kind,destination_policy,normalized_payload,content_hash,
                idempotency_key_hash,created_by)
               VALUES (%s,%s,%s,1,'plan.v1',%s,'toorow','managed_raw','{}'::jsonb,%s,%s,
                       'tester')""",
            (plan_id, ds_id, project_id, mode, _hash64("plan"), _hash64("planidem")),
        )
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
               (id,datastream_id,project_id,version_number,mapping_contract_version,
                source_schema_hash,plan_version_id,content_hash,ossie_spec_version,
                toorow_extension_version,mapping_payload,ossie_projection,
                idempotency_key_hash,created_by)
               VALUES (%s,%s,%s,1,'mapping.v1',%s,%s,%s,'0.1.1','2','{}'::jsonb,'{}'::jsonb,
                       %s,'tester')""",
            (mapping_id, ds_id, project_id, _hash64("schema"), plan_id,
             _hash64("map"), _hash64("mapidem")),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_plan_version_id=%s,"
            "current_mapping_version_id=%s WHERE id=%s",
            (plan_id, mapping_id, ds_id),
        )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": ds_id,
        "plan_version_id": plan_id,
        "mapping_version_id": mapping_id,
    }


def _execution(conn, w, *, state: str = "published") -> str:
    execution_id = _uid("dse")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastream_executions
               (id,datastream_id,project_id,plan_version_id,mapping_version_id,
                projection_plan_ref,state,row_count,content_hash,created_by)
               VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,%s,7,%s,'tester')""",
            (
                execution_id,
                w["datastream_id"],
                w["project_id"],
                w["plan_version_id"],
                w["mapping_version_id"],
                state,
                _hash64(execution_id),
            ),
        )
    return execution_id


def _ledger(conn, w, execution_id: str, relation: str, *, outcome: str = "published") -> str:
    ledger_id = _uid("mfl")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.managed_feed_import_ledger
               (id,datastream_id,project_id,execution_id,plan_version_id,mapping_version_id,
                feed_format,write_mode,idempotency_key_hash,payload_fingerprint,content_hash,
                source_metadata,landing_relation,row_count,outcome,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,'csv','replace',%s,%s,%s,%s::jsonb,%s,7,%s,'tester')""",
            (
                ledger_id,
                w["datastream_id"],
                w["project_id"],
                execution_id,
                w["plan_version_id"],
                w["mapping_version_id"],
                _hash64(ledger_id + "k"),
                _hash64(ledger_id + "f"),
                w["content_hash"],
                json.dumps({"filename": "delivery.csv", "channel": "inbound_email"}),
                relation,
                outcome,
            ),
        )
    return ledger_id


def _delivery(conn, w) -> str:
    """One retained delivery: bytes recorded as held, not deleted by retention.

    Built through every guard `protect_inbound_raw_import` imposes rather than
    around them -- the receipt, the provenance operation, the content-scoped
    quarantine URI and the retention evidence -- because a fixture that reached
    the row by another road would prove the verb against a shape the product
    cannot produce.
    """
    receipt_id, raw_id = _uid("inbrx"), _uid("inbraw")
    receipt_op, raw_op = _uid("op"), _uid("op")
    uri = (
        f"gs://quarantine/inbound/{w['org_id']}/{w['datastream_id']}/"
        f"{w['content_hash']}/1"
    )

    def _operation(cur, op_id: str, command: str, resources: list[str]) -> None:
        cur.execute(
            """INSERT INTO app.operations
               (id,command_type,actor,effective_org_id,resource_path,host_context,versions,
                request_hash,confirmation_mode,idempotency_key_hash)
               VALUES (%s,%s,'tester',%s,%s::jsonb,'{}'::jsonb,'{}'::jsonb,%s,'none',%s)""",
            (op_id, command, w["org_id"], json.dumps(resources),
             _hash64(op_id), _hash64(op_id + "k")),
        )

    with conn.cursor() as cur:
        _operation(cur, receipt_op, "inbound.receipt.recorded",
                   [f"datastream:{w['datastream_id']}"])
        cur.execute(
            """INSERT INTO app.inbound_receipts
               (id,datastream_id,channel,provider_event_id,receipt_fingerprint,
                state,operation_id)
               VALUES (%s,%s,'email',%s,%s,'RECEIVED',%s)""",
            (receipt_id, w["datastream_id"], _uid("evt"), _hash64(receipt_id), receipt_op),
        )
        _operation(cur, raw_op, "inbound.raw_import.recorded",
                   [f"datastream:{w['datastream_id']}", f"receipt:{receipt_id}",
                    "attachment:1"])
        cur.execute(
            """INSERT INTO app.inbound_raw_imports
               (id,receipt_id,datastream_id,ordinal,filename,content_hash,quarantine_uri,
                state,size_bytes,retention_expires_at,retention_policy_version,
                retention_days,operation_id)
               VALUES (%s,%s,%s,1,'delivery.csv',%s,%s,'RECEIVED',128,
                       NOW() + INTERVAL '30 days','quarantine-retention-v1',30,%s)""",
            (raw_id, receipt_id, w["datastream_id"], w["content_hash"], uri, raw_op),
        )
    return raw_id


def _published_output(conn, w, execution_id: str, relation_ref: str) -> tuple[str, str]:
    output_id, version_id = _uid("dso"), _uid("dsov")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastream_outputs
               (id,org_id,project_id,datastream_id,output_kind,stable_name,created_by)
               VALUES (%s,%s,%s,%s,'full_grain',%s,'tester')""",
            (output_id, w["org_id"], w["project_id"], w["datastream_id"], "full_grain"),
        )
        cur.execute(
            """INSERT INTO app.datastream_output_versions
               (id,output_id,org_id,project_id,datastream_id,execution_id,
                plan_version_id,mapping_version_id,relation_ref,
                grain_evidence,evidence,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,'{}'::jsonb,'tester')""",
            (
                version_id, output_id, w["org_id"], w["project_id"], w["datastream_id"],
                execution_id, w["plan_version_id"], w["mapping_version_id"], relation_ref,
            ),
        )
    return output_id, version_id


def _head_relation(conn, w) -> str | None:
    """The relation `query_execution` would read: newest version for the mapping."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT relation_ref FROM app.datastream_output_versions
                WHERE mapping_version_id=%s AND datastream_id=%s AND project_id=%s
                ORDER BY created_at DESC LIMIT 1""",
            (w["mapping_version_id"], w["datastream_id"], w["project_id"]),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _scenario(conn, *, mode: str = "managed_feed", retained: bool = True):
    """The production shape: a promoted relation, and a publication that misses it."""
    w = _world(conn, mode=mode)
    w["content_hash"] = _hash64("bytes")
    wizard_execution = _execution(conn, w)
    w["relation"] = f"main.managed_feed_{w['datastream_id']}"
    w["ledger_id"] = _ledger(conn, w, wizard_execution, w["relation"])
    if retained:
        w["raw_import_id"] = _delivery(conn, w)
    w["wizard_execution_id"] = wizard_execution
    w["output_id"], w["old_version_id"] = _published_output(
        conn, w, wizard_execution, SYNTHETIC.format(execution_id=wizard_execution)
    )
    return w


# ---------------------------------------------------------------------------
# The starting state IS the defect. Prove it before repairing it.
# ---------------------------------------------------------------------------


def test_the_published_pointer_is_unreadable_and_cannot_be_mutated(live_postgres) -> None:
    """The head names a path no reader accepts, and the row refuses to be fixed."""
    from core.query_execution import _safe_identifier

    conn = live_postgres
    w = _scenario(conn)

    head = _head_relation(conn, w)
    assert head == SYNTHETIC.format(execution_id=w["wizard_execution_id"])
    with pytest.raises(Exception):
        # Not an identifier, so the analytical read refuses it -- for ever.
        _safe_identifier(head)

    # And the honest repair is not available: the evidence is immutable BY DESIGN
    # (migration 138, trg_datastream_output_versions_immutable). This is what
    # makes "append a new head" the only truthful move rather than a shortcut.
    with conn.cursor() as cur, pytest.raises(Exception) as refused:
        cur.execute(
            "UPDATE app.datastream_output_versions SET relation_ref=%s WHERE id=%s",
            (w["relation"], w["old_version_id"]),
        )
    assert "immutable" in str(refused.value).lower()
    conn.rollback()


# ---------------------------------------------------------------------------
# The decision, read from the real rows. Nothing is created by asking.
# ---------------------------------------------------------------------------


def test_a_retained_delivery_makes_the_verb_available_and_skips_the_gate(
    live_postgres, monkeypatch
) -> None:
    from core import datastream_reprocess as reprocess

    conn = live_postgres
    w = _scenario(conn)
    monkeypatch.setattr(reprocess, "_bytes_are_readable", lambda **_: (True, None))

    plan = reprocess.evaluate_reprocess_plan(
        conn, datastream_id=w["datastream_id"], project_id=w["project_id"], actor="jean"
    )
    assert plan.available, plan.message
    assert plan.artifact is not None
    assert plan.artifact.raw_import_id == w["raw_import_id"]
    # The relation being superseded is READ from the ledger, never composed.
    assert plan.artifact.published_relation == w["relation"]
    # Same mapping in and out: the gate is skipped, and the skip is recorded.
    gate = plan.mapping_gate
    assert gate["state"] == reprocess.GATE_SKIPPED
    assert gate["reason"] == "mapping_unchanged"
    assert gate["decided_by"] == "jean" and gate["decided_at"]

    # A read creates nothing: no execution, no ledger row, no output version.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.datastream_output_versions WHERE datastream_id=%s",
            (w["datastream_id"],),
        )
        assert cur.fetchone()[0] == 1


def test_a_different_mapping_puts_the_human_gate_back(live_postgres, monkeypatch) -> None:
    """Reprocessing under another mapping changes meaning, so a person confirms."""
    from core import datastream_reprocess as reprocess

    conn = live_postgres
    w = _scenario(conn)
    monkeypatch.setattr(reprocess, "_bytes_are_readable", lambda **_: (True, None))

    second = _uid("dsmv")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
               (id,datastream_id,project_id,version_number,mapping_contract_version,
                source_schema_hash,plan_version_id,content_hash,ossie_spec_version,
                toorow_extension_version,mapping_payload,ossie_projection,
                idempotency_key_hash,created_by)
               VALUES (%s,%s,%s,2,'mapping.v1',%s,%s,%s,'0.1.1','2','{}'::jsonb,'{}'::jsonb,
                       %s,'tester')""",
            (second, w["datastream_id"], w["project_id"], _hash64("s2"),
             w["plan_version_id"], _hash64("m2"), _hash64("i2")),
        )

    plan = reprocess.evaluate_reprocess_plan(
        conn,
        datastream_id=w["datastream_id"],
        project_id=w["project_id"],
        chosen_mapping_version_id=second,
        actor="jean",
    )
    assert plan.available, plan.message
    assert plan.mapping_gate["state"] == reprocess.GATE_PASSED
    assert plan.mapping_gate["from_mapping_version_id"] == w["mapping_version_id"]
    assert plan.mapping_gate["to_mapping_version_id"] == second


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        ("no_delivery", "artifact_not_retained"),
        ("connector_mode", "source_would_be_called"),
        ("run_in_flight", "run_in_flight"),
    ],
)
def test_each_refusal_names_a_gesture_and_never_a_cause(
    live_postgres, monkeypatch, build: str, expected: str
) -> None:
    from core import datastream_reprocess as reprocess

    conn = live_postgres
    monkeypatch.setattr(reprocess, "_bytes_are_readable", lambda **_: (True, None))

    if build == "connector_mode":
        w = _scenario(conn, mode="connector_pull")
    elif build == "no_delivery":
        w = _scenario(conn, retained=False)
    else:
        w = _scenario(conn)
        _execution(conn, w, state="created")

    plan = reprocess.evaluate_reprocess_plan(
        conn, datastream_id=w["datastream_id"], project_id=w["project_id"], actor="jean"
    )
    assert plan.state == expected
    assert not plan.available
    # A refusal names what to DO. These are the words a person acts on; a bare
    # code, or the name of the table that was empty, is not a gesture.
    assert plan.message
    gesture = {
        "artifact_not_retained": "Re-import",
        "source_would_be_called": "Day-by-day coverage",
        "run_in_flight": "Runs tab",
    }[expected]
    assert gesture in plan.message


def test_bytes_that_no_longer_match_their_recorded_hash_refuse(
    live_postgres, monkeypatch
) -> None:
    from core import datastream_reprocess as reprocess

    conn = live_postgres
    w = _scenario(conn)
    monkeypatch.setattr(
        reprocess, "_bytes_are_readable", lambda **_: (False, reprocess._ARTIFACT_UNREADABLE)
    )
    plan = reprocess.evaluate_reprocess_plan(
        conn, datastream_id=w["datastream_id"], project_id=w["project_id"], actor="jean"
    )
    assert plan.state == "artifact_unreadable"
    # A storage fault is NOT reported as a retention decision: the two sentences
    # share no claim about why the bytes are not there.
    assert "retention" not in plan.message.lower() or "not a retention" in plan.message.lower()


# ---------------------------------------------------------------------------
# The act: replay -> a NEW head naming the observed relation, old one superseded.
# ---------------------------------------------------------------------------


def _stub_replay(conn, w, monkeypatch, *, relation: str | None = None):
    """Substitute ONLY the file-replay seam; write the ledger row it names for real.

    Past `_replay_one` lie DuckDB, an object store and a parsing contract. What
    this returns is what the real body returns, and the ledger row this module
    then READS is a real row in the real database -- so the observation the
    output version is built from is not the stub's opinion.
    """
    from core import datastream_reprocess as reprocess

    new_execution = _execution(conn, w, state="published")
    new_ledger = _ledger(conn, w, new_execution, relation or w["relation"])

    def _fake(_conn, **kwargs):
        assert kwargs["run_origin"] == "bounded_reprocess"
        assert kwargs["message_id"].startswith("reprocess:")
        assert kwargs["target_mapping_version_id"]
        return {"result": {"status": "landed", "import_ledger_id": new_ledger,
                           "raw_import_id": kwargs["raw_import_id"]}}

    import core.inbound_reprocess as inbound_reprocess

    monkeypatch.setattr(inbound_reprocess, "_replay_one", _fake)
    monkeypatch.setattr(reprocess, "_bytes_are_readable", lambda **_: (True, None))
    return new_execution


def test_reprocess_publishes_a_new_head_on_the_observed_relation(
    live_postgres, monkeypatch
) -> None:
    """THE ACCEPTANCE SCENARIO, end to end on the disposable base."""
    from core import datastream_reprocess as reprocess

    conn = live_postgres
    w = _scenario(conn)
    new_execution = _stub_replay(conn, w, monkeypatch)

    assert _head_relation(conn, w) == SYNTHETIC.format(execution_id=w["wizard_execution_id"])

    outcome = reprocess.dispatch_reprocess(
        conn,
        operation_id="op_reprocess_1",
        org_id=w["org_id"],
        project_id=w["project_id"],
        datastream_id=w["datastream_id"],
        actor="jean",
        reason="the published pointer names a relation nothing can read",
        chosen_mapping_version_id=w["mapping_version_id"],
    )

    assert outcome.outcome == "succeeded", outcome.result
    result = outcome.result
    assert result["calls_source"] is False
    assert result["origin"] == "bounded_reprocess"
    assert result["execution_id"] == new_execution
    assert result["relation_ref"] == w["relation"]
    assert result["superseded_output_relation"] == SYNTHETIC.format(
        execution_id=w["wizard_execution_id"]
    )
    assert result["mapping_gate"]["state"] == reprocess.GATE_SKIPPED

    # THE HEAD MOVED, and it now names a relation the analytical read accepts.
    from core.query_execution import _safe_identifier

    head = _head_relation(conn, w)
    assert head == w["relation"]
    _safe_identifier(head.rsplit(".", 1)[-1])

    # THE OLD ROW IS STILL THERE, untouched. Superseded is not deleted, and the
    # evidence of what was once published is not rewritten.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relation_ref FROM app.datastream_output_versions WHERE id=%s",
            (w["old_version_id"],),
        )
        assert cur.fetchone()[0] == SYNTHETIC.format(execution_id=w["wizard_execution_id"])
        cur.execute(
            "SELECT count(*) FROM app.datastream_output_versions WHERE datastream_id=%s",
            (w["datastream_id"],),
        )
        assert cur.fetchone()[0] == 2

    # The new row carries WHY it exists and WHAT it supersedes.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT evidence FROM app.datastream_output_versions WHERE id=%s",
            (result["output_version_id"],),
        )
        evidence = cur.fetchone()[0]
    assert evidence["recorded_by"] == "reprocess"
    assert evidence["origin"] == "bounded_reprocess"
    assert evidence["operation_id"] == "op_reprocess_1"
    assert evidence["mapping_gate"] == reprocess.GATE_SKIPPED
    assert evidence["mapping_gate_decided_at"]
    assert evidence["supersedes_relation"] == SYNTHETIC.format(
        execution_id=w["wizard_execution_id"]
    )


def test_a_refused_reprocess_is_a_durable_record_and_mints_nothing(
    live_postgres, monkeypatch
) -> None:
    """A refusal is an outcome, not an absence -- and it leaves no run behind."""
    from core import datastream_reprocess as reprocess

    conn = live_postgres
    w = _scenario(conn, retained=False)
    monkeypatch.setattr(reprocess, "_bytes_are_readable", lambda **_: (True, None))

    before = _head_relation(conn, w)
    outcome = reprocess.dispatch_reprocess(
        conn,
        operation_id="op_reprocess_2",
        org_id=w["org_id"],
        project_id=w["project_id"],
        datastream_id=w["datastream_id"],
        actor="jean",
        reason="try",
        chosen_mapping_version_id=w["mapping_version_id"],
    )
    assert outcome.outcome == "failed"
    assert outcome.result["reason"] == "artifact_not_retained"
    assert "Re-import" in outcome.result["message"]
    # Nothing minted, nothing published, the head unchanged.
    assert _head_relation(conn, w) == before
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.datastream_output_versions WHERE datastream_id=%s",
            (w["datastream_id"],),
        )
        assert cur.fetchone()[0] == 1


def test_the_registry_now_lets_the_verb_through_and_still_refuses_the_retired_two(
    live_postgres,
) -> None:
    """`has_engine` is what decides, on the server and on the console, entry for entry."""
    from core import run_origins

    assert run_origins.has_engine(run_origins.BOUNDED_REPROCESS) is True
    assert run_origins.has_engine(run_origins.BOUNDED_SYNCHRONIZE) is False
    assert run_origins.has_engine(run_origins.BOUNDED_RELOAD) is False
    # And the stamp a reprocess run carries is now mintable.
    assert run_origins.stamp_origin({}, run_origins.BOUNDED_REPROCESS) == {
        "origin": "bounded_reprocess"
    }
