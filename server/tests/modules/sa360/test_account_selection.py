"""Le compte choisi par l'operateur atteint-il l'appel SA360 ?

MEME DEFAUT QUE CM360 ET DV360. Ce connecteur lisait `selection["customer_id"]`
et `selection["login_customer_id"]` : un objet de COMPTE. La `selection` que le
plan fournit ne porte que `selection_mode` / `metrics` / `dimensions` / `grain`
/ `filters` (`core/schemas/datastream-intent.schema.json`,
`additionalProperties: false`), et `core/queue.py` ne remplit jamais
`job["selection"]`.

CE QUI EST PROPRE A SA360. Le `login-customer-id` n'est pas un compte, c'est une
ROUTE : l'en-tete qui dit par quel manager on atteint un client. La recherche
ratifiee le dit ainsi (`sa360-catalog-research.md` §2 : « For manager-to-client
calls, send `login-customer-id` without hyphens and route the query to the
client customer id »). Un client atteint directement n'en a pas besoin. L'id
opaque composite `'<client_customer_id>@<login_customer_id>'` porte donc la
route quand elle existe, et l'id nu suffit quand elle n'existe pas -- exactement
la forme que google-ads a ratifiee en 26.2 pour la meme famille d'API.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "sa360"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_sa360_account", MODULE_DIR / "connector.py"
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
    """Une topologie invalide est traitee comme ABSENTE, pas comme degradee."""
    from core.account_topology import validate_topology

    topology = manifest["account_topology"]
    assert validate_topology(topology) == []
    assert topology["pull_parameter"] == "client_customer_id"


def test_pull_accepts_the_declared_parameter_and_never_requires_it(connector, manifest):
    declared = manifest["account_topology"]["pull_parameter"]
    for name in (
        "pull",
        "pull_campaign_daily",
        "pull_ad_group_daily",
        "pull_keyword_daily",
        "pull_catalog_daily",
    ):
        params = inspect.signature(getattr(connector, name)).parameters
        assert declared in params, f"{name}() n'accepte pas {declared!r}"
        assert params[declared].default is None, f"{name}(): {declared} doit etre optionnel"


def test_discovery_emits_exactly_the_id_the_pull_consumes(connector):
    """`sa360_selection_1` etait un compteur de boucle : il ne pilotait rien."""
    client = MagicMock()
    client.request.return_value = Response(payload={"resourceNames": ["customers/123-456"]})
    node = connector.discover_accounts("c", _client=client, _token="t")[0]
    assert node["id"] == "123456"
    assert node["label"] == "customers/123-456"
    assert connector.split_account_id(node["id"]) == ("123456", None)


def test_a_manager_route_survives_the_round_trip(connector):
    """`with_manager_route` produit un id que `pull()` sait relire."""
    node = {"id": "123456", "label": "Client", "customer_id": "123456"}
    routed = connector.with_manager_route(node, "999-888")
    assert routed["id"] == "123456@999888"
    assert connector.split_account_id(routed["id"]) == ("123456", "999888")


def test_the_selected_account_reaches_the_url_and_the_login_header(connector, monkeypatch):
    """De l'id opaque au chemin `/customers/<client>` et a l'en-tete de routage."""
    monkeypatch.setattr(connector, "_charge_query", lambda *a, **k: None)
    landed: dict = {}
    monkeypatch.setattr(
        connector, "_land", lambda rows, fields, context: landed.update(context=context) or 1
    )
    client = MagicMock()
    client.request.return_value = Response(payload={"results": []})

    connector.pull(
        "conn",
        "2026-07-01",
        "2026-07-02",
        "project",
        "pull-1",
        client_customer_id="123456@999888",
        _client=client,
        _token="token",
    )

    call = client.request.call_args
    assert call.args[1].endswith("/customers/123456/searchAds360:search")
    assert call.kwargs["headers"]["login-customer-id"] == "999888"
    assert landed["context"]["customer_id"] == "123456"
    assert landed["context"]["login_customer_id"] == "999888"


def test_a_direct_customer_sends_no_login_header(connector, monkeypatch):
    """L'absence de manager n'est pas une absence de compte : la route est directe."""
    monkeypatch.setattr(connector, "_charge_query", lambda *a, **k: None)
    landed: dict = {}
    monkeypatch.setattr(
        connector, "_land", lambda rows, fields, context: landed.update(context=context) or 1
    )
    client = MagicMock()
    client.request.return_value = Response(payload={"results": []})

    connector.pull(
        "conn",
        "2026-07-01",
        "2026-07-02",
        "project",
        "pull-1",
        client_customer_id="123456",
        _client=client,
        _token="token",
    )
    assert "login-customer-id" not in client.request.call_args.kwargs["headers"]
    assert landed["context"]["login_customer_id"] is None


def test_the_report_selection_is_not_the_account_channel(connector, monkeypatch):
    monkeypatch.setattr(connector, "_charge_query", lambda *a, **k: None)
    monkeypatch.setattr(connector, "_land", lambda rows, fields, context: 0)
    client = MagicMock()

    with pytest.raises(connector.Sa360OnboardingError) as raised:
        connector.pull(
            "conn",
            "2026-07-01",
            "2026-07-02",
            "project",
            "pull-1",
            selection={"customer_id": "123456", "login_customer_id": "999888"},
            _client=client,
            _token="token",
        )
    assert client.request.call_count == 0
    assert "client_customer_id" in str(raised.value)


def test_no_account_is_a_typed_error_that_names_the_selection(connector, monkeypatch):
    """Jamais un TypeError nu, jamais un repli sur l'environnement."""
    monkeypatch.setenv("SA360_CUSTOMER_ID", "should-never-be-read")
    client = MagicMock()

    with pytest.raises(connector.Sa360OnboardingError) as raised:
        connector.pull(
            "conn", "2026-07-01", "2026-07-02", "project", "pull-1", _client=client, _token="token"
        )
    message = str(raised.value)
    assert client.request.call_count == 0
    assert "client_customer_id" in message
    assert "discover_accounts" in message
    assert "should-never-be-read" not in message
