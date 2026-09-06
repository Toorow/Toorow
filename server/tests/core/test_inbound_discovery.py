"""AI-113 -- the first delivery reveals the shape, and the journey stops being inverted.

The product promises: create an inbound Datastream, get its address, sit in
draft, send the first file, SEE ITS SHAPE, validate. The code enforced the
reverse -- `ingest_inbound_file` refuses anything without a locked plan, a
locked mapping and a recorded projection, so a file had to be uploaded by hand
BEFORE email could ever work.

This file proves the missing link, and proves it did not open a hole:

  (a) A draft Datastream's delivery is ACCEPTED and described, not FAILED.
  (b) An ACTIVE Datastream still ingests -- and still fails loudly when it is
      genuinely misconfigured. The guard is not relaxed, it is bypassed only
      where there is nothing to publish against.
  (c) The observation is the SAME payload an uploaded file produces, so the
      review screen cannot tell which channel it came from.
  (d) No row value reaches the observation. Fields and types are schema; the
      values behind them are the tenant's data.
  (e) Every unavailability is a coded exception, never an error -- this is the
      screen a person opens to find out why nothing is happening.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

_CSV = (
    b"date,clicks,respondent_email\n"
    b"2026-01-15,10,alice@example.com\n"
    b"2026-01-16,12,bob@example.com\n"
)
_HASH = hashlib.sha256(_CSV).hexdigest()


def _store(tmp_path):
    from core.inbound_quarantine import LocalFsQuarantineStore

    return LocalFsQuarantineStore(root=str(tmp_path))


def _stored(store, data=_CSV, filename="report.csv"):
    return store.put(
        partition="p",
        message_id="m",
        filename=filename,
        data=data,
        content_type="text/csv",
    ).uri


def _raw_row(uri, **overrides):
    row = {
        "raw_import_id": "inbraw_1",
        "datastream_id": "ds-1",
        "ordinal": 0,
        "filename": "report.csv",
        "media_type_declared": "text/csv",
        "media_type_detected": "text/csv",
        "size_bytes": len(_CSV),
        "content_hash": _HASH,
        "quarantine_uri": uri,
        "state": "ACCEPTED",
        "error_code": None,
    }
    row.update(overrides)
    return row


def _patch_list(monkeypatch, rows):
    import core.inbound_raw_imports as iri

    monkeypatch.setattr(iri, "list_raw_imports", lambda conn, **kw: rows)


def _conn_lifecycle(state):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = (state,)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


# ---------------------------------------------------------------------------
# (a) (b) The posture rule -- one fact, not a heuristic.
# ---------------------------------------------------------------------------


def test_a_draft_datastream_describes_its_delivery():
    from core.inbound_discovery import POSTURE_DISCOVERY, delivery_posture

    assert delivery_posture(_conn_lifecycle("draft"), datastream_id="ds-1") == (
        POSTURE_DISCOVERY
    )


@pytest.mark.parametrize("state", ["active", "paused", "archived"])
def test_anything_that_is_not_a_draft_still_ingests(state):
    """The guard is bypassed only where there is nothing to publish against.

    An ACTIVE Datastream missing a plan is a real misconfiguration and must keep
    failing loudly -- routing it here would trade an actionable failure for a
    silent description, which is the trade this repository keeps unwinding.
    """
    from core.inbound_discovery import POSTURE_INGEST, delivery_posture

    assert delivery_posture(_conn_lifecycle(state), datastream_id="ds-1") == (
        POSTURE_INGEST
    )


def test_an_unreadable_lifecycle_is_not_treated_as_a_draft():
    """Fail towards the STRICTER path when the fact cannot be read."""
    from core.inbound_discovery import POSTURE_INGEST, delivery_posture

    class _Down:
        def cursor(self):
            raise RuntimeError("database unreachable")

    assert delivery_posture(_Down(), datastream_id="ds-1") == POSTURE_INGEST


# ---------------------------------------------------------------------------
# (c) The observation is indistinguishable from an upload's.
# ---------------------------------------------------------------------------


def test_the_observation_matches_the_upload_adapter_contract(monkeypatch, tmp_path):
    """Same keys, same coverage vocabulary, same normalisation.

    The wizard must not be able to tell whether a shape came from an upload or
    from an email: the delivery channel is transport, and transport must not
    change what governance sees.
    """
    from core.datastream_setup_observations import normalize_adapter_evidence
    from core.inbound_discovery import observe_first_delivery

    store = _store(tmp_path)
    _patch_list(monkeypatch, [_raw_row(_stored(store))])

    observation = observe_first_delivery(
        object(), datastream_id="ds-1", store=store
    )

    assert observation["exceptions"] == []
    assert observation["coverage"] == {
        "schema": "available",
        "rows": "bounded_count_only",
    }
    meta = observation["safe_metadata"]
    assert [f["name"] for f in meta["fields"]] == [
        "date", "clicks", "respondent_email"
    ]
    assert meta["content_hash"] == _HASH
    assert meta["schema_hash"]
    assert meta["row_count_bucket"] == "1-99"

    # It survives the seam's deny-by-default projection with its fields intact.
    normalized = normalize_adapter_evidence(observation)
    assert len(normalized["safe_metadata"]["fields"]) == 3
    assert normalized["evidence_fingerprint"]


def test_the_adapter_ref_says_where_the_shape_came_from(monkeypatch, tmp_path):
    """Identical payload, distinct provenance -- an auditor can still tell."""
    from core.inbound_discovery import ADAPTER_REF, observe_first_delivery

    store = _store(tmp_path)
    _patch_list(monkeypatch, [_raw_row(_stored(store))])

    observation = observe_first_delivery(
        object(), datastream_id="ds-1", store=store
    )
    assert observation["adapter_ref"] == ADAPTER_REF
    assert observation["adapter_ref"] != "managed_feed.file.readonly.v1"


def test_a_sav_delivery_is_described_too(monkeypatch, tmp_path):
    pyreadstat = pytest.importorskip("pyreadstat")
    pd = pytest.importorskip("pandas")

    from core.inbound_discovery import observe_first_delivery

    path = tmp_path / "s.sav"
    pyreadstat.write_sav(pd.DataFrame({"q1": [1.0, 2.0]}), str(path))
    data = path.read_bytes()

    store = _store(tmp_path / "q")
    uri = _stored(store, data=data, filename="survey.sav")
    _patch_list(
        monkeypatch,
        [
            _raw_row(
                uri,
                filename="survey.sav",
                content_hash=hashlib.sha256(data).hexdigest(),
            )
        ],
    )

    observation = observe_first_delivery(
        object(), datastream_id="ds-1", store=store
    )
    assert observation["safe_metadata"]["detected_format"] == "sav"
    assert [f["name"] for f in observation["safe_metadata"]["fields"]] == ["q1"]


# ---------------------------------------------------------------------------
# (d) Schema, never values.
# ---------------------------------------------------------------------------


def test_no_row_value_reaches_the_observation(monkeypatch, tmp_path):
    """`sample_rows` and `raw_sample` are FORBIDDEN_KEYS in the setup seam.

    Field names and types are schema; the values behind them are the tenant's
    data, and a discovery payload travelling into a setup review is the last
    place they belong.
    """
    from core.inbound_discovery import observe_first_delivery

    store = _store(tmp_path)
    _patch_list(monkeypatch, [_raw_row(_stored(store))])

    blob = str(observe_first_delivery(object(), datastream_id="ds-1", store=store))
    assert "alice@example.com" not in blob
    assert "bob@example.com" not in blob
    assert "2026-01-15" not in blob
    # A bucket, never an exact count.
    assert "detected_row_count" not in blob


# ---------------------------------------------------------------------------
# (e) Unavailability is a coded exception.
# ---------------------------------------------------------------------------


def test_no_delivery_yet_is_the_ordinary_state_not_an_error(monkeypatch):
    from core.inbound_discovery import NO_DELIVERY_YET, observe_first_delivery

    _patch_list(monkeypatch, [])
    observation = observe_first_delivery(object(), datastream_id="ds-1")

    assert observation["exceptions"] == [{"code": NO_DELIVERY_YET}]
    assert observation["safe_metadata"]["fields"] == []
    assert observation["coverage"]["schema"] == "unavailable"


def test_a_rejected_attachment_is_not_described(monkeypatch, tmp_path):
    """Describing a file the scan gate refused would put it on a review screen.

    A REJECTED attachment failed 38.10 -- showing its shape beside a validate
    button is an invitation to accept it.
    """
    from core.inbound_discovery import NO_DELIVERY_YET, observe_first_delivery

    store = _store(tmp_path)
    _patch_list(
        monkeypatch,
        [_raw_row(_stored(store), state="REJECTED", error_code="archive_ratio")],
    )
    observation = observe_first_delivery(
        object(), datastream_id="ds-1", store=store
    )
    assert observation["exceptions"] == [{"code": NO_DELIVERY_YET}]


def test_unreadable_bytes_are_a_coded_exception(monkeypatch, tmp_path):
    from core.inbound_discovery import NO_BYTES, observe_first_delivery

    store = _store(tmp_path)
    _patch_list(monkeypatch, [_raw_row("file:///gone/missing.csv")])
    observation = observe_first_delivery(
        object(), datastream_id="ds-1", store=store
    )
    assert observation["exceptions"] == [{"code": NO_BYTES}]


def test_an_unparseable_delivery_is_a_coded_exception(monkeypatch, tmp_path):
    from core.inbound_discovery import NO_READABLE_DELIVERY, observe_first_delivery

    store = _store(tmp_path)
    uri = _stored(store, data=b"\x00\x01 binary junk", filename="corrupt.csv")
    _patch_list(monkeypatch, [_raw_row(uri, filename="corrupt.csv")])
    observation = observe_first_delivery(
        object(), datastream_id="ds-1", store=store
    )
    code = observation["exceptions"][0]["code"]
    assert code in {NO_READABLE_DELIVERY, "encoding_error", "unsupported_file_type"}
    assert observation["exceptions"] == [{"code": code}]


def test_the_latest_delivery_is_described_not_the_first_ever(monkeypatch, tmp_path):
    """A sender who gets it wrong twice must see their SECOND file."""
    from core.inbound_discovery import observe_first_delivery

    store = _store(tmp_path)
    newer = b"a,b,c\n1,2,3\n"
    newest_uri = _stored(store, data=newer, filename="second.csv")
    older_uri = _stored(store, data=_CSV, filename="first.csv")

    # `list_raw_imports` is newest-first; the module must honour that order.
    _patch_list(
        monkeypatch,
        [
            _raw_row(
                newest_uri,
                filename="second.csv",
                content_hash=hashlib.sha256(newer).hexdigest(),
            ),
            _raw_row(older_uri, filename="first.csv"),
        ],
    )
    observation = observe_first_delivery(
        object(), datastream_id="ds-1", store=store
    )
    assert [f["name"] for f in observation["safe_metadata"]["fields"]] == [
        "a", "b", "c"
    ]


# ---------------------------------------------------------------------------
# Story 57.3 -- the channel contract state, read and never invented.
#
# Offline by construction: `read_channel_contract` is a pure read, so a scripted
# cursor is enough. No Mailgun account exists for this repository and none is
# asked for; what these tests pin is OURS -- that no address is fabricated, that
# no frequency is invented, and that three absences read as three absences.
# ---------------------------------------------------------------------------


class _ScriptedCursor:
    """Answers each statement by the table it names, in any order."""

    def __init__(self, *, domain=None, template=None, credential=False):
        self._domain = domain
        self._template = template
        self._credential = credential
        self._answer = None
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if "connector_domain_configs" in sql:
            self._answer = (self._domain,) if self._domain else None
        elif "file_source_templates" in sql:
            self._answer = self._template
        elif "datastream_inbound_credentials" in sql:
            self._answer = (1,) if self._credential else None
        else:  # pragma: no cover -- an unexpected read must not answer silently
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        return self._answer


class _ScriptedConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_channel_contract_says_when_the_deployment_has_no_verified_domain():
    """No domain is a PLATFORM state, and it names no address at all."""
    from core.inbound_discovery import read_channel_contract

    state = read_channel_contract(
        _ScriptedConn(_ScriptedCursor(domain=None)),
        project_id="proj_1",
        channel="inbound_email",
    )
    assert state["domain"] == "not_configured"
    assert state["inbound_domain"] is None
    assert state["address_form"] is None


def test_a_draft_has_no_address_and_that_is_the_ordinary_state():
    """`not_addressable_yet` -- there is no Datastream to attach a secret to.

    The step shows the FORM of the address and where it is issued. An address
    shown before it is minted would resolve no token and be a secret nobody
    created.
    """
    from core.inbound_discovery import read_channel_contract

    state = read_channel_contract(
        _ScriptedConn(_ScriptedCursor(domain="feeds.toorow-test.invalid")),
        project_id="proj_1",
        channel="inbound_email",
    )
    assert state["capability"] == "not_addressable_yet"
    assert state["address_form"] == "ds_<token>@feeds.toorow-test.invalid"
    assert "ds_" not in state["address_form"].removeprefix("ds_<token>@")


def test_a_webhook_has_a_token_rather_than_an_address():
    """No dedicated URL exists for a webhook: its capability IS a token."""
    from core.inbound_discovery import read_channel_contract

    state = read_channel_contract(
        _ScriptedConn(_ScriptedCursor(domain="feeds.toorow-test.invalid")),
        project_id="proj_1",
        channel="webhook",
    )
    assert state["domain"] == "configured"
    assert state["address_form"] is None


def test_the_format_clause_is_a_reference_and_says_when_none_is_bound():
    """A Template REFERENCE, never a parsing rule -- that belongs to epic 22."""
    from core.inbound_discovery import read_channel_contract

    unbound = read_channel_contract(
        _ScriptedConn(_ScriptedCursor(domain="feeds.toorow-test.invalid")),
        project_id="proj_1",
        channel="inbound_email",
    )
    bound = read_channel_contract(
        _ScriptedConn(
            _ScriptedCursor(
                domain="feeds.toorow-test.invalid",
                template=("weekly_spend", 3, "date/campaign"),
            )
        ),
        project_id="proj_1",
        channel="inbound_email",
        template_ref="fst_1",
    )
    assert unbound["format"] == "not_declared"
    assert unbound["template"] is None
    assert bound["format"] == "template_bound"
    assert bound["template"] == {
        "template_code": "weekly_spend",
        "version": 3,
        "grain": "date/campaign",
    }
    assert "delimiter" not in str(bound)


def test_the_sender_clause_says_token_only_until_a_list_is_declared():
    """The default is the truth: the token in the address is the authorization."""
    from core.inbound_discovery import read_channel_contract

    undeclared = read_channel_contract(
        _ScriptedConn(_ScriptedCursor(domain="feeds.toorow-test.invalid")),
        project_id="proj_1",
        channel="inbound_email",
    )
    declared = read_channel_contract(
        _ScriptedConn(_ScriptedCursor(domain="feeds.toorow-test.invalid")),
        project_id="proj_1",
        channel="inbound_email",
        declared={"allowed_senders": ["  Reports@Agency.invalid ", "agency.invalid", ""]},
    )
    assert undeclared["sender"] == "token_only"
    assert undeclared["allowed_senders"] == []
    assert declared["sender"] == "declared_list"
    assert declared["allowed_senders"] == ["reports@agency.invalid", "agency.invalid"]


def test_the_arrival_clause_is_never_defaulted():
    """No declaration means NOT DECLARED. `1440` is nobody's promise."""
    from core.inbound_discovery import read_channel_contract

    undeclared = read_channel_contract(
        _ScriptedConn(_ScriptedCursor(domain="feeds.toorow-test.invalid")),
        project_id="proj_1",
        channel="webhook",
    )
    declared = read_channel_contract(
        _ScriptedConn(_ScriptedCursor(domain="feeds.toorow-test.invalid")),
        project_id="proj_1",
        channel="webhook",
        declared={"expected_interval_minutes": 60},
    )
    nonsense = read_channel_contract(
        _ScriptedConn(_ScriptedCursor(domain="feeds.toorow-test.invalid")),
        project_id="proj_1",
        channel="webhook",
        declared={"expected_interval_minutes": 0},
    )
    assert undeclared["arrival"] == "not_declared"
    assert undeclared["expected_interval_minutes"] is None
    assert declared["arrival"] == "declared"
    assert declared["expected_interval_minutes"] == 60
    assert nonsense["arrival"] == "not_declared"


def test_an_issued_capability_is_read_as_state_and_never_as_a_secret():
    """`issued` says an address EXISTS. It never says what it is."""
    from core.inbound_discovery import read_channel_contract

    cursor = _ScriptedCursor(domain="feeds.toorow-test.invalid", credential=True)
    state = read_channel_contract(
        _ScriptedConn(cursor),
        project_id="proj_1",
        channel="inbound_email",
        datastream_id="ds_1",
    )
    assert state["capability"] == "issued"
    # The read asks for existence, not for the columns that could reconstruct one.
    credential_sql = [sql for sql in cursor.statements if "inbound_credentials" in sql]
    assert credential_sql and "token_hash" not in credential_sql[0]
    assert "safe_suffix" not in credential_sql[0]
