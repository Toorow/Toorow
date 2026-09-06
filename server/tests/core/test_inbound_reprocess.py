"""Story 38.18: reprocess retained raw evidence, without asking for it again.

The three refusals are the substance of this story, so each one is exercised
against a REAL quarantine store with real bytes -- a mocked store that "fails"
proves nothing about which failures are distinguished.

Offline:
  (a) An absent or foreign raw import is unavailable, indistinguishably.
  (b) A missing object reference is its own reason, not a generic failure.
  (c) An unreadable object is a STORAGE fault, never reported as retention.
  (d) An integrity mismatch refuses -- the one case where continuing is wrong.
  (e) Expired retention refuses; a legal hold OVERRIDES expiry.
  (f) The proposal states effect, not mechanism, and has no side effect.
  (g) The message_id is derived from the idempotency key, so a replay cannot
      create a second execution and a new key can.
  (h) A malformed request raises before any work; a missing reason is malformed.
  (i) The scan runs AGAIN on reprocess -- bounds are policy, and policy changes.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

_CSV = b"date,clicks\n2026-01-01,10\n"
_HASH = hashlib.sha256(_CSV).hexdigest()

# EVERY STATEMENT ONE `execute_reprocess` RUN ISSUES against the fake connection,
# NAMED. The fake below used to end its `if/elif` chain on nothing at all: any
# other read came back `None` from `fetchone()` and the run carried on -- which
# is exactly how `SELECT config` (the Template pin, read through
# `read_reprocess_template_version`) got answered by accident for a while and
# how a moved query would go on being "answered" forever (AI-317).
#
# `SELECT config` is NOT this module's statement: it is issued by
# `core/inbound_ingest.py:622` under `read_reprocess_template_version`, called
# from `core/inbound_reprocess.py:685`. It is here because this fake is the
# connection that path reads through.
_REPLAY = StatementInventory(
    "test_inbound_reprocess._Cur",
    project="select project_id from app.datastreams",
    config="select config from app.datastreams",
    origin_channel=("select rx.channel", "join app.inbound_receipts rx"),
)


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: the replay fake answers the QUERY, and refuses what it never saw.

    Its `execute()` ended without an `else`, so `fetchone()` returned `None` to
    every statement the chain did not name. `None` is a real answer everywhere
    on this path -- "no project", "no config", "no receipt" -- so an unmodelled
    read looked like an empty database and the assertions downstream were about
    a branch the product never took.
    """
    assert (
        _REPLAY.find("SELECT project_id FROM app.datastreams WHERE id = %s") == "project"
    )
    assert (
        _REPLAY.find(
            "SELECT config FROM app.datastreams WHERE id = %s AND project_id = %s"
        )
        == "config"
    )
    with pytest.raises(UnknownStatement) as raised:
        _REPLAY.match(
            "SELECT state FROM app.inbound_raw_imports WHERE id = %s"
        )
    assert "app.inbound_raw_imports" in str(raised.value)
    assert "origin_channel" in str(raised.value)


def _store(tmp_path):
    from core.inbound_quarantine import LocalFsQuarantineStore

    return LocalFsQuarantineStore(root=str(tmp_path))


def _stored(store, data=_CSV, filename="report.csv"):
    obj = store.put(
        partition="p",
        message_id="m",
        filename=filename,
        data=data,
        content_type="text/csv",
    )
    return obj.uri


def _raw_row(**overrides):
    """The safe read-model shape `get_raw_import` returns."""
    row = {
        "raw_import_id": "inbraw_1",
        "receipt_id": "inbrx_1",
        "datastream_id": "ds-1",
        "ordinal": 0,
        "filename": "report.csv",
        "media_type_declared": "text/csv",
        "media_type_detected": "text/csv",
        "size_bytes": len(_CSV),
        "content_hash": _HASH,
        "quarantine_uri": "file:///nowhere",
        "state": "LANDED",
        "scan_verdict": None,
        "error_code": None,
        "import_ledger_id": "mfl_old",
        "retention_expires_at": None,
        "legal_hold": False,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _patch_raw(monkeypatch, row):
    import core.inbound_raw_imports as iri

    monkeypatch.setattr(iri, "get_raw_import", lambda conn, **kw: row)


# ---------------------------------------------------------------------------
# (a) (b) Absent evidence.
# ---------------------------------------------------------------------------


def test_an_absent_or_foreign_raw_import_is_one_answer(monkeypatch):
    from core.inbound_reprocess import UNAVAILABLE_NOT_FOUND, evaluate_reprocess

    _patch_raw(monkeypatch, None)
    out = evaluate_reprocess(
        object(), raw_import_id="inbraw_x", datastream_id="ds-1"
    )
    assert out.available is False
    assert out.reason == UNAVAILABLE_NOT_FOUND


def test_a_record_without_an_object_reference_says_so(monkeypatch):
    from core.inbound_reprocess import UNAVAILABLE_NO_URI, evaluate_reprocess

    _patch_raw(monkeypatch, _raw_row(quarantine_uri=None))
    out = evaluate_reprocess(
        object(), raw_import_id="inbraw_1", datastream_id="ds-1"
    )
    assert out.reason == UNAVAILABLE_NO_URI


# ---------------------------------------------------------------------------
# (c) (d) A storage fault and a wrong file are DIFFERENT answers.
# ---------------------------------------------------------------------------


def test_an_unreadable_object_is_a_storage_fault_not_a_retention_decision(
    monkeypatch, tmp_path
):
    """Reporting this as 'nothing to reprocess' would hide an outage as policy."""
    from core.inbound_reprocess import (
        UNAVAILABLE_NO_OBJECT,
        UNAVAILABLE_RETENTION_EXPIRED,
        evaluate_reprocess,
    )

    store = _store(tmp_path)
    _patch_raw(monkeypatch, _raw_row(quarantine_uri="file:///gone/missing.csv"))

    out = evaluate_reprocess(
        object(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )
    assert out.reason == UNAVAILABLE_NO_OBJECT
    assert out.reason != UNAVAILABLE_RETENTION_EXPIRED


def test_an_integrity_mismatch_refuses(monkeypatch, tmp_path):
    """The bytes are readable and are NOT the bytes the evidence describes.

    Continuing here would reprocess a different file under an audit trail
    claiming it was this one -- the only refusal in this module where proceeding
    is worse than failing.
    """
    from core.inbound_reprocess import UNAVAILABLE_INTEGRITY, evaluate_reprocess

    store = _store(tmp_path)
    uri = _stored(store, data=b"date,clicks\n2026-01-01,999\n")  # different bytes
    _patch_raw(monkeypatch, _raw_row(quarantine_uri=uri))  # hash still says _HASH

    out = evaluate_reprocess(
        object(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )
    assert out.available is False
    assert out.reason == UNAVAILABLE_INTEGRITY


def test_matching_bytes_are_available(monkeypatch):
    from core.inbound_reprocess import evaluate_reprocess

    class MemoryStore:
        def get(self, uri):
            return _CSV

    store = MemoryStore()
    _patch_raw(monkeypatch, _raw_row(quarantine_uri="memory://retained"))

    out = evaluate_reprocess(
        object(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )
    assert out.available is True
    assert out.reason is None
    assert out.content_hash == _HASH


# ---------------------------------------------------------------------------
# (e) Retention, and the hold that overrides it.
# ---------------------------------------------------------------------------


def test_expired_retention_refuses_with_a_bounded_reason(monkeypatch, tmp_path):
    from core.inbound_reprocess import (
        UNAVAILABLE_RETENTION_EXPIRED,
        evaluate_reprocess,
    )

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(
        monkeypatch,
        _raw_row(
            quarantine_uri=uri,
            retention_expires_at="2020-01-01T00:00:00+00:00",
        ),
    )

    out = evaluate_reprocess(
        object(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )
    assert out.reason == UNAVAILABLE_RETENTION_EXPIRED
    # Explicit, not a silent request to the provider (E38-NFR15).
    assert out.detail


def test_a_legal_hold_overrides_expiry(monkeypatch, tmp_path):
    """That override IS what a hold is for; the check order is the policy."""
    from core.inbound_reprocess import evaluate_reprocess

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(
        monkeypatch,
        _raw_row(
            quarantine_uri=uri,
            retention_expires_at="2020-01-01T00:00:00+00:00",
            legal_hold=True,
        ),
    )

    out = evaluate_reprocess(
        object(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )
    assert out.available is True
    assert out.legal_hold is True


# ---------------------------------------------------------------------------
# (f) The proposal.
# ---------------------------------------------------------------------------


def test_the_proposal_states_the_effect_and_writes_nothing(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from core.inbound_reprocess import prepare_reprocess

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw_row(quarantine_uri=uri))

    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("plan_v1", "map_v1")
    conn = MagicMock()
    conn.cursor.return_value = cur

    proposal = prepare_reprocess(
        conn, raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )

    assert proposal["availability"]["available"] is True
    assert proposal["creates_new_execution"] is True
    assert proposal["mutates_prior_execution"] is False
    assert proposal["requires_provider_call"] is False
    assert proposal["may_move_published_pointer"] is True
    assert proposal["bound_versions"] == {
        "plan_version_id": "plan_v1",
        "mapping_version_id": "map_v1",
    }
    assert proposal["rollback"]
    # Preparation has no side effect: it read, it did not commit.
    conn.commit.assert_not_called()


def test_an_unavailable_proposal_promises_nothing(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from core.inbound_reprocess import prepare_reprocess

    _patch_raw(monkeypatch, None)
    proposal = prepare_reprocess(
        MagicMock(), raw_import_id="inbraw_x", datastream_id="ds-1"
    )
    assert proposal["availability"]["available"] is False
    assert proposal["creates_new_execution"] is False
    assert proposal["may_move_published_pointer"] is False


# ---------------------------------------------------------------------------
# (g) (h) The execution contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"actor": ""}, "actor"),
        ({"idempotency_key": ""}, "idempotency_key"),
        ({"reason": ""}, "reason"),
        ({"reason": "   "}, "reason"),
    ],
)
def test_a_malformed_request_raises_before_any_work(kwargs, fragment):
    """A reprocess without a stated reason is an unexplained change to data."""
    from unittest.mock import MagicMock

    from core.inbound_reprocess import ReprocessValidationError, execute_reprocess

    base = dict(
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        actor="operator",
        idempotency_key="ik-1",
        reason="mapping repaired",
    )
    base.update(kwargs)
    conn = MagicMock()

    with pytest.raises(ReprocessValidationError, match=fragment):
        execute_reprocess(conn, **base)
    conn.cursor.assert_not_called()


def test_unavailable_evidence_raises_a_coded_refusal(monkeypatch, tmp_path):
    from core.inbound_reprocess import (
        UNAVAILABLE_INTEGRITY,
        ReprocessUnavailable,
        execute_reprocess,
    )

    store = _store(tmp_path)
    uri = _stored(store, data=b"other bytes entirely\n")
    _patch_raw(monkeypatch, _raw_row(quarantine_uri=uri))

    with pytest.raises(ReprocessUnavailable) as exc:
        execute_reprocess(
            object(),
            raw_import_id="inbraw_1",
            datastream_id="ds-1",
            actor="operator",
            idempotency_key="ik-1",
            reason="mapping repaired",
            store=store,
        )
    # A stable code, not a string to parse.
    assert exc.value.code == UNAVAILABLE_INTEGRITY


def _patch_execution(monkeypatch, *, ingest_result=None, scan_accepted=True, stub_scan=True):
    """Wire the operation seam and the pipeline so the mutation really runs."""
    from unittest.mock import MagicMock

    import core.inbound_ingest as ii
    import core.inbound_raw_imports as iri
    from core import operations

    captured: dict = {}

    def execute(operation_conn, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(operation_conn, "op-reprocess-1")
        captured["result"] = changed.result
        return operations.OperationResult(
            "op-reprocess-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    # `execute_reprocess` imports the seam lazily inside the function, so the
    # patch has to land on the SOURCE module, not on a name the module never
    # bound. Patching `core.inbound_reprocess.execute_operation` raises
    # AttributeError and is the first thing to get wrong here.
    monkeypatch.setattr(operations, "execute_operation", execute)
    monkeypatch.setattr(iri, "_resolve_org_id", lambda conn, **kw: "org-1")

    def _ingest(conn, **kwargs):
        captured.setdefault("ingest_calls", []).append(kwargs)
        return ingest_result or {"blocked": False, "ledger": {"id": "mfl_new"}}

    monkeypatch.setattr(ii, "ingest_inbound_file", _ingest)
    current_bundle = {
        "schema": "managed-file-dispatch-bundle-v1",
        "datastream_id": "ds-1",
        "project_id": "proj-1",
        "plan_version_id": "dsp_current",
        "mapping_version_id": "dmap_current",
    }

    def _resolve_current(conn, **kwargs):
        captured["resolved_current_bundle"] = kwargs
        return current_bundle

    monkeypatch.setattr(
        ii,
        "resolve_dispatch_bundle_for_acceptance",
        _resolve_current,
    )
    captured["current_bundle"] = current_bundle

    import core.inbound_scan as isc
    from core.inbound_scan import ScanVerdict

    # LA DOUBLURE EST OPTIONNELLE, et c'est tout l'enjeu. Elle a ete posee sans
    # condition (681c314e), ce qui a fait scanner la VRAIE bombe zip du test
    # ci-dessous par un lambda qui accepte toujours : la story affirmait << le
    # scan 38.10 tourne a nouveau au rejeu, prouve avec une vraie bombe zip >>
    # et plus rien ne le prouvait. Un test qui dit << le scan est le vrai >> doit
    # pouvoir l'obtenir.
    if stub_scan:
        monkeypatch.setattr(
            isc,
            "scan_bytes",
            lambda data, **kw: ScanVerdict(
                accepted=scan_accepted,
                reason=None if scan_accepted else "policy_tightened",
                detected_type="text/csv",
                declared_type="text/csv",
                size_bytes=len(data),
            ),
        )

    # LE FAUX CURSEUR REPOND A LA REQUETE, PAS A SON RANG. Une `side_effect`
    # ordonnee faisait dependre chaque test de l'ordre exact des lectures : y
    # ajouter une lecture ailleurs dans le module rendait rouge un test qui
    # n'exerce pas cette lecture, et le faux devenait la chose la plus fragile du
    # fichier.
    class _Cur:
        description = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            # Dispatch on the NAME, not on the spelling, and RAISE on anything
            # this replay was never taught. The chain used to fall through to
            # `self._one = None`, which is a plausible answer everywhere here.
            statement = _REPLAY.match(sql)
            # Derived, never typed: a hand-written column tuple is a second copy
            # of the product's SELECT, and it is the copy nothing reads.
            self.description = describe(sql)
            self._one = {
                "project": ("proj-1",),
                "config": ({"source_owner": {}},),
                "origin_channel": ("email",),
            }[statement]

        def fetchone(self):
            return getattr(self, "_one", None)

        def fetchall(self):
            raise AssertionError(
                "no statement on this path reads more than one row; "
                f"{_REPLAY.owner} answers `fetchone` only"
            )

    conn = MagicMock()
    conn.cursor.return_value = _Cur()
    return conn, captured


def test_the_message_id_is_derived_from_the_idempotency_key(monkeypatch):
    """The whole of AC5, in one assertion.

    `run_import` is idempotent on message_id. Reusing the ORIGINAL delivery's
    message_id would make every reprocess a silent no-op that looked like a
    success; a random one would make a retried confirmation import twice.
    """
    from core.inbound_reprocess import execute_reprocess

    class MemoryStore:
        def get(self, uri):
            return _CSV

    store = MemoryStore()
    _patch_raw(monkeypatch, _raw_row(quarantine_uri="memory://retained"))
    conn, captured = _patch_execution(monkeypatch)

    out = execute_reprocess(
        conn,
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        actor="operator",
        idempotency_key="ik-same",
        reason="mapping repaired",
        store=store,
    )

    message_id = captured["ingest_calls"][0]["message_id"]
    assert message_id.startswith("reprocess:inbraw_1:")
    # Deterministic in the key: the same confirmation replays the same import.
    expected = hashlib.sha256(b"ik-same").hexdigest()[:16]
    assert message_id.endswith(expected)
    # And it is NOT the original delivery's identifier.
    assert message_id != "inbraw_1"
    assert out["status"] == "landed"
    assert out["import_ledger_id"] == "mfl_new"
    call = captured["ingest_calls"][0]
    assert call["dispatch_bundle_override"] == captured["current_bundle"]
    assert call["force_new_execution"] is True
    assert captured["resolved_current_bundle"] == {
        "datastream_id": "ds-1",
        "project_id": "proj-1",
    }


def test_a_different_key_yields_a_different_execution(monkeypatch, tmp_path):
    from core.inbound_reprocess import execute_reprocess

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw_row(quarantine_uri=uri))

    ids = []
    for key in ("ik-a", "ik-b"):
        conn, captured = _patch_execution(monkeypatch)
        execute_reprocess(
            conn,
            raw_import_id="inbraw_1",
            datastream_id="ds-1",
            actor="operator",
            idempotency_key=key,
            reason="second attempt",
            store=store,
        )
        ids.append(captured["ingest_calls"][0]["message_id"])

    assert ids[0] != ids[1]


def test_the_audit_payload_carries_the_hash_not_the_filename(monkeypatch, tmp_path):
    """A filename is attacker-controlled text; a content hash identifies exactly."""
    from core.inbound_reprocess import execute_reprocess

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(
        monkeypatch, _raw_row(quarantine_uri=uri, filename="=cmd|'/c calc'!A1.csv")
    )
    conn, captured = _patch_execution(monkeypatch)

    execute_reprocess(
        conn,
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        actor="operator",
        idempotency_key="ik-1",
        reason="mapping repaired",
        store=store,
    )

    payload = captured["spec"].request_payload
    assert payload["content_hash"] == _HASH
    assert payload["reason"] == "mapping repaired"
    assert "filename" not in payload
    assert "cmd" not in str(payload)


def test_the_reprocess_reuses_the_original_channel(monkeypatch, tmp_path):
    """A webhook file must not slip back in as an email one.

    The Datastream's channel allowlist is re-checked downstream, so inventing a
    channel here would either bypass that check or fail it for the wrong reason.
    """
    from core.inbound_reprocess import execute_reprocess

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw_row(quarantine_uri=uri))
    conn, captured = _patch_execution(monkeypatch)

    execute_reprocess(
        conn,
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        actor="operator",
        idempotency_key="ik-1",
        reason="mapping repaired",
        store=store,
    )
    assert captured["ingest_calls"][0]["channel"] == "email"


# ---------------------------------------------------------------------------
# (i) The scan runs AGAIN.
# ---------------------------------------------------------------------------


def test_the_scan_runs_again_on_reprocess(monkeypatch, tmp_path):
    """Bounds are policy, and policy changes.

    A file accepted under last month's limits must not bypass this month's
    simply because a verdict was stored at receipt time.
    """
    from core.inbound_reprocess import execute_reprocess

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw_row(quarantine_uri=uri))
    conn, captured = _patch_execution(monkeypatch, scan_accepted=False)

    out = execute_reprocess(
        conn,
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        actor="operator",
        idempotency_key="ik-1",
        reason="retry under current policy",
        store=store,
    )

    assert out["status"] == "rejected"
    assert out["error_code"] == "policy_tightened"
    assert out["import_ledger_id"] is None
    # Refused before the pipeline, not after.
    assert "ingest_calls" not in captured


def test_a_bomb_that_slipped_in_earlier_is_refused_on_reprocess(
    monkeypatch, tmp_path
):
    """The scan is the real one here, not a stub -- with a real archive."""
    from core.inbound_reprocess import execute_reprocess

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("bomb.csv", b"0" * (8 * 1024 * 1024))
    bomb = buf.getvalue()

    store = _store(tmp_path)
    uri = _stored(store, data=bomb, filename="bomb.zip")
    _patch_raw(
        monkeypatch,
        _raw_row(
            quarantine_uri=uri,
            content_hash=hashlib.sha256(bomb).hexdigest(),
            media_type_declared="application/zip",
            size_bytes=len(bomb),
        ),
    )
    conn, captured = _patch_execution(monkeypatch, stub_scan=False)

    out = execute_reprocess(
        conn,
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        actor="operator",
        idempotency_key="ik-1",
        reason="retry",
        store=store,
    )

    assert out["status"] == "rejected"
    assert out["error_code"] in (
        "archive_compression_ratio_exceeded",
        "archive_uncompressed_too_large",
    )
    assert "ingest_calls" not in captured


# ---------------------------------------------------------------------------
# Mounting. Same reason as 38.14: a command nothing routes to is not a command.
# ---------------------------------------------------------------------------


def test_the_reprocess_routes_are_mounted_with_both_verbs():
    from core.admin_api import router

    path = (
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/raw-imports/{raw_import_id}/reprocess"
    )
    verbs = {
        m
        for r in router.routes
        if getattr(r, "path", None) == path
        for m in (r.methods or set())
        if m in ("GET", "POST")
    }
    # GET prepares without side effect; POST commits. Both, or the story is
    # only half reachable.
    assert verbs == {"GET", "POST"}, verbs


# ---------------------------------------------------------------------------
# Story 57.3 -- a replay is not a way around the declared sender allowlist.
#
# THE CASE THE LIST EXISTS FOR. One declares a sender allowlist after seeing an
# arrival one did not want; the unwanted file is already retained, so the only
# thing left to stop is its replay. The first version of the policy guarded only
# the arrival path, which left this one open -- a list with a way around it is
# not a list.
#
# The guard is NOT duplicated here. It lives in `ingest_inbound_file`, the point
# this path and the arrival path share. What this file proves is that the replay
# really reaches it, carrying the one thing the guard needs: the `raw_import_id`
# that resolves the receipt the sender was recorded on.
# ---------------------------------------------------------------------------


def test_a_replay_hands_the_guard_the_evidence_it_needs(monkeypatch, tmp_path):
    """`raw_import_id` travels, or the sender can never be resolved.

    `read_delivery_sender` joins raw import -> receipt. Dropping this argument
    would make every replay look like a delivery with no sender -- which a
    declared list refuses, so the failure would be silent AND wrong.
    """
    from core.inbound_reprocess import execute_reprocess

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw_row(quarantine_uri=uri))
    conn, captured = _patch_execution(monkeypatch)

    execute_reprocess(
        conn,
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        actor="operator",
        idempotency_key="ik-sender",
        reason="mapping repaired",
        store=store,
    )

    assert captured["ingest_calls"][0]["raw_import_id"] == "inbraw_1"


def test_a_replay_refused_by_the_sender_allowlist_imports_nothing(monkeypatch, tmp_path):
    """The refusal reaches the operator NAMED, and no ledger row is produced.

    An operator replaying a file they no longer accept must be told which of the
    two things refused it -- the mapping they repaired, or the sender they since
    narrowed.
    """
    import core.inbound_ingest as ii
    from core.inbound_ingest import SenderNotAllowed
    from core.inbound_reprocess import execute_reprocess

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw_row(quarantine_uri=uri))
    conn, _captured = _patch_execution(monkeypatch)

    def _refuse(conn, **kwargs):
        raise SenderNotAllowed(
            "the sender of this delivery is not in the declared allowlist of "
            "datastream 'ds-1'; nothing was imported"
        )

    monkeypatch.setattr(ii, "ingest_inbound_file", _refuse)

    with pytest.raises(SenderNotAllowed) as refused:
        execute_reprocess(
            conn,
            raw_import_id="inbraw_1",
            datastream_id="ds-1",
            actor="operator",
            idempotency_key="ik-refused",
            reason="mapping repaired",
            store=store,
        )

    assert refused.value.code == "sender_not_allowed"
    # No address in the refusal: it travels into logs and operator surfaces.
    assert "@" not in str(refused.value)


def test_the_reason_survives_and_the_refusal_is_actionable(monkeypatch, tmp_path):
    """Exiger une justification puis la jeter, c'est ecrire pour personne.

    Le module REFUSE un rejeu sans raison (`execute_reprocess`, garde
    `reason is required`) et la met
    dans le `request_payload` -- mais `execute_operation` ne stocke que le
    `request_hash` et ecrit `'{}'` en metadata d'audit. Aucune ligne ne portait
    la phrase. `app.operations.result` est durable et relu au rejeu : c'est la
    que la raison de CE rejeu appartient.

    Et ce qui arrive a l'operateur doit etre ACTIONNABLE : `run_import` construit
    un `repair` a l'endroit exact du refus (<< ajoute des lignes >>, << rebranche
    le Template >>), et le rejeu ne rendait qu'un code. Un fichier refuse avec
    `empty_import_blocked` et rien d'autre laisse la personne devant le meme
    fichier sans savoir quoi en faire.
    """
    from core.inbound_reprocess import execute_reprocess

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw_row(quarantine_uri=uri))
    conn, captured = _patch_execution(
        monkeypatch,
        ingest_result={
            "blocked": True,
            "reason": "empty_import_blocked",
            "repair": {"add_data_rows": True},
        },
    )

    out = execute_reprocess(
        conn,
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        actor="operator",
        idempotency_key="ik-reason",
        reason="le fournisseur a renvoye le fichier corrige",
        store=store,
    )

    assert out["reason"] == "le fournisseur a renvoye le fichier corrige"
    assert out["repair"] == {"add_data_rows": True}
    assert out["error_code"] == "empty_import_blocked"
