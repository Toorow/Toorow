from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "taboola"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location("connector_taboola", MODULE_DIR / "connector.py")
    m = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(m)
    return m


class Response:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self.payload


def test_account_discovery_timezone_currency(connector):
    c = MagicMock()
    c.request.return_value = Response(
        payload={
            "results": [
                {"account_id": "acct", "name": "Adv", "timezone": "Europe/Paris", "currency": "EUR"}
            ]
        }
    )
    r = connector.discover_accounts("x", _client=c, _token_value="t")[0]
    assert r["timezone"] == "Europe/Paris" and r["currency"] == "EUR"


def test_empty_account_actionable(connector):
    c = MagicMock()
    c.request.return_value = Response(payload={"results": []})
    with pytest.raises(connector.TaboolaOnboardingError):
        connector.discover_accounts("x", _client=c, _token_value="t")


def test_report_dimension_and_filter_fail_before_call(connector):
    with pytest.raises(connector.TaboolaCompatibilityError):
        connector.validate_shape("campaign_summary", "item_breakdown")
    with pytest.raises(connector.TaboolaCompatibilityError):
        connector.validate_shape("campaign_summary", "day", {"bad": 1})


def test_dynamic_metadata_must_declare_returned_columns(connector):
    assert connector.validate_response_metadata(
        [{"date": "x", "my_conversion": 1}], {"fields": [{"id": "my_conversion"}]}
    ) == ["my_conversion"]
    with pytest.raises(connector.TaboolaCompatibilityError):
        connector.validate_response_metadata([{"mystery": 1}], {"fields": []})


def test_top_content_limit_is_explicit(connector):
    c = MagicMock()
    c.request.return_value = Response(
        payload={
            "results": [{"item_id": str(i), "impressions": 1} for i in range(1000)],
            "total": 1500,
            "metadata": {"fields": []},
        }
    )
    r = connector.fetch_report(
        c, "t", "a", "top_campaign_content", "item_breakdown", page_size=2000
    )
    assert len(r["rows"]) == 1000 and r["complete"] is False and r["row_limit"] == 1000


def test_history_paginates_and_remains_record_rows(connector):
    c = MagicMock()
    c.request.side_effect = [
        Response(payload={"results": [{"record_id": "1"}], "total": 2}),
        Response(payload={"results": [{"record_id": "2"}], "total": 2}),
    ]
    r = connector.fetch_report(c, "t", "a", "campaign_history", "by_campaign", page_size=1)
    assert [x["record_id"] for x in r["rows"]] == ["1", "2"]


def test_prior_month_fifth_refresh(connector):
    assert connector.should_refresh_prior_month(date(2026, 7, 5))
    assert connector.prior_month_window(date(2026, 7, 5)) == ("2026-06-01", "2026-06-30")


def test_429_breaker(connector):
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as e:
        connector._raise_response(Response(429, {}, {"Retry-After": "8"}))
    assert e.value.retry_after == 8


# ---------------------------------------------------------------------------
# Le compte arrive par le parametre declare, jamais par `selection`.
#
# Deux objets portent le nom `selection` et ce ne sont pas les memes. Celui que
# le PLAN fournit est decrit dans core/schemas/datastream-intent.schema.json
# ($defs.selection) : selection_mode / metrics / dimensions / grain / filters,
# `additionalProperties: false`. Il ne porte pas de compte, et core/queue.py ne
# remplit meme jamais job["selection"]. Le compte choisi par l'operateur arrive
# par le parametre que le manifeste declare (account_topology.pull_parameter),
# passe par core/queue.py::_account_kwargs.
# ---------------------------------------------------------------------------


def _report_response(rows=None, total=None):
    if rows is None:
        rows = [{"date": "2026-07-01", "campaign_id": "c1", "clicks": 3}]
    payload = {
        "results": rows,
        "total": total if total is not None else len(rows),
        "metadata": {},
    }
    return Response(payload=payload)


@pytest.fixture()
def http(connector, monkeypatch, tmp_path):
    """Stub the Backstage transport and land into a throwaway DuckDB file."""
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setattr(connector, "_token", lambda connection_id: "t")
    client = MagicMock()
    client.request.return_value = _report_response()
    monkeypatch.setattr(connector.httpx, "Client", lambda *a, **k: client)
    return client


def test_manifest_declares_the_pull_parameter(connector):
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    topology = manifest["account_topology"]
    declared = topology.get("pull_parameter")
    assert declared == "account_id", (
        "Backstage addresses every report as /{account_id}/reports/... "
        "(taboola-catalog-research.md, section 2). Le manifeste doit nommer ce "
        "parametre pour que core/queue.py::_account_kwargs sache le remplir."
    )
    import inspect

    for name in (
        "pull",
        "pull_campaign_summary",
        "pull_top_campaign_content",
        "pull_campaign_history",
    ):
        params = inspect.signature(getattr(connector, name)).parameters
        assert declared in params, f"{name}() n'accepte pas {declared!r}"
        assert params[declared].default is None, (
            f"{name}(): {declared} doit etre OPTIONNEL -- le worker ne passe le "
            f"compte que si une selection existe, un requis leve un TypeError nu."
        )


def test_topology_contract_is_valid(connector):
    """Une topologie malformee retourne None dans get_topology() : la decouverte
    de comptes est alors morte et l'operateur ne peut RIEN selectionner."""
    from core.account_topology import get_topology, validate_topology

    manifest = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert validate_topology(manifest["account_topology"]) == []
    assert get_topology(manifest) is not None


def test_discovery_id_is_the_account_the_pull_targets(connector):
    """core ne retient que `id` (_flatten_account_ids) et `label`.

    Un `id` synthetique ('taboola_selection_1') est stocke tel quel dans
    app.connection_account_scope et repasse a pull() : l'appel Backstage vise
    alors un compte qui n'existe pas.
    """
    client = MagicMock()
    client.request.return_value = Response(
        payload={"results": [{"account_id": "acct-abc", "name": "Adv", "currency": "EUR"}]}
    )
    row = connector.discover_accounts("x", _client=client, _token_value="t")[0]
    assert row["id"] == "acct-abc"
    assert row["label"] == "Adv"


def test_pull_targets_the_account_passed_as_a_parameter(connector, http):
    connector.pull("conn", "2026-07-01", "2026-07-01", "proj", "pull-1", account_id="acct-abc")
    url = http.request.call_args.args[1]
    assert "/acct-abc/reports/campaign-summary/dimensions/campaign_breakdown" in url


def test_pull_without_an_account_raises_a_named_typed_error(connector, http):
    with pytest.raises(connector.TaboolaOnboardingError) as raised:
        connector.pull("conn", "2026-07-01", "2026-07-01", "proj", "pull-1")
    assert "account_id" in str(raised.value)
    http.request.assert_not_called()


def test_the_report_selection_can_no_longer_smuggle_an_account(connector, http):
    """`selection` reste lisible pour ce qui lui appartient (filters, page_size,
    dimension), mais un compte qui y transiterait doit etre ignore -- sinon le
    defaut d'origine se reinstalle par la porte de service."""
    with pytest.raises(connector.TaboolaOnboardingError):
        connector.pull(
            "conn",
            "2026-07-01",
            "2026-07-01",
            "proj",
            "pull-1",
            selection={"account_id": "ghost"},
        )
    http.request.assert_not_called()


def test_report_selection_keys_still_reach_the_request(connector, http):
    connector.pull_campaign_summary(
        "conn",
        "2026-07-01",
        "2026-07-02",
        "proj",
        "pull-1",
        account_id="acct-abc",
        selection={"dimension": "day", "filters": {"country": "FR"}, "page_size": 25},
    )
    url = http.request.call_args.args[1]
    params = http.request.call_args.kwargs["params"]
    assert "/acct-abc/reports/campaign-summary/dimensions/day" in url
    assert params["country"] == "FR"
    assert params["page_size"] == 25
    assert params["start_date"] == "2026-07-01" and params["end_date"] == "2026-07-02"


def test_no_environment_fallback_for_the_account(connector, http, monkeypatch):
    """core/account_topology.py:514 declare ces replis deprecies : une variable
    d'environnement est unique pour tout le deploiement, donc TOUS les projets
    tireraient le meme compte."""
    import re

    monkeypatch.setenv("TABOOLA_ACCOUNT_ID", "acct-from-env")
    with pytest.raises(connector.TaboolaOnboardingError):
        connector.pull("conn", "2026-07-01", "2026-07-01", "proj", "pull-1")
    source = (MODULE_DIR / "connector.py").read_text(encoding="utf-8")
    reads = re.findall(
        r"(?:environ(?:\.get)?|getenv)\(\s*[\"']([A-Z0-9_]*(?:ACCOUNT|ADVERTISER)[A-Z0-9_]*)",
        source,
    )
    assert not reads, f"le connecteur lit encore {reads} dans l'environnement"


def test_catalog_zero_planned_and_history_separate():
    cat = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    assert not [f for f in cat["fields"] if f["exposure"] == "planned"]
    assert (
        "raw_taboola_history"
        in (MODULE_DIR / "dbt" / "staging" / "stg_taboola_history.sql").read_text()
    )
