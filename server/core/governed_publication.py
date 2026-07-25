"""toorow -- Governed mapping-publication domain service (Story 36.18, Epic 36).

THE GAP THIS MODULE FILLS
=========================
Story 36.17 (``mapping_versions.create_mapping_proposal``) persists an immutable
NON-LIVE mapping proposal (state 'ready') and NEVER advances the live pointer.
Story 36.18 is the OPPOSITE and complementary half: it is the ONE place a 'ready'
proposal becomes LIVE. Publication here advances
``app.datastreams.current_mapping_version_id`` -- atomically with audit + outbox --
through the AD-27 prepare-confirm-commit contract, and leaves the prior version
ROLLBACKABLE. Rollback is a DISTINCT confirmed idempotent operation.

THE THREE ENTRY POINTS
----------------------
* ``prepare_publication_review``  -- read-only. Assembles the governed review object
  (profile, actor/resource scope, versions, mapping diff via ``diff_mappings``,
  interval/destination, impact, checks, rollback, expiry) and mints an OPAQUE
  MODEL-HIDDEN confirmation secret server-side. Persists ONE immutable
  ``app.publication_confirmations`` row (migration 073) carrying only the secret's
  one-way hash. Creates NO durable operation and advances NO pointer.
* ``confirm_and_publish``         -- write. Verifies the presented opaque secret
  (hashed, never echoed), RE-CHECKS every precondition (expired / used / replayed /
  actor-or-workspace mismatch / stale versions / changed pointer / changed policy /
  failed check), then routes EXACTLY ONE ``operations.execute_operation`` whose
  mutation advances ``current_mapping_version_id`` (pointer + publication-log +
  audit + outbox) atomically. A duplicate confirm / timeout replays the ORIGINAL
  operation. On failure / outcome_unknown the caller reconciles using the original
  operation/provider references -- no blind fresh mutation.
* ``rollback_publication``        -- write. Re-points ``current_mapping_version_id``
  back to a prior version as a DISTINCT confirmed idempotent operation with its OWN
  audit + outbox (never a reuse of the forward publication's operation).

INVARIANTS (enforced here, asserted by the tests)
-------------------------------------------------
* The confirmation secret is OPAQUE and MODEL-HIDDEN: it is minted server-side,
  stored only as ``confirmation_reference_hash`` (sha256), and NEVER returned in any
  review/confirm payload. (AD-27 / E36-NFR07.)
* Confirmation is ATOMIC and BOUND: it creates exactly ONE durable operation bound
  to ALL reviewed hashes/versions/policy; dispatch reuses it (idempotent replay).
* The pointer advance + publication-log + audit + outbox commit together or not at
  all (the mutation runs INSIDE ``execute_operation``). (NFR14/AD-28.)
* The prior version is captured (``prior_mapping_version_id``) and stays
  rollbackable; rollback is a DISTINCT confirmed idempotent operation. (NFR15.)
* E36-NFR05: reuse the existing mapping engine + Story 36.2 operation engine -- NO
  parallel mapping store, NO source vocabulary in core.

Mirrors the mapping_versions / operations_mcp conventions: ``from __future__ import
annotations``, module logger, lazy ``core.*`` imports inside function bodies (no
import cycle), pure helpers separate from I/O so the invariants are offline-testable.
French user-facing messages. ASCII-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from ulid import ULID

logger = logging.getLogger(__name__)

# The governance profile the publication belongs to (E36-FR06/FR07). Kept local so
# this module never imports a private of mcp_profiles.
GOVERNANCE_PROFILE = "governance"

# The durable command routed through execute_operation for a forward publication and
# for a rollback. Distinct command_types so the two operations never share an
# idempotency namespace (a rollback is a DISTINCT operation, NFR15).
COMMAND_PUBLISH = "datastream.mapping.publish"
COMMAND_ROLLBACK = "datastream.mapping.rollback"

# Review time-to-live: a review not confirmed within this many seconds is stale and a
# confirm must refuse it (AD-27 immutable + bounded). Mirrors operations_mcp TTL.
_REVIEW_TTL_SECONDS = 900  # 15 minutes

# Only a proposal in this state may be published (Story 36.17 lifecycle).
_PROPOSAL_READY = "ready"


class PublicationReviewUnavailable(RuntimeError):
    """The review could not be assembled/persisted safely (fail-closed)."""


class PublicationConfirmationRefused(PermissionError):
    """A confirm precondition failed: NO operation is created or dispatched.

    Carries a stable ``code`` (expired / used / replayed / actor_mismatch /
    workspace_mismatch / stale_versions / changed_pointer / policy_changed /
    proposal_not_ready / confirmation_invalid) so the MCP layer maps it to a French
    ToolError without leaking why beyond the code.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class PublicationReview:
    """The read-only governed review object + the model-hidden secret handle.

    ``confirmation_secret`` is the ONE field that must NEVER reach model/MCP output;
    the caller passes it back into ``confirm_and_publish`` out-of-band (trusted
    console / server-verified in-host presence). Everything else is model-safe.
    """

    confirmation_id: str
    review: dict[str, Any]
    confirmation_secret: str


@dataclass(frozen=True)
class PublicationResult:
    confirmation_id: str
    operation_id: str
    outcome: str
    replayed: bool
    current_mapping_version_id: str
    prior_mapping_version_id: str | None
    result: dict[str, Any]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_hash(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _mint_confirmation_id() -> str:
    return f"pubc_{ULID()}"


def _mint_confirmation_secret() -> str:
    """Mint the opaque, high-entropy, MODEL-HIDDEN confirmation secret.

    URL-safe, 256-bit. Returned ONLY inside ``PublicationReview.confirmation_secret``
    (never persisted raw, never echoed to model context). Its sha256 is what lives in
    ``app.publication_confirmations.confirmation_reference_hash`` (AD-27 / E36-NFR07).
    """
    return f"pubs_{secrets.token_urlsafe(32)}"


# ---------------------------------------------------------------------------
# Pure precondition helpers (offline-testable, no I/O).
# ---------------------------------------------------------------------------


def _publication_idempotency_key(
    org_id: str, datastream_id: str, mapping_version_id: str, confirmation_id: str
) -> str:
    """Derive the durable-operation idempotency key for one publication.

    Deterministic over (org, datastream, target version, confirmation) so a duplicate
    confirm / timeout / retry of the SAME confirmation replays the SAME durable
    operation rather than creating a second publication.
    """
    return f"{COMMAND_PUBLISH}:{org_id}:{datastream_id}:{mapping_version_id}:{confirmation_id}"


def _rollback_idempotency_key(
    org_id: str, datastream_id: str, target_version_id: str, confirmation_id: str
) -> str:
    """Derive the durable-operation idempotency key for one rollback.

    Distinct namespace (COMMAND_ROLLBACK) so a rollback is ALWAYS a separate durable
    operation from the forward publication (NFR15), yet still idempotent on replay.
    """
    return f"{COMMAND_ROLLBACK}:{org_id}:{datastream_id}:{target_version_id}:{confirmation_id}"


def reviewed_hashes_match(reviewed: dict[str, Any], live: dict[str, Any]) -> bool:
    """True only when every reviewed hash/version equals the live value.

    The review pinned ``content_hash`` / ``source_schema_hash`` of the candidate and
    the live ``current_mapping_version_id`` it saw. If the candidate version changed
    (impossible -- versions are immutable) OR the live pointer moved since review (a
    CHANGED-POINTER: someone else published), the confirm refuses. This is the
    stale/changed-pointer recheck (AC3).
    """
    for key in ("content_hash", "source_schema_hash", "prior_mapping_version_id"):
        if reviewed.get(key) != live.get(key):
            return False
    return True


# ---------------------------------------------------------------------------
# I/O: load the proposal (ready) + candidate/prior version rows for the diff.
# ---------------------------------------------------------------------------


_VERSION_COLUMNS = (
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


def _fetch_version_row(
    cur, version_id: str, datastream_id: str, project_id: str
) -> dict[str, Any] | None:
    """Read one immutable mapping version row (shape ``diff_mappings`` reads)."""
    cur.execute(
        f"""
        SELECT {", ".join(_VERSION_COLUMNS)}
        FROM app.datastream_mapping_versions
        WHERE id = %s AND datastream_id = %s AND project_id = %s
        """,
        (version_id, datastream_id, project_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    result = dict(zip(_VERSION_COLUMNS, row))
    payload = result.get("mapping_payload")
    if isinstance(payload, str):
        try:
            result["mapping_payload"] = json.loads(payload)
        except (TypeError, ValueError):
            result["mapping_payload"] = {}
    return result


def _load_ready_proposal(conn, proposal_id: str) -> dict[str, Any] | None:
    """Load one proposal (any state) with its scope + pinned evidence, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, mapping_version_id, datastream_id, project_id, org_id, mode,
                   state, checks, diff, content_hash, source_schema_hash,
                   policy_version, catalog_version, tool_version
            FROM app.mapping_proposals
            WHERE id = %s
            """,
            (proposal_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    checks = row[7] if isinstance(row[7], (dict, list)) else _load_json(row[7])
    diff = row[8] if isinstance(row[8], dict) else _load_json(row[8])
    return {
        "id": row[0],
        "mapping_version_id": row[1],
        "datastream_id": row[2],
        "project_id": row[3],
        "org_id": row[4],
        "mode": row[5],
        "state": row[6],
        "checks": checks,
        "diff": diff,
        "content_hash": row[9],
        "source_schema_hash": row[10],
        "policy_version": row[11],
        "catalog_version": row[12],
        "tool_version": row[13],
    }


def _load_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if value is not None else {}


def _load_live_pointer(conn, datastream_id: str, project_id: str) -> str | None:
    """Read the live ``current_mapping_version_id`` for the changed-pointer recheck."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_mapping_version_id
            FROM app.datastreams
            WHERE id = %s AND project_id = %s
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _current_policy_version() -> str:
    """Return the governing policy version (admin capability-catalog snapshot).

    Reuses ``mcp_profiles.catalog_version("admin")`` as the governing policy pin so a
    changed catalog/policy since review is detectable at confirm (AC3). Pure and
    deterministic; never a wall-clock value.
    """
    from core import mcp_profiles  # noqa: PLC0415

    try:
        return mcp_profiles.catalog_version("admin")
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Confirmation-record persistence (migration 073). Immutable review evidence.
# ---------------------------------------------------------------------------


def _insert_confirmation(
    conn,
    *,
    confirmation_id: str,
    proposal: dict[str, Any],
    actor: str,
    confirmation_reference_hash: str,
    reviewed_hashes: dict[str, Any],
    prior_mapping_version_id: str | None,
    policy_version: str,
    idempotency_key_hash: str,
) -> None:
    """Insert one immutable prepared publication confirmation. Caller owns the txn.

    The row is the persisted AD-27 review evidence: proposal/version scope, actor,
    the confirmation secret's one-way hash, the reviewed hash bundle, the rollback
    target (prior version), the pinned policy, and the idempotency-key hash. NO
    durable operation exists yet and NO pointer moved.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.publication_confirmations
                (id, proposal_id, mapping_version_id, datastream_id, project_id, org_id,
                 actor, confirmation_reference_hash, reviewed_hashes,
                 prior_mapping_version_id, policy_version, idempotency_key_hash,
                 expires_at, state)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s,
                    NOW() + (%s || ' seconds')::interval, 'prepared')
            """,
            (
                confirmation_id,
                proposal["id"],
                proposal["mapping_version_id"],
                proposal["datastream_id"],
                proposal["project_id"],
                proposal["org_id"],
                actor,
                confirmation_reference_hash,
                _canonical_json(reviewed_hashes),
                prior_mapping_version_id,
                policy_version,
                idempotency_key_hash,
                str(_REVIEW_TTL_SECONDS),
            ),
        )


def _load_confirmation(conn, confirmation_id: str) -> dict[str, Any] | None:
    """Load one publication confirmation by id (for confirm), or None. FOR UPDATE."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, proposal_id, mapping_version_id, datastream_id, project_id, org_id,
                   actor, confirmation_reference_hash, reviewed_hashes,
                   prior_mapping_version_id, policy_version, idempotency_key_hash,
                   state, operation_id, (expires_at <= NOW()) AS expired
            FROM app.publication_confirmations
            WHERE id = %s
            FOR UPDATE
            """,
            (confirmation_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "proposal_id": row[1],
        "mapping_version_id": row[2],
        "datastream_id": row[3],
        "project_id": row[4],
        "org_id": row[5],
        "actor": row[6],
        "confirmation_reference_hash": row[7],
        "reviewed_hashes": row[8] if isinstance(row[8], dict) else _load_json(row[8]),
        "prior_mapping_version_id": row[9],
        "policy_version": row[10],
        "idempotency_key_hash": row[11],
        "state": row[12],
        "operation_id": row[13],
        "expired": bool(row[14]),
    }


def _mark_confirmation_state(
    conn, confirmation_id: str, state: str, operation_id: str | None
) -> None:
    """Attach the durable operation + flip the lifecycle (lifecycle-only transition).

    The migration-073 trigger permits ONLY this lifecycle transition; every
    substantive review field stays frozen (AD-27 immutable review evidence).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.publication_confirmations
            SET state = %s, operation_id = COALESCE(%s, operation_id), updated_at = NOW()
            WHERE id = %s
            """,
            (state, operation_id, confirmation_id),
        )


# ---------------------------------------------------------------------------
# The pointer-advance mutation (routed through operations.execute_operation ONCE).
# ---------------------------------------------------------------------------


def _advance_pointer(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    to_version_id: str,
    expected_from_version_id: str | None,
    actor: str,
    command: str,
) -> tuple[str | None, int]:
    """Advance ``current_mapping_version_id`` to *to_version_id*, guarded.

    Locks the datastream row FOR UPDATE, verifies the live pointer still equals
    ``expected_from_version_id`` (a last-line changed-pointer guard INSIDE the
    operation txn), and swaps the pointer. Returns ``(prior_version_id, rowcount)``.
    A ``rowcount`` of 0 means the guard failed (someone moved the pointer) -- the
    caller surfaces a failed outcome so NO fresh blind mutation is submitted.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_mapping_version_id
            FROM app.datastreams
            WHERE id = %s AND project_id = %s
            FOR UPDATE
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None, 0
        prior = row[0]
        # Last-line changed-pointer guard: the live pointer must still be the one the
        # review pinned. A drift here means a concurrent publish beat us -> refuse.
        if prior != expected_from_version_id:
            return prior, 0
        cur.execute(
            """
            UPDATE app.datastreams
            SET current_mapping_version_id = %s
            WHERE id = %s AND project_id = %s
              AND current_mapping_version_id IS NOT DISTINCT FROM %s
            """,
            (to_version_id, datastream_id, project_id, expected_from_version_id),
        )
        rowcount = cur.rowcount
        if rowcount == 1:
            # Append-only publication log (rollback evidence): the version that just
            # went live + the prior one it replaced.
            cur.execute(
                """
                INSERT INTO app.datastream_mapping_publication_log
                    (id, datastream_id, project_id, mapping_version_id,
                     prior_mapping_version_id, command, published_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    f"mpub_{ULID()}",
                    datastream_id,
                    project_id,
                    to_version_id,
                    prior,
                    command,
                    actor,
                ),
            )
    return prior, rowcount


def _publish_mutation(
    conn,
    operation_id: str,
    *,
    confirmation: dict[str, Any],
    command: str,
    to_version_id: str,
    from_version_id: str | None,
):
    """The publication/rollback mutation (Story 36.2). Advances the live pointer.

    Runs INSIDE ``operations.execute_operation`` so the pointer swap + publication
    log are atomic with the durable operation's audit + outbox and idempotent on
    replay. Returns a ``MutationResult``. On a changed-pointer guard failure it
    returns a FAILED outcome carrying the original references (the caller reconciles
    with those refs; it never submits a fresh blind mutation).
    """
    from core.operations import MutationResult  # noqa: PLC0415

    try:
        prior, rowcount = _advance_pointer(
            conn,
            datastream_id=confirmation["datastream_id"],
            project_id=confirmation["project_id"],
            to_version_id=to_version_id,
            expected_from_version_id=from_version_id,
            actor=confirmation["actor"],
            command=command,
        )
    except Exception as exc:  # noqa: BLE001 -- outcome uncertain, never a duplicate.
        logger.error(
            "governed_publication: pointer advance failed conf=%s: %s", confirmation["id"], exc
        )
        return MutationResult(
            outcome="outcome_unknown",
            before_hash=None,
            after_hash=None,
            result={"reason": "pointer_advance_uncertain", "command": command},
            outbox_payload={"command": command, "datastream_id": confirmation["datastream_id"]},
        )
    if rowcount != 1:
        # Changed-pointer: a concurrent publish moved the live pointer. No blind fresh
        # mutation -- report failed with the original refs so reconciliation is possible.
        return MutationResult(
            outcome="failed",
            before_hash=None,
            after_hash=None,
            result={"reason": "changed_pointer", "command": command,
                    "observed_pointer": prior, "expected_pointer": from_version_id},
            outbox_payload={"command": command, "datastream_id": confirmation["datastream_id"]},
        )
    result = {
        "command": command,
        "datastream_id": confirmation["datastream_id"],
        "current_mapping_version_id": to_version_id,
        "prior_mapping_version_id": prior,
    }
    return MutationResult(
        outcome="succeeded",
        before_hash=(_canonical_hash(prior) if prior else None),
        after_hash=_canonical_hash(to_version_id),
        result=result,
        outbox_payload=result,
    )


# ---------------------------------------------------------------------------
# 1) prepare_publication_review -- read-only + mint model-hidden secret.
# ---------------------------------------------------------------------------


def prepare_publication_review(
    conn,
    *,
    proposal_id: str,
    actor: str,
    org_id: str,
    host_context: dict[str, Any] | None = None,
) -> PublicationReview:
    """Assemble the governed review object and mint the model-hidden secret.

    Read-only over the domain state (it inserts ONLY the immutable confirmation
    record; it advances NO pointer). The review shows: profile (governance),
    actor/resource scope, versions (candidate + prior), the mapping diff (via
    ``diff_mappings`` -- no raw provider values), interval/destination, expected
    impact, the recorded gate checks, the rollback path (prior version), and expiry.

    Mints an OPAQUE confirmation secret server-side; only its sha256 is persisted
    (``confirmation_reference_hash``). The secret is returned in
    ``PublicationReview.confirmation_secret`` for the trusted console / in-host
    presence path and is NEVER placed in ``review`` (the model-facing object).

    Raises ``PublicationReviewUnavailable`` when the proposal is missing, out of
    scope, or not in the 'ready' state (fail-closed; only a ready proposal is
    publishable).
    """
    from core.mapping_diff import diff_mappings  # noqa: PLC0415

    proposal = _load_ready_proposal(conn, proposal_id)
    if proposal is None or str(proposal["org_id"]) != str(org_id):
        # Existence-hiding: do not disclose whether the proposal exists out of scope.
        raise PublicationReviewUnavailable("Proposition introuvable.")
    if proposal["state"] != _PROPOSAL_READY:
        raise PublicationReviewUnavailable("La proposition n'est pas prete a etre publiee.")

    datastream_id = proposal["datastream_id"]
    project_id = proposal["project_id"]
    live_pointer = _load_live_pointer(conn, datastream_id, project_id)

    # Build the diff from the immutable candidate + the live/prior version rows.
    with conn.cursor() as cur:
        after_row = _fetch_version_row(
            cur, proposal["mapping_version_id"], datastream_id, project_id
        )
        before_row = (
            _fetch_version_row(cur, live_pointer, datastream_id, project_id)
            if live_pointer
            else None
        )
    if after_row is None:
        raise PublicationReviewUnavailable("Version candidate introuvable.")
    diff = proposal.get("diff") or diff_mappings(before_row, after_row)

    policy_version = _current_policy_version()
    confirmation_id = _mint_confirmation_id()
    confirmation_secret = _mint_confirmation_secret()

    # The reviewed-hash bundle the confirm will re-check against live state.
    # Bind the reviewed workspace when the host context carries a server-verified
    # workspace_id, so a confirm from a DIFFERENT workspace than the review is
    # rejected (review H1 -- the mismatch guard was dead because this was never set).
    reviewed_hashes = {
        "content_hash": proposal["content_hash"],
        "source_schema_hash": proposal["source_schema_hash"],
        "prior_mapping_version_id": live_pointer,
    }
    _reviewed_workspace = (host_context or {}).get("workspace_id")
    if _reviewed_workspace is not None:
        reviewed_hashes["workspace_id"] = _reviewed_workspace
    idempotency_key = _publication_idempotency_key(
        str(org_id), datastream_id, proposal["mapping_version_id"], confirmation_id
    )

    try:
        _insert_confirmation(
            conn,
            confirmation_id=confirmation_id,
            proposal=proposal,
            actor=actor,
            confirmation_reference_hash=_sha256(confirmation_secret),
            reviewed_hashes=reviewed_hashes,
            prior_mapping_version_id=live_pointer,
            policy_version=policy_version,
            idempotency_key_hash=_sha256(idempotency_key),
        )
    except Exception as exc:  # noqa: BLE001 -- fail-closed on persistence error.
        logger.error(
            "governed_publication: review persistence failed prop=%s: %s", proposal_id, exc
        )
        raise PublicationReviewUnavailable("Preparation de la revue impossible.") from exc

    review = {
        "confirmation_id": confirmation_id,
        "profile": GOVERNANCE_PROFILE,
        "scope": {
            "actor": actor,
            "org_id": str(org_id),
            "project_id": project_id,
            "datastream_id": datastream_id,
            "resource_path": [f"organization:{org_id}", f"flux:{datastream_id}"],
        },
        "versions": {
            "candidate_mapping_version_id": proposal["mapping_version_id"],
            "candidate_content_hash": proposal["content_hash"],
            "candidate_source_schema_hash": proposal["source_schema_hash"],
            "prior_mapping_version_id": live_pointer,
            "policy_version": policy_version,
        },
        "diff": diff,
        # Publication is a mapping-pointer advance: the destination is the datastream's
        # current mapping pointer, not a provider interval. Interval is None here.
        "interval": None,
        "destination": {
            "kind": "mapping_pointer",
            "datastream_id": datastream_id,
            "advances": "current_mapping_version_id",
        },
        "impact": {
            "advances_pointer": True,
            "from_version_id": live_pointer,
            "to_version_id": proposal["mapping_version_id"],
            "prior_version_rollbackable": True,
        },
        "checks": proposal.get("checks"),
        "rollback": {
            "available": live_pointer is not None,
            "target_mapping_version_id": live_pointer,
        },
        "expires_in_seconds": _REVIEW_TTL_SECONDS,
        "confirmation_required": True,
        # NOTE: the confirmation secret is INTENTIONALLY ABSENT from this object.
    }
    return PublicationReview(
        confirmation_id=confirmation_id,
        review=review,
        confirmation_secret=confirmation_secret,
    )


# ---------------------------------------------------------------------------
# 2) confirm_and_publish -- verify secret + rechecks + ONE durable operation.
# ---------------------------------------------------------------------------


def confirm_and_publish(
    conn,
    *,
    confirmation_id: str,
    confirmation_secret: str,
    actor: str,
    org_id: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> PublicationResult:
    """Verify the opaque secret + rechecks, then route ONE publication operation.

    Sequence (all inside *conn*):
      1. Load the immutable confirmation (FOR UPDATE). Missing -> refused.
      2. Verify the presented opaque secret hashes to the pinned
         ``confirmation_reference_hash``; an invalid secret is ``confirmation_invalid``
         and the secret is NEVER echoed back.
      3. Recheck: expired / already used (state != prepared) / actor mismatch /
         workspace-or-org mismatch / changed policy / stale versions / changed pointer.
         ANY breach raises ``PublicationConfirmationRefused`` with a stable code and
         NO operation is created or dispatched (AC3).
      4. Route EXACTLY ONE ``operations.execute_operation`` bound to all reviewed
         hashes/versions/policy; its mutation advances ``current_mapping_version_id``
         atomically with audit + outbox. A duplicate confirm / timeout replays the
         ORIGINAL operation (never a duplicate).
      5. On failure / outcome_unknown the caller reconciles with the ORIGINAL
         operation/provider references (no blind fresh mutation).

    The caller owns the commit. Returns a ``PublicationResult``.
    """
    from core.operations import OperationSpec, execute_operation  # noqa: PLC0415

    confirmation = _load_confirmation(conn, confirmation_id)
    if confirmation is None or str(confirmation["org_id"]) != str(org_id):
        raise PublicationConfirmationRefused("used", "Confirmation introuvable.")

    # (secret) Verify the opaque, model-hidden secret. Constant-time comparison of the
    # one-way digests; the raw secret never leaves this frame and is never returned.
    presented_hash = _sha256(confirmation_secret or "")
    if not secrets.compare_digest(presented_hash, confirmation["confirmation_reference_hash"]):
        raise PublicationConfirmationRefused("confirmation_invalid", "Confirmation invalide.")

    # (used / replayed) Only a 'prepared' confirmation may be consumed. A confirmed/
    # dispatched one is a replay -> return the ORIGINAL operation, never a duplicate.
    if confirmation["state"] != "prepared":
        if confirmation.get("operation_id"):
            return _replay_from_operation(conn, confirmation)
        raise PublicationConfirmationRefused("used", "Confirmation deja traitee.")

    # (expired)
    if confirmation["expired"]:
        raise PublicationConfirmationRefused("expired", "Confirmation expiree.")

    # (actor mismatch) confirmation is bound to the reviewing actor.
    if confirmation["actor"] != actor:
        raise PublicationConfirmationRefused(
            "actor_mismatch", "Acteur non autorise pour cette confirmation."
        )

    # (workspace mismatch) the confirming workspace must match the review workspace
    # when the review was bound to one (server-verified in-host presence). Absent a
    # bound workspace on either side we do not manufacture a match.
    host_context = host_context or {}
    reviewed_ws = (confirmation.get("reviewed_hashes") or {}).get("workspace_id")
    presented_ws = host_context.get("workspace_id")
    if reviewed_ws is not None and reviewed_ws != presented_ws:
        raise PublicationConfirmationRefused(
            "workspace_mismatch", "Espace de travail non concordant."
        )

    # (changed policy)
    if confirmation["policy_version"] != _current_policy_version():
        raise PublicationConfirmationRefused(
            "policy_changed", "Politique modifiee depuis la revue."
        )

    # Reload the proposal: it must still be 'ready' (a failed-check/superseded proposal
    # must not publish).
    proposal = _load_ready_proposal(conn, confirmation["proposal_id"])
    if proposal is None or proposal["state"] != _PROPOSAL_READY:
        raise PublicationConfirmationRefused(
            "proposal_not_ready", "La proposition n'est plus publiable."
        )

    # (stale versions / changed pointer) re-check the reviewed hashes against live.
    live_pointer = _load_live_pointer(
        conn, confirmation["datastream_id"], confirmation["project_id"]
    )
    live_facts = {
        "content_hash": proposal["content_hash"],
        "source_schema_hash": proposal["source_schema_hash"],
        "prior_mapping_version_id": live_pointer,
    }
    if not reviewed_hashes_match(confirmation["reviewed_hashes"], live_facts):
        # Distinguish a moved pointer from a candidate-hash drift for the code.
        if confirmation["reviewed_hashes"].get("prior_mapping_version_id") != live_pointer:
            raise PublicationConfirmationRefused(
                "changed_pointer", "Le pointeur publie a change depuis la revue."
            )
        raise PublicationConfirmationRefused("stale_versions", "Versions revues perimees.")

    # All rechecks pass: route EXACTLY ONE durable operation (Story 36.2). The
    # idempotency key is rebuilt from the SAME immutable confirmation so a duplicate
    # confirm replays the original operation.
    idempotency_key = _publication_idempotency_key(
        confirmation["org_id"],
        confirmation["datastream_id"],
        confirmation["mapping_version_id"],
        confirmation["id"],
    )
    if _sha256(idempotency_key) != confirmation["idempotency_key_hash"]:
        raise PublicationConfirmationRefused("used", "Confirmation alteree.")

    to_version_id = confirmation["mapping_version_id"]
    from_version_id = confirmation["reviewed_hashes"].get("prior_mapping_version_id")

    spec = OperationSpec(
        command_type=COMMAND_PUBLISH,
        actor=actor,
        effective_org_id=confirmation["org_id"],
        resource_path=(
            f"organization:{confirmation['org_id']}",
            f"flux:{confirmation['datastream_id']}",
        ),
        idempotency_key=idempotency_key,
        host_context={
            k: host_context.get(k)
            for k in ("host", "workspace_id", "workspace_type", "client_id")
            if host_context.get(k) is not None
        },
        versions={
            "policy": confirmation["policy_version"],
            "catalog": _current_policy_version(),
            "tool": "confirm_agent_change",
        },
        request_payload={
            "proposal_id": confirmation["proposal_id"],
            "mapping_version_id": to_version_id,
            "from_version_id": from_version_id,
            "content_hash": confirmation["reviewed_hashes"].get("content_hash"),
            "source_schema_hash": confirmation["reviewed_hashes"].get("source_schema_hash"),
        },
        provider_references={},
        confirmation_mode="human",
        # hashed by prepare_operation into confirmation_reference_hash; never persisted raw.
        confirmation_reference=confirmation_secret,
        trace_id=trace_id,
    )

    op_result = execute_operation(
        conn,
        spec,
        mutation=lambda c, op_id: _publish_mutation(
            c, op_id, confirmation=confirmation, command=COMMAND_PUBLISH,
            to_version_id=to_version_id, from_version_id=from_version_id,
        ),
    )
    # Link the durable operation back to the immutable confirmation (lifecycle only).
    if op_result.outcome == "succeeded":
        new_state = "dispatched"
    elif op_result.outcome == "failed":
        new_state = "failed"
    else:
        new_state = "confirmed"
    _mark_confirmation_state(conn, confirmation["id"], new_state, op_result.operation_id)

    # First-value funnel (E36-FR09): a governed publish is a recovery_action with the
    # reconcile recovery kind. The pseudonymous ref is built inside the seam from the
    # confirmation's (org, project, datastream); only enums + the hash are persisted.
    # Best-effort -- NEVER breaks the publication.
    from core.first_value_instrumentation import record_stage  # noqa: PLC0415

    record_stage(
        conn,
        org_id=confirmation.get("org_id"),
        project_id=confirmation.get("project_id"),
        subject=confirmation.get("datastream_id"),
        stage="recovery_action",
        outcome="succeeded" if op_result.outcome == "succeeded" else "failed",
        recovery_kind="reconcile",
    )

    result_data = op_result.result or {}
    return PublicationResult(
        confirmation_id=confirmation["id"],
        operation_id=op_result.operation_id,
        outcome=op_result.outcome,
        replayed=op_result.replayed,
        current_mapping_version_id=str(
            result_data.get("current_mapping_version_id", to_version_id)
        ),
        prior_mapping_version_id=result_data.get("prior_mapping_version_id", from_version_id),
        result=result_data,
    )


def _replay_from_operation(conn, confirmation: dict[str, Any]) -> PublicationResult:
    """Return the ORIGINAL operation outcome for an already-consumed confirmation.

    A duplicate confirm after the operation was minted must never re-run the mutation
    or create a duplicate: it reads the durable operation's recorded outcome (Story
    36.2 is the single source of truth) and returns it as a replay.
    """
    operation_id = confirmation["operation_id"]
    outcome = "outcome_unknown"
    result_data: dict[str, Any] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, result FROM app.operations WHERE id = %s",
            (operation_id,),
        )
        row = cur.fetchone()
    if row:
        outcome = row[0] or "outcome_unknown"
        result_data = row[1] if isinstance(row[1], dict) else _load_json(row[1])
    return PublicationResult(
        confirmation_id=confirmation["id"],
        operation_id=operation_id,
        outcome=outcome,
        replayed=True,
        current_mapping_version_id=str(
            result_data.get("current_mapping_version_id", confirmation["mapping_version_id"])
        ),
        prior_mapping_version_id=result_data.get("prior_mapping_version_id"),
        result=result_data,
    )


# ---------------------------------------------------------------------------
# 3) rollback_publication -- a DISTINCT confirmed idempotent operation.
# ---------------------------------------------------------------------------


def rollback_publication(
    conn,
    *,
    confirmation_id: str,
    target_mapping_version_id: str,
    actor: str,
    org_id: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> PublicationResult:
    """Roll the live pointer back to a prior version as a DISTINCT operation.

    Rollback re-points ``current_mapping_version_id`` back to
    *target_mapping_version_id* (the prior version captured at publication). It is a
    SEPARATE durable operation (``COMMAND_ROLLBACK`` idempotency namespace) with its
    OWN audit + outbox -- NEVER a reuse of the forward publication's operation
    (NFR15). It reuses the same guarded pointer-advance mutation, so it is atomic and
    idempotent on replay. The rollback target must be the version this confirmation
    replaced (its ``prior_mapping_version_id``); any other target is refused.

    The caller owns the commit. Returns a ``PublicationResult``.
    """
    from core.operations import OperationSpec, execute_operation  # noqa: PLC0415

    confirmation = _load_confirmation(conn, confirmation_id)
    if confirmation is None or str(confirmation["org_id"]) != str(org_id):
        raise PublicationConfirmationRefused("used", "Confirmation introuvable.")
    if confirmation["actor"] != actor:
        raise PublicationConfirmationRefused(
            "actor_mismatch", "Acteur non autorise pour ce rollback."
        )

    prior = confirmation.get("prior_mapping_version_id")
    if not prior or target_mapping_version_id != prior:
        raise PublicationConfirmationRefused(
            "rollback_target_invalid", "Cible de rollback non autorisee."
        )

    # The forward publication advanced the pointer to the candidate; rollback expects
    # the live pointer to be that candidate and moves it back to the prior version.
    from_version_id = confirmation["mapping_version_id"]

    idempotency_key = _rollback_idempotency_key(
        confirmation["org_id"],
        confirmation["datastream_id"],
        target_mapping_version_id,
        confirmation["id"],
    )

    rollback_confirmation = dict(confirmation)
    rollback_confirmation["id"] = f"{confirmation['id']}:rollback"

    spec = OperationSpec(
        command_type=COMMAND_ROLLBACK,
        actor=actor,
        effective_org_id=confirmation["org_id"],
        resource_path=(
            f"organization:{confirmation['org_id']}",
            f"flux:{confirmation['datastream_id']}",
        ),
        idempotency_key=idempotency_key,
        host_context={
            k: (host_context or {}).get(k)
            for k in ("host", "workspace_id", "workspace_type", "client_id")
            if (host_context or {}).get(k) is not None
        },
        versions={
            "policy": confirmation["policy_version"],
            "catalog": _current_policy_version(),
            "tool": "rollback_agent_change",
        },
        request_payload={
            "confirmation_id": confirmation["id"],
            "rollback_to_version_id": target_mapping_version_id,
            "from_version_id": from_version_id,
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=None,
        trace_id=trace_id,
    )

    op_result = execute_operation(
        conn,
        spec,
        mutation=lambda c, op_id: _publish_mutation(
            c, op_id, confirmation=rollback_confirmation, command=COMMAND_ROLLBACK,
            to_version_id=target_mapping_version_id, from_version_id=from_version_id,
        ),
    )
    result_data = op_result.result or {}

    # First-value funnel (E36-FR09): a governed rollback is a recovery_action with the
    # rollback recovery kind. Best-effort -- NEVER breaks the rollback.
    from core.first_value_instrumentation import record_stage  # noqa: PLC0415

    record_stage(
        conn,
        org_id=confirmation.get("org_id"),
        project_id=confirmation.get("project_id"),
        subject=confirmation.get("datastream_id"),
        stage="recovery_action",
        outcome="succeeded" if op_result.outcome == "succeeded" else "failed",
        recovery_kind="rollback",
    )

    return PublicationResult(
        confirmation_id=confirmation["id"],
        operation_id=op_result.operation_id,
        outcome=op_result.outcome,
        replayed=op_result.replayed,
        current_mapping_version_id=str(
            result_data.get("current_mapping_version_id", target_mapping_version_id)
        ),
        prior_mapping_version_id=result_data.get("prior_mapping_version_id", from_version_id),
        result=result_data,
    )


# ---------------------------------------------------------------------------
# Reconciliation on failure / outcome_unknown -- uses ORIGINAL refs only.
# ---------------------------------------------------------------------------


def reconcile_publication_outcome(conn, *, operation_id: str, resolver) -> bool:
    """Resolve one uncertain publication using ONLY the original durable refs.

    Delegates to ``operations.reconcile_uncertain_operation`` which reads the stored
    ``provider_references`` for *operation_id* and passes them to *resolver* -- so
    reconciliation is grounded in the ORIGINAL operation/provider references and never
    submits a fresh blind mutation (AC5). Returns True when the state resolved.
    """
    from core.operations import reconcile_uncertain_operation  # noqa: PLC0415

    return reconcile_uncertain_operation(conn, operation_id=operation_id, resolver=resolver)
