"""toorow -- Agentic mapping-proposal creation service (Story 36.17, Epic 36).

THE GAP THIS MODULE FILLS
=========================
``datastream_field_mapping.save_field_mapping`` (Story 12.3) appends an immutable
mapping version AND advances ``app.datastreams.current_mapping_version_id`` -- it is
the PUBLICATION path. Story 36.17 needs the OPPOSITE: an agent-authored *proposal*
that persists an immutable candidate version but NEVER advances the live pointer.
Publication is owned by Story 36.18 (the governed confirm-and-publish path).

This module adds ``create_mapping_proposal`` -- the missing constructor. It reuses
the Story 12.3 engine (``normalize_mapping`` / ``project_ossie`` /
``compute_source_schema_hash``) so there is NO parallel mapping store and NO source
vocabulary in core (E36-NFR05). It inserts a row into the SAME immutable
``app.datastream_mapping_versions`` table and records a lifecycle marker in
``app.mapping_proposals`` (migration 071) so the version table's immutability
contract is never mutated to add proposal state.

INVARIANTS (enforced here, asserted by the tests)
-------------------------------------------------
* ``current_mapping_version_id`` NEVER advances in this story. This module issues
  ZERO ``UPDATE app.datastreams SET current_mapping_version_id`` statements. The
  live pointer only moves in Story 36.18. (E36-FR07 / epic invariant.)
* A saved proposal is an IMMUTABLE new mapping version pinned to source-capability,
  schema, policy and tool-contract versions plus a human-readable diff.
* Gate checks (compile / semantic / sensitivity / authorization / DQ / cardinality /
  cost) run before the row is durably accepted. ANY failure => candidate rejected,
  proposal state ``rejected``, and the pointer never advances. (E36-FR07 AC4.)
* All checks pass => proposal state ``ready`` (non-live) routed to the governed
  review/confirmation screen (Story 36.18). (E36-FR07 AC5.)
* Provider schema/values/samples are UNTRUSTED: only schema, hashes and counts are
  persisted/returned -- never raw rows. (E36-NFR01.)
* The proposal save is routed through ``operations.execute_operation`` so the
  version insert + proposal insert + audit + outbox commit atomically (AD-28).
* Fail-closed access + a mode/profile gate (Advisory / Delegated /
  Autonomous-within-policy) decide whether the agent may explain, persist, or test.

Mirrors the metric_semantics / datastream_diagnosis conventions: ``from __future__
import annotations``, module logger, lazy ``core.*`` imports inside function bodies
(no import cycle), pure gate functions kept separate from I/O so the invariants are
testable without Postgres. French user-facing messages. ASCII-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from ulid import ULID

logger = logging.getLogger(__name__)

# The three agentic modes (E36-FR07). Advisory may only explain; Delegated may
# persist a proposal; Autonomous-within-policy may persist AND run bounded tests.
MODE_ADVISORY = "advisory"
MODE_DELEGATED = "delegated"
MODE_AUTONOMOUS = "autonomous"
_MODES: frozenset[str] = frozenset({MODE_ADVISORY, MODE_DELEGATED, MODE_AUTONOMOUS})

# Proposal lifecycle states. The version-table immutability contract is untouched:
# this lifecycle lives in app.mapping_proposals (migration 071).
STATE_PROPOSED = "proposed"
STATE_TESTING = "testing"
STATE_READY = "ready"
STATE_REJECTED = "rejected"

# The durable command routed through execute_operation for a proposal save. It is a
# WRITE effect but NON-LIVE: it never advances the live pointer.
COMMAND_MAPPING_PROPOSE = "datastream.mapping.propose"

# The ordered gate checks (E36-FR07 AC4). Each has a stable code and an origin the
# docstring/return payload names so a reviewer can audit where the evidence came
# from. ``authorization`` is enforced by the caller (strict AD-5 seam) BEFORE this
# module runs; it is echoed here so the recorded checks list is complete.
_GATE_ORDER: tuple[str, ...] = (
    "authorization",
    "compile",
    "semantic",
    "sensitivity",
    "cardinality",
    "dq",
    "cost",
)


class MappingProposalRejected(ValueError):
    """Raised when a gate check fails: the candidate is rejected, pointer frozen.

    Carries the structured per-check failures so the caller returns them verbatim
    (never the raw provider evidence that produced them).
    """

    def __init__(self, checks: list[dict[str, Any]]):
        self.checks = tuple(checks)
        failed = [c for c in checks if not c.get("passed")]
        super().__init__("; ".join(f"{c['check']}:{c.get('code')}" for c in failed))


class MappingProposalModeDenied(PermissionError):
    """Raised when the agentic mode/policy does not permit the requested effect."""


class MappingProposalUnavailable(RuntimeError):
    """The proposal could not be validated or saved safely (fail-closed)."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _mint_proposal_id() -> str:
    return f"mprop_{ULID()}"


# ---------------------------------------------------------------------------
# Mode gate (PURE -- offline-testable). Advisory=explain, Delegated=persist,
# Autonomous=persist+test. A profile that is not "operations"/"governance", or an
# unknown mode, fails closed to explain-only.
# ---------------------------------------------------------------------------


def mode_permits(mode: str, *, effect: str) -> bool:
    """Return whether *mode* permits *effect* in ("explain", "persist", "test").

    * ``explain``  -- always allowed (a read-only inspection/explanation).
    * ``persist``  -- Delegated and Autonomous only (Advisory may NOT persist).
    * ``test``     -- Autonomous only (bounded candidate tests are policy-gated).
    An unknown mode fails closed to explain-only. (E36-FR07 AC1.)
    """
    m = (mode or "").strip().lower()
    if effect == "explain":
        return True
    if m not in _MODES:
        return False
    if effect == "persist":
        return m in (MODE_DELEGATED, MODE_AUTONOMOUS)
    if effect == "test":
        return m == MODE_AUTONOMOUS
    return False


def assert_mode_permits(mode: str, *, effect: str) -> None:
    """Raise ``MappingProposalModeDenied`` unless *mode* permits *effect*."""
    if not mode_permits(mode, effect=effect):
        raise MappingProposalModeDenied(
            f"mode {mode!r} does not permit effect {effect!r}"
        )


# ---------------------------------------------------------------------------
# Gate checks (PURE over the normalized payload + safe evidence). Each returns a
# ``{check, passed, code, detail}`` record. NONE of them receives or emits raw
# provider rows -- only the normalized mapping structure and bounded counts/hashes
# assembled by the caller. (E36-NFR01.)
# ---------------------------------------------------------------------------


def _check_authorization(*, authorized: bool) -> dict[str, Any]:
    """authorization -- from the strict AD-5 access seam (project_access).

    The caller resolves ``resolve_strict_resource_access(minimum_capability="edit")``
    and passes the boolean; a False here means the agent lacks edit authority on the
    Datastream and the candidate is rejected before any persistence.
    """
    return {
        "check": "authorization",
        "passed": bool(authorized),
        "code": None if authorized else "insufficient_capability",
        "detail": "resolve_strict_resource_access(edit)",
    }


def _check_compile(normalized: dict[str, Any], *, structural_ok: bool) -> dict[str, Any]:
    """compile -- from datastream_field_mapping.normalize_mapping + project_ossie.

    ``structural_ok`` is True only when ``normalize_mapping`` accepted the payload
    (JSON-schema valid) AND ``project_ossie`` produced a schema-valid projection. A
    structurally invalid mapping cannot compile to an executable projection.
    """
    fields = normalized.get("fields") or []
    return {
        "check": "compile",
        "passed": bool(structural_ok) and len(fields) > 0,
        "code": None if (structural_ok and fields) else "compile_failed",
        "detail": f"fields={len(fields)}",
    }


def _check_semantic(normalized: dict[str, Any]) -> dict[str, Any]:
    """semantic -- from the Story 12.3 ambiguity vocabulary on the payload.

    A payload carrying unresolved ambiguities (multiple date candidates, mixed
    grain, currency conflict, non-additive-bound-additively) is not semantically
    coherent. Reuses the SAME conflict codes the mapping engine records.
    """
    ambiguities = normalized.get("ambiguities") or []
    return {
        "check": "semantic",
        "passed": len(ambiguities) == 0,
        "code": None if not ambiguities else "unresolved_ambiguity",
        "detail": f"ambiguities={len(ambiguities)}",
    }


def _check_sensitivity(normalized: dict[str, Any]) -> dict[str, Any]:
    """sensitivity -- from each field's declared sensitivity in the payload.

    A field bound (status confirmed/resolved) while still flagged ``credentials``
    is rejected: a proposal must never route secret-class data into a published
    mapping. ``pii``/``financial`` are allowed but recorded. (E36-NFR01 spirit.)
    """
    offending = []
    for f in normalized.get("fields") or []:
        sens = ((f.get("suggestion") or {}).get("sensitivity") or "unknown")
        status = ((f.get("binding") or {}).get("status") or "suggested")
        if sens == "credentials" and status in ("confirmed", "resolved"):
            offending.append(f.get("field_id"))
    return {
        "check": "sensitivity",
        "passed": not offending,
        "code": None if not offending else "sensitive_field_bound",
        "detail": f"credentials_bound={len(offending)}",
    }


def _check_cardinality(normalized: dict[str, Any], *, max_fields: int = 512) -> dict[str, Any]:
    """cardinality -- bounded field/grain count from the payload structure.

    A mapping that explodes past a bound (field count or an empty grain when
    measures exist) is rejected as an unbounded cardinality risk. Uses ONLY the
    structural counts, never provider row cardinality.
    """
    fields = normalized.get("fields") or []
    grain = normalized.get("grain") or []
    has_measure = any(
        str((f.get("suggestion") or {}).get("semantic_role") or "").startswith("measure")
        for f in fields
    )
    too_many = len(fields) > max_fields
    grain_missing = has_measure and len(grain) == 0
    passed = not too_many and not grain_missing
    code = None
    if too_many:
        code = "cardinality_exceeded"
    elif grain_missing:
        code = "grain_required_for_measures"
    return {
        "check": "cardinality",
        "passed": passed,
        "code": code,
        "detail": f"fields={len(fields)},grain={len(grain)}",
    }


def _check_dq(dq_evidence: dict[str, Any] | None) -> dict[str, Any]:
    """dq -- from dq_api.fetch_dq_report_data (bounded monitor summary).

    ``dq_evidence`` is the SANITIZED DQ report (counts only). An unavailable DQ read
    is fail-closed (the check does not pass on missing evidence). Open blocking
    issues above zero reject the candidate: a proposal must not ride over a red DQ
    gate.
    """
    if not isinstance(dq_evidence, dict):
        return {
            "check": "dq",
            "passed": False,
            "code": "dq_unavailable",
            "detail": "no_dq_evidence",
        }
    unresolved = int(dq_evidence.get("total_unresolved") or 0)
    return {
        "check": "dq",
        "passed": unresolved == 0,
        "code": None if unresolved == 0 else "dq_open_issues",
        "detail": f"total_unresolved={unresolved}",
    }


def _check_cost(
    cost_estimate: dict[str, Any] | None, *, max_units: int = 100_000
) -> dict[str, Any]:
    """cost -- from the caller-supplied bounded cost estimate (structural).

    ``cost_estimate`` carries ``estimated_units`` (a bounded integer). An estimate
    above the ceiling rejects the candidate. Missing estimate is treated as zero
    (a proposal that changes only mapping shape has no extra extraction cost).
    """
    units = 0
    if isinstance(cost_estimate, dict):
        try:
            units = int(cost_estimate.get("estimated_units") or 0)
        except (TypeError, ValueError):
            units = max_units + 1  # a non-numeric estimate fails closed
    passed = 0 <= units <= max_units
    return {
        "check": "cost",
        "passed": passed,
        "code": None if passed else "cost_exceeded",
        "detail": f"estimated_units={units}",
    }


def run_gate_checks(
    normalized: dict[str, Any],
    *,
    authorized: bool,
    structural_ok: bool,
    dq_evidence: dict[str, Any] | None,
    cost_estimate: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Run every gate check in ``_GATE_ORDER`` and return the ordered records.

    Deterministic and PURE: two calls with the same inputs return an equal list.
    The caller inspects ``all(c["passed"] for c in result)`` -- ANY False means the
    candidate is rejected and the live pointer must never advance (E36-FR07 AC4).
    """
    by_code = {
        "authorization": _check_authorization(authorized=authorized),
        "compile": _check_compile(normalized, structural_ok=structural_ok),
        "semantic": _check_semantic(normalized),
        "sensitivity": _check_sensitivity(normalized),
        "cardinality": _check_cardinality(normalized),
        "dq": _check_dq(dq_evidence),
        "cost": _check_cost(cost_estimate),
    }
    return [by_code[name] for name in _GATE_ORDER]


# ---------------------------------------------------------------------------
# I/O: version insert + proposal marker, routed through execute_operation. This is
# the ONLY place that writes -- and it writes ZERO pointer UPDATEs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalResult:
    proposal_id: str
    mapping_version_id: str
    version_number: int
    state: str
    executable: bool
    checks: tuple[dict[str, Any], ...]
    diff: dict[str, Any]
    operation_id: str | None
    content_hash: str
    source_schema_hash: str


def _fetch_current_version_row(cur, datastream_id: str, project_id: str) -> tuple[Any, ...] | None:
    """Read the CURRENT (live) mapping version row for the before-diff, if any.

    Read-only: we lock the datastream row FOR UPDATE only to serialize concurrent
    proposals on version_number, and we DO NOT modify current_mapping_version_id.
    Returns the raw version row (or None when the datastream has no live mapping).
    """
    cur.execute(
        """
        SELECT current_mapping_version_id, current_plan_version_id
        FROM app.datastreams
        WHERE id = %s AND project_id = %s
        FOR UPDATE
        """,
        (datastream_id, project_id),
    )
    ds_row = cur.fetchone()
    if ds_row is None:
        raise MappingProposalUnavailable("Datastream introuvable")
    current_id = ds_row[0]
    if not current_id:
        return None
    cur.execute(
        """
        SELECT id, datastream_id, project_id, version_number, mapping_contract_version,
               source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
               ossie_spec_version, toorow_extension_version, executable, blocking_count,
               mapping_payload, ossie_projection, created_by, created_at
        FROM app.datastream_mapping_versions
        WHERE id = %s AND datastream_id = %s AND project_id = %s
        """,
        (current_id, datastream_id, project_id),
    )
    return cur.fetchone()


def create_mapping_proposal(
    conn: Any,
    *,
    datastream_id: str,
    project_id: str,
    effective_org_id: str,
    mapping_payload: dict[str, Any],
    plan_version_id: str,
    actor: str,
    mode: str,
    idempotency_key: str,
    authorized: bool,
    dq_evidence: dict[str, Any] | None = None,
    cost_estimate: dict[str, Any] | None = None,
    policy_version: str | None = None,
    catalog_version: str | None = None,
    tool_version: str | None = None,
    trace_id: str | None = None,
    dataset_name: str = "default_dataset",
) -> ProposalResult:
    """Create an immutable NON-LIVE mapping proposal; the live pointer never moves.

    Sequence (all inside *conn*, one atomic transaction via execute_operation):
      1. Mode gate: ``persist`` requires Delegated/Autonomous (Advisory is refused
         here -- Advisory callers must only ``explain`` and never reach this).
      2. Normalize + compile: reuse ``normalize_mapping`` (JSON-schema) and
         ``project_ossie`` (Ossie projection). Structural failure => structural_ok
         is False and the compile gate fails.
      3. Compute ``content_hash`` (canonical payload) and ``source_schema_hash``
         (ordered field universe) via the Story 12.3 helpers.
      4. Run the gate checks (compile/semantic/sensitivity/authorization/DQ/
         cardinality/cost). ANY failure raises ``MappingProposalRejected`` and NO
         version row and NO proposal row are inserted -> pointer frozen.
      5. On all-pass: insert ONE immutable ``app.datastream_mapping_versions`` row
         (version_number = MAX+1) WITHOUT touching current_mapping_version_id, and
         record an ``app.mapping_proposals`` row in state ``ready`` bound to that
         version + the checks + the human-readable diff + operation_id.
      6. Everything commits through ``execute_operation`` (audit + outbox, AD-28).

    Returns a ``ProposalResult``. Raises ``MappingProposalModeDenied`` (mode gate),
    ``MappingProposalRejected`` (a gate check failed), or
    ``MappingProposalUnavailable`` (fail-closed persistence error).
    """
    from core.datastream_field_mapping import (  # noqa: PLC0415
        DatastreamMappingStructuralError,
        compute_source_schema_hash,
        normalize_mapping,
        project_ossie,
    )
    from core.mapping_diff import diff_mappings  # noqa: PLC0415
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationSpec,
        execute_operation,
    )

    # 1. Mode gate -- persisting a proposal is a Delegated/Autonomous effect.
    assert_mode_permits(mode, effect="persist")

    if not idempotency_key or not idempotency_key.strip():
        raise MappingProposalUnavailable("Idempotency-Key requis")

    # 2. Normalize + compile (reuse the Story 12.3 engine; no parallel store).
    structural_ok = True
    try:
        normalized, content_hash = normalize_mapping(mapping_payload)
        # Pin the plan version reference and re-hash so the persisted payload is
        # exactly what the checks ran against.
        normalized["plan_version_id"] = plan_version_id
        normalized, content_hash = normalize_mapping(normalized)
        ossie_projection = project_ossie(normalized, dataset_name=dataset_name)
    except (DatastreamMappingStructuralError, ValueError) as exc:
        logger.info("mapping_versions: structural/compile rejection: %s", exc)
        structural_ok = False
        normalized = {"fields": [], "grain": [], "ambiguities": []}
        content_hash = _sha256(_canonical_json(normalized))
        ossie_projection = {}

    # 3. Source-schema hash from the ordered field universe (Story 12.3 helper).
    source_schema_hash = compute_source_schema_hash(list(normalized.get("fields") or []))

    # 4. Gate checks (PURE). Any failure rejects before persistence -> pointer frozen.
    checks = run_gate_checks(
        normalized,
        authorized=authorized,
        structural_ok=structural_ok,
        dq_evidence=dq_evidence,
        cost_estimate=cost_estimate,
    )
    if not all(c["passed"] for c in checks):
        # No version row, no proposal row, no operation, no pointer move.
        raise MappingProposalRejected(checks)

    blocking_count = sum(
        1
        for f in normalized.get("fields") or []
        if (f.get("binding") or {}).get("status") == "blocking"
    )
    ambiguity_count = len(normalized.get("ambiguities") or [])
    executable = (blocking_count == 0) and (ambiguity_count == 0)

    mapping_version_id = f"dmap_{ULID()}"
    proposal_id = _mint_proposal_id()

    # The mutation runs INSIDE execute_operation (which owns audit + outbox). It
    # performs the version insert + proposal insert and returns bounded evidence.
    # CRITICAL INVARIANT: it issues NO UPDATE to app.datastreams.
    def mutation(_conn: Any, operation_id: str) -> MutationResult:
        with _conn.cursor() as cur:
            before_row = _fetch_current_version_row(cur, datastream_id, project_id)

            # Allocate the next immutable version number.
            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM app.datastream_mapping_versions
                WHERE datastream_id = %s AND project_id = %s
                """,
                (datastream_id, project_id),
            )
            version_number = int(cur.fetchone()[0])

            idempotency_hash = _sha256(idempotency_key)
            cur.execute(
                """
                INSERT INTO app.datastream_mapping_versions
                    (id, datastream_id, project_id, version_number, mapping_contract_version,
                     source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                     ossie_spec_version, toorow_extension_version, executable, blocking_count,
                     mapping_payload, ossie_projection, idempotency_key_hash, created_by)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     %s::jsonb, %s::jsonb, %s, %s)
                RETURNING id, datastream_id, project_id, version_number, mapping_contract_version,
                          source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                          ossie_spec_version, toorow_extension_version, executable, blocking_count,
                          mapping_payload, ossie_projection, created_by, created_at
                """,
                (
                    mapping_version_id,
                    datastream_id,
                    project_id,
                    version_number,
                    normalized.get("mapping_contract_version", "1"),
                    source_schema_hash,
                    plan_version_id,
                    normalized.get("capability_fingerprint"),
                    content_hash,
                    "0.1.1",
                    "1",
                    executable,
                    blocking_count,
                    _canonical_json(normalized),
                    _canonical_json(ossie_projection),
                    idempotency_hash,
                    actor,
                ),
            )
            after_row = cur.fetchone()

            # Human-readable diff (deterministic, no raw provider values).
            diff = diff_mappings(_row_to_version(before_row), _row_to_version(after_row))

            # Record the proposal lifecycle marker (state='ready', NON-LIVE). This
            # is the ONLY row that carries proposal state -- the version table's
            # immutability contract is untouched (migration 071).
            checks_json = _canonical_json({"checks": list(checks)})
            diff_json = _canonical_json(diff)
            cur.execute(
                """
                INSERT INTO app.mapping_proposals
                    (id, mapping_version_id, datastream_id, project_id, org_id, mode,
                     state, checks, diff, operation_id, source_schema_hash, content_hash,
                     policy_version, catalog_version, tool_version, created_by)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s,
                     %s, %s, %s, %s)
                """,
                (
                    proposal_id,
                    mapping_version_id,
                    datastream_id,
                    project_id,
                    effective_org_id,
                    (mode or "").strip().lower(),
                    STATE_READY,
                    checks_json,
                    diff_json,
                    operation_id,
                    source_schema_hash,
                    content_hash,
                    policy_version,
                    catalog_version,
                    tool_version,
                    actor,
                ),
            )

        result = {
            "proposal_id": proposal_id,
            "mapping_version_id": mapping_version_id,
            "version_number": version_number,
            "state": STATE_READY,
            "executable": executable,
            "content_hash": content_hash,
            "source_schema_hash": source_schema_hash,
            "checks_passed": True,
            "non_live": True,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=(before_row[8] if before_row else None),
            after_hash=content_hash,
            result=result,
            outbox_payload={
                "proposal_id": proposal_id,
                "mapping_version_id": mapping_version_id,
                "state": STATE_READY,
            },
        )

    try:
        op_result = execute_operation(
            conn,
            OperationSpec(
                command_type=COMMAND_MAPPING_PROPOSE,
                actor=actor,
                effective_org_id=effective_org_id,
                resource_path=(
                    f"organization:{effective_org_id}",
                    f"flux:{datastream_id}",
                ),
                idempotency_key=idempotency_key,
                host_context={},
                versions={
                    k: v
                    for k, v in (
                        ("policy", policy_version),
                        ("catalog", catalog_version),
                        ("tool", tool_version),
                    )
                    if v is not None
                },
                request_payload={
                    "content_hash": content_hash,
                    "source_schema_hash": source_schema_hash,
                    "plan_version_id": plan_version_id,
                },
                provider_references={},
                confirmation_mode="human",
                confirmation_reference=None,
                trace_id=trace_id,
            ),
            mutation=mutation,
        )
    except (MappingProposalRejected, MappingProposalModeDenied):
        raise
    except Exception as exc:  # noqa: BLE001 -- fail-closed on any persistence error.
        logger.error("mapping_versions: proposal persistence failed: %s", exc)
        raise MappingProposalUnavailable("Enregistrement de la proposition impossible") from exc

    data = op_result.result or {}
    # An idempotent replay returns the original operation's result. Re-derive the
    # version number defensively (a replay may not re-run the mutation body).
    return ProposalResult(
        proposal_id=str(data.get("proposal_id", proposal_id)),
        mapping_version_id=str(data.get("mapping_version_id", mapping_version_id)),
        version_number=int(data.get("version_number", 0) or 0),
        state=str(data.get("state", STATE_READY)),
        executable=bool(data.get("executable", executable)),
        checks=tuple(checks),
        diff={},  # the full diff is persisted; the caller reads it via the proposal row
        operation_id=op_result.operation_id,
        content_hash=content_hash,
        source_schema_hash=source_schema_hash,
    )


def _row_to_version(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    """Map a raw mapping-version row tuple into the dict shape diff_mappings reads."""
    if row is None:
        return None
    keys = (
        "id",
        "datastream_id",
        "project_id",
        "version_number",
        "mapping_contract_version",
        "source_schema_hash",
        "plan_version_id",
        "capability_fingerprint",
        "content_hash",
        "ossie_spec_version",
        "toorow_extension_version",
        "executable",
        "blocking_count",
        "mapping_payload",
        "ossie_projection",
        "created_by",
        "created_at",
    )
    result = dict(zip(keys, row))
    payload = result.get("mapping_payload")
    if isinstance(payload, str):
        try:
            result["mapping_payload"] = json.loads(payload)
        except (TypeError, ValueError):
            result["mapping_payload"] = {}
    return result
