"""Le compte choisi par l'operateur atteint-il l'appel CM360 ?

POURQUOI CE FICHIER EXISTE. Deux objets differents portent le nom `selection`.
Celui que le PLAN fournit est decrit dans
`server/core/schemas/datastream-intent.schema.json` (`$defs.selection`) :
`selection_mode`, `metrics`, `dimensions`, `grain`, `filters` -- et rien d'autre,
`additionalProperties: false`. Celui que ce connecteur attendait etait un objet
de COMPTE (`selection["advertiser_id"]`, `selection["profile_id"]`) que le plan
ne produit pas et ne peut pas produire. Et `core/queue.py` ne remplit jamais
`job["selection"]` (son propre commentaire : « None today; wired by the
datastream path later »). Le compte choisi dans l'assistant n'atteignait donc
jamais CM360.

LE PONT REEL, lu dans `core/queue.py::_account_kwargs` : le manifeste declare
`account_topology.pull_parameter`, et le worker passe sous ce nom la CHAINE
OPAQUE que `discover_accounts` a emise, que l'operateur a choisie et que
`verify_and_select_account` a verifiee. Une chaine, une seule.

CE QUE CE FICHIER MESURE, ET POURQUOI CETTE FORME. CM360 a besoin de DEUX
identifiants pour un seul appel : l'advertiser (le niveau de selection) et le
`profileId` du CM360 user profile, qui est dans le CHEMIN de l'URL
(`/userprofiles/{profileId}/reportData/query`). Un seul brin passe par le
scope. La reponse deja etablie ici est l'id opaque composite -- google-ads le
fait depuis la story 26.2 avec `'<cid>@<login_cid>'`. Ces tests exigent donc
l'aller-retour complet : ce que `discover_accounts` emet doit etre exactement
ce que `pull()` sait consommer.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "cm360"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_cm360_account", MODULE_DIR / "connector.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def manifest():
    return json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))


class Response:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self.payload


def test_the_topology_is_valid_and_says_where_the_account_lands(manifest):
    """Une topologie invalide n'est pas une topologie degradee : elle est ABSENTE.

    `core/account_topology.get_topology` renvoie None des qu'une regle de
    `validate_topology` casse -- le module est alors traite comme ne participant
    PAS au flux de selection. `levels` en chaines nues (au lieu de
    `{id, label}`) suffisait a rendre CM360 invisible a tout l'appareil de choix
    de compte, sans une seule erreur.
    """
    from core.account_topology import validate_topology

    topology = manifest["account_topology"]
    assert validate_topology(topology) == []
    assert topology["pull_parameter"] == "advertiser_id"


def test_pull_accepts_the_declared_parameter_and_never_requires_it(connector, manifest):
    """Le worker ne passe le compte QUE si une selection existe.

    Un positionnel requis leve donc un `TypeError` nu -- hors de toute
    taxonomie -- des qu'un datastream n'a pas encore de compte choisi.
    """
    declared = manifest["account_topology"]["pull_parameter"]
    for name in ("pull", "pull_standard_daily", "pull_floodlight_daily", "pull_reach"):
        params = inspect.signature(getattr(connector, name)).parameters
        assert declared in params, f"{name}() n'accepte pas {declared!r}"
        assert params[declared].default is None, f"{name}(): {declared} doit etre optionnel"


def test_discovery_emits_exactly_the_id_the_pull_consumes(connector):
    """L'aller-retour. `id` est ce que le scope stocke et ce que le pull recoit.

    `cm360_selection_1` etait un compteur de boucle : instable d'une decouverte
    a l'autre, et ne portant NI l'advertiser NI le profile. Il ne pouvait rien
    piloter.
    """
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"items": [{"profileId": "p1", "accountId": "a1", "subAccountId": "s1"}]}),
        Response(payload={"advertisers": [{"id": "adv1", "name": "Advertiser"}]}),
    ]
    node = connector.discover_accounts("conn", _client=client, _token="secret")[0]
    assert node["id"] == "adv1@p1"
    assert node["label"] == "Advertiser"
    assert connector.split_account_id(node["id"]) == ("adv1", "p1")


def test_the_selected_account_reaches_the_provider_call(connector, monkeypatch):
    """De l'id opaque a l'URL et au filtre : le dernier metre, mesure."""
    monkeypatch.setattr(connector, "_consume_query_quota", lambda *a, **k: None)
    landed: dict = {}
    monkeypatch.setattr(
        connector, "_land", lambda rows, context: landed.update(context=context) or 1
    )
    client = MagicMock()
    client.request.return_value = Response(payload={"rows": [{"metrics": {"impressions": "1"}}]})

    connector.pull(
        "conn",
        "2026-07-01",
        "2026-07-02",
        "project",
        "pull-1",
        advertiser_id="adv1@p1",
        _client=client,
        _token="token",
    )

    call = client.request.call_args
    assert call.args[1].endswith("/userprofiles/p1/reportData/query")
    assert call.kwargs["json"]["dimensionFilters"] == [
        {"dimensionName": "dfa:advertiser", "value": "adv1"}
    ]
    assert landed["context"]["advertiser_id"] == "adv1"
    assert landed["context"]["profile_id"] == "p1"


def test_the_report_selection_is_not_the_account_channel(connector, monkeypatch):
    """Un compte pose dans `selection` ne doit RIEN piloter.

    Le plan ne peut pas produire cet objet ; l'accepter, c'est laisser vivre un
    chemin que la production n'emprunte jamais et qui masque l'absence de compte.
    """
    monkeypatch.setattr(connector, "_consume_query_quota", lambda *a, **k: None)
    monkeypatch.setattr(connector, "_land", lambda rows, context: 0)
    client = MagicMock()

    with pytest.raises(connector.Cm360OnboardingError) as raised:
        connector.pull(
            "conn",
            "2026-07-01",
            "2026-07-02",
            "project",
            "pull-1",
            selection={"advertiser_id": "adv1", "profile_id": "p1"},
            _client=client,
            _token="token",
        )
    assert client.request.call_count == 0
    assert "advertiser_id" in str(raised.value)


def test_no_account_is_a_typed_error_that_names_the_selection(connector, monkeypatch):
    """Jamais un TypeError nu, jamais un repli sur l'environnement.

    `core/account_topology.py:514` declare les replis `*_ACCOUNT_ID` deprecies :
    une variable d'environnement est unique pour tout le deploiement, donc TOUS
    les datastreams de TOUS les projets tireraient le meme advertiser.
    """
    monkeypatch.setenv("CM360_ADVERTISER_ID", "should-never-be-read")
    monkeypatch.setenv("CM360_PROFILE_ID", "should-never-be-read")
    client = MagicMock()

    with pytest.raises(connector.Cm360OnboardingError) as raised:
        connector.pull(
            "conn", "2026-07-01", "2026-07-02", "project", "pull-1", _client=client, _token="token"
        )
    message = str(raised.value)
    assert client.request.call_count == 0
    assert "advertiser_id" in message
    assert "discover_accounts" in message
    assert "should-never-be-read" not in message


def test_a_bare_advertiser_id_says_which_half_is_missing(connector, monkeypatch):
    """CM360 exige le user profile en plus de l'advertiser -- et le DIT.

    Ce test existe parce que la moitie manquante est invisible autrement : sans
    `profileId`, l'URL n'a pas de chemin, et un message generique enverrait
    chercher un droit d'acces la ou il manque un identifiant.
    """
    client = MagicMock()
    with pytest.raises(connector.Cm360OnboardingError) as raised:
        connector.pull(
            "conn",
            "2026-07-01",
            "2026-07-02",
            "project",
            "pull-1",
            advertiser_id="adv1",
            _client=client,
            _token="token",
        )
    message = str(raised.value)
    assert client.request.call_count == 0
    assert "profileId" in message or "profile_id" in message
    assert "discover_accounts" in message
