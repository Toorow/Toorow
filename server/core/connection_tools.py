"""Which Connectors one authorization actually opens.

WHY THIS EXISTS
`app.connection_ref.provider` doubles as the Connector name everywhere else in
the codebase, and that identity holds for every Nango connection: one
authorization, one provider, one Connector. It breaks for exactly one path --
Google direct OAuth (AD-21), where a SINGLE consent screen grants seven scopes
reaching seven different Connectors. Those rows are stored with provider='google',
which matches no installed Connector at all, so `get_scoped_source_capabilities`
raised NotFound for every Google connection ever created. The wizard could not
have listed a report even once the connection worked.

So the question "what can I build from this authorization?" has two shapes, and
this module answers both with one call:

    nango          -> the one Connector named by the provider
    google_direct  -> every Connector whose scope Google actually granted

GRANTED, NOT REQUESTED
The set is derived from `granted_scopes` -- what came back from Google -- never
from GOOGLE_STACK_SCOPES, what we asked for. A person can untick products on the
consent screen, and incremental auth means the grant can also grow later. Reading
the request would offer Connectors the token cannot open, and the failure would
only surface at extraction time, on a schedule, hours later.

A Connector the authorization does not open is NOT returned as a disabled entry:
absence of a scope is not a state of that Connector, it is the Connector not
being part of this authorization. What IS returned with `available: false` is a
Connector whose scope was granted but which cannot be used here -- not installed
in this deployment, or not enabled for the project -- because that gap is about
this deployment and is worth stating rather than hiding.

VOCABULARY
`Tool` used to be the name of the dataclass below and of the wizard step. It is
now reserved for MCP tools and nothing else, per
docs/product-architecture/glossary.md: a model reading "tool" must never have to
decide whether it means one of its own callable tools or a data adapter. The
object here is a CONNECTOR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class ConnectionConnectorsNotFound(Exception):
    """The connection is unknown, or out of the caller's scope (fail-closed)."""


class ConnectionConnectorsUnavailable(Exception):
    """Infrastructure or contract failure -- distinct from an empty answer."""


# Which Connector each scope opens. A Connector may need SEVERAL scopes -- the
# map is scope-keyed, so two keys pointing at the same Connector is the normal
# way to say so (youtube-analytics reads two distinct Google APIs).
#
# It was "pinned against GOOGLE_STACK_SCOPES, both ways" -- two hand-written
# constants checking each other. That guard cannot catch what is missing from
# both, and three installed Connectors were (AI-94). The reference is now the
# manifests on disk: tests/conformance/test_google_consent.py asserts that every
# module declaring auth_type='google_direct' is opened by a requested scope, and
# that no requested scope names a Connector this deployment does not install.
GOOGLE_SCOPE_CONNECTORS: dict[str, str] = {
    "https://www.googleapis.com/auth/webmasters.readonly": "gsc",
    "https://www.googleapis.com/auth/analytics.readonly": "google-analytics",
    "https://www.googleapis.com/auth/adwords": "google-ads",
    "https://www.googleapis.com/auth/spreadsheets.readonly": "google-sheets",
    "https://www.googleapis.com/auth/dfareporting": "cm360",
    "https://www.googleapis.com/auth/display-video": "dv360",
    "https://www.googleapis.com/auth/doubleclicksearch": "sa360",
    # AI-285. Sans cette ligne le scope est demande et n'ouvre rien : la
    # connexion existe, et le selecteur d'objet BigQuery reste vide sans dire
    # pourquoi.
    "https://www.googleapis.com/auth/bigquery.readonly": "bigquery",
    # AI-94 -- installes depuis des semaines, ouverts par rien.
    "https://www.googleapis.com/auth/dfp": "google-ad-manager",
    # The SAME product under the name Google now issues. The consent granted to
    # this deployment carries `admanager`, never `dfp`, so the Connector opened
    # by that scope was reachable by nobody -- the AI-94 class again, one
    # spelling later. Both keys stay: an older grant still carries `dfp`.
    "https://www.googleapis.com/auth/admanager": "google-ad-manager",
    "https://www.googleapis.com/auth/business.manage": "google-business-profile",
    "https://www.googleapis.com/auth/yt-analytics.readonly": "youtube-analytics",
    "https://www.googleapis.com/auth/youtube.readonly": "youtube-analytics",
}


@dataclass(frozen=True)
class ConnectionConnector:
    """One Connector reachable through an authorization."""

    connector_name: str
    display_name: str
    #: False when the scope is granted but the Connector cannot serve it here.
    available: bool
    #: Stated only when unavailable, in the console's words, never a stack trace.
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "connector_name": self.connector_name,
            "display_name": self.display_name,
            "available": self.available,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def _connector_display_name(loaded_modules: list[Any], connector_name: str) -> str | None:
    for candidate in loaded_modules:
        if getattr(candidate, "name", None) == connector_name:
            manifest = getattr(candidate, "manifest", None) or {}
            display = manifest.get("display_name") if isinstance(manifest, dict) else None
            return str(display or connector_name)
    return None


def _read_authorization(
    *, project_id: str, connection_ref_id: str, identity: str, conn: Any
) -> tuple[str, str, list[str]]:
    """Return (provider, auth_path, granted_scopes) or refuse, fail-closed.

    Extrait le 2026-08-01 pour que `list_connection_connectors` et
    `list_unopened_connectors` partagent UNE seule lecture. Le motif n'est pas
    l'economie de lignes : c'est un controle d'acces fail-closed, et deux copies
    d'un controle d'acces, c'est deux endroits ou se tromper -- dont un qui peut
    diverger sans que rien ne le dise.

    Unknown, foreign, inactive et desactivee partagent un seul resultat
    not-found : sonder cet endpoint ne revele rien de ce qui existe dans un autre
    projet.
    """
    from core.project_access import (  # noqa: PLC0415
        ProjectAccessUnavailable,
        identity_can_read_project,
    )

    try:
        if not identity_can_read_project(project_id, identity, conn, fail_closed=True):
            raise ConnectionConnectorsNotFound

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.provider, r.auth_path, r.status, r.enabled, r.granted_scopes
                  FROM app.connection_ref r
                 WHERE r.id = %s AND r.project_id = %s
                """,
                (connection_ref_id, project_id),
            )
            row = cur.fetchone()
    except ConnectionConnectorsNotFound:
        raise
    except ProjectAccessUnavailable as exc:
        raise ConnectionConnectorsUnavailable("connection scope is unavailable") from exc
    except Exception as exc:  # noqa: BLE001
        raise ConnectionConnectorsUnavailable("connection scope is unavailable") from exc

    if row is None:
        raise ConnectionConnectorsNotFound
    provider, auth_path, status, enabled, granted_scopes = row[:5]
    if status != "active" or not bool(enabled):
        raise ConnectionConnectorsNotFound
    return str(provider), str(auth_path or "nango"), [str(s) for s in (granted_scopes or [])]


def list_connection_connectors(
    *,
    project_id: str,
    connection_ref_id: str,
    identity: str,
    loaded_modules: list[Any],
    conn: Any,
) -> list[ConnectionConnector]:
    """Return the Connectors this authorization opens, ordered for display.

    Fail-closed exactly like `get_scoped_source_capabilities`: unknown, foreign,
    and inactive connections share one not-found result, so probing this endpoint
    reveals nothing about what exists in another project.
    """

    project_id = (project_id or "").strip()
    connection_ref_id = (connection_ref_id or "").strip()
    if not project_id or not connection_ref_id:
        raise ValueError("project_id and connection_ref_id are required")

    from core.module_enablement import (  # noqa: PLC0415
        ModuleEnablementUnavailable,
        is_module_enabled,
    )

    provider, auth_path, granted_scopes = _read_authorization(
        project_id=project_id,
        connection_ref_id=connection_ref_id,
        identity=identity,
        conn=conn,
    )

    if auth_path == "google_direct":
        # Deduplicated: two scopes never map to one Connector today, but the set
        # is ordered by the map so the list a person sees is stable between calls.
        wanted: list[str] = []
        for scope in list(granted_scopes or []):
            connector_name = GOOGLE_SCOPE_CONNECTORS.get(str(scope))
            if connector_name and connector_name not in wanted:
                wanted.append(connector_name)
    else:
        wanted = [str(provider)]

    connectors: list[ConnectionConnector] = []
    for connector_name in wanted:
        display = _connector_display_name(loaded_modules, connector_name)
        if display is None:
            connectors.append(
                ConnectionConnector(
                    connector_name=connector_name,
                    display_name=connector_name,
                    available=False,
                    reason="This connector is not installed in this deployment.",
                )
            )
            continue
        try:
            permitted = is_module_enabled(connector_name, project_id, conn, fail_closed=True)
        except ModuleEnablementUnavailable as exc:
            raise ConnectionConnectorsUnavailable("connector scope is unavailable") from exc
        if not permitted:
            connectors.append(
                ConnectionConnector(
                    connector_name=connector_name,
                    display_name=display,
                    available=False,
                    reason="This connector is not enabled for this project.",
                )
            )
            continue
        connectors.append(
            ConnectionConnector(
                connector_name=connector_name, display_name=display, available=True
            )
        )

    return connectors


def list_unopened_connectors(
    *,
    project_id: str,
    connection_ref_id: str,
    identity: str,
    loaded_modules: list[Any],
    conn: Any,
) -> list[ConnectionConnector]:
    """Les Connecteurs qu'un RE-CONSENTEMENT ouvrirait, et que celui-ci n'ouvre pas.

    LE DEFAUT QU'ELLE FERME (AI-112). `list_connection_connectors` derive les
    Connecteurs de `granted_scopes` -- ce que Google a REELLEMENT accorde -- et
    jamais de `GOOGLE_STACK_SCOPES`, ce qu'on demande. Ce choix est juste : une
    personne peut decocher des produits sur l'ecran de consentement, et lire la
    demande proposerait des Connecteurs que le jeton n'ouvre pas -- l'echec ne
    surgirait qu'a l'extraction, sur planification, des heures plus tard.

    Mais quand on AJOUTE un scope au produit, les autorisations deja emises ne le
    portent pas. Les quatre scopes ajoutes par AI-94 ne sont dans aucune : trois
    Connecteurs installes depuis des semaines restent invisibles pour toute
    connexion anterieure, jusqu'a un re-consentement que rien ne suggere. La
    personne n'a rien decoche -- le produit a change sous elle.

    `google_oauth.py` nomme lui-meme cette frontiere : << That degradation is
    silent by design here; making it visible is the console's job >>. Voici la
    moitie console, et elle ne change AUCUNE regle : rien n'est declare ouvert
    qui ne le soit. Elle dit seulement ce qu'un re-consentement ajouterait.

    CE QU'ELLE NE PEUT PAS DIRE, ET NE PRETEND PAS DIRE. `connection_ref` ne garde
    pas les scopes DEMANDES au moment du consentement -- seulement les accordes.
    On ne peut donc pas distinguer << la personne l'a decoche >> de << ce scope
    n'existait pas encore >>. Les deux ressortent ici, et c'est honnete dans les
    deux cas : dans le premier elle redecochera, dans le second elle decouvrira
    ce qu'elle n'avait jamais pu accepter. Le libelle ne doit donc jamais dire
    << vous avez refuse >>.

    Rend une liste vide pour toute connexion Nango : une autorisation Nango
    n'ouvre qu'un Connecteur, il n'y a rien a comparer.

    Fail-closed a l'identique de `list_connection_connectors`, par construction :
    les deux passent par `_read_authorization`.
    """
    project_id = (project_id or "").strip()
    connection_ref_id = (connection_ref_id or "").strip()
    if not project_id or not connection_ref_id:
        raise ValueError("project_id and connection_ref_id are required")

    from core.google_oauth import GOOGLE_STACK_SCOPES  # noqa: PLC0415
    from core.module_enablement import (  # noqa: PLC0415
        ModuleEnablementUnavailable,
        is_module_enabled,
    )

    _provider, auth_path, granted_scopes = _read_authorization(
        project_id=project_id,
        connection_ref_id=connection_ref_id,
        identity=identity,
        conn=conn,
    )
    if auth_path != "google_direct":
        return []

    granted = set(granted_scopes)
    opened = {
        GOOGLE_SCOPE_CONNECTORS[scope] for scope in granted if scope in GOOGLE_SCOPE_CONNECTORS
    }

    # On n'annonce QUE ce qu'un re-consentement demanderait vraiment : un scope
    # present dans la table de correspondance mais absent de `GOOGLE_STACK_SCOPES`
    # ne serait pas redemande, donc le promettre serait un mensonge poli.
    missing: list[str] = []
    for scope in GOOGLE_STACK_SCOPES:
        connector_name = GOOGLE_SCOPE_CONNECTORS.get(scope)
        if not connector_name or scope in granted:
            continue
        if connector_name in opened or connector_name in missing:
            # youtube-analytics est ouvert par DEUX scopes : n'en tenir qu'un
            # accorde suffit a l'ouvrir, et il ne doit pas etre propose deux fois.
            continue
        missing.append(connector_name)

    unopened: list[ConnectionConnector] = []
    for connector_name in missing:
        display = _connector_display_name(loaded_modules, connector_name)
        if display is None:
            # Pas installe ici : le proposer ferait promettre au re-consentement
            # quelque chose que ce deploiement ne saurait pas servir.
            continue
        try:
            permitted = is_module_enabled(connector_name, project_id, conn, fail_closed=True)
        except ModuleEnablementUnavailable as exc:
            raise ConnectionConnectorsUnavailable("connector scope is unavailable") from exc
        if not permitted:
            continue
        unopened.append(
            ConnectionConnector(
                connector_name=connector_name,
                display_name=display,
                available=False,
                reason="Reconnect this source to authorize this connector.",
            )
        )
    return unopened


def resolve_connection_connector(
    *,
    project_id: str,
    connection_ref_id: str,
    identity: str,
    loaded_modules: list[Any],
    conn: Any,
    requested_connector: str | None,
) -> str:
    """Return the Connector a capability request should read, or refuse.

    `requested_connector` is what the wizard picked at the Connector step. It is
    checked against the authorization's OWN Connector set rather than trusted: a
    Connector name in a query string must not become a way to read a catalog this
    authorization does not open. When nothing is requested and the authorization
    opens exactly one Connector, that Connector is the answer -- which is every
    Nango connection, so the single-Connector caller never has to know this step
    exists.
    """

    connectors = list_connection_connectors(
        project_id=project_id,
        connection_ref_id=connection_ref_id,
        identity=identity,
        loaded_modules=loaded_modules,
        conn=conn,
    )
    usable = [connector for connector in connectors if connector.available]
    requested = (requested_connector or "").strip()

    if requested:
        for connector in usable:
            if connector.connector_name == requested:
                return connector.connector_name
        raise ConnectionConnectorsNotFound

    if len(usable) == 1:
        return usable[0].connector_name
    # Zero usable Connectors is not-found; several with no choice made is a caller
    # error the endpoint turns into an explicit "pick a connector", never a guess
    # -- picking the first would silently build a Datastream on the wrong product.
    raise ConnectionConnectorsNotFound
