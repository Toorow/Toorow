"""Story 36.10 -- expose first-pull PROGRESS and AUTHORITATIVE report readiness.

One honest, VERSIONED ``first_report_readiness`` object that answers exactly three
operator questions: what is usable NOW, what is still LOADING, and what BLOCKS
host connection. It is computed from SERVER TRUTH only and COMPOSES existing
seams (E36-NFR05) -- it never re-queries raw run/verification/publication/DQ
tables itself:

  * ``recent_first_publication.get_recent_first_state`` (Story 36.9): the current
    publication pointer + the SEPARATE recent and historical coverage markers
    (``app.datastream_coverage``, migration 072). This is the PRIMARY input --
    the recent result stays available regardless of the historical marker.

  * ``datastream_diagnosis._collect_events`` / ``_diagnose`` (Story 36.12): the
    sanitized, allowlisted run / verification / execution / publication / audit /
    connection-health evidence read model. We reuse it verbatim for the phase
    evidence (attempts, interval, canonical error class) -- NO re-query of the raw
    tables, NO second diagnostic engine, and the same redaction invariants.

  * ``dq_api.fetch_dq_report_data`` (Story 13.4): the canonical DQ state + freshness.

  * ``datastream_field_mapping.get_mapping_version`` (migration 032): the immutable
    mapping version + provenance (contract version, source schema hash, capability
    fingerprint, content hash) carried by the active publication.

  * ``app.first_report_drafts.recommendation`` (migration 069): the model-safe
    selected source objects (report/metrics/dimensions/grain/timezone/currency)
    and the recent interval, plus known exclusions.

  * ``app.datastream_publication_log`` (migration 042): the last SUCCESSFUL pull
    (published_at + row_count) -- read through the sanitized diagnosis events, not
    a fresh raw query.

INVARIANTS (held HARD, mirrored in the tests):

  * ONE VERSIONED object: a deterministic ``readiness_version`` hash over the
    canonical inputs makes the whole snapshot content-addressable, so a host or the
    UI can detect a real change without trusting a wall clock.

  * DEGRADED IS HONEST, NEVER "READY": partial history OR degraded DQ while the
    recent policy still passes => ``overall == "degraded"`` with honest coverage.
    It is NEVER labelled fully ready. A hard failure / no recent publication =>
    ``overall == "blocked"``.

  * HOST CTA IS SERVER-DERIVED: ``host_cta`` in {enabled, disabled, degraded} is
    computed here from ``overall`` and returned in the object. The UI MUST render
    that value and NEVER re-infer it from colours or its own logic.

  * PER-PHASE STATE: each named phase carries a state in {waiting, running,
    succeeded, degraded, failed, blocked} plus interval / rows / attempts /
    live-publication / next action.

  * FAIL-CLOSED ACCESS: the REST caller resolves ``resolve_strict_resource_access``
    (view+) and existence-hides on denial; this domain never opens a resource by
    itself. It is a PURE read/compose surface -- it mutates nothing.

Source-agnostic (AD-2): no provider name, no provider field name, no import from
``server/modules/*``. French user-facing messages (``next_action.label`` /
``headline``). Counts/hashes/ids only -- never raw rows (the composed seams already
redact).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Versioned schema of the readiness object itself (bump on a shape change so a
# host that froze an old shape can tell it must rescan).
READINESS_SCHEMA_VERSION = "1"

# ---------------------------------------------------------------------------
# Phase + state + overall + CTA vocabularies (allowlisted -- never free text).
# ---------------------------------------------------------------------------
PHASE_WAITING = "waiting"
PHASE_RUNNING = "running"
PHASE_SUCCEEDED = "succeeded"
PHASE_DEGRADED = "degraded"
PHASE_FAILED = "failed"
PHASE_BLOCKED = "blocked"
_PHASE_STATES = frozenset(
    (PHASE_WAITING, PHASE_RUNNING, PHASE_SUCCEEDED, PHASE_DEGRADED, PHASE_FAILED, PHASE_BLOCKED)
)

OVERALL_READY = "ready"
OVERALL_DEGRADED = "degraded"
OVERALL_BLOCKED = "blocked"

CTA_ENABLED = "enabled"
CTA_DISABLED = "disabled"
CTA_DEGRADED = "degraded"

# Named phases, in operator-facing order (UX-DR28). Recent-pull is the phase that
# gates host connection; history runs in the background and can only DEGRADE.
PHASE_SELECTION = "selection"
PHASE_RECENT_PULL = "recent_pull"
PHASE_VERIFICATION = "verification"
PHASE_PUBLICATION = "publication"
PHASE_DATA_QUALITY = "data_quality"
PHASE_HISTORY = "history"
_PHASE_ORDER = (
    PHASE_SELECTION,
    PHASE_RECENT_PULL,
    PHASE_VERIFICATION,
    PHASE_PUBLICATION,
    PHASE_DATA_QUALITY,
    PHASE_HISTORY,
)


class FirstReportReadinessUnavailable(RuntimeError):
    """The datastream/project scope cannot be resolved safely for readiness."""


# ---------------------------------------------------------------------------
# Value objects. Model/UI-facing -- ids/counts/hashes/enums only, never raw rows.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PhaseState:
    """One named phase of the first-pull journey (UX-DR28)."""

    phase: str
    state: str  # waiting | running | succeeded | degraded | failed | blocked
    interval: dict[str, Any] | None
    rows: int | None
    attempts: int
    # The live/current publication pointer AS OF this phase (never a candidate).
    live_publication_execution_id: str | None
    next_action: dict[str, Any] | None  # {label, owner} -- French label.
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "state": self.state,
            "interval": self.interval,
            "rows": self.rows,
            "attempts": self.attempts,
            "live_publication_execution_id": self.live_publication_execution_id,
            "next_action": self.next_action,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FirstReportReadiness:
    schema_version: str
    readiness_version: str
    datastream_id: str
    project_id: str
    overall: str  # ready | degraded | blocked
    host_cta: str  # enabled | disabled | degraded  -- SERVER-derived
    headline: str  # honest French one-liner (never "prêt" when degraded)
    current_publication: dict[str, Any] | None
    selected_objects: dict[str, Any]
    recent_coverage: dict[str, Any] | None
    historical_coverage: dict[str, Any] | None
    freshness: dict[str, Any] | None
    verification: dict[str, Any] | None
    dq: dict[str, Any] | None
    mapping: dict[str, Any] | None
    provenance: dict[str, Any] | None
    exclusions: list[dict[str, Any]]
    last_successful_pull: dict[str, Any] | None
    diagnosis: dict[str, Any] | None
    phases: list[PhaseState]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "readiness_version": self.readiness_version,
            "datastream_id": self.datastream_id,
            "project_id": self.project_id,
            "overall": self.overall,
            "host_cta": self.host_cta,
            "headline": self.headline,
            "current_publication": self.current_publication,
            "selected_objects": self.selected_objects,
            "recent_coverage": self.recent_coverage,
            "historical_coverage": self.historical_coverage,
            "freshness": self.freshness,
            "verification": self.verification,
            "dq": self.dq,
            "mapping": self.mapping,
            "provenance": self.provenance,
            "exclusions": self.exclusions,
            "last_successful_pull": self.last_successful_pull,
            "diagnosis": self.diagnosis,
            "phases": [p.as_dict() for p in self.phases],
        }


# ---------------------------------------------------------------------------
# Deterministic readiness version -- content-addressable over canonical inputs.
# ---------------------------------------------------------------------------
def _readiness_version(payload: dict[str, Any]) -> str:
    """Return a deterministic hex hash over the readiness-defining inputs.

    INVARIANT: identical server truth -> identical ``readiness_version``; any real
    change to coverage/publication/DQ/verification/overall flips it. A wall clock
    is NEVER part of the hash so idempotent polls don't churn the announcement.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Input composition (all through existing seams -- E36-NFR05).
# ---------------------------------------------------------------------------
def _load_active_draft(conn, *, project_id: str, datastream_id: str) -> dict[str, Any] | None:
    """Read the model-safe recommendation snapshot (selected objects + exclusions).

    This is the ONLY direct read this module does, and it touches only the
    already-redacted ``recommendation`` JSONB (report/metrics/dimensions/grain/
    timezone/currency/interval + known exclusions) -- identifiers/counts only.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT plan_version_id, mapping_version_id, recommendation
            FROM app.first_report_drafts
            WHERE datastream_id = %s AND project_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    recommendation = row[2] if isinstance(row[2], dict) else {}
    return {
        "plan_version_id": row[0],
        "mapping_version_id": row[1],
        "recommendation": recommendation,
    }


def _selected_objects(recommendation: dict[str, Any]) -> dict[str, Any]:
    """Project the selected source objects from the redacted recommendation."""
    return {
        "report_id": recommendation.get("report_id"),
        "metrics": list(recommendation.get("metrics") or []),
        "dimensions": list(recommendation.get("dimensions") or []),
        "grain": list(recommendation.get("grain") or []),
        "timezone": recommendation.get("timezone"),
        "currency": recommendation.get("currency"),
        "interval": recommendation.get("interval") or None,
    }


def _exclusions(recommendation: dict[str, Any]) -> list[dict[str, Any]]:
    """Known exclusions declared by the recommendation (allowlisted shape)."""
    raw = recommendation.get("exclusions") or recommendation.get("known_exclusions") or []
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            out.append(
                {
                    "kind": str(item.get("kind") or item.get("type") or "exclusion")[:64],
                    "reason": str(item.get("reason") or "")[:200] or None,
                }
            )
    return out


def _last_successful_pull(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Derive the last SUCCESSFUL pull from the sanitized publication events.

    A publication event is emitted only for a committed pointer swap (Story 36.12
    ``_sanitize_publication_event``), so the newest one IS the last successful
    pull. Reusing the sanitized events keeps the redaction contract and avoids a
    second raw query (E36-NFR05).
    """
    pub = [e for e in events if e.get("kind") == "publication"]
    if not pub:
        return None
    pub.sort(key=lambda e: (e.get("at") or ""), reverse=True)
    latest = pub[0]
    return {
        "execution_id": (latest.get("correlation") or {}).get("execution_id"),
        "row_count": latest.get("row_count"),
        "at": latest.get("at"),
    }


def _latest_verification(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Derive the latest verification verdict from the sanitized events."""
    ver = [e for e in events if e.get("kind") == "verification"]
    if not ver:
        return None
    ver.sort(key=lambda e: (e.get("at") or ""), reverse=True)
    latest = ver[0]
    return {
        "verdict": latest.get("state"),
        "row_count": latest.get("row_count"),
        "expected_rows": latest.get("expected_rows"),
        "at": latest.get("at"),
    }


def _recent_attempts(events: list[dict[str, Any]]) -> int:
    """Max attempt count across the sanitized pull/ledger events (bounded)."""
    attempts = [int(e.get("attempts") or 0) for e in events if e.get("kind") in ("pull", "ledger")]
    return max(attempts) if attempts else 0


# ---------------------------------------------------------------------------
# DQ + freshness projection (from fetch_dq_report_data).
# ---------------------------------------------------------------------------
def _dq_state(dq_report: dict[str, Any]) -> dict[str, Any]:
    """Project the DQ report into a compact readiness DQ block.

    ``degraded`` is True when there are unresolved issues OR monitors are
    unavailable (an honest "cannot assert green" -- mirrors dq_api's H1 rule).
    """
    total = int(dq_report.get("total_unresolved") or 0)
    unavailable = bool(dq_report.get("monitors_unavailable"))
    return {
        "total_unresolved": total,
        "monitors_unavailable": unavailable,
        "degraded": total > 0 or unavailable,
        "evaluated_days_30d": int(dq_report.get("evaluated_days_30d") or 0),
        "issue_count": len(dq_report.get("issues") or []),
    }


def _freshness(dq_report: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Project freshness from the DQ report + connection-health context."""
    return {
        "last_pull_at": dq_report.get("freshness_last_pull_at")
        or context.get("freshness_last_pull_at"),
        "connection_health": context.get("connection_health"),
    }


# ---------------------------------------------------------------------------
# Phase assembly (UX-DR28) -- each phase carries interval/rows/attempts/next-action.
# ---------------------------------------------------------------------------
def _next_action(label: str, owner: str = "operateur") -> dict[str, Any]:
    return {"label": label, "owner": owner}


def _build_phases(
    *,
    selected: dict[str, Any],
    recent: dict[str, Any] | None,
    historical: dict[str, Any] | None,
    verification: dict[str, Any] | None,
    current_pointer: str | None,
    dq: dict[str, Any],
    recent_available: bool,
    attempts: int,
    last_pull: dict[str, Any] | None,
) -> list[PhaseState]:
    """Derive one PhaseState per named phase from the composed server truth."""
    interval = selected.get("interval")
    recent_state = (recent or {}).get("state")
    hist_state = (historical or {}).get("state")
    recent_rows = (recent or {}).get("row_count")

    phases: list[PhaseState] = []

    # 1. Selection -- succeeded once we have a report + metrics selected.
    has_selection = bool(selected.get("report_id") and selected.get("metrics"))
    phases.append(
        PhaseState(
            phase=PHASE_SELECTION,
            state=PHASE_SUCCEEDED if has_selection else PHASE_WAITING,
            interval=interval,
            rows=None,
            attempts=0,
            live_publication_execution_id=current_pointer,
            next_action=None
            if has_selection
            else _next_action("Choisir un rapport et des métriques."),
        )
    )

    # 2. Recent pull -- the phase that gates host connection.
    if recent_state == "covered":
        recent_phase = PHASE_SUCCEEDED
        recent_action = None
    elif recent_state == "degraded":
        recent_phase = PHASE_DEGRADED
        recent_action = _next_action("Le résultat récent est partiel : vérifier la couverture.")
    elif recent_state == "loading":
        recent_phase = PHASE_RUNNING
        recent_action = None
    elif recent_state in ("failed",):
        recent_phase = PHASE_FAILED
        recent_action = _next_action("Le pull récent a échoué : diagnostiquer puis relancer.")
    elif recent_state == "pending" or recent_state is None:
        recent_phase = PHASE_WAITING
        recent_action = _next_action("Lancer le premier pull récent.")
    else:  # pragma: no cover - defensive
        recent_phase = PHASE_WAITING
        recent_action = _next_action("Lancer le premier pull récent.")
    phases.append(
        PhaseState(
            phase=PHASE_RECENT_PULL,
            state=recent_phase,
            interval=interval,
            rows=recent_rows,
            attempts=attempts,
            live_publication_execution_id=current_pointer,
            next_action=recent_action,
        )
    )

    # 3. Verification -- the verdict retained BEFORE read.
    verdict = (verification or {}).get("verdict")
    if verdict in ("ok", "passed", "verified"):
        ver_phase = PHASE_SUCCEEDED
        ver_action = None
    elif verdict in ("partial", "empty"):
        ver_phase = PHASE_DEGRADED
        ver_action = _next_action("Vérification partielle : couverture honnête exposée.")
    elif verdict in ("invalid", "failed"):
        ver_phase = PHASE_FAILED
        ver_action = _next_action("Vérification en échec : le candidat n'est pas publiable.")
    elif verification is None:
        ver_phase = PHASE_WAITING if recent_phase in (PHASE_WAITING,) else PHASE_RUNNING
        ver_action = None
    else:  # pragma: no cover - defensive
        ver_phase = PHASE_RUNNING
        ver_action = None
    phases.append(
        PhaseState(
            phase=PHASE_VERIFICATION,
            state=ver_phase,
            interval=interval,
            rows=(verification or {}).get("row_count"),
            attempts=attempts,
            live_publication_execution_id=current_pointer,
            next_action=ver_action,
            detail={"expected_rows": (verification or {}).get("expected_rows")}
            if verification
            else {},
        )
    )

    # 4. Publication -- succeeded when the live pointer exists (last-known-good).
    if current_pointer:
        pub_phase = PHASE_SUCCEEDED
        pub_action = None
    elif recent_phase == PHASE_FAILED:
        pub_phase = PHASE_BLOCKED
        pub_action = _next_action("Aucune publication : dernier bon rapport indisponible.")
    else:
        pub_phase = PHASE_WAITING
        pub_action = _next_action("Publier le résultat récent validé.")
    phases.append(
        PhaseState(
            phase=PHASE_PUBLICATION,
            state=pub_phase,
            interval=interval,
            rows=(last_pull or {}).get("row_count"),
            attempts=0,
            live_publication_execution_id=current_pointer,
            next_action=pub_action,
            detail={"published_at": (last_pull or {}).get("at")} if last_pull else {},
        )
    )

    # 5. Data quality -- degraded (never failed) when unresolved issues exist while
    #    the recent result is still available (honest degraded, never blocked).
    if dq.get("monitors_unavailable"):
        dq_phase = PHASE_DEGRADED
        dq_action = _next_action("Monitors DQ indisponibles : état non affirmé.")
    elif dq.get("degraded"):
        dq_phase = PHASE_DEGRADED
        dq_action = _next_action("Des contrôles DQ sont ouverts : couverture dégradée honnête.")
    else:
        dq_phase = PHASE_SUCCEEDED
        dq_action = None
    phases.append(
        PhaseState(
            phase=PHASE_DATA_QUALITY,
            state=dq_phase,
            interval=interval,
            rows=None,
            attempts=0,
            live_publication_execution_id=current_pointer,
            next_action=dq_action,
            detail={
                "total_unresolved": dq.get("total_unresolved"),
                "issue_count": dq.get("issue_count"),
            },
        )
    )

    # 6. History -- background; can only DEGRADE (never flips the recent result).
    if hist_state == "covered":
        hist_phase = PHASE_SUCCEEDED
        hist_action = None
    elif hist_state == "loading":
        hist_phase = PHASE_RUNNING
        hist_action = None
    elif hist_state in ("degraded", "failed"):
        hist_phase = PHASE_DEGRADED
        hist_action = _next_action(
            "L'historique est partiel : le résultat récent reste disponible.", owner="systeme"
        )
    else:  # pending / None
        hist_phase = PHASE_WAITING
        hist_action = None
    phases.append(
        PhaseState(
            phase=PHASE_HISTORY,
            state=hist_phase,
            interval=(historical or {}).get("interval_start") and {
                "start": (historical or {}).get("interval_start"),
                "end_exclusive": (historical or {}).get("interval_end_exclusive"),
            }
            or None,
            rows=(historical or {}).get("row_count"),
            attempts=0,
            live_publication_execution_id=current_pointer,
            next_action=hist_action,
        )
    )
    return phases


# ---------------------------------------------------------------------------
# Overall + CTA derivation (SERVER-side -- the UI never re-infers this).
# ---------------------------------------------------------------------------
def _derive_overall(
    *,
    recent_available: bool,
    recent_state: str | None,
    historical_state: str | None,
    dq: dict[str, Any],
    current_pointer: str | None,
) -> str:
    """Return overall in {ready, degraded, blocked} from server truth.

    INVARIANT (the story's core rule):
      * no recent publication / hard failure -> BLOCKED.
      * recent policy passes BUT (partial history OR degraded DQ) -> DEGRADED
        (honest coverage; NEVER labelled fully ready).
      * recent policy passes AND history covered AND DQ clean -> READY.
    """
    # Blocked: the recent result is not available or nothing is published.
    if not recent_available or not current_pointer:
        return OVERALL_BLOCKED
    if recent_state == "failed":
        return OVERALL_BLOCKED

    partial_history = historical_state in ("degraded", "failed", "loading", "pending", None)
    degraded_dq = bool(dq.get("degraded"))
    recent_degraded = recent_state == "degraded"

    if partial_history or degraded_dq or recent_degraded:
        return OVERALL_DEGRADED
    return OVERALL_READY


def _host_cta_for(overall: str) -> str:
    """Map overall -> host CTA. SERVER-authoritative; the UI renders this verbatim.

    INVARIANT: blocked readiness => the host CTA is DISABLED; degraded => the CTA is
    explicitly DEGRADED (usable-with-caveats, never "prêt"); ready => enabled.
    """
    if overall == OVERALL_READY:
        return CTA_ENABLED
    if overall == OVERALL_DEGRADED:
        return CTA_DEGRADED
    return CTA_DISABLED


def _headline(overall: str) -> str:
    """Honest French one-liner. NEVER says "prêt" unless overall == ready."""
    if overall == OVERALL_READY:
        return "Rapport prêt : période récente publiée et contrôles au vert."
    if overall == OVERALL_DEGRADED:
        return (
            "Rapport dégradé mais utilisable : le résultat récent est publié ; "
            "l'historique ou la qualité des données reste partiel."
        )
    return "Rapport indisponible : aucun résultat récent publié pour l'instant."


# ---------------------------------------------------------------------------
# First-value funnel instrumentation (E36-FR09) -- best-effort, separate sink.
# ---------------------------------------------------------------------------
_OVERALL_TO_OUTCOME = {
    OVERALL_READY: "succeeded",
    OVERALL_DEGRADED: "degraded",
    OVERALL_BLOCKED: "blocked",
}


def _record_readiness_funnel(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    overall: str,
    historical_state: str | None,
) -> None:
    """Best-effort: mirror the readiness verdict + history completion into the funnel.

    Gated on the funnel pepper so this pure read adds no query when analytics are off
    (and MagicMock-based tests, which set no pepper, exercise none of it). All work is
    swallowed on failure -- readiness is NEVER affected. E36-FR09 / AD-32.
    """
    from core.first_value_instrumentation import funnel_enabled, record_stage  # noqa: PLC0415

    if not funnel_enabled():
        return
    org_id = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
            org_id = row[0] if row else None
    except Exception as exc:  # noqa: BLE001 -- never fail readiness on a telemetry read.
        logger.debug("first_report_readiness: funnel org lookup skipped: %s", exc)
        org_id = None

    record_stage(
        conn,
        org_id=org_id,
        project_id=project_id,
        subject=datastream_id,
        stage="report_readiness",
        outcome=_OVERALL_TO_OUTCOME.get(overall, "blocked"),
    )
    # History completion is a distinct funnel stage: covered -> succeeded; a partial /
    # failed background history is an honest degraded (never blocks the recent result).
    if historical_state == "covered":
        hist_outcome: str | None = "succeeded"
    elif historical_state in ("degraded", "failed"):
        hist_outcome = "degraded"
    else:
        hist_outcome = None
    if hist_outcome is not None:
        record_stage(
            conn,
            org_id=org_id,
            project_id=project_id,
            subject=datastream_id,
            stage="history_completion",
            outcome=hist_outcome,
        )


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------
def compute_first_report_readiness(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    actor: str,
) -> FirstReportReadiness:
    """Compute ONE versioned first_report_readiness object from server truth.

    PURE read/compose surface (E36-NFR05): it reuses ``get_recent_first_state``
    (36.9), ``_collect_events`` / ``_diagnose`` (36.12), ``fetch_dq_report_data``
    (13.4), ``get_mapping_version`` (032) and the redacted draft recommendation
    (069). It mutates NOTHING and opens NO warehouse cursor.

    The caller (REST) has ALREADY resolved fail-closed access (view+) and
    existence-hides on denial (E36-NFR02); this function trusts that boundary and
    never opens the resource by itself.

    Returns a ``FirstReportReadiness`` whose ``overall`` / ``host_cta`` are derived
    SERVER-side (the UI renders them, never re-infers), whose ``readiness_version``
    is a deterministic hash of the inputs, and whose phases each carry state +
    interval / rows / attempts / live-publication / next action.
    """
    # Lazy imports keep this module import-cycle-free and offline-testable.
    from core import datastream_diagnosis as diag  # noqa: PLC0415
    from core.dq_api import fetch_dq_report_data  # noqa: PLC0415
    from core.recent_first_publication import get_recent_first_state  # noqa: PLC0415

    # 1. Recent-first state (PRIMARY input): pointer + separate coverage markers.
    recent_state = get_recent_first_state(
        conn, project_id=project_id, datastream_id=datastream_id
    )
    current_pointer = recent_state.get("current_published_execution_id")
    recent_cov = recent_state.get("recent_coverage")
    hist_cov = recent_state.get("historical_coverage")
    recent_available = bool(recent_state.get("recent_available"))

    # 2. Sanitized evidence (reuse 36.12 -- NO re-query of raw tables).
    events, context = diag._collect_events(conn, datastream_id)
    diagnosis = diag._diagnose(events)
    verification = _latest_verification(events)
    last_pull = _last_successful_pull(events)
    attempts = _recent_attempts(events)

    # 3. DQ + freshness (reuse 13.4).
    try:
        dq_report = fetch_dq_report_data(project_id, conn)
    except Exception as exc:  # noqa: BLE001 -- never fail readiness on a degraded DQ read.
        logger.warning("first_report_readiness: dq read failed ds=%s: %s", datastream_id, exc)
        dq_report = {"total_unresolved": 0, "monitors_unavailable": True, "issues": []}
    dq = _dq_state(dq_report)
    freshness = _freshness(dq_report, context)

    # 4. Selected objects + exclusions + mapping/provenance (draft + 032 seam).
    draft = _load_active_draft(conn, project_id=project_id, datastream_id=datastream_id)
    recommendation = (draft or {}).get("recommendation") or {}
    selected = _selected_objects(recommendation)
    exclusions = _exclusions(recommendation)

    mapping: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    mapping_version_id = (draft or {}).get("mapping_version_id")
    if mapping_version_id:
        try:
            from core.datastream_field_mapping import (  # noqa: PLC0415
                DatastreamMappingNotFound,
                get_mapping_version,
            )

            mv = get_mapping_version(datastream_id, project_id, mapping_version_id, conn)
            mapping = {
                "mapping_version_id": mv.get("id"),
                "version_number": mv.get("version_number"),
                "mapping_contract_version": mv.get("mapping_contract_version"),
                "executable": mv.get("executable"),
            }
            provenance = {
                "source_schema_hash": mv.get("source_schema_hash"),
                "capability_fingerprint": mv.get("capability_fingerprint"),
                "content_hash": mv.get("content_hash"),
                "plan_version_id": mv.get("plan_version_id"),
                "ossie_spec_version": mv.get("ossie_spec_version"),
            }
        except DatastreamMappingNotFound:
            mapping = None
        except Exception as exc:  # noqa: BLE001 -- provenance is best-effort evidence.
            logger.warning(
                "first_report_readiness: mapping read failed ds=%s: %s", datastream_id, exc
            )

    current_publication = None
    if current_pointer:
        current_publication = {
            "execution_id": current_pointer,
            "row_count": (last_pull or {}).get("row_count"),
            "published_at": (last_pull or {}).get("at"),
        }

    # 5. Overall + CTA (SERVER-derived) + honest headline.
    overall = _derive_overall(
        recent_available=recent_available,
        recent_state=(recent_cov or {}).get("state"),
        historical_state=(hist_cov or {}).get("state"),
        dq=dq,
        current_pointer=current_pointer,
    )
    host_cta = _host_cta_for(overall)
    headline = _headline(overall)

    # 6. Named phases (UX-DR28).
    phases = _build_phases(
        selected=selected,
        recent=recent_cov,
        historical=hist_cov,
        verification=verification,
        current_pointer=current_pointer,
        dq=dq,
        recent_available=recent_available,
        attempts=attempts,
        last_pull=last_pull,
    )

    # 7. Deterministic version over the readiness-defining inputs (no wall clock).
    readiness_version = _readiness_version(
        {
            "schema": READINESS_SCHEMA_VERSION,
            "overall": overall,
            "host_cta": host_cta,
            "current_pointer": current_pointer,
            "recent": recent_cov,
            "historical": hist_cov,
            "verification": verification,
            "dq": dq,
            "mapping": mapping,
            "provenance": provenance,
            "selected": selected,
            "exclusions": exclusions,
            "last_pull": last_pull,
            "phases": [p.as_dict() for p in phases],
        }
    )

    # First-value funnel (E36-FR09): mirror the AUTHORITATIVE readiness verdict + the
    # history-completion state into the funnel. This is telemetry only -- it writes to
    # the SEPARATE analytics sink, never to the readiness inputs, and is fully best-
    # effort (gated on the funnel pepper; the org lookup + record are swallowed on any
    # failure) so this pure read/compose surface is never broken by a telemetry write.
    _record_readiness_funnel(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        overall=overall,
        historical_state=(hist_cov or {}).get("state"),
    )

    return FirstReportReadiness(
        schema_version=READINESS_SCHEMA_VERSION,
        readiness_version=readiness_version,
        datastream_id=datastream_id,
        project_id=project_id,
        overall=overall,
        host_cta=host_cta,
        headline=headline,
        current_publication=current_publication,
        selected_objects=selected,
        recent_coverage=recent_cov,
        historical_coverage=hist_cov,
        freshness=freshness,
        verification=verification,
        dq=dq,
        mapping=mapping,
        provenance=provenance,
        exclusions=exclusions,
        last_successful_pull=last_pull,
        diagnosis=diagnosis,
        phases=phases,
    )
