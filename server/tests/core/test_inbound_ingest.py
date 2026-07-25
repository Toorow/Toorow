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
    test_contract_fallback_pulls_template_column_types

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
    InboundIngestValidationError,
    ingest_inbound_file,
)

# ---------------------------------------------------------------------------
# Fake connection / cursor.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Minimal cursor that dispatches SELECTs to canned row lists by table name.

    The module issues one SELECT per read helper; we key the response off a
    distinctive substring of the SQL so each helper gets its intended row.
    """

    def __init__(self, responses: dict[str, object]):
        self._responses = responses
        self._current = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if "FROM app.datastreams" in text:
            self._current = self._responses.get("datastream")
        elif "FROM app.datastream_executions" in text:
            self._current = self._responses.get("projection")
        elif "FROM app.csv_excel_import_contracts" in text:
            self._current = self._responses.get("contract")
        elif "FROM app.project_preferences" in text:
            self._current = self._responses.get("preference")
        elif "FROM app.import_templates" in text:
            self._current = self._responses.get("template")
        else:
            self._current = None

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
        config = {"channels": ["email", "webhook"], "template_code": None}
    return (source_kind, enabled, config, plan_version_id, mapping_version_id)


def _responses(
    *,
    datastream,
    projection=({"executable": True},),
    contract=({"format": "csv", "write_mode": "replace"},),
    preference=(True,),
    template=None,
):
    """Assemble the canned per-table responses. A None value => fetchone None."""
    return {
        "datastream": datastream,
        "projection": projection,
        "contract": contract,
        "preference": preference,
        "template": template,
    }


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
    conn = _FakeConn(
        _responses(datastream=_ds_row(config={"channels": ["email"]}))
    )
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
    conn = _FakeConn(
        _responses(datastream=_ds_row(source_kind="connector_pull"))
    )
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
    conn = _FakeConn(
        _responses(datastream=_ds_row(mapping_version_id=None))
    )
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


def test_contract_fallback_when_no_active_contract(run_spy):
    # No active parsing contract row -> build the minimal auto-detect contract.
    conn = _FakeConn(
        _responses(
            datastream=_ds_row(config={"channels": ["email"]}),
            contract=None,
        )
    )
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
    assert len(run_spy.calls) == 1
    contract = run_spy.calls[0]["contract"]
    assert contract == {"format": "csv", "write_mode": "replace"}


def test_contract_fallback_pulls_template_column_types(run_spy):
    # Fallback contract, and the datastream pins a template that supplies typing
    # hints -> valid hints are forwarded as column_types (unknown ones dropped).
    config = {
        "channels": ["email"],
        "template_code": "TV_LINEAR_V1",
        "template_version": 1,
    }
    # get_template reads columns: template_code, version, title, contract,
    # is_generic, created_at (contract at index 3; created_at None -> no isoformat).
    template_row = (
        "TV_LINEAR_V1",
        1,
        "TV Linear",
        {
            "field_types": {
                "spend": "decimal",
                "media_date": "date",
                "bogus": "not_a_type",  # dropped
            }
        },
        False,
        None,
    )
    conn = _FakeConn(
        _responses(
            datastream=_ds_row(config=config),
            contract=None,
            template=template_row,
        )
    )
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
    contract = run_spy.calls[0]["contract"]
    assert contract["format"] == "csv"
    assert contract["write_mode"] == "replace"
    assert contract["column_types"] == {"spend": "decimal", "media_date": "date"}


# ---------------------------------------------------------------------------
# Idempotency + provenance.
# ---------------------------------------------------------------------------


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
