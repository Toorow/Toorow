"""Le controle de portee que tout outil MCP nomme -- story 53.1, CAV-09.

POURQUOI CE MODULE EXISTE, ET PAS UNE FONCTION DE `main.py`. La premiere version
de ce seam vivait dans `core/main.py`, et sa garde structurelle ne balayait que ce
fichier-la. Cinq autres modules de `core/` enregistrent des outils MCP
(`dimension_labels_mcp`, `schedule_mcp`, `stop_run_mcp`, `inbound_mcp`,
`metric_semantics_mcp`, ...) : un outil scope projet enregistre ailleurs
n'attrapait rien. Un seam prive de `main.py` aurait force ces modules a importer
un nom prive d'un fichier de 5 000 lignes -- ce qu'ils n'auraient pas fait. Il vit
donc a un endroit qui est le sien.

TROIS PROPRIETES, ET CHACUNE EST UN DEFAUT DEJA COMMIS ICI.

1. REFUSE, ABSENT ET INDISPONIBLE SONT LA MEME REPONSE. Ce seam leve le
   `project_not_found` canonique de `core.project_resolver` -- exactement l'erreur
   que `_resolve_project` leve deja pour un projet inexistant. La reutiliser est
   le point : un appelant ne peut pas apprendre qu'un projet EXISTE en comparant
   deux refus. Une seconde enveloppe << interdit >> serait un oracle
   d'enumeration portant un mot de securite.

2. IL ECHOUE FERME. `core.main._assert_project_access` rend `True` quand la base
   est injoignable -- posture deliberee et documentee que cette story ne retourne
   pas pour ses appelants existants. Mais un outil qui n'a JAMAIS eu de controle
   ne gagne rien a en recevoir un qui s'ouvre sous charge : le chemin le moins
   cher vers les donnees du voisin serait de faire tomber la base. Celui-ci
   refuse, et une panne ressemble a un projet absent.

3. RIEN N'EST LU AVANT LA REPONSE. La connexion ouverte ici resout
   l'autorisation et se ferme ; l'appelant lit ensuite, sur la sienne -- armee
   elle aussi (`core.db.request_connection`), voir le rappel ci-dessous.

LE PLANCHER EST LA DEUXIEME BARRIERE, PAS LA PREMIERE. `core/db.py` ecrit noir
sur blanc qu'<< armer une AUTRE connexion n'achete rien : le controle
d'autorisation ouvre, utilise et ferme la sienne, puis le handler en ouvre une
fraiche, non armee >>. C'est pour cela que les outils gardes acquierent leur
connexion metier par `core.db.request_connection(identity)` et non par
`get_connection()` : la connexion qui LIT est celle qui porte le contexte.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def refuse_unless_project_scope(
    project_id: str, identity: str, *, minimum_capability: str = "view"
) -> None:
    """Resolve the caller's access to *project_id* BEFORE anything is read.

    Story 53.1 / CAV-09. Nine registered MCP tools of ``core/main.py`` accepted a
    ``project_id`` from the caller and resolved no access at all -- three of them
    writes. Their docstrings said otherwise: ``search_context`` claims "AD-5
    scoped: the caller sees its own project's context plus platform context,
    never another project's", and nothing in its body ever asked.

    ``minimum_capability`` is the whole distinction between a read and a write.
    A write demanded at ``view`` lets a viewer FABRICATE a row in the neighbour's
    project -- which is not a leak of a datum, it is the invention of one. The
    module docstring above carries the three properties this function holds.

    The auth-disabled carve-out is the single local operator, and it is the same
    one ``core.db.install_access_context``, ``core.main._assert_project_access``
    and ``query_specs_api`` already make. It is not a fourth copy of a rule: it
    asks ``core.db`` for it.
    """
    from core import db as _core_db  # noqa: PLC0415
    from core.identity_bridge import canonical_identity_standalone  # noqa: PLC0415
    from core.project_resolver import _raise_not_found  # noqa: PLC0415

    resolved = (identity or "").strip()
    if _core_db._auth_is_disabled() and (not resolved or resolved == "anonymous"):
        return

    # MEMBERSHIP ONLY KNOWS THE CANONICAL IDENTITY. An MCP caller arrives with its
    # raw OIDC subject; `app.org_members.identity` carries `person_<ULID>`.
    # Without this translation this gate refused the OWNER of the organization
    # with `project_not_found`, on an active project -- measured 2026-08-12. The
    # same identity is handed to the acquisition AND to the access decision, so
    # the armed connection and the resolved access cannot be about two different
    # people.
    resolved = canonical_identity_standalone(resolved)

    try:
        from core.project_access import (  # noqa: PLC0415
            resolve_strict_resource_access,
        )

        with _core_db.request_connection(resolved) as _conn:
            decision = resolve_strict_resource_access(
                resolved,
                _conn,
                project_id=project_id,
                minimum_capability=minimum_capability,
            )
    except Exception as _exc:  # fail CLOSED -- see property 2 in the module docstring.
        logger.warning(
            "mcp_scope: access unresolved project=%s: %s", project_id, type(_exc).__name__
        )
        _raise_not_found()
        return

    if not decision.allowed:
        _raise_not_found()


def caller_identity() -> str:
    """The MCP subject, or ``anonymous`` when no token rides the call.

    Same shape as ``recovery_mcp._identity`` and ``dimension_labels_mcp._identity``
    rather than a fifth copy: two tools that named the same person differently
    would produce two audit trails for one act. ``anonymous`` is not a bypass --
    ``refuse_unless_project_scope`` refuses it whenever authentication is on.
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    from core.identity_bridge import canonical_identity_standalone  # noqa: PLC0415

    token = get_access_token()
    if token is None:
        return "anonymous"
    # The OIDC subject is what the TOKEN carries; the canonical identity is what
    # the PRODUCT knows about that person, and it is the only one membership, the
    # RLS floor and the audit trail recognise. Returning the raw subject here
    # would produce two trails for one act -- exactly what the docstring above
    # refuses, one line too early.
    return canonical_identity_standalone(token.claims.get("sub", token.client_id))


#: The refusal an organization-scoped tool answers with, whatever the reason.
#:
#: Same discipline as ``project_not_found`` above, for the same reason: if
#: "you may not" and "it does not exist" wore two envelopes, comparing two
#: refusals would enumerate the organizations of the platform. The message names
#: the GESTURE that repairs -- being invited into the organization -- and never
#: the cause, which is the only half a caller could act on anyway.
ORG_NOT_FOUND_CODE = "organization_not_found"
ORG_NOT_FOUND_MESSAGE = (
    "Organization not found. Check the organization id, or ask one of its owners "
    "or admins to invite you before naming anything there."
)

#: The refusal a platform-scoped WRITE answers with. It is not an existence
#: question -- nobody writes the platform scope through a tool -- so it carries
#: its own envelope and names where the gesture belongs instead.
PLATFORM_SCOPE_CODE = "platform_scope_not_writable"
PLATFORM_SCOPE_MESSAGE = (
    "Platform-scope entries ship with the product and are not written from here. "
    "Name it for your organization or for one of its projects instead."
)

#: The two ranks an organization scope knows, mapped onto the two membership
#: questions `core.project_access` already answers. There is no third: the
#: ratified REST door of the same state (`dimension_lineage_api.py:15-17`) reads
#: with membership and writes with owner/admin, and a rank this seam invented
#: would be a design decision wearing a repair's clothes.
_ORG_RANKS = ("view", "manage")


def refuse_unless_org_scope(
    org_id: str, identity: str, *, minimum_capability: str = "view"
) -> None:
    """Resolve the caller's access to *org_id* BEFORE anything is read or written.

    THE RULE IS NOT INVENTED HERE, IT IS THE ONE THE SIBLING DOOR ALREADY HOLDS.
    ``core/dimension_lineage_api.py`` -- the REST half of the same state -- says
    it in its own module docstring: "Reads require org membership
    (identity_has_org_access), writes require org-manage (owner/admin)". A tool
    that named a dimension for a neighbouring organization without asking either
    question was a SECOND answer to "who may rename here", and the whole point of
    two doors onto one state is that there is only one.

    ``minimum_capability`` is ``view`` for a read and ``manage`` for a write.
    ``edit`` is deliberately absent: organization membership has no such rank --
    `project_access._ORG_MANAGE_ROLES` is owner/admin and everything else is a
    plain member -- so accepting the word would promise a gradation the store
    cannot express.

    It fails CLOSED and it refuses with the same envelope in all three cases
    (denied, absent, unavailable), for the reasons the module docstring gives.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core.identity_bridge import canonical_identity_standalone  # noqa: PLC0415

    if minimum_capability not in _ORG_RANKS:
        raise ValueError(
            f"minimum_capability must be one of {list(_ORG_RANKS)}, not "
            f"{minimum_capability!r}: organization membership has no other rank."
        )

    def _refuse() -> None:
        raise ToolError(f"{ORG_NOT_FOUND_CODE}: {ORG_NOT_FOUND_MESSAGE}")

    resolved = (identity or "").strip()
    if _core_db._auth_is_disabled() and (not resolved or resolved == "anonymous"):
        return

    # An organization is never resolved from an empty id: without this line a
    # caller who simply omitted `org_id` would sail past the gate and reach the
    # store with a scope nobody checked.
    if not (org_id or "").strip():
        _refuse()
        return

    resolved = canonical_identity_standalone(resolved)

    try:
        from core.project_access import (  # noqa: PLC0415
            identity_can_manage_org,
            identity_has_org_access,
        )

        with _core_db.request_connection(resolved) as _conn:
            allowed = (
                identity_can_manage_org(org_id, resolved, _conn)
                if minimum_capability == "manage"
                else identity_has_org_access(org_id, resolved, _conn)
            )
    except Exception as _exc:  # fail CLOSED -- see property 2 in the module docstring.
        logger.warning(
            "mcp_scope: org access unresolved org=%s: %s", org_id, type(_exc).__name__
        )
        _refuse()
        return

    if not allowed:
        _refuse()


def refuse_platform_scope_write() -> None:
    """Refuse a write declared at the ``platform`` scope, and say where it belongs.

    The platform vocabulary is governed with the provider and seeded by
    migrations -- `governance.md:167-169` states it for value tables and
    `dimension_lineage_api.py:250-256` already answers `403` for it on the REST
    door. A tool that accepted the word would be the one surface where a token
    holder could rewrite what EVERY organization reads.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    raise ToolError(f"{PLATFORM_SCOPE_CODE}: {PLATFORM_SCOPE_MESSAGE}")
