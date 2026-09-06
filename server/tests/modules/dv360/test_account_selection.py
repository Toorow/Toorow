"""Le compte choisi par l'operateur atteint-il l'appel DV360 ?

MEME DEFAUT QUE CM360 ET SA360, meme cause. Ce connecteur lisait
`selection["advertiser_id"]`, c'est-a-dire un objet de COMPTE. Or la `selection`
que le plan fournit ne porte que `selection_mode` / `metrics` / `dimensions` /
`grain` / `filters` (`core/schemas/datastream-intent.schema.json`,
`additionalProperties: false`), et `core/queue.py` ne remplit meme jamais
`job["selection"]`. Le choix fait dans l'assistant mourait avant l'appel.

CE QUI EST PROPRE A DV360. L'appel Bid Manager n'a besoin QUE de l'advertiser :
`filters: [{"type": "FILTER_ADVERTISER", "value": ...}]`. Le `partner_id` n'est
pas un argument d'appel, c'est une colonne de provenance dans
`raw_dv360_daily`. Il voyage donc dans l'id opaque composite
`'<advertiser_id>@<partner_id>'` -- non parce que l'API l'exige, mais parce que
la decouverte le connait et que la table le stocke : le perdre transformerait
une colonne renseignee en colonne vide, sans que rien ne rougisse.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "dv360"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_dv360_account", MODULE_DIR / "connector.py"
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
    assert topology["pull_parameter"] == "advertiser_id"


def test_pull_accepts_the_declared_parameter_and_never_requires_it(connector, manifest):
    declared = manifest["account_topology"]["pull_parameter"]
    for name in (
        "pull",
        "pull_standard_daily",
        "pull_conversion_daily",
        "pull_reach",
        "pull_youtube_compatible",
    ):
        params = inspect.signature(getattr(connector, name)).parameters
        assert declared in params, f"{name}() n'accepte pas {declared!r}"
        assert params[declared].default is None, f"{name}(): {declared} doit etre optionnel"


def test_discovery_emits_exactly_the_id_the_pull_consumes(connector):
    """`dv360_selection_1` etait un compteur de boucle : il ne pilotait rien."""
    client = MagicMock()
    client.request.side_effect = [
        Response(
            payload={"partners": [{"partnerId": "p1", "entityStatus": "ENTITY_STATUS_ACTIVE"}]}
        ),
        Response(
            payload={
                "advertisers": [
                    {
                        "advertiserId": "a1",
                        "displayName": "Advertiser",
                        "entityStatus": "ENTITY_STATUS_ACTIVE",
                        "generalConfig": {"currencyCode": "EUR", "timeZone": "Europe/Paris"},
                    }
                ]
            }
        ),
    ]
    node = connector.discover_accounts("conn", _client=client, _token="secret")[0]
    assert node["id"] == "a1@p1"
    assert node["label"] == "Advertiser"
    assert connector.split_account_id(node["id"]) == ("a1", "p1")


def test_the_selected_account_reaches_the_query_filter_and_the_landed_row(connector, monkeypatch):
    """De l'id opaque au FILTER_ADVERTISER -- et le partner survit en provenance."""
    landed: dict = {}
    monkeypatch.setattr(
        connector, "_land", lambda rows, context: landed.update(context=context) or 1
    )
    monkeypatch.setattr(connector, "ensure_saved_query", lambda *a, **k: "query-1")
    seen: dict = {}

    def _run_saved_query(client, token, query_id, definition, connection_id, project_id):
        seen["definition"] = definition
        return {"status": "completed", "rows": ""}

    monkeypatch.setattr(connector, "run_saved_query", _run_saved_query)
    monkeypatch.setattr(connector, "_parse_artifact", lambda *a, **k: [])

    connector.pull(
        "conn",
        "2026-07-01",
        "2026-07-02",
        "project",
        "pull-1",
        advertiser_id="a1@p1",
        _client=MagicMock(),
        _token="token",
    )

    assert seen["definition"]["params"]["filters"] == [{"type": "FILTER_ADVERTISER", "value": "a1"}]
    assert landed["context"]["advertiser_id"] == "a1"
    assert landed["context"]["partner_id"] == "p1"


def test_a_bare_advertiser_id_is_accepted_and_leaves_the_partner_empty(connector, monkeypatch):
    """Le partner n'est pas exige : l'API n'en a pas besoin.

    C'est la difference avec CM360, ou la moitie manquante rend l'URL
    inconstructible. Ici la colonne de provenance reste vide, et c'est tout --
    refuser le pull pour cela serait une severite inventee.
    """
    landed: dict = {}
    monkeypatch.setattr(
        connector, "_land", lambda rows, context: landed.update(context=context) or 1
    )
    monkeypatch.setattr(connector, "ensure_saved_query", lambda *a, **k: "query-1")
    monkeypatch.setattr(
        connector, "run_saved_query", lambda *a, **k: {"status": "completed", "rows": ""}
    )
    monkeypatch.setattr(connector, "_parse_artifact", lambda *a, **k: [])

    connector.pull(
        "conn",
        "2026-07-01",
        "2026-07-02",
        "project",
        "pull-1",
        advertiser_id="a1",
        _client=MagicMock(),
        _token="token",
    )
    assert landed["context"]["advertiser_id"] == "a1"
    assert landed["context"]["partner_id"] == ""


def test_the_report_selection_is_not_the_account_channel(connector, monkeypatch):
    monkeypatch.setattr(connector, "_land", lambda rows, context: 0)
    calls: list = []
    monkeypatch.setattr(connector, "ensure_saved_query", lambda *a, **k: calls.append(a) or "q")
    monkeypatch.setattr(connector, "run_saved_query", lambda *a, **k: calls.append(a) or {})

    with pytest.raises(connector.Dv360OnboardingError) as raised:
        connector.pull(
            "conn",
            "2026-07-01",
            "2026-07-02",
            "project",
            "pull-1",
            selection={"advertiser_id": "a1", "partner_id": "p1"},
            _client=MagicMock(),
            _token="token",
        )
    assert calls == []
    assert "advertiser_id" in str(raised.value)


def test_no_account_is_a_typed_error_that_names_the_selection(connector, monkeypatch):
    """Jamais un TypeError nu, jamais un repli sur l'environnement."""
    monkeypatch.setenv("DV360_ADVERTISER_ID", "should-never-be-read")
    calls: list = []
    monkeypatch.setattr(connector, "ensure_saved_query", lambda *a, **k: calls.append(a) or "q")

    with pytest.raises(connector.Dv360OnboardingError) as raised:
        connector.pull(
            "conn",
            "2026-07-01",
            "2026-07-02",
            "project",
            "pull-1",
            _client=MagicMock(),
            _token="token",
        )
    message = str(raised.value)
    assert calls == []
    assert "advertiser_id" in message
    assert "discover_accounts" in message
    assert "should-never-be-read" not in message
