"""Story 36.9 -- execute and publish the recent-first candidate SAFELY.

A new operator has a valid SAVED first-report draft (Story 36.8: an immutable plan
version + immutable mapping version + a bounded recent interval, linked from
``app.first_report_drafts``). This module turns that saved draft into ONE
trustworthy recent-first publication WITHOUT risking the current data and WITHOUT a
second engine:

  1. ``execute_recent_first`` creates EXACTLY ONE idempotent candidate from the
     EXACT approved ``plan_version_id`` / ``mapping_version_id`` + recent interval,
     reusing the Epic 12 seams verbatim:
       * ``datastream_publication.create_execution`` (idempotent on the
         idempotency key; a duplicate execute returns the ORIGINAL candidate, never
         a second one -- INVARIANT: one candidate),
       * the existing ``queue`` machinery for the recent-window pull (no new
         backfill engine -- E36-NFR05),
       * ``verification.run_post_pull_verification`` evidence retained BEFORE read,
       * ``datastream_publication.advance_state`` for created->loading->validating,
       * ``datastream_publication.run_dq_gates`` for the fail-closed gates,
       * ``datastream_publication.commit_publication`` for the ATOMIC 4-write
         publish (pointer + publication_log + outbox + audit) -- called ONLY on a
         ready + valid candidate.

  2. A candidate that is EMPTY / PARTIAL / INVALID / FAILED / CANCELLED is marked
     terminal and NEVER touches the live pointer: ``commit_publication`` is not
     called at all, so ``app.datastreams.current_published_execution_id`` is
     untouched and the last-known-good report stays visible (INVARIANT).

  3. Recent and historical coverage are tracked SEPARATELY in
     ``app.datastream_coverage`` (migration 072): the recent-first candidate updates
     ONLY the ``recent`` marker; a later background-history failure updates ONLY the
     ``historical`` marker and can NEVER flip the recent result to unavailable
     (INVARIANT). Chunked backfill stays parked by Epic 32 -- ``note_history_progress``
     is a thin marker updater, not a backfill engine.

INVARIANTS (held HARD, mirrored in the tests):
  * ONE idempotent candidate per saved draft + idempotency key.
  * EXACT approved plan/mapping versions drive the candidate (from the saved draft).
  * A candidate failure NEVER replaces the publication (last-known-good preserved).
  * Publication is ATOMIC (commit_publication's single transaction).
  * Recent vs historical coverage are SEPARATE, independently-updatable markers.
  * Fail-closed access is enforced by the REST caller
    (``project_access.resolve_strict_resource_access``, edit+); this domain never
    opens a resource by itself.

Source-agnostic (AD-2): no provider name, no provider field name, no import from
``server/modules/*``. French user-facing messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ulid import ULID

from core.datastream_publication import (
    STATE_CREATED,
    STATE_FAILED,
    STATE_LOADING,
    STATE_READY,
    STATE_VALIDATING,
    TERMINAL_STATES,
    ConcurrentExecutionActive,
    DQGateFailed,
    IdempotencyConflict,
    InvalidReference,
    PublicationError,
    advance_state,
    commit_publication,
    create_execution,
    run_dq_gates,
)

COVERAGE_RECENT = "recent"
COVERAGE_HISTORICAL = "historical"

# Coverage marker states (mirror migration 072's CHECK constraint).
COV_PENDING = "pending"
COV_LOADING = "loading"
COV_COVERED = "covered"
COV_DEGRADED = "degraded"
COV_FAILED = "failed"


class RecentFirstValidationError(ValueError):
    """Raised before SQL when the recent-first inputs are unsafe or malformed."""


class RecentFirstConflict(RuntimeError):
    """The idempotency key was reused with a different request (409)."""


class RecentFirstUnavailable(RuntimeError):
    """The saved draft, its versions, or its scope cannot be resolved safely."""


# ---------------------------------------------------------------------------
# Result value objects. Model-facing -- counts/hashes/ids only, never raw rows.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageMarker:
    coverage_kind: str
    state: str
    interval_start: str | None
    interval_end_exclusive: str | None
    execution_id: str | None
    pull_id: str | None
    row_count: int | None
    content_hash: str | None
    covered_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "coverage_kind": self.coverage_kind,
            "state": self.state,
            "interval_start": self.interval_start,
            "interval_end_exclusive": self.interval_end_exclusive,
            "execution_id": self.execution_id,
            "pull_id": self.pull_id,
            "row_count": self.row_count,
            "content_hash": self.content_hash,
            "covered_at": self.covered_at,
        }


@dataclass(frozen=True)
class RecentFirstResult:
    draft_id: str | None
    datastream_id: str
    project_id: str
    execution_id: str
    # "published" | "blocked" | "failed" -- honest terminal disposition. "blocked"
    # means a candidate ran but did NOT publish (empty/partial/invalid/DQ-blocked);
    # the live pointer is untouched and last-known-good stays visible.
    disposition: str
    published: bool
    replayed: bool
    # The live publication pointer AFTER this call -- unchanged on any non-publish.
    current_published_execution_id: str | None
    publication_log_id: str | None
    dq_issues: list[dict[str, Any]] = field(default_factory=list)
    recent_coverage: dict[str, Any] | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "datastream_id": self.datastream_id,
            "project_id": self.project_id,
            "execution_id": self.execution_id,
            "disposition": self.disposition,
            "published": self.published,
            "replayed": self.replayed,
            "current_published_execution_id": self.current_published_execution_id,
            "publication_log_id": self.publication_log_id,
            "dq_issues": self.dq_issues,
            "recent_coverage": self.recent_coverage,
            "reason": self.reason,
        }


def _mint_coverage_id() -> str:
    return f"dcov_{ULID()}"


# First-value funnel disposition -> allowlisted outcome (E36-FR09).
_RECENT_PULL_OUTCOME = {
    "published": "succeeded",
    "blocked": "blocked",
    "failed": "failed",
}


def _record_recent_pull(conn, *, project_id: str, actor: str, disposition: str) -> None:
    """Best-effort: record the recent_pull funnel stage from the terminal disposition.

    Gated on the funnel pepper so a MagicMock-based unit test (no pepper) exercises no
    extra query, and the org lookup / record are both swallowed on any failure -- the
    recent-first publication is NEVER affected. E36-FR09 / AD-32.
    """
    from core.first_value_instrumentation import funnel_enabled, record_stage  # noqa: PLC0415

    if not funnel_enabled():
        return
    outcome = _RECENT_PULL_OUTCOME.get(disposition)
    if outcome is None:
        return
    org_id = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
            org_id = row[0] if row else None
    except Exception:  # noqa: BLE001 -- telemetry context read must never break the pull.
        org_id = None
    record_stage(
        conn,
        org_id=org_id,
        project_id=project_id,
        subject=project_id,
        stage="recent_pull",
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Saved-draft + provenance loading.
# ---------------------------------------------------------------------------
def _load_saved_draft(
    conn,
    *,
    project_id: str,
    draft_id: str | None,
    datastream_id: str | None,
) -> dict[str, Any]:
    """Resolve the EXACT saved plan/mapping versions + recent interval to execute.

    Either a ``draft_id`` (preferred) or a ``datastream_id`` (uses that
    datastream's latest saved draft) identifies the draft. The plan version is
    validated as executable; a non-executable / superseded draft is a NON-silent
    refusal. Returns the immutable provenance the candidate must carry.
    """
    if bool(draft_id) == bool(datastream_id):
        raise RecentFirstValidationError(
            "exactly one of draft_id / datastream_id is required"
        )

    with conn.cursor() as cur:
        if draft_id:
            cur.execute(
                """
                SELECT id, datastream_id, plan_version_id, mapping_version_id,
                       recommendation, state
                FROM app.first_report_drafts
                WHERE id = %s AND project_id = %s
                """,
                (draft_id, project_id),
            )
        else:
            cur.execute(
                """
                SELECT id, datastream_id, plan_version_id, mapping_version_id,
                       recommendation, state
                FROM app.first_report_drafts
                WHERE datastream_id = %s AND project_id = %s
                  AND state IN ('saved', 'previewed')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (datastream_id, project_id),
            )
        row = cur.fetchone()
        if row is None:
            raise RecentFirstUnavailable("saved first-report draft unavailable")
        (
            resolved_draft_id,
            resolved_datastream_id,
            plan_version_id,
            mapping_version_id,
            recommendation,
            draft_state,
        ) = row
        if draft_state == "superseded":
            raise RecentFirstUnavailable("saved draft has been superseded")

        # The saved plan version is the executable admission ticket.
        cur.execute(
            """
            SELECT executable
            FROM app.datastream_plan_versions
            WHERE id = %s AND datastream_id = %s AND project_id = %s
            """,
            (plan_version_id, resolved_datastream_id, project_id),
        )
        plan_row = cur.fetchone()

        # Governed project-scoped projection thresholds (no platform-wide hardcode).
        cur.execute(
            """
            SELECT max_projection_grain_cardinality, max_projection_scan_bytes
            FROM app.project_preferences
            WHERE project_id = %s
            """,
            (project_id,),
        )
        pref_row = cur.fetchone()

    if plan_row is None:
        raise RecentFirstUnavailable("saved draft versions unavailable")

    recommendation = recommendation if isinstance(recommendation, dict) else {}
    interval = recommendation.get("interval") or {}

    # The compiled 12.4 projection plan is derived from the saved IMMUTABLE mapping
    # version via the Epic 12 compile seam (no second engine, no stored duplicate):
    # this guarantees the candidate runs the EXACT approved plan/mapping versions.
    from core.datastream_field_mapping import (  # noqa: PLC0415
        DatastreamMappingNotFound,
        get_mapping_version,
    )
    from core.datastream_projection import (  # noqa: PLC0415
        ProjectionCompileError,
        compile_projection,
    )

    try:
        mapping_version = get_mapping_version(
            resolved_datastream_id, project_id, mapping_version_id, conn
        )
    except DatastreamMappingNotFound as exc:
        raise RecentFirstUnavailable("saved draft versions unavailable") from exc
    if mapping_version.get("plan_version_id") != plan_version_id:
        raise RecentFirstUnavailable("draft versions are inconsistent")

    preferences: dict[str, Any] = {}
    if pref_row is not None:
        preferences = {
            "max_projection_grain_cardinality": pref_row[0],
            "max_projection_scan_bytes": pref_row[1],
        }

    try:
        projection_plan = compile_projection(
            mapping_version, project_preferences=preferences
        )
    except ProjectionCompileError as exc:
        raise RecentFirstUnavailable("projection compilation unavailable") from exc

    if not bool(plan_row[0]) or not projection_plan.get("executable", False):
        raise RecentFirstValidationError(
            "le brouillon enregistré n'est pas exécutable"
        )

    return {
        "draft_id": resolved_draft_id,
        "datastream_id": resolved_datastream_id,
        "plan_version_id": plan_version_id,
        "mapping_version_id": mapping_version_id,
        "projection_plan": projection_plan,
        "interval": interval,
    }


# ---------------------------------------------------------------------------
# Coverage markers (recent vs historical -- SEPARATE rows, migration 072).
# ---------------------------------------------------------------------------
def _upsert_coverage(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    coverage_kind: str,
    state: str,
    actor: str,
    interval: dict[str, Any] | None = None,
    execution_id: str | None = None,
    pull_id: str | None = None,
    row_count: int | None = None,
    content_hash: str | None = None,
    covered: bool = False,
) -> None:
    """Insert or update ONE coverage marker for exactly one coverage_kind.

    The UNIQUE(datastream_id, project_id, coverage_kind) constraint guarantees the
    recent and historical facts are DISTINCT rows: updating one never touches the
    other. ``covered`` sets covered_at when the window is fully covered.
    """
    interval = interval or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_coverage
                (id, datastream_id, project_id, coverage_kind, state,
                 interval_start, interval_end_exclusive, execution_id, pull_id,
                 row_count, content_hash, covered_at, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s THEN NOW() ELSE NULL END, %s)
            ON CONFLICT (datastream_id, project_id, coverage_kind)
            DO UPDATE SET
                state = EXCLUDED.state,
                interval_start = COALESCE(EXCLUDED.interval_start,
                                          app.datastream_coverage.interval_start),
                interval_end_exclusive = COALESCE(EXCLUDED.interval_end_exclusive,
                                          app.datastream_coverage.interval_end_exclusive),
                execution_id = COALESCE(EXCLUDED.execution_id,
                                        app.datastream_coverage.execution_id),
                pull_id = COALESCE(EXCLUDED.pull_id, app.datastream_coverage.pull_id),
                row_count = COALESCE(EXCLUDED.row_count,
                                     app.datastream_coverage.row_count),
                content_hash = COALESCE(EXCLUDED.content_hash,
                                        app.datastream_coverage.content_hash),
                covered_at = COALESCE(EXCLUDED.covered_at,
                                      app.datastream_coverage.covered_at),
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            """,
            (
                _mint_coverage_id(),
                datastream_id,
                project_id,
                coverage_kind,
                state,
                interval.get("start"),
                interval.get("end_exclusive"),
                execution_id,
                pull_id,
                row_count,
                content_hash,
                bool(covered),
                actor,
            ),
        )


def _read_coverage(
    conn, *, datastream_id: str, project_id: str, coverage_kind: str
) -> CoverageMarker | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT coverage_kind, state, interval_start, interval_end_exclusive,
                   execution_id, pull_id, row_count, content_hash, covered_at
            FROM app.datastream_coverage
            WHERE datastream_id = %s AND project_id = %s AND coverage_kind = %s
            """,
            (datastream_id, project_id, coverage_kind),
        )
        row = cur.fetchone()
    if row is None:
        return None

    def _iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    return CoverageMarker(
        coverage_kind=row[0],
        state=row[1],
        interval_start=_iso(row[2]),
        interval_end_exclusive=_iso(row[3]),
        execution_id=row[4],
        pull_id=row[5],
        row_count=row[6],
        content_hash=row[7],
        covered_at=_iso(row[8]),
    )


def _current_pointer(conn, *, datastream_id: str, project_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams "
            "WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------
def execute_recent_first(
    conn,
    *,
    project_id: str,
    actor: str,
    idempotency_key: str,
    draft_id: str | None = None,
    datastream_id: str | None = None,
    row_count: int | None = None,
    content_hash: str | None = None,
    verification_verdict: str | None = None,
    pull_id: str | None = None,
    validated_content_hash: str | None = None,
    force_empty_publish: bool = False,
    approved: bool = False,
) -> RecentFirstResult:
    """Create ONE idempotent recent-first candidate and publish it ONLY if valid.

    The caller (REST) has already resolved fail-closed access (edit+) and owns the
    transaction boundary at the HTTP layer. This function drives the candidate
    through the Epic 12 state machine and commits the atomic publication itself
    (``commit_publication`` owns its own transaction).

    ``row_count`` / ``content_hash`` / ``verification_verdict`` / ``pull_id`` come
    from the bounded recent-window pull the caller already ran through the existing
    queue + verification machinery (E36-NFR05: no new engine). They are the
    validated candidate evidence retained BEFORE read.

    Disposition:
      * ``published`` -- ready + valid candidate; ``commit_publication`` swapped the
        pointer atomically; the ``recent`` coverage marker is set to ``covered``.
      * ``blocked``   -- empty / partial / invalid / DQ-blocked candidate; the
        candidate is marked ``failed`` (terminal), the live pointer is UNTOUCHED,
        last-known-good stays visible, and the ``recent`` marker records ``failed``
        (or ``degraded``). ``commit_publication`` is NEVER called.

    Idempotency: a duplicate execute with the SAME key returns the ORIGINAL
    candidate (via ``create_execution``); it never mints a second candidate.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise RecentFirstValidationError("Idempotency-Key is required")

    draft = _load_saved_draft(
        conn, project_id=project_id, draft_id=draft_id, datastream_id=datastream_id
    )
    resolved_datastream_id = draft["datastream_id"]
    plan_version_id = draft["plan_version_id"]
    mapping_version_id = draft["mapping_version_id"]
    interval = draft["interval"]

    prior_pointer = _current_pointer(
        conn, datastream_id=resolved_datastream_id, project_id=project_id
    )

    # 1) ONE idempotent candidate from the EXACT approved plan/mapping versions.
    #    A duplicate execute returns the ORIGINAL candidate (no second candidate).
    try:
        execution = create_execution(
            resolved_datastream_id,
            project_id,
            plan_version_id,
            mapping_version_id,
            draft["projection_plan"],
            actor,
            idempotency_key,
            conn,
        )
    except IdempotencyConflict as exc:
        raise RecentFirstConflict("idempotency_conflict") from exc
    except ConcurrentExecutionActive as exc:
        # Another non-terminal candidate already exists for this datastream. That
        # is the single-candidate guard doing its job -- surface as a conflict.
        raise RecentFirstConflict("concurrent_execution_active") from exc
    except InvalidReference as exc:
        raise RecentFirstUnavailable("draft references are invalid") from exc

    execution_id = execution["id"]

    # A replayed/terminal candidate (idempotent return of an already-resolved
    # execution) must NOT be re-driven or re-published: return its resolved shape.
    if execution["state"] in TERMINAL_STATES:
        pointer = _current_pointer(
            conn, datastream_id=resolved_datastream_id, project_id=project_id
        )
        published = execution["state"] == "published" and pointer == execution_id
        recent = _read_coverage(
            conn,
            datastream_id=resolved_datastream_id,
            project_id=project_id,
            coverage_kind=COVERAGE_RECENT,
        )
        _record_recent_pull(
            conn,
            project_id=project_id,
            actor=actor,
            disposition="published" if published else "blocked",
        )
        return RecentFirstResult(
            draft_id=draft["draft_id"],
            datastream_id=resolved_datastream_id,
            project_id=project_id,
            execution_id=execution_id,
            disposition="published" if published else "blocked",
            published=published,
            replayed=True,
            current_published_execution_id=pointer,
            publication_log_id=None,
            recent_coverage=recent.as_dict() if recent else None,
            reason="already_resolved",
        )

    # Mark the recent coverage as loading (the historical marker is never touched
    # here -- separate row).
    _upsert_coverage(
        conn,
        datastream_id=resolved_datastream_id,
        project_id=project_id,
        coverage_kind=COVERAGE_RECENT,
        state=COV_LOADING,
        actor=actor,
        interval=interval,
        execution_id=execution_id,
        pull_id=pull_id,
    )

    # 2) created -> loading. Retain pull ID + provenance + verification evidence
    #    (row_count/content_hash) on the execution row BEFORE any read/publish.
    if execution["state"] == STATE_CREATED:
        advance_state(
            execution_id,
            STATE_CREATED,
            STATE_LOADING,
            actor,
            conn,
            project_id=project_id,
        )

    # 3) loading -> validating, carrying the retained verification evidence.
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

    # 3b) Empty / partial / invalid verification NEVER publishes. Fail closed BEFORE
    #     the DQ gates so an unverified/partial candidate can never reach the pointer.
    bad_verdict = verification_verdict in {"empty", "partial", "invalid", "failed"}
    if bad_verdict or row_count is None or (row_count == 0 and not force_empty_publish):
        return _block_candidate(
            conn,
            execution_id=execution_id,
            draft=draft,
            project_id=project_id,
            actor=actor,
            prior_pointer=prior_pointer,
            interval=interval,
            pull_id=pull_id,
            row_count=row_count,
            content_hash=content_hash,
            reason=f"verification_{verification_verdict or 'unavailable'}",
            dq_issues=[],
        )

    # 4) DQ gates (fail closed). Only an all-pass candidate advances to ready.
    dq_issues = run_dq_gates(
        execution_id,
        project_id,
        conn,
        approved=approved,
        force_empty_publish=force_empty_publish,
        validated_content_hash=validated_content_hash,
    )
    if dq_issues:
        return _block_candidate(
            conn,
            execution_id=execution_id,
            draft=draft,
            project_id=project_id,
            actor=actor,
            prior_pointer=prior_pointer,
            interval=interval,
            pull_id=pull_id,
            row_count=row_count,
            content_hash=content_hash,
            reason="dq_gate_failed",
            dq_issues=dq_issues,
        )

    # 5) validating -> ready. The candidate is proven valid; only now may it publish.
    advance_state(
        execution_id,
        STATE_VALIDATING,
        STATE_READY,
        actor,
        conn,
        project_id=project_id,
    )

    # 6) ATOMIC publication -- pointer + publication_log + outbox + audit in ONE
    #    transaction (commit_publication owns and commits its own transaction; we do
    #    NOT double-write the outbox here). On ANY error it rolls the whole group
    #    back, marks the execution failed out-of-band, and leaves the prior pointer
    #    intact -- so a failed publish still preserves last-known-good.
    try:
        published = commit_publication(execution_id, project_id, actor, conn)
    except (PublicationError, DQGateFailed) as exc:
        # The candidate failed to publish: the pointer is untouched (rolled back).
        _upsert_coverage(
            conn,
            datastream_id=resolved_datastream_id,
            project_id=project_id,
            coverage_kind=COVERAGE_RECENT,
            state=COV_FAILED,
            actor=actor,
            interval=interval,
            execution_id=execution_id,
            pull_id=pull_id,
        )
        conn.commit()
        recent = _read_coverage(
            conn,
            datastream_id=resolved_datastream_id,
            project_id=project_id,
            coverage_kind=COVERAGE_RECENT,
        )
        _record_recent_pull(
            conn, project_id=project_id, actor=actor, disposition="failed"
        )
        return RecentFirstResult(
            draft_id=draft["draft_id"],
            datastream_id=resolved_datastream_id,
            project_id=project_id,
            execution_id=execution_id,
            disposition="failed",
            published=False,
            replayed=False,
            current_published_execution_id=prior_pointer,
            publication_log_id=None,
            reason=getattr(exc, "code", "publication_error"),
            recent_coverage=recent.as_dict() if recent else None,
        )

    # 7) Published: mark the RECENT coverage covered (historical marker untouched).
    _upsert_coverage(
        conn,
        datastream_id=resolved_datastream_id,
        project_id=project_id,
        coverage_kind=COVERAGE_RECENT,
        state=COV_COVERED,
        actor=actor,
        interval=interval,
        execution_id=execution_id,
        pull_id=pull_id,
        row_count=row_count,
        content_hash=content_hash or published["execution"].get("content_hash"),
        covered=True,
    )
    conn.commit()

    recent = _read_coverage(
        conn,
        datastream_id=resolved_datastream_id,
        project_id=project_id,
        coverage_kind=COVERAGE_RECENT,
    )
    _record_recent_pull(
        conn, project_id=project_id, actor=actor, disposition="published"
    )
    return RecentFirstResult(
        draft_id=draft["draft_id"],
        datastream_id=resolved_datastream_id,
        project_id=project_id,
        execution_id=execution_id,
        disposition="published",
        published=True,
        replayed=False,
        current_published_execution_id=execution_id,
        publication_log_id=published.get("publication_log_id"),
        recent_coverage=recent.as_dict() if recent else None,
    )


def _block_candidate(
    conn,
    *,
    execution_id: str,
    draft: dict[str, Any],
    project_id: str,
    actor: str,
    prior_pointer: str | None,
    interval: dict[str, Any],
    pull_id: str | None,
    row_count: int | None,
    content_hash: str | None,
    reason: str,
    dq_issues: list[dict[str, Any]],
) -> RecentFirstResult:
    """Mark the candidate terminal WITHOUT touching the live pointer.

    INVARIANT: an empty / partial / invalid / DQ-blocked candidate never calls
    ``commit_publication`` and never mutates ``current_published_execution_id`` --
    the last-known-good publication stays visible. The ``recent`` coverage marker
    records the honest state; the ``historical`` marker is never touched here.
    """
    datastream_id = draft["datastream_id"]
    # Fail the candidate on THIS connection (it is non-terminal and not publishing,
    # so the advance is safe and not on a rolled-back connection).
    advance_state(
        execution_id,
        None,
        STATE_FAILED,
        actor,
        conn,
        project_id=project_id,
        error_code=reason,
        error_detail="recent-first candidate blocked; live publication preserved",
    )
    _upsert_coverage(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        coverage_kind=COVERAGE_RECENT,
        state=COV_FAILED,
        actor=actor,
        interval=interval,
        execution_id=execution_id,
        pull_id=pull_id,
        row_count=row_count,
        content_hash=content_hash,
    )
    conn.commit()

    recent = _read_coverage(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        coverage_kind=COVERAGE_RECENT,
    )
    _record_recent_pull(
        conn, project_id=project_id, actor=actor, disposition="blocked"
    )
    return RecentFirstResult(
        draft_id=draft["draft_id"],
        datastream_id=datastream_id,
        project_id=project_id,
        execution_id=execution_id,
        disposition="blocked",
        published=False,
        replayed=False,
        current_published_execution_id=prior_pointer,
        publication_log_id=None,
        dq_issues=dq_issues,
        reason=reason,
        recent_coverage=recent.as_dict() if recent else None,
    )


def note_history_progress(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    actor: str,
    state: str,
    interval: dict[str, Any] | None = None,
    execution_id: str | None = None,
    pull_id: str | None = None,
    row_count: int | None = None,
) -> CoverageMarker:
    """Update ONLY the HISTORICAL coverage marker (never the recent one).

    Background progressive history reports its progress/failure here. Because the
    historical marker is a SEPARATE row (migration 072 UNIQUE per coverage_kind), a
    ``failed`` history update can NEVER flip a ``covered`` recent marker or touch
    the live publication pointer -- the recent result stays available (INVARIANT).
    Chunked backfill stays parked by Epic 32: this is a thin marker updater, NOT a
    backfill engine (E36-NFR05).
    """
    if state not in {COV_PENDING, COV_LOADING, COV_COVERED, COV_DEGRADED, COV_FAILED}:
        raise RecentFirstValidationError("invalid coverage state")
    _upsert_coverage(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        coverage_kind=COVERAGE_HISTORICAL,
        state=state,
        actor=actor,
        interval=interval,
        execution_id=execution_id,
        pull_id=pull_id,
        row_count=row_count,
        covered=(state == COV_COVERED),
    )
    conn.commit()
    marker = _read_coverage(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        coverage_kind=COVERAGE_HISTORICAL,
    )
    if marker is None:  # pragma: no cover - defensive; just upserted above.
        raise RecentFirstUnavailable("historical coverage marker unavailable")
    return marker


def get_recent_first_state(
    conn,
    *,
    project_id: str,
    datastream_id: str,
) -> dict[str, Any]:
    """Read the recent-first state for the Story 36.10 readiness object.

    Returns the current publication pointer plus the SEPARATE recent and historical
    coverage markers. A later history failure is visible on the historical marker
    ONLY; the recent marker and the current publication are independent -- proving
    the two coverages never conflate.
    """
    pointer = _current_pointer(
        conn, datastream_id=datastream_id, project_id=project_id
    )
    recent = _read_coverage(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        coverage_kind=COVERAGE_RECENT,
    )
    historical = _read_coverage(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        coverage_kind=COVERAGE_HISTORICAL,
    )
    # The recent result is available whenever its marker is covered/degraded,
    # REGARDLESS of the historical marker's state (separate coverage invariant).
    recent_available = bool(
        recent and recent.state in {COV_COVERED, COV_DEGRADED}
    )
    return {
        "datastream_id": datastream_id,
        "project_id": project_id,
        "current_published_execution_id": pointer,
        "recent_coverage": recent.as_dict() if recent else None,
        "historical_coverage": historical.as_dict() if historical else None,
        "recent_available": recent_available,
    }
