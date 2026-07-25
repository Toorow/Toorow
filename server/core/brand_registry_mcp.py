"""toorow -- Brand-registry admin MCP surface (Story 40.5, Epic 40).

The LLM SURFACE of Epic 40: the tracked-brand registry made operable by the admin
LLM in natural language -- create/edit an entity, curate aliases, wire a per-source
binding, confirm/correct an inbound match, approve a new-entity alert -- as MCP tools
registered UNDER the existing ``governance`` capability profile (Story 36.11). This
module WRAPS the stores landed by 40.1-40.3; it reimplements NO store logic, NO
confidentiality guard, NO governance workflow, and NO migration. It is the exact
analogue of ``metric_semantics_mcp`` (Story 27.6) for Epic 40, registered the way
``governance_mcp`` (Story 36.18) registers its publication tools.

INVARIANTS (E40-FR08, E40-FR09, E40-NFR01/03/04/05, E40-AD1/AD5)
---------------------------------------------------------------
* Every tool is registered via ``mcp_profiles.register_profiled(profile="governance",
  ...)`` so the 36.11 ``CapabilityProfileMiddleware`` HIDES it from discovery unless a
  verified governance opt-in is present (endpoint binding + 64-hex workspace evidence +
  ``TOOROW_MCP_HIGHRISK_ENABLED``) AND DENIES a direct call to it even when it was never
  listed (the fail-closed-if-hidden guarantee, AC6). 40.5 adds NO enforcement -- it
  inherits both gates by registering the tool in the profiled registry.
* Read tools are ``effect="read"``/``confirmation_mode="none"``; every mutation tool is
  ``effect="write"``/``confirmation_mode="human"`` -- the AI proposes, a human ratifies
  (E40-AD5). No autonomous silent write.
* Authorization is THIS surface's job (40.1 TRUST CONTRACT S-3): every mutation guards
  the caller's org (``_guard_org_manage``, fail-closed forbidden) BEFORE delegating; the
  store's existence-hiding primitives (``assert_entity_in_org`` / ``CrossProjectDenied``)
  are the second, defence-in-depth line. Cross-org access-by-id is existence-hiding
  (``not_found``, never 403 -- IDOR lesson 27.2 F-1).
* Provenance = the token identity (``_identity()``) passed as ``created_by``/``identity``
  to every store mutation so the append-only audit (049, written by the 40.x store in its
  OWN transaction) stamps the actor (E40-NFR04). 40.5 writes NO audit itself.
* Source-agnostic (E40-NFR03, AD-2): ZERO provider name, ZERO brand name in this module.
  A ``source`` handle arrives as OPAQUE org data; the only vocabulary is the ``ROLE_*``
  constants IMPORTED from 40.1 and the decision literals (approve / alias / negative
  alias).

Mirrors ``governance_mcp`` / ``metric_semantics_mcp``: local ``_envelope`` / ``_tool_error``
/ ``_identity`` / ``_result`` helpers (no ``core.main`` coupling), lazy ``core.*`` imports
inside handler bodies (no cycle), French ToolError microcopy, ASCII-only.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

# Decision literals for the mis-match feedback (E40-FR09/AC6). These are the ONLY string
# literals in the module besides the ROLE_* constants imported from 40.1 -- they are
# 40.5's OWN vocabulary (an MCP argument value), not provider/brand vocabulary (AD-2).
_CORRECTION_ALIAS = "alias"
_CORRECTION_NEGATIVE = "negative_alias"
_CORRECTIONS = frozenset({_CORRECTION_ALIAS, _CORRECTION_NEGATIVE})


# ---------------------------------------------------------------------------
# Envelope + error + identity + result helpers (local -- no coupling to main).
# ---------------------------------------------------------------------------


def _envelope(data: dict) -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _result(summary: str, data: dict):
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


def _clean(value: str | None) -> str | None:
    """Strip *value*; an empty/whitespace string collapses to None (param hygiene)."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# ---------------------------------------------------------------------------
# Guard helpers -- MIRROR the REST guards (never a bypass). Source of truth for the
# rights is core.project_access (epic 21); we reuse the SAME helpers 27.2/27.6 use.
# Lifted almost verbatim from metric_semantics_mcp._guard_* (fail-closed).
# ---------------------------------------------------------------------------


def _guard_org_read(org_id: str, identity: str) -> None:
    """Raise not_found (existence-hiding) unless *identity* may READ *org_id*.

    Mirrors metric_semantics_api._require_org_read (identity_has_org_access). A non-member
    of an enrolled org gets 404 -- never disclose the org's existence. A guard-evaluation
    failure (DB down) also 404s: the read must NOT proceed unguarded.
    """
    from core.metric_semantics_api import _require_org_read  # noqa: PLC0415

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _require_org_read(org_id, identity, conn)
    except Exception as exc:  # noqa: BLE001 -- fail-closed on the guard (never unguarded).
        logger.error("brand_registry_mcp: org-read guard failed org=%s: %s", org_id, exc)
        raise _tool_error("not_found", "Organisation introuvable.") from exc
    if not allowed:
        raise _tool_error("not_found", "Organisation introuvable.")


def _guard_org_manage(org_id: str, identity: str) -> None:
    """Raise forbidden unless *identity* is owner/admin of *org_id* (manage capability).

    Mirrors metric_semantics_api._require_org_manage (identity_can_manage_org). A guard
    evaluation failure fails closed (forbidden) -- the mutation must NOT proceed unguarded.
    """
    from core.metric_semantics_api import _require_org_manage  # noqa: PLC0415

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _require_org_manage(org_id, identity, conn)
    except Exception as exc:  # noqa: BLE001 -- fail-closed on the guard (never unguarded).
        logger.error("brand_registry_mcp: org-manage guard failed org=%s: %s", org_id, exc)
        raise _tool_error("forbidden", "Droits insuffisants.") from exc
    if not allowed:
        raise _tool_error("forbidden", "Droits insuffisants.")


def _assert_project_in_org(project_id: str, org_id: str) -> None:
    """F-3/N-1: verify *project_id* belongs to *org_id*, else raise not_found.

    A member of org A must never read/mutate the PROJECT roles of a project of org B.
    Absent/foreign project => not_found (existence-hiding). A lookup failure also 404s.
    """
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.error("brand_registry_mcp: project lookup failed project=%s: %s", project_id, exc)
        raise _tool_error("not_found", "Projet introuvable.") from exc
    if not row or row[0] != org_id:
        raise _tool_error("not_found", "Projet introuvable.")


def _map_store_error(exc: Exception):
    """Map a 40.1-40.3 store typed error to a French ToolError (S-1: constant message out).

    EntityNotFound / AlertNotFound / MatchDecisionNotFound -> not_found (existence-hiding,
    a foreign-org id is indistinguishable from absent). InvalidRole -> invalid_param.
    InvalidSource / CorrectionTargetError -> invalid_param. CrossProjectDenied ->
    cross_project_denied (a typed refusal that does NOT leak the sibling project). Any other
    exception -> server_error, with the detail logged and a constant message returned.
    """
    name = exc.__class__.__name__
    if name in ("EntityNotFound", "AlertNotFound", "MatchDecisionNotFound"):
        return _tool_error("not_found", "Ressource introuvable.")
    if name == "InvalidRole":
        return _tool_error("invalid_param", "Role invalide.")
    if name in ("InvalidSource", "CorrectionTargetError"):
        return _tool_error("invalid_param", "Parametre invalide.")
    if name == "CrossProjectDenied":
        return _tool_error("cross_project_denied", "Ressource d'un autre projet.")
    logger.error("brand_registry_mcp: store error %s: %s", name, exc)
    return _tool_error("server_error", "Operation indisponible.")


# ---------------------------------------------------------------------------
# register(mcp) -- define each handler locally + register_profiled.
# ---------------------------------------------------------------------------


def register(mcp) -> None:
    """Register the brand-registry admin MCP tools on *mcp* (Story 40.5).

    Called once from core.main with the other Epic-36/40 registrations and BEFORE
    ``validate_catalog()`` so the boot validator sees these tools. Every tool is
    registered via ``mcp_profiles.register_profiled`` under ``profile="governance"`` so
    the capability middleware hides it unless a verified governance opt-in is present and
    denies a direct call even when hidden. Read tools are ``effect="read"``/
    ``confirmation_mode="none"``; every mutation tool is ``effect="write"``/
    ``confirmation_mode="human"`` (the AI proposes, a human ratifies -- E40-AD5).
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # =====================================================================
    # Read tools (effect="read", confirmation_mode="none", data_class="sensitive").
    # Ground the admin LLM before it proposes a mutation. Org-scoped only (E40-AD1).
    # =====================================================================

    def registry_entities(org_id: str, status: str | None = None):
        """Liste les entites (marques suivies) d'une organisation (Story 40.5).

        Enumeration ORG-SCOPEE UNIQUEMENT (E40-AD1) : nom canonique + alias + statut.
        Il n'existe PAS d'enumerateur tous-orgs. Garde org-read (organisation etrangere
        -> introuvable). Delegue a tracked_entity_registry.list_tracked_entities_by_org.
        Ancre le modele avant une mutation proposee ; ne mute rien.
        """
        org = _clean(org_id)
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        status_v = _clean(status)
        identity = _identity()
        _guard_org_read(org, identity)

        from core import tracked_entity_registry as reg  # noqa: PLC0415

        try:
            rows = reg.list_tracked_entities_by_org(org, status=status_v)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        summary = f"{len(rows)} entite(s) suivie(s) pour l'organisation {org!r}."
        return _result(summary, {"org_id": org, "entities": rows, "count": len(rows)})

    def registry_project_roles(org_id: str, project_id: str):
        """Liste les entites que CE projet role (own/competitor/reference) (Story 40.5).

        La surface CONFIDENTIELLE (E40-NFR01) : via list_project_roles(project_id),
        scopee au projet appelant. Garde org-read + _assert_project_in_org (projet d'un
        autre org -> introuvable). Ne RETOURNE JAMAIS les marques d'un projet frere.
        """
        org = _clean(org_id)
        project = _clean(project_id)
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if project is None:
            raise _tool_error("missing_param", "project_id est requis.")
        identity = _identity()
        _guard_org_read(org, identity)
        _assert_project_in_org(project, org)

        from core import tracked_entity_registry as reg  # noqa: PLC0415

        try:
            rows = reg.list_project_roles(project)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        summary = f"{len(rows)} role(s) d'entite pour le projet {project!r}."
        return _result(summary, {"project_id": project, "roles": rows, "count": len(rows)})

    def registry_new_entity_alerts(org_id: str):
        """Liste les alertes 'nouvelle entite' OUVERTES de l'organisation (Story 40.5).

        Les chaines de marque entrantes non resolues attendant une ratification humaine,
        via le store 40.3 list_open_alerts(org_id=...). Garde org-read. Lecture seule :
        l'approbation passe par registry_alert_approve (chemin gouverne Epic 13).
        """
        org = _clean(org_id)
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        identity = _identity()
        _guard_org_read(org, identity)

        from core import tracked_entity_matching as match  # noqa: PLC0415

        try:
            rows = match.list_open_alerts(org_id=org)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        summary = f"{len(rows)} alerte(s) 'nouvelle entite' ouverte(s) pour {org!r}."
        return _result(summary, {"org_id": org, "alerts": rows, "count": len(rows)})

    # =====================================================================
    # Mutation tools (effect="write", confirmation_mode="human", data_class="sensitive").
    # Each: param hygiene -> _identity() -> _guard_org_manage -> delegate with the token
    # identity as provenance -> map typed errors -> _result. The AI proposes, a human
    # ratifies (E40-AD5); the store audits in its OWN transaction (E40-NFR04).
    # =====================================================================

    def registry_entity_upsert(
        org_id: str,
        canonical_name: str,
        display_name: str | None = None,
        aliases: list | None = None,
        status: str = "approved",
    ):
        """Cree/edite UNE entite d'organisation (E40-FR08). Confirmation humaine.

        Delegue a tracked_entity_registry.upsert_tracked_entity(created_by=identity) : le
        store audite + estampille l'acteur (E40-NFR04). Idempotent (re-upsert = MEME ligne,
        unicite 40.1 sur (org_id, canonical_name)). Garde org-manage.
        """
        org = _clean(org_id)
        name = _clean(canonical_name)
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if name is None:
            raise _tool_error("missing_param", "canonical_name est requis.")
        status_v = _clean(status) or "approved"
        display = _clean(display_name)
        identity = _identity()
        _guard_org_manage(org, identity)

        from core import tracked_entity_registry as reg  # noqa: PLC0415

        try:
            row = reg.upsert_tracked_entity(
                org_id=org,
                canonical_name=name,
                created_by=identity,
                display_name=display,
                aliases=aliases,
                status=status_v,
            )
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        return _result(f"Entite {name!r} enregistree pour {org!r}.", {"entity": row})

    def registry_alias_add(entity_id: str, org_id: str, alias: str):
        """Ajoute un alias a une entite (additif, E40-FR08). Confirmation humaine.

        Garde org-manage ; assert_entity_in_org(entity_id, org_id) (org etrangere ->
        introuvable) AVANT add_alias(entity_id, alias, identity=identity). Les alias
        NEGATIFS se curent via registry_match_correct (store 40.3), pas ici.
        """
        entity = _clean(entity_id)
        org = _clean(org_id)
        alias_v = _clean(alias)
        if entity is None:
            raise _tool_error("missing_param", "entity_id est requis.")
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if alias_v is None:
            raise _tool_error("missing_param", "alias est requis.")
        identity = _identity()
        _guard_org_manage(org, identity)

        from core import tracked_entity_registry as reg  # noqa: PLC0415

        try:
            reg.assert_entity_in_org(entity, org)
            row = reg.add_alias(entity, alias_v, identity=identity)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        return _result(f"Alias ajoute a l'entite {entity!r}.", {"entity": row})

    def registry_alias_remove(entity_id: str, org_id: str, alias: str):
        """Retire un alias d'une entite (E40-FR08). Confirmation humaine.

        Garde org-manage ; assert_entity_in_org AVANT remove_alias(entity_id, alias,
        identity=identity). Retirer un alias absent est un NO-OP (aucun audit).
        """
        entity = _clean(entity_id)
        org = _clean(org_id)
        alias_v = _clean(alias)
        if entity is None:
            raise _tool_error("missing_param", "entity_id est requis.")
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if alias_v is None:
            raise _tool_error("missing_param", "alias est requis.")
        identity = _identity()
        _guard_org_manage(org, identity)

        from core import tracked_entity_registry as reg  # noqa: PLC0415

        try:
            reg.assert_entity_in_org(entity, org)
            row = reg.remove_alias(entity, alias_v, identity=identity)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        return _result(f"Alias retire de l'entite {entity!r}.", {"entity": row})

    def registry_wire_binding(
        entity_id: str,
        org_id: str,
        source: str,
        external_id: str | None = None,
        query_spec: dict | None = None,
    ):
        """Cable la liaison par-source qui pilote la collecte sortante (E40-FR03/FR08).

        ``source`` est une DONNEE OPAQUE (AD-2 : aucun litteral de provider dans le code).
        Garde org-manage ; assert_entity_in_org ; delegue au store 40.2
        upsert_source_binding(created_by=identity). Entite cross-org -> introuvable ;
        cross-projet -> refus type. Ne LANCE PAS le previsionnel de cout/backfill (c'est
        la Story 40.4) -- ce tool cable la liaison seulement.
        """
        entity = _clean(entity_id)
        org = _clean(org_id)
        source_v = _clean(source)
        if entity is None:
            raise _tool_error("missing_param", "entity_id est requis.")
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if source_v is None:
            raise _tool_error("missing_param", "source est requis.")
        external = _clean(external_id)
        identity = _identity()
        _guard_org_manage(org, identity)

        from core import entity_source_bindings as binding  # noqa: PLC0415
        from core import tracked_entity_registry as reg  # noqa: PLC0415

        try:
            reg.assert_entity_in_org(entity, org)
            row = binding.upsert_source_binding(
                entity_id=entity,
                source=source_v,
                created_by=identity,
                external_id=external,
                query_spec=query_spec,
            )
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        return _result(f"Liaison source cablee pour l'entite {entity!r}.", {"binding": row})

    def registry_binding_remove(binding_id: str, org_id: str):
        """Retire une liaison par-source par id (E40-FR08). Confirmation humaine.

        Garde org-manage ; assert_binding_in_org(binding_id, org_id) (liaison d'une autre
        org -> introuvable) AVANT delete_source_binding(binding_id, identity=identity).
        """
        binding_v = _clean(binding_id)
        org = _clean(org_id)
        if binding_v is None:
            raise _tool_error("missing_param", "binding_id est requis.")
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        identity = _identity()
        _guard_org_manage(org, identity)

        from core import entity_source_bindings as binding  # noqa: PLC0415

        try:
            binding.assert_binding_in_org(binding_v, org)
            removed = binding.delete_source_binding(binding_v, identity=identity)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        return _result(
            f"Liaison {binding_v!r} retiree.", {"binding_id": binding_v, "removed": removed}
        )

    def registry_role_set(entity_id: str, project_id: str, org_id: str, role: str):
        """Attribue le role PROJET-scope (own|competitor|reference) (E40-FR02).

        Garde org-manage + _assert_project_in_org. Delegue a set_entity_project_role(
        created_by=identity). Role invalide -> invalid_param (via InvalidRole du store) ;
        entite.org != projet.org -> cross_project_denied (E40-NFR01).
        """
        entity = _clean(entity_id)
        project = _clean(project_id)
        org = _clean(org_id)
        role_v = _clean(role)
        if entity is None:
            raise _tool_error("missing_param", "entity_id est requis.")
        if project is None:
            raise _tool_error("missing_param", "project_id est requis.")
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if role_v is None:
            raise _tool_error("missing_param", "role est requis.")
        identity = _identity()
        _guard_org_manage(org, identity)
        _assert_project_in_org(project, org)

        from core import tracked_entity_registry as reg  # noqa: PLC0415

        try:
            # Existence-hiding FIRST (like the other 4 mutation tools): a foreign-org entity must
            # collapse to not_found, never leak cross_project_denied vs not_found (F-1 oracle).
            reg.assert_entity_in_org(entity, org)
            row = reg.set_entity_project_role(
                entity_id=entity, project_id=project, role=role_v, created_by=identity
            )
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        return _result(f"Role attribue pour l'entite {entity!r}.", {"role": row})

    def registry_match_confirm(
        org_id: str,
        project_id: str,
        decision_id: str,
        raw_string: str,
        entity_id: str,
    ):
        """Confirme une correspondance entrante auto-assignee (E40-FR04/FR08).

        Reaffirme que ``raw_string`` appartient a ``entity_id`` en le curant comme alias
        de la BONNE entite -- via le store 40.3 correct_match(correct_entity_id=entity_id,
        created_by=identity). Provenance = identite + decision 'alias'. Garde org-manage.
        JAMAIS une ecriture autonome : c'est une invocation confirmee par un humain.
        """
        return _correct_or_confirm(
            org_id=org_id,
            project_id=project_id,
            decision_id=decision_id,
            raw_string=raw_string,
            correction=_CORRECTION_ALIAS,
            entity_id=entity_id,
        )

    def registry_match_correct(
        org_id: str,
        project_id: str,
        decision_id: str,
        raw_string: str,
        correction: str,
        entity_id: str,
    ):
        """Corrige une mauvaise correspondance sous gouvernance (E40-FR09, AC6).

        Reinjecte la chaine fautive soit comme ALIAS de la bonne entite
        (correction='alias'), soit comme alias NEGATIF pour un homonyme
        (correction='negative_alias'), via le store 40.3 correct_match(created_by=
        identity, correct_entity_id|negative_for_entity_id). JAMAIS une ecriture
        silencieuse autonome -- toujours ce tool confirme par un humain. Provenance =
        acteur + decision (E40-NFR04, E40-AD5). Garde org-manage.
        """
        correction_v = _clean(correction)
        if correction_v is None:
            raise _tool_error("missing_param", "correction est requis.")
        if correction_v not in _CORRECTIONS:
            raise _tool_error("invalid_param", "correction doit etre 'alias' ou 'negative_alias'.")
        return _correct_or_confirm(
            org_id=org_id,
            project_id=project_id,
            decision_id=decision_id,
            raw_string=raw_string,
            correction=correction_v,
            entity_id=entity_id,
        )

    def _correct_or_confirm(
        *,
        org_id: str,
        project_id: str,
        decision_id: str,
        raw_string: str,
        correction: str,
        entity_id: str,
    ):
        """Shared body for confirm/correct (both delegate to the 40.3 correct_match).

        A 'confirm' is a correct-to-the-right-entity (kind=alias); a 'correct' routes to
        alias OR negative_alias per the decision. EXACTLY ONE store target is set. Guard
        org-manage; the store existence-hides a foreign-org decision/entity (not_found).
        """
        org = _clean(org_id)
        project = _clean(project_id)
        decision = _clean(decision_id)
        raw = _clean(raw_string)
        entity = _clean(entity_id)
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if project is None:
            raise _tool_error("missing_param", "project_id est requis.")
        if decision is None:
            raise _tool_error("missing_param", "decision_id est requis.")
        if raw is None:
            raise _tool_error("missing_param", "raw_string est requis.")
        if entity is None:
            raise _tool_error("missing_param", "entity_id est requis.")
        identity = _identity()
        _guard_org_manage(org, identity)
        _assert_project_in_org(project, org)

        from core import tracked_entity_matching as match  # noqa: PLC0415

        kwargs: dict = {
            "decision_id": decision,
            "raw_string": raw,
            "project_id": project,
            "identity": identity,
        }
        if correction == _CORRECTION_NEGATIVE:
            kwargs["negative_for_entity_id"] = entity
        else:
            kwargs["correct_entity_id"] = entity

        try:
            result = match.correct_match(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        return _result(
            f"Correspondance corrigee ({correction}) pour la decision {decision!r}.",
            {"decision_id": decision, "correction": correction, "result": result},
        )

    def registry_alert_approve(alert_id: str, org_id: str, canonical_name: str):
        """Approuve une alerte 'nouvelle entite' (E40-FR05/FR08) via le chemin Epic 13.

        Route par le store 40.3 approve_new_entity_alert(identity=identity) : draft ->
        approved, l'entite draft devient 'approved' avec la chaine brute comme premier
        alias. Garde org-manage ; alerte d'une autre org -> introuvable. Provenance =
        approbateur + decision. L'IA propose, cet appel humain ratifie (E40-AD5).
        """
        alert = _clean(alert_id)
        org = _clean(org_id)
        name = _clean(canonical_name)
        if alert is None:
            raise _tool_error("missing_param", "alert_id est requis.")
        if org is None:
            raise _tool_error("missing_param", "org_id est requis.")
        if name is None:
            raise _tool_error("missing_param", "canonical_name est requis.")
        identity = _identity()
        _guard_org_manage(org, identity)

        from core import tracked_entity_matching as match  # noqa: PLC0415

        try:
            result = match.approve_new_entity_alert(
                alert_id=alert, canonical_name=name, identity=identity, org_id=org
            )
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            raise _map_store_error(exc) from exc
        return _result(f"Alerte {alert!r} approuvee (draft -> approved).", result)

    # ---- Register every handler under the governance profile ----
    for handler in (registry_entities, registry_project_roles, registry_new_entity_alerts):
        register_profiled(
            mcp,
            handler,
            profile="governance",
            effect="read",
            data_class="sensitive",
            confirmation_mode="none",
        )
    for handler in (
        registry_entity_upsert,
        registry_alias_add,
        registry_alias_remove,
        registry_wire_binding,
        registry_binding_remove,
        registry_role_set,
        registry_match_confirm,
        registry_match_correct,
        registry_alert_approve,
    ):
        register_profiled(
            mcp,
            handler,
            profile="governance",
            effect="write",
            data_class="sensitive",
            confirmation_mode="human",
        )
