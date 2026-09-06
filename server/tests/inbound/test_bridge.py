"""Inbound bridge tests (Epic 38).

Offline, no pg/GCS. Exercises the Pub/Sub PUSH bridge through Starlette's
TestClient (same pattern as test_receipt_handler.py). All boundaries are
monkeypatched:

  * ``core.inbound_quarantine.open_quarantine_store`` -> a fake store whose
    ``.get`` returns a known manifest JSON (never hits GCS);
  * ``core.inbound_processing.process_inbound_delivery`` -> a spy;
  * ``core.db.get_connection`` -> a fake connection context manager that records
    commit / rollback.

Covered:
  * wrong secret (when configured) -> constant-shape 403; unset secret ->
    proceeds under platform IAM;
  * non-manifest object name -> 204 ignored, worker NOT called;
  * valid manifest push in BOTH the attributes form and the base64-data form ->
    worker called once with the manifest dict, 200, conn.commit() called;
  * worker raises -> 500 + conn.rollback();
  * malformed envelope -> 400;
  * no secret/token leaks into any response body.
"""

from __future__ import annotations

import base64
import json

import pytest
from inbound.bridge import build_bridge_app
from starlette.testclient import TestClient

_WORKER_SECRET = "test-worker-secret-DO-NOT-USE-IN-PROD"
_PROCESS_PATH = "/v1/internal/inbound-process"
_BUCKET = "toorow-dev-inbound-quarantine"
_MANIFEST_NAME = "inbound/deadbeefhash/msg-1/_manifest.json"
_ATTACHMENT_NAME = "inbound/deadbeefhash/msg-1/data.xlsx"

_FORBIDDEN_BODY = {"code": "forbidden", "message": "forbidden"}

# A realistic manifest (schema shared with core.inbound_processing).
_MANIFEST = {
    "schema": "inbound-delivery-manifest-v1",
    "provider_event_id": "evt-123",
    "channel": "email",
    "token_hash": "deadbeefhash",
    "recipient_hash": "recipienthash",
    "received_at": "2026-07-25T00:00:00+00:00",
    "attachments": [
        {
            "filename": "data.xlsx",
            "quarantine_uri": f"gs://{_BUCKET}/{_ATTACHMENT_NAME}",
            "size": 1024,
            "content_type": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        }
    ],
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStore:
    """Fake QuarantineStore: ``.get`` returns the known manifest bytes."""

    def __init__(self, manifest: dict) -> None:
        self._bytes = json.dumps(manifest).encode("utf-8")
        self.get_calls: list[str] = []

    def get(self, uri: str) -> bytes:
        self.get_calls.append(uri)
        return self._bytes

    def get_bounded(self, uri: str, *, max_bytes: int, **kwargs) -> bytes:
        assert len(self._bytes) <= max_bytes
        return self.get(uri)


class _FakeConn:
    """Fake DB connection recording commit / rollback."""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class _FakeConnCtx:
    """Context manager yielding a single _FakeConn (mirrors get_connection)."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeConn:
        return self._conn

    def __exit__(self, *exc) -> bool:
        return False


class _Spy:
    """Records calls to process_inbound_delivery; configurable outcome."""

    def __init__(self, *, result=None, raises: Exception | None = None) -> None:
        self.result = (
            result
            if result is not None
            else [
                {
                    "job_id": "inbscan_" + "a" * 24,
                    "state": "QUEUED",
                    "task_name": None,
                }
            ]
        )
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, conn, **kwargs):
        self.calls.append({"conn": conn, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.result


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _worker_secret_env(monkeypatch):
    monkeypatch.setenv("INBOUND_WORKER_SECRET", _WORKER_SECRET)


def _client() -> TestClient:
    return TestClient(build_bridge_app(), raise_server_exceptions=False)


def _wire(
    monkeypatch,
    *,
    spy: _Spy | None = None,
    conn: _FakeConn | None = None,
    store: _FakeStore | None = None,
):
    """Monkeypatch the core boundaries the bridge lazily imports.

    Patched on the ORIGIN modules (the bridge imports them by module inside the
    handler, so the lookups resolve to these attributes at call time).
    """
    import core.db as db_mod
    import core.inbound_quarantine as quar_mod
    import core.inbound_scan_jobs as jobs_mod

    spy = spy or _Spy()
    conn = conn or _FakeConn()
    store = store or _FakeStore(_MANIFEST)

    monkeypatch.setattr(jobs_mod, "enqueue_manifest_jobs", spy)
    monkeypatch.setattr(jobs_mod, "dispatch_job", lambda conn, job_id: "task")
    monkeypatch.setattr(quar_mod, "open_quarantine_store", lambda *a, **k: store)
    monkeypatch.setattr(db_mod, "get_connection", lambda: _FakeConnCtx(conn))
    return spy, conn, store


def _attributes_envelope(
    *, bucket: str = _BUCKET, name: str = _MANIFEST_NAME, message_id: str = "pubsub-msg-1"
) -> dict:
    return {
        "message": {
            "attributes": {"bucketId": bucket, "objectId": name},
            "messageId": message_id,
        },
        "subscription": "projects/toorow-dev/subscriptions/inbound-bridge",
    }


def _data_envelope(
    *, bucket: str = _BUCKET, name: str = _MANIFEST_NAME, message_id: str = "pubsub-msg-2"
) -> dict:
    payload = base64.b64encode(json.dumps({"bucket": bucket, "name": name}).encode("utf-8")).decode(
        "ascii"
    )
    return {
        "message": {"data": payload, "messageId": message_id},
        "subscription": "projects/toorow-dev/subscriptions/inbound-bridge",
    }


def _hdr(secret: str = _WORKER_SECRET) -> dict:
    return {"X-Inbound-Worker-Secret": secret}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_secret_header_is_403(self, monkeypatch):
        _wire(monkeypatch)
        resp = _client().post(_PROCESS_PATH, json=_attributes_envelope())
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_wrong_secret_header_is_403(self, monkeypatch):
        spy, _, _ = _wire(monkeypatch)
        resp = _client().post(_PROCESS_PATH, json=_attributes_envelope(), headers=_hdr("nope"))
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY
        assert spy.calls == []

    def test_managed_iam_mode_explicitly_bypasses_the_self_hosted_secret(self, monkeypatch):
        # Cloud Run IAM/OIDC has already authenticated the managed request.
        spy, _, _ = _wire(monkeypatch)
        monkeypatch.setenv("INBOUND_REQUIRE_WORKER_SECRET", "false")
        monkeypatch.delenv("INBOUND_WORKER_SECRET", raising=False)
        resp = _client().post(_PROCESS_PATH, json=_attributes_envelope())
        assert resp.status_code == 200
        assert len(spy.calls) == 1

    def test_self_hosted_default_fails_closed_when_secret_is_unset(self, monkeypatch):
        spy, _, _ = _wire(monkeypatch)
        monkeypatch.delenv("INBOUND_WORKER_SECRET", raising=False)
        resp = _client().post(_PROCESS_PATH, json=_attributes_envelope())
        assert resp.status_code == 403
        assert spy.calls == []

    def test_403_body_leaks_no_secret(self, monkeypatch):
        _wire(monkeypatch)
        resp = _client().post(_PROCESS_PATH, json=_attributes_envelope(), headers=_hdr("nope"))
        assert _WORKER_SECRET not in resp.text


# ---------------------------------------------------------------------------
# Filter: non-manifest object -> 204 ignored, worker not called
# ---------------------------------------------------------------------------


class TestIgnoreNonManifest:
    def test_attachment_object_is_204_ignored(self, monkeypatch):
        spy, conn, _ = _wire(monkeypatch)
        env = _attributes_envelope(name=_ATTACHMENT_NAME)
        resp = _client().post(_PROCESS_PATH, json=env, headers=_hdr())
        assert resp.status_code == 204
        assert spy.calls == []
        assert conn.committed is False


# ---------------------------------------------------------------------------
# Happy path: worker called once, 200, commit
# ---------------------------------------------------------------------------


class TestProcess:
    def test_attributes_form_invokes_worker_and_commits(self, monkeypatch):
        spy, conn, store = _wire(monkeypatch)
        resp = _client().post(_PROCESS_PATH, json=_attributes_envelope(), headers=_hdr())
        assert resp.status_code == 200
        assert resp.json() == {"status": "queued", "jobs": 1}
        assert len(spy.calls) == 1
        assert spy.calls[0]["manifest"] == _MANIFEST
        assert spy.calls[0]["manifest_uri"].endswith("/_manifest.json")
        assert conn.committed is True
        assert conn.rolled_back is False
        # Read from the correct gs:// uri (bucket + object name).
        assert store.get_calls == [f"gs://{_BUCKET}/{_MANIFEST_NAME}"]

    def test_base64_data_form_invokes_worker_and_commits(self, monkeypatch):
        spy, conn, store = _wire(monkeypatch)
        resp = _client().post(_PROCESS_PATH, json=_data_envelope(), headers=_hdr())
        assert resp.status_code == 200
        assert len(spy.calls) == 1
        assert spy.calls[0]["manifest"] == _MANIFEST
        assert conn.committed is True
        assert store.get_calls == [f"gs://{_BUCKET}/{_MANIFEST_NAME}"]

    def test_a_manifest_with_no_jobs_is_acknowledged(self, monkeypatch):
        spy = _Spy(result=[])
        _wire(monkeypatch, spy=spy)
        resp = _client().post(_PROCESS_PATH, json=_attributes_envelope(), headers=_hdr())
        assert resp.status_code == 200
        assert resp.json() == {"status": "queued", "jobs": 0}


# ---------------------------------------------------------------------------
# Worker raises -> 500 + rollback
# ---------------------------------------------------------------------------


class TestWorkerFailure:
    def test_worker_exception_is_500_and_rolls_back(self, monkeypatch):
        spy = _Spy(raises=RuntimeError("boom SECRET-LEAK-token"))
        _, conn, _ = _wire(monkeypatch, spy=spy)
        resp = _client().post(_PROCESS_PATH, json=_attributes_envelope(), headers=_hdr())
        assert resp.status_code == 500
        assert conn.rolled_back is True
        assert conn.committed is False
        # Generic body -- must not leak the exception message / token.
        assert "SECRET-LEAK-token" not in resp.text
        assert resp.json() == {"code": "internal", "message": "internal"}


# ---------------------------------------------------------------------------
# Permanently malformed envelope -> poison ACK 204
# ---------------------------------------------------------------------------


class TestMalformed:
    def test_non_json_body_is_acknowledged_204(self, monkeypatch):
        _wire(monkeypatch)
        resp = _client().post(_PROCESS_PATH, data="not json", headers=_hdr())
        assert resp.status_code == 204

    def test_envelope_without_object_is_acknowledged_204(self, monkeypatch):
        spy, _, _ = _wire(monkeypatch)
        # A valid JSON object but no attributes and no data -> cannot derive.
        env = {"message": {"messageId": "x"}, "subscription": "s"}
        resp = _client().post(_PROCESS_PATH, json=env, headers=_hdr())
        assert resp.status_code == 204
        assert spy.calls == []

    def test_envelope_not_object_is_acknowledged_204(self, monkeypatch):
        _wire(monkeypatch)
        resp = _client().post(_PROCESS_PATH, json=[1, 2, 3], headers=_hdr())
        assert resp.status_code == 204


class TestDurableScanRoutes:
    def test_task_retry_commits_claim_and_scanning_intent_before_failure(self, monkeypatch):
        import core.db as db_mod
        import core.inbound_processing as processing
        import core.inbound_quarantine as quarantine
        import core.inbound_scan_jobs as jobs_mod
        from core.inbound_scan import REASON_MALWARE_UNAVAILABLE, ScanRetryableError

        events: list[str] = []

        class EventConn(_FakeConn):
            def commit(self):
                events.append("commit")
                super().commit()

        conn = EventConn()
        store = _FakeStore(_MANIFEST)
        monkeypatch.setattr(db_mod, "get_connection", lambda: _FakeConnCtx(conn))
        monkeypatch.setattr(quarantine, "open_quarantine_store", lambda: store)
        monkeypatch.setattr(
            jobs_mod,
            "claim_job",
            lambda conn, job_id: (
                events.append("claim")
                or {
                    "job_id": job_id,
                    "manifest_uri": "gs://bucket/manifest",
                    "ordinal": 0,
                    "receipt_id": "inbrx_1",
                    "datastream_id": "ds-1",
                    "trace_id": None,
                }
            ),
        )

        def fail(conn, **kwargs):
            events.append("fail_attempt")
            return "retry_wait"

        monkeypatch.setattr(jobs_mod, "fail_attempt", fail)
        monkeypatch.setattr(jobs_mod, "finish_job", lambda *args, **kwargs: None)
        monkeypatch.setattr(jobs_mod, "reconcile_receipt", lambda *args, **kwargs: None)

        def process(conn, **kwargs):
            events.append("process")
            assert kwargs["persist_scan_intent"] is True
            raise ScanRetryableError(REASON_MALWARE_UNAVAILABLE)

        monkeypatch.setattr(processing, "process_inbound_delivery", process)
        response = _client().post(
            "/v1/internal/inbound-scan-task",
            json={"job_id": "inbscan_" + "a" * 24},
            headers=_hdr(),
        )
        assert response.status_code == 500
        assert events[:3] == ["claim", "commit", "process"]
        assert "fail_attempt" in events

    def test_exhausted_task_is_acked_only_after_dead_letter_reconciliation(self, monkeypatch):
        import core.db as db_mod
        import core.inbound_processing as processing
        import core.inbound_quarantine as quarantine
        import core.inbound_scan_jobs as jobs_mod
        from core.inbound_scan import REASON_SCAN_TIMEOUT, ScanRetryableError

        conn = _FakeConn()
        monkeypatch.setattr(db_mod, "get_connection", lambda: _FakeConnCtx(conn))
        monkeypatch.setattr(quarantine, "open_quarantine_store", lambda: _FakeStore(_MANIFEST))
        monkeypatch.setattr(
            jobs_mod,
            "claim_job",
            lambda conn, job_id: {
                "job_id": job_id,
                "manifest_uri": "gs://bucket/manifest",
                "ordinal": 0,
                "receipt_id": "inbrx_1",
                "datastream_id": "ds-1",
                "trace_id": None,
            },
        )
        monkeypatch.setattr(
            processing,
            "process_inbound_delivery",
            lambda *args, **kwargs: (_ for _ in ()).throw(ScanRetryableError(REASON_SCAN_TIMEOUT)),
        )
        monkeypatch.setattr(jobs_mod, "fail_attempt", lambda *args, **kwargs: "dead_letter")
        reconciled: list[str] = []
        monkeypatch.setattr(
            jobs_mod,
            "reconcile_receipt",
            lambda conn, receipt_id, trace_id: reconciled.append(receipt_id),
        )
        monkeypatch.setattr(jobs_mod, "finish_job", lambda *args, **kwargs: None)
        response = _client().post(
            "/v1/internal/inbound-scan-task",
            json={"job_id": "inbscan_" + "b" * 24},
            headers=_hdr(),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "dead_letter"
        assert reconciled == ["inbrx_1"]

    def test_scheduler_route_recovers_stale_and_lost_dispatched_tasks(self, monkeypatch):
        import core.db as db_mod
        import core.inbound_scan_jobs as jobs_mod

        conn = _FakeConn()
        monkeypatch.setattr(db_mod, "get_connection", lambda: _FakeConnCtx(conn))
        stale: list[bool] = []
        dispatched: list[str] = []
        monkeypatch.setattr(jobs_mod, "reconcile_stale_jobs", lambda conn: stale.append(True) or 1)
        monkeypatch.setattr(
            jobs_mod,
            "queued_without_task",
            lambda conn, limit: ["inbscan_" + "c" * 24, "inbscan_" + "d" * 24],
        )
        monkeypatch.setattr(
            jobs_mod,
            "dispatch_job",
            lambda conn, job_id: dispatched.append(job_id) or f"task/{job_id}",
        )
        response = _client().post("/v1/internal/inbound-scan-reconcile", json={}, headers=_hdr())
        assert response.status_code == 200
        assert response.json()["dispatched"] == 2
        assert stale == [True]
        assert len(dispatched) == 2
