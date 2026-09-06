"""Story 6.5 — the core-owned notebook tools: save_notebook / run_notebook.

Notebooks are "living documents": the window rule is stored as a relative string
and resolved against the CURRENT date on every re-run, so each run reflects
today's window. The three bodies moved here whole from ``core.main``; nothing in
them changed but the address.

TWO NAMES COME FROM ``core.main``, AND THEY COME AT CALL TIME.
``_resolve_project`` and ``_loaded_modules`` are the entrypoint's, and they are
imported inside the bodies rather than at the top. Two reasons, and the second
is the one that matters: ``core.main`` imports this module, so a module-level
import back would be a cycle; and twenty-one suites patch
``core.main._resolve_project`` while thirty-five patch
``core.main._loaded_modules``. A module that captured them at import time would
ignore the patch in silence and read production instead. This is the same late
import idiom the rest of ``server/core`` uses for ``core.db``.
"""

from __future__ import annotations

import json
import logging

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core.audit import declare_action
from core.mcp_scope import refuse_unless_project_scope

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_NOTEBOOK_CREATED = declare_action("notebook_created")
ACTION_NOTEBOOK_RUN = declare_action("notebook_run")

# LE CHEMIN `adhoc` A ETE RETIRE AVEC CE QUI LE PRODUISAIT (2026-08-22, 67.23).
#
# `ANALYTICAL_PATH_ADHOC` etait declare ici pour AI-275 / CAV-17 : le run herite
# batissait une enveloppe pour un notebook SANS definition de report, donc une
# figure dont aucune relation ne repondait. La declaration disait cette absence
# honnetement, ce qui etait la bonne reponse a l'epoque.
#
# Le run gouverne n'a pas cette forme. Un bloc epingle une VERSION -- de Query
# Spec ou de Report -- et l'execution passe par `core.query_execution`
# (`accept_execution` / `run_execution`), donc elle est gouvernee et son
# enveloppe est batie la. Il n'existe plus de chemin ad-hoc a declarer, et
# laisser la declaration ferait porter au registre un chemin que plus rien
# n'emprunte : un lecteur y verrait une famille de figures qui n'existe pas.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Story 6.5 — save_notebook / run_notebook: core-owned notebook tools (AC3, AC4).
#
# Notebooks are "living documents": the window_rule is stored as a relative string
# (e.g. 'last_30d') and resolved against the CURRENT date on every re-run.
# This is what makes them living — each run reflects today's data window.
#
# pull_ids stored in notebook_runs are ALWAYS freshly resolved at run time
# (never copied from a previous run) — this is the provenance re-resolution
# proof required by AC7 / AD-9.
#
# envelope_inline size gate: store JSONB inline when < 512KB; else set
# envelope_ref='deferred' (GCS blob storage deferred to Epic 7).
# ---------------------------------------------------------------------------

_ENVELOPE_INLINE_MAX_BYTES = 512 * 1024  # 512KB


def _assert_project_access(project_id: str, identity: str) -> bool:
    """Return True if identity has access to project_id (Story 7.4, AD-5, FR12).

    Delegates to core.project_access.identity_can_read_project against the
    strict Organization membership and Project resource-grant policy.

    Story 46.4 removed `project_access._ALWAYS_ALLOWED`, and this function kept
    importing it -- so every MCP tool body that guards itself here raised
    ImportError instead of deciding. The dev fast path is restored explicitly,
    and tightened while it is: `anonymous` is the subject `api_auth` injects
    when TOOROW_AUTH_MODE=disabled, so it is honoured ONLY in that mode. The old
    check accepted it whatever the mode, which is the default-open the epic
    removed everywhere else.

    Still fails OPEN on DB error / DB-less unit-test mocks, as documented since
    Story 7.4; the hard isolation proof is server/tests/isolation/. That posture
    is deliberate and unchanged here -- flipping it is a product decision, not a
    repair.
    """
    import os as _os  # noqa: PLC0415

    from core.project_access import identity_can_read_project  # noqa: PLC0415

    # Fast path: the disabled-auth dev subject needs NO DB round-trip. It also
    # keeps tool-body connection accounting unchanged for the single-tenant
    # path (a token with a real subject only appears under multi-tenant auth,
    # exercised by the isolation suite against live Postgres).
    auth_mode = _os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if identity == "anonymous" and auth_mode == "disabled":
        return True

    from core import db as _core_db  # noqa: PLC0415

    try:
        # 67-1 (2026-08-24): the identity is a PARAMETER here, so there was
        # never anything to thread -- the acquisition simply did not use it.
        # Arming it puts the Epic-36 floor under the very question being asked.
        with _core_db.request_connection(identity) as _conn:
            return identity_can_read_project(project_id, identity, _conn)
    except Exception as _exc:  # pragma: no cover - resilience path
        logger.debug("assert_project_access: passthrough (db unavailable): %s", _exc)
        return True
def _org_of(conn, project_id: str) -> str | None:
    """L'organisation d'un projet. Une lecture, une requete, aucune invention."""
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _scope_of_notebook(conn, notebook_id: str) -> tuple[str, str] | None:
    """`(org_id, project_id)` d'un Notebook gouverne, ou None.

    La portee est LUE, jamais recue en argument : un appelant qui nommerait le
    projet d'un notebook qui n'est pas le sien ferait resoudre l'acces contre le
    mauvais objet.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, project_id FROM app.analysis_notebooks WHERE id = %s",
            (notebook_id,),
        )
        row = cur.fetchone()
    return (str(row[0]), str(row[1])) if row else None


def save_notebook(
    project_id: str,
    label: str,
    blocks: list,
    description: str = "",
) -> ToolResult:
    """Compose a governed Notebook for a Project. Writes to PostgreSQL.

    CE QUE CET OUTIL ECRIVAIT, ET POURQUOI CE N'EST PLUS CA. Jusqu'au 2026-08-22
    il inserait dans `app.notebooks` -- le magasin HERITE. Les ecrans canoniques
    d'Analyze lisent `app.analysis_notebooks` et ses versions ; le seul lecteur du
    magasin herite est `analyze_artifacts.list_legacy_notebooks`, dont le docstring
    dit qu'il existe pour tenir AC12 << remain readable >> depuis que
    `NotebooksPanel.tsx` n'est monte nulle part. **Un modele creait donc un objet
    reel, audite, et visible par aucun ecran.**

    Mesure de production du 2026-08-22, qui a rendu la bascule decidable sans
    arbitrage : `app.notebooks` 0 ligne, `app.analysis_notebooks` 0 ligne. Rien a
    migrer, et AC12 garde zero ligne.

    ET LA BASCULE EST INDIVISIBLE. Repointer la seule ECRITURE aurait coupe la
    paire entre les deux produits -- on composerait un notebook gouverne qu'on ne
    pourrait plus executer. `run_notebook` bascule dans le meme commit, la porte
    REST heritee cesse d'ecrire, et le tir nocturne herite est retire.

    Le contrat est celui de `docs/product-architecture/analyze-and-test.md`,
    amendement 67.23 -- declare avant d'etre ecrit, et derive de
    `analyze_artifacts._validate_blocks` plutot qu'invente.

    Parameters:
        project_id:  Project identifier.
        label:       The Notebook's name, as a person reads it.
        blocks:      1..100 ordered blocks. Each carries `block_key` (stable,
                     unique, lowercase), `block_type` (`query` | `report` |
                     `narrative`), `as_of_rule` (`current` | `as_of`), and the
                     pinned VERSION its type requires -- `query_spec_version_id`
                     for a query block, `report_version_id` for a report block.
                     A pinned head instead of a version would move under the reader.
        description: Optional. What this Notebook answers.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core.analyze_artifacts import ArtifactRefused, create_notebook  # noqa: PLC0415
    from core.audit import write_audit_row  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    identity = (token.claims.get("sub") or token.client_id) if token else "anonymous"

    project_id = _resolve_project(project_id, identity)

    # AI-269 : c'est une ECRITURE, donc `edit`, et par le seam qui echoue FERME.
    # `_assert_project_access` rendait True quand la base etait injoignable -- le
    # moyen le moins cher de creer une ligne chez le voisin etait de faire tomber
    # la base.
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")

    if not isinstance(blocks, list):
        raise ToolError(
            json.dumps(
                {"code": "invalid_input", "message": "blocks must be a list of block objects."}
            )
        )

    try:
        # LA CONNEXION EST ARMEE, et c'est le SECOND plancher. La garde de portee
        # ci-dessus decide << cet appelant peut-il ecrire dans ce projet >> ; le
        # contexte d'acces decide ce que la connexion elle-meme peut atteindre.
        # `db.py:313-316` mesure le cout d'avoir separe les deux : 113 des 118
        # modules qui lisaient une table sous RLS ne l'armaient jamais. Armer une
        # AUTRE connexion n'achete rien -- celle qui ECRIT est celle qui doit
        # porter le contexte.
        with request_connection(identity) as conn:
            org_id = _org_of(conn, project_id)
            if org_id is None:
                raise ToolError(
                    json.dumps(
                        {"code": "not_found", "message": "Project not found or archived."}
                    )
                )
            version = create_notebook(
                conn,
                org_id=org_id,
                project_id=project_id,
                label=label,
                actor=identity,
                blocks=blocks,
                description=description or None,
            )
            conn.commit()
    except ArtifactRefused as refused:
        # TOUS LES REFUS, PAS LE PREMIER. `_validate_blocks` accumule ses
        # `Refusal(code, message, subject)` avec `subject = "blocks[3]"` : un
        # modele qui compose cent blocs apprend ses cent erreurs en un tour.
        raise ToolError(json.dumps(refused.as_dict())) from None
    except ToolError:
        raise
    except Exception as exc:
        logger.warning("save_notebook: db_write_failed: %s", exc)
        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Database write failed: {exc}"})
        ) from None

    notebook_id = str(version.get("notebook_id") or version.get("id") or "")
    write_audit_row(
        identity=identity,
        action=ACTION_NOTEBOOK_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "notebook_id": notebook_id,
            "notebook_version_id": version.get("id"),
            "project_id": project_id,
            "label": label,
            "block_count": len(blocks),
        },
    )

    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Notebook '{label}' saved with {len(blocks)} block"
                    f"{'' if len(blocks) == 1 else 's'} (id: {notebook_id}, version 1)."
                ),
            )
        ],
    )


def run_notebook_direct(notebook_id: str, as_of: str | None = None) -> dict:
    """Run a governed Notebook and return a result dict. Callable without MCP.

    C'EST LE MEME MOTEUR QUE LA CONSOLE, et c'est tout ce que cette fonction est
    devenue le 2026-08-22. Elle portait auparavant sa propre marche : resoudre
    une `window_rule` plate, rendre un report, batir une narration, INSERT dans
    `app.notebook_runs`. Un second moteur d'execution, sur un second magasin, que
    seul le tir nocturne herite appelait -- et `app.notebooks` porte 0 ligne en
    production, donc il ne tirait sur rien.

    `analyze_artifacts.run_notebook` accepte un Run IDEMPOTENT sur la version
    EXACTE du Notebook : la version est epinglee AVANT qu'un bloc s'execute, donc
    une edition en cours de route ne peut pas changer ce que le Run dit avoir
    joue. Aucun de ces deux acquis n'existait ici.

    `idempotency_key` est OBLIGATOIRE en aval, et il est compose ici a partir de
    ce qui identifie l'acte -- le notebook, la date demandee, et l'instant. Deux
    appels distincts sont deux Runs ; un rejeu du MEME appel rend le Run
    d'origine.
    """
    from ulid import ULID  # noqa: PLC0415

    from core import db as core_db  # noqa: PLC0415
    from core.analyze_artifacts import ArtifactNotFound, ArtifactRefused  # noqa: PLC0415
    from core.analyze_artifacts import run_notebook as _run_governed  # noqa: PLC0415

    requested_as_of = (as_of or "").strip() or None
    # UN ULID, PAS UN HORODATAGE. La premiere version composait la clef avec
    # `strftime("%Y%m%dT%H%M%S%f")` ; deux appels dans la meme microseconde
    # partageaient alors la clef, et le second se faisait servir le Run du
    # premier -- une personne redemande une execution et recoit l'ancienne, sans
    # que rien ne le dise. Mesure : `test_two_calls_of_the_same_notebook_are_two_
    # runs_and_not_one_replayed` etait ROUGE sur deux appels consecutifs.
    stamp = str(ULID())
    with core_db.get_connection() as conn:
        scope = _scope_of_notebook(conn, notebook_id)
        if scope is None:
            raise ArtifactNotFound(f"notebook not found: {notebook_id}")
        org_id, project_id = scope
        try:
            answer = _run_governed(
                conn,
                org_id=org_id,
                project_id=project_id,
                notebook_id=notebook_id,
                actor="scheduler",
                idempotency_key=f"direct:{notebook_id}:{requested_as_of or 'current'}:{stamp}",
                dispatch_source="scheduled",
                requested_as_of=requested_as_of,
            )
        except ArtifactRefused:
            conn.rollback()
            raise
        conn.commit()
    return {**answer, "notebook_id": notebook_id, "project_id": project_id}


def run_notebook(
    notebook_id: str,
    as_of: str = "",
) -> ToolResult:
    """Run a governed Notebook, producing one idempotent Run over its exact version.

    BASCULE DU 2026-08-22 (story 67.23), et elle est indivisible avec celle de
    `save_notebook` : cet outil lisait `app.notebooks` et ecrivait
    `app.notebook_runs` -- le magasin herite, que les ecrans canoniques ne lisent
    pas. Laisser l'un des deux outils sur l'ancien magasin aurait coupe la paire
    entre deux produits.

    LA PORTEE EST LUE, JAMAIS RECUE. Le notebook porte son organisation et son
    projet ; les prendre en argument laisserait un appelant faire resoudre
    l'acces contre le mauvais objet.

    Parameters:
        notebook_id: The `nbk_` id of the governed Notebook to run.
        as_of:       Optional ISO date for as-of replay. Empty = current data.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415
    from ulid import ULID  # noqa: PLC0415

    from core.analyze_artifacts import ArtifactNotFound, ArtifactRefused  # noqa: PLC0415
    from core.analyze_artifacts import run_notebook as _run_governed  # noqa: PLC0415
    from core.audit import write_audit_row  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    token: AccessToken | None = get_access_token()
    identity = (token.claims.get("sub") or token.client_id) if token else "anonymous"
    requested_as_of = (as_of or "").strip() or None
    # UN ULID, pour la raison ecrite dans `run_notebook_direct` : un horodatage
    # a la microseconde collisionne sur deux appels consecutifs, et une clef
    # partagee fait servir un Run ancien pour une demande neuve.
    stamp = str(ULID())

    try:
        # ARMEE, meme raison que `save_notebook` : ce chemin ECRIT un Run.
        with request_connection(identity) as conn:
            scope = _scope_of_notebook(conn, notebook_id)
            if scope is None:
                # UN NOTEBOOK QU'ON N'A PAS LE DROIT DE LIRE EST UN NOTEBOOK QUI
                # N'EXISTE PAS : le meme refus que l'absence, sinon comparer deux
                # refus apprend qu'un objet existe.
                raise ToolError(
                    json.dumps(
                        {"code": "not_found", "message": f"Notebook not found: {notebook_id}"}
                    )
                )
            org_id, project_id = scope
            # `edit`, et la raison est la classification : ce chemin ECRIT un Run
            # et depense le budget d'entrepot du projet. Une ecriture croisee ne
            # fuit pas une donnee, elle en FABRIQUE une.
            refuse_unless_project_scope(project_id, identity, minimum_capability="edit")
            answer = _run_governed(
                conn,
                org_id=org_id,
                project_id=project_id,
                notebook_id=notebook_id,
                actor=identity,
                idempotency_key=f"mcp:{notebook_id}:{requested_as_of or 'current'}:{stamp}",
                dispatch_source="manual",
                requested_as_of=requested_as_of,
            )
            conn.commit()
    except ArtifactNotFound:
        raise ToolError(
            json.dumps({"code": "not_found", "message": f"Notebook not found: {notebook_id}"})
        ) from None
    except ArtifactRefused as refused:
        raise ToolError(json.dumps(refused.as_dict())) from None
    except ToolError:
        raise
    except Exception as exc:
        logger.warning("run_notebook: run_failed nb=%s: %s", notebook_id, exc)
        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Notebook run failed: {exc}"})
        ) from None

    write_audit_row(
        identity=identity,
        action=ACTION_NOTEBOOK_RUN,
        provider_account="",
        connection_ref="",
        metadata={
            "notebook_id": notebook_id,
            "project_id": project_id,
            "run_id": answer.get("run_id"),
            "notebook_version_id": answer.get("notebook_version_id"),
            "as_of": requested_as_of,
        },
    )

    blocks = answer.get("blocks") or []
    replay = " (idempotent replay)" if answer.get("idempotent_replay") else ""
    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Run {answer.get('run_id')} on notebook version "
                    f"{answer.get('notebook_version_id')}: {answer.get('state')}"
                    f", {len(blocks)} block{'' if len(blocks) == 1 else 's'}{replay}."
                ),
            )
        ],
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the two notebook tools on the given FastMCP app.

    ``run_notebook_direct`` is deliberately NOT a tool. Since the AC12 cutover
    (857f1580) it has no production caller — the scheduler dispatches canonical
    notebooks through the governed service — and it stays re-exported by
    ``core.main`` only because test suites patch it at that address.

    Declared (AD-43). Both write: ``save_notebook`` creates the definition and
    ``run_notebook`` "stores the run with fresh pull_ids" -- a run is an evidence
    record, not a computation that vanishes. Neither can therefore be ``insights``,
    which ``_assert_consistent`` has always required to be ``effect=read``; until
    2026-08-12 they reached the default catalog because nobody had declared them
    at all.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        save_notebook,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="host",
    )
    register_profiled(
        mcp,
        run_notebook,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="host",
    )
