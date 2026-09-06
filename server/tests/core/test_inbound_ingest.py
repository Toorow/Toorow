"""Offline unit tests for inbound_ingest.py (ingress parity keystone).

These run WITHOUT Postgres. A fake connection/cursor returns exactly the rows the
module's read helpers issue (datastream row, latest execution projection, active
parsing contract, project preference), and ``core.csv_excel_import.run_import`` is
monkeypatched so we can assert the inbound worker resolves and forwards the SAME
arguments a direct upload would -- proving ingress parity without a live warehouse.

Coverage map:
  PARITY (happy path)
    test_happy_path_calls_run_import_with_resolved_args
    test_result_is_augmented_with_provenance_keys

  FAIL-CLOSED PRECONDITIONS (no run_import call)
    test_channel_not_enabled_raises
    test_not_managed_feed_raises
    test_missing_plan_or_mapping_raises
    test_not_found_raises
    test_disabled_raises
    test_no_projection_recorded_raises
    test_empty_bytes_raises
    test_blank_channel_raises
    test_blank_message_id_raises

  PARSING-CONTRACT FALLBACK
    test_contract_fallback_when_no_active_contract

  IDEMPOTENCY
    test_idempotency_key_derivation_from_message_id
    test_source_metadata_shape

The live DB invariants (real projection_plan_ref read, real contract table read,
end-to-end ledger write) are proven against Postgres in the pg-gated integration
suite; this module owns the pure orchestration + resolution logic.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core.inbound_ingest import (  # noqa: E402
    DatastreamNotIngestable,
    InboundContractReviewRequired,
    InboundIngestValidationError,
    ingest_inbound_file,
)

from tests.support.statement_router import (  # noqa: E402
    StatementInventory,
    UnknownStatement,
    describe,
)

# ---------------------------------------------------------------------------
# Fake connection / cursor.
# ---------------------------------------------------------------------------


# EVERY STATEMENT THE INBOUND PATH ISSUES, NAMED ONCE (AI-317). The `if/elif`
# this replaces ended in `else: self._current = None`, and `None` is not a
# refusal here -- it is the exact answer three helpers read as a fact:
# `_load_ingestable_datastream` reads it as "no such Datastream",
# `_fetch_projection_plan` as "never imported", and
# `resolve_file_source_producer` as "this Datastream carries no file-source
# binding, run the ordinary CSV path". A statement no branch recognised was
# therefore ANSWERED, plausibly, and every assertion downstream was about a
# branch the product never took.
#
# TWO CALLERS EVEN SWALLOW THE REFUSAL if it is not taught: `_catalog_template_
# producer` wraps `get_template` in `except Exception` (file_source_resolution.
# py:361) and `_read_allow_empty_publication` wraps its read the same way
# (inbound_ingest.py:986). `UnknownStatement` is an `AssertionError`, hence an
# `Exception` -- so those two reads must be IN the inventory rather than left to
# the raise, or the silence comes back one frame deeper.
#
# Declaration order is the order an `if/elif` would test in: first match wins.
# The two reads of `app.datastreams` are told apart by their PROJECTION, not by
# their relation, because both name the same table.
_STATEMENTS = StatementInventory(
    "test_inbound_ingest._FakeCursor",
    # inbound_ingest.py:118 -- `_load_ingestable_datastream`: the five columns
    # every unattended precondition is asserted from.
    ingestable_datastream="select source_kind, enabled, config",
    # file_source_resolution.py:442 -- `resolve_file_source_producer` asks the
    # SAME table for ONE column and reads `row[0]` as a JSON document. NEVER
    # MODELLED APART: it shared the branch above and was handed the five-tuple,
    # whose `row[0]` is the string 'managed_feed'. `_as_dict` cannot parse that,
    # so the resolver returned None on every test in this file -- no catalog
    # Template was ever looked up, and the branch below could not be reached.
    datastream_config="select config from app.datastreams",
    # inbound_ingest.py:166 -- `_fetch_projection_plan`: the last executable
    # projection recorded for the pinned plan version.
    projection_plan="from app.datastream_executions",
    # inbound_ingest.py:281 -- `_fetch_mapping_bundle`: the pinned mapping AND
    # its plan version in one scoped read, with the fingerprint evidence.
    mapping_bundle=(
        "from app.datastream_mapping_versions m",
        "join app.datastream_plan_versions p",
    ),
    # inbound_ingest.py:217 -- `_fetch_active_parsing_contract`: the sole active
    # contract and, in the same snapshot, how many are active.
    parsing_contract="from app.csv_excel_import_contracts",
    # import_templates.py:109 -- `get_template`, reached from
    # `_catalog_template_producer` when the Datastream config pins
    # `{template_code, template_version}`. The old fake had a branch for this
    # table that nothing could reach, because the read above already came back
    # unparseable and the resolver had returned.
    catalog_template=("from app.import_templates", "select template_code"),
    # inbound_ingest.py:982 -- the empty-publication preference, read here so
    # the inbound and the upload path apply one policy.
    allow_empty_publication="from app.project_preferences",
)

#: The one statement whose `description` cannot be derived: `COUNT(*) OVER ()`
#: is a window function, not a column name, so `describe` refuses rather than
#: guess. Named here, once, beside the query it belongs to.
_NAMED_DESCRIPTIONS = {
    "parsing_contract": [
        ("contract",),
        ("id",),
        ("fingerprint",),
        ("confirmed_by",),
        ("confirmed_at",),
        ("count",),
    ],
}


class _FakeCursor:
    """Answers the statements above, and REFUSES every other one.

    `description` is DERIVED from the statement rather than spelled out: a
    hand-written column tuple is a second copy of the projection, and it is the
    copy that goes stale first, because nothing reads it.
    """

    def __init__(self, responses: dict[str, object]):
        self._responses = responses
        self._current = None
        self.description = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        statement = _STATEMENTS.match(sql)
        self.description = _NAMED_DESCRIPTIONS.get(statement) or describe(sql)
        match statement:
            case "ingestable_datastream":
                self._current = self._responses.get("datastream")
            case "datastream_config":
                self._current = self._responses.get("datastream_config")
            case "projection_plan":
                self._current = self._responses.get("projection")
            case "mapping_bundle":
                self._current = self._responses.get("mapping_bundle")
            case "parsing_contract":
                self._current = self._responses.get("contract")
            case "catalog_template":
                self._current = self._responses.get("template")
            case "allow_empty_publication":
                self._current = self._responses.get("preference")
            case _:  # pragma: no cover - a name in the inventory, unanswered
                raise _STATEMENTS.unknown(sql)

    def fetchone(self):
        return self._current


class _FakeConn:
    def __init__(self, responses: dict[str, object]):
        self._responses = responses
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self._responses)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


# ---------------------------------------------------------------------------
# Fixtures / builders.
# ---------------------------------------------------------------------------

_CSV = b"date,channel,spend\n2026-01-01,search,1000.00\n"

_RUN_RESULT = {
    "ledger": {"id": "mfl_test_001", "outcome": "written"},
    "execution": {"id": "dse_test_001", "state": "created"},
    "import_contract_id": "cic_test_01",
    "no_op": False,
    "replay": False,
    "row_count": 1,
    "rejected_count": 0,
    "blocked": False,
    "reason": None,
    "outcome": "written_pending_publication",
    "published": False,
}


def _ds_row(
    *,
    source_kind="managed_feed",
    enabled=True,
    config=None,
    plan_version_id="dsp_01",
    mapping_version_id="dmap_01",
):
    """Build a datastreams SELECT tuple in column order the module reads."""
    if config is None:
        config = {"channels": ["upload", "email", "webhook"], "template_code": None}
    return (source_kind, enabled, config, plan_version_id, mapping_version_id)


def _responses(
    *,
    datastream,
    projection=({"executable": True},),
    contract=({"format": "csv", "write_mode": "replace"},),
    preference=(True,),
    template=None,
    mapping_bundle=(
        {
            "fields": [
                {
                    "field_id": "date",
                    "physical_type": "date",
                    "binding": {
                        "status": "confirmed",
                        "canonical_target": "day",
                    },
                }
            ]
        },
        "mapping-content-hash",
        "source-schema-hash",
        "mapping-capability-fingerprint",
        "plan-content-hash",
        "plan-capability-fingerprint",
        "universal-datastream-plan-v1",
    ),
):
    """Assemble the canned per-table responses. A None value => fetchone None."""
    return {
        "datastream": datastream,
        # THE SAME ROW'S `config` COLUMN, so the two reads of `app.datastreams`
        # cannot drift apart. `resolve_file_source_producer` asks for that one
        # column alone and reads `row[0]` as the JSON document it is.
        "datastream_config": (datastream[2],) if datastream else None,
        "projection": projection,
        "contract": contract,
        "preference": preference,
        "template": template,
        "mapping_bundle": mapping_bundle,
    }


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an unrecognised statement must NAME itself, not answer no rows.

    The old chain ended in `else: self._current = None`, and every read helper
    in this module treats `None` as a fact rather than as a gap -- not found,
    never imported, no binding. A query the product rewrote, or a read added to
    the inbound path tomorrow, would have been answered "absent" in silence and
    the eighteen tests below would have stayed green about a path never taken.
    """
    cursor = _FakeCursor(_responses(datastream=_ds_row()))
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT sender_hash, sender_domain_hash FROM app.inbound_raw_imports "
            "WHERE id = %s AND datastream_id = %s"
        )
    message = str(raised.value)
    # The statement that moved, and a neighbour to compare it against.
    assert "app.inbound_raw_imports" in message
    assert "ingestable_datastream" in message

    # The two reads of `app.datastreams` really are told apart.
    assert (
        _STATEMENTS.find(
            "SELECT source_kind, enabled, config, current_plan_version_id, "
            "current_mapping_version_id FROM app.datastreams WHERE id = %s"
        )
        == "ingestable_datastream"
    )
    assert (
        _STATEMENTS.find("SELECT config FROM app.datastreams WHERE id = %s")
        == "datastream_config"
    )


class _RunSpy:
    """Records the args run_import was called with; returns the canned result."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result or dict(_RUN_RESULT)

    def __call__(self, data, **kwargs):
        self.calls.append({"data": data, **kwargs})
        return self._result


@pytest.fixture()
def run_spy(monkeypatch):
    spy = _RunSpy()
    # run_import is lazily imported from core.csv_excel_import inside the function,
    # so patching the source module attribute intercepts the call.
    monkeypatch.setattr("core.csv_excel_import.run_import", spy)
    return spy


# ---------------------------------------------------------------------------
# Happy path / parity.
# ---------------------------------------------------------------------------


def test_happy_path_calls_run_import_with_resolved_args(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row()))
    result = ingest_inbound_file(
        conn,
        datastream_id="ds_01",
        project_id="proj_1",
        file_bytes=_CSV,
        filename="spend.csv",
        channel="email",
        message_id="msg-abc-123",
        actor="inbound-worker",
    )
    assert len(run_spy.calls) == 1
    call = run_spy.calls[0]
    # Parity: exactly the arguments the direct-upload path resolves.
    assert call["data"] == _CSV
    assert call["datastream_id"] == "ds_01"
    assert call["project_id"] == "proj_1"
    assert call["plan_version_id"] == "dsp_01"
    assert call["mapping_version_id"] == "dmap_01"
    assert call["projection_plan"] == {"executable": True}
    assert call["actor"] == "inbound-worker"
    assert call["contract"] == {"format": "csv", "write_mode": "replace"}
    assert call["write_mode"] == "replace"
    assert call["force_empty_publish"] is False
    assert call["preferences"] == {"allow_empty_publication": True}
    assert call["conn"] is conn
    # Caller owns the transaction: the worker never commits/rolls back.
    assert conn.committed is False
    assert conn.rolled_back is False
    # Result is the run_import shape (parity with a direct upload).
    assert result["outcome"] == "written_pending_publication"
    assert result["ledger"]["id"] == "mfl_test_001"


def test_upload_email_and_webhook_resolve_the_same_governed_bundle(run_spy):
    seed_conn = _FakeConn(_responses(datastream=_ds_row()))
    ingest_inbound_file(
        seed_conn,
        datastream_id="ds_01",
        project_id="proj_1",
        file_bytes=_CSV,
        filename="spend.csv",
        channel="upload",
        message_id="bundle-seed",
        actor="inbound-worker",
    )
    frozen = run_spy.calls.pop()["dispatch_bundle"]

    calls = []
    for channel in ("upload", "email", "webhook"):
        conn = _FakeConn(_responses(datastream=_ds_row()))
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel=channel,
            message_id=f"msg-{channel}",
            actor="inbound-worker",
            raw_import_id=f"raw-{channel}",
            dispatch_bundle_override=frozen,
        )
        calls.append(run_spy.calls[-1])

    for call in calls:
        assert call["plan_version_id"] == "dsp_01"
        assert call["mapping_version_id"] == "dmap_01"
        assert call["projection_plan"] == {"executable": True}
        assert call["mapping_payload"]["fields"][0]["binding"]["status"] == "confirmed"
        assert call["publish_candidate"] is True
        assert call["dispatch_bundle"] == calls[0]["dispatch_bundle"]


def test_result_is_augmented_with_provenance_keys(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row()))
    result = ingest_inbound_file(
        conn,
        datastream_id="ds_01",
        project_id="proj_1",
        file_bytes=_CSV,
        filename="spend.csv",
        channel="webhook",
        message_id="msg-xyz-9",
        actor="inbound-worker",
    )
    assert result["datastream_id"] == "ds_01"
    assert result["channel"] == "webhook"
    assert result["message_id"] == "msg-xyz-9"
    # Original run_import keys still present.
    assert result["published"] is False


# ---------------------------------------------------------------------------
# Fail-closed preconditions (run_import never called).
# ---------------------------------------------------------------------------


def test_channel_not_enabled_raises(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row(config={"channels": ["email"]})))
    with pytest.raises(DatastreamNotIngestable):
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="webhook",  # not in allowlist
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


def test_not_managed_feed_raises(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row(source_kind="connector_pull")))
    with pytest.raises(DatastreamNotIngestable):
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="email",
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


def test_missing_plan_or_mapping_raises(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row(mapping_version_id=None)))
    with pytest.raises(DatastreamNotIngestable):
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="email",
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


def test_not_found_raises(run_spy):
    conn = _FakeConn(_responses(datastream=None))
    with pytest.raises(DatastreamNotIngestable):
        ingest_inbound_file(
            conn,
            datastream_id="ds_missing",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="email",
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


def test_disabled_raises(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row(enabled=False)))
    with pytest.raises(DatastreamNotIngestable):
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="email",
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


def test_no_projection_recorded_raises(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row(), projection=None))
    with pytest.raises(DatastreamNotIngestable):
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="email",
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


def test_empty_bytes_raises(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row()))
    with pytest.raises(InboundIngestValidationError):
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=b"",
            filename="spend.csv",
            channel="email",
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


def test_blank_channel_raises(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row()))
    with pytest.raises(InboundIngestValidationError):
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="   ",
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


def test_blank_message_id_raises(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row()))
    with pytest.raises(InboundIngestValidationError):
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="email",
            message_id="",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


# ---------------------------------------------------------------------------
# Parsing-contract fallback.
# ---------------------------------------------------------------------------


def test_missing_contract_retains_raw_for_review(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row(config={"channels": ["email"]}), contract=None))
    with pytest.raises(InboundContractReviewRequired) as exc:
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="email",
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert exc.value.code == "inbound_contract_review_required"
    assert run_spy.calls == []


def test_template_hints_do_not_authorize_unattended_guess(run_spy):
    config = {
        "channels": ["email"],
        "template_code": "TV_LINEAR_V1",
        "template_version": 1,
    }
    conn = _FakeConn(_responses(datastream=_ds_row(config=config), contract=None))
    with pytest.raises(InboundContractReviewRequired):
        ingest_inbound_file(
            conn,
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="email",
            message_id="msg-1",
            actor="inbound-worker",
        )
    assert run_spy.calls == []


def test_idempotency_key_derivation_from_message_id(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row()))
    ingest_inbound_file(
        conn,
        datastream_id="ds_01",
        project_id="proj_1",
        file_bytes=_CSV,
        filename="spend.csv",
        channel="email",
        message_id="prov-message-42",
        actor="inbound-worker",
    )
    key = run_spy.calls[0]["idempotency_key"]
    assert key == "inbound:email:ds_01:prov-message-42"


def test_source_metadata_shape(run_spy):
    conn = _FakeConn(_responses(datastream=_ds_row()))
    ingest_inbound_file(
        conn,
        datastream_id="ds_01",
        project_id="proj_1",
        file_bytes=_CSV,
        filename="spend.csv",
        channel="email",
        message_id="msg-99",
        actor="inbound-worker",
    )
    meta = run_spy.calls[0]["source_metadata"]
    assert meta == {
        "ingress_channel": "email",
        "message_id": "msg-99",
        "filename": "spend.csv",
    }


def test_a_managed_feed_bundle_ingests_without_connector_capability_fingerprints(run_spy):
    """AI-321 (2026-08-29): a file Datastream the assistant materialises carries NO
    capability fingerprint on its plan (there is no connector capability to
    fingerprint) and its mapping inherits the None. Demanding both refused every
    unattended import of every such Datastream as "incomplete fingerprint
    evidence". The two content hashes and the contract version stay mandatory."""
    nullable = (
        {"fields": [{"field_id": "date", "physical_type": "date",
                     "binding": {"status": "confirmed", "canonical_target": "day"}}]},
        "mapping-content-hash",
        "source-schema-hash",
        None,
        "plan-content-hash",
        None,
        "universal-datastream-plan-v1",
    )
    conn = _FakeConn(_responses(datastream=_ds_row(), mapping_bundle=nullable))
    ingest_inbound_file(
        conn,
        datastream_id="ds_01",
        project_id="proj_1",
        file_bytes=_CSV,
        filename="spend.csv",
        channel="upload",
        message_id="nullable-fingerprints",
        actor="inbound-worker",
    )
    bundle = run_spy.calls.pop()["dispatch_bundle"]
    # The import RAN with both capability fingerprints absent; the content hashes
    # it carries under `governed_evidence` are the mandatory ones.
    evidence = bundle["governed_evidence"]
    assert evidence["capability_fingerprint"] is None
    assert evidence["plan_capability_fingerprint"] is None
    assert evidence["mapping_fingerprint"] == "mapping-content-hash"
    assert evidence["plan_fingerprint"] == "plan-content-hash"

    missing_hash = list(nullable)
    missing_hash[1] = ""
    with pytest.raises(DatastreamNotIngestable, match="incomplete fingerprint evidence"):
        ingest_inbound_file(
            _FakeConn(_responses(datastream=_ds_row(), mapping_bundle=tuple(missing_hash))),
            datastream_id="ds_01",
            project_id="proj_1",
            file_bytes=_CSV,
            filename="spend.csv",
            channel="upload",
            message_id="missing-hash",
            actor="inbound-worker",
        )


def test_a_frozen_bundle_keeps_the_catalog_governance_of_its_producer():
    """AI-321 (2026-08-29): a catalog Template answers `confirmed_for` True by
    construction; the producer rebuilt from the frozen dispatch bundle forgot that
    flag, so every catalog-bound import was refused `file_source_confirmation_required`
    one link after the resolver had accepted it."""
    from core.csv_excel_import import FileSourceProducer
    from core.inbound_ingest import _producer_from_frozen_bundle

    catalog = FileSourceProducer(
        {"contract": {"required_fields": ["day"]}, "content_hash": None},
        {"day": "day"},
        "template:OFFLINE_OOH_V1:2",
        confirmation_operation_id=None,
        confirmation_evidence={},
        catalog_governed=True,
    )
    block = {
        "template": catalog.template,
        "mapping": catalog.mapping,
        "template_id": catalog.template_id,
        "confirmation_operation_id": None,
        "confirmation_evidence": {},
        "catalog_governed": True,
    }
    rebuilt = _producer_from_frozen_bundle({"template": block})
    assert rebuilt.confirmed_for("dmap_any") is True

    legacy = dict(block)
    legacy.pop("catalog_governed")
    legacy["template_id"] = "fst_0123456789ABCDEFGHJKMNPQRS"
    assert _producer_from_frozen_bundle({"template": legacy}).confirmed_for("dmap_any") is False
