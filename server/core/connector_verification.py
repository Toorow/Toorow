"""Story 38.4 -- continuous domain verification and synthetic test delivery.

A platform operator runs verification to evaluate the evidence that the active
domain config is correctly routing deliveries. Each run is immutable evidence
appended to ``app.connector_verification_runs`` and drives the installation state
machine: a passing run advances DOMAIN_PENDING/VERIFYING -> READY; a failing run
on a READY installation moves it to DEGRADED (with first-seen carried forward from
the prior degraded run, if any).

A synthetic test delivery exercises the receipt-adapter authentication seam at
auth-only level and is explicitly flagged ``synthetic_delivery = TRUE``. It writes
NO durable receipt, quarantine object, mapping row, or publication -- none of those
stages exist yet in 38.4. Its outcome is recorded as a verification evidence row,
not as a tenant import.

INVARIANTS (adversarially enforced, mirroring connector_installation.py):

  * SOURCE-AGNOSTIC. No provider/adapter/vendor vocabulary enters this module --
    not in code, not in comments, not in docstrings (the AD-2 boundary scanner
    is literal). Check functions are INJECTABLE: they are passed in by the caller
    so provider-specific logic lives outside core (AD-2). Synthetic delivery calls
    the receipt-adapter seam through an injectable runner function that the caller
    supplies; core only defines the shape of that call.

  * EVERY WRITE ROUTES THROUGH ``operations.execute_operation`` (atomic audit +
    outbox, idempotent replay). No parallel audit path exists here.

  * SECRET-FREE READ MODEL. ``get_verification_state`` and every return value
    carry NO DNS token, NO signing secret, NO raw credential. ``evidence_hash``
    is a sha256 digest of evidence class + timestamp -- safe for storage.

  * DETERMINISTIC IDEMPOTENCY (review H1). No random id enters
    ``request_payload``. Row ids are generated at WRITE TIME inside the mutation
    closure. Two identical runs with the same Idempotency-Key replay cleanly.

  * ROWCOUNT CHECKED (review H2). Every immutable INSERT must affect exactly
    one row. An impossible collision is rejected instead of being reconciled
    to unrelated evidence. The read-model comes from the operation result.

  * SYNTHETIC DELIVERY SHORT-CIRCUITS (AC3). The injectable delivery runner MUST
    NOT write a quarantine object, raw import, or publication. The mutation asserts
    ``synthetic_delivery = TRUE`` on the appended run row. No Datastream import
    path is called here.

  * CARRY FORWARD first_seen_at (AC2). When a READY installation degrades, the
    ``first_seen_at`` is carried forward from the prior degraded run (if any) so
    operators see the original onset time, not the most recent detection.

Mirrors ``connector_installation.py`` and ``connector_domain.py`` conventions:
``from __future__ import annotations``, lazy imports of the shared seams,
mutations only through the operation seam, ASCII-only source.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from ulid import ULID

from core.audit import declare_action

# Imported at module scope on purpose: it is the one fact this module needs about
# the connector, and binding it here is what makes the answer substitutable in a
# test without touching the registry the rest of the process shares.
from core.connector_family import owes_routing_contract
from core.operations import MutationResult, OperationSpec, execute_operation

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_CONNECTOR_VERIFICATION_RAN = declare_action("connector.verification.ran")


# ---------------------------------------------------------------------------
# Constants / evidence classes (AC1, Task 2).
# ---------------------------------------------------------------------------

#: Outcome values matching the CHECK constraint in migration 085.
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_DEGRADED = "degraded"
OUTCOME_NOT_CONFIGURED = "not_configured"

#: Evidence class tags. Opaque strings only -- no provider vocabulary.
EVIDENCE_CLASS_ROUTING = "routing_check"
EVIDENCE_CLASS_AUTH = "auth_check"
EVIDENCE_CLASS_SYNTHETIC = "synthetic_delivery_auth"
EVIDENCE_CLASS_NO_CHECKS = "no_checks"

# States from which a passing verification can advance to READY.
# Includes DEGRADED (F2): DEGRADED -> READY is an allowed transition and must
# be driven on all-pass so a recovered installation can return to READY.
_ADVANCEABLE_TO_READY: frozenset[str] = frozenset({"DOMAIN_PENDING", "VERIFYING", "DEGRADED"})

# States that can degrade to DEGRADED on a failing check.
_DEGRADABLE: frozenset[str] = frozenset({"READY"})

# Default re-check interval TTL in seconds (1 hour).
_DEFAULT_TTL_SECONDS = 3600
_MAX_IDEMPOTENCY_KEY_LENGTH = 255
_ALLOWED_CHECK_EVIDENCE_CLASSES: frozenset[str] = frozenset({
    EVIDENCE_CLASS_ROUTING,
    EVIDENCE_CLASS_AUTH,
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConnectorVerificationError(ValueError):
    """Raised before SQL when verification inputs are unsafe or malformed."""


class ConnectorVerificationUnavailable(RuntimeError):
    """The installation or domain config row is missing; verification cannot run."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _evidence_hash(evidence_class: str, timestamp_isoformat: str) -> str:
    """Produce a sha256 digest of evidence class + timestamp (safe for storage).

    Never hashes a secret, DNS token, or signing value -- only the opaque
    evidence class string and the ISO-8601 wall-clock string.
    """
    payload = json.dumps(
        {"evidence_class": evidence_class, "sampled_at": timestamp_isoformat},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_iso(ts: Any) -> str | None:
    """Serialize a timestamp to ISO-8601, or None."""
    if ts is None:
        return None
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)


# ---------------------------------------------------------------------------
# Safe read-model projection (AC4). No secret, no DNS token, no credential.
# ---------------------------------------------------------------------------

_SAFE_NEXT_ACTIONS: dict[str, str] = {
    "passed":   "no action required -- verification passed; connector is READY",
    "failed":   "platform_admin: resolve blocking reason, then re-run verification",
    "degraded": "platform_admin: resolve blocking reason, then re-run verification",
}


def _safe_run_read_model(
    *,
    connector_name: str,
    environment: str,
    state: str,
    outcome: str | None,
    evidence_class: str | None,
    first_seen_at: Any,
    created_at: Any,
    blocking_reason: str | None,
    synthetic_delivery: bool,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Build the secret-free read-model for a verification run.

    NEVER includes evidence_hash, signing secrets, DNS tokens, or any raw
    credential (AC4, E38-NFR03).

    ``ttl_seconds`` IS PART OF THE ANSWER, not decoration. The evidence row has
    carried a re-check interval since migration 085 and no reader ever asked for
    it, so "verified" and "verified within the interval it is checked on" were
    the same sentence to every caller. A console that must tell a person whether
    a `READY` connector's evidence has run out cannot invent that interval, and
    the expiry itself is NOT returned: `last_run_at + ttl_seconds` is derivable,
    and two representations of one instant can disagree.
    """
    return {
        "connector_name": connector_name,
        "environment": environment,
        "installation_state": state,
        "last_outcome": outcome,
        "evidence_class": evidence_class,
        "first_seen_at": _safe_iso(first_seen_at),
        "last_run_at": _safe_iso(created_at),
        "blocking_reason": blocking_reason,
        "synthetic_delivery": synthetic_delivery,
        "ttl_seconds": ttl_seconds,
        "safe_next_action": _SAFE_NEXT_ACTIONS.get(outcome or "", "run verification"),
    }


# ---------------------------------------------------------------------------
# get_verification_state -- nondisclosing read (AC4, Task 2).
# ---------------------------------------------------------------------------


def get_verification_state(
    conn,
    *,
    environment: str,
    connector_name: str,
) -> dict[str, Any] | None:
    """Return the safe read-model for the latest verification run, or None.

    None means no verification run has ever been recorded for this connector
    in this environment. Callers treat None as unverified. Does NOT raise for
    absence; existence is never disclosed to unauthorized callers (that is the
    REST layer's responsibility).

    NEVER returns evidence_hash, signing secrets, DNS tokens, or any raw
    credential (AC4, E38-NFR03).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.outcome, r.evidence_class, r.first_seen_at, r.created_at, "
            "       r.blocking_reason, r.synthetic_delivery, r.ttl_seconds, "
            "       i.state "
            "FROM app.connector_verification_runs r "
            "JOIN app.connector_installations i "
            "     ON i.id = r.installation_id "
            "WHERE r.environment = %s AND r.connector_name = %s "
            "ORDER BY r.created_at DESC "
            "LIMIT 1",
            (environment, connector_name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    (
        outcome,
        ev_class,
        first_seen,
        last_run,
        reason,
        synthetic,
        ttl_seconds,
        inst_state,
    ) = row
    return _safe_run_read_model(
        connector_name=connector_name,
        environment=environment,
        state=inst_state,
        outcome=outcome,
        evidence_class=ev_class,
        first_seen_at=first_seen,
        created_at=last_run,
        blocking_reason=reason,
        synthetic_delivery=bool(synthetic),
        ttl_seconds=None if ttl_seconds is None else int(ttl_seconds),
    )


# ---------------------------------------------------------------------------
# run_verification -- evaluate checks and drive state (AC1, AC2, Task 2).
# ---------------------------------------------------------------------------


def run_verification(
    conn,
    *,
    environment: str,
    connector_name: str,
    checks: list[Callable[[], tuple[bool, str, str]]],
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Evaluate injectable checks, append a run, and drive the installation state.

    Parameters
    ----------
    checks:
        A list of zero-argument callables, each returning
        ``(passed: bool, evidence_class: str, reason: str)``.
        All checks are evaluated; the first failure wins.
        The callables are OPAQUE to this module (AD-2): they contain any
        provider-specific logic the caller requires, but this module never
        inspects or names them.

    Returns the safe read-model (secret-free).

    State transitions (AC1):
      * DOMAIN_PENDING / VERIFYING -> READY on all-pass.
      * READY -> DEGRADED on any blocking check failing; first_seen_at carried
        forward from the most recent prior DEGRADED run (AC2).

    Each run is an immutable append to ``app.connector_verification_runs``
    through ``execute_operation`` (AC5).
    """
    # ------------------------------------------------------------------
    # Validation.
    # ------------------------------------------------------------------
    if not isinstance(environment, str) or not environment.strip():
        raise ConnectorVerificationError("environment is required")
    if not isinstance(connector_name, str) or not connector_name.strip():
        raise ConnectorVerificationError("connector_name is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ConnectorVerificationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ConnectorVerificationError("idempotency_key is required")
    if len(idempotency_key.strip()) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ConnectorVerificationError("idempotency_key is too long")

    environment = environment.strip()
    connector_name = connector_name.strip()
    actor = actor.strip()
    idempotency_key = idempotency_key.strip()

    # ------------------------------------------------------------------
    # Load the installation row.
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state "
            "FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s "
            "FOR UPDATE",
            (environment, connector_name),
        )
        inst_row = cur.fetchone()

    if inst_row is None:
        raise ConnectorVerificationUnavailable(
            "connector installation row not found -- apply installation first"
        )
    installation_id, current_state = inst_row

    # ------------------------------------------------------------------
    # Load and lock the active domain config. Verification without the active
    # configuration would attach evidence to no routing contract.
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.connector_domain_configs "
            "WHERE environment = %s AND connector_name = %s "
            "AND superseded_by IS NULL "
            "FOR SHARE",
            (environment, connector_name),
        )
        dc_row = cur.fetchone()

    if dc_row is None:
        # AI-206. An installation that never owed a domain cannot be refused for
        # not having one. THE QUESTION IS ASKED OF THE FAMILY, not of the current
        # state: this read `current_state != "VERIFYING"` until 2026-08-17, which
        # is true for exactly one instant -- the moment after `apply_installation`
        # opens a domain-less installation at VERIFYING (migration 267). The first
        # passing run moves the same row to READY, and every later verification of
        # that connector was then refused for not having a domain it never owed.
        #
        # Three things that cost: story 38.4 is CONTINUOUS verification and the
        # evidence row carries a `ttl_seconds` whose re-run could never happen for
        # any of the 39 module connectors; a platform prerequisite that disappeared
        # could never degrade the installation, READY being the only state
        # `_DEGRADABLE` admits and READY being refused entry; and
        # `_SAFE_NEXT_ACTIONS["DEGRADED"]` offered « resolve cause, then advance to
        # READY or VERIFYING », a gesture this function would have refused.
        #
        # Not a second caller-supplied flag either -- that was the objection the
        # state proxy was chosen over, and it was right: two hand-passed booleans
        # can disagree, and the same connector would then owe a domain to one
        # function and not to the other. `core.connector_family` is DERIVED from
        # the registry and shared with `apply_installation`'s caller, so there is
        # nothing to keep in step. `run_synthetic_delivery` below keeps the hard
        # requirement for every family, and correctly -- a synthetic delivery IS
        # the routing contract being exercised, so without one it has nothing to
        # send through.
        if owes_routing_contract(connector_name):
            raise ConnectorVerificationUnavailable(
                "active domain configuration is required"
            )
        domain_config_id = None
    else:
        domain_config_id = dc_row[0]

    # ------------------------------------------------------------------
    # A zero-check attempt still enters the idempotency/audit spine. Its mutation
    # records no verification row and advances no state, but a retry can replay
    # the same not_configured result without depending on current module wiring.
    checks_evaluated = len(checks)

    # ------------------------------------------------------------------
    # Check evaluation belongs inside the operation mutation. On an idempotent
    # replay execute_operation returns the stored result without invoking it.

    # ------------------------------------------------------------------
    # Build OperationSpec (deterministic idempotency -- no random id in payload).
    # ------------------------------------------------------------------
    spec = OperationSpec(
        command_type=ACTION_CONNECTOR_VERIFICATION_RAN,
        actor=actor,
        effective_org_id=None,
        resource_path=(
            "platform:connector-verification-runs",
            f"environment:{environment}",
            f"connector:{connector_name}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "connector-verification-v1",
            "catalog": "connector-verification-v1",
            "tool": "rest-v1",
        },
        # Deterministic over business inputs only (review H1). No row id here.
        request_payload={
            "environment": environment,
            "connector_name": connector_name,
            "synthetic_delivery": False,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"connector-verification:{environment}:{connector_name}:{idempotency_key}"
        ),
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        if not checks:
            from core.operations import _canonical_hash  # noqa: PLC0415

            result = {
                "environment": environment,
                "connector_name": connector_name,
                "outcome": OUTCOME_NOT_CONFIGURED,
                "evidence_class": EVIDENCE_CLASS_NO_CHECKS,
                "blocking_reason": None,
                "first_seen_at": None,
                "created_at": None,
                "checks_evaluated": 0,
            }
            return MutationResult(
                outcome="succeeded",
                before_hash=None,
                after_hash=_canonical_hash(result),
                result=result,
                outbox_payload={
                    "connector_name": connector_name,
                    "environment": environment,
                    "outcome": OUTCOME_NOT_CONFIGURED,
                },
            )

        all_pass = True
        fail_class = EVIDENCE_CLASS_ROUTING
        fail_reason: str | None = None
        # WHAT THE CHECKS ACTUALLY REPORTED (AI-206). The pass branch below used
        # to hardcode `EVIDENCE_CLASS_ROUTING`, discarding every class the checks
        # returned -- so a passing `auth_check` was written to the ledger as a
        # ROUTING verdict, describing a route that may not exist. Collected here
        # because the loop is the only place that sees them.
        seen_classes: set[str] = set()
        for check_fn in checks:
            try:
                result = check_fn()
                if not isinstance(result, tuple) or len(result) != 3:
                    raise ValueError("invalid check result")
                passed, ev_class, reason = result
                if not isinstance(passed, bool):
                    raise ValueError("invalid check outcome")
                if ev_class not in _ALLOWED_CHECK_EVIDENCE_CLASSES:
                    raise ValueError("invalid evidence class")
                if not isinstance(reason, str):
                    raise ValueError("invalid check reason")
            except Exception:  # noqa: BLE001 -- collapse untrusted check failures.
                passed = False
                ev_class = EVIDENCE_CLASS_ROUTING
                reason = "verification_failed"
            seen_classes.add(ev_class)
            if not passed and all_pass:
                all_pass = False
                fail_class = ev_class
                fail_reason = reason

        blocking_reason: str | None = None
        first_seen_at: Any = None
        if all_pass:
            outcome = OUTCOME_PASSED
            # Routing WINS when any check exercised a route, because a run that
            # proved a route is a routing verdict whatever else it also proved.
            # With no routing check at all the run is what it is: an auth_check.
            evidence_class = (
                EVIDENCE_CLASS_ROUTING
                if EVIDENCE_CLASS_ROUTING in seen_classes or not seen_classes
                else EVIDENCE_CLASS_AUTH
            )
        else:
            from core.connector_installation import sanitize_blocking_cause  # noqa: PLC0415

            evidence_class = fail_class
            blocking_reason = sanitize_blocking_cause(
                fail_reason, fallback="verification_failed"
            )
            if current_state in _DEGRADABLE:
                outcome = OUTCOME_DEGRADED
                with operation_conn.cursor() as cur:
                    cur.execute(
                        "SELECT first_seen_at FROM app.connector_verification_runs "
                        "WHERE environment = %s AND connector_name = %s "
                        "AND outcome = 'degraded' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (environment, connector_name),
                    )
                    prior_degradation = cur.fetchone()
                if prior_degradation is not None:
                    first_seen_at = prior_degradation[0]
                else:
                    import datetime as _dt  # noqa: PLC0415

                    first_seen_at = _dt.datetime.now(_dt.UTC)
            else:
                outcome = OUTCOME_FAILED

        run_id = f"cvr_{ULID()}"

        import datetime as _dt  # noqa: PLC0415

        sampled_at = _dt.datetime.now(_dt.UTC)
        ev_hash = _evidence_hash(evidence_class, sampled_at.isoformat())

        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.connector_verification_runs "
                "(id, installation_id, domain_config_id, environment, "
                "connector_name, outcome, evidence_class, evidence_hash, "
                "blocking_reason, first_seen_at, ttl_seconds, "
                "synthetic_delivery, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)",
                (
                    run_id,
                    installation_id,
                    domain_config_id,
                    environment,
                    connector_name,
                    outcome,
                    evidence_class,
                    ev_hash,
                    blocking_reason,
                    first_seen_at,
                    _DEFAULT_TTL_SECONDS,
                    operation_id,
                ),
            )
            if cur.rowcount != 1:
                raise ConnectorVerificationError(
                    "verification evidence insert did not create exactly one row"
                )

        from core.operations import _canonical_hash  # noqa: PLC0415

        result = {
            "run_id": run_id,
            "environment": environment,
            "connector_name": connector_name,
            "outcome": outcome,
            "evidence_class": evidence_class,
            "blocking_reason": blocking_reason,
            "first_seen_at": _safe_iso(first_seen_at),
            "created_at": sampled_at.isoformat(),
            "checks_evaluated": checks_evaluated,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "connector_name": connector_name,
                "environment": environment,
                "outcome": outcome,
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    outcome = data.get("outcome", OUTCOME_FAILED)
    evidence_class = data.get("evidence_class", EVIDENCE_CLASS_ROUTING)
    blocking_reason = data.get("blocking_reason")
    first_seen_at = data.get("first_seen_at")

    target_state: str | None = None
    if not op_result.replayed:
        if outcome == OUTCOME_PASSED and current_state in _ADVANCEABLE_TO_READY:
            target_state = "READY"
        elif outcome == OUTCOME_DEGRADED and current_state in _DEGRADABLE:
            target_state = "DEGRADED"

    # ------------------------------------------------------------------
    # F1: Drive the installation state transition via the LEGAL walk.
    #
    # The allowed path to READY from DOMAIN_PENDING is:
    #   DOMAIN_PENDING -> VERIFYING  (hop 1, key + ":to_verifying")
    #   VERIFYING      -> READY      (hop 2, key + ":to_ready")
    # From VERIFYING or DEGRADED a single hop suffices:
    #   VERIFYING  -> READY          (key + ":to_ready")
    #   DEGRADED   -> READY          (key + ":to_ready")
    # From READY no transition is needed (already there).
    #
    # Each hop is its own transition_state call with a derived idempotency key
    # so replays are clean. ConnectorInstallationConflict is only benign when
    # the current state already equals the hop target (idempotent / race);
    # otherwise it is a real error and must propagate.
    #
    # F4: After the walk, RE-READ the persisted installation state and build
    # the read-model from THAT actual value, never the intended target.
    # ------------------------------------------------------------------
    if target_state is not None:
        from core.connector_installation import (  # noqa: PLC0415
            ConnectorInstallationConflict,
            get_installation_state,
            transition_state,
        )

        def _hop(from_state: str, to_state: str, suffix: str) -> None:
            """Drive one hop; treat ConnectorInstallationConflict as benign only if
            the installation already reached the hop target (idempotent / race won)."""
            import datetime as _dt2  # noqa: PLC0415
            try:
                transition_state(
                    conn,
                    environment=environment,
                    connector_name=connector_name,
                    target_state=to_state,
                    # AN ACTOR CLASS, NEVER THE SUBJECT (AI-206). This passed
                    # `actor` -- the identity that called the endpoint -- into a
                    # parameter whose own validator refuses anything but
                    # `automated` / `platform_admin` / `platform_support`, and
                    # whose docstring says « never a subject identity ». So every
                    # transition raised `responsible_actor must be one of the
                    # supported actor classes`, and NO verification could ever
                    # advance an installation, for any connector, domain or not.
                    #
                    # The class answers « who owns the NEXT step »: nobody does
                    # when the run passed and the connector is READY, so
                    # `automated` carries it; a person owns a failure, so a hop
                    # that lands blocked is `platform_admin`.
                    responsible_actor=(
                        "platform_admin" if blocking_reason else "automated"
                    ),
                    blocking_cause=blocking_reason,
                    last_verified_at=(
                        _dt2.datetime.now(_dt2.UTC) if to_state == "READY" else None
                    ),
                    actor=actor,
                    idempotency_key=f"{idempotency_key}{suffix}",
                    host_context=host_context,
                    trace_id=trace_id,
                )
            except ConnectorInstallationConflict as exc:
                # Benign only if the installation already reached this hop target
                # (concurrent run won the race); otherwise it is a real error.
                current = get_installation_state(
                    conn, environment=environment, connector_name=connector_name,
                )
                actual = (current or {}).get("state", "")
                if actual != to_state:
                    raise ConnectorInstallationConflict(
                        f"state hop {from_state!r}->{to_state!r} conflict: "
                        f"actual state is {actual!r}, not {to_state!r}"
                    ) from exc

        if current_state == "DOMAIN_PENDING" and target_state == "READY":
            # Two-hop walk: DOMAIN_PENDING -> VERIFYING -> READY.
            _hop("DOMAIN_PENDING", "VERIFYING", ":to_verifying")
            _hop("VERIFYING", "READY", ":to_ready")
        elif target_state == "READY":
            # Single hop: VERIFYING -> READY or DEGRADED -> READY.
            _hop(current_state, "READY", ":to_ready")
        else:
            # Single hop to DEGRADED (READY -> DEGRADED).
            _hop(current_state, target_state, f":state:{target_state}")

    # F4: Re-read the actual persisted installation state; never report the intended
    # target as the final state. Fall back to current_state only if the row is gone.
    from core.connector_installation import (  # noqa: PLC0415
        get_installation_state,
    )
    actual_inst = get_installation_state(
        conn, environment=environment, connector_name=connector_name,
    )
    actual_state = (actual_inst or {}).get("state", current_state)

    rm = _safe_run_read_model(
        connector_name=connector_name,
        environment=environment,
        state=actual_state,
        outcome=data.get("outcome", outcome),
        evidence_class=data.get("evidence_class", evidence_class),
        first_seen_at=data.get("first_seen_at", first_seen_at),
        created_at=data.get("created_at"),
        blocking_reason=data.get("blocking_reason", blocking_reason),
        synthetic_delivery=False,
        ttl_seconds=_DEFAULT_TTL_SECONDS,
    )
    rm["checks_evaluated"] = data.get("checks_evaluated", checks_evaluated)
    return rm


# ---------------------------------------------------------------------------
# run_synthetic_delivery -- auth-only test delivery (AC3, Task 2).
# ---------------------------------------------------------------------------


def run_synthetic_delivery(
    conn,
    *,
    environment: str,
    connector_name: str,
    delivery_runner: Callable[[], tuple[bool, str]],
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Exercise the auth-only path and record a synthetic run (AC3, Task 2).

    Parameters
    ----------
    delivery_runner:
        A zero-argument callable that exercises the receipt-adapter
        authentication seam and returns ``(auth_passed: bool, reason: str)``.

        The runner is OPAQUE to this module (AD-2): it may call any
        adapter seam the caller chooses, but this module never names or
        imports it. The caller is responsible for ensuring the runner
        DOES NOT write a quarantine object, raw import, mapping row, or
        publication (AC3: synthetic = auth-only).

    Returns the safe read-model (secret-free). The run is explicitly
    flagged ``synthetic_delivery = TRUE`` in the DB and in the response.
    """
    # ------------------------------------------------------------------
    # Validation.
    # ------------------------------------------------------------------
    if not isinstance(environment, str) or not environment.strip():
        raise ConnectorVerificationError("environment is required")
    if not isinstance(connector_name, str) or not connector_name.strip():
        raise ConnectorVerificationError("connector_name is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ConnectorVerificationError("actor is required")
    # F8: validate idempotency_key (mirrors run_verification guard).
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ConnectorVerificationError("idempotency_key is required")
    if len(idempotency_key.strip()) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ConnectorVerificationError("idempotency_key is too long")

    environment = environment.strip()
    connector_name = connector_name.strip()
    actor = actor.strip()
    idempotency_key = idempotency_key.strip()

    # ------------------------------------------------------------------
    # Load the installation row.
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state "
            "FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s "
            "FOR UPDATE",
            (environment, connector_name),
        )
        inst_row = cur.fetchone()

    if inst_row is None:
        raise ConnectorVerificationUnavailable(
            "connector installation row not found -- apply installation first"
        )
    installation_id, current_state = inst_row

    # ------------------------------------------------------------------
    # Load and lock the active domain config.
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.connector_domain_configs "
            "WHERE environment = %s AND connector_name = %s "
            "AND superseded_by IS NULL "
            "FOR SHARE",
            (environment, connector_name),
        )
        dc_row = cur.fetchone()

    if dc_row is None:
        raise ConnectorVerificationUnavailable(
            "active domain configuration is required"
        )
    domain_config_id = dc_row[0]

    # ------------------------------------------------------------------
    # The delivery runner is invoked inside the operation mutation so replays do
    # not repeat an external authentication attempt.

    # ------------------------------------------------------------------
    # Build OperationSpec (deterministic idempotency -- no random id in payload).
    # ------------------------------------------------------------------
    spec = OperationSpec(
        command_type=ACTION_CONNECTOR_VERIFICATION_RAN,
        actor=actor,
        effective_org_id=None,
        resource_path=(
            "platform:connector-verification-runs",
            f"environment:{environment}",
            f"connector:{connector_name}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "connector-verification-v1",
            "catalog": "connector-verification-v1",
            "tool": "rest-v1",
        },
        # Deterministic over business inputs only (review H1). No row id here.
        request_payload={
            "environment": environment,
            "connector_name": connector_name,
            "synthetic_delivery": True,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"connector-verification:{environment}:{connector_name}:{idempotency_key}:synthetic"
        ),
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        try:
            runner_result = delivery_runner()
            if not isinstance(runner_result, tuple) or len(runner_result) != 2:
                raise ValueError("invalid delivery runner result")
            auth_passed, auth_reason = runner_result
            if not isinstance(auth_passed, bool) or not isinstance(auth_reason, str):
                raise ValueError("invalid delivery runner result")
        except Exception:  # noqa: BLE001 -- collapse untrusted runner failures.
            auth_passed = False
            auth_reason = "synthetic_verification_failed"

        outcome = OUTCOME_PASSED if auth_passed else OUTCOME_FAILED
        evidence_class = EVIDENCE_CLASS_SYNTHETIC
        blocking_reason: str | None = None
        if not auth_passed:
            from core.connector_installation import sanitize_blocking_cause  # noqa: PLC0415

            blocking_reason = sanitize_blocking_cause(
                auth_reason, fallback="synthetic_verification_failed"
            )

        run_id = f"cvr_{ULID()}"

        import datetime as _dt  # noqa: PLC0415

        sampled_at = _dt.datetime.now(_dt.UTC)
        ev_hash = _evidence_hash(evidence_class, sampled_at.isoformat())

        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.connector_verification_runs "
                "(id, installation_id, domain_config_id, environment, "
                "connector_name, outcome, evidence_class, evidence_hash, "
                "blocking_reason, first_seen_at, ttl_seconds, "
                "synthetic_delivery, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, TRUE, %s)",
                (
                    run_id,
                    installation_id,
                    domain_config_id,
                    environment,
                    connector_name,
                    outcome,
                    evidence_class,
                    ev_hash,
                    blocking_reason,
                    _DEFAULT_TTL_SECONDS,
                    operation_id,
                ),
            )
            if cur.rowcount != 1:
                raise ConnectorVerificationError(
                    "synthetic evidence insert did not create exactly one row"
                )

        from core.operations import _canonical_hash  # noqa: PLC0415

        result = {
            "run_id": run_id,
            "environment": environment,
            "connector_name": connector_name,
            "outcome": outcome,
            "evidence_class": evidence_class,
            "blocking_reason": blocking_reason,
            "created_at": sampled_at.isoformat(),
            "synthetic_delivery": True,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "connector_name": connector_name,
                "environment": environment,
                "outcome": outcome,
                "synthetic_delivery": True,
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    returned_outcome = data.get("outcome", OUTCOME_FAILED)
    returned_reason = data.get("blocking_reason")
    if returned_outcome != OUTCOME_PASSED and returned_reason is None:
        returned_reason = "synthetic_verification_failed"
    return _safe_run_read_model(
        connector_name=connector_name,
        environment=environment,
        state=current_state,
        outcome=returned_outcome,
        evidence_class=data.get("evidence_class", EVIDENCE_CLASS_SYNTHETIC),
        first_seen_at=None,
        created_at=data.get("created_at"),
        blocking_reason=returned_reason,
        synthetic_delivery=True,
        ttl_seconds=_DEFAULT_TTL_SECONDS,
    )
