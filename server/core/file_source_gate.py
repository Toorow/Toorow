"""toorow -- File-source required-field validation gate (Story 22.15, AD-7).

The lock checkpoint of the file-source ingestion framework. Before a template
locks, the gate confirms the required canonical fields were actually recovered
from the source and that no mapping is low-confidence or ambiguous. A missing
required field or an ambiguous/low-confidence binding is FLAGGED (surfaced with
the offending field) and NOT landed -- never a silent drop or mis-map.

The gate REUSES the Story 12.3 ``datastream_field_mapping.profile_fields``
confidence scoring + ambiguity detection (one vocabulary, no second scorer). It
is evaluated on a sample preview (``sample_only=True`` upstream); the template
locks only after a human confirms a PASSING preview, and that confirmation is
recorded durably through ``operations.execute_operation`` (audit + outbox).

Two layers: a PURE ``evaluate_required_field_gate`` (no I/O, offline-testable) +
``confirm_mapping_version`` (the durable human confirmation, execute_operation).
That second name was ``record_gate_confirmation`` until 2026-08-09; the removal
note at the head of the confirmation section says why, and this line said the
dead name until it was re-measured on 2026-08-17.
"""

from __future__ import annotations

from typing import Any

from core.datastream_field_mapping import profile_fields

ACTION_FILE_SOURCE_GATE_CONFIRMED = "file_source.template.gate_confirmed"


class FileSourceGateError(ValueError):
    """Base for gate errors (carries a stable code)."""

    code = "file_source_gate_error"


class GateNotPassed(FileSourceGateError):
    """A caller tried to lock/confirm a template whose gate did not pass."""

    code = "gate_not_passed"


def _infer_physical_type(values: list[Any]) -> str:
    """Infer a coarse physical type from sample values (for confidence scoring).

    Mirrors the spirit of csv_excel_import inference but stays dependency-light:
    the FIRST non-empty value decides date > number > string; an all-empty column
    is ``unknown`` so the gate keeps it blocking by default.
    """
    non_null = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if not non_null:
        return "unknown"
    head = non_null[0]
    from datetime import datetime  # noqa: PLC0415

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            datetime.strptime(head, fmt)
            return "date"
        except ValueError:
            pass
    try:
        float(head.replace(",", ".").replace(" ", ""))
        return "decimal"
    except ValueError:
        return "string"


def evaluate_required_field_gate(
    template: dict[str, Any],
    mapping: dict[str, str],
    sample_rows: list[dict[str, Any]] | None = None,
    *,
    confidence_threshold: float = 0.75,
) -> dict[str, Any]:
    """Evaluate the required-field gate for a template + a source->canonical mapping.

    ``mapping`` is {source_column -> canonical_field_id}. ``sample_rows`` are the
    bounded preview rows (keyed by SOURCE column). Returns::

      {
        "passed": bool,
        "missing_required": [canonical_id, ...],   # required, not mapped at all
        "flagged": [ {field_id, canonical_target, blocking_reason, confidence}, ...],
        "ambiguities": [ ... ],                     # from profile_fields
      }

    A required canonical field with no mapping -> missing_required. A mapped field
    whose binding is blocking (low confidence / unknown / ambiguous) AND targets a
    required canonical field -> flagged. ``passed`` iff neither is present.
    """
    contract = template.get("contract") if "contract" in template else template
    required = list((contract or {}).get("required_fields") or [])
    required_set = set(required)

    mapped_targets = set(mapping.values())
    missing_required = [rid for rid in required if rid not in mapped_targets]

    sample = sample_rows or []
    field_records: list[dict[str, Any]] = []
    for source_col, canonical_id in mapping.items():
        values = [row.get(source_col) for row in sample]
        physical_type = _infer_physical_type(values)
        kind = "metric" if physical_type == "decimal" else (
            "date" if physical_type == "date" else "dimension"
        )
        field_records.append(
            {
                "field_id": source_col,
                "canonical_target": canonical_id,
                "physical_type": physical_type,
                "kind": kind,
            }
        )

    profile = profile_fields(
        field_records=field_records,
        sample_data=sample,
        confidence_threshold=confidence_threshold,
    )

    span = _declared_date_span(contract)
    ambiguities = [
        amb
        for amb in profile.get("ambiguities", [])
        if not _answered_by_the_declared_span(amb, span)
    ]

    flagged: list[dict[str, Any]] = []
    for f in profile["fields"]:
        binding = f.get("binding") or {}
        target = binding.get("canonical_target")
        if binding.get("status") != "blocking" or target not in required_set:
            continue
        # A field blocked ONLY because it competed with the other end of a span
        # the template declared is not ambiguous: the declaration answered it.
        if (
            binding.get("blocking_reason") == "multiple_date_candidates"
            and target in span
        ):
            continue
        flagged.append(
            {
                "field_id": f["field_id"],
                "source_column": f["field_id"],
                "canonical_target": target,
                "blocking_reason": binding.get("blocking_reason"),
                "confidence": (f.get("profile") or {}).get("confidence"),
            }
        )

    passed = not missing_required and not flagged
    return {
        "passed": passed,
        "missing_required": missing_required,
        "flagged": flagged,
        "ambiguities": ambiguities,
    }


def _declared_date_span(contract: dict[str, Any] | None) -> frozenset[str]:
    """The two date fields a Template DECLARES as a line's span, if it declares one.

    `multiple_date_candidates` is an INFERENCE-time question -- "several columns
    look like dates, which one is THE date?" -- and it is the right question when
    nobody has answered it. A reshape Template answers it in its contract:
    ``start_field`` and ``end_field`` are not rival candidates for one date, they
    are the two ends of one span.

    The daily grain never reaches this case because it CONSUMES the pair into a
    single ``date_field``, so only one date column is ever produced. At the line
    grain the span survives into the landed row -- that is the point of the grain
    -- and the gate would otherwise refuse every plan for carrying exactly the two
    columns its Template told it to carry.

    Reading the declaration off the persisted contract is the same lesson the
    discriminator taught: the normalised contract IS the agreement, not a summary
    of it, and a key nobody reads back is a key nobody can rely on.
    """
    reshape = (contract or {}).get("reshape")
    if not isinstance(reshape, dict):
        return frozenset()
    start = reshape.get("start_field")
    end = reshape.get("end_field")
    if not isinstance(start, str) or not isinstance(end, str) or start == end:
        return frozenset()
    return frozenset({start, end})


def _answered_by_the_declared_span(
    ambiguity: dict[str, Any], span: frozenset[str]
) -> bool:
    """Whether this ambiguity is exactly the declared span and nothing more.

    Narrow on purpose: a THIRD date column alongside the declared pair is a real
    ambiguity the declaration does NOT answer, and it must keep blocking.
    """
    if not span or ambiguity.get("code") != "multiple_date_candidates":
        return False
    return set(ambiguity.get("candidates") or ()) == set(span)


#: How many produced rows the landing gate scores. The gate reuses the 12.3
#: confidence scorer, which is bounded-sample by design; scoring a whole file
#: would make every arrival pay for evidence a sample already gives.
_LANDING_GATE_SAMPLE_ROWS = 50


def evaluate_landing_gate(
    template: dict[str, Any],
    mapping: dict[str, str] | None,
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    confidence_threshold: float = 0.75,
) -> dict[str, Any]:
    """Check a producer's OUTPUT against the template's required fields, pre-landing.

    AD-4 requires the produced rows to be validated against the template's
    required canonical fields BEFORE landing; AD-7 owns the same check at lock
    time. This is deliberately not a second scorer: it adapts the produced rows
    into the shape ``evaluate_required_field_gate`` already reads, so onboarding
    and arrival return the same verdict in the same vocabulary.

    Two shapes reach here:

    * TABULAR -- the producer renamed source columns to canonical ids and nothing
      else, so inverting ``mapping`` recovers the source-keyed sample the gate
      scores on EXACTLY, not approximately. A required field whose column the
      producer did not actually emit is dropped from the effective mapping, so it
      surfaces as ``missing_required`` rather than passing on the strength of a
      mapping entry no row honoured.
    * RESHAPE -- the producer owns its own projection and there is no source->
      canonical mapping to invert. The only truthful subject is what came out, so
      the produced columns are scored against the required fields directly.

    On the tabular path, a landed canonical column that NO mapping entry claims is
    scored on its own name. Those exist: ``stamp_placement`` writes the matrix
    class and the declared-discriminator dimension onto every row, and neither
    comes from a source column, so neither can appear in a source->canonical
    mapping. Without this, a template declaring its discriminator dimension
    REQUIRED -- the FR/DE market case the framework exists for -- was refused for
    a missing field that was present in every row it had just produced.
    """
    landed = {str(name) for name in columns}
    sample_rows = [dict(row) for row in rows[:_LANDING_GATE_SAMPLE_ROWS]]

    if mapping:
        inverse = {canonical: source for source, canonical in mapping.items()}
        sample = [
            {
                (inverse[key] if key in inverse else key): value
                for key, value in row.items()
                if key in inverse or key in landed
            }
            for row in sample_rows
        ]
        effective = {
            source: canonical for source, canonical in mapping.items() if canonical in landed
        }
        for name in sorted(landed - set(effective.values())):
            effective[name] = name
    else:
        sample = sample_rows
        effective = {name: name for name in sorted(landed)}

    return evaluate_required_field_gate(
        template,
        effective,
        sample,
        confidence_threshold=confidence_threshold,
    )


# `record_gate_confirmation` a ete RETIREE le 2026-08-09 (AI-92), et ce n'est pas
# un nettoyage : c'etait un SECOND ecrivain pour une seule confirmation. Elle
# ecrivait l'operation AD-7 d'une confirmation de Template sans version de
# mapping ; `confirm_mapping_version` ci-dessous fait la meme operation AD-7, le
# meme refus fail-closed, ET frappe la version de mapping -- et c'est elle que les
# deux routes de production appellent (`file_source_template_api.py:577`).
#
# Trouvee par `scripts/check_unused_public_api.py` : zero appelant en production,
# deux tests. Un commentaire de `test_file_source_chain_offline.py` la donnait
# encore comme << en attente de son appelant >>, alors que son appelant avait ete
# ecrit autrement. Son assertion fail-closed a ete PORTEE sur le successeur avant
# le retrait -- supprimer du code mort ne doit pas emporter une preuve.

class GateAlreadyConfirmed(FileSourceGateError):
    """The immutable template already carries a different confirmation."""

    code = "gate_already_confirmed"


def _record_confirmation(
    conn,
    *,
    operation_id: str,
    template_id: str,
    project_id: str,
    datastream_id: str,
    mapping_version_id: str,
    plan_version_id: str,
    content_hash: str,
    evidence: dict[str, Any],
    actor: str,
) -> None:
    """Insert one immutable proof for an exact reusable Template + Mapping."""
    from json import dumps  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.file_source_template_confirmations (
                operation_id, template_id, project_id, datastream_id,
                mapping_version_id, plan_version_id, template_content_hash,
                sample_content_hash, sample_filename, evidence, confirmed_by,
                confirmed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, NOW()
            )
            ON CONFLICT DO NOTHING
            RETURNING operation_id
            """,
            (
                operation_id,
                template_id,
                project_id,
                datastream_id,
                mapping_version_id,
                plan_version_id,
                content_hash,
                evidence["sample_content_hash"],
                str(evidence.get("sample_filename") or ""),
                dumps(evidence, sort_keys=True, separators=(",", ":")),
                actor,
            ),
        )
        if cur.fetchone() is not None:
            return
        cur.execute(
            """
            SELECT operation_id
            FROM app.file_source_template_confirmations
            WHERE template_id = %s AND mapping_version_id = %s
            """,
            (template_id, mapping_version_id),
        )
        existing = cur.fetchone()
        if existing is None or existing[0] != operation_id:
            raise GateAlreadyConfirmed(
                "this Template and Mapping already carry a different human confirmation"
            )

def confirm_mapping_version(
    conn,
    *,
    template_id: str,
    project_id: str,
    org_id: str,
    datastream_id: str,
    actor: str,
    gate_result: dict[str, Any],
    mapping_payload: dict[str, Any],
    evidence: dict[str, Any],
    idempotency_key: str,
    content_hash: str,
    pinned_plan_version_id: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Atomically record the human decision and mint a NON-LIVE mapping version.

    The mapping version is the exact executable decision. A separate immutable
    confirmation row binds it to the reusable Template and human operation.
    The Datastream pointer is deliberately not advanced here; publication remains
    a separate governed action.
    """
    if not gate_result.get("passed"):
        raise GateNotPassed("the recomputed required-field gate did not pass")

    from core.datastream_field_mapping import save_field_mapping  # noqa: PLC0415
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationSpec,
        _canonical_hash,
        execute_operation,
    )

    request_payload = {
        "template_id": template_id,
        "content_hash": content_hash,
        "datastream_id": datastream_id,
        "sample_content_hash": evidence["sample_content_hash"],
        "resolutions": evidence.get("resolutions", []),
        "accepted_warning_codes": [
            str(item.get("code") or "") for item in evidence.get("accepted_warnings", [])
        ],
    }

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        mapping = save_field_mapping(
            datastream_id=datastream_id,
            project_id=project_id,
            mapping_payload=mapping_payload,
            identity=actor,
            idempotency_key=f"{idempotency_key}:mapping",
            conn=operation_conn,
            trace_id=trace_id,
            pinned_plan_version_id=pinned_plan_version_id,
            advance_pointer=False,
            commit=False,
        )
        if not mapping.get("executable") or int(mapping.get("blocking_count") or 0) != 0:
            raise GateNotPassed(
                "the pending mapping still contains blocking bindings after resolution"
            )
        durable_evidence = {
            **evidence,
            "mapping_version_id": mapping["id"],
            "plan_version_id": mapping["plan_version_id"],
        }
        # A CATALOG TEMPLATE HAS NO PER-PROJECT CONFIRMATION ROW (AI-321,
        # 2026-08-29). `app.file_source_template_confirmations` binds a
        # CLIENT artifact (migration 188 checks `app.file_source_templates` by
        # id and its content hash); a catalog Template is the immutable
        # (code, version) pair and `FileSourceProducer.confirmed_for` already
        # answers True for it. The human decision still lands where it
        # matters -- the mapping version above carries the resolutions and
        # the operation row carries the actor and the date.
        if not template_id.startswith("template:"):
            _record_confirmation(
                operation_conn,
                operation_id=operation_id,
                template_id=template_id,
                project_id=project_id,
                datastream_id=datastream_id,
                mapping_version_id=mapping["id"],
                plan_version_id=mapping["plan_version_id"],
                content_hash=content_hash,
                evidence=durable_evidence,
                actor=actor,
            )
        result_payload = {
            "template_id": template_id,
            "mapping_version_id": mapping["id"],
            "plan_version_id": mapping["plan_version_id"],
            "content_hash": content_hash,
            "sample_content_hash": evidence["sample_content_hash"],
            "confirmed": True,
            "active_pointer_advanced": False,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result_payload),
            result=result_payload,
            outbox_payload=result_payload,
        )

    spec = OperationSpec(
        command_type=ACTION_FILE_SOURCE_GATE_CONFIRMED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"datastream:{datastream_id}",
            f"file_source_template:{template_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={"policy": "file-source-gate-v2"},
        request_payload=request_payload,
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=(
            f"file-source-gate:{project_id}:{template_id}:"
            f"{content_hash}:{evidence['sample_content_hash']}"
        ),
        trace_id=trace_id,
    )
    operation = execute_operation(conn, spec, mutation=mutation)
    return {
        **operation.result,
        "operation_id": operation.operation_id,
        "replayed": operation.replayed,
    }


def confirm_adaptation_template(
    conn,
    *,
    template_id: str,
    project_id: str,
    org_id: str,
    datastream_id: str,
    actor: str,
    gate_result: dict[str, Any],
    evidence: dict[str, Any],
    idempotency_key: str,
    content_hash: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Record human proof for this Template + Mapping after isolated self-test."""
    if not gate_result.get("passed"):
        raise GateNotPassed("the adaptation self-test gate did not pass")

    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationSpec,
        _canonical_hash,
        execute_operation,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        _record_confirmation(
            operation_conn,
            operation_id=operation_id,
            template_id=template_id,
            project_id=project_id,
            datastream_id=datastream_id,
            mapping_version_id=evidence["mapping_version_id"],
            plan_version_id=evidence["plan_version_id"],
            content_hash=content_hash,
            evidence=evidence,
            actor=actor,
        )
        result_payload = {
            "template_id": template_id,
            "mapping_version_id": evidence["mapping_version_id"],
            "plan_version_id": evidence["plan_version_id"],
            "content_hash": content_hash,
            "sample_content_hash": evidence["sample_content_hash"],
            "confirmed": True,
            "active_pointer_advanced": False,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result_payload),
            result=result_payload,
            outbox_payload=result_payload,
        )

    spec = OperationSpec(
        command_type=ACTION_FILE_SOURCE_GATE_CONFIRMED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"datastream:{datastream_id}",
            f"file_source_template:{template_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={"policy": "file-source-adaptation-gate-v1"},
        request_payload={
            "template_id": template_id,
            "content_hash": content_hash,
            "datastream_id": datastream_id,
            "sample_content_hash": evidence["sample_content_hash"],
            "mapping_version_id": evidence["mapping_version_id"],
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=(
            f"file-source-adaptation-gate:{project_id}:{template_id}:"
            f"{content_hash}:{evidence['sample_content_hash']}"
        ),
        trace_id=trace_id,
    )
    operation = execute_operation(conn, spec, mutation=mutation)
    return {
        **operation.result,
        "operation_id": operation.operation_id,
        "replayed": operation.replayed,
    }
