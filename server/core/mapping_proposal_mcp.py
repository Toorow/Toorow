"""toorow -- Agentic mapping-proposal MCP surface (Story 36.17, Epic 36).

Exposes the agentic mapping-correction tools through ``register(mcp)``:

  * ``inspect_mapping``           -- Operations, READ. Explain a mapping issue with
                                     SCHEMA/HASHES/COUNTS only; NEVER raw rows.
  * ``propose_mapping_correction``-- Operations, WRITE, confirmation_mode="human".
                                     Persist an immutable NON-LIVE proposal. The live
                                     pointer NEVER advances (Story 36.18 publishes).
  * ``test_mapping_candidate``    -- Operations, WRITE, confirmation_mode="server".
                                     Run bounded candidate gate checks (Autonomous
                                     mode only) WITHOUT persisting or publishing.

INVARIANTS (E36-FR07, E36-NFR01, E36-NFR05)
-------------------------------------------
* Provider schema/values/samples are UNTRUSTED. Every tool output carries schema,
  hashes, counts and synthetic/bounded-redacted evidence -- NEVER raw provider rows.
  ``inspect_mapping`` runs the same redaction discipline as datastream_diagnosis
  (only names/types/hashes/counts survive).
* The mode gate (Advisory=explain / Delegated=persist / Autonomous=test) is enforced
  from the tool + policy: Advisory reaches only ``inspect_mapping``; persisting a
  proposal requires Delegated/Autonomous; a bounded test requires Autonomous.
* Fail-closed access: every tool resolves the strict AD-5 seam
  (``resolve_strict_resource_access``) -- reads need ``view``, writes need ``edit``.
  A foreign/absent Datastream is ``not_found`` (existence-hiding, E36-NFR02).
* The persist path routes through ``mapping_versions.create_mapping_proposal`` which
  routes through ``operations.execute_operation`` (atomic audit/outbox, AD-28) and
  issues ZERO pointer UPDATEs.

Mirrors metric_semantics_mcp: ``_identity`` / ``_tool_error`` / ``_result`` /
``_envelope``, lazy ``core.*`` imports inside function bodies (no cycle), French
ToolError microcopy, ASCII-only. Registered via ``mcp_profiles.register_profiled``
so the tools carry their capability declaration and pass ``validate_catalog``.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"


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


def _agentic_mode() -> str:
    """Resolve the agentic mode from the MCP capability context, defaulting closed.

    The mode arrives from the server-verified capability context claim
    (``capability_context.agentic_mode``), never from a tool argument the model
    controls. Absent a bound context, the mode defaults to ``advisory`` (the most
    restrictive: explain only). This is the policy source for the mode gate.
    """
    from core.mapping_versions import MODE_ADVISORY  # noqa: PLC0415

    try:
        from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

        token = get_access_token()
        if token is None:
            return MODE_ADVISORY
        context = (token.claims or {}).get("capability_context")
        if isinstance(context, dict):
            mode = context.get("agentic_mode")
            if isinstance(mode, str) and mode.strip():
                return mode.strip().lower()
    except Exception as exc:  # noqa: BLE001 -- fail closed to advisory.
        logger.debug("mapping_proposal_mcp: mode resolution failed (%s)", exc)
    return MODE_ADVISORY


def _result(summary: str, data: dict):
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


# ---------------------------------------------------------------------------
# Access guard -- MIRRORS the strict AD-5 seam. Existence-hiding on denial.
# ---------------------------------------------------------------------------


def _guard_datastream(datastream_id: str, identity: str, *, minimum_capability: str) -> str:
    """Return the effective org_id or raise not_found (existence-hiding).

    Wraps ``project_access.resolve_strict_resource_access`` on the Datastream scope.
    Reads pass ``view``; writes pass ``edit``. Denial for ANY reason (not a member,
    no grant, foreign resource, guard-evaluation failure/DB down) surfaces a single
    ``not_found`` -- we NEVER disclose whether the Datastream exists (E36-NFR02).
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                datastream_id=datastream_id,
                minimum_capability=minimum_capability,
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed (never unguarded).
        logger.error("mapping_proposal_mcp: access guard failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("not_found", "Flux introuvable.") from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("not_found", "Flux introuvable.")
    return str(decision.org_id)


# ---------------------------------------------------------------------------
# Redaction -- provider schema/values/samples are UNTRUSTED. Only names/types/
# hashes/counts survive into MCP/model output (E36-NFR01). PURE + offline-testable.
# ---------------------------------------------------------------------------


def redact_schema_evidence(field_records: list) -> dict:
    """Reduce a (possibly hostile) provider schema into safe schema-only evidence.

    Accepts the raw provider field universe (which may smuggle sample rows, values,
    PII, tokens under arbitrary keys). Returns ONLY: per-field ``{field_id, type,
    kind}`` (bounded tokens), the deterministic source-schema hash, and counts.
    Provider sample values, observed rows, and any non-allowlisted key are DROPPED
    -- they never reach the model/MCP output. Deterministic (fields sorted by id).
    """
    from core.datastream_field_mapping import compute_source_schema_hash  # noqa: PLC0415

    safe_fields = []
    for record in field_records or []:
        if not isinstance(record, dict):
            continue
        fid = record.get("field_id") or record.get("name") or record.get("id")
        if not isinstance(fid, str) or not fid:
            continue
        physical_type = record.get("physical_type") or record.get("data_type") or "unknown"
        kind = record.get("kind") or "dimension"
        safe_fields.append(
            {
                "field_id": fid[:128],
                "type": (str(physical_type) or "unknown")[:64],
                "kind": (str(kind) or "dimension")[:32],
            }
        )
    safe_fields.sort(key=lambda item: item["field_id"])
    return {
        "fields": safe_fields,
        "field_count": len(safe_fields),
        "source_schema_hash": compute_source_schema_hash(list(field_records or [])),
    }


def _safe_checks(checks) -> list:
    """Clamp gate-check records to the allowlisted keys (no free-form detail leak)."""
    out = []
    for c in checks or ():
        if not isinstance(c, dict):
            continue
        out.append(
            {
                "check": str(c.get("check"))[:32],
                "passed": bool(c.get("passed")),
                "code": (str(c["code"])[:64] if c.get("code") is not None else None),
            }
        )
    return out


# ---------------------------------------------------------------------------
# register(mcp) -- define each handler locally + register_profiled.
# ---------------------------------------------------------------------------


def register(mcp) -> None:
    """Register the agentic mapping-proposal MCP tools on *mcp* (Story 36.17).

    Called once from core.main. Each tool is registered via
    ``mcp_profiles.register_profiled`` so it carries its capability declaration
    (profile/effect/data_class/confirmation_mode) and passes ``validate_catalog``.
    The three tools all belong to the ``operations`` profile: two writes
    (propose/test) and one read (inspect).
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # ---- Read: inspect a mapping issue (schema/hashes/counts only) --------
    def inspect_mapping(datastream_id: str, mapping_version_id: str | None = None):
        """Inspecte un probleme de mapping : SCHEMA / HASHES / COMPTES uniquement.

        Compose la version de mapping courante (ou *mapping_version_id* si fourni)
        en preuve SANITISEE : liste des champs (field_id, type, role), classe de
        blocage/ambiguite, comptes, hash de contenu et hash de schema source. Le
        schema/les valeurs/les echantillons du provider sont des DONNEES NON FIABLES
        (E36-NFR01) : la sortie ne contient JAMAIS de lignes brutes -- seulement des
        noms/types/hash/comptes. Guard strict AD-5 (view) ; flux etranger ->
        not_found. Aucun effet de bord (lecture pure). Disponible dans les trois
        modes (Advisory peut expliquer).
        """
        datastream_id = (datastream_id or "").strip() or None
        mapping_version_id = (mapping_version_id or "").strip() or None
        if datastream_id is None:
            raise _tool_error("missing_param", "datastream_id est requis.")
        identity = _identity()
        _guard_datastream(datastream_id, identity, minimum_capability="view")

        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        try:
            from core.datastream_field_mapping import (  # noqa: PLC0415
                get_mapping_version,
                list_mapping_versions,
            )
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                # We need the project scope for the version read; resolve it from
                # the datastream row (the strict guard already confirmed access).
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT project_id, current_mapping_version_id "
                        "FROM app.datastreams WHERE id = %s",
                        (datastream_id,),
                    )
                    row = cur.fetchone()
                if not row or not row[0]:
                    raise _tool_error("not_found", "Flux introuvable.")
                project_id = row[0]
                target = mapping_version_id or row[1]
                if not target:
                    versions = list_mapping_versions(datastream_id, project_id, conn)
                    version = versions[0] if versions else None
                else:
                    version = get_mapping_version(datastream_id, project_id, target, conn)
        except ToolError:  # re-raise our own ToolError untouched
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("mapping_proposal_mcp: inspect failed ds=%s: %s", datastream_id, exc)
            raise _tool_error("server_error", "Erreur serveur.") from exc

        if version is None:
            data = {
                "datastream_id": datastream_id,
                "mapping_version": None,
                "note": "Aucune version de mapping courante.",
            }
            return _result("Aucune version de mapping courante pour ce flux.", data)

        # Build sanitized evidence from the persisted payload (already structural,
        # already schema-only). We echo names/types/roles/statuses + counts + hashes.
        payload = version.get("mapping_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        fields = []
        blocking = 0
        for f in payload.get("fields") or []:
            if not isinstance(f, dict):
                continue
            binding = f.get("binding") or {}
            status = binding.get("status")
            if status == "blocking":
                blocking += 1
            fields.append(
                {
                    "field_id": str(f.get("field_id"))[:128],
                    "type": str(f.get("physical_type") or "unknown")[:64],
                    "role": str(
                        (f.get("suggestion") or {}).get("semantic_role") or "dimension"
                    )[:64],
                    "binding_status": (str(status)[:32] if status else None),
                    "blocking_reason": (
                        str(binding.get("blocking_reason"))[:64]
                        if binding.get("blocking_reason")
                        else None
                    ),
                }
            )
        fields.sort(key=lambda item: item["field_id"])
        ambiguities = [
            {"code": str(a.get("code"))[:64]}
            for a in (payload.get("ambiguities") or [])
            if isinstance(a, dict)
        ]
        data = {
            "datastream_id": datastream_id,
            "mapping_version": {
                "id": version.get("id"),
                "version_number": version.get("version_number"),
                "content_hash": version.get("content_hash"),
                "source_schema_hash": version.get("source_schema_hash"),
                "executable": version.get("executable"),
                "blocking_count": blocking,
                "field_count": len(fields),
                "fields": fields,
                "ambiguities": ambiguities,
            },
        }
        summary = (
            f"Mapping v{version.get('version_number')} du flux : "
            f"{len(fields)} champ(s), {blocking} bloquant(s), "
            f"{len(ambiguities)} ambiguite(s)."
        )
        return _result(summary, data)

    # ---- Write: persist an immutable NON-LIVE proposal --------------------
    def propose_mapping_correction(
        datastream_id: str,
        mapping_payload: dict,
        plan_version_id: str,
        idempotency_key: str,
    ):
        """Cree une PROPOSITION de mapping immuable et NON-LIVE (Story 36.17).

        Delegue a ``mapping_versions.create_mapping_proposal`` : normalise+compile le
        mapping (moteur 12.3), calcule content_hash + source_schema_hash, execute les
        controles (compile/semantic/sensitivite/autorisation/DQ/cardinalite/cout), et
        insere UNE version de mapping IMMUABLE sans jamais avancer
        current_mapping_version_id (la publication appartient a la Story 36.18). Si un
        controle echoue -> candidat REJETE, pointeur figé, controles renvoyes. Si tout
        passe -> proposition 'ready' (non-live) routee vers l'ecran de revue gouverne.
        Le mode agentique doit permettre 'persist' (Delegated/Autonomous ; Advisory ->
        refuse). Guard strict AD-5 (edit) ; flux etranger -> not_found. Le
        schema/valeurs/echantillons du provider restent NON FIABLES : la sortie ne
        porte que schema/hash/comptes.
        """
        datastream_id = (datastream_id or "").strip() or None
        plan_version_id = (plan_version_id or "").strip() or None
        idempotency_key = (idempotency_key or "").strip() or None
        if datastream_id is None:
            raise _tool_error("missing_param", "datastream_id est requis.")
        if plan_version_id is None:
            raise _tool_error("missing_param", "plan_version_id est requis.")
        if idempotency_key is None:
            raise _tool_error("missing_param", "idempotency_key est requis.")
        if not isinstance(mapping_payload, dict):
            raise _tool_error("invalid_param", "mapping_payload doit etre un objet.")

        identity = _identity()
        mode = _agentic_mode()

        from core.mapping_versions import (  # noqa: PLC0415
            MappingProposalModeDenied,
            MappingProposalRejected,
            MappingProposalUnavailable,
            create_mapping_proposal,
            mode_permits,
        )

        # Mode gate BEFORE any work: Advisory may not persist.
        if not mode_permits(mode, effect="persist"):
            raise _tool_error(
                "mode_denied",
                "Le mode agentique actuel ne permet que d'expliquer, pas de proposer.",
            )

        org_id = _guard_datastream(datastream_id, identity, minimum_capability="edit")

        try:
            from core.db import get_connection  # noqa: PLC0415
            from core.dq_api import fetch_dq_report_data  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT project_id FROM app.datastreams WHERE id = %s",
                        (datastream_id,),
                    )
                    prow = cur.fetchone()
                if not prow or not prow[0]:
                    raise _tool_error("not_found", "Flux introuvable.")
                project_id = prow[0]
                # DQ evidence: sanitized counts only (E36-NFR01), fail-closed on error.
                try:
                    dq_evidence = fetch_dq_report_data(project_id, conn)
                except Exception as exc:  # noqa: BLE001 -- fail-closed: DQ check rejects.
                    logger.warning("mapping_proposal_mcp: DQ evidence unavailable: %s", exc)
                    dq_evidence = None

                result = create_mapping_proposal(
                    conn,
                    datastream_id=datastream_id,
                    project_id=project_id,
                    effective_org_id=org_id,
                    mapping_payload=mapping_payload,
                    plan_version_id=plan_version_id,
                    actor=identity,
                    mode=mode,
                    idempotency_key=idempotency_key,
                    authorized=True,  # the strict guard above already granted edit
                    dq_evidence=dq_evidence,
                    cost_estimate={"estimated_units": 0},
                )
                conn.commit()
        except MappingProposalRejected as exc:
            # A gate check failed: the candidate is rejected and the pointer never
            # advanced. Return the structured checks (safe -- codes only), NOT raw
            # provider evidence. isError with a stable code.
            data = {
                "datastream_id": datastream_id,
                "state": "rejected",
                "checks": _safe_checks(exc.checks),
                "pointer_advanced": False,
            }
            logger.info("mapping_proposal_mcp: proposal rejected ds=%s", datastream_id)
            raise _tool_error(
                "candidate_rejected",
                "Candidat rejete : un controle a echoue ; le mapping publie est inchange.",
            ) from _StructuredRejection(data)
        except MappingProposalModeDenied as exc:
            raise _tool_error(
                "mode_denied",
                "Le mode agentique actuel ne permet pas de proposer.",
            ) from exc
        except MappingProposalUnavailable as exc:
            raise _tool_error("server_error", "Enregistrement impossible.") from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("mapping_proposal_mcp: propose failed ds=%s: %s", datastream_id, exc)
            raise _tool_error("server_error", "Erreur serveur.") from exc

        data = {
            "datastream_id": datastream_id,
            "proposal_id": result.proposal_id,
            "mapping_version_id": result.mapping_version_id,
            "version_number": result.version_number,
            "state": result.state,
            "executable": result.executable,
            "content_hash": result.content_hash,
            "source_schema_hash": result.source_schema_hash,
            "checks": _safe_checks(result.checks),
            "operation_id": result.operation_id,
            "non_live": True,
            "pointer_advanced": False,
            "next": "governed_review",
        }
        summary = (
            f"Proposition {result.proposal_id} enregistree (v{result.version_number}, "
            f"etat 'ready', NON-LIVE) ; routee vers la revue gouvernee (Story 36.18). "
            f"Le mapping publie reste inchange."
        )
        return _result(summary, data)

    # ---- Write: run bounded candidate gate checks (no persistence) --------
    def test_mapping_candidate(datastream_id: str, mapping_payload: dict, plan_version_id: str):
        """Teste un candidat de mapping (controles bornes) SANS persister ni publier.

        Reserve au mode Autonomous-within-policy. Normalise+compile le candidat et
        execute les MEMES controles que la proposition (compile/semantic/sensitivite/
        autorisation/DQ/cardinalite/cout) puis renvoie le verdict par controle --
        AUCUNE version n'est inseree, AUCUN pointeur n'avance. Guard strict AD-5
        (edit) ; flux etranger -> not_found. Le schema/valeurs/echantillons du
        provider restent NON FIABLES : la sortie ne porte que codes/hash/comptes.
        """
        datastream_id = (datastream_id or "").strip() or None
        plan_version_id = (plan_version_id or "").strip() or None
        if datastream_id is None:
            raise _tool_error("missing_param", "datastream_id est requis.")
        if plan_version_id is None:
            raise _tool_error("missing_param", "plan_version_id est requis.")
        if not isinstance(mapping_payload, dict):
            raise _tool_error("invalid_param", "mapping_payload doit etre un objet.")

        identity = _identity()
        mode = _agentic_mode()

        from core.mapping_versions import mode_permits, run_gate_checks  # noqa: PLC0415

        if not mode_permits(mode, effect="test"):
            raise _tool_error(
                "mode_denied",
                "Le test de candidat n'est autorise qu'en mode Autonomous-within-policy.",
            )

        org_id = _guard_datastream(datastream_id, identity, minimum_capability="edit")

        # Compile the candidate in-memory (no DB write).
        structural_ok = True
        try:
            from core.datastream_field_mapping import (  # noqa: PLC0415
                DatastreamMappingStructuralError,
                compute_source_schema_hash,
                normalize_mapping,
                project_ossie,
            )

            normalized, content_hash = normalize_mapping(mapping_payload)
            normalized["plan_version_id"] = plan_version_id
            normalized, content_hash = normalize_mapping(normalized)
            project_ossie(normalized)
            source_schema_hash = compute_source_schema_hash(list(normalized.get("fields") or []))
        except (DatastreamMappingStructuralError, ValueError) as exc:
            logger.info("mapping_proposal_mcp: candidate compile failed: %s", exc)
            structural_ok = False
            normalized = {"fields": [], "grain": [], "ambiguities": []}
            content_hash = None
            source_schema_hash = None

        # DQ evidence (sanitized counts only), fail-closed.
        dq_evidence = None
        try:
            from core.db import get_connection  # noqa: PLC0415
            from core.dq_api import fetch_dq_report_data  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT project_id FROM app.datastreams WHERE id = %s",
                        (datastream_id,),
                    )
                    prow = cur.fetchone()
                if prow and prow[0]:
                    dq_evidence = fetch_dq_report_data(prow[0], conn)
        except Exception as exc:  # noqa: BLE001 -- fail-closed: DQ check rejects.
            logger.warning("mapping_proposal_mcp: test DQ evidence unavailable: %s", exc)

        checks = run_gate_checks(
            normalized,
            authorized=True,
            structural_ok=structural_ok,
            dq_evidence=dq_evidence,
            cost_estimate={"estimated_units": 0},
        )
        all_pass = all(c["passed"] for c in checks)
        data = {
            "datastream_id": datastream_id,
            "org_id": org_id,
            "would_pass": all_pass,
            "checks": _safe_checks(checks),
            "content_hash": content_hash,
            "source_schema_hash": source_schema_hash,
            "persisted": False,
            "pointer_advanced": False,
        }
        verdict = "tous les controles passent" if all_pass else "controle(s) en echec"
        summary = (
            f"Test candidat : {verdict} "
            f"(aucune version inseree, aucun pointeur avance)."
        )
        return _result(summary, data)

    register_profiled(
        mcp,
        inspect_mapping,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        propose_mapping_correction,
        profile="operations",
        effect="write",
        data_class="sensitive",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        test_mapping_candidate,
        profile="operations",
        effect="write",
        data_class="operational",
        confirmation_mode="server",
    )


class _StructuredRejection(Exception):
    """Carries the safe structured rejection payload alongside the ToolError.

    Attached as the ``__cause__`` of the ``candidate_rejected`` ToolError so a test
    (or a host that inspects the cause) can read the per-check verdicts without the
    codes ever leaking raw provider evidence. The user-facing message stays generic.
    """

    def __init__(self, data: dict):
        self.data = data
        super().__init__("candidate_rejected")
