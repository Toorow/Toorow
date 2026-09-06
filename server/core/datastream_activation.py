"""Safe preview and two-ceremony Datastream activation contracts (Story 47.4)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Callable

from ulid import ULID

from core.audit import declare_action
from core.column_treatments import is_excluded, is_reviewed

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_DATASTREAM_SETUP_PREVIEW_CREATED = declare_action("datastream.setup.preview_created")


logger = logging.getLogger(__name__)

MODES = frozenset({"connector_pull", "external_bq", "managed_feed"})

#: Channels whose file does not exist until a delivery arrives. Their schema
#: CANNOT be reviewed before materialization -- an address is only issued against
#: a materialized Datastream, and `inbound_credentials._require_receivable`
#: already allows exactly that: "Allow draft discovery and active intake". So the
#: Datastream is created WITHOUT a plan, a mapping or a candidate; the first
#: delivery is the evidence that mints them. Mirrors
#: `datastream_preconfiguration._CHANNELS_WITH_NO_FILE_BEFORE_DELIVERY`; core
#: never imports the inbound package (AD-2), so the set is stated in each.
CHANNELS_AWAITING_FIRST_DELIVERY = frozenset({"inbound_email", "webhook"})


def awaits_first_delivery(operator_input: dict[str, Any]) -> bool:
    """True when this draft describes a feed whose file has not arrived yet."""
    source = operator_input.get("source") or {}
    return (
        operator_input.get("mode") == "managed_feed"
        and (source.get("managed_feed") or source).get("channel")
        in CHANNELS_AWAITING_FIRST_DELIVERY
    )
_REQUIRED_PINS = frozenset(
    {
        "draft_revision_id",
        "proposal_id",
        "observation_id",
        "connector_contract_version_id",
        "project_configuration_version_id",
        "capability_version_ids",
        "mapping_hash",
        "processing_hash",
    }
)
_MODE_REQUIRED = {
    "connector_pull": {"requested_interval", "observed_interval", "quota_cost", "schema"},
    "external_bq": {"object_identity", "schema", "estimated_scan", "read_only"},
    "managed_feed": {"schema", "write_mode"},
}
_SAFE_EVIDENCE_KEYS = frozenset(
    {
        "requested_interval",
        "observed_interval",
        "quota_cost",
        "schema",
        "row_count_bucket",
        "scan_estimate",
        "coverage",
        "freshness",
        "dq",
        "processing",
        "outputs",
        "exceptions",
        "object_identity",
        "estimated_scan",
        "read_only",
        "watermark",
        "detected_format",
        "format",
        "parse_errors",
        "write_mode",
        "arrival_contract",
    }
)
_SECRET_KEYS = frozenset(
    {
        "credential",
        "credentials",
        "secret",
        "access_token",
        "refresh_token",
        "authorization",
        "file_bytes",
        "raw_payload",
    }
)
_MAX_ROWS = 25
_MAX_INPUT_ROWS = 1000
_HEX64 = re.compile(r"[0-9a-f]{64}")


class PreviewValidationError(ValueError):
    code = "invalid_datastream_preview"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def record_run_output_version(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    execution_id: str,
    actor: str,
) -> str | None:
    """Say WHERE a completed run landed, so the analytical read can find it.

    An output version was written at publication and NOWHERE ELSE, from the
    candidate's isolated relation (`raw_..._cand_<execution>`). That relation
    belongs to the candidate and does not survive it, so every later run left the
    pointer aimed at a throwaway: `query_execution` read the newest output
    version, asked the warehouse for a relation that is gone, and answered
    `unavailable` with `missing_link: warehouse` -- measured 2026-08-12, on a
    Datastream whose run had just landed 196 rows an hour earlier.

    The address is the one the module DECLARES for this report profile, resolved
    by `stage_relation_resolver` -- the same declaration the Data tab reads its
    `Collected` stage from. Nothing is composed here: a profile the resolver does
    not know records nothing rather than a guessed name, because a wrong read
    address is another Connector's rows served under this Datastream's name.

    AND IT CARRIES THE SCHEMA FINGERPRINT, BECAUSE IT IS THE ONLY WRITER THAT
    STILL CAN. `app.datastream_output_versions` is UNIQUE on
    `(output_id, execution_id)` and immutable by trigger (migration 138), and BOTH
    writers of it insert `ON CONFLICT ... DO NOTHING`. So when a run lands before
    its own publication -- which is every run after the first, since this function
    is a no-op until a publication has created the `app.datastream_outputs` row --
    the row that survives is THIS one, and the publication's INSERT (which does
    compute `schema_hash`) is silently discarded. No UPDATE can repair it
    afterwards: `trg_datastream_output_versions_immutable` raises on UPDATE and
    DELETE alike.

    Measured consequence before this read existed: exactly the FIRST publication
    of a Datastream carried a `schema_hash`, and every one after it was NULL --
    so `CandidateReadiness` on the Outputs tab read "Unavailable" on the candidate
    side and printed `Not comparable` for the life of the Datastream, on the
    screen that decides a promotion.

    The value is the candidate's own, read from `app.datastream_executions.
    candidate_evidence` where `complete_candidate_from_adapter` put it, through
    the same `candidate_schema_hash` the publication path uses. Nothing is
    composed here: an execution whose candidate evidence carries no fingerprint
    writes NULL, and the diff stays honestly "Not comparable" rather than
    comparing a hash this function invented.

    Returns the relation recorded, or None when there is nothing true to record.
    """
    from ulid import ULID  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """SELECT org_id, module_name, report_profile_id, current_mapping_version_id,
                      current_plan_version_id
                 FROM app.datastreams WHERE id=%s AND project_id=%s""",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None or not row[3]:
        return None
    org_id, module, profile_id, mapping_version_id, plan_version_id = row

    from core.stage_relation_resolver import resolve_stage_relations  # noqa: PLC0415

    stages = resolve_stage_relations(connector=module, report_profile_id=profile_id)
    relation = stages.get("collected_relation")
    if not relation:
        logger.info(
            "activation: run_output_not_recorded datastream=%s reason=%s",
            datastream_id,
            stages.get("reason") or "no declared relation",
        )
        return None

    with conn.cursor() as cur:
        cur.execute(
            """SELECT id FROM app.datastream_outputs
                WHERE project_id=%s AND datastream_id=%s
                ORDER BY created_at LIMIT 1""",
            (project_id, datastream_id),
        )
        output = cur.fetchone()
    if output is None:
        return None

    # The candidate's fingerprint, from the row the worker already wrote. See the
    # docstring: this INSERT wins the conflict against the publication's, so if it
    # does not carry the hash nothing ever will.
    with conn.cursor() as cur:
        cur.execute(
            """SELECT candidate_evidence FROM app.datastream_executions
                WHERE id=%s AND datastream_id=%s AND project_id=%s""",
            (execution_id, datastream_id, project_id),
        )
        candidate = cur.fetchone()
    schema_hash = candidate_schema_hash(_json_value(candidate[0], {}) if candidate else {})

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastream_output_versions
               (id,output_id,org_id,project_id,datastream_id,execution_id,
                plan_version_id,mapping_version_id,relation_ref,schema_hash,
                grain_evidence,evidence,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
               ON CONFLICT (output_id,execution_id) DO NOTHING""",
            (
                f"dsov_{ULID()}",
                output[0],
                org_id,
                project_id,
                datastream_id,
                execution_id,
                plan_version_id,
                mapping_version_id,
                relation,
                schema_hash,
                # The two evidence columns are NOT NULL with a safety CHECK, so
                # an omitted one is a refused insert -- measured 2026-08-12 as
                # `datastream_output_versions_grain_evidence_check`. A run's
                # evidence is what it is: the stage it landed in and the pull
                # that landed it. Nothing is copied from the candidate.
                _canonical({"stage": "collected", "relation": relation}),
                _canonical({"recorded_by": "run", "execution_id": execution_id}),
                actor,
            ),
        )
    logger.info(
        "activation: run_output_recorded datastream=%s relation=%s", datastream_id, relation
    )
    return str(relation)


def candidate_schema_hash(*sources: Any) -> str | None:
    """Return the candidate's schema fingerprint, under either of its two names.

    The fact exists in the database on every path -- the adapter path writes it
    into `candidate_evidence` as `schema_hash`
    (`complete_candidate_from_adapter`), the managed-file/CSV path as
    `candidate_schema_fingerprint` (`csv_excel_import`) -- and both are a plain
    `sha256(...).hexdigest()`. Nothing between the worker and the Outputs tab
    carried it, so `app.datastream_output_versions.schema_hash` was NULL on
    every row ever published and the publication diff read "Not comparable" for
    a comparison the evidence could always have made.

    Returns None rather than a best-effort value: migration 138 CHECKs both
    `schema_hash` columns against `^[0-9a-f]{64}$`, and a violation here aborts
    the whole publish transaction -- the same failure class as the
    `grain_evidence` object/array mismatch recorded below. An honest NULL keeps
    the diff at "Not comparable"; a wrong value would make it lie.
    """
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("schema_hash", "candidate_schema_fingerprint"):
            value = source.get(key)
            if isinstance(value, str) and len(value) == 64 and _HEX64.fullmatch(value):
                return value
    return None


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _validate_pins(pins: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(pins, dict) or not _REQUIRED_PINS.issubset(pins):
        raise PreviewValidationError("Preview dependency pins are incomplete")
    if any(pins.get(key) in (None, "") for key in _REQUIRED_PINS):
        raise PreviewValidationError("Preview dependency pins cannot be empty")
    if not isinstance(pins.get("capability_version_ids"), list):
        raise PreviewValidationError("Capability version pins must be an exact list")
    for key in ("mapping_hash", "processing_hash"):
        value = pins[key]
        if not isinstance(value, str) or len(value) != 64:
            raise PreviewValidationError(f"{key} must be a SHA-256 hash")
    return deepcopy(pins)


def _mask_rows(rows: list[dict[str, Any]], classifications: dict[str, str]) -> list[dict[str, Any]]:
    if len(rows) > _MAX_INPUT_ROWS or any(not isinstance(row, dict) for row in rows):
        raise PreviewValidationError("Preview row input is invalid or unbounded")
    ordered = sorted(rows, key=_canonical)[:_MAX_ROWS]
    return [
        {
            str(field): value if classifications.get(str(field)) == "none" else "[MASKED]"
            for field, value in sorted(row.items())
        }
        for row in ordered
    ]


def build_safe_preview(
    *,
    mode: str,
    pins: dict[str, Any],
    adapter_evidence: dict[str, Any],
    classifications: dict[str, str],
) -> dict[str, Any]:
    """Build deterministic browser-safe evidence from a registered adapter result."""
    if mode not in MODES or not isinstance(adapter_evidence, dict):
        raise PreviewValidationError("Preview mode or adapter evidence is invalid")
    if adapter_evidence.get("adapter_verified") is not True:
        raise PreviewValidationError("An unavailable adapter cannot produce preview evidence")
    if adapter_evidence.get("placeholder") is True:
        raise PreviewValidationError("Placeholder evidence cannot pass preview")
    missing = _MODE_REQUIRED[mode] - set(adapter_evidence)
    if missing:
        raise PreviewValidationError(f"Mode evidence is incomplete: {', '.join(sorted(missing))}")
    if mode == "external_bq" and adapter_evidence.get("read_only") is not True:
        raise PreviewValidationError("External BigQuery preview must be read-only")
    if mode == "managed_feed" and not (
        adapter_evidence.get("detected_format")
        or adapter_evidence.get("format")
        or adapter_evidence.get("arrival_contract")
    ):
        raise PreviewValidationError("Managed-feed format or arrival evidence is required")
    if set(_walk_keys(adapter_evidence)) & _SECRET_KEYS:
        raise PreviewValidationError("Secret-bearing adapter evidence is forbidden")

    pinned = _validate_pins(pins)
    rows = list(adapter_evidence.get("rows") or [])
    sample = _mask_rows(rows, classifications)
    safe_evidence = {
        key: deepcopy(adapter_evidence[key])
        for key in sorted(_SAFE_EVIDENCE_KEYS & set(adapter_evidence))
    }
    if len(_canonical(safe_evidence).encode("utf-8")) > 16384:
        raise PreviewValidationError("Preview evidence exceeds the bounded limit")
    payload = {
        "schema_version": "1",
        "mode": mode,
        "dependency_snapshot": pinned,
        "dependency_hash": _hash(pinned),
        "sample": sample,
        "sample_limit": _MAX_ROWS,
        "safe_evidence": safe_evidence,
        "status": "blocked"
        if (safe_evidence.get("dq") or {}).get("blocking")
        else "ready_for_review",
    }
    payload["evidence_hash"] = _hash(payload)
    return payload


def preview_dispatch_request(
    *,
    mode: str,
    project_id: str,
    draft_id: str,
    pins: dict[str, Any],
    dispatch: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Dispatch provider-backed preview work through the shared queue boundary."""
    if mode not in MODES or not callable(dispatch):
        raise PreviewValidationError("Preview dispatch is unavailable")
    pinned = _validate_pins(pins)
    payload = {
        "kind": "datastream_setup_preview",
        "mode": mode,
        "project_id": project_id,
        "draft_id": draft_id,
        "correlation_id": pinned["draft_revision_id"],
        "dependency_hash": _hash(pinned),
    }
    dispatch(payload)
    return payload


def preview_idempotency_hash(idempotency_key: str) -> str:
    """The key a persisted preview is stored under. ONE definition, both sides.

    `_hash` canonicalises before hashing, so for a plain string it hashes the
    JSON form -- quotes included. A reader that recomputed a bare
    `sha256(correlation_id)` looked for a hash this table never contains, and
    reported a preview it had just written as missing evidence. Whoever needs
    this key asks here rather than recomputing it.
    """
    return _hash(idempotency_key)


def persist_preview(
    conn,
    *,
    project_id: str,
    draft_id: str,
    actor: str,
    idempotency_key: str,
    preview: dict[str, Any],
) -> dict[str, Any]:
    """Append one immutable, replay-safe preview without persisting raw adapter rows."""
    if not idempotency_key.strip() or preview.get("schema_version") != "1":
        raise PreviewValidationError("Preview persistence input is invalid")
    pins = _validate_pins(preview.get("dependency_snapshot") or {})
    safe_evidence = {
        "sample": deepcopy(preview.get("sample") or []),
        **deepcopy(preview.get("safe_evidence") or {}),
        "status": preview.get("status"),
        "sample_limit": preview.get("sample_limit"),
    }
    # THE BOUND TRIMS THE SAMPLE; IT DOES NOT DESTROY THE PREVIEW.
    #
    # 8 KiB is a storage bound, and a preview is mostly SAMPLE ROWS. A workbook
    # of twenty-two columns carrying titles, tags and descriptions passes it on
    # the first row, so the whole preview was refused -- and with no preview a
    # Datastream cannot be reviewed or activated. Measured 2026-08-11 on a real
    # 531-row catalogue: any file wide enough to be interesting was unpreviewable.
    #
    # The schema, the coverage and the counters are what the review READS; the
    # sample only illustrates. So rows go first, fewest possible, and the
    # evidence SAYS it was trimmed. If it still does not fit with no sample at
    # all, the schema itself is oversized and that is a real refusal.
    if len(_canonical(safe_evidence).encode("utf-8")) > 8192:
        sample = list(safe_evidence.get("sample") or [])
        while sample and len(_canonical(safe_evidence).encode("utf-8")) > 8192:
            sample.pop()
            safe_evidence["sample"] = sample
        exceptions = list(safe_evidence.get("exceptions") or [])
        exceptions.append(
            {"code": "sample_trimmed_to_evidence_bound", "kept_rows": len(sample)}
        )
        safe_evidence["exceptions"] = exceptions
        if len(_canonical(safe_evidence).encode("utf-8")) > 8192:
            raise PreviewValidationError(
                "Persisted preview evidence exceeds the bounded limit with no sample rows: "
                "the schema alone is over the bound"
            )
    key_hash = preview_idempotency_hash(idempotency_key)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id,dependency_hash,evidence_hash,safe_evidence,created_at
            FROM app.datastream_setup_previews
            WHERE project_id=%s AND draft_id=%s AND idempotency_key_hash=%s
            """,
            (project_id, draft_id, key_hash),
        )
        existing = cur.fetchone()
        if existing is not None:
            if existing[1] != preview["dependency_hash"] or existing[2] != preview["evidence_hash"]:
                raise PreviewValidationError("Preview idempotency key is already bound")
            return {
                "preview_ref": existing[0],
                "dependency_hash": existing[1],
                "evidence_hash": existing[2],
                "safe_evidence": existing[3],
                "created_at": existing[4],
                "idempotent_replay": True,
            }
        preview_id = f"dspv_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.datastream_setup_previews
                (id,project_id,draft_id,draft_revision_id,proposal_id,observation_id,
                 mode,dependency_snapshot,dependency_hash,mapping_hash,processing_hash,
                 safe_evidence,evidence_hash,idempotency_key_hash,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s,%s)
            """,
            (
                preview_id,
                project_id,
                draft_id,
                pins["draft_revision_id"],
                pins["proposal_id"],
                pins["observation_id"],
                preview["mode"],
                _canonical(pins),
                preview["dependency_hash"],
                pins["mapping_hash"],
                pins["processing_hash"],
                _canonical(safe_evidence),
                preview["evidence_hash"],
                key_hash,
                actor,
            ),
        )
    from core.audit import insert_audit_row  # noqa: PLC0415

    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_DATASTREAM_SETUP_PREVIEW_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "project_id": project_id,
            "draft_id": draft_id,
            "preview_id": preview_id,
            "dependency_hash": preview["dependency_hash"],
            "evidence_hash": preview["evidence_hash"],
        },
    )
    return {
        "preview_ref": preview_id,
        "dependency_hash": preview["dependency_hash"],
        "evidence_hash": preview["evidence_hash"],
        "safe_evidence": safe_evidence,
        "idempotent_replay": False,
    }


class ActivationValidationError(ValueError):
    code = "invalid_datastream_activation"


def freeze_final_review(
    *,
    project_id: str,
    draft_id: str,
    proposal: dict[str, Any],
    preview: dict[str, Any],
    acknowledged_warning_ids: list[str],
) -> dict[str, Any]:
    """Freeze the exact non-authorizing review snapshot after all warnings are acknowledged."""
    if preview.get("status") != "ready_for_review" or preview.get("is_stale") is True:
        raise ActivationValidationError("A current passing preview is required")
    blockers: list[str] = []
    warnings: list[str] = []
    for section in proposal.get("sections") or []:
        for item in section.get("items") or []:
            if item.get("status") in {"blocked", "missing"} or item.get("blockers"):
                blockers.append(str(item.get("key")))
            if item.get("status") in {"warning", "needs_review"} or item.get("warnings"):
                warnings.append(str(item.get("key")))
    if blockers:
        raise ActivationValidationError("Blocking review items cannot be acknowledged away")
    acknowledged = sorted(set(acknowledged_warning_ids))
    if set(warnings) - set(acknowledged):
        raise ActivationValidationError("Every warning requires an explicit acknowledgement")
    snapshot = {
        "schema_version": "1",
        "project_id": project_id,
        "draft_id": draft_id,
        "proposal_ref": proposal.get("proposal_ref"),
        "proposal_hash": proposal.get("content_hash"),
        "preview_ref": preview.get("preview_ref"),
        "preview_dependency_hash": preview.get("dependency_hash"),
        "preview_evidence_hash": preview.get("evidence_hash"),
        "confirmed_intent_bundle": deepcopy(proposal.get("confirmed_intent_bundle") or {}),
        "acknowledged_warning_ids": acknowledged,
    }
    snapshot["content_hash"] = _hash(snapshot)
    return snapshot


def prepare_confirmation(
    conn,
    *,
    command_type: str,
    actor_person_id: str,
    project_id: str,
    resource_id: str,
    content_hash: str,
    idempotency_key: str,
):
    """Issue one short-lived AD-27 secret for an exact persisted review."""
    from core.entry_confirmations import issue_entry_confirmation  # noqa: PLC0415

    payload = {
        "project_id": project_id,
        "resource_id": resource_id,
        "content_hash": content_hash,
    }
    return issue_entry_confirmation(
        conn,
        actor_person_id=actor_person_id,
        command_type=command_type,
        request_payload=payload,
        idempotency_key=idempotency_key,
        context_reference=f"{project_id}/{resource_id}",
    )


def execute_confirmed_operation(
    conn,
    *,
    command_type: str,
    actor_person_id: str,
    actor: str,
    org_id: str,
    project_id: str,
    resource_id: str,
    content_hash: str,
    idempotency_key: str,
    confirmation_id: str,
    confirmation_secret: str,
    mutation: Callable[[Any, str], Any],
):
    """Consume confirmation, mutate, audit, outbox and bind in one outer transaction."""
    from core.entry_confirmations import (  # noqa: PLC0415
        bind_entry_confirmation_operation,
        consume_entry_confirmation,
    )
    from core.operations import OperationSpec, execute_operation  # noqa: PLC0415

    payload = {
        "project_id": project_id,
        "resource_id": resource_id,
        "content_hash": content_hash,
    }
    with conn.transaction():
        confirmation = consume_entry_confirmation(
            conn,
            confirmation_id=confirmation_id,
            confirmation_secret=confirmation_secret,
            actor_person_id=actor_person_id,
            command_type=command_type,
            request_payload=payload,
            idempotency_key=idempotency_key,
            context_reference=f"{project_id}/{resource_id}",
        )
        result = execute_operation(
            conn,
            OperationSpec(
                command_type=command_type,
                actor=actor,
                effective_org_id=org_id,
                resource_path=("projects", project_id, "datastreams", resource_id),
                idempotency_key=idempotency_key,
                host_context={},
                versions={},
                request_payload=payload,
                provider_references={},
                confirmation_mode="server",
                confirmation_reference=confirmation.confirmation_id,
                trace_id=None,
            ),
            mutation=mutation,
        )
        bind_entry_confirmation_operation(
            conn, confirmation=confirmation, operation_id=result.operation_id
        )
    return result


def candidate_dispatch_request(
    *, project_id: str, datastream_id: str, execution_id: str, mode: str
) -> dict[str, Any]:
    if mode not in MODES or not execution_id.startswith("dse_"):
        raise ActivationValidationError("Candidate dispatch scope is invalid")
    return {
        "kind": "datastream_candidate",
        "project_id": project_id,
        "datastream_id": datastream_id,
        "execution_id": execution_id,
        "correlation_id": execution_id,
        "idempotency_key": execution_id,
        "mode": mode,
    }


def build_candidate_review(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return exact review evidence only for a real isolated Ready candidate."""
    execution_id = str(candidate.get("execution_id") or candidate.get("id") or "")
    artifact_ref = str(candidate.get("artifact_ref") or "")
    dq = candidate.get("dq") or {}
    if candidate.get("state") != "ready":
        raise ActivationValidationError("Only a Ready candidate can be reviewed")
    if candidate.get("placeholder") or not candidate.get("adapter_verified"):
        raise ActivationValidationError("Placeholder or unverified candidates cannot become Ready")
    if not execution_id or execution_id not in artifact_ref:
        raise ActivationValidationError("Candidate artifact is not isolated by execution")
    if not candidate.get("content_hash") or not isinstance(candidate.get("row_count"), int):
        raise ActivationValidationError("Candidate provenance is incomplete")
    # AI-308: TWO STATES, TWO SENTENCES. One message said both -- "Empty or
    # failed-DQ candidates cannot become Current" -- which names no gesture, says
    # `DQ` to a person, and leaves the two halves indistinguishable although they
    # are repaired by different acts. A window that measured nothing is not a
    # window whose checks stopped it; that is the same distinction AI-307 drew on
    # the pull, applied to publication.
    if candidate["row_count"] <= 0:
        raise ActivationValidationError(
            "This run collected no rows, so there is nothing to publish. "
            "Check the dates you asked for, or the file you sent, then run it again."
        )
    if dq.get("blocking"):
        raise ActivationValidationError(
            "This run's quality checks stopped it, so its rows are not published. "
            "Open its Runs tab to see which check stopped it, then run it again."
        )
    required = {"plan_version_id", "mapping_version_id", "projection_hash", "output_plan"}
    if any(not candidate.get(key) for key in required):
        raise ActivationValidationError("Candidate version evidence is incomplete")
    review = {
        key: deepcopy(candidate.get(key))
        for key in (
            "project_id",
            "datastream_id",
            "execution_id",
            "artifact_ref",
            "content_hash",
            "schema_hash",
            "row_count",
            "plan_version_id",
            "mapping_version_id",
            "projection_hash",
            "output_plan",
            "candidate_columns",
            "schedule",
            "expected_current_execution_id",
            "dq",
        )
    }
    review["review_hash"] = _hash(review)
    return review


def _promote_managed_candidate(
    conn,
    *,
    execution_id: str,
    project_id: str,
    row_count: Any,
    artifact_ref: str | None = None,
    candidate_columns: Any = None,
) -> dict[str, Any] | None:
    """Append a published isolated candidate into the SHARED relation.

    The evidence is READ, never rebuilt. Both pieces live on the import ledger
    row of this execution, and both are the ones the reconciliation engine
    already uses (`managed_file_dispatch.reconcile_dispatch`):

      * the shared table is `landing_relation` minus its `__cand_<execution_id>`
        suffix -- the name that was actually written, not a name recomposed from
        a rule that can drift;
      * the typed columns are the governed bundle's `candidate_columns`, frozen
        into immutable `source_metadata` when the import opened.

    Managed files read their immutable dispatch bundle from Postgres. Connector
    pulls carry the exact typed list observed by ``raw_landing`` at write time in
    candidate evidence. If neither path supplies that evidence, publication
    fails closed rather than guessing a relation schema.

    `expected_rows` is checked and the two fingerprints are NOT passed. The
    execution's `content_hash` on this path is the ADAPTER's digest of its own
    inputs, not the canonical fingerprint of the landed rows, so handing it over
    as `expected_content_fingerprint` would assert a check that did not happen.

    Cross-store safety is `promote_candidate`'s own idempotency predicate: the
    MERGE keys on `execution_id`, so a retry after a Postgres rollback appends
    nothing twice.
    """
    from core.managed_feed_ledger import fetch_ledger_for_execution  # noqa: PLC0415
    from core.raw_landing import promote_candidate  # noqa: PLC0415

    ledger = fetch_ledger_for_execution(conn, execution_id, project_id)
    relation = str(
        ledger.get("landing_relation") if ledger is not None else artifact_ref or ""
    ).rsplit(".", 1)[-1]
    suffix = f"__cand_{execution_id}"
    table = relation[: -len(suffix)] if relation.endswith(suffix) else relation
    if not table:
        raise ActivationValidationError(
            "The published candidate names no landing relation to promote into"
        )

    bundle = (
        (ledger.get("source_metadata") or {}).get("dispatch_bundle") or {}
        if ledger is not None
        else {"candidate_columns": candidate_columns or []}
    )
    columns = [
        (str(item["name"]), str(item["type"]))
        for item in (bundle.get("candidate_columns") or [])
        if isinstance(item, dict) and item.get("name") and item.get("type")
    ]
    if not columns:
        raise ActivationValidationError(
            "The published candidate carries no governed column list; promoting "
            "it would guess the shape of the shared relation"
        )

    return promote_candidate(
        table,
        execution_id,
        columns=columns,
        project_id=project_id,
        idempotency_column="execution_id",
        idempotency_value=execution_id,
        expected_rows=int(row_count or 0),
    )


def _reverify_entity_designations(
    conn, *, project_id: str, datastream_id: str, mapping_version_id: str
) -> None:
    """Re-verify the entity designations of the mapping about to publish (68.2).

    Called INSIDE the publish transaction, under the same FOR UPDATE as every
    other last-line guard of this act. What can drift between the draft's
    append and this instant is the REGISTRY's side -- an entity type archived
    (`disabled`) or stripped between draft and publish -- never the payload,
    which is immutable and was validated at append; so the `mdm_target`
    contradiction is not re-read here and the check names the type it refuses.

    A payload without designations costs NO registry read. A registry that
    cannot be read fails CLOSED, the same posture `save_field_mapping` took
    when the version was born.
    """

    from core.object_kind_registry import (  # noqa: PLC0415
        entity_designations,
        fetch_entity_type_lookup,
        validate_entity_designations,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mapping_payload
              FROM app.datastream_mapping_versions
             WHERE id = %s AND datastream_id = %s AND project_id = %s
            """,
            (mapping_version_id, datastream_id, project_id),
        )
        row = cur.fetchone()
    payload = row[0] if row is not None else None
    if isinstance(payload, str):  # driver-dependent
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    if not isinstance(payload, dict):
        payload = {}
    pairs = entity_designations(payload)
    if not pairs:
        return
    try:
        declared_types = fetch_entity_type_lookup(
            conn, project_id=project_id, object_kinds=[kind for _f, kind in pairs]
        )
    except Exception as exc:
        raise ActivationValidationError(
            "Entity designations could not be re-verified -- the type registry "
            "could not be read"
        ) from exc
    issues = validate_entity_designations(payload, declared_types=declared_types)
    if issues:
        raise ActivationValidationError(
            f"Entity designation refused at publish: {issues[0].message}"
        )


def publish_activate_mutation(conn, *, review: dict[str, Any], actor: str, operation_id: str):
    """Atomically publish exact candidate and activate its exact versions/schedule."""
    from ulid import ULID  # noqa: PLC0415

    from core.datastream_publication import (  # noqa: PLC0415
        InvalidStateTransition,
        advance_state,
    )
    from core.operations import MutationResult  # noqa: PLC0415

    execution_id = review["execution_id"]
    project_id = review["project_id"]
    datastream_id = review["datastream_id"]
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.state,e.content_hash,e.row_count,d.current_published_execution_id,d.org_id,
                      d.enabled
               FROM app.datastream_executions e JOIN app.datastreams d
                 ON d.id=e.datastream_id AND d.project_id=e.project_id
              WHERE e.id=%s AND e.datastream_id=%s AND e.project_id=%s FOR UPDATE""",
            (execution_id, datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None or row[0] != "ready" or row[1] != review["content_hash"]:
            raise ActivationValidationError("Candidate changed after review")
        if row[2] != review["row_count"] or row[3] != review.get("expected_current_execution_id"):
            raise ActivationValidationError("Candidate or Current pointer changed after review")
        org_id = row[4] if len(row) > 4 else review.get("org_id")
        already_enabled = bool(row[5]) if len(row) > 5 else False
        # THE TRIAL GUARD BELONGS TO THE ACT, NOT TO ONE OF ITS DOORS (P0-1,
        # audit 2026-08-17). The org's trial allowance counts `enabled = TRUE`
        # rows, and the UPDATE below is the statement that writes `enabled=TRUE`.
        # `check_datastream_limit` had three callers -- CRUD create
        # (`datastreams.py:305`), schedule re-arm (`schedule_mcp.py:326`) and its
        # own definition -- and not one of them was this act, so the whole wizard
        # (materialize -> candidate -> publish-activate) and the MCP tool
        # `publish_activate_datastream_candidate` activated the (cap+1)-th
        # Datastream of a trial org without ever meeting the limit. Guarding here
        # guards every caller that exists and every door opened later, because
        # they all arrive at this one statement.
        #
        # Only when the row is NOT already enabled. A REpublication of a live
        # Datastream (new mapping, new candidate) changes no count; refusing it at
        # exactly `current == limit` would freeze the last Datastream of every
        # trial org on the mapping it happened to launch with. Same reading, and
        # the same `if enabled and not already_enabled`, as the schedule door.
        #
        # BEFORE any write of this function: a refusal leaves the execution
        # `ready`, the Current pointer untouched, and nothing to unwind.
        if not already_enabled:
            from core.trial_enforcement import check_datastream_limit  # noqa: PLC0415

            check_datastream_limit(project_id, conn, identity=actor, org_id=org_id)
        # Story 68.2: the designations the pinned mapping carries are re-verified
        # HERE, under the same FOR UPDATE, BEFORE any write of this act -- an
        # entity type archived between draft and publish refuses the publish
        # with the type named, rather than activating a key that reconciles on
        # nothing.
        _reverify_entity_designations(
            conn,
            project_id=project_id,
            datastream_id=datastream_id,
            mapping_version_id=review["mapping_version_id"],
        )
        before_hash = _hash({"current": row[3], "lifecycle": "draft"})
        # AI-223 -- THE MACHINE WRITES THE STATE, NOT THIS FUNCTION.
        #
        # The two statements this replaces (`state='publishing'` then
        # `state='published'`) wrote `state` WITHOUT `state_changed_at`, and the
        # table carries no trigger to make up for it (migration 042 says so in its
        # own header). Only the `DEFAULT now()` of the insertion held, so a run
        # published through the wizard measured its duration up to `ready` and not
        # up to `published` -- and nothing closed its open step spans either. Both
        # come free from `advance_state`, which is why the fix is to stop writing
        # here rather than to add two more columns to two more statements.
        #
        # Scoped by id alone: the row is locked `FOR UPDATE` under the full
        # (id, datastream_id, project_id) scope in the SELECT above, in this same
        # transaction, and its state was proven `ready` there. The refusal keeps
        # this module's vocabulary -- an operator reads "Candidate changed during
        # publication", never `invalid_state_transition`.
        try:
            advance_state(execution_id, "ready", "publishing", actor, conn)
        except InvalidStateTransition as exc:
            raise ActivationValidationError(
                "Candidate changed during publication"
            ) from exc
        # BETWEEN `publishing` and `published`, because that is what publishing a
        # candidate MEANS. `raw_landing` states the contract in its own words:
        # "Publication is then `promote_candidate`, which appends those rows into
        # the shared table with their pull_id intact (AD-7)". Isolation by
        # execution is what keeps a candidate out of the marts, and promotion is
        # the only thing that ever lets it in.
        #
        # This did not happen here, and the measurement is what it looks like: a
        # Datastream `active`, an execution `published`, an Output version -- and
        # the rows still in `..__cand_<execution_id>`, a relation no staging model
        # names. The data existed and was reachable by nobody.
        promotion = _promote_managed_candidate(
            conn,
            execution_id=execution_id,
            project_id=project_id,
            row_count=review["row_count"],
            artifact_ref=review.get("artifact_ref"),
            candidate_columns=review.get("candidate_columns"),
        )
        # The terminal transition, through the same machine: it stamps
        # `state_changed_at` and closes every step span this run left open.
        try:
            advance_state(execution_id, "publishing", "published", actor, conn)
        except InvalidStateTransition as exc:
            raise ActivationValidationError(
                "Candidate changed during publication"
            ) from exc
        log_id = f"dplog_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_publication_log
               (id,execution_id,datastream_id,project_id,plan_version_id,mapping_version_id,
                content_hash,row_count,prior_execution_id,published_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                log_id,
                execution_id,
                datastream_id,
                project_id,
                review["plan_version_id"],
                review["mapping_version_id"],
                review["content_hash"],
                review["row_count"],
                row[3],
                actor,
            ),
        )
        schedule = review.get("schedule") or {}
        sched_mode = schedule.get("schedule_mode") or "nightly"
        cur.execute(
            """UPDATE app.datastreams
                  SET current_published_execution_id=%s,current_plan_version_id=%s,
                      current_mapping_version_id=%s,lifecycle_state='active',enabled=TRUE,
                      schedule_mode=%s,
                      arrival_hour_local=COALESCE(%s, arrival_hour_local)
                WHERE id=%s AND project_id=%s""",
            (
                execution_id,
                review["plan_version_id"],
                review["mapping_version_id"],
                sched_mode,
                # Story 57.8. COALESCE, not assignment: a republication whose
                # review names no arrival hour must not erase the hour someone
                # set on the Workbench since the last activation.
                schedule.get("arrival_hour_local"),
                datastream_id,
                project_id,
            ),
        )
        # AI-217: `weekly` is a scheduled cadence, not a manual one. The list was
        # written before migration 204 and kept the same shape as the dispatcher's
        # filter -- one omission, repeated. A weekly activation that fell through
        # here was published with no instant at all in `next_run_at`.
        activation_kind = schedule.get("activation_kind") or (
            "schedule" if sched_mode in ("nightly", "weekly", "hourly") else "manual"
        )
        next_run_at = schedule.get("next_run_at")
        if not next_run_at and activation_kind == "schedule":
            from datetime import UTC, datetime  # noqa: PLC0415
            next_run_at = datetime.now(UTC).isoformat()
        cur.execute(
            "UPDATE app.datastream_schedule_state SET next_run_at=%s "
            "WHERE plan_version_id=%s AND datastream_id=%s AND project_id=%s",
            (next_run_at, review["plan_version_id"], datastream_id, project_id),
        )
        # AN UNDECLARED EXPECTATION LEAVES THE MONITOR UNARMED, and says so
        # (story 57.3). The row used to be written unconditionally against a
        # fabricated `1440`; a monitor measuring a promise nobody made is worse
        # than no monitor. No row now means exactly what it says: this Datastream
        # reports no lateness, because nobody declared when a delivery is due.
        if activation_kind == "arrival_monitor" and not schedule.get("expected_interval_minutes"):
            logger.info(
                "activation: arrival monitor NOT armed ds=%s -- no expected arrival "
                "interval was declared, so no lateness can be reported",
                datastream_id,
            )
        elif activation_kind == "arrival_monitor":
            cur.execute(
                """INSERT INTO app.datastream_arrival_monitors
                   (datastream_id,project_id,expected_interval_minutes,owner_person_id,
                    state,activated_at)
                   VALUES (%s,%s,%s,%s,'active',NOW())
                   ON CONFLICT (datastream_id,project_id) DO UPDATE
                   SET expected_interval_minutes=EXCLUDED.expected_interval_minutes,
                       owner_person_id=EXCLUDED.owner_person_id,state='active',activated_at=NOW()""",
                (
                    datastream_id,
                    project_id,
                    int(schedule["expected_interval_minutes"]),
                    schedule["owner_person_id"],
                ),
            )
        if org_id:
            # THE RELATION A READER IS HANDED IS THE PROMOTED ONE (2026-09-04). The
            # candidate landed in `<table>__cand_<execution_id>` and `_promote_managed_
            # candidate` above appended it into `<table>`; recording the isolated
            # name here handed Analyze a relation that no longer existed (404 on the
            # harness project). A connector pull already records the shared name.
            from core.raw_landing import promoted_relation  # noqa: PLC0415

            published_relation = promoted_relation(str(review.get("artifact_ref") or ""))
            raw_output_plan = review.get("output_plan")
            output_plan = raw_output_plan if isinstance(raw_output_plan, dict) else {}
            output_specs = (
                output_plan.get("outputs") if isinstance(output_plan.get("outputs"), list) else None
            )
            if not output_specs:
                output_specs = [
                    {
                        "output_kind": "full_grain",
                        "stable_name": str(
                            output_plan.get("relation") or f"{datastream_id}.full_grain"
                        ),
                        "relation_ref": published_relation,
                    }
                ]
            for output in output_specs:
                if not isinstance(output, dict):
                    raise ActivationValidationError("Published Output identity is invalid")
                output_kind = str(output.get("output_kind") or output.get("kind") or "")
                stable_name = str(output.get("stable_name") or "")
                if (
                    output_kind not in {"full_grain", "safe_projection", "delivery"}
                    or not stable_name
                ):
                    raise ActivationValidationError("Published Output identity is invalid")
                output_id = f"dso_{ULID()}"
                cur.execute(
                    """INSERT INTO app.datastream_outputs
                       (id,org_id,project_id,datastream_id,output_kind,stable_name,created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (project_id,datastream_id,output_kind,stable_name) DO NOTHING""",
                    (output_id, org_id, project_id, datastream_id, output_kind, stable_name, actor),
                )
                cur.execute(
                    """SELECT id FROM app.datastream_outputs
                        WHERE project_id=%s AND datastream_id=%s
                          AND output_kind=%s AND stable_name=%s""",
                    (project_id, datastream_id, output_kind, stable_name),
                )
                output_id = cur.fetchone()[0]
                # The schema fingerprint the candidate already recorded. Absent
                # from this column list until 2026-08-04, which is why the
                # Outputs tab could never compare two publications: it reads
                # `v.schema_hash`, that read was NULL on every row, and the
                # "Schema" line said "Not comparable" for the life of the
                # screen. `candidate_schema_hash` yields None rather than a
                # near-value -- the column's CHECK would abort the publish.
                output_schema_hash = candidate_schema_hash(output, review)
                cur.execute(
                    """INSERT INTO app.datastream_output_versions
                       (id,output_id,org_id,project_id,datastream_id,execution_id,
                        publication_log_id,plan_version_id,mapping_version_id,
                        projection_version_ref,relation_ref,delivery_ref,schema_hash,
                        grain_evidence,evidence,created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                       ON CONFLICT (output_id,execution_id) DO NOTHING""",
                    (
                        f"dsov_{ULID()}",
                        output_id,
                        org_id,
                        project_id,
                        datastream_id,
                        execution_id,
                        log_id,
                        review["plan_version_id"],
                        review["mapping_version_id"],
                        output.get("projection_version_ref"),
                        promoted_relation(str(output.get("relation_ref") or published_relation)),
                        output.get("delivery_ref"),
                        output_schema_hash,
                        # An OBJECT, not the bare list. `grain_evidence` is CHECKed by
                        # `app.safe_preconfiguration_evidence` (migration 134), which
                        # requires `jsonb_typeof(value) = 'object'` -- so a bare array
                        # is refused by the database, and so is the column's own
                        # DEFAULT `'[]'::jsonb` (migration 138 declares both). Measured
                        # 2026-08-01 on a disposable Postgres:
                        #   SELECT app.safe_preconfiguration_evidence('[]'::jsonb) -> false
                        # Every publish+activate therefore aborted here on a
                        # CheckViolation before any Output row existed, which is why
                        # production shows `app.datastream_output_versions` empty and no
                        # Datastream `active`. Wrapping is the repair that does not need
                        # a migration owned by another session; relaxing the CHECK to
                        # accept an array would match the column DEFAULT instead, and is
                        # recorded as the alternative rather than taken silently.
                        #
                        # AND THE VALUE, not only its shape. `output` is a per-output
                        # SPEC, and when `output_plan` declares no `outputs` list the
                        # spec is SYNTHESIZED right here (:675-683) from three keys --
                        # kind, name, relation. It never carries a `grain`, so this
                        # read yielded `[]` on every publication that did not hand-roll
                        # its outputs, which is all of them. The grain lives on the
                        # projection, one level up, which is exactly where the
                        # `published` stage evidence below reads it.
                        #
                        # Measured 2026-08-08 on the SAME publication, in the SAME
                        # transaction, `dse_01KZGNQ8H7R68EH0EAB9A2D670`:
                        #   stage evidence   {"grain": ["campaign", "date"]}
                        #   output version   {"grain": []}
                        # Two rows of one act disagreeing about the grain, and the
                        # Outputs tab reads the empty one. A spec that DOES declare its
                        # own grain still wins: a `safe_projection` output is allowed to
                        # be narrower than the full grain.
                        json.dumps(
                            {
                                "grain": list(
                                    output.get("grain") or output_plan.get("grain") or []
                                )
                            },
                            sort_keys=True,
                        ),
                        json.dumps(
                            {
                                "review_hash": review["review_hash"],
                                "content_hash": review["content_hash"],
                                # NOT `or content_hash`. The fallback that used
                                # to stand here labelled the CONTENT hash as a
                                # schema hash, so the one place that did carry
                                # the field carried a fact about the rows under
                                # the name of a fact about the shape.
                                "schema_hash": output_schema_hash,
                            },
                            sort_keys=True,
                        ),
                        actor,
                    ),
                )
    if org_id:
        from core.datastream_workbench import (  # noqa: PLC0415
            append_phase_evidence,
            append_stage_evidence,
        )

        append_stage_evidence(
            conn,
            org_id=str(org_id),
            project_id=project_id,
            datastream_id=datastream_id,
            execution_id=execution_id,
            stage="published",
            actor=actor,
            plan_version_id=review["plan_version_id"],
            mapping_version_id=review["mapping_version_id"],
            evidence={
                "phase_state": "succeeded",
                "artifact_ref": review["artifact_ref"],
                "materialization_ref": log_id,
                "schema_hash": review.get("schema_hash"),
                "row_count": review["row_count"],
                # Same object shape, same reason: `app.datastream_execution_stage_evidence`
                # carries the identical `grain_evidence JSONB DEFAULT '[]'` +
                # object-only CHECK pair (migration 138), so the array that used to be
                # passed here would have failed two statements after the Output insert.
                #
                # This comment used to end "These two call sites are the ONLY writers
                # of a `grain_evidence` column in the repository". That was true of
                # the COLUMNS and false of the WRITER: `append_stage_evidence` had the
                # same `[]` as its own default, so every caller that carries no grain
                # -- every candidate materialization of every mode -- died on a raw
                # CheckViolation. Repaired at the writer (AI-244); wrapping here is
                # still right, because these two DO have a grain to state.
                "grain_evidence": {
                    "grain": list(
                        (review.get("output_plan") or {}).get("grain") or []
                        if isinstance(review.get("output_plan"), dict)
                        else []
                    )
                },
                "evidence": {
                    "publication_log_id": log_id,
                    "operation_id": operation_id,
                    # What promotion actually did, so "published" is auditable as an
                    # act on the warehouse and not only as a state in Postgres.
                    "promoted_rows": int((promotion or {}).get("promoted") or 0),
                    "promoted_into": str((promotion or {}).get("into") or ""),
                },
            },
        )
        append_phase_evidence(
            conn,
            org_id=str(org_id),
            project_id=project_id,
            datastream_id=datastream_id,
            execution_id=execution_id,
            phase="publication",
            actor=actor,
            plan_version_id=review["plan_version_id"],
            mapping_version_id=review["mapping_version_id"],
            evidence={
                "phase_state": "succeeded",
                "artifact_ref": review["artifact_ref"],
                "row_count": review["row_count"],
                "evidence": {"publication_log_id": log_id, "operation_id": operation_id},
            },
        )
    after = {"current": execution_id, "lifecycle": "active", "operation_id": operation_id}
    return MutationResult(
        outcome="succeeded",
        before_hash=before_hash,
        after_hash=_hash(after),
        result=after,
        outbox_payload={"event": "datastream.published_activated", **after},
    )


def _materialize_without_versions(
    conn,
    *,
    review: dict[str, Any],
    actor: str,
    operation_id: str,
    datastream_id: str,
    org_id: str,
    project_id: str,
    draft_id: str,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    """Close a materialization that has no schema to version yet.

    Same ledger row and same draft transition as the full path -- the three
    version columns are NULL, which migration 224 made sayable and which its
    CHECK keeps all-or-nothing. No candidate is enqueued: there is nothing to
    execute until a file arrives.
    """
    from core.business_taxonomy import create_link  # noqa: PLC0415
    from core.operations import MutationResult  # noqa: PLC0415

    for domain_id in bundle.get("business_domain_ids") or []:
        create_link(
            conn,
            org_id=org_id,
            project_id=project_id,
            taxonomy_type="business_domain",
            taxonomy_id=domain_id,
            target_type="datastream",
            target_id=datastream_id,
            relation_type="governs",
            actor=actor,
            reason="Confirmed Datastream setup",
        )
    materialization_id = f"dsmat_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.datastream_setup_materializations
               (id,project_id,draft_id,final_review_id,datastream_id,plan_version_id,
                mapping_version_id,candidate_execution_id,operation_id,created_by)
               VALUES (%s,%s,%s,%s,%s,NULL,NULL,NULL,%s,%s)""",
            (
                materialization_id,
                project_id,
                draft_id,
                review["final_review_id"],
                datastream_id,
                operation_id,
                actor,
            ),
        )
        cur.execute(
            """UPDATE app.datastream_setup_drafts
                  SET state='materialized',materialized_datastream_id=%s,updated_at=NOW()
                WHERE id=%s AND project_id=%s
                  AND state IN ('draft','materialized')""",
            (datastream_id, draft_id, project_id),
        )
    result = {
        "materialization_id": materialization_id,
        "datastream_id": datastream_id,
        "plan_version_id": None,
        "mapping_version_id": None,
        "candidate_execution_id": None,
        "awaiting_first_delivery": True,
    }
    # The SAME shape the full path returns: `execute_operation` reads `.outcome`,
    # and a bare dict here made the whole materialization answer a 503 that named
    # nothing. The event says what happened -- a Datastream was created and is
    # waiting for its first file, which is not the same fact as a full one.
    return MutationResult(
        outcome="succeeded",
        before_hash=None,
        after_hash=_hash(result),
        result=result,
        outbox_payload={"event": "datastream.draft_materialized", **result},
    )


def _ensure_event_configuration(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    org_id: str,
    actor: str,
    operation_id: str,
) -> None:
    """An event report gets its Event Configuration WHERE THE DATASTREAM IS BORN.

    `app.require_event_observation_binding` refuses an event observation that no
    Datastream-owned Event Configuration version covers -- rightly: an event with
    no governed declaration is a row nobody can trace. But the four operations
    that produce one (create, version, confirm, activate) were reachable only from
    the data surface and the MCP, and the setup wizard called none of them. So a
    Datastream built on an event report materialized, collected from the provider,
    and died at the write with `db_error` -- measured 2026-08-12 on a report that
    had just returned 5 publications.

    A screen that demands an action must be able to perform it. The declaration
    comes from the Connector's own manifest -- which events this report emits --
    and the cadence from the Datastream's schedule; nothing here is invented. A
    report that emits no event does nothing at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT module_name, report_profile_id, schedule_mode FROM app.datastreams "
            "WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if not row or not row[0] or not row[1]:
        return
    module, report_id, schedule_mode = str(row[0]), str(row[1]), str(row[2] or "manual")

    try:
        from core.context_seed import load_registry_entry  # noqa: PLC0415

        manifest = (load_registry_entry(module) or {}).get("manifest") or {}
        profile = next(
            (
                item
                for item in (manifest.get("report_profiles") or [])
                if item.get("id") == report_id
            ),
            None,
        )
        events = list((profile or {}).get("events") or [])
        if not events:
            return

        from core.event_configurations import (  # noqa: PLC0415
            activate_event_configuration_version,
            confirm_event_configuration_version,
            create_event_configuration,
            create_event_configuration_version,
        )

        created = create_event_configuration(
            conn,
            project_id=project_id,
            datastream_id=datastream_id,
            org_id=org_id,
            name=report_id,
            actor=actor,
            idempotency_key=f"{operation_id}:event-configuration",
        )
        configuration_id = created.get("event_configuration_id")
        if not configuration_id:
            return
        version = create_event_configuration_version(
            conn,
            project_id=project_id,
            event_configuration_id=configuration_id,
            org_id=org_id,
            # WHAT THE CONNECTOR SAYS IT EMITS, and where from. Naming the report
            # and its declared events is the whole binding: an observation is
            # traceable to a Connector, a report and an event type.
            source_mapping={"module": module, "report_id": report_id, "events": events},
            collection_policy={"cadence": schedule_mode, "timezone": "UTC"},
            actor=actor,
            idempotency_key=f"{operation_id}:event-configuration-version",
        )
        version_id = version.get("version_id")
        if not version_id:
            return
        confirm_event_configuration_version(
            conn,
            project_id=project_id,
            event_configuration_id=configuration_id,
            version_id=version_id,
            org_id=org_id,
            actor=actor,
            idempotency_key=f"{operation_id}:event-configuration-confirm",
        )
        activate_event_configuration_version(
            conn,
            project_id=project_id,
            event_configuration_id=configuration_id,
            version_id=version_id,
            org_id=org_id,
            actor=actor,
            idempotency_key=f"{operation_id}:event-configuration-activate",
        )
        logger.info(
            "activation: event_configuration_active datastream=%s events=%s",
            datastream_id,
            ",".join(events),
        )
    except Exception as exc:  # noqa: BLE001 -- an absent configuration does not lose the Datastream
        logger.warning(
            "activation: event_configuration_not_created datastream=%s: %s: %s",
            datastream_id,
            type(exc).__name__,
            exc,
        )


def _dq_evidence_for(conn, project_id: str) -> dict[str, Any]:
    """The Project's DQ report, read -- never assumed, never fabricated."""
    try:
        from core.dq_api import fetch_dq_report_data  # noqa: PLC0415

        report = fetch_dq_report_data(project_id, conn)
        return report if isinstance(report, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("activation: dq_report_unavailable project=%s: %s",
                       project_id, type(exc).__name__)
        return {}


def _deferred_datastream_awaiting_setup(conn, review: dict[str, Any]) -> str | None:
    """Le Datastream que ce brouillon a deja materialise et qui attend son mapping.

    Rend son id quand TROIS choses sont vraies ensemble, et None autrement : le
    brouillon a materialise quelque chose, cette ligne existe encore dans le meme
    projet, et elle ne porte NI plan NI mapping. La derniere condition est celle
    qui compte : un flux qui a deja ses pointeurs n'attend rien, et ecrire
    par-dessus serait une reprise silencieuse, pas un raccord.

    Les deux pointeurs sont exiges ensemble a dessein. Un flux qui n'en aurait
    qu'un serait dans un etat que rien ne produit ; le completer masquerait
    l'anomalie au lieu de la laisser se voir.
    """
    draft_id, project_id = review["draft_id"], review["project_id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.materialized_datastream_id "
            "  FROM app.datastream_setup_drafts d "
            " WHERE d.id = %s AND d.project_id = %s",
            (draft_id, project_id),
        )
        row = cur.fetchone()
        candidate = row[0] if row else None
        if not candidate:
            return None
        cur.execute(
            "SELECT current_plan_version_id, current_mapping_version_id "
            "  FROM app.datastreams WHERE id = %s AND project_id = %s",
            (candidate, project_id),
        )
        pointers = cur.fetchone()
    if pointers is None:
        return None
    return candidate if pointers[0] is None and pointers[1] is None else None


def materialize_draft_mutation(
    conn,
    *,
    operation_id: str,
    review: dict[str, Any],
    actor: str,
):
    """Create the stable Draft, non-live versions and one candidate in the caller transaction."""
    from core.business_taxonomy import create_link  # noqa: PLC0415
    from core.datastream_field_mapping import save_field_mapping  # noqa: PLC0415
    from core.datastream_intents import save_datastream_intent  # noqa: PLC0415
    from core.datastream_projection import compile_projection  # noqa: PLC0415
    from core.datastream_publication import create_execution  # noqa: PLC0415
    from core.operations import MutationResult  # noqa: PLC0415
    from core.run_origins import FIRST_CANDIDATE, stamp_origin  # noqa: PLC0415

    project_id = review["project_id"]
    draft_id = review["draft_id"]
    org_id = review["org_id"]
    # UN BROUILLON ABANDONNE NE SE PUBLIE PLUS (AI-336, 2026-08-31). Ce n'est PAS
    # une redite de la garde de `persist_final_review` : une revue finale gelee
    # AVANT l'abandon reste confirmable apres, et les deux UPDATE plus bas
    # portent `AND state IN ('draft','materialized')` -- ils auraient donc
    # silencieusement touche zero ligne pendant qu'un Datastream etait cree, un
    # Datastream dont le brouillon ne garde aucune trace.
    from core.datastream_preconfiguration import require_live_draft  # noqa: PLC0415

    require_live_draft(conn, project_id=project_id, draft_id=draft_id)
    bundle = review["confirmed_intent_bundle"]
    source = review["plan_intent"]["source"]
    # LE RACCORD. Un flux entrant se materialise AVANT sa premiere livraison --
    # c'est la seule facon d'avoir une adresse ou recevoir. Son plan et son
    # mapping viennent donc PLUS TARD, quand le fichier a revele sa forme. Sans
    # ce raccord, ce second passage creait un Datastream neuf a cote du premier :
    # l'adresse emise, les evidences retenues et le nouveau mapping se
    # retrouvaient sur deux lignes differentes, aucune complete.
    completing = _deferred_datastream_awaiting_setup(conn, review)
    datastream_id = completing or f"ds_{ULID()}"
    source_kind = source["kind"]
    role = str(bundle.get("data_role") or "")
    from core.datastreams import DATA_ROLES, schedule_mode_for_cadence  # noqa: PLC0415

    if role not in DATA_ROLES:
        raise ActivationValidationError("Confirmed Datastream data role is invalid")
    module_name = source.get("module") if source_kind == "connector_pull" else None
    connection_ref_id = source.get("connection_ref_id") if source_kind == "connector_pull" else None
    report_profile_id = source.get("report_id") if source_kind == "connector_pull" else None
    plan_schedule_mode = str((review["plan_intent"].get("schedule") or {}).get("mode") or "manual")
    # AI-217: one table, in `core.datastreams`. This dictionary was one of three
    # copies and none of them knew `weekly`, so the cadence a person chose was
    # written to the column as `manual` -- the opposite of what they asked for.
    schedule_mode = schedule_mode_for_cadence(plan_schedule_mode)
    source_owner = {
        "selected_account_ref": source.get("selected_account_ref"),
        "declared_writer": (source.get("external_object") or {}).get("writer_identity"),
        "managed_feed_source_ref": (source.get("managed_feed") or {}).get("source_ref"),
        # AI-91: the Template binding travels under its own name all the way to the
        # Datastream row. `resolve_file_source_producer` reads THIS key; it no
        # longer sniffs an `fst_` prefix out of the catch-all above.
        "managed_feed_template_ref": (source.get("managed_feed") or {}).get("template_ref"),
    }
    # The operator chose a Source Account three screens ago; until migration 211
    # the choice only ever reached `config.source_owner`, which nothing reads at
    # extraction time. It is now a column, and the composite FK checks that the
    # account really belongs to the authorization this Datastream points at.
    selected_account_ref = (
        source.get("selected_account_ref") if source_kind == "connector_pull" else None
    )
    with conn.cursor() as cur:
        # Completer, ce n'est pas recreer : la ligne existe, son adresse a ete
        # emise contre elle et ses evidences y sont rattachees. Seule sa
        # configuration se rafraichit, pour que ce qui vient d'etre confirme soit
        # ce qui est ecrit.
        if completing:
            cur.execute(
                "UPDATE app.datastreams SET name=%s, config=%s::jsonb, "
                "       data_role=%s, updated_at=NOW() "
                " WHERE id=%s AND project_id=%s",
                (
                    bundle["datastream_name"],
                    json.dumps(
                        datastream_config(source, source_owner=source_owner),
                        sort_keys=True,
                    ),
                    role,
                    datastream_id,
                    project_id,
                ),
            )
        else:
            cur.execute(
            """INSERT INTO app.datastreams
               (id,project_id,org_id,name,module_name,connection_ref_id,source_account_id,
                report_profile_id,
                enabled,schedule_mode,refetch_days,date_window_days,config,created_by,
                lifecycle_state,source_kind,data_role)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,3,30,%s::jsonb,%s,'draft',%s,%s)""",
            (
                datastream_id,
                project_id,
                org_id,
                bundle["datastream_name"],
                module_name,
                connection_ref_id,
                selected_account_ref,
                report_profile_id,
                schedule_mode,
                json.dumps(
                    datastream_config(source, source_owner=source_owner),
                    sort_keys=True,
                ),
                actor,
                source_kind,
                role,
            ),
            )
        cur.execute(
            """INSERT INTO app.project_flux (project_id,flux_id,org_id)
               VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
            (project_id, datastream_id, org_id),
        )
    # A feed whose file has not arrived carries no schema, so there is nothing to
    # version: no plan, no mapping, no candidate. The Datastream exists, in
    # `draft`, with its channel -- which is exactly what an address is issued
    # against. The first delivery is the evidence that mints the rest.
    if review.get("deferred_until_first_delivery"):
        return _materialize_without_versions(
            conn,
            review=review,
            actor=actor,
            operation_id=operation_id,
            datastream_id=datastream_id,
            org_id=org_id,
            project_id=project_id,
            draft_id=draft_id,
            bundle=bundle,
        )
    plan = save_datastream_intent(
        datastream_id=datastream_id,
        project_id=project_id,
        intent=review["plan_intent"],
        identity=actor,
        idempotency_key=f"{operation_id}:plan",
        conn=conn,
        capabilities=review.get("connector_capabilities"),
        commit=False,
        advance_pointer=False,
    )
    mapping = save_field_mapping(
        datastream_id=datastream_id,
        project_id=project_id,
        mapping_payload=review["mapping_payload"],
        identity=actor,
        idempotency_key=f"{operation_id}:mapping",
        conn=conn,
        pinned_plan_version_id=plan["id"],
        advance_pointer=False,
        commit=False,
    )
    # LA PROPOSITION QUE LA PUBLICATION CONSOMME, ECRITE LA OU LA VERSION NAIT.
    #
    # `POST /api/governance/publication-reviews` -- le SEUL chemin vers
    # `confirm_and_publish` -- prend un `proposal_id` de `app.mapping_proposals`,
    # et l'ecran ne montre « Publish this mapping... » que s'il en existe une en
    # etat `ready`. Or `create_mapping_proposal` n'avait qu'un appelant : un
    # outil MCP. Mesure 2026-08-12 : zero proposition pour les neuf Datastreams
    # de ce Projet, donc AUCUN ne pouvait rendre son mapping vivant depuis
    # l'interface -- la version existait, liait ses champs, et rien en aval ne
    # pouvait la lire.
    #
    # La creation d'un Datastream produit donc sa proposition en meme temps que
    # sa version. La publication reste un acte GOUVERNE et distinct : ce qui
    # manquait n'etait pas la confirmation, c'etait l'objet a confirmer.
    try:
        from core.mapping_versions import MODE_DELEGATED, create_mapping_proposal  # noqa: PLC0415

        create_mapping_proposal(
            conn,
            datastream_id=datastream_id,
            project_id=project_id,
            effective_org_id=org_id,
            mapping_payload=review["mapping_payload"],
            plan_version_id=plan["id"],
            actor=actor,
            # `mode` est le MODE AGENTIQUE, pas une etiquette libre : seuls
            # `delegated` et `autonomous` permettent l'effet `persist`, et un
            # mode inconnu echoue en fermeture. Une personne a cree ce
            # Datastream et l'a confirme DEUX fois -- c'est une delegation
            # explicite, pas une initiative autonome.
            mode=MODE_DELEGATED,
            idempotency_key=f"{operation_id}:mapping-proposal",
            authorized=True,
            # LE GATE DQ ECHOUE EN FERMETURE SUR UNE PREUVE ABSENTE, et c'est
            # juste : « a proposal must not ride over a red DQ gate ». Mais
            # l'absence de preuve n'est pas une preuve rouge -- un Datastream qui
            # vient de naitre n'a aucun probleme ouvert, et le rapport le dit.
            # Il est LU, jamais suppose : mesure locale du 2026-08-12, six gates
            # sur sept passaient et seul celui-ci refusait, faute de lecture.
            dq_evidence=_dq_evidence_for(conn, project_id),
        )
    except Exception as exc:  # noqa: BLE001 -- une proposition absente ne perd pas le Datastream
        logger.warning(
            "activation: mapping_proposal_not_created datastream=%s: %s",
            datastream_id,
            type(exc).__name__,
        )

    _ensure_event_configuration(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        org_id=org_id,
        actor=actor,
        operation_id=operation_id,
    )

    # THE OPERATOR'S APPROVAL REACHES THE COMPILER. `compile_projection` refuses
    # an over-limit grain unless `approved` says a person took the cost on -- the
    # repair its own issue names is `approve_or_reduce_grain`. This call passed
    # no approval at all, so the approve half did not exist on this path and only
    # "reduce" was reachable: a grain of three dimensions estimates 1e9 against a
    # 1e6 default (no manifest declares `cardinality_signal`, so every dimension
    # counts as 1000), and the wizard refused it with no way to accept it.
    #
    # The approval is the one the review already collected: the cost/quota
    # warning cannot be frozen unacknowledged (`freeze_final_review` refuses),
    # so an acknowledged cost item IS the explicit governed approval.
    acknowledged = set(review.get("acknowledged_warning_ids") or [])
    cost_approved = any(
        key.startswith("cost_quota.") or key.startswith("grain.") for key in acknowledged
    )
    projection = compile_projection(mapping, approved=cost_approved)
    if not projection.get("executable"):
        # LE PLAN SAIT DEJA POURQUOI ; CE REFUS LE JETAIT.
        #
        # `compile_projection` rend un plan dont chaque `issue` porte un `code`,
        # un `path` et les `field_ids` concernes -- << rien n'est silencieusement
        # ecarte ou coerce >>, dit son propre docstring. Ce refus n'en gardait
        # rien, et l'operateur recevait une phrase sans un seul pas suivant : ni
        # quel champ, ni quelle regle.
        #
        # Mesure du 2026-08-08 : c'est le douzieme et dernier appel du parcours
        # fondateur, et il etait impossible de dire depuis l'exterieur si le
        # mapping etait pauvre ou si le compilateur refusait a tort -- deux
        # causes menant a des corrections opposees. Meme classe que le refus de
        # taille repare le meme jour : une borne protege, elle n'a pas a
        # protéger le diagnostic.
        issues = projection.get("issues") or []
        named = "; ".join(
            f"{issue.get('code')} at {issue.get('path')}"
            + (f" ({', '.join(str(f) for f in issue.get('field_ids') or [])})"
               if issue.get("field_ids") else "")
            for issue in issues[:5]
            if isinstance(issue, dict)
        )
        raise ActivationValidationError(
            "Confirmed mapping does not compile to an executable projection: "
            + (f"{len(issues)} issue(s) -- {named}" if named
               else "the plan reported no issue, which is itself the defect")
        )
    # Story 63.7: a Datastream confirmed through setup mints its FIRST candidate
    # -- the same origin `datastream_first_candidate` stamps, because it is the
    # same event seen from the other entry point.
    projection = stamp_origin(projection, FIRST_CANDIDATE)
    candidate = create_execution(
        datastream_id,
        project_id,
        plan["id"],
        mapping["id"],
        projection,
        actor,
        f"{operation_id}:candidate",
        conn,
    )
    for domain_id in bundle.get("business_domain_ids") or []:
        create_link(
            conn,
            org_id=org_id,
            project_id=project_id,
            taxonomy_type="business_domain",
            taxonomy_id=domain_id,
            target_type="datastream",
            target_id=datastream_id,
            relation_type="governs",
            actor=actor,
            reason="Confirmed Datastream setup",
        )
    materialization_id = f"dsmat_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            # UNE LIGNE PAR ACTE, jamais une par brouillon. Un flux entrant en
            # produit deux : la creation -- qui donne l'adresse et laisse les
            # versions a NULL -- puis la completion, quand la premiere livraison
            # a revele son schema. La table est IMMUABLE (migration 137) et cela
            # doit le rester : on n'efface pas ce qui a ete atteste. La
            # completion AJOUTE donc une ligne, elle ne corrige pas la premiere,
            # et l'unicite porte desormais sur la revue finale (migration 231) --
            # rejouer la MEME revue reste refuse.
            """INSERT INTO app.datastream_setup_materializations
               (id,project_id,draft_id,final_review_id,datastream_id,plan_version_id,
                mapping_version_id,candidate_execution_id,operation_id,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                materialization_id,
                project_id,
                draft_id,
                review["final_review_id"],
                datastream_id,
                plan["id"],
                mapping["id"],
                candidate["id"],
                operation_id,
                actor,
            ),
        )
        cur.execute(
            """UPDATE app.datastream_setup_drafts
                  SET state='materialized',materialized_datastream_id=%s,updated_at=NOW()
                WHERE id=%s AND project_id=%s
                  AND state IN ('draft','materialized')""",
            (datastream_id, draft_id, project_id),
        )
    from core.queue import enqueue_activation_work  # noqa: PLC0415

    queued = enqueue_activation_work(
        kind="candidate_materialization",
        project_id=project_id,
        # NO draft_id here, on purpose (AI-321, 2026-08-28): a candidate job's
        # scope is (datastream, execution) and `enqueue_activation_work` refuses
        # a draft on that kind. The managed_feed driver still resolves the
        # staged asset inside the draft scope, and the worker reads the draft
        # from `app.datastream_setup_materializations`, written just above in
        # this same transaction -- the row is the binding, not the job.
        datastream_id=datastream_id,
        execution_id=candidate["id"],
        correlation_id=candidate["id"],
        payload={
            "mode": review["mode"],
            "channel": review.get("channel"),
            "final_review_id": review["final_review_id"],
        },
        requested_by=actor,
        conn=conn,
    )
    result = {
        "materialization_id": materialization_id,
        "datastream_id": datastream_id,
        "lifecycle_state": "draft",
        "plan_version_id": plan["id"],
        "mapping_version_id": mapping["id"],
        "candidate_execution_id": candidate["id"],
        "current_published_execution_id": None,
        "schedule_active": False,
        "candidate_job_id": queued["job_id"],
    }
    return MutationResult(
        outcome="succeeded",
        before_hash=None,
        after_hash=_hash(result),
        result=result,
        outbox_payload={"event": "datastream.draft_materialized", **result},
    )


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return deepcopy(default)
    return json.loads(value) if isinstance(value, str) else deepcopy(value)


def read_preview(conn, *, project_id: str, draft_id: str, preview_id: str) -> dict[str, Any]:
    """Return one safe immutable preview plus an exact current/stale verdict."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.id,p.draft_revision_id,p.proposal_id,p.observation_id,p.mode,
                      p.dependency_snapshot,p.dependency_hash,p.mapping_hash,p.processing_hash,
                      p.safe_evidence,p.evidence_hash,p.created_at,d.current_revision_id,
                      d.current_proposal_id
                 FROM app.datastream_setup_previews p
                 JOIN app.datastream_setup_drafts d
                   ON d.id=p.draft_id AND d.project_id=p.project_id
                WHERE p.id=%s AND p.draft_id=%s AND p.project_id=%s""",
            (preview_id, draft_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ActivationValidationError("Preview not found")
    safe = _json_value(row[9], {})
    return {
        "preview_ref": row[0],
        "draft_revision_ref": row[1],
        "proposal_ref": row[2],
        "observation_ref": row[3],
        "mode": row[4],
        "dependency_snapshot": _json_value(row[5], {}),
        "dependency_hash": row[6],
        "mapping_hash": row[7],
        "processing_hash": row[8],
        "safe_evidence": safe,
        "status": safe.get("status", "blocked"),
        "evidence_hash": row[10],
        "created_at": row[11].isoformat() if hasattr(row[11], "isoformat") else row[11],
        "is_stale": row[1] != row[12] or row[2] != row[13],
    }


#: Le budget d'un snapshot de revue finale. Il borne le CONTRAT DE
#: MATERIALISATION complet -- plan, mapping, planning, capacites -- et pas un
#: resume, ce qui est la raison pour laquelle il se remplit vite.
#: Aligne sur la migration 253, qui porte la mesure et la raison (un tableur
#: de 22 colonnes pesait 16474 o pour un budget de 16384 -- quatre-vingt-dix
#: octets, et aucun Datastream ne pouvait naitre d'un fichier ordinaire). Les deux
#: chiffres DOIVENT rester egaux : un garde plus large laisse passer une charge
#: que la contrainte refusera en CheckViolation, et un garde plus etroit refuse
#: ce que la base accepterait.
_FINAL_REVIEW_MAX_BYTES = 65536


def persist_final_review(
    conn,
    *,
    project_id: str,
    draft_id: str,
    preview_id: str,
    actor: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Append/replay the exact reviewed consequence snapshot."""
    # UN BROUILLON ABANDONNE NE SE REVOIT PAS (AI-336, 2026-08-31). La garde est
    # ici, sur l'ECRIVAIN, et non chez le compositeur : les deux portes du
    # parcours -- REST et MCP -- passent par cette fonction, une garde chez l'une
    # d'elles seulement serait une garde qui ne couvre pas sa classe.
    from core.datastream_preconfiguration import require_live_draft  # noqa: PLC0415

    require_live_draft(conn, project_id=project_id, draft_id=draft_id)
    content_hash = str(snapshot.get("content_hash") or "")
    if len(content_hash) != 64 or set(_walk_keys(snapshot)) & _SECRET_KEYS:
        raise ActivationValidationError("Final review snapshot is unsafe")
    # MESURER CE QUE POSTGRES MESURERA, PAS AUTRE CHOSE.
    #
    # La colonne porte `CHECK (app.safe_preconfiguration_evidence(review_snapshot))`
    # (migration 137), dont la clause de taille est
    # `octet_length(value::text) <= 8192`. Or `jsonb::text` rend AVEC des espaces
    # -- `{"a": 1, "b": 2}` -- la ou `_canonical` rend compact. Sur un objet
    # imbrique l'ecart depasse 20 %.
    #
    # Ce garde comparait donc du JSON compact a un plafond qui s'applique a du
    # JSON espace : une charge pouvait le PASSER et faire quand meme exploser la
    # contrainte, et la CheckViolation ressortait en 503
    # `preconfiguration_unavailable` -- un fourre-tout qui ne dit rien.
    # Mesure du 2026-08-08, sur le chemin de l'assistant. Le garde n'etait pas
    # trop strict, il etait DECORATIF pour exactement les charges limites.
    size = len(json.dumps(snapshot, sort_keys=True, separators=(", ", ": "),
                          ensure_ascii=False).encode("utf-8"))
    if size > _FINAL_REVIEW_MAX_BYTES:
        # UN REFUS QUI NE DIT PAS CE QUI DEBORDE N'EST PAS REPARABLE.
        # << exceeds the safe limit >> laissait un operateur -- et un robot de
        # marche -- sans le moindre pas suivant : ni la taille atteinte, ni la
        # part qui la fait. Mesure du 2026-08-08 : le refus a arrete la marche
        # du parcours fondateur a l'etape 8, et il a fallu lire le code pour
        # savoir seulement quel etait le plafond.
        #
        # Les trois plus grosses cles sont nommees avec leur poids. La borne
        # protege le snapshot, elle n'a pas a proteger le diagnostic.
        weights = sorted(
            (
                (key, len(json.dumps(value, sort_keys=True, separators=(", ", ": "),
                                     ensure_ascii=False).encode("utf-8")))
                for key, value in snapshot.items()
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )[:3]
        heaviest = ", ".join(f"{key} {weight} o" for key, weight in weights)
        raise ActivationValidationError(
            f"Final review snapshot exceeds the safe limit: {size} bytes for a "
            f"{_FINAL_REVIEW_MAX_BYTES} byte budget. Heaviest keys: {heaviest}."
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id,review_snapshot,created_at FROM app.datastream_setup_final_reviews "
            "WHERE project_id=%s AND draft_id=%s AND content_hash=%s",
            (project_id, draft_id, content_hash),
        )
        existing = cur.fetchone()
        if existing is not None:
            return {
                **_json_value(existing[1], {}),
                "final_review_ref": existing[0],
                "created_at": existing[2].isoformat()
                if hasattr(existing[2], "isoformat")
                else existing[2],
                "idempotent_replay": True,
            }
        review_id = f"dsfr_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_setup_final_reviews
               (id,project_id,draft_id,preview_id,review_snapshot,
                warning_acknowledgements,content_hash,created_by)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)""",
            (
                review_id,
                project_id,
                draft_id,
                preview_id,
                _canonical(snapshot),
                _canonical(snapshot.get("acknowledged_warning_ids") or []),
                content_hash,
                actor,
            ),
        )
    return {
        **deepcopy(snapshot),
        "final_review_ref": review_id,
        "idempotent_replay": False,
    }


def read_final_review(
    conn, *, project_id: str, draft_id: str, final_review_id: str
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT r.review_snapshot,r.content_hash,r.created_at,p.draft_revision_id,
                      p.proposal_id,d.current_revision_id,d.current_proposal_id
                 FROM app.datastream_setup_final_reviews r
                 JOIN app.datastream_setup_previews p
                   ON p.id=r.preview_id AND p.draft_id=r.draft_id AND p.project_id=r.project_id
                 JOIN app.datastream_setup_drafts d
                   ON d.id=r.draft_id AND d.project_id=r.project_id
                WHERE r.id=%s AND r.draft_id=%s AND r.project_id=%s""",
            (final_review_id, draft_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ActivationValidationError("Final review not found")
    snapshot = _json_value(row[0], {})
    return {
        **snapshot,
        "final_review_ref": final_review_id,
        "content_hash": row[1],
        "created_at": row[2].isoformat() if hasattr(row[2], "isoformat") else row[2],
        "is_stale": row[3] != row[5] or row[4] != row[6],
    }


def complete_candidate_from_adapter(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    execution_id: str,
    actor: str,
    adapter_result: dict[str, Any],
) -> dict[str, Any]:
    """Advance a real adapter artifact to Ready, or fail closed before any pointer change."""
    from core.datastream_publication import (  # noqa: PLC0415
        STATE_CREATED,
        STATE_FAILED,
        STATE_LOADING,
        STATE_READY,
        STATE_VALIDATING,
        advance_state,
        run_dq_gates,
    )

    if not isinstance(adapter_result, dict) or adapter_result.get("adapter_verified") is not True:
        raise ActivationValidationError("A verified candidate adapter result is required")
    if adapter_result.get("placeholder") is True or set(_walk_keys(adapter_result)) & _SECRET_KEYS:
        raise ActivationValidationError(
            "Placeholder or secret-bearing candidate evidence is forbidden"
        )
    artifact_ref = str(adapter_result.get("artifact_ref") or "")
    content_hash = str(adapter_result.get("content_hash") or "")
    validated_hash = str(adapter_result.get("validated_content_hash") or "")
    artifact_hash = str(adapter_result.get("artifact_hash") or content_hash)
    row_count = adapter_result.get("row_count")
    if execution_id not in artifact_ref or any(
        len(value) != 64 for value in (content_hash, validated_hash, artifact_hash)
    ):
        raise ActivationValidationError("Candidate artifact provenance is invalid")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        raise ActivationValidationError("Candidate row count is invalid")
    safe = {
        key: deepcopy(adapter_result[key])
        for key in (
            "adapter_verified",
            "placeholder",
            "adapter_ref",
            "schema_hash",
            "coverage",
            "freshness",
            "dq",
            "output_plan",
            "candidate_columns",
        )
        if key in adapter_result
    }
    if len(_canonical(safe).encode("utf-8")) > 8192:
        raise ActivationValidationError("Candidate evidence exceeds the safe limit")

    with conn.cursor() as cur:
        cur.execute(
            """SELECT state,projection_plan_ref FROM app.datastream_executions
                WHERE id=%s AND datastream_id=%s AND project_id=%s FOR UPDATE""",
            (execution_id, datastream_id, project_id),
        )
        execution = cur.fetchone()
    if execution is None:
        raise ActivationValidationError("Candidate not found")
    state = execution[0]
    projection = _json_value(execution[1], {})
    if state == STATE_CREATED:
        advance_state(
            execution_id,
            STATE_CREATED,
            STATE_LOADING,
            actor,
            conn,
            project_id=project_id,
        )
        state = STATE_LOADING
    if state != STATE_LOADING:
        raise ActivationValidationError("Candidate worker cannot resume from this state")
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE app.datastream_executions
                  SET artifact_ref=%s,artifact_hash=%s,adapter_ref=%s,
                      candidate_evidence=%s::jsonb,validated_content_hash=%s,updated_at=NOW()
                WHERE id=%s AND datastream_id=%s AND project_id=%s AND state='loading'""",
            (
                artifact_ref,
                artifact_hash,
                str(adapter_result.get("adapter_ref") or "registered.adapter"),
                _canonical(safe),
                validated_hash,
                execution_id,
                datastream_id,
                project_id,
            ),
        )
    advance_state(
        execution_id,
        STATE_LOADING,
        STATE_VALIDATING,
        actor,
        conn,
        project_id=project_id,
        content_hash=content_hash,
        row_count=row_count,
    )
    issues = run_dq_gates(
        execution_id,
        project_id,
        conn,
        validated_content_hash=validated_hash,
        plan_source_schema_hash=adapter_result.get("plan_source_schema_hash"),
        current_capability_fingerprint=adapter_result.get("current_capability_fingerprint"),
        landing_schema_hash=adapter_result.get("schema_hash"),
        plan_declared_schema_hash=adapter_result.get("plan_declared_schema_hash"),
    )
    if issues:
        advance_state(
            execution_id,
            STATE_VALIDATING,
            STATE_FAILED,
            actor,
            conn,
            project_id=project_id,
            error_code="dq_gate_failed",
            error_detail="candidate DQ gates failed; Current was preserved",
        )
        return {"execution_id": execution_id, "state": STATE_FAILED, "dq": {"blocking": issues}}
    advance_state(
        execution_id,
        STATE_VALIDATING,
        STATE_READY,
        actor,
        conn,
        project_id=project_id,
    )
    return {
        "execution_id": execution_id,
        "state": STATE_READY,
        "artifact_ref": artifact_ref,
        "projection_hash": _hash(projection),
        "dq": {"blocking": []},
    }


def read_candidate_review(
    conn, *, project_id: str, datastream_id: str, execution_id: str
) -> dict[str, Any]:
    """Build the exact scoped review from persisted worker and setup evidence."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.state,e.artifact_ref,e.content_hash,e.row_count,e.plan_version_id,
                      e.mapping_version_id,e.projection_plan_ref,e.candidate_evidence,
                      d.current_published_execution_id,r.review_snapshot,d.schedule_mode,
                      f.candidate_schema_fingerprint
                 FROM app.datastream_executions e
                 JOIN app.datastreams d ON d.id=e.datastream_id AND d.project_id=e.project_id
                 LEFT JOIN app.datastream_setup_materializations m
                   ON m.candidate_execution_id=e.id AND m.datastream_id=e.datastream_id
                 LEFT JOIN app.datastream_setup_final_reviews r ON r.id=m.final_review_id
                 -- The managed-file path keeps the same fact under its own name,
                 -- on its own table (`execution_id` is UNIQUE there, migration
                 -- 194), and never copies it into `candidate_evidence`. Joined
                 -- rather than assumed absent, so a CSV/feed publication carries
                 -- a comparable schema hash exactly like a connector pull.
                 LEFT JOIN app.managed_file_dispatches f
                   ON f.execution_id=e.id AND f.datastream_id=e.datastream_id
                  AND f.project_id=e.project_id
                WHERE e.id=%s AND e.datastream_id=%s AND e.project_id=%s""",
            (execution_id, datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ActivationValidationError("Candidate not found")
    projection = _json_value(row[6], {})
    evidence = _json_value(row[7], {})
    setup = _json_value(row[9], {})
    schedule = setup.get("schedule") or {
        "activation_kind": "manual" if row[10] == "manual" else "schedule",
        "schedule_mode": row[10],
        "next_run_at": None,
    }
    # AI-217: the second of the three copies. Same table as the other two.
    from core.datastreams import schedule_mode_for_cadence  # noqa: PLC0415

    schedule.setdefault(
        "schedule_mode",
        schedule_mode_for_cadence(
            (setup.get("plan_intent") or {}).get("schedule", {}).get("mode")
        ),
    )
    return build_candidate_review(
        {
            "project_id": project_id,
            "datastream_id": datastream_id,
            "execution_id": execution_id,
            "state": row[0],
            "artifact_ref": row[1],
            "content_hash": row[2],
            "schema_hash": candidate_schema_hash(
                evidence, projection, {"candidate_schema_fingerprint": row[11]}
            ),
            "row_count": row[3],
            "plan_version_id": row[4],
            "mapping_version_id": row[5],
            "projection_hash": _hash(projection),
            "output_plan": projection,
            "candidate_columns": evidence.get("candidate_columns") or [],
            "adapter_verified": evidence.get("adapter_verified"),
            "placeholder": evidence.get("placeholder", False),
            "dq": evidence.get("dq") or {"blocking": []},
            "schedule": schedule,
            "expected_current_execution_id": row[8],
        }
    )


def read_materialization_status(conn, *, project_id: str, draft_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.id,m.datastream_id,m.plan_version_id,m.mapping_version_id,
                      m.candidate_execution_id,e.state,j.id,j.state,j.error_code,
                      d.current_published_execution_id,d.lifecycle_state
                 FROM app.datastream_setup_materializations m
                 JOIN app.datastream_executions e ON e.id=m.candidate_execution_id
                 JOIN app.datastreams d ON d.id=m.datastream_id AND d.project_id=m.project_id
                 LEFT JOIN app.datastream_activation_jobs j
                   ON j.execution_id=m.candidate_execution_id AND j.project_id=m.project_id
                WHERE m.project_id=%s AND m.draft_id=%s""",
            (project_id, draft_id),
        )
        row = cur.fetchone()
    if row is None:
        return {"state": "not_materialized", "draft_ref": draft_id}
    return {
        "state": "materialized",
        "materialization_ref": row[0],
        "datastream_ref": row[1],
        "plan_version_ref": row[2],
        "mapping_version_ref": row[3],
        "candidate_execution_ref": row[4],
        "candidate_state": row[5],
        "job_ref": row[6],
        "job_state": row[7],
        "job_error_code": row[8],
        "current_published_execution_ref": row[9],
        "lifecycle_state": row[10],
    }


#: The wizard's channel vocabulary translated into the DELIVERY vocabulary that
#: `app.datastreams.config.channels` is read in. Two spellings of one thing had
#: already been reconciled in `inbound_credentials._get_datastream_status`, which
#: folds `inbound_email` into `email`; the other writer of this key
#: (`import_templates.create_inbound_datastream`, `VALID_CHANNELS`) writes
#: `email`. This map writes what that reader and that writer already agree on,
#: rather than leaning on a translation to absorb a third spelling.
#:
#: `file_upload` MAPS TO `upload` since AI-321 (2026-08-29). It was absent on
#: purpose -- "nothing is delivered to it, no address is issued" -- but
#: `config.channels` is ALSO what `inbound_ingest` reads to admit an ingress
#: (`channel 'upload' is not enabled for datastream ... (allowed: [])`), and
#: the upload door has always written `upload` there
#: (`import_templates.create_inbound_datastream`). Two writers of one config,
#: one reader: a file Datastream the assistant materialised could never accept
#: the very upload it was created for. `google_sheets` stays absent: it is
#: read, not received.
DELIVERY_CHANNELS = {"inbound_email": "email", "webhook": "webhook", "file_upload": "upload"}


def datastream_config(
    source_contract: dict[str, Any], *, source_owner: dict[str, Any]
) -> dict[str, Any]:
    """The `app.datastreams.config` a materialization writes.

    STORY 57.3 CLOSED A HOLE HERE, and it is the hole that story opened. Step 1
    now tells an operator "the address is issued against the Datastream, on its
    Overview, once this draft is created". That sentence was false at the next
    link: `inbound_credentials._get_datastream_status` reads
    `COALESCE(config->'channels','[]'::jsonb)`, materialization never wrote that
    key, so the set came back EMPTY and `_require_receivable(channel="email")`
    raised "delivery channel is not configured". No address could ever be issued
    for a Datastream the wizard created.

    Nobody saw it because nothing read the field on this path -- the four-column
    compatibility branch of that reader only covers offline doubles, never a real
    row. A declaration that arms nothing is theatre, so the declared channel is
    written here, in the same statement that creates the row.
    """
    config: dict[str, Any] = {"source_owner": source_owner}
    managed_feed = source_contract.get("managed_feed")
    managed_feed = managed_feed if isinstance(managed_feed, dict) else {}
    channel = DELIVERY_CHANNELS.get(str(managed_feed.get("channel") or ""))
    if channel:
        config["channels"] = [channel]
        # THE OTHER HALF OF THE SAME HOLE. 57.3 wrote the channel and stopped
        # there, but `inbound_credentials` reads the connector under
        # `COALESCE(config->>'connector_name', module_name)` (:363, :1262) and a
        # managed feed has no `module_name`. So `datastream_matches_connector`
        # answered False and `_require_receivable` refused a Datastream that was
        # otherwise perfectly receivable -- measured 2026-08-07, right after the
        # first inbound Datastream the assistant ever materialised.
        #
        # `managed_feed` is the connector these deliveries arrive through: it is
        # what `app.connector_installations`, `app.connector_domain_configs` and
        # `app.connector_activations` all carry, and what the other writer
        # (`import_templates.create_inbound_datastream`) has always written.
        config["connector_name"] = "managed_feed"
    contract = managed_feed.get("channel_contract")
    if contract:
        # The DECLARED channel contract, carried whole so the Datastream that
        # receives deliveries can apply the sender declaration made at step 1. A
        # sender allowlist stored nowhere would govern nothing -- and a control
        # that governs nothing is worse than an absent one, because it reads as
        # protection.
        config["channel_contract"] = contract
    return config


def _channel_contract_declaration(source: dict[str, Any]) -> dict[str, Any]:
    """The inbound channel contract the operator declared, projected and bounded.

    Only the two clauses an operator can declare travel: the expected arrival
    interval and the sender allowlist. Everything else about a channel is READ
    (the verified domain, the issued capability, the bound Template), so copying
    it here would turn a reading into a claim.
    """
    from core.inbound_discovery import normalize_allowed_senders  # noqa: PLC0415

    declared = source.get("channel_contract")
    if not isinstance(declared, dict):
        return {}
    contract: dict[str, Any] = {}
    minutes = _arrival_minutes(declared.get("expected_interval_minutes"))
    if minutes is not None:
        contract["expected_interval_minutes"] = minutes
    senders = normalize_allowed_senders(declared.get("allowed_senders"))
    if senders:
        contract["allowed_senders"] = senders
    return contract


def _arrival_minutes(raw: Any) -> int | None:
    """A declared interval in minutes, or None. NEVER a default."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        minutes = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return minutes if 1 <= minutes <= 525_600 else None


def _declared_arrival_interval(
    source: dict[str, Any], requested_schedule: dict[str, Any]
) -> int | None:
    """What the operator promised about arrival frequency, or None.

    The channel contract is the declaration's home (step 1); the schedule block
    is read as well so a REST client that only knows the older shape still
    declares rather than silently gets a default. Absent in both is absent.
    """
    return _arrival_minutes(
        (source.get("channel_contract") or {}).get("expected_interval_minutes")
        if isinstance(source.get("channel_contract"), dict)
        else None
    ) or _arrival_minutes(requested_schedule.get("expected_interval_minutes"))


def compile_materialization_contract(
    *,
    frozen_review: dict[str, Any],
    operator_input: dict[str, Any],
    observation: dict[str, Any],
    org_id: str,
    actor: str,
    timezone_name: str,
    #: Story 48.3: WHERE this clock came from. `scheduling_default_unconfirmed` means
    #: the Project has confirmed no reporting timezone and the scheduler is running on
    #: UTC for want of anything else -- which is a scheduling fact, not a statement
    #: about this Project's reporting day boundary. Recording the origin is what stops
    #: a downstream reader treating the second as the first.
    timezone_origin: str = "project_configuration",
    connector_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile exact plan/mapping/schedule inputs from persisted review evidence."""
    review = deepcopy(frozen_review)
    bundle = review.get("confirmed_intent_bundle") or {}
    mode = str(operator_input.get("mode") or "")
    source = operator_input.get("source") or {}
    configure = operator_input.get("configure") or {}
    if mode not in MODES or not bundle.get("datastream_name") or not bundle.get("data_role"):
        raise ActivationValidationError("Datastream identity and governed role are required")
    fields = list(bundle.get("field_mappings") or [])
    grain = list(bundle.get("joint_grain") or [])
    # LE REPORT TIENT A L'ABSENCE DE SCHEMA, PAS AU CANAL. Un canal entrant n'a
    # rien a versionner tant qu'aucun fichier n'est arrive -- mais des que la
    # premiere livraison est observee, il a exactement ce qu'il faut et le report
    # doit CESSER. Le garder attache au seul canal figeait le flux dans son etat
    # differe pour toujours : la seconde materialisation reprenait le chemin sans
    # versions et rejouait l'ecriture du meme registre.
    deferred = awaits_first_delivery(operator_input) and (not fields or not grain)
    if (not fields or not grain) and not deferred:
        raise ActivationValidationError("A complete reviewed mapping and joint grain are required")

    # Story 60.6 / AI-249. EXCLUDING ONE COLUMN USED TO MAKE ACTIVATION IMPOSSIBLE.
    #
    # This loop skipped every field whose `included` flag was not exactly True,
    # and the count below compared what it kept against the WHOLE list -- so the
    # first person to exclude a column got "Every included physical field must be
    # reviewed", a refusal accusing them of not reviewing what they had just
    # decided. Nobody could reach it, because the only writer of that flag set it
    # to True in hard for every field (`datastream_preconfiguration.py`), which
    # is also why no existing caller depends on the old count.
    #
    # The authority is `binding.status == "excluded"`, the key five live readers
    # already read (`datastream_projection`, `csv_excel_import`,
    # `datastream_field_mapping`). Excluded fields leave BOTH sides of the
    # comparison; the refusal survives for what it was always meant to catch --
    # an INCLUDED field carrying no binding decision at all.
    included_fields = [field for field in fields if not is_excluded(field)]
    mapping_fields: list[dict[str, Any]] = []
    for field in included_fields:
        if not is_reviewed(field):
            continue
        binding = deepcopy(field.get("binding") or {})
        if binding.get("status") == "blocking":
            raise ActivationValidationError("Blocking field mappings cannot be materialized")
        if binding.get("status") == "suggested":
            binding.update(
                status="confirmed",
                blocking_reason=None,
                confirmed_by=actor,
                confirmed_reason="Confirmed in Datastream final review",
            )
        # UNE MESURE NON ADDITIVE N'EST PAS UNE FAUTE : c'est un ACHEMINEMENT,
        # et le compilateur le dit lui-meme -- « it stays in the full-grain
        # relation and is served by the semantic_* VIEW pattern », repair
        # `route_to_semantic`. Mais toute issue rend le plan non executable, donc
        # un rapport dont TOUTES les mesures sont non additives -- un stock
        # d'abonnes, une repartition en pourcentage -- ne pouvait pas exister du
        # tout. Mesure 2026-08-12 : deux rapports sur neuf mouraient ici, apres
        # avoir passe la decouverte, le preview et la revue.
        #
        # Elle est donc exclue de la projection CANONIQUE, ou elle serait sommee,
        # et reste dans la relation au grain complet, ou elle se lit. Le gate
        # n'est pas relache : il n'a plus rien a refuser.
        suggestion = field.get("suggestion") or {}
        role = str(suggestion.get("semantic_role") or "")
        if (
            role.startswith("measure")
            and bool(suggestion.get("non_additive"))
            and binding.get("status") != "excluded"
        ):
            binding.update(
                status="excluded",
                blocking_reason=None,
                confirmed_by=actor,
                confirmed_reason=(
                    "Non-additive: kept in the full-grain relation and read through the "
                    "semantic layer, never summed into the canonical projection (AD-4)."
                ),
            )
        mapping_fields.append(
            {
                "field_id": str(field["field_id"]),
                "physical_type": str(field.get("semantic_type") or "unknown"),
                "profile": deepcopy(field.get("profile") or {}),
                "suggestion": deepcopy(field.get("suggestion") or {}),
                "binding": binding,
            }
        )
    if len(mapping_fields) != len(included_fields):
        raise ActivationValidationError("Every included physical field must be reviewed")
    selection = {
        "selection_mode": "subset",
        "metrics": sorted(
            field["field_id"]
            for field in mapping_fields
            if str(field["suggestion"].get("semantic_role") or "").startswith("measure")
        ),
        "dimensions": sorted(
            field["field_id"]
            for field in mapping_fields
            if not str(field["suggestion"].get("semantic_role") or "").startswith("measure")
        ),
        "grain": sorted(str(value) for value in grain),
        "filters": [],
    }
    if mode == "connector_pull":
        raw_filters = configure.get("filters")
        try:
            selection["filters"] = (
                json.loads(raw_filters)
                if isinstance(raw_filters, str) and raw_filters.strip()
                else []
            )
        except json.JSONDecodeError as exc:
            raise ActivationValidationError("Reviewed Connector filters are invalid") from exc
        report_id = str(source.get("report_ref") or "")
        report = next(
            (
                item
                for item in (connector_capabilities or {}).get("reports", [])
                if item.get("id") == report_id
            ),
            None,
        )
        if report is None:
            raise ActivationValidationError("Reviewed Connector contract is unavailable")
        selection["selection_mode"] = str(report.get("selection_mode") or "subset")
        source_contract: dict[str, Any] = {
            "kind": mode,
            "connection_ref_id": str((connector_capabilities or {}).get("connection_ref_id") or ""),
            "module": str(((connector_capabilities or {}).get("module") or {}).get("name") or ""),
            "report_id": report_id,
            # `source_account_ref` IS THE KEY THE DRAFT WRITES.
            # `_OPERATOR_SOURCE_KEYS["connector_pull"]` is a closed set of five
            # and `account_ref` is not one of them, so this read was always None:
            # `selected_account_ref` came out empty, and
            # `fk_datastreams_source_account` -- FOREIGN KEY (connection_ref_id,
            # source_account_id) -- refused the insert. No Connector pull could
            # be materialized, whatever the operator had chosen. Measured
            # 2026-08-11 by walking the funnel to its last step.
            # `account_ref` stays as a tolerated alias; the canonical key wins.
            "selected_account_ref": str(
                source.get("source_account_ref")
                or source.get("account_ref")
                or (connector_capabilities or {}).get("selected_account_ref")
                or ""
            ),
            "selection": selection,
        }
    elif mode == "external_bq":
        coordinates = str(source.get("object_ref") or "").split(".")
        if len(coordinates) != 3 or any(not value for value in coordinates):
            raise ActivationValidationError("Reviewed BigQuery object identity is invalid")
        source_contract = {
            "kind": mode,
            "external_object": {
                "project": coordinates[0],
                "dataset": coordinates[1],
                "object": coordinates[2],
                "writer_identity": str(source.get("declared_writer") or ""),
            },
            "selection": selection,
        }
    else:
        channel = str(source.get("channel") or "")
        feed_format = (
            "google_sheets"
            if channel == "google_sheets"
            else "excel"
            if observation.get("safe_metadata", {}).get("detected_format") == "xlsx"
            else "csv"
        )
        source_contract = {
            "kind": mode,
            # AI-91, ratified by Jean 2026-07-31: the Template reference and the
            # staged preview asset are SEPARATE KEYS, not alternatives.
            #
            # They answer two different questions -- "which contract do I replay"
            # and "which file was used to preview it" -- and `template_ref` used to
            # sit fourth in a single `or` chain behind `staged_asset_ref`. A draft
            # that previewed a file AND chose a Template therefore lost the Template
            # at activation: `source_ref` carried no `fst_` prefix, so
            # `csv_excel_import.resolve_file_source_producer` found nothing to
            # resolve and the whole Story 22.12 wiring stayed inert -- on exactly
            # the journey the wizard produces, since it offers `template_ref` on the
            # same `file_upload` channel that carries a staged asset.
            #
            # Reordering the chain was the cheaper repair and was NOT taken: it
            # would have lost the staged asset instead of the Template. Nothing is
            # traded here; both are carried.
            "managed_feed": {
                "format": feed_format,
                # WHICH CHANNEL, under its own name. It used to be readable only
                # by inference -- `source_ref` falls back to the channel string
                # when no asset and no sheet were pinned -- and materialization
                # needs it plainly to decide whether this Datastream can ever
                # RECEIVE anything (`config.channels`).
                "channel": channel or None,
                # The input this plan pinned for the channel: the same bytes the
                # preview sampled. Never the Template.
                # POUR UN CANAL QUI RECOIT, C'EST L'ADRESSE DE CONTENU DU
                # FICHIER OBSERVE. Le repli sur le NOM DU CANAL vaut pour un flux
                # dont rien n'a encore ete epingle ; pour une livraison, il donne
                # `inbound_email` la ou la materialisation candidate attend un
                # hachage -- elle rejoue le fichier PAR SON CONTENU, seul
                # identifiant sur lequel l'apercu et elle puissent s'accorder.
                # Sans cela le candidat meurt en `dead_letter` et la publication
                # n'a jamais de preuve de qualite a se mettre sous la dent.
                "source_ref": str(
                    source.get("staged_asset_ref")
                    or source.get("sheet_ref")
                    or (
                        observation.get("safe_metadata", {}).get("content_hash")
                        if channel in CHANNELS_AWAITING_FIRST_DELIVERY
                        else None
                    )
                    or channel
                ),
                # The governed contract to replay on every arrival, or None. Absent
                # rather than empty-string: a missing binding must read as missing.
                "template_ref": source.get("template_ref") or None,
                "staged_asset_ref": source.get("staged_asset_ref") or None,
                # Story 57.3. The channel's DECLARED contract, carried whole so
                # the Datastream that receives deliveries can apply the sender
                # declaration the operator made at step 1. Absent for a channel
                # that declares none, never an empty promise.
                "channel_contract": _channel_contract_declaration(source) or None,
            },
            "selection": selection,
        }
    requested_schedule = operator_input.get("schedule") or {}
    schedule_mode = str(
        requested_schedule.get("mode")
        or configure.get("cadence_intent")
        or (
            "daily"
            if mode in ("connector_pull", "external_bq")
            or (mode == "managed_feed" and source.get("channel") == "google_sheets")
            else "manual"
        )
    )
    if mode == "managed_feed" and source.get("channel") != "google_sheets":
        schedule_mode = "manual"
    if schedule_mode not in {"manual", "daily", "weekly", "hourly"}:
        raise ActivationValidationError("Reviewed schedule mode is unsupported")
    interval_minutes = (
        None
        if schedule_mode == "manual"
        else 10080
        if schedule_mode == "weekly"
        else 1440
        if schedule_mode == "daily"
        else int(requested_schedule.get("interval_minutes") or 60)
    )
    # Story 57.8. The hour of the project's LOCAL day the pull is expected to
    # arrive. Only the cadences that run once a period can name one (A3), so a
    # value carried by an hourly or manual draft is dropped here rather than
    # recorded against a schedule that can never honour it.
    arrival_hour = requested_schedule.get("arrival_hour")
    if arrival_hour is not None and schedule_mode in ("daily", "weekly"):
        try:
            arrival_hour = int(arrival_hour)
        except (TypeError, ValueError):
            raise ActivationValidationError("Reviewed arrival hour is not a number") from None
        if arrival_hour < 0 or arrival_hour > 23:
            raise ActivationValidationError(
                "Reviewed arrival hour must be an hour of the local day, between 0 and 23"
            )
    else:
        arrival_hour = None
    schedule = {
        "mode": schedule_mode,
        "interval_minutes": interval_minutes,
        "timezone": timezone_name,
        "timezone_origin": timezone_origin,
        "run_at_hour": arrival_hour,
        "watermark": {
            "kind": "source_timestamp" if mode == "external_bq" else "date_window",
            "delay_minutes": int(requested_schedule.get("delay_minutes") or 0),
        },
        "late_arrival": {"lookback_minutes": int(requested_schedule.get("lookback_minutes") or 0)},
        "retry": {"max_attempts": 3, "initial_backoff_seconds": 60, "max_backoff_seconds": 3600},
        "missed_run": {"mode": "coalesce", "max_catchup_windows": 1},
    }
    plan_intent = {
        "contract_version": "1",
        "source": source_contract,
        "destination": {"policy": "external_read_only" if mode == "external_bq" else "managed_raw"},
        "historical": {"start": None, "end_exclusive": None},
        "schedule": schedule,
    }
    mapping_payload = {
        "mapping_contract_version": "1",
        "source_schema_hash": str(observation.get("schema_hash") or _hash(mapping_fields)),
        "plan_version_id": "pending_confirmation",
        "capability_fingerprint": (connector_capabilities or {}).get("capability_fingerprint"),
        "grain": sorted(str(value) for value in grain),
        "fields": mapping_fields,
        "ambiguities": [],
    }
    channel = source.get("channel") if mode == "managed_feed" else None
    if channel in {"inbound_email", "webhook"}:
        # NO `or 1440`, AND THAT REMOVAL IS THE POINT (story 57.3).
        #
        # This branch used to read `int(requested_schedule.get(
        # "expected_interval_minutes") or 1440)` -- a daily arrival nobody had
        # promised. `dq_monitors._check_arrival_timeliness` then fired, or stayed
        # silent, against that invented number; `dq_monitors` says it itself:
        # "a monitor that cannot be satisfied is worse than no monitor".
        #
        # Absent now means ABSENT. The monitor is not armed, the review says so,
        # and what arms it is the operator's declaration on the channel contract
        # at step 1.
        declared_interval = _declared_arrival_interval(source, requested_schedule)
        activation = {
            "activation_kind": "arrival_monitor",
            "expected_interval_minutes": declared_interval,
            "arrival_expectation": "declared" if declared_interval else "not_declared",
            "owner_person_id": actor,
            "next_run_at": None,
            "schedule_mode": "manual",
        }
    elif schedule_mode == "manual":
        activation = {
            "activation_kind": "manual",
            "next_run_at": None,
            "schedule_mode": "manual",
        }
    else:
        from core.datastream_schedule import calculate_schedule_window  # noqa: PLC0415

        window = calculate_schedule_window(schedule, now_utc=datetime.now(UTC))
        activation = {
            "activation_kind": "schedule",
            "next_run_at": window.next_run_at.isoformat() if window.next_run_at else None,
            "schedule_mode": "nightly" if schedule_mode == "daily" else schedule_mode,
            # Carried onto the stable row too (story 57.8): the plan version is
            # immutable, so the hour has to live somewhere the Workbench can
            # still edit it after activation.
            "arrival_hour_local": schedule.get("run_at_hour"),
        }
    review.update(
        {
            "org_id": org_id,
            "mode": mode,
            "channel": channel,
            "plan_intent": plan_intent,
            "mapping_payload": mapping_payload,
            "connector_capabilities": deepcopy(connector_capabilities),
            "schedule": activation,
            # The plan is COMPLETE for this feed -- it carries the channel, the
            # declared arrival expectation and the sender allowlist, which are
            # what an inbound Datastream is made of. Only the schema-derived half
            # is absent, so the mutation knows to write the row and stop there
            # rather than version an empty mapping.
            "deferred_until_first_delivery": deferred,
        }
    )
    # LE BUNDLE A SERVI ; IL NE VOYAGE PAS PLUS LOIN EN ENTIER.
    #
    # `confirmed_intent_bundle` porte `field_mappings`, et chaque champ y porte
    # `profile`, `suggestion` et `binding`. Tout cela a ete lu quelques lignes
    # plus haut pour compiler `mapping_payload` et `selection` -- un document
    # complet, valide par son propre schema, qui exige ces objets. Les garder
    # AUSSI dans le snapshot persiste faisait porter deux fois la meme
    # information : mesure du 2026-08-08, 14622 octets pour un budget de 8192,
    # dont `confirmed_intent_bundle` 7039 o et `mapping_payload` 5460 o. La
    # revue finale etait refusee, donc `publish-activate` hors d'atteinte par
    # l'assistant, donc le parcours fondateur bloque a l'etape 8 sur 9.
    #
    # CE N'EST PAS LA COPIE QU'ON SUPPRIME, C'EST LE PORTEUR EN TROP. Une
    # premiere tentative retirait `profile`/`suggestion` de `mapping_payload` :
    # refutee par son schema, qui les declare requis. C'est donc l'autre bout
    # qui s'allege, et la mesure dit qu'il le peut : le seul lecteur du bundle
    # sur un snapshot RELU (`activate_from_review`) n'en tire que `data_role`.
    # `compile_materialization_contract` en lit davantage, mais il tourne ICI,
    # sur l'objet en memoire, jamais sur une ligne relue.
    #
    # Le HASH du bundle reste, pour que la provenance soit verifiable : ce qui
    # a ete revu est nommable, meme si son detail vit dans le mapping.
    full_bundle = review.get("confirmed_intent_bundle") or {}
    review["confirmed_intent_bundle"] = {
        "datastream_name": full_bundle.get("datastream_name"),
        "data_role": full_bundle.get("data_role"),
        "joint_grain": deepcopy(full_bundle.get("joint_grain") or []),
        "field_mappings_count": len(full_bundle.get("field_mappings") or []),
        "bundle_hash": _hash(full_bundle),
    }
    review["content_hash"] = _hash(
        {key: value for key, value in review.items() if key != "content_hash"}
    )
    return review


# ---------------------------------------------------------------------------
# A binding-only mapping change is an OVERLAY, never a re-pull (2026-09-05).
# ---------------------------------------------------------------------------

OVERLAY_ADAPTER = "mapping_overlay"


class OverlayNotApplicable(RuntimeError):
    """The Datastream has no published execution to lay the mapping over."""


def publish_mapping_overlay(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    mapping_version_id: str,
    actor: str,
    operation_id: str,
) -> dict[str, Any]:
    """Publish a mapping version over the CURRENT publication, pulling nothing.

    A change that touches only bindings -- which canonical field a column names,
    its binding state -- changes nothing about the landed rows (`governance.md`,
    amendment of 2026-09-05). So the publication is a new execution row that
    pulls nothing: it carries the prior publication's content hash and row
    count, is logged with that execution as its prior, receives one output
    version per prior output pointing at the SAME relation, and the Datastream's
    current execution and mapping version move to it. Measured 2026-09-05 on the
    reference project: nine flows pinned `date` and `channel_id`, every
    candidate re-pulled YouTube Analytics and died on the provider's 403.

    The caller owns the transaction and the operation; `datastream_output_versions`
    is UNIQUE on (output, execution), which is exactly why the overlay is a new
    execution and not a second version of the prior one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT current_published_execution_id, org_id
                 FROM app.datastreams WHERE id=%s AND project_id=%s FOR UPDATE""",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None or not row[0]:
            raise OverlayNotApplicable("no published execution to lay the mapping over")
        prior_execution_id, org_id = str(row[0]), row[1]
        cur.execute(
            """SELECT state, content_hash, row_count, plan_version_id, artifact_ref, artifact_hash
                 FROM app.datastream_executions WHERE id=%s AND datastream_id=%s AND project_id=%s""",
            (prior_execution_id, datastream_id, project_id),
        )
        prior = cur.fetchone()
        if prior is None or prior[0] != "published":
            raise OverlayNotApplicable("the current execution is not a publication")
        _state, content_hash, row_count, plan_version_id, _artifact_ref, _artifact_hash = prior
        cur.execute(
            """SELECT output_id, relation_ref, schema_hash, grain_evidence, evidence,
                      projection_version_ref, delivery_ref
                 FROM app.datastream_output_versions
                WHERE project_id=%s AND datastream_id=%s AND execution_id=%s
                ORDER BY created_at, id""",
            (project_id, datastream_id, prior_execution_id),
        )
        prior_outputs = cur.fetchall()
        if not prior_outputs:
            raise OverlayNotApplicable("the current publication names no output to lay the mapping over")

        execution_id = f"dse_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_executions
               (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                projection_plan_ref, state, content_hash, row_count, artifact_ref, artifact_hash,
                adapter_ref, candidate_evidence, created_by, started_at)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb,'published',%s,%s,%s,%s,%s,%s::jsonb,%s,NOW())""",
            (
                execution_id,
                datastream_id,
                project_id,
                plan_version_id,
                mapping_version_id,
                json.dumps({"kind": OVERLAY_ADAPTER, "overlay_of": prior_execution_id, "operation_id": operation_id}),
                content_hash,
                row_count,
                # No artifact: an overlay produces none, and the table's CHECK
                # (`ck_datastream_execution_artifact_scope`) binds an artifact
                # reference to the execution that produced it -- the prior's
                # reference names the prior (measured 2026-09-05, 00248).
                None,
                None,
                OVERLAY_ADAPTER,
                json.dumps({"overlay_of": prior_execution_id, "nothing_pulled": True}),
                actor,
            ),
        )
        cur.execute(
            """INSERT INTO app.datastream_publication_log
               (id, execution_id, datastream_id, project_id, plan_version_id, mapping_version_id,
                content_hash, row_count, prior_execution_id, published_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                f"dplog_{ULID()}",
                execution_id,
                datastream_id,
                project_id,
                plan_version_id,
                mapping_version_id,
                content_hash,
                row_count,
                prior_execution_id,
                actor,
            ),
        )
        log_id = cur.fetchone()[0]
        output_version_ids: list[str] = []
        for output_id, relation_ref, schema_hash, grain_evidence, evidence, projection_ref, delivery_ref in prior_outputs:
            version_id = f"dsov_{ULID()}"
            cur.execute(
                """INSERT INTO app.datastream_output_versions
                   (id, output_id, org_id, project_id, datastream_id, execution_id,
                    publication_log_id, plan_version_id, mapping_version_id,
                    projection_version_ref, relation_ref, delivery_ref, schema_hash,
                    grain_evidence, evidence, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                   ON CONFLICT (output_id, execution_id) DO NOTHING""",
                (
                    version_id,
                    output_id,
                    org_id,
                    project_id,
                    datastream_id,
                    execution_id,
                    log_id,
                    plan_version_id,
                    mapping_version_id,
                    projection_ref,
                    relation_ref,
                    delivery_ref,
                    schema_hash,
                    json.dumps(grain_evidence if grain_evidence is not None else {}),
                    json.dumps(evidence if evidence is not None else {}),
                    actor,
                ),
            )
            output_version_ids.append(version_id)
        cur.execute(
            """UPDATE app.datastreams
                  SET current_published_execution_id=%s, current_mapping_version_id=%s
                WHERE id=%s AND project_id=%s""",
            (execution_id, mapping_version_id, datastream_id, project_id),
        )
    logger.info(
        "datastream_activation: mapping overlay published ds=%s execution=%s over=%s mapping=%s",
        datastream_id, execution_id, prior_execution_id, mapping_version_id,
    )
    return {
        "execution_id": execution_id,
        "overlay_of": prior_execution_id,
        "publication_log_id": log_id,
        "output_version_ids": output_version_ids,
        "relation_ref": str(prior_outputs[0][1] or ""),
    }
