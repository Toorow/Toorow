"""Story 38.13: three real ingress entries, one governed pipeline."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any

import pytest

PROJECT_ID = "proj-closure"
DATASTREAM_ID = "ds-closure"
PLAN_ID = "dsp-closure-v1"
MAPPING_ID = "dmap-closure-v1"
LKG_ID = "dse-last-known-good"


def _mapping() -> dict[str, Any]:
    field_types = {
        "day": "date",
        "spend": "decimal",
        "clicks": "integer",
        "active": "boolean",
    }
    return {
        "fields": [
            {
                "field_id": source,
                "physical_type": physical_type,
                "binding": {
                    "status": "confirmed",
                    "canonical_target": f"canonical_{source}",
                },
            }
            for source, physical_type in field_types.items()
        ]
    }


def _projection() -> dict[str, Any]:
    return {
        "executable": True,
        "plan_version_id": PLAN_ID,
        "mapping_version_id": MAPPING_ID,
        "projection_contract_version": "1",
        "source_schema_hash": "source-schema-v1",
        "capability_fingerprint": "capability-v1",
        "full_grain_relation": {
            "grain_columns": [{"field_id": "day", "type_class": "date"}],
            "grain_key": "grain_key",
            "source_fields": ["active", "clicks", "spend"],
            "provenance": {},
            "scope_columns": ["project_id"],
        },
        "additive_measures": [
            {"field_id": "clicks"},
            {"field_id": "spend"},
        ],
        "governed_dimension_projection": None,
    }


def _dispatch_bundle() -> dict[str, Any]:
    return {
        "schema": "managed-file-dispatch-bundle-v1",
        "datastream_id": DATASTREAM_ID,
        "project_id": PROJECT_ID,
        "plan_version_id": PLAN_ID,
        "mapping_version_id": MAPPING_ID,
        "projection_plan": _projection(),
        "mapping_payload": _mapping(),
        "governed_evidence": {
            "mapping_fingerprint": "mapping-fingerprint-v1",
            "source_schema_hash": "source-schema-v1",
            "capability_fingerprint": "capability-v1",
            "plan_fingerprint": "plan-fingerprint-v1",
            "plan_capability_fingerprint": "capability-v1",
            "plan_contract_version": "1",
        },
        "parser": {
            "contract": {
                "format": "csv",
                "delimiter": ",",
                "encoding": "utf-8",
                "header_row": 1,
                "write_mode": "replace",
                "column_types": {
                    "day": "date",
                    "spend": "decimal",
                    "clicks": "integer",
                    "active": "boolean",
                },
            },
            "contract_id": "cic-closure-v1",
            "contract_fingerprint": "parser-fingerprint-v1",
            "confirmed_by": "member-closure",
            "confirmed_at": "2026-08-02T10:00:00+00:00",
        },
        "template": None,
    }


@dataclass
class _StoredObject:
    uri: str
    size: int


class _MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.ordinal = 0

    def put(self, *, data: bytes, **_kwargs: Any) -> _StoredObject:
        self.ordinal += 1
        digest = hashlib.sha256(data).hexdigest()
        uri = f"memory://closure/{self.ordinal}/{digest}"
        self.objects[uri] = data
        return _StoredObject(uri=uri, size=len(data))

    def get_bounded(self, uri: str, *, max_bytes: int, **_kwargs: Any) -> bytes:
        data = self.objects[uri]
        if len(data) > max_bytes:
            raise AssertionError("fixture unexpectedly exceeded scanner bound")
        return data


class _Connection:
    def __init__(self, harness: "_Harness") -> None:
        self.harness = harness

    def commit(self) -> None:
        self.harness.commits += 1

    def rollback(self) -> None:
        self.harness.rollbacks += 1


class _CleanScanner:
    version = "closure-clean-scanner-v1"

    def __call__(self, _data: bytes) -> tuple[str, str]:
        return "clean", "closure-signatures-v1"


class _Harness:
    def __init__(self) -> None:
        self.bundle = _dispatch_bundle()
        self.conn = _Connection(self)
        self.store = _MemoryStore()
        self.channel = "upload"
        self.receipt_id = ""
        self.reject_dq = False
        self.run_number = 0
        self.commits = 0
        self.rollbacks = 0
        self.current_pointer = LKG_ID
        self.raw: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.ledgers: dict[str, dict[str, Any]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.dispatches: dict[str, dict[str, Any]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.events: list[tuple[str, str]] = []
        self.accepted_bundles: list[dict[str, Any]] = []
        self.intent_bundles: list[dict[str, Any]] = []

    def start(self, channel: str, *, reject_dq: bool = False) -> None:
        self.channel = channel
        self.reject_dq = reject_dq
        self.run_number += 1
        self.receipt_id = f"inbrx-{channel}-{self.run_number}"

    @property
    def execution_id(self) -> str:
        return f"dse-{self.channel}-{self.run_number}"

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core import (
            datastream_publication,
            import_runner,
            inbound_credentials,
            inbound_discovery,
            inbound_ingest,
            inbound_processing,
            inbound_raw_imports,
            inbound_receipts,
            inbound_scan,
            managed_feed_ledger,
            managed_file_dispatch,
            raw_landing,
        )

        rows_fingerprint = raw_landing.candidate_rows_fingerprint
        schema_fingerprint = raw_landing.candidate_schema_fingerprint

        monkeypatch.setattr(
            inbound_credentials,
            "resolve_by_token_hash",
            lambda _conn, *, token_hash: {
                "allowed": bool(token_hash),
                "scope": {
                    "datastream_id": DATASTREAM_ID,
                    "credential_id": "dic-closure",
                    "channel": self.channel,
                    "version": 1,
                },
            },
        )
        monkeypatch.setattr(
            inbound_processing,
            "_resolve_project_id",
            lambda _conn, *, datastream_id: (
                PROJECT_ID if datastream_id == DATASTREAM_ID else None
            ),
        )
        monkeypatch.setattr(
            inbound_processing,
            "_resolve_org_id",
            lambda _conn, *, datastream_id: (
                "org-closure" if datastream_id == DATASTREAM_ID else None
            ),
        )
        monkeypatch.setattr(
            inbound_discovery,
            "delivery_posture",
            lambda _conn, *, datastream_id: "ingest",
        )
        monkeypatch.setattr(
            inbound_scan,
            "configured_malware_scanner",
            lambda **_kwargs: _CleanScanner(),
        )

        def record_receipt(_conn, **kwargs: Any) -> dict[str, Any]:
            receipt = {
                "receipt_id": self.receipt_id,
                "state": "RECEIVED",
                "deduplicated": False,
                **kwargs,
            }
            self.receipts[self.receipt_id] = receipt
            return {"receipt_id": self.receipt_id, "deduplicated": False}

        def get_receipt(
            _conn, *, receipt_id: str, datastream_id: str
        ) -> dict[str, Any]:
            assert datastream_id == DATASTREAM_ID
            return dict(self.receipts[receipt_id])

        def mark_receipt(
            _conn, *, receipt_id: str, state: str, **kwargs: Any
        ) -> dict[str, Any]:
            receipt = self.receipts[receipt_id]
            receipt.update(state=state, **kwargs)
            self.events.append((f"receipt:{state.lower()}", receipt_id))
            return dict(receipt)

        monkeypatch.setattr(inbound_receipts, "record_receipt", record_receipt)
        monkeypatch.setattr(inbound_receipts, "get_receipt", get_receipt)
        monkeypatch.setattr(inbound_receipts, "mark_state", mark_receipt)

        def record_raw(
            _conn, *, receipt_id: str, ordinal: int, **kwargs: Any
        ) -> dict[str, Any]:
            raw_id = f"inbraw-{receipt_id}-{ordinal}"
            row = self.raw.setdefault(
                raw_id,
                {
                    "raw_import_id": raw_id,
                    "state": "RECEIVED",
                    "deduplicated": False,
                    "receipt_id": receipt_id,
                    **kwargs,
                },
            )
            return dict(row)

        def mark_raw(
            _conn, *, raw_import_id: str, state: str, **kwargs: Any
        ) -> dict[str, Any]:
            row = self.raw[raw_import_id]
            row.update(state=state, **kwargs)
            if state == "ACCEPTED":
                frozen = copy.deepcopy(kwargs.get("dispatch_bundle"))
                assert frozen is not None
                row["dispatch_bundle"] = frozen
                row["dispatch_bundle_fingerprint"] = (
                    managed_file_dispatch.fingerprint_bundle(frozen)
                )
                self.accepted_bundles.append(frozen)
            self.events.append((f"raw:{state.lower()}", raw_import_id))
            return dict(row)

        monkeypatch.setattr(inbound_raw_imports, "record_raw_import", record_raw)
        monkeypatch.setattr(inbound_raw_imports, "mark_raw_import_state", mark_raw)
        monkeypatch.setattr(
            inbound_raw_imports,
            "duplicate_content_decision",
            lambda *_args, **_kwargs: {
                "policy_version": "skip-exact-v1",
                "skip_execution": False,
                "duplicate_of_raw_import_id": None,
            },
        )
        monkeypatch.setattr(
            inbound_ingest,
            "resolve_dispatch_bundle_for_acceptance",
            lambda _conn, **_kwargs: copy.deepcopy(self.bundle),
        )
        monkeypatch.setattr(
            inbound_ingest,
            "_load_frozen_dispatch_bundle",
            lambda _conn, *, raw_import_id, **_kwargs: copy.deepcopy(
                self.raw[raw_import_id]["dispatch_bundle"]
            ),
        )
        monkeypatch.setattr(
            inbound_ingest,
            "_load_ingestable_datastream",
            lambda _conn, **_kwargs: {
                "source_kind": "managed_feed",
                "enabled": True,
                "config": {"channels": ["upload", "email", "webhook"]},
                "current_plan_version_id": PLAN_ID,
                "current_mapping_version_id": MAPPING_ID,
            },
        )
        monkeypatch.setattr(
            inbound_ingest,
            "_read_allow_empty_publication",
            lambda _conn, _project_id: False,
        )
        monkeypatch.setattr(
            import_runner,
            "version_contract",
            lambda *_args, **_kwargs: "cic-closure-v1",
        )

        def open_import(**_kwargs: Any) -> dict[str, Any]:
            execution_id = self.execution_id
            ledger_id = f"mfl-{self.channel}-{self.run_number}"
            execution = {
                "id": execution_id,
                "datastream_id": DATASTREAM_ID,
                "project_id": PROJECT_ID,
                "state": "created",
            }
            ledger = {
                "id": ledger_id,
                "execution_id": execution_id,
                "outcome": "opened",
                "row_count": None,
                "rejected_row_count": None,
            }
            self.executions[execution_id] = execution
            self.ledgers[ledger_id] = ledger
            self.events.append(("ledger:opened", execution_id))
            return {
                "ledger": dict(ledger),
                "execution": dict(execution),
                "no_op": False,
            }

        def record_rows(
            *,
            ledger_id: str,
            landing_relation: str,
            accepted_row_count: int,
            rejected_rows: list[dict[str, Any]],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            ledger = self.ledgers[ledger_id]
            ledger.update(
                outcome="written",
                row_count=accepted_row_count,
                rejected_row_count=len(rejected_rows),
                landing_relation=landing_relation,
            )
            self.events.append(("ledger:written", ledger["execution_id"]))
            return dict(ledger)

        def mark_outcome(
            *, ledger_id: str, outcome: str, **kwargs: Any
        ) -> dict[str, Any]:
            ledger = self.ledgers[ledger_id]
            ledger.update(outcome=outcome, **kwargs)
            return dict(ledger)

        monkeypatch.setattr(managed_feed_ledger, "open_import", open_import)
        monkeypatch.setattr(
            managed_feed_ledger,
            "allocate_landing_relation",
            lambda **_kwargs: "raw_project.managed_feed_closure",
        )
        monkeypatch.setattr(managed_feed_ledger, "record_rows", record_rows)
        monkeypatch.setattr(
            managed_feed_ledger,
            "evaluate_rejection_gate_for_ledger",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(managed_feed_ledger, "mark_outcome", mark_outcome)

        def record_intent(
            _conn,
            *,
            ledger_id: str,
            execution_id: str,
            bundle: dict[str, Any],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            dispatch_id = f"mfd-{self.channel}-{self.run_number}"
            dispatch = {
                "id": dispatch_id,
                "ledger_id": ledger_id,
                "execution_id": execution_id,
                "state": "pending",
                "bundle": copy.deepcopy(bundle),
                "bundle_fingerprint": (
                    managed_file_dispatch.fingerprint_bundle(bundle)
                ),
            }
            self.dispatches[dispatch_id] = dispatch
            self.intent_bundles.append(copy.deepcopy(bundle))
            self.events.append(("dispatch:pending", execution_id))
            return copy.deepcopy(dispatch)

        def advance_dispatch(
            _conn, *, dispatch_id: str, state: str, **evidence: Any
        ) -> dict[str, Any]:
            dispatch = self.dispatches[dispatch_id]
            dispatch["state"] = state
            dispatch.update(
                {key: value for key, value in evidence.items() if value is not None}
            )
            self.events.append((f"dispatch:{state}", dispatch["execution_id"]))
            return copy.deepcopy(dispatch)

        monkeypatch.setattr(
            managed_file_dispatch, "record_intent", record_intent
        )
        monkeypatch.setattr(
            managed_file_dispatch, "advance_dispatch", advance_dispatch
        )

        def land_raw_rows(
            table: str,
            rows: list[dict[str, Any]],
            *,
            columns,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            execution_id = raw_landing.active_candidate_execution()
            assert execution_id is not None
            self.candidates[execution_id] = {
                "rows": copy.deepcopy(rows),
                "columns": list(columns),
                "table": table,
                "discarded": False,
                "promoted": False,
            }
            self.events.append(("warehouse:candidate", execution_id))
            return {
                "rows": len(rows),
                "backend": "memory",
                "mode": None,
                "table": table,
                "execution_id": execution_id,
            }

        def inspect_candidate(
            _table: str, execution_id: str, *, columns, **_kwargs: Any
        ) -> dict[str, Any]:
            candidate = self.candidates[execution_id]
            assert list(columns) == candidate["columns"]
            return {
                "row_count": len(candidate["rows"]),
                "content_fingerprint": rows_fingerprint(
                    candidate["rows"], columns=candidate["columns"]
                ),
                "schema_fingerprint": schema_fingerprint(candidate["columns"]),
            }

        def promote_candidate(
            _table: str,
            execution_id: str,
            *,
            columns,
            expected_rows: int,
            expected_content_fingerprint: str,
            expected_schema_fingerprint: str,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            proof = inspect_candidate("", execution_id, columns=columns)
            assert proof == {
                "row_count": expected_rows,
                "content_fingerprint": expected_content_fingerprint,
                "schema_fingerprint": expected_schema_fingerprint,
            }
            self.candidates[execution_id]["promoted"] = True
            self.events.append(("warehouse:promoted", execution_id))
            return {"promoted": True, **proof}

        def discard_candidate(
            _table: str, execution_id: str, **_kwargs: Any
        ) -> dict[str, Any]:
            self.candidates[execution_id]["discarded"] = True
            self.events.append(("warehouse:discarded", execution_id))
            return {"discarded": True}

        monkeypatch.setattr(raw_landing, "land_raw_rows", land_raw_rows)
        monkeypatch.setattr(raw_landing, "inspect_candidate", inspect_candidate)
        monkeypatch.setattr(raw_landing, "promote_candidate", promote_candidate)
        monkeypatch.setattr(raw_landing, "discard_candidate", discard_candidate)

        def advance_state(
            execution_id: str,
            _from_state: str | None,
            state: str,
            _actor: str,
            _conn,
            **evidence: Any,
        ) -> dict[str, Any]:
            execution = self.executions[execution_id]
            execution.update(state=state, **evidence)
            self.events.append((f"execution:{state}", execution_id))
            return dict(execution)

        def run_dq_gates(
            execution_id: str, _project_id: str, _conn, **_evidence: Any
        ) -> list[dict[str, Any]]:
            self.events.append(("dq:evaluated", execution_id))
            if self.reject_dq:
                return [{"code": "closure_negative_control", "severity": "blocking"}]
            return []

        def begin_promotion(
            execution_id: str,
            _project_id: str,
            _actor: str,
            _conn,
            *,
            dispatch_id: str,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            dispatch = self.dispatches[dispatch_id]
            assert dispatch["state"] == "ready"
            assert dispatch["dq_evidence"]["status"] == "passed"
            self.executions[execution_id]["state"] = "publishing"
            dispatch["state"] = "promoting"
            self.events.append(("publication:promotion-begun", execution_id))
            return {
                "execution_id": execution_id,
                "dispatch_id": dispatch_id,
                "state": "promoting",
            }

        def commit_publication(
            execution_id: str,
            _project_id: str,
            _actor: str,
            _conn,
            *,
            ledger_id: str,
            dispatch_id: str,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            execution_events = [
                event for event, owner in self.events if owner == execution_id
            ]
            assert "warehouse:promoted" in execution_events
            assert "publication:pointer" not in execution_events
            assert "publication:outbox" not in execution_events
            prior = self.current_pointer
            self.current_pointer = execution_id
            self.executions[execution_id]["state"] = "published"
            self.ledgers[ledger_id]["outcome"] = "published"
            self.dispatches[dispatch_id]["state"] = "published"
            self.events.append(("publication:pointer", execution_id))
            self.events.append(("publication:outbox", execution_id))
            return {
                "execution": dict(self.executions[execution_id]),
                "publication_log_id": f"dpl-{execution_id}",
                "prior_execution_id": prior,
                "ledger": dict(self.ledgers[ledger_id]),
                "dispatch": copy.deepcopy(self.dispatches[dispatch_id]),
            }

        monkeypatch.setattr(
            datastream_publication, "advance_state", advance_state
        )
        monkeypatch.setattr(
            datastream_publication, "run_dq_gates", run_dq_gates
        )
        monkeypatch.setattr(
            datastream_publication,
            "begin_managed_file_promotion",
            begin_promotion,
        )
        monkeypatch.setattr(
            datastream_publication,
            "commit_publication",
            commit_publication,
        )


def _attachment(store: _MemoryStore, payload: bytes) -> dict[str, Any]:
    stored = store.put(data=payload)
    return {
        "filename": "daily.csv",
        "quarantine_uri": stored.uri,
        "size": stored.size,
        "content_type": "text/csv",
        "content_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _manifest(harness: _Harness, payload: bytes) -> dict[str, Any]:
    from core.inbound_receipts import canonical_receipt_fingerprint

    attachment = {**_attachment(harness.store, payload), "ordinal": 0}
    recipient_hash = "b" * 64
    return {
        "schema": "inbound-delivery-manifest-v1",
        "receipt_id": harness.receipt_id,
        "provider_event_id": f"event-{harness.channel}-{harness.run_number}",
        "channel": harness.channel,
        "token_hash": "a" * 64,
        "recipient_hash": recipient_hash,
        "received_at": "2026-08-02T10:00:00+00:00",
        "retention_policy": {
            "version": "quarantine-retention-v1",
            "days": 30,
        },
        "attachments": [attachment],
        "receipt_fingerprint": canonical_receipt_fingerprint(
            datastream_id=DATASTREAM_ID,
            credential_id="dic-closure",
            channel=harness.channel,
            recipient_hash=recipient_hash,
            attachments=[attachment],
        ),
    }


def _invoke(harness: _Harness, payload: bytes) -> dict[str, Any]:
    from core.inbound_processing import (
        process_authorized_upload,
        process_inbound_delivery,
    )

    if harness.channel == "upload":
        return process_authorized_upload(
            harness.conn,
            datastream_id=DATASTREAM_ID,
            project_id=PROJECT_ID,
            file_bytes=payload,
            filename="daily.csv",
            media_type="text/csv",
            actor="member-closure",
            idempotency_key=f"upload-{harness.run_number}",
            store=harness.store,
        )
    return process_inbound_delivery(
        harness.conn,
        manifest=_manifest(harness, payload),
        store=harness.store,
    )


def _business_candidate(
    harness: _Harness, execution_id: str
) -> dict[str, Any]:
    row = harness.candidates[execution_id]["rows"][0]
    provenance = {
        "execution_id",
        "plan_version_id",
        "mapping_version_id",
        "project_id",
    }
    return {key: value for key, value in row.items() if key not in provenance}


def test_three_real_entries_publish_the_same_typed_full_grain_candidate(
    monkeypatch,
):
    from core import csv_excel_import, import_runner
    from core.csv_excel_import import run_import
    from core.inbound_ingest import ingest_inbound_file

    # ONE implementation, reached by every channel. `csv_excel_import` is the
    # stable seam and `import_runner` is where the code lives; what this guards
    # is that the two are the SAME object, so a channel cannot acquire a private
    # copy of the import driver.
    assert run_import.__module__ == "core.import_runner"
    assert csv_excel_import.run_import is import_runner.run_import
    assert ingest_inbound_file.__module__ == "core.inbound_ingest"

    payload = (
        b"day,spend,clicks,active\n"
        b"2026-08-01,12.50,4,true\n"
    )
    snapshots: dict[str, dict[str, Any]] = {}

    for channel in ("upload", "email", "webhook"):
        with monkeypatch.context() as scoped:
            harness = _Harness()
            harness.install(scoped)
            harness.start(channel)
            result = _invoke(harness, payload)
            execution_id = harness.execution_id
            dispatch = harness.dispatches[f"mfd-{channel}-1"]
            candidate = harness.candidates[execution_id]
            business = _business_candidate(harness, execution_id)
            execution_events = [
                event for event, owner in harness.events if owner == execution_id
            ]

            assert result["status"] == "landed"
            dispatch_result = result.get("dispatch_result")
            if dispatch_result is None:
                dispatch_result = result["attachments"][0]["dispatch_result"]
            assert dispatch_result["published"] is True
            assert candidate["promoted"] is True
            assert candidate["discarded"] is False
            assert candidate["columns"][:5] == [
                ("canonical_day", "DATE"),
                ("canonical_active", "BOOLEAN"),
                ("canonical_clicks", "INTEGER"),
                ("canonical_spend", "FLOAT"),
                ("grain_key", "STRING"),
            ]
            assert business == {
                "canonical_day": "2026-08-01",
                "canonical_active": True,
                "canonical_clicks": 4,
                "canonical_spend": 12.5,
                "grain_key": business["grain_key"],
            }
            assert len(business["grain_key"]) == 64
            row = candidate["rows"][0]
            assert row["plan_version_id"] == PLAN_ID
            assert row["mapping_version_id"] == MAPPING_ID
            assert row["project_id"] == PROJECT_ID
            assert dispatch["dq_evidence"]["status"] == "passed"
            assert (
                dispatch["dq_evidence"]["observed_content_fingerprint"]
                == dispatch["candidate_content_fingerprint"]
            )
            assert (
                dispatch["dq_evidence"]["observed_schema_fingerprint"]
                == dispatch["candidate_schema_fingerprint"]
            )
            assert execution_events.index(
                "warehouse:promoted"
            ) < execution_events.index("publication:pointer")
            assert execution_events.index(
                "warehouse:promoted"
            ) < execution_events.index("publication:outbox")
            assert harness.accepted_bundles == [harness.bundle]

            snapshots[channel] = {
                "business_candidate": business,
                "schema": candidate["columns"],
                "accepted_bundle": harness.accepted_bundles[0],
                "intent_bundle": harness.intent_bundles[0],
                "dq_schema": dispatch["dq_evidence"][
                    "observed_schema_fingerprint"
                ],
            }

    assert snapshots["upload"] == snapshots["email"] == snapshots["webhook"]


@pytest.mark.parametrize("channel", ["upload", "email", "webhook"])
def test_dq_rejection_preserves_last_known_good_and_emits_no_outbox(
    monkeypatch, channel
):
    passing_payload = (
        b"day,spend,clicks,active\n"
        b"2026-08-01,12.50,4,true\n"
    )
    rejected_payload = (
        b"day,spend,clicks,active\n"
        b"2026-08-02,99.00,9,false\n"
    )
    harness = _Harness()
    harness.install(monkeypatch)

    harness.start(channel)
    passing_result = _invoke(harness, passing_payload)
    assert passing_result["status"] == "landed"
    published_lkg = harness.current_pointer
    outbox_before = sum(
        event == "publication:outbox" for event, _owner in harness.events
    )

    harness.start(channel, reject_dq=True)
    rejected_result = _invoke(harness, rejected_payload)
    rejected_execution = harness.execution_id
    rejected_events = [
        event
        for event, owner in harness.events
        if owner == rejected_execution
    ]

    assert rejected_result["status"] == "rejected"
    assert harness.current_pointer == published_lkg
    assert "dq:evaluated" in rejected_events
    assert "warehouse:discarded" in rejected_events
    assert "warehouse:promoted" not in rejected_events
    assert "publication:pointer" not in rejected_events
    assert "publication:outbox" not in rejected_events
    outbox_after = sum(
        event == "publication:outbox" for event, _owner in harness.events
    )
    assert outbox_after == outbox_before
    raw_id = f"inbraw-{harness.receipt_id}-0"
    assert harness.raw[raw_id]["state"] == "REJECTED"

