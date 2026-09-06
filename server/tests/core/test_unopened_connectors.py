r"""Ce qu'une autorisation n'ouvre PAS doit se voir -- sinon le produit change en silence.

LE DEFAUT (AI-112). `list_connection_connectors` derive les Connecteurs d'une
connexion depuis `connection_ref.granted_scopes` : ce que Google a REELLEMENT
accorde, jamais `GOOGLE_STACK_SCOPES`, ce qu'on demande. Ce choix est juste et il
ne bouge pas ici -- lire la demande proposerait des Connecteurs que le jeton
n'ouvre pas, et l'echec ne surgirait qu'a l'extraction, sur planification, des
heures plus tard.

Ce que ce choix laisse dans l'ombre : quand le produit AJOUTE un scope, aucune
autorisation deja emise ne le porte. Les quatre scopes ajoutes par AI-94 ne sont
dans aucune, donc trois Connecteurs installes depuis des semaines etaient
invisibles pour toute connexion anterieure -- jusqu'a un re-consentement que rien
ne suggerait. La personne n'avait rien decoche : le produit avait change sous
elle.

`google_oauth.py` nomme la frontiere : << That degradation is silent by design
here; making it visible is the console's job >>. Ces tests tiennent la moitie
console.

ET ILS COUVRENT UNE DETTE QUI N'ETAIT PAS LA MIENNE. Mesure du 2026-08-01 :
`grep -rln "list_connection_connectors\|ConnectionConnector" server/tests` ne rend
RIEN. Ce seam est un controle d'acces fail-closed -- inconnu, etranger, inactif et
desactive partagent un seul not-found pour qu'on ne puisse pas sonder ce qui
existe ailleurs -- et il n'avait aucune garde directe. Les quatre premiers tests
ci-dessous portent sur `_read_authorization`, donc sur les DEUX fonctions.

DERNIERE SECTION, AJOUTEE PAR 57.11 : `resolve_connection_connector`, le TROISIEME
lecteur de ce meme seam. C'est lui que `GET /api/connections/{id}/accounts?connector=`
appelle pour choisir l'outil d'une autorisation multi-outils, et il n'avait aucune
garde non plus. Il vit ici et pas dans un fichier a part parce qu'un second faux
`conn` serait une seconde copie d'un controle d'acces.
"""

from __future__ import annotations

import pytest
from core.connection_tools import (
    ConnectionConnectorsNotFound,
    list_unopened_connectors,
    resolve_connection_connector,
)

#: Les sept scopes qu'une autorisation d'avant AI-94 portait.
BIGQUERY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"

SCOPES_BEFORE_AI94 = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/dfareporting",
    "https://www.googleapis.com/auth/display-video",
    "https://www.googleapis.com/auth/doubleclicksearch",
]


class _Module:
    def __init__(self, name: str, display: str) -> None:
        self.name = name
        self.manifest = {"display_name": display}


#: Ce que le deploiement installe. Volontairement PARTIEL : `youtube-analytics`
#: est absent, pour que le test de non-installation ait quelque chose a mordre.
LOADED = [
    _Module("gsc", "Search Console"),
    _Module("google-analytics", "Google Analytics"),
    _Module("google-ads", "Google Ads"),
    _Module("google-sheets", "Google Sheets"),
    _Module("cm360", "Campaign Manager 360"),
    _Module("dv360", "Display & Video 360"),
    _Module("sa360", "Search Ads 360"),
    _Module("google-ad-manager", "Google Ad Manager"),
    _Module("google-business-profile", "Google Business Profile"),
]


def _conn(row):
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return row

    class _Conn:
        def cursor(self):
            return _Cursor()

    return _Conn()


@pytest.fixture
def allow_everything(monkeypatch):
    """Acces lisible et tous les Connecteurs actives : on isole la comparaison."""
    monkeypatch.setattr(
        "core.project_access.identity_can_read_project", lambda *a, **k: True
    )
    monkeypatch.setattr("core.module_enablement.is_module_enabled", lambda *a, **k: True)


def _call(row, loaded=None):
    return list_unopened_connectors(
        project_id="proj_EXAMPLE",
        connection_ref_id="conn_EXAMPLE",
        identity="owner@example.com",
        loaded_modules=LOADED if loaded is None else loaded,
        conn=_conn(row),
    )


# ---------------------------------------------------------------------------
# Le fail-closed partage -- la dette qui n'avait aucune garde
# ---------------------------------------------------------------------------


def test_an_identity_that_cannot_read_the_project_learns_nothing(monkeypatch):
    monkeypatch.setattr(
        "core.project_access.identity_can_read_project", lambda *a, **k: False
    )
    with pytest.raises(ConnectionConnectorsNotFound):
        _call(("google", "google_direct", "active", True, SCOPES_BEFORE_AI94))


def test_an_unknown_connection_is_indistinguishable_from_a_foreign_one(allow_everything):
    with pytest.raises(ConnectionConnectorsNotFound):
        _call(None)


@pytest.mark.parametrize(
    ("status", "enabled"),
    [("revoked", True), ("active", False), ("pending", True)],
)
def test_an_inactive_authorization_answers_like_an_absent_one(
    allow_everything, status, enabled
):
    """Meme reponse que << inconnue >> : sonder ne doit rien apprendre."""
    with pytest.raises(ConnectionConnectorsNotFound):
        _call(("google", "google_direct", status, enabled, SCOPES_BEFORE_AI94))


# ---------------------------------------------------------------------------
# La comparaison elle-meme
# ---------------------------------------------------------------------------


def test_an_authorization_from_before_ai94_shows_what_a_reconsent_would_add(
    allow_everything,
):
    """LE CAS REEL, et le seul en production aujourd'hui.

    Sept scopes accordes, quatre ajoutes depuis. `youtube-analytics` n'est pas
    dans LOADED, donc il ne sort pas -- promettre au re-consentement un
    Connecteur que ce deploiement ne sait pas servir serait le meme mensonge, a
    l'envers, que celui qu'on repare.
    """
    unopened = _call(("google", "google_direct", "active", True, SCOPES_BEFORE_AI94))
    assert [c.connector_name for c in unopened] == [
        "google-ad-manager",
        "google-business-profile",
    ]
    assert all(c.available is False for c in unopened)
    assert all(c.reason and "Reconnect" in c.reason for c in unopened)


def test_a_complete_authorization_proposes_nothing(allow_everything):
    """Rien a dire quand rien ne manque -- la surface doit disparaitre, pas rassurer."""
    from core.google_oauth import GOOGLE_STACK_SCOPES

    assert _call(("google", "google_direct", "active", True, list(GOOGLE_STACK_SCOPES))) == []


def test_a_connector_opened_by_two_scopes_is_not_proposed_twice(allow_everything):
    """`youtube-analytics` a DEUX scopes, et un seul suffit a l'ouvrir.

    Sans la garde, il sortirait deux fois dans la liste -- et pire, il sortirait
    comme << a debloquer >> alors que l'un de ses deux scopes est deja accorde.
    """
    loaded = [*LOADED, _Module("youtube-analytics", "YouTube Analytics")]
    granted = [*SCOPES_BEFORE_AI94, "https://www.googleapis.com/auth/yt-analytics.readonly"]
    names = [c.connector_name for c in _call(
        ("google", "google_direct", "active", True, granted), loaded=loaded
    )]
    assert names.count("youtube-analytics") == 0, names

    partial = [c.connector_name for c in _call(
        ("google", "google_direct", "active", True, SCOPES_BEFORE_AI94), loaded=loaded
    )]
    assert partial.count("youtube-analytics") == 1, partial


def test_a_nango_authorization_has_nothing_to_compare(allow_everything):
    """Une connexion Nango n'ouvre qu'un Connecteur : proposer un re-consentement
    la-dessus serait un bouton qui ne repare rien."""
    assert _call(("meta-ads", "nango", "active", True, [])) == []


def test_a_connector_the_project_disabled_is_not_proposed(monkeypatch):
    """Se reconnecter n'activerait pas un Connecteur que le PROJET a desactive.

    C'est une decision de gouvernance, pas un manque d'autorisation ; l'afficher
    ici enverrait la personne faire un consentement qui ne changerait rien.
    """
    monkeypatch.setattr(
        "core.project_access.identity_can_read_project", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "core.module_enablement.is_module_enabled",
        lambda name, *a, **k: name != "google-ad-manager",
    )
    names = [
        c.connector_name
        for c in _call(("google", "google_direct", "active", True, SCOPES_BEFORE_AI94))
    ]
    assert names == ["google-business-profile"]


def test_nothing_is_promised_that_the_consent_would_not_request(allow_everything):
    """Le pivot est `GOOGLE_STACK_SCOPES`, jamais la table de correspondance seule.

    Un scope present dans `GOOGLE_SCOPE_CONNECTORS` mais absent de ce qu'on
    demande ne serait pas redemande au re-consentement : le proposer serait un
    mensonge poli. Ce test le prouve en retirant un scope de la DEMANDE et en
    verifiant que son Connecteur cesse d'etre propose.
    """
    from core import google_oauth

    reduced = tuple(
        s for s in google_oauth.GOOGLE_STACK_SCOPES
        if s != "https://www.googleapis.com/auth/dfp"
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(google_oauth, "GOOGLE_STACK_SCOPES", reduced)
        names = [
            c.connector_name
            for c in _call(("google", "google_direct", "active", True, SCOPES_BEFORE_AI94))
        ]
    assert "google-ad-manager" not in names
    assert "google-business-profile" in names


# ---------------------------------------------------------------------------
# `resolve_connection_connector` -- quel outil la porte de browse va lister
# ---------------------------------------------------------------------------


def _resolve(row, requested, loaded=None):
    return resolve_connection_connector(
        project_id="proj_EXAMPLE",
        connection_ref_id="conn_EXAMPLE",
        identity="owner@example.com",
        loaded_modules=LOADED if loaded is None else loaded,
        conn=_conn(row),
        requested_connector=requested,
    )


#: Le deploiement qui sert la marche BigQuery. `bigquery` est un module comme les
#: autres du point de vue du registre ; ce qui le distingue est son autorisation.
LOADED_WITH_BIGQUERY = [*LOADED, _Module("bigquery", "Google BigQuery")]


def test_a_bigquery_authorization_resolves_the_tool_the_wizard_asked_for(allow_everything):
    """Story 57.11 -- la porte de browse de l'etape Source, prouvee sur sa vraie forme.

    L'assistant appelle `GET /api/connections/{id}/accounts?connector=bigquery`
    pour parcourir projet -> dataset -> table (57.1 D2 : cette porte est reutilisee,
    aucune seconde n'est ouverte). Un acces BigQuery est une autorisation dont le
    `provider` EST le connecteur, donc `wanted = [provider]` et la resolution rend
    l'outil demande. Non prouve avant cette story ; mesure ici, et intact.
    """
    assert _resolve(
        ("bigquery", "nango", "active", True, []),
        "bigquery",
        loaded=LOADED_WITH_BIGQUERY,
    ) == "bigquery"


def test_a_bigquery_authorization_needs_no_connector_named_at_all(allow_everything):
    """Un seul outil ouvert -> pas de question. Le parametre reste facultatif."""
    assert _resolve(
        ("bigquery", "nango", "active", True, []),
        None,
        loaded=LOADED_WITH_BIGQUERY,
    ) == "bigquery"


def test_a_google_consent_opens_bigquery_because_bigquery_is_google(
    allow_everything,
):
    """RETOURNE LE 2026-08-11 (arbitrage Jean, AI-285). Le contraire etait epingle ici.

    Ce test s'appelait « does not open bigquery and must not pretend to » et
    defendait un refus : le module declarait `auth_type: "none"`, le deploiement
    lisait l'entrepot du client avec SES propres identifiants (ADC / le compte de
    service Cloud Run), et aucun scope du consentement n'ouvrait BigQuery.

    **La regle est l'inverse, et elle vaut pour tout le reste de la pile : ce qui
    est Google se lit par le consentement Google.** Un client dont l'entrepot est
    lu par le principal toorow ne peut ni revoquer cet acces depuis son propre
    compte Google, ni voir dans son ecran de consentement ce que toorow lit.
    C'est la raison, et ce n'est pas une preference.

    L'ancienne docstring disait « ajouter la correspondance ferait dire a l'ecran
    qu'un consentement ouvre un outil que le jeton ne peut pas servir ». C'etait
    vrai tant que le scope n'etait pas demande -- et c'est pour cela que les
    TROIS maillons se posent ensemble : `GOOGLE_STACK_SCOPES` le demande,
    `GOOGLE_SCOPE_CONNECTORS` dit ce qu'il ouvre, `_GOOGLE_SCOPE_LABELS` le nomme
    a la personne. `tests/conformance/test_google_consent.py` refuse qu'un seul
    des trois manque.

    L'ADC reste, et c'est le cas AUTO-HEBERGE : un deploiement qui lit son propre
    projet. Jamais le parcours d'un client.
    """
    from core.connection_tools import GOOGLE_SCOPE_CONNECTORS

    assert GOOGLE_SCOPE_CONNECTORS[BIGQUERY_SCOPE] == "bigquery"
    assert (
        _resolve(
            ("google", "google_direct", "active", True, [*SCOPES_BEFORE_AI94, BIGQUERY_SCOPE]),
            "bigquery",
            loaded=LOADED_WITH_BIGQUERY,
        )
        == "bigquery"
    )


def test_a_consent_granted_before_bigquery_still_opens_only_what_it_granted(
    allow_everything,
):
    """Un consentement ancien ne gagne pas BigQuery retroactivement.

    `connection_tools` lit `connection_ref.granted_scopes`, jamais la constante :
    une autorisation accordee avant que le scope soit demande ouvre exactement ce
    qu'elle ouvrait, et BigQuery n'y apparait qu'apres un nouveau consentement.
    Sans cette borne, l'ecran offrirait un Connecteur que le jeton ne peut pas
    servir -- l'echec ne surgirait qu'a l'extraction, ce que l'ancienne docstring
    de ce fichier craignait a juste titre.
    """
    with pytest.raises(ConnectionConnectorsNotFound):
        _resolve(
            ("google", "google_direct", "active", True, SCOPES_BEFORE_AI94),
            "bigquery",
            loaded=LOADED_WITH_BIGQUERY,
        )


def test_a_multi_tool_authorization_resolves_exactly_the_tool_named(allow_everything):
    """Un consentement Google ouvre dix Connecteurs et leurs listes de comptes sont
    dix listes differentes : le parametre est ce qui choisit, jamais le premier."""
    row = ("google", "google_direct", "active", True, SCOPES_BEFORE_AI94)
    assert _resolve(row, "gsc") == "gsc"
    assert _resolve(row, "google-sheets") == "google-sheets"


def test_a_multi_tool_authorization_refuses_to_guess(allow_everything):
    """Sans outil nomme, plusieurs ouverts : refus, jamais le premier de la liste.

    Deviner batirait un Datastream sur le mauvais produit, et rien a l'ecran ne le
    dirait avant la premiere extraction.
    """
    with pytest.raises(ConnectionConnectorsNotFound):
        _resolve(("google", "google_direct", "active", True, SCOPES_BEFORE_AI94), None)


def test_a_connector_the_project_disabled_is_not_resolvable(monkeypatch):
    """La gouvernance mord ici aussi : `available=False` ne se resout pas.

    Sinon un outil desactive par le projet resterait parcourable par la chaine de
    caracteres de la query string -- exactement le contournement que la validation
    de `requested_connector` contre l'ensemble de l'autorisation existe pour fermer.
    """
    monkeypatch.setattr(
        "core.project_access.identity_can_read_project", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "core.module_enablement.is_module_enabled", lambda name, *a, **k: name != "gsc"
    )
    with pytest.raises(ConnectionConnectorsNotFound):
        _resolve(("google", "google_direct", "active", True, SCOPES_BEFORE_AI94), "gsc")


def test_an_identity_that_cannot_read_the_project_resolves_nothing(monkeypatch):
    """Le fail-closed de `_read_authorization` couvre le troisieme lecteur aussi."""
    monkeypatch.setattr(
        "core.project_access.identity_can_read_project", lambda *a, **k: False
    )
    with pytest.raises(ConnectionConnectorsNotFound):
        _resolve(("bigquery", "nango", "active", True, []), "bigquery",
                 loaded=LOADED_WITH_BIGQUERY)
