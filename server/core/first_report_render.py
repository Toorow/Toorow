"""Story 36.15 -- RENDER, VALIDATE and REPRODUCE the first report (E36-FR05).

This is the domain surface behind the read-only *starter request*: once the
Story 36.10 readiness object says the recent result is usable (``ready`` or
``degraded`` within policy), the operator (and then a SECOND authorized user)
must be able to render the first correct answer, VALIDATE that its figures match
the active publication + mapping versions, and REPRODUCE it independently. It
proves setup is a SHARED, reproducible value -- not a one-person demo.

It COMPOSES existing seams (E36-NFR05 / E36-NFR06) and NEVER opens a second
report engine:

  * ``first_report_readiness.compute_first_report_readiness`` (Story 36.10): the
    ONE versioned readiness object. The starter request runs ONLY when its
    ``overall`` is ``ready`` or ``degraded`` (degraded-within-policy). ``blocked``
    -> refused. The readiness object already carries freshness / coverage /
    provenance (mapping version) / exclusions -- we cite THOSE, never re-derive.

  * the existing report read path (``core.main.get_report`` / the report read
    model): the rendered report itself. We DO NOT send the full dataset into
    model context (E36-NFR06). The render returns BOUNDED EVIDENCE (counts /
    hashes / a handful of headline figures) + an OPTIONAL authenticated deep-link
    that a human follows to see the full dataset OUT of band.

  * ``project_access.resolve_strict_resource_access`` (Story 36.1): the SECOND
    user's reproduction INDEPENDENTLY re-evaluates access. Unauthorized ->
    existence-hiding ``not_found`` (no report existence / no first-user result
    leak).

  * ``host_preflight`` (Story 36.14, landing in parallel): the bound capability
    context decides app-UI vs standard-tool fallback (``ui_supported``). Because
    36.14 may not be on disk yet, we reference it by a DOCUMENTED interface and
    GUARD the import -- a missing module simply means ``ui_supported=False`` (the
    safe compact-text fallback), never a crash and never an app-UI claim we can't
    back.

INVARIANTS (held HARD, mirrored in the tests):

  * READ-ONLY: the starter request mutates NOTHING. No pointer swap, no version
    advance, no candidate. It is a pure read/compose surface.

  * READINESS GATE: render runs ONLY when ``overall in {ready, degraded}``.
    ``blocked`` (or a missing readiness) raises ``FirstReportRenderUnavailable``.

  * NO FULL DATASET INTO MODEL CONTEXT (E36-NFR06): the model-facing payload is
    BOUNDED -- counts, hashes, at most ``_MAX_BOUNDED_FIGURES`` headline figures,
    plus an OPTIONAL authenticated deep-link. The full dataset is NEVER embedded.

  * FIGURES CITE THE ACTIVE PUBLICATION + MAPPING VERSION: every render carries
    ``publication.execution_id`` and ``mapping.mapping_version_id`` +
    provenance hashes, so validation can confirm the figures match the versions
    that produced them.

  * HONEST DEGRADED / STALE, NON-COLOUR: the render restates the server
    ``overall`` / ``host_cta`` and an explicit French ``degraded`` note; it never
    upgrades ``degraded`` to "prêt".

  * SECOND-USER ACCESS INDEPENDENTLY EVALUATED + EXISTENCE-HIDING: reproduction
    resolves the strict access seam for the SECOND identity/workspace on its own;
    denial for ANY reason -> a single ``not_found`` with NO report/first-user
    leak. Authorized -> the SAME publication/provenance result is reproducible.

Source-agnostic (AD-2): no provider name, no ``server/modules/*`` import. French
user-facing messages. Counts / hashes / ids only -- never raw rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Versioned schema of the render object (bump on a shape change so a host that
# froze an old shape can tell it must rescan).
RENDER_SCHEMA_VERSION = "1"

# The readiness dispositions under which the starter request may run. ``blocked``
# is EXCLUDED -- there is no usable recent publication to render.
_RENDERABLE_OVERALL = frozenset({"ready", "degraded"})

# The model-facing render never embeds the full dataset (E36-NFR06). At most this
# many HEADLINE figures ride in the bounded evidence; the rest is behind the
# authenticated deep-link a human follows out of band.
_MAX_BOUNDED_FIGURES = 6


class FirstReportRenderUnavailable(RuntimeError):
    """The starter request cannot run: readiness is blocked / missing, or the scope
    cannot be resolved safely. Existence-hiding on the second-user path is a
    SEPARATE ``FirstReportReproductionDenied`` so the caller can map it to
    ``not_found`` without leaking report existence."""


class FirstReportReproductionDenied(RuntimeError):
    """The second user/workspace is NOT authorized. The caller MUST surface a single
    existence-hiding ``not_found`` -- never disclose report existence or the
    first-user result."""


# ---------------------------------------------------------------------------
# Value objects. Model/UI-facing -- ids / counts / hashes / enums only.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RenderedFirstReport:
    schema_version: str
    datastream_id: str
    project_id: str
    # Restated from the server readiness (the UI/model NEVER re-infers these).
    overall: str  # ready | degraded
    host_cta: str  # enabled | disabled | degraded
    ui_supported: bool  # app-UI vs compact-text fallback (from host_preflight)
    render_mode: str  # "app_ui" | "compact_text"
    headline: str  # honest French one-liner (never "prêt" when degraded)
    # The CITED evidence (from readiness) so validation can confirm the match.
    publication: dict[str, Any] | None
    mapping: dict[str, Any] | None
    provenance: dict[str, Any] | None
    freshness: dict[str, Any] | None
    coverage: dict[str, Any]
    exclusions: list[dict[str, Any]]
    # BOUNDED evidence only -- counts / hashes / a few headline figures.
    bounded_evidence: dict[str, Any]
    # An OPTIONAL authenticated deep-link (a human follows it OUT of band). The
    # full dataset is behind THIS, never in the payload.
    report_deep_link: dict[str, Any] | None
    # The validation checklist the operator confirms (figures match versions).
    validation: dict[str, Any]
    readiness_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "datastream_id": self.datastream_id,
            "project_id": self.project_id,
            "overall": self.overall,
            "host_cta": self.host_cta,
            "ui_supported": self.ui_supported,
            "render_mode": self.render_mode,
            "headline": self.headline,
            "publication": self.publication,
            "mapping": self.mapping,
            "provenance": self.provenance,
            "freshness": self.freshness,
            "coverage": self.coverage,
            "exclusions": self.exclusions,
            "bounded_evidence": self.bounded_evidence,
            "report_deep_link": self.report_deep_link,
            "validation": self.validation,
            "readiness_version": self.readiness_version,
        }


# ---------------------------------------------------------------------------
# Story 36.14 dependency -- the bound capability context (documented interface).
# ---------------------------------------------------------------------------
_FALLBACK_HOST_CTX: dict[str, Any] = {
    "ui_supported": False,
    "endpoint_binding": None,
    "workspace_id": None,
}


def _bound_host_connection(
    conn, *, project_id: str | None = None, org: str | None = None
) -> dict[str, Any]:
    """Return the bound host connection context for the current scope.

    ASSUMED 36.14 interface (guarded -- 36.14 lands in parallel, and may expose a
    different reader name):

        host_preflight.get_bound_host_connection(conn, *, project_id/org) ->
            {endpoint_binding, ui_supported, enabled_profiles, workspace_id,
             catalog_version}

    ``ui_supported`` decides app-UI vs standard-tool fallback (E36-NFR03). We try,
    in order: (1) an explicit ``get_bound_host_connection`` reader if 36.14 exposes
    one; (2) a DIRECT read of the Story 36.11 ``app.mcp_capability_contexts`` row
    36.14 writes (a bound endpoint means the host connection is established -> app
    UI is supported). Absent the module / row / any failure, we fail SAFE:
    ``ui_supported=False`` -> the compact-text fallback with mandatory bounded
    evidence. We NEVER claim app-UI we can't back, and we NEVER crash the render.
    """
    try:
        from core import host_preflight  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 -- module not landed yet -> safe fallback.
        logger.debug("first_report_render: host_preflight unavailable (%s)", exc)
        return dict(_FALLBACK_HOST_CTX)

    getter = getattr(host_preflight, "get_bound_host_connection", None)
    if callable(getter):
        try:
            ctx = getter(conn, project_id=project_id, org=org)
        except Exception as exc:  # noqa: BLE001 -- fail safe to the compact fallback.
            logger.debug("first_report_render: bound host lookup failed (%s)", exc)
            return dict(_FALLBACK_HOST_CTX)
        if isinstance(ctx, dict):
            return ctx
        return dict(_FALLBACK_HOST_CTX)

    # No dedicated reader on 36.14: read the bound capability context row directly.
    return _read_capability_context(conn, project_id=project_id)


def _read_capability_context(conn, *, project_id: str | None) -> dict[str, Any]:
    """Read the bound ``app.mcp_capability_contexts`` row for the project's org.

    A bound endpoint (Story 36.14 ``bind_host_connection``) means the MCP host
    connection is established, so the in-host app UI is supported. Returns the safe
    fallback (compact text) when no bound context exists or on any read error.
    """
    if not project_id:
        return dict(_FALLBACK_HOST_CTX)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.endpoint_binding, c.workspace_id
                FROM app.mcp_capability_contexts c
                JOIN app.projects p ON p.org_id = c.org_id
                WHERE p.id = %s AND c.endpoint_binding IS NOT NULL
                ORDER BY c.created_at DESC
                LIMIT 1
                """,
                (project_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- fail safe to the compact fallback.
        logger.debug("first_report_render: capability context read failed (%s)", exc)
        return dict(_FALLBACK_HOST_CTX)
    if not row or not row[0]:
        return dict(_FALLBACK_HOST_CTX)
    return {
        "ui_supported": True,
        "endpoint_binding": row[0],
        "workspace_id": row[1],
    }


# ---------------------------------------------------------------------------
# Bounded evidence + deep-link builders (pure -- offline-testable).
# ---------------------------------------------------------------------------
def _bounded_evidence(readiness: dict[str, Any]) -> dict[str, Any]:
    """Assemble the BOUNDED, model-safe evidence from the readiness object.

    INVARIANT (E36-NFR06): counts / hashes / at most ``_MAX_BOUNDED_FIGURES``
    headline figures -- NEVER the full dataset. The readiness object is already
    redacted (ids/counts/hashes), so we only project a compact subset of it.
    """
    publication = readiness.get("current_publication") or {}
    verification = readiness.get("verification") or {}
    selected = readiness.get("selected_objects") or {}
    provenance = readiness.get("provenance") or {}

    # Headline figures: bounded to a handful of selected metric NAMES + counts.
    metrics = list(selected.get("metrics") or [])[:_MAX_BOUNDED_FIGURES]
    return {
        "row_count": publication.get("row_count"),
        "verified_row_count": verification.get("row_count"),
        "expected_row_count": verification.get("expected_rows"),
        "metric_names": metrics,
        "metric_count": len(selected.get("metrics") or []),
        "dimension_count": len(selected.get("dimensions") or []),
        # Content hash lets a host/second-user confirm they see the SAME published
        # content without transferring the dataset.
        "content_hash": provenance.get("content_hash"),
        "truncated": len(selected.get("metrics") or []) > _MAX_BOUNDED_FIGURES,
        "note": (
            "Preuve bornee : totaux, empreintes et quelques figures. "
            "Le jeu de donnees complet reste hors du contexte du modele."
        ),
    }


def _report_deep_link(
    *, project_id: str, datastream_id: str, readiness: dict[str, Any]
) -> dict[str, Any]:
    """Build an OPTIONAL authenticated deep-link descriptor (never the dataset).

    A human follows this link (authenticated, project-scoped) to see the full
    report OUT of band. We return a descriptor -- path + the version handles that
    scope it -- NOT the dataset. The link is authenticated: it carries no bearer
    and the console re-authorizes on open (fail closed).
    """
    publication = readiness.get("current_publication") or {}
    return {
        "kind": "authenticated_report",
        "path": (
            f"/projects/{project_id}/datastreams/{datastream_id}"
            "/first-report/rendered"
        ),
        "requires_authentication": True,
        "publication_execution_id": publication.get("execution_id"),
        "readiness_version": readiness.get("readiness_version"),
        "note": (
            "Lien authentifie : ouvre le rapport complet dans la console "
            "(re-autorisation cote serveur ; aucun jeton dans le lien)."
        ),
    }


def _validation_checklist(readiness: dict[str, Any]) -> dict[str, Any]:
    """Build the validation checklist: figures match publication/mapping versions.

    The checklist is what the operator confirms (UX-DR31). It pins the ACTIVE
    publication execution id + mapping version + provenance content hash so the
    validator can assert the rendered figures were produced by exactly those
    versions -- and restates the honest degraded/stale disposition (non-colour).
    """
    publication = readiness.get("current_publication") or {}
    mapping = readiness.get("mapping") or {}
    provenance = readiness.get("provenance") or {}
    overall = readiness.get("overall")
    return {
        "figures_cite_publication": bool(publication.get("execution_id")),
        "figures_cite_mapping_version": bool(mapping.get("mapping_version_id")),
        "publication_execution_id": publication.get("execution_id"),
        "mapping_version_id": mapping.get("mapping_version_id"),
        "mapping_version_number": mapping.get("version_number"),
        "content_hash": provenance.get("content_hash"),
        # Honest, explicit, NON-COLOUR: the degraded/stale state is a TEXT token
        # the UI renders verbatim -- never a bare colour.
        "state_token": overall,  # ready | degraded
        "is_degraded": overall == "degraded",
        "degraded_note": (
            "Rapport degrade mais utilisable : le resultat recent est publie ; "
            "l'historique ou la qualite des donnees reste partiel."
        )
        if overall == "degraded"
        else None,
    }


def _headline(readiness: dict[str, Any]) -> str:
    """Honest French one-liner restated from readiness. Never "prêt" when degraded."""
    overall = readiness.get("overall")
    if overall == "ready":
        return "Premier rapport rendu : periode recente publiee et controles au vert."
    # degraded (the only other renderable disposition).
    return (
        "Premier rapport rendu (degrade mais utilisable) : le resultat recent est "
        "publie ; l'historique ou la qualite des donnees reste partiel."
    )


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------
def render_first_report(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    actor: str,
) -> RenderedFirstReport:
    """Render the first report for the starter request (READ-ONLY, E36-FR05).

    Runs ONLY when the Story 36.10 readiness ``overall`` is ``ready`` or
    ``degraded`` (degraded-within-policy). ``blocked`` / missing readiness raises
    ``FirstReportRenderUnavailable`` (nothing to render). The render CITES
    freshness / coverage / provenance (mapping version) / exclusions from the
    readiness object, returns a compact BOUNDED-evidence summary (never the full
    dataset, E36-NFR06) and an OPTIONAL authenticated deep-link, and pins the
    active publication + mapping version so validation can confirm the figures
    match.

    The caller (REST / MCP) has ALREADY resolved fail-closed access (view+) and
    existence-hides on denial (E36-NFR02); this function trusts that boundary and
    mutates NOTHING.
    """
    from core.first_report_readiness import compute_first_report_readiness  # noqa: PLC0415

    readiness_obj = compute_first_report_readiness(
        conn, datastream_id=datastream_id, project_id=project_id, actor=actor
    )
    readiness = readiness_obj.as_dict()
    overall = readiness.get("overall")

    # READINESS GATE: the starter request runs ONLY for a renderable disposition.
    if overall not in _RENDERABLE_OVERALL:
        raise FirstReportRenderUnavailable(
            "readiness_not_renderable"  # blocked / missing -> nothing to render
        )

    # app-UI vs compact-text fallback comes from the BOUND host connection (36.14).
    host_ctx = _bound_host_connection(conn, project_id=project_id)
    ui_supported = bool(host_ctx.get("ui_supported"))
    render_mode = "app_ui" if ui_supported else "compact_text"

    bounded = _bounded_evidence(readiness)
    # The deep-link is ALWAYS available as additional help; in the compact-text
    # fallback it is the human's out-of-band path to the full dataset -- but the
    # fallback still carries mandatory bounded evidence (never deep-link-only).
    deep_link = _report_deep_link(
        project_id=project_id, datastream_id=datastream_id, readiness=readiness
    )
    validation = _validation_checklist(readiness)

    return RenderedFirstReport(
        schema_version=RENDER_SCHEMA_VERSION,
        datastream_id=datastream_id,
        project_id=project_id,
        overall=overall,
        host_cta=readiness.get("host_cta"),
        ui_supported=ui_supported,
        render_mode=render_mode,
        headline=_headline(readiness),
        publication=readiness.get("current_publication"),
        mapping=readiness.get("mapping"),
        provenance=readiness.get("provenance"),
        freshness=readiness.get("freshness"),
        coverage={
            "recent": readiness.get("recent_coverage"),
            "historical": readiness.get("historical_coverage"),
        },
        exclusions=list(readiness.get("exclusions") or []),
        bounded_evidence=bounded,
        report_deep_link=deep_link,
        validation=validation,
        readiness_version=readiness.get("readiness_version"),
    )


def reproduce_first_report(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    second_actor: str,
    workspace_ref: str | None = None,
) -> RenderedFirstReport:
    """Reproduce the first report for a SECOND user (E36-FR05, independent access).

    The second user's access is re-evaluated INDEPENDENTLY through the strict AD-5
    seam for the second identity (and, when provided, the second workspace). On
    ANY denial we raise ``FirstReportReproductionDenied`` so the caller surfaces a
    single existence-hiding ``not_found`` -- NEVER exposing report existence or the
    first-user result. Authorized -> the SAME publication/provenance render is
    reproducible (same server truth, same versions, same bounded evidence).

    INVARIANT: this is the SAME read/compose path as ``render_first_report`` -- it
    opens no second report engine, mutates nothing, and returns the same shape.
    The reproduction result therefore matches the first user's publication +
    provenance whenever the second user is authorized.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    # INDEPENDENT access evaluation for the SECOND identity (fail closed). Any
    # failure (not a member, no grant, foreign resource, DB error) -> denied. We
    # never disclose whether the datastream/report exists.
    try:
        decision = resolve_strict_resource_access(
            second_actor, conn, project_id=project_id, minimum_capability="view"
        )
    except Exception as exc:  # noqa: BLE001 -- fail closed, existence-hiding.
        logger.debug(
            "first_report_render: reproduce access check failed ds=%s: %s",
            datastream_id,
            type(exc).__name__,
        )
        raise FirstReportReproductionDenied("access_denied") from exc
    if not decision.allowed or not decision.org_id:
        raise FirstReportReproductionDenied("access_denied")

    # (Optional) workspace binding: when a second WORKSPACE ref is supplied it must
    # resolve to an approved bound host connection; an unbound/foreign workspace is
    # denied WITHOUT disclosing the report. Absent 36.14 the bound-host lookup
    # returns a safe fallback (no ui, no workspace) -> a supplied workspace_ref that
    # does not match is denied. A None workspace_ref reproduces the read directly.
    if workspace_ref is not None:
        host_ctx = _bound_host_connection(conn, project_id=project_id)
        bound_ws = host_ctx.get("workspace_id")
        if not bound_ws or str(bound_ws) != str(workspace_ref):
            raise FirstReportReproductionDenied("workspace_not_approved")

    # Authorized -> the SAME render (same publication/provenance/bounded evidence).
    try:
        return render_first_report(
            conn,
            datastream_id=datastream_id,
            project_id=project_id,
            actor=second_actor,
        )
    except FirstReportRenderUnavailable:
        # If readiness is not renderable for the reproduction, that is NOT an
        # access leak -- but we must not disclose report existence to an
        # unauthorized caller. The caller has already passed the access check
        # above, so re-raise as unavailable (the authorized second user sees the
        # honest "not renderable", the unauthorized one never reaches here).
        raise
