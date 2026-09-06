"""The core-owned agent surface: search_context, get_procedure, resolve_business_path.

All three are CORE-owned and registered un-namespaced (AD-2 -- the context layer
is cross-module knowledge, never provider-prefixed). Retrieval and the documented
ranking live in `core.context_search`; these bodies own only the AD-1
dual-channel wrapping -- a <=30-line plain-text summary on the LLM channel plus
the full ranked detail in structuredContent. AD-5 scoping is enforced in the SQL
(platform + the caller's project only), current versions only.

The `from core.main import ...` lines inside the bodies are the same seam every
extracted surface uses: `core.main` imports this module, so a module-level
import back would be a cycle -- and `_resolve_project` and `get_access_token`
are patched by the suites at the `core.main` address, so capturing them at
import time would ignore the patch in silence.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth import AccessToken
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core import tracing

logger = logging.getLogger(__name__)



# NFR1 / AD-1: the search_context summary never exceeds this many lines.
_SEARCH_CONTEXT_MAX_LINES = 30


def _current_identity() -> str | None:
    """The authenticated caller, or None when the host sent no token.

    AI-84: the adherence session key folds this in, so one operator's context
    call cannot make another operator's data query count as adherent. None is
    passed through rather than defaulted to a string, so a genuinely anonymous
    caller degrades to the previous wider bucket instead of every anonymous
    caller sharing one identity.
    """
    from core.main import get_access_token  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    if token is None:
        return None
    return token.claims.get("sub") or token.client_id or None


def _mark_context_consulted(tool_name: str, project_id: str | None = None) -> None:
    """Mark the current session as having consulted context (Story 11.6, AD-18).

    Records that a context tool (``search_context``, ``get_procedure``,
    ``resolve_business_path``, and ``get_knowledge`` from `context_hub_mcp`) ran
    under the current session key (trace parent + time window, scoped by
    ``project_id`` in the time-window branch) so a subsequent data query in the
    SAME session is measured as adherent. Best-effort; never raises into the
    tool path.

    The trace comes from `adherence.current_exchange_trace_id` -- the SAME source
    the data verdict reads (2026-09-01). The two halves used to read the active
    span only, while the AI Path recorder keyed the observed path on the client's
    traceparent; with tracing off, a client that named the exchange on every
    call was still measured by the wall-clock inference.
    """
    try:
        from core import adherence as _adherence  # noqa: PLC0415

        trace_id = _adherence.current_exchange_trace_id()
        _adherence.mark_context_call(
            tool_name,
            trace_id=trace_id,
            project_id=project_id,
            identity=_current_identity(),
        )
    except Exception as _mark_exc:  # noqa: BLE001
        logger.debug("%s: context_mark_skipped: %s", tool_name, _mark_exc)


def _build_search_context_summary(
    query: str, hits: list, project_id: str, *, store_available: bool = True
) -> str:
    """Build the <=30-line plain-text summary for search_context (AD-1).

    One header line + one line per ranked hit, hard-capped at 30 lines total so
    the heavy detail never enters the LLM context (NFR1). A trailing "[+N de
    plus]" line is emitted (still within the cap) when hits are truncated.
    """
    if not store_available:
        # NOT "no context found". The model must be able to tell an outage from
        # an empty store, because the two call for opposite behaviour: one is a
        # reason to say so and stop, the other is a fact about the project.
        return (
            f"The governed context store could not be read, so nothing can be said "
            f"about '{query}'. This is NOT a statement that no context exists: "
            f"treat it as missing evidence, not as an empty project."
        )
    if not hits:
        return f"No context found for '{query}'."

    # Story 45.9 : un resultat ELARGI ne se donne pas pour une reponse directe.
    # Le modele ne peut pas le deviner -- rien dans la ligne d'un resultat ne dit
    # que les mots demandes n'y sont pas tous -- et il ecrirait une reponse
    # ferme sur un rapprochement approximatif.
    widened = sum(1 for hit in hits if getattr(hit, "widened", False))
    header = f"Context for '{query}' — {len(hits)} result(s), most relevant first:"
    if widened:
        header = (
            f"Context for '{query}' — {len(hits)} result(s), most relevant first"
            f" ({widened} matched only SOME of the words):"
        )
    lines = [header]
    # Reserve one line for a possible truncation marker.
    budget = _SEARCH_CONTEXT_MAX_LINES - 1
    shown = 0
    for hit in hits:
        if len(lines) >= budget:
            break
        marker = "" if hit.matched else " (lié)"
        # [title cap] Collapse whitespace in the title so an embedded newline cannot
        # expand ONE ranked entry into many physical lines and break the <=30-line
        # cap (AD-1 / NFR1). The snippet is already single-line (_snippet collapses).
        safe_title = " ".join((hit.title or "").split())
        lines.append(f"[{hit.tier}] {hit.kind}: {safe_title}{marker} — {hit.snippet}")
        shown += 1
    remaining = len(hits) - shown
    if remaining > 0:
        lines.append(f"[+{remaining} more in the detail]")
    # Belt-and-suspenders: never exceed the cap even if arithmetic drifts.
    return "\n".join(lines[:_SEARCH_CONTEXT_MAX_LINES])



def _ai_settings_block(identity: str, project_id: str) -> dict | None:
    """Story 75-4: the resolved AI settings the model must read (`meta.ai_settings`).

    Its own armed connection, opened after the tool's own work, so a failure
    here can neither roll back what the tool wrote nor hide the payload: the
    block is absent, never fabricated (see `ai_settings.envelope_block`).
    """
    from core import db as _core_db  # noqa: PLC0415
    from core.ai_settings import envelope_block_for_project  # noqa: PLC0415

    try:
        with _core_db.request_connection(identity) as conn:
            return envelope_block_for_project(conn, project_id)
    except Exception as exc:  # noqa: BLE001 -- degrade to an absent block
        logger.debug("ai_settings: block unavailable for %s: %s", project_id, exc)
        return None


def search_context(query: str, project_id: str = "default") -> ToolResult:
    """Search the governed context layer -- lean summary + ranked detail (AD-1).

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    Ranks topics, procedures and schema-context docs by relevance to *query*
    (title/name/relation match > body/description match > one-hop graph
    neighbour of a match -- the algorithm is documented in core.context_search).
    Returns a <=30-line plain-text summary on the LLM channel and the full ranked
    list in structuredContent (token-burn split, AD-1 / NFR1). AD-5 scoped: the
    caller sees its own project's context plus platform context, never another
    project's. Current versions only.

    Parameters:
        query:      Free-text search term (a metric name, a concept, a warehouse
                    relation, ...). Blank -> empty result.
        project_id: Project identifier (default: 'default').
    """
    from core import context_search as _ctx_search  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _envelope,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    project_id = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(project_id, identity)

    hits: list = []
    # AI-83 / context-hub.md [5]: "an unavailable context store is
    # indistinguishable from an empty one, so a model reads 'nothing is defined
    # here' and answers from its own priors". This block used to set `hits = []`
    # on any failure, with a comment calling it a resilience path -- and the
    # summary below then said "No context found", which is the sentence the
    # criterion forbids. The store's availability now travels with the result.
    store_available = True
    try:
        with _core_db.request_connection(identity) as _conn:
            hits = _ctx_search.search_context(
                _conn, query=query, project_id=project_id
            )
            # AI-157 : la marche GARDE le sort des candidats ecartes
            # (`record_fates`), et l'acquisition NE COMMITE PAS ce que le
            # handler ecrit -- `request_connection` ne commite qu'a l'ouverture,
            # pour poser le contexte d'acces, et une connexion fermee sur une
            # transaction ouverte rollback. Sans cette
            # ligne le seam serait branche et n'ecrirait rien : exactement le
            # defaut qu'AI-157 repare. Ce `commit` est ici parce que c'est ICI
            # qu'on possede la transaction.
            _conn.commit()
    except Exception as _exc:  # noqa: BLE001 -- degrade, but never as "empty"
        logger.debug("search_context: store_unavailable: %s", _exc)
        hits = []
        store_available = False

    # Story 11.6 (AD-18): mark this session as "context consulted" so a later data
    # query (get_report/get_card/get_daily_report) in the SAME session records as
    # adherent. [F-1] The mark fires ONLY after a successful, NON-EMPTY retrieval:
    # a DB failure / empty search leaves the session non-adherent (mark-before-
    # retrieval would have falsely marked a failed search as "context consulted").
    if hits:
        _mark_context_consulted("search_context", project_id)

    summary = _build_search_context_summary(
        query, hits, project_id, store_available=store_available
    )

    envelope = _envelope(
        {
            "query": query,
            "project_id": project_id,
            "identity": identity,
            "ranking": "title > description > graph_neighbor",
            "results": [h.as_dict() for h in hits],
            "count": len(hits),
            # A consumer reading `count: 0` must be able to ask WHY it is zero.
            "context_store_state": "available" if store_available else "unavailable",
        },
        provenance={
            "source_system": "connector-core",
            "source_field": "search_context",
            "pull_id": None,
        },
        freshness="live",
        ai_settings=_ai_settings_block(identity, project_id),
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )




def get_procedure(name: str, project_id: str = "default") -> ToolResult:
    """Fetch a Skill-Pack procedure by NAME -- frontmatter + body (Story 11.5).

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    A procedure is citable by name. Returns the current (active) version scoped to
    the caller's project, preferring a project-scoped override over the platform
    procedure of the same name (AD-5). The LLM channel carries the procedure name
    + description; the full frontmatter YAML and Markdown body ride
    structuredContent (AD-1). A not-found name returns a stable, NON-DISCLOSING
    result (never reveals whether the name exists in another project's scope).

    A caller without access to *project_id* is REFUSED before anything is read,
    with the same envelope an absent project returns -- the Skill's body is never
    served on a refusal, and comparing two refusals cannot reveal that a project
    exists.

    Parameters:
        name:       The procedure's frontmatter ``name`` (citable identifier).
        project_id: Project identifier (default: 'default').
    """
    from core import context_search as _ctx_search  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _envelope,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    project_id = _resolve_project(project_id, identity)

    # ── The access decision governs THE BODY, not a sub-field of the payload.
    #
    # Until 2026-08-17 this tool resolved access only inside the remarks block:
    # a refusal removed the open remarks and served the Skill anyway -- body,
    # frontmatter, standardized sequence and MDM references -- to anyone who
    # could name the project. The 53.1 conformance guard counted it protected
    # because a guard was IMPORTED in the body; the behaviour was not.
    #
    # REFUSAL, NOT DEGRADATION (audit arbitration of 2026-08-17). The rest of
    # this surface refuses: `search_context` raises through this same seam and
    # `resolve_business_path` answers `denied`. Serving the platform-scoped
    # Skills of a project the caller cannot read would answer "this project
    # exists" -- and the not-found below is deliberately non-disclosing for
    # exactly that reason. The seam raises the canonical `project_not_found`, so
    # refused, absent and unavailable are one answer.
    #
    # IT IS OUTSIDE THE `try` ON PURPOSE: the resilience path below turns any
    # exception into a stable not-found, and a refusal swallowed by it would be
    # a guard that never refuses.
    _refuse_unless_project_scope(project_id, identity)

    proc: dict | None = None
    try:
        mdm = None
        remarks: list[dict] = []
        # ARMED CONNECTION (`core/db.py`): the connection that READS is the one
        # that carries the access context. `get_connection()` here left the
        # row-level floor down on the very read the guard above authorises.
        with _core_db.request_connection(identity) as _conn:
            proc = _ctx_search.get_procedure_by_name(
                _conn, name=name, project_id=project_id
            )
            # Ce que la Skill DESIGNE du modele de donnees : ses `{{champ}}` et
            # ses `mdm_tags`, resolus contre le catalogue gouverne -- le Modele
            # Semantique du Projet d'abord, `app.target_fields` en repli. Sans
            # cela un agent lit « compute {{cost}} per {{country}} » sans savoir
            # de quoi il s'agit -- ni le type, ni la mesure, ni s'il est approuve.
            # RESOLU DANS LA MEME CONNEXION que la lecture : deux connexions
            # rendraient un catalogue d'un autre instant que la Skill.
            if proc is not None:
                from core import mdm_references as _mdm  # noqa: PLC0415

                _front = proc.get("frontmatter_yaml", "")
                mdm = _mdm.resolve(
                    _conn,
                    frontmatter_yaml=_front,
                    body_md=proc.get("body_md", ""),
                    mdm_tags=_mdm.tags_of(_front),
                    project_id=project_id,
                )

                # ── AI-158 : ce qu'on REPROCHE a cette Skill part avec elle.
                #
                # `context-hub.md` : « whatever the MCP tool returns for a
                # Skill, the console shows for that Skill ». La console montre
                # les remarques ouvertes depuis AI-158 ; sans ce bloc, l'agent
                # etait le seul lecteur a qui l'on cachait « cette contrainte
                # n'existe plus » -- et c'est LUI qui applique la procedure.
                # Le modele prevoyait deja qu'une machine en DEPOSE
                # (`origin='agent'`) ; personne n'avait dit qu'elle devait en
                # LIRE.
                #
                # MEME CONNEXION que la lecture de la Skill, pour la meme
                # raison que `mdm` : deux connexions rendraient une file d'un
                # autre instant que la procedure qu'elle commente.
                try:
                    from core import context_review as _review  # noqa: PLC0415
                    from core.project_access import (  # noqa: PLC0415
                        resolve_strict_resource_access as _access,
                    )

                    _acc = _access(
                        identity,
                        _conn,
                        project_id=project_id,
                        minimum_capability="view",
                        hold_access=True,
                    )
                    if _acc.allowed and _acc.org_id:
                        remarks = [
                            {
                                "id": _r["id"],
                                "note": _r["note"],
                                "node_version": _r["node_version"],
                                "origin": _r["origin"],
                                "requested_by": _r["requested_by"],
                                # Le noeud a-t-il AVANCE depuis ? Une remarque
                                # sur une version depassee ne se lit pas comme
                                # une remarque sur celle qu'on vient de rendre,
                                # et l'agent ne peut pas le deduire seul.
                                "speaks_of_current_version": (
                                    _r["node_version"] == proc.get("version_number")
                                ),
                            }
                            for _r in _review.list_open(
                                _conn,
                                org_id=_acc.org_id,
                                project_id=project_id,
                                node_id=proc["id"],
                            )
                        ]
                except Exception as _rex:  # noqa: BLE001 -- la Skill prime
                    # Une file en panne ne doit pas emporter la lecture de la
                    # procedure : l'agent perd un avertissement, pas l'outil.
                    logger.debug(
                        "get_procedure: remarks_skipped: %s", type(_rex).__name__
                    )
                    remarks = []
    except Exception as _exc:  # noqa: BLE001 -- resilience path
        logger.debug("get_procedure: fetch_skipped (db unavailable): %s", _exc)
        proc = None
        mdm = None
        remarks = []

    # Story 11.6 (AD-18): mark this session as "context consulted" (see search_context).
    # [F-1] Only a genuine, found procedure counts: a not-found / DB-down lookup
    # (proc is None) must NOT mark the session adherent.
    if proc is not None:
        _mark_context_consulted("get_procedure", project_id)

    if proc is None:
        # Stable, non-disclosing not-found. Same message whether the name is
        # unknown or lives in another project's scope (no scope leak, AD-5).
        summary = f"Procedure '{name}' not found."
        envelope = _envelope(
            {
                "name": name,
                "project_id": project_id,
                "identity": identity,
                "found": False,
                "procedure": None,
            },
            provenance={
                "source_system": "connector-core",
                "source_field": "get_procedure",
                "pull_id": None,
            },
            freshness="live",
            ai_settings=_ai_settings_block(identity, project_id),
        )
        return ToolResult(
            content=[TextContent(type="text", text=summary)],
            structured_content=envelope,
        )

    # LLM channel: name + one-line description only (the body rides the detail).
    desc = (proc.get("description") or "").strip()
    summary = f"Procedure '{proc['name']}'"
    if desc:
        summary += f" : {desc}"
    # AI-158 : le canal LLM reste BORNE (AD-1) -- les notes rident dans
    # structuredContent, comme le corps. Mais un compte de zero caractere
    # utile est ce qui separe « l'agent pouvait le lire » de « l'agent l'a
    # vu » : une Skill contestee qu'on applique sans le savoir est exactement
    # ce que la file existe pour eviter.
    if remarks:
        _stale = sum(1 for _r in remarks if not _r["speaks_of_current_version"])
        summary += f" — ⚠ {len(remarks)} open remark(s) on this Skill"
        if _stale:
            summary += f" ({_stale} of an older version)"

    # ── Story 45.6 : la forme standardisee, LUE, pour celui qui applique.
    #
    # `structuredContent` portait `frontmatter_yaml` en bloc de texte : la
    # console lisait la sequence et l'agent devait parser du YAML. Ici elle est
    # lue par les MEMES validateurs que l'ecriture (`STANDARD_VALIDATORS`).
    #
    # LE CANAL LLM RESTE BORNE (AD-1), et ne grandit pas avec la sequence : un
    # compte, l'existence d'une branche d'arret, et le fait que la Skill s'exclue
    # elle-meme. Un agent qui a demande cette Skill PAR SON NOM doit apprendre
    # qu'elle se declare hors sujet -- le classement, lui, ne l'a pas vue passer.
    from core.context_store import read_standard_frontmatter as _read_standard  # noqa: PLC0415

    skill = _read_standard(proc.get("frontmatter_yaml", ""))

    # ── Story 45.7 : retenir CE QUI A ETE SERVI, et a quelle version.
    #
    # Le serveur ne voit pas un agent « executer un pas » : il voit une Skill
    # servie, puis des appels d'outils. Le seul pont qui n'invente rien est le
    # pas qui declare `tool: X`, suivi dans la MEME trace d'un appel a X.
    # La version retenue est celle SERVIE -- une Skill qui avance ensuite ne
    # re-etiquette pas une execution deja faite.
    import contextlib as _contextlib  # noqa: PLC0415

    from core import skill_steps as _skill_steps  # noqa: PLC0415
    from core.ai_path_recorder import current_call_trace_id as _call_trace  # noqa: PLC0415

    with _contextlib.suppress(Exception):  # observer never breaks the observed
        _skill_steps.remember_served(
            # ⚠️ L'identifiant DE L'APPEL, pas celui d'OpenTelemetry. Relecture du
            # 2026-08-05 : `tracing.current_trace_id_hex()` rend `None` tant que
            # `TRACING_ENABLED` est faux -- son defaut -- donc ce producteur
            # n'enregistrait RIEN en configuration deployee, pendant que le
            # consommateur lisait le `traceparent` du client. Deux bouts, deux
            # identifiants, aucun pas emis.
            _call_trace(),
            procedure_id=proc["id"],
            version_number=proc.get("version_number"),
            steps=skill["steps"],
            project_id=project_id,
        )
    if not skill["readable"]:
        summary += " — ⚠ this Skill's frontmatter could not be read"
    elif skill["unreadable_keys"]:
        summary += " — ⚠ unreadable: " + ", ".join(skill["unreadable_keys"])
    if skill["steps"]:
        summary += f" — {len(skill['steps'])}-step sequence"
        _stops = sum(1 for _s in skill["steps"] if _s.get("stop_if"))
        if _stops:
            summary += f" ({_stops} stop condition{'s' if _stops > 1 else ''})"
    if skill["anti_triggers"]:
        summary += f" — declares {len(skill['anti_triggers'])} anti-trigger(s)"

    # THE LAST WALK, BEFORE THIS ONE (2026-09-05). Read from the recorded paths of
    # this Project that took this Skill; one sentence in the model channel so the
    # next walk starts from what the previous one skipped. Best effort: an
    # unreadable history serves the Skill exactly as before.
    previous_walks: list[dict] = []
    try:
        from core.ai_paths import previous_walks as _previous_walks  # noqa: PLC0415

        with _core_db.request_connection(identity) as _conn:
            previous_walks = _previous_walks(_conn, project_id=project_id, procedure_id=proc["id"], limit=3)
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_procedure: previous walks unreadable: %s", type(exc).__name__)
        previous_walks = []
    if previous_walks:
        last = previous_walks[0]
        summary += (
            f" -- last walk here ({str(last.get('started_at') or '')[:16]}): "
            + (f"verdict {last['verdict']}, " if last.get("verdict") else "")
            + f"crossed {', '.join(last.get('crossed') or []) or 'nothing'}"
            + (f", skipped {', '.join(last.get('skipped') or [])}" if last.get("skipped") else "")
            + (f" ({'; '.join(last.get('skipped_labels') or [])})" if last.get("skipped_labels") else "")
        )
    envelope = _envelope(
        {
            "name": proc["name"],
            "project_id": project_id,
            "identity": identity,
            "found": True,
            "procedure": {
                "id": proc["id"],
                "name": proc["name"],
                "description": proc.get("description", ""),
                # Le YAML brut RESTE : un client qui le lit aujourd'hui ne doit
                # pas casser parce que la structure est apparue a cote.
                "frontmatter_yaml": proc.get("frontmatter_yaml", ""),
                # Story 45.6 : la meme chose, LUE. `readable` et
                # `unreadable_keys` separent « cette Skill n'a pas de sequence »
                # de « on n'a pas pu la lire » -- une liste vide pour les deux
                # ferait dire au silence ce qu'il ne dit pas.
                "skill": skill,
                "body_md": proc.get("body_md", ""),
                "version_number": proc.get("version_number"),
                "scope": "platform" if proc.get("project_id") is None else "project",
                # Ce que la Skill designe du modele de donnees. Les deux cotes
                # sont rendus : une reference qui NE resout PAS est le retour sur
                # la Skill, et ne la publier pas laisserait croire qu'elle ne
                # cite que des champs existants.
                "mdm_references": mdm,
                # Ce qu'on reproche a cette Skill, ouvert et non tranche. La
                # console montre la meme file pour le meme objet (AI-158) --
                # c'est l'invariant que `context-hub.md` ecrit noir sur blanc.
                "open_remarks": remarks,
            },
            # BESIDE the procedure, not inside it: the procedure block (frontmatter,
            # body) is moved to the app channel by the model-channel guard on a
            # real Skill, and the previous walk must stay where the model reads.
            "previous_walks": previous_walks,
        },
        provenance={
            "source_system": "connector-core",
            "source_field": "get_procedure",
            "pull_id": None,
        },
        freshness="live",
        ai_settings=_ai_settings_block(identity, project_id),
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )




def resolve_business_path(
    target_type: str,
    target_id: str,
    project_id: str = "default",
    taxonomy_id: str | None = None,
) -> ToolResult:
    """Resolve the governed business route to a knowledge, semantic or report view.

    The LLM channel stays bounded. The complete ordered, versioned path is returned
    in structuredContent and persisted under a deterministic path_key for later
    evaluation and reporting.
    """
    from core import business_taxonomy as _taxonomy  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _envelope,
        _resolve_project,
        get_access_token,
    )
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"
    project_id = _resolve_project(project_id, identity)
    trace_id = tracing.current_trace_id_hex() or None
    resolved: dict | None = None
    # Three outcomes the caller must be able to tell apart. Collapsing them into
    # one sentence made an access refusal and a broken database read as the
    # factual absence of a governed route -- and Story 45.1's AC8 then scores that
    # absence as a `missing_path` regression.
    state = "absent"
    try:
        # SAME CLASS AS `get_procedure` (2026-08-17): the connection that READS
        # is the one that must carry the access context. This tool already
        # branches on its refusal, but it read the taxonomy on an unarmed
        # connection -- so the row-level floor, the second barrier, was down.
        with _core_db.request_connection(identity) as conn:
            access = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability="view",
                hold_access=True,
            )
            if not (access.allowed and access.org_id):
                state = "denied"
            else:
                resolved = _taxonomy.resolve_business_path(
                    conn,
                    org_id=access.org_id,
                    project_id=project_id,
                    target_type=target_type,
                    target_id=target_id,
                    taxonomy_id=taxonomy_id,
                    purpose="llm",
                    actor=identity,
                    trace_id=trace_id,
                )
                conn.commit()
    except Exception as exc:  # noqa: BLE001 -- stable non-disclosing MCP result
        logger.debug("resolve_business_path: resolution skipped: %s", type(exc).__name__)
        resolved = None
        state = "unavailable"

    if resolved is None:
        # Non-disclosing on denial -- the wording names the refusal without
        # revealing whether the resource exists.
        summary = {
            "denied": "This project's governed business context is not available to you.",
            "unavailable": (
                "The governed business path could not be resolved. Treat this as unknown, "
                "not as an absence of business context."
            ),
        }.get(state, "No governed business path is available for this resource.")
        data = {
            "found": False,
            "state": state,
            "project_id": project_id,
            "target": {"type": target_type, "id": target_id},
            "path_key": None,
            "ordered_path": [],
        }
    else:
        path_key = resolved["path_key"]
        node_labels = [
            segment.get("title") or segment["id"]
            for segment in resolved["ordered_path"]
            if segment.get("kind") == "node"
        ]
        summary = (
            f"Governed business path: {' > '.join(node_labels)}\n"
            f"Evidence key: {path_key}"
        )
        data = {"found": True, "state": "resolved", "project_id": project_id, **resolved}
        tracing.record_current_span_attributes(
            {
                "context.path_key": path_key,
                "context.path_target_type": target_type,
                "context.path_link_origin": resolved["link_origin"],
            }
        )
        _mark_context_consulted("resolve_business_path", project_id)

    envelope = _envelope(
        data,
        provenance={
            "source_system": "connector-core",
            "source_field": "resolve_business_path",
            "pull_id": None,
        },
        freshness="live",
        ai_settings=_ai_settings_block(identity, project_id),
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )




def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the three agent-surface tools on the given FastMCP app.

    Declared (AD-43): all three are governed-context reads, and until 2026-08-12
    they reached the catalog on plain ``mcp.tool`` -- visible because
    ``_tool_profile`` defaulted an undeclared tool to Insights, not because anyone
    had said they belonged there.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        search_context,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_procedure,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        resolve_business_path,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
