"""toorow -- the ONE best-effort seam that Epic 36 stage points call to record the
first-value funnel (E36-FR09), without ever breaking the domain operation (AD-32).

WHY THIS MODULE EXISTS
======================
``core.first_value_funnel`` is the privacy-safe analytics sink (Story 36.19): closed
enums + a pseudonymous keyed-hash reference + a coarse bucket, nothing else. But that
module is inert unless the stage points actually CALL it. Rather than sprinkle the
enum-building + pseudonymisation + try/except at every stage point (which would
duplicate the seam and risk a telemetry failure breaking a real operation), every
stage point calls exactly ONE function here:

    record_stage(conn, org_id=..., project_id=..., subject=..., stage=..., outcome=...)

This wrapper guarantees the two Epic-36 invariants at the call site:

  * NON-FATAL / BEST-EFFORT: the funnel is telemetry, never a gate. EVERY path here is
    wrapped so a missing pepper, an absent ``app.first_value_events`` table, an
    off-allowlist value, or ANY DB error degrades to a silent no-op and NEVER raises
    into the caller. A stage point can add its one line with zero risk to the domain op.

  * ALLOWLIST + PSEUDONYMOUS ONLY (AD-32): the caller passes the RAW org/project/subject
    only so THIS module can build the one-way HMAC ``journey_reference`` /
    ``cohort_reference`` -- the raw values never leave this frame and never reach the
    analytics sink. The caller passes only closed enums (stage/outcome/recovery_kind/
    abandon_reason) and an optional elapsed number that we coarse-bucket. No content,
    raw error, email, prompt or free text can travel through this narrow signature.

E36-NFR05: ONE seam, no duplication -- all stage points share this single entry point.

It is intentionally gated on ``TOOROW_FUNNEL_PEPPER``: when the pepper is absent (e.g.
in the MagicMock-based unit tests of the domain modules) the seam no-ops before it ever
touches the connection, so instrumenting an existing domain function does not perturb
its existing tests.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _funnel_enabled() -> bool:
    """The seam is live only when a valid pepper is configured.

    The pepper is required to build the pseudonymous reference; its absence means we are
    either not in a funnel-recording environment or in a unit test that mocks the DB, so
    we no-op before touching the connection.
    """
    return len(os.environ.get("TOOROW_FUNNEL_PEPPER", "")) >= 32


def funnel_enabled() -> bool:
    """Public predicate: True only when a valid funnel pepper is configured.

    Lets a stage point skip any funnel-only preparatory work (e.g. a context lookup)
    when the seam would no-op anyway, so instrumenting a pure read adds no cost -- and
    a MagicMock-based unit test (no pepper) exercises none of it.
    """
    return _funnel_enabled()


def _policy_version() -> str:
    return os.environ.get("TOOROW_POLICY_VERSION", "v1")


def record_stage(
    conn,
    *,
    org_id,
    project_id,
    subject,
    stage: str,
    outcome: str,
    elapsed_seconds=None,
    recovery_kind: str | None = None,
    abandon_reason: str | None = None,
    bind_project: bool = False,
) -> None:
    """Best-effort: record ONE first-value funnel stage. NEVER raises.

    Builds the pseudonymous journey/cohort references from the raw identifiers (which
    stay in this frame), coarse-buckets ``elapsed_seconds`` if given, and appends one
    allowlisted event through ``first_value_funnel.record_funnel_stage``. Any failure --
    missing pepper, missing table, off-allowlist enum, DB error -- is swallowed.

    When ``bind_project`` is True (call ONCE per journey, at acceptance) it also bridges
    the pseudonymous journey hash to the project so the tenant-facing read can show an
    authorized owner their own journeys. The bridge is best-effort too.
    """
    if not _funnel_enabled():
        return
    if not (org_id and project_id and subject):
        # Without a full (org, project, subject) we cannot build a stable pseudonymous
        # reference; skip rather than contort. (Documented in the wiring notes.)
        return
    try:
        from core import first_value_funnel as fvf  # noqa: PLC0415

        journey_ref = fvf.journey_reference(
            org_id=str(org_id), project_id=str(project_id), subject=str(subject)
        )
        cohort_ref = fvf.cohort_reference(org_id=str(org_id))
        duration_bucket = (
            fvf.bucket_duration(elapsed_seconds) if elapsed_seconds is not None else None
        )
        fvf.record_funnel_stage(
            conn,
            journey_ref=journey_ref,
            cohort_ref=cohort_ref,
            stage=stage,
            outcome=outcome,
            policy_version=_policy_version(),
            duration_bucket=duration_bucket,
            recovery_kind=recovery_kind,
            abandon_reason=abandon_reason,
        )
        if bind_project:
            try:
                fvf.bind_journey_to_project(
                    conn, journey_ref=journey_ref, project_id=str(project_id)
                )
            except Exception:  # noqa: BLE001 -- the bridge is best-effort telemetry.
                logger.debug("first_value: bind_journey_to_project skipped", exc_info=False)
    except Exception:  # noqa: BLE001 -- telemetry MUST NEVER break the domain operation.
        # Deliberately swallow: no raw value, no re-raise. The funnel is a sink, not a gate.
        logger.debug("first_value: record_stage skipped (best-effort)", exc_info=False)
