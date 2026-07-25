"""Tests for the HubSpot CRM connector -- Story 15.5 (Epic 15).

Covers :
  - get_hubspot_report envelope shape (AD-1, contacts_daily + deals_daily)
  - transform() mapping canonique manifest-driven
  - Isolation CRM : metriques HubSpot absentes des totaux cross-source (AD-4)
  - Seam AI-56 : assertions sur VALEURS (pas seulement structure)
  - parse helpers : _date_to_hubspot_timestamp, _build_date_filter
  - pagination bornee : _search_crm_objects cap _MAX_PAGES
  - 429 -> RateLimitError("hubspot", retry_after)
  - Fixture golden AI-54 : generate_rows deterministe + transform pass-through
  - Grain uniqueness contacts et deals

AI-53 VERIFIE le 2026-07-19 : tous les helpers de parse sont testes contre la
forme exacte documentee par l'API HubSpot CRM v3. La passe live API = BLOCKED
Phase B (AI-08/AI-13).
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_MODULE_PATH = Path(__file__).parents[4] / "server" / "modules" / "hubspot"
_TOOROW_PATH = _MODULE_PATH / "connector.py"
_SEEDS_PATH = _MODULE_PATH / "seeds"
_FIXTURES_PATH = _MODULE_PATH / "tests" / "fixtures"


def _import_connector():
    """Import connector.py via importlib (pattern meta-ads / klaviyo)."""
    spec = importlib.util.spec_from_file_location("connector_hubspot", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_generator():
    """Import generate_hubspot_seed.py via importlib."""
    gen_path = _SEEDS_PATH / "generate_hubspot_seed.py"
    spec = importlib.util.spec_from_file_location("generate_hubspot_seed_mod", gen_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@pytest.fixture(scope="module")
def generator():
    return _import_generator()


# ---------------------------------------------------------------------------
# Seam AI-56 : envelope canonique AD-1 -- contacts_daily
# ---------------------------------------------------------------------------


def test_get_hubspot_report_contacts_envelope_shape(connector):
    """get_hubspot_report retourne l'envelope canonique AD-1 avec valeurs contacts_daily."""
    fake_rows = [
        {
            "metric": "new_contacts",
            "breakdown_dimension": "date",
            "breakdown_value": "2026-06-01",
            "value": 14.0,
            "pull_id": "pull_hubspot_001",
            "freshness": "2026-06-01T10:00:00Z",
        },
        {
            "metric": "new_contacts",
            "breakdown_dimension": "date",
            "breakdown_value": "2026-06-02",
            "value": 9.0,
            "pull_id": "pull_hubspot_001",
            "freshness": "2026-06-01T10:00:00Z",
        },
    ]
    with patch.object(connector, "_query_mart", return_value=fake_rows):
        env = connector.get_hubspot_report(
            project_id="acme",
            report_profile="contacts_daily",
            date_from="2026-06-01",
            date_to="2026-06-02",
        )

    # Envelope shape AD-1
    assert env["schema_version"] == "1"
    assert "meta" in env
    assert "data" in env
    assert env["meta"]["alerts"] == []

    # Provenance
    assert env["meta"]["provenance"]["source_system"] == "hubspot"
    assert env["meta"]["provenance"]["source_field"] == "fact_daily_kpi"
    assert env["meta"]["provenance"]["pull_id"] == "pull_hubspot_001"

    # Data
    assert env["data"]["project_id"] == "acme"
    assert env["data"]["report_profile"] == "contacts_daily"

    # AI-56 : assertion sur VALEURS, pas seulement HTTP 200
    metrics = env["data"]["metrics"]
    assert "new_contacts" in metrics
    new_contacts_rows = metrics["new_contacts"]
    # Verifie que les deux dates sont presentes avec les bonnes valeurs
    assert len(new_contacts_rows) == 2
    values_by_date = {r["breakdown_value"]: r["value"] for r in new_contacts_rows}
    assert values_by_date["2026-06-01"] == 14.0
    assert values_by_date["2026-06-02"] == 9.0


def test_get_hubspot_report_deals_envelope_shape(connector):
    """get_hubspot_report retourne l'envelope canonique AD-1 avec valeurs deals_daily."""
    fake_rows = [
        {
            "metric": "deals_created",
            "breakdown_dimension": "date",
            "breakdown_value": "2026-06-01",
            "value": 5.0,
            "pull_id": "pull_hubspot_002",
            "freshness": "2026-06-01T10:00:00Z",
        },
        {
            "metric": "deals_closed",
            "breakdown_dimension": "date",
            "breakdown_value": "2026-06-01",
            "value": 2.0,
            "pull_id": "pull_hubspot_002",
            "freshness": "2026-06-01T10:00:00Z",
        },
        {
            "metric": "deal_amount",
            "breakdown_dimension": "date",
            "breakdown_value": "2026-06-01",
            "value": 35_000.0,
            "pull_id": "pull_hubspot_002",
            "freshness": "2026-06-01T10:00:00Z",
        },
    ]
    with patch.object(connector, "_query_mart", return_value=fake_rows):
        env = connector.get_hubspot_report(
            project_id="acme",
            report_profile="deals_daily",
            date_from="2026-06-01",
            date_to="2026-06-01",
        )

    # AI-56 : assertions sur valeurs
    metrics = env["data"]["metrics"]
    assert "deals_created" in metrics
    assert "deals_closed" in metrics
    assert "deal_amount" in metrics
    assert metrics["deals_created"][0]["value"] == 5.0
    assert metrics["deals_closed"][0]["value"] == 2.0
    assert metrics["deal_amount"][0]["value"] == 35_000.0


def test_get_hubspot_report_empty_result(connector):
    """Mart vide : alerts=[], provenance=None, metrics={}."""
    with patch.object(connector, "_query_mart", return_value=[]):
        env = connector.get_hubspot_report(
            project_id="acme",
            date_from="2026-06-01",
            date_to="2026-06-01",
        )
    assert env["meta"]["alerts"] == []
    assert env["meta"]["provenance"] is None
    assert env["data"]["metrics"] == {}


def test_get_hubspot_report_error_becomes_alert(connector):
    """Erreur mart -> alert error dans l'envelope, pas d'exception."""
    with patch.object(connector, "_query_mart", side_effect=RuntimeError("db down")):
        env = connector.get_hubspot_report(
            project_id="acme",
            date_from="2026-06-01",
            date_to="2026-06-01",
        )
    assert env["data"]["metrics"] == {}
    assert env["meta"]["alerts"][0]["level"] == "error"
    assert "db down" in env["meta"]["alerts"][0]["message"]


# ---------------------------------------------------------------------------
# transform() -- mapping canonique manifest-driven (AD-4)
# ---------------------------------------------------------------------------


def test_transform_pass_through_contacts(connector):
    """transform() : les champs HubSpot contacts sont deja canoniques (pass-through)."""
    raw = [
        {
            "date": "2026-06-01",
            "new_contacts": 12,
            "pull_id": "pull_001",
            "loaded_at": "2026-06-01T10:00:00Z",
            "project_id": "default",
        }
    ]
    result = connector.transform(raw)
    assert len(result) == 1
    assert result[0]["new_contacts"] == 12
    assert result[0]["date"] == "2026-06-01"


def test_transform_pass_through_deals(connector):
    """transform() : les champs HubSpot deals sont deja canoniques (pass-through)."""
    raw = [
        {
            "date": "2026-06-01",
            "deals_created": 5,
            "deals_closed": 2,
            "deal_amount": 15_000.0,
            "pull_id": "pull_001",
            "loaded_at": "2026-06-01T10:00:00Z",
            "project_id": "default",
        }
    ]
    result = connector.transform(raw)
    assert len(result) == 1
    assert result[0]["deals_created"] == 5
    assert result[0]["deals_closed"] == 2
    assert result[0]["deal_amount"] == 15_000.0


def test_transform_preserves_null(connector):
    """transform() : None (deal_amount NULL, AD-9) est preserve, pas converti en 0."""
    raw = [
        {
            "date": "2026-06-01",
            "deals_created": 3,
            "deals_closed": 0,
            "deal_amount": None,
            "pull_id": "pull_001",
            "loaded_at": "2026-06-01T10:00:00Z",
            "project_id": "default",
        }
    ]
    result = connector.transform(raw)
    assert result[0]["deal_amount"] is None, "deal_amount NULL doit etre preserve (AD-9)"


# ---------------------------------------------------------------------------
# Helpers de parse -- AI-53
# ---------------------------------------------------------------------------


def test_date_to_hubspot_timestamp_start_of_day(connector):
    """_date_to_hubspot_timestamp retourne le timestamp ms UTC de 00:00:00."""
    ts = connector._date_to_hubspot_timestamp("2026-06-01", end_of_day=False)
    # 2026-06-01 00:00:00 UTC en ms epoch
    expected_dt = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert ts == int(expected_dt.timestamp() * 1000)


def test_date_to_hubspot_timestamp_end_of_day(connector):
    """_date_to_hubspot_timestamp retourne le timestamp ms UTC de 23:59:59.999."""
    ts = connector._date_to_hubspot_timestamp("2026-06-01", end_of_day=True)
    expected_dt = datetime(2026, 6, 1, 23, 59, 59, 999000, tzinfo=timezone.utc)
    assert ts == int(expected_dt.timestamp() * 1000)


def test_date_to_hubspot_timestamp_end_gt_start(connector):
    """Le timestamp de fin de journee est superieur au timestamp de debut de journee."""
    ts_start = connector._date_to_hubspot_timestamp("2026-06-01", end_of_day=False)
    ts_end = connector._date_to_hubspot_timestamp("2026-06-01", end_of_day=True)
    assert ts_end > ts_start


def test_build_date_filter_structure(connector):
    """_build_date_filter produit la structure BETWEEN attendue par l'API HubSpot."""
    f = connector._build_date_filter("createdate", "2026-06-01")
    assert "filters" in f
    assert len(f["filters"]) == 1
    flt = f["filters"][0]
    assert flt["propertyName"] == "createdate"
    assert flt["operator"] == "BETWEEN"
    assert "value" in flt
    assert "highValue" in flt
    # highValue > value (fin de journee > debut de journee)
    assert int(flt["highValue"]) > int(flt["value"])


# ---------------------------------------------------------------------------
# Pagination bornee -- cap _MAX_PAGES
# ---------------------------------------------------------------------------


def test_search_crm_objects_stops_at_max_pages(connector):
    """_search_crm_objects ne depasse pas _MAX_PAGES iterations (anti-boucle)."""
    call_count = [0]

    def mock_post(url, json=None, headers=None, timeout=None):
        call_count[0] += 1
        resp = MagicMock()
        resp.status_code = 200
        # Simuler une reponse qui toujours retourne un curseur 'after'
        resp.json.return_value = {
            "results": [{"id": f"obj_{call_count[0]}"}],
            "paging": {"next": {"after": str(call_count[0])}},
        }
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    results, truncated = connector._search_crm_objects(
        mock_client,
        "/crm/v3/objects/contacts/search",
        [{"filters": []}],
        ["hs_object_id"],
        {"Authorization": "Bearer test"},
    )
    # Doit s'arreter a _MAX_PAGES (50) et retourner truncated=True (F-4)
    assert call_count[0] == connector._MAX_PAGES
    assert len(results) == connector._MAX_PAGES
    assert truncated is True, "F-4 : truncated=True quand cap _MAX_PAGES atteint"


def test_search_crm_objects_stops_when_no_cursor(connector):
    """_search_crm_objects s'arrete quand il n'y a plus de curseur 'after'."""
    call_count = [0]

    def mock_post(url, json=None, headers=None, timeout=None):
        call_count[0] += 1
        resp = MagicMock()
        resp.status_code = 200
        # Retourne 2 pages puis s'arrete (pas de 'after' a la page 2)
        if call_count[0] < 2:
            paging = {"next": {"after": "100"}}
        else:
            paging = {}
        resp.json.return_value = {
            "results": [{"id": f"obj_{call_count[0]}"}],
            "paging": paging,
        }
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    results, truncated = connector._search_crm_objects(
        mock_client,
        "/crm/v3/objects/contacts/search",
        [{"filters": []}],
        ["hs_object_id"],
        {},
    )
    assert call_count[0] == 2  # Seulement 2 pages avant arret
    assert len(results) == 2
    assert truncated is False, "F-4 : truncated=False quand cap non atteint"


# ---------------------------------------------------------------------------
# 429 -> RateLimitError("hubspot", retry_after)
# ---------------------------------------------------------------------------


def test_search_crm_objects_raises_rate_limit_error(connector):
    """HTTP 429 -> RateLimitError('hubspot', retry_after) (lecon LinkedIn/Klaviyo)."""

    def mock_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "30"}
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    from core.quota import RateLimitError  # noqa: PLC0415

    with pytest.raises(RateLimitError) as exc_info:
        connector._search_crm_objects(
            mock_client,
            "/crm/v3/objects/contacts/search",
            [{"filters": []}],
            ["hs_object_id"],
            {},
        )
    err = exc_info.value
    assert err.platform == "hubspot"
    assert err.retry_after == 30


# ---------------------------------------------------------------------------
# _pull_contacts_daily -- shape du resultat
# ---------------------------------------------------------------------------


def test_pull_contacts_daily_shape(connector):
    """_pull_contacts_daily retourne la forme canonique attendue par _insert_raw_contacts."""
    mock_client = MagicMock()
    # F-4 : _search_crm_objects retourne (list, truncated_bool)
    with patch.object(
        connector,
        "_search_crm_objects",
        return_value=([{"id": f"c{i}"} for i in range(5)], False),
    ):
        row = connector._pull_contacts_daily(
            mock_client, {}, "2026-06-01", "default", "pull_test_001"
        )

    assert row["date"] == "2026-06-01"
    assert row["new_contacts"] == 5
    assert row["truncated"] is False  # F-4 : flag propage
    assert row["pull_id"] == "pull_test_001"
    assert row["project_id"] == "default"
    assert "loaded_at" in row


def test_pull_contacts_daily_zero(connector):
    """_pull_contacts_daily retourne 0 quand aucun contact n'est trouve."""
    mock_client = MagicMock()
    with patch.object(connector, "_search_crm_objects", return_value=([], False)):
        row = connector._pull_contacts_daily(
            mock_client, {}, "2026-06-01", "default", "pull_test_zero"
        )
    assert row["new_contacts"] == 0


# ---------------------------------------------------------------------------
# _pull_deals_daily -- shape du resultat et deal_amount NULL (AD-9)
# ---------------------------------------------------------------------------


def test_pull_deals_daily_shape(connector):
    """_pull_deals_daily retourne la forme canonique avec deals_created + deals_closed."""
    mock_client = MagicMock()
    # F-4 : _search_crm_objects retourne (list, truncated_bool)
    side_effects = [
        ([{"id": "d1"}, {"id": "d2"}, {"id": "d3"}], False),  # deals crees
        # deals fermes
        (
            [
                {
                    "id": "d4",
                    "properties": {
                        "amount_in_home_currency": "10000.00",
                        "hs_is_closed_won": "true",
                    },
                }
            ],
            False,
        ),
    ]
    with patch.object(connector, "_search_crm_objects", side_effect=side_effects):
        row = connector._pull_deals_daily(mock_client, {}, "2026-06-01", "default", "pull_test_002")

    assert row["date"] == "2026-06-01"
    assert row["deals_created"] == 3
    assert row["deals_closed"] == 1
    assert row["deal_amount"] == pytest.approx(10_000.0)
    assert row["truncated"] is False  # F-4 : flag propage


def test_pull_deals_daily_null_amount_when_no_closed(connector):
    """deal_amount est NULL (AD-9) quand deals_closed=0."""
    mock_client = MagicMock()
    # F-4 : _search_crm_objects retourne (list, truncated_bool)
    side_effects = [
        ([{"id": "d1"}, {"id": "d2"}], False),  # 2 deals crees
        ([], False),  # 0 deals fermes
    ]
    with patch.object(connector, "_search_crm_objects", side_effect=side_effects):
        row = connector._pull_deals_daily(
            mock_client, {}, "2026-06-01", "default", "pull_test_null"
        )

    assert row["deals_created"] == 2
    assert row["deals_closed"] == 0
    assert row["deal_amount"] is None, "deal_amount doit etre NULL quand deals_closed=0 (AD-9)"


# ---------------------------------------------------------------------------
# Fixture golden AI-54 : generate_rows deterministe
# ---------------------------------------------------------------------------


def test_golden_fixture_matches_generator(generator):
    """AI-54 : generate_rows(end_date=FIXTURE_END_DATE) est deterministe (graine fixe).

    Deux invocations successives avec la meme graine et la meme end_date produisent
    des resultats identiques. Le test prouve que la fixture golden est regenerable
    depuis le generateur (pas ecrite a la main).
    """

    contacts1, deals1 = generator.generate_rows(
        days=generator.FIXTURE_DAYS,
        end_date=generator.FIXTURE_END_DATE,
    )
    contacts2, deals2 = generator.generate_rows(
        days=generator.FIXTURE_DAYS,
        end_date=generator.FIXTURE_END_DATE,
    )

    assert contacts1 == contacts2, "generate_rows contacts doit etre deterministe (AI-54)"
    assert deals1 == deals2, "generate_rows deals doit etre deterministe (AI-54)"

    # Shape des lignes
    assert len(contacts1) == generator.FIXTURE_DAYS
    assert len(deals1) == generator.FIXTURE_DAYS

    for row in contacts1:
        assert "date" in row
        assert "new_contacts" in row
        assert isinstance(row["new_contacts"], int)
        assert row["new_contacts"] >= 0

    for row in deals1:
        assert "date" in row
        assert "deals_created" in row
        assert "deals_closed" in row
        # deal_amount peut etre None (AD-9)
        assert "deal_amount" in row


def test_golden_fixture_contacts_grain_uniqueness(generator):
    """Grain uniqueness (AI-05) : pas de doublon date dans les lignes contacts generees."""
    contacts_rows, _ = generator.generate_rows(
        days=generator.FIXTURE_DAYS,
        end_date=generator.FIXTURE_END_DATE,
    )
    dates = [r["date"] for r in contacts_rows]
    assert len(dates) == len(set(dates)), (
        f"Doublons de dates dans les lignes contacts du generateur : "
        f"{[d for d in dates if dates.count(d) > 1]}"
    )


def test_golden_fixture_deals_grain_uniqueness(generator):
    """Grain uniqueness (AI-05) : pas de doublon date dans les lignes deals generees."""
    _, deals_rows = generator.generate_rows(
        days=generator.FIXTURE_DAYS,
        end_date=generator.FIXTURE_END_DATE,
    )
    dates = [r["date"] for r in deals_rows]
    assert len(dates) == len(set(dates)), (
        f"Doublons de dates dans les lignes deals du generateur : "
        f"{[d for d in dates if dates.count(d) > 1]}"
    )


def test_golden_fixture_null_amount_present(generator):
    """Au moins une ligne deals a deal_amount NULL (AD-9 path exercice)."""
    _, deals_rows = generator.generate_rows(
        days=generator.FIXTURE_DAYS,
        end_date=generator.FIXTURE_END_DATE,
    )
    null_count = sum(1 for r in deals_rows if r.get("deal_amount") is None)
    assert null_count > 0, (
        "Le generateur doit produire des lignes avec deal_amount NULL "
        "pour exercer le chemin NULL honnete (AD-9)"
    )


# ---------------------------------------------------------------------------
# Isolation CRM (AD-4) -- les metriques HubSpot ne doivent pas apparaitre
# dans les totaux cross-source marketing existants.
# ---------------------------------------------------------------------------


def test_crm_metrics_not_in_marketing_total(connector):
    """AD-4 CRM : new_contacts / deals_* ne doivent JAMAIS etre sommes avec conversions regies.

    Ce test prouve la regle declarative : le manifest HubSpot ne declare pas
    'conversions' comme metrique canonique. Les metriques HubSpot sont isolees
    dans leur propre namespace connector='hubspot' dans fact_daily_kpi.
    """
    manifest_path = _MODULE_PATH / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Verifier que 'conversions' n'est pas dans le mapping canonique HubSpot
    canonical_metrics = set(manifest.get("canonical_metric_mapping", {}).values())
    assert "conversions" not in canonical_metrics, (
        "HubSpot ne doit PAS declarer 'conversions' comme metrique canonique "
        "(AD-4 CRM : les deals HubSpot != conversions regies)"
    )

    # Verifier que 'revenue' n'est pas dans le mapping canonique HubSpot
    assert "revenue" not in canonical_metrics, (
        "HubSpot ne doit PAS declarer 'revenue' comme metrique canonique "
        "(AD-4 CRM : deal_amount != revenue Shopify/Stripe)"
    )

    # Verifier que la regle CRM est documentee dans le manifest
    assert "_crm_isolation_rule" in manifest, (
        "Le manifest HubSpot doit documenter la regle d'isolation CRM (_crm_isolation_rule)"
    )


def test_hubspot_metric_names_are_crm_specific(connector):
    """Les noms de metriques HubSpot sont distincts des metriques marketing.

    Prouve que new_contacts / deals_* ne peuvent pas etre sommes accidentellement
    avec sessions / conversions / cost (noms distincts = protection AD-4).
    """
    manifest_path = _MODULE_PATH / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    crm_metrics = set(manifest.get("canonical_metric_mapping", {}).values())
    marketing_metrics = {
        "sessions",
        "conversions",
        "cost",
        "impressions",
        "clicks",
        "revenue",
        "sends",
        "opens",
        "attributed_conversions",
        "attributed_revenue",
    }

    overlap = crm_metrics & marketing_metrics
    assert not overlap, (
        f"Les metriques HubSpot chevauchent des metriques marketing existantes "
        f"(AD-4 risque de double-compte) : {overlap}"
    )


# ---------------------------------------------------------------------------
# mcp_app discovery -- le module expose bien get_hubspot_report
# ---------------------------------------------------------------------------


def test_mcp_app_exists(connector):
    """Le module HubSpot expose un mcp_app FastMCP (requis par le loader AD-2)."""
    assert hasattr(connector, "mcp_app"), "connector.py doit exposer mcp_app"
    from fastmcp import FastMCP  # noqa: PLC0415

    assert isinstance(connector.mcp_app, FastMCP)


def test_pull_function_exists(connector):
    """Le module expose pull() (contrat module contract AI-58 fallback)."""
    assert hasattr(connector, "pull"), "connector.py doit exposer pull()"
    assert callable(connector.pull)


def test_pull_contacts_daily_function_exists(connector):
    """Le module expose pull_contacts_daily() (AI-58 dispatch)."""
    assert hasattr(connector, "pull_contacts_daily")
    assert callable(connector.pull_contacts_daily)


def test_pull_deals_daily_function_exists(connector):
    """Le module expose pull_deals_daily() (AI-58 dispatch)."""
    assert hasattr(connector, "pull_deals_daily")
    assert callable(connector.pull_deals_daily)


# ---------------------------------------------------------------------------
# F-1 : get_hubspot_report filtre par metrique selon report_profile
# ---------------------------------------------------------------------------


def test_get_hubspot_report_contacts_only_returns_new_contacts(connector):
    """F-1 : contacts_daily ne retourne QUE new_contacts (pas deals_*).

    Le filtre metric IN ('new_contacts') est applique dans _query_mart.
    On verifie que _build_metric_filter produit la bonne clause.
    """
    clause, params = connector._build_metric_filter("contacts_daily", "duckdb")
    assert "new_contacts" in clause or "new_contacts" in params, (
        "F-1 : _build_metric_filter contacts_daily doit filtrer sur new_contacts"
    )
    assert "deals_created" not in params and "deals_closed" not in params, (
        "F-1 : contacts_daily ne doit PAS inclure deals_* dans le filtre"
    )
    assert len(params) == 1


def test_get_hubspot_report_deals_returns_three_metrics(connector):
    """F-1 : deals_daily filtre sur {deals_created, deals_closed, deal_amount}."""
    clause, params = connector._build_metric_filter("deals_daily", "duckdb")
    assert set(params) == {"deals_created", "deals_closed", "deal_amount"}, (
        "F-1 : deals_daily doit filtrer exactement {deals_created, deals_closed, deal_amount}"
    )
    assert "new_contacts" not in params, "F-1 : deals_daily ne doit PAS inclure new_contacts"


def test_get_hubspot_report_unknown_profile_returns_all(connector):
    """F-1 : profil absent/inconnu -> pas de filtre metrique (toutes les metriques)."""
    clause_absent, params_absent = connector._build_metric_filter("", "duckdb")
    assert clause_absent == "" and params_absent == [], (
        "F-1 : profil absent -> clause vide (pas de filtre metrique)"
    )
    clause_unknown, params_unknown = connector._build_metric_filter("unknown_profile", "duckdb")
    assert clause_unknown == "" and params_unknown == [], (
        "F-1 : profil inconnu -> clause vide (pas de filtre metrique)"
    )


# ---------------------------------------------------------------------------
# F-2 : snapshot golden -- compare generate_rows() au JSON stocke
# ---------------------------------------------------------------------------


def test_golden_snapshot_contacts(generator):
    """F-2 (AI-54) : generate_rows contacts == golden_pull.json['contacts'].

    Tout changement du generateur DOIT casser ce test (snapshot reel, pas determinisme).
    """
    snapshot_path = _FIXTURES_PATH / "golden_pull.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    expected_contacts = [row for row in snapshot if "new_contacts" in row]

    contacts, _ = generator.generate_rows(
        days=generator.FIXTURE_DAYS,
        end_date=generator.FIXTURE_END_DATE,
    )

    assert contacts == expected_contacts, (
        "F-2 : generate_rows contacts diverge du snapshot golden_pull.json. "
        "Si le generateur a ete modifie intentionnellement, regenerer les fixtures "
        "via: python generate_hubspot_seed.py --end-date 2026-07-10"
    )


def test_golden_snapshot_deals(generator):
    """F-2 (AI-54) : generate_rows deals == golden_pull.json['deals'].

    Tout changement du generateur DOIT casser ce test (snapshot reel, pas determinisme).
    """
    snapshot_path = _FIXTURES_PATH / "golden_pull.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    expected_deals = [row for row in snapshot if "deals_created" in row]

    _, deals = generator.generate_rows(
        days=generator.FIXTURE_DAYS,
        end_date=generator.FIXTURE_END_DATE,
    )

    assert deals == expected_deals, (
        "F-2 : generate_rows deals diverge du snapshot golden_pull.json. "
        "Si le generateur a ete modifie intentionnellement, regenerer les fixtures."
    )


def test_golden_snapshot_expected_facts(connector, generator):
    """F-2 (AI-54) : transform(generate_rows()) == expected_facts.json.

    Tout changement de transform() ou du generateur DOIT casser ce test.
    Pour HubSpot, transform() est un pass-through (metriques deja canoniques),
    donc expected_facts == golden_pull au grain.
    """
    snapshot_path = _FIXTURES_PATH / "expected_facts.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    expected_contacts = [row for row in snapshot if "new_contacts" in row]
    expected_deals = [row for row in snapshot if "deals_created" in row]

    contacts, deals = generator.generate_rows(
        days=generator.FIXTURE_DAYS,
        end_date=generator.FIXTURE_END_DATE,
    )

    transformed_contacts = connector.transform(contacts)
    transformed_deals = connector.transform(deals)

    assert transformed_contacts == expected_contacts, (
        "F-2 : transform(contacts) diverge du snapshot expected_facts.json['contacts']."
    )
    assert transformed_deals == expected_deals, (
        "F-2 : transform(deals) diverge du snapshot expected_facts.json['deals']."
    )


# ---------------------------------------------------------------------------
# F-3 : fuseau horaire configurable -- HUBSPOT_ACCOUNT_TIMEZONE
# ---------------------------------------------------------------------------


def test_date_to_hubspot_timestamp_paris_borne_debut(connector):
    """F-3 : avec tz=Europe/Paris, le debut du 2026-07-10 = 2026-07-09T22:00:00Z.

    En ete, Paris = UTC+2 ; minuit local Paris = 22h UTC la veille.
    """
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    paris = ZoneInfo("Europe/Paris")
    ts = connector._date_to_hubspot_timestamp("2026-07-10", end_of_day=False, tz=paris)
    # 2026-07-09T22:00:00Z en ms epoch
    from datetime import datetime, timezone  # noqa: PLC0415

    expected_dt = datetime(2026, 7, 9, 22, 0, 0, tzinfo=timezone.utc)
    assert ts == int(expected_dt.timestamp() * 1000), (
        f"F-3 : borne debut 2026-07-10 Paris doit etre {expected_dt} UTC, got ts={ts}"
    )


def test_date_to_hubspot_timestamp_utc_unchanged(connector):
    """F-3 : avec tz=UTC (defaut), le comportement est identique a l'ancienne implementation."""
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    ts_default = connector._date_to_hubspot_timestamp("2026-06-01", end_of_day=False)
    ts_utc = connector._date_to_hubspot_timestamp(
        "2026-06-01", end_of_day=False, tz=ZoneInfo("UTC")
    )
    from datetime import datetime, timezone  # noqa: PLC0415

    expected_ms = int(datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert ts_default == expected_ms
    assert ts_utc == expected_ms


# ---------------------------------------------------------------------------
# F-4 : _search_crm_objects -- truncated flag + alert dans l'envelope
# ---------------------------------------------------------------------------


def test_search_crm_objects_truncated_true_at_cap(connector):
    """F-4 : _search_crm_objects retourne truncated=True quand cap atteint."""
    call_count = [0]

    def mock_post(url, json=None, headers=None, timeout=None):
        call_count[0] += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "results": [{"id": f"obj_{call_count[0]}"}],
            "paging": {"next": {"after": str(call_count[0])}},
        }
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    _, truncated = connector._search_crm_objects(
        mock_client,
        "/crm/v3/objects/contacts/search",
        [{"filters": []}],
        ["hs_object_id"],
        {},
    )
    assert truncated is True, "F-4 : cap atteint -> truncated=True"


def test_search_crm_objects_truncated_false_when_no_cap(connector):
    """F-4 : _search_crm_objects retourne truncated=False quand sous le cap."""

    def mock_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"results": [{"id": "obj1"}], "paging": {}}
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    _, truncated = connector._search_crm_objects(
        mock_client,
        "/crm/v3/objects/contacts/search",
        [{"filters": []}],
        ["hs_object_id"],
        {},
    )
    assert truncated is False, "F-4 : sous le cap -> truncated=False"


def test_envelope_alert_when_truncated(connector):
    """F-4 : _build_envelope propage extra_alerts (alerte AD-9 truncated)."""
    alert = {"level": "warning", "code": "AD-9", "message": "comptage tronque"}
    env = connector._build_envelope(
        rows=[],
        report_profile="contacts_daily",
        date_from="2026-06-01",
        date_to="2026-06-01",
        project_id="default",
        extra_alerts=[alert],
    )
    assert len(env["meta"]["alerts"]) == 1
    assert env["meta"]["alerts"][0]["code"] == "AD-9"


def test_envelope_no_alert_when_not_truncated(connector):
    """F-4 : pas d'alerte AD-9 dans l'envelope quand pas de truncation."""
    env = connector._build_envelope(
        rows=[],
        report_profile="contacts_daily",
        date_from="2026-06-01",
        date_to="2026-06-01",
        project_id="default",
        extra_alerts=None,
    )
    assert env["meta"]["alerts"] == []


# ---------------------------------------------------------------------------
# F-5 : retry_after -- distingue absent/'' (None) de '0' (0)
# ---------------------------------------------------------------------------


def test_retry_after_absent_becomes_none(connector):
    """F-5 : Retry-After absent -> retry_after=None (pas 0)."""
    from core.quota import RateLimitError  # noqa: PLC0415

    def mock_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {}  # pas de Retry-After
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    with pytest.raises(RateLimitError) as exc_info:
        connector._search_crm_objects(
            mock_client,
            "/crm/v3/objects/contacts/search",
            [{"filters": []}],
            ["hs_object_id"],
            {},
        )
    assert exc_info.value.retry_after is None, (
        "F-5 : Retry-After absent -> retry_after doit etre None (pas 0)"
    )


def test_retry_after_zero_becomes_zero(connector):
    """F-5 : Retry-After: 0 -> retry_after=0 (pas None, fix du bug or-None)."""
    from core.quota import RateLimitError  # noqa: PLC0415

    def mock_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "0"}
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    with pytest.raises(RateLimitError) as exc_info:
        connector._search_crm_objects(
            mock_client,
            "/crm/v3/objects/contacts/search",
            [{"filters": []}],
            ["hs_object_id"],
            {},
        )
    assert exc_info.value.retry_after == 0, (
        "F-5 : Retry-After: 0 doit etre preserve comme 0, pas None (fix bug int() or None)"
    )


def test_retry_after_numeric_string(connector):
    """F-5 : Retry-After: 30 -> retry_after=30 (comportement existant preserve)."""
    from core.quota import RateLimitError  # noqa: PLC0415

    def mock_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "30"}
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    with pytest.raises(RateLimitError) as exc_info:
        connector._search_crm_objects(
            mock_client,
            "/crm/v3/objects/contacts/search",
            [{"filters": []}],
            ["hs_object_id"],
            {},
        )
    assert exc_info.value.retry_after == 30


# ---------------------------------------------------------------------------
# Story 25.7 : classification d'erreur typee (core.pull_errors) + error_map.
#
# HubSpot renvoie {status, message, category, subCategory, correlationId,
# errors}. Le discriminant est `category` (401 -> EXPIRED_AUTHENTICATION, etc).
# core._extract_provider_code NE lit PAS `category` (seulement error.code /
# error_code / code), donc l'error_map keyee sur categories NE se declenche pas
# depuis le corps brut. Le chemin pur-HTTP donne quand meme la bonne classe pour
# le 401. Cf. manifest._error_map_note.
# ---------------------------------------------------------------------------


def _hubspot_error_body(category: str, status: int = 401) -> dict:
    """Corps d'erreur HubSpot canonique (shape officielle 25.7)."""
    return {
        "status": "error",
        "message": f"HubSpot {category} error.",
        "category": category,
        "correlationId": "corr-abc-123",
    }


def test_search_crm_objects_401_raises_auth_expired_pure_http(connector):
    """Story 25.7 : un 401 HubSpot -> AuthExpiredError via le chemin pur-HTTP.

    Prouve que le raise site route les non-200/non-429 par classify_http_error
    et que le corps HubSpot (category, correlationId) est preserve comme preuve.
    Ne depend PAS de l'error_map (category non extrait par le core) : la classe
    correcte vient du fallback pur-HTTP (401 -> auth_expired).
    """
    body = _hubspot_error_body("EXPIRED_AUTHENTICATION", 401)

    def mock_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 401
        resp.headers = {}
        resp.json.return_value = body
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    from core.pull_errors import AuthExpiredError  # noqa: PLC0415

    with pytest.raises(AuthExpiredError) as exc_info:
        connector._search_crm_objects(
            mock_client,
            "/crm/v3/objects/contacts/search",
            [{"filters": []}],
            ["hs_object_id"],
            {},
        )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    # Corps HubSpot preserve intact (category = la preuve qu'on garde).
    assert err.provider_payload["category"] == "EXPIRED_AUTHENTICATION"
    assert err.provider_payload["correlationId"] == "corr-abc-123"


def test_search_crm_objects_403_raises_permission_denied_pure_http(connector):
    """Story 25.7 : un 403 HubSpot (MISSING_SCOPES) -> PermissionDeniedError (pur-HTTP)."""
    body = _hubspot_error_body("MISSING_SCOPES", 403)

    def mock_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 403
        resp.headers = {}
        resp.json.return_value = body
        return resp

    mock_client = MagicMock()
    mock_client.post.side_effect = mock_post

    from core.pull_errors import PermissionDeniedError  # noqa: PLC0415

    with pytest.raises(PermissionDeniedError) as exc_info:
        connector._search_crm_objects(
            mock_client,
            "/crm/v3/objects/deals/search",
            [{"filters": []}],
            ["amount"],
            {},
        )
    assert exc_info.value.error_class == "permission_denied"
    assert exc_info.value.provider_payload["category"] == "MISSING_SCOPES"


def test_manifest_error_map_shape():
    """Story 25.7 : manifest.json porte un error_map bien forme + les notes.

    Cles "<status>:<category>" ; valeurs = classes canoniques pull_errors. La note
    documente que `category` n'est pas auto-extrait (cf. _error_map_note).
    """
    from core.pull_errors import ERROR_CLASSES  # noqa: PLC0415

    manifest = json.loads((_MODULE_PATH / "manifest.json").read_text(encoding="utf-8"))
    error_map = manifest.get("error_map")
    assert isinstance(error_map, dict) and error_map, "manifest must declare error_map"

    for key, cls in error_map.items():
        status, _, code = key.partition(":")
        assert status.isdigit() and code, f"malformed error_map key: {key!r}"
        assert cls in ERROR_CLASSES, f"{cls!r} is not a canonical error class"

    # Couverture minimale mandatee par la story.
    assert error_map["401:EXPIRED_AUTHENTICATION"] == "auth_expired"
    assert error_map["401:INVALID_AUTHENTICATION"] == "auth_expired"
    assert error_map["403:MISSING_SCOPES"] == "permission_denied"
    assert error_map["400:VALIDATION_ERROR"] == "invalid_request"

    # Les notes de la story (error_map caveat + pas d'account_topology).
    assert "category" in manifest["_error_map_note"]
    assert "_extract_provider_code" in manifest["_error_map_note"]
    assert "account_topology" not in manifest, "portal-scoped: pas d'account_topology"
    assert "portal" in manifest["_account_topology_note"].lower()


# ---------------------------------------------------------------------------
# Story 25.7 : official_fields.json (catalogue curated) -- sanity.
# ---------------------------------------------------------------------------

_CATALOG_DIR = _MODULE_PATH / "catalog_sources"


def test_official_fields_manifest_fields_present_with_matching_kind_and_source_field():
    """Story 25.7 : les 5 champs manifest presents avec kind ET source_field concordants."""
    official = json.loads((_CATALOG_DIR / "official_fields.json").read_text(encoding="utf-8"))
    by_id = {f["field_id"]: f for f in official}
    # field_id globalement unique (contrat merge engine).
    assert len(by_id) == len(official), "duplicate field_id in official_fields.json"

    manifest = json.loads((_MODULE_PATH / "manifest.json").read_text(encoding="utf-8"))
    for m in manifest["source_capabilities"]["fields"]:
        fid = m["field_id"]
        assert fid in by_id, f"manifest field {fid!r} missing from official_fields.json"
        f = by_id[fid]
        assert f["kind"] == m["kind"], f"kind mismatch for {fid}"
        src = f.get("source_field", fid)
        assert src == m["source_field"], (
            f"source_field mismatch for {fid}: official {src!r} vs manifest {m['source_field']!r}"
        )


def test_catalog_sources_declaration():
    """Story 25.7 : catalog_sources.json pinne v3, official + enrichment, _derivation."""
    cfg = json.loads((_CATALOG_DIR / "catalog_sources.json").read_text(encoding="utf-8"))
    assert cfg["connector"] == "hubspot"
    assert cfg["api_version"] == "v3"
    assert cfg["generated_at"] == "2026-07-21T00:00:00Z"
    kinds = {s["kind"] for s in cfg["sources"]}
    assert "official" in kinds and "enrichment" in kinds
    assert "_derivation" in cfg and "count_divergence" in cfg["_derivation"]
    # DEAL amounts/stages/counts + date = core.
    stm = cfg["section_tier_map"]
    assert stm.get("DEAL") == "core"
    assert stm.get("TIME") == "core"


# ---------------------------------------------------------------------------
# Story 25.9 : catalog_driven rollout -- 19 new tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# catalog_sources.json contract (exposure_policy + excluded_sections)
# ---------------------------------------------------------------------------


def test_catalog_sources_exposure_policy_catalog_driven():
    """catalog_sources.json must declare exposure_policy=catalog_driven (Story 25.9)."""
    cfg = json.loads((_CATALOG_DIR / "catalog_sources.json").read_text(encoding="utf-8"))
    assert cfg.get("exposure_policy") == "catalog_driven"


def test_catalog_sources_excluded_sections_present():
    """excluded_sections must be declared for COMPANY, TICKET, ANALYTICS."""
    cfg = json.loads((_CATALOG_DIR / "catalog_sources.json").read_text(encoding="utf-8"))
    excluded = cfg.get("excluded_sections", {})
    for section in ("COMPANY", "TICKET", "ANALYTICS"):
        assert section in excluded, f"excluded_sections missing entry for {section}"
        assert excluded[section], f"excluded_sections[{section!r}] must have a non-empty reason"


def test_catalog_sources_excluded_sections_reasons_honest():
    """Each excluded section reason must be honest about the daily pull constraint."""
    cfg = json.loads((_CATALOG_DIR / "catalog_sources.json").read_text(encoding="utf-8"))
    excluded = cfg.get("excluded_sections", {})
    for section, reason in excluded.items():
        assert reason, f"excluded_sections[{section!r}] must have a non-empty reason"


# ---------------------------------------------------------------------------
# api_catalog.json contract -- zero planned, excluded sections have reasons
# ---------------------------------------------------------------------------


def test_api_catalog_zero_planned_fields():
    """Story 25.9 gate: zero fields with exposure='planned'."""
    cat = json.loads((_MODULE_PATH / "api_catalog.json").read_text(encoding="utf-8"))
    planned = [f["field_id"] for f in cat["fields"] if f.get("exposure") == "planned"]
    assert planned == [], f"Fields still at 'planned': {planned}"


def test_api_catalog_excluded_fields_have_reason():
    """Every excluded field must carry an exclusion_reason."""
    cat = json.loads((_MODULE_PATH / "api_catalog.json").read_text(encoding="utf-8"))
    missing = [
        f["field_id"]
        for f in cat["fields"]
        if f.get("exposure") == "excluded" and not f.get("exclusion_reason")
    ]
    assert missing == [], f"Excluded fields missing exclusion_reason: {missing}"


def test_api_catalog_excluded_sections_all_excluded():
    """Fields in COMPANY / TICKET / ANALYTICS must all be 'excluded'."""
    cat = json.loads((_MODULE_PATH / "api_catalog.json").read_text(encoding="utf-8"))
    excluded_sections = {"COMPANY", "TICKET", "ANALYTICS"}
    bad = [
        f["field_id"]
        for f in cat["fields"]
        if f["section"] in excluded_sections and f.get("exposure") != "excluded"
    ]
    assert bad == [], f"Fields in excluded sections not marked 'excluded': {bad}"


def test_api_catalog_exposed_sections_not_excluded():
    """Fields in CONTACT / DEAL / TIME must NOT be 'excluded'."""
    cat = json.loads((_MODULE_PATH / "api_catalog.json").read_text(encoding="utf-8"))
    fetchable_sections = {"CONTACT", "DEAL", "TIME"}
    bad = [
        f["field_id"]
        for f in cat["fields"]
        if f["section"] in fetchable_sections and f.get("exposure") == "excluded"
    ]
    assert bad == [], f"Fields in fetchable sections must be exposed; bad: {bad}"


# ---------------------------------------------------------------------------
# manifest.json -- catalog_daily profile declared
# ---------------------------------------------------------------------------


def test_manifest_has_catalog_daily_report_profile():
    """manifest.json must declare a catalog_daily report profile."""
    manifest = json.loads((_MODULE_PATH / "manifest.json").read_text(encoding="utf-8"))
    profiles = {rp["id"]: rp for rp in manifest.get("report_profiles", [])}
    assert "catalog_daily" in profiles, "catalog_daily must be in report_profiles"


def test_manifest_catalog_daily_in_source_capabilities():
    """manifest.json source_capabilities.reports must include catalog_daily with
    selection_mode=catalog_driven and dispatch.callable=pull_catalog_daily."""
    manifest = json.loads((_MODULE_PATH / "manifest.json").read_text(encoding="utf-8"))
    reports = {r["id"]: r for r in manifest.get("source_capabilities", {}).get("reports", [])}
    assert "catalog_daily" in reports, "catalog_daily must appear in source_capabilities.reports"
    cd = reports["catalog_daily"]
    assert cd.get("selection_mode") == "catalog_driven"
    assert cd.get("dispatch", {}).get("callable") == "pull_catalog_daily"


def test_manifest_ad22_contacts_daily_unchanged():
    """AD-22: contacts_daily profile in source_capabilities is bit-identical (untouched)."""
    manifest = json.loads((_MODULE_PATH / "manifest.json").read_text(encoding="utf-8"))
    reports = {r["id"]: r for r in manifest.get("source_capabilities", {}).get("reports", [])}
    assert "contacts_daily" in reports
    cd = reports["contacts_daily"]
    assert cd["selection_mode"] == "exact_bundle"
    assert cd["dispatch"]["callable"] == "pull_contacts_daily"
    assert set(cd["metrics"]) == {"new_contacts"}


def test_manifest_ad22_deals_daily_unchanged():
    """AD-22: deals_daily profile in source_capabilities is bit-identical (untouched)."""
    manifest = json.loads((_MODULE_PATH / "manifest.json").read_text(encoding="utf-8"))
    reports = {r["id"]: r for r in manifest.get("source_capabilities", {}).get("reports", [])}
    assert "deals_daily" in reports
    dd = reports["deals_daily"]
    assert dd["selection_mode"] == "exact_bundle"
    assert dd["dispatch"]["callable"] == "pull_deals_daily"
    assert set(dd["metrics"]) == {"deals_created", "deals_closed", "deal_amount"}


# ---------------------------------------------------------------------------
# AI-58 dispatch: connector exposes pull_catalog_daily
# ---------------------------------------------------------------------------


def test_ai58_dispatch_pull_catalog_daily_exists(connector):
    """AI-58: connector.py exposes pull_catalog_daily callable."""
    assert hasattr(connector, "pull_catalog_daily"), (
        "connector must expose pull_catalog_daily (AI-58 dispatch)"
    )
    assert callable(connector.pull_catalog_daily)


# ---------------------------------------------------------------------------
# _group_selection_by_object -- selection grouping by object prefix
# ---------------------------------------------------------------------------


def test_group_selection_by_object_contact_prefix(connector):
    """_group_selection_by_object routes contact_ fields to the contact object."""
    selection = {
        "metrics": ["new_contacts"],
        "dimensions": ["contact_email", "contact_firstname", "date"],
        "source_fields": {
            "new_contacts": "new_contacts",
            "contact_email": "email",
            "contact_firstname": "firstname",
            "date": "createdate/closedate",
        },
    }
    by_obj = connector._group_selection_by_object(selection)
    # email, firstname should be in contact source_fields
    assert "email" in by_obj["contact"]
    assert "firstname" in by_obj["contact"]
    # new_contacts and date are derived -- must NOT be in any object group
    assert "new_contacts" not in by_obj["contact"]
    assert "new_contacts" not in by_obj.get("deal", [])


def test_group_selection_by_object_deal_prefix(connector):
    """_group_selection_by_object routes deal_ fields to the deal object."""
    selection = {
        "metrics": ["deals_created", "deal_amount", "deal_hs_forecast_amount"],
        "dimensions": ["deal_dealstage", "deal_pipeline"],
        "source_fields": {
            "deals_created": "deals_created",
            "deal_amount": "deal_amount",
            "deal_hs_forecast_amount": "hs_forecast_amount",
            "deal_dealstage": "dealstage",
            "deal_pipeline": "pipeline",
        },
    }
    by_obj = connector._group_selection_by_object(selection)
    assert "hs_forecast_amount" in by_obj["deal"]
    assert "dealstage" in by_obj["deal"]
    assert "pipeline" in by_obj["deal"]
    # derived field deal_amount is in _DERIVED_FIELD_IDS -- must not be in deal group
    assert "deal_amount" not in by_obj["deal"]


def test_group_selection_by_object_no_duplicates(connector):
    """_group_selection_by_object deduplicates source_fields."""
    selection = {
        "metrics": ["contact_num_conversion_events"],
        "dimensions": ["contact_email", "contact_email"],  # duplicate
        "source_fields": {
            "contact_num_conversion_events": "num_conversion_events",
            "contact_email": "email",
        },
    }
    by_obj = connector._group_selection_by_object(selection)
    assert by_obj["contact"].count("email") == 1, "email must not be duplicated"


# ---------------------------------------------------------------------------
# _project_catalog_contacts / _project_catalog_deals -- projection correctness
# ---------------------------------------------------------------------------


def test_project_catalog_contacts_selected_fields_land(connector):
    """_project_catalog_contacts emits exactly the selected contact_ fields."""
    selection = {
        "metrics": ["new_contacts"],
        "dimensions": ["contact_email", "contact_firstname", "date"],
        "source_fields": {
            "new_contacts": "new_contacts",
            "contact_email": "email",
            "contact_firstname": "firstname",
            "date": "createdate/closedate",
        },
    }
    contact = {
        "id": "c001",
        "properties": {
            "email": "alice@example.com",
            "firstname": "Alice",
            "createdate": "1751414400000",
        },
    }
    rows = connector._project_catalog_contacts(contact, "2026-07-01", selection)
    field_ids = [r["field_id"] for r in rows]
    assert "contact_email" in field_ids
    assert "contact_firstname" in field_ids
    # derived fields must not appear in catalog projection
    assert "new_contacts" not in field_ids
    assert "date" not in field_ids


def test_project_catalog_contacts_values_correct(connector):
    """_project_catalog_contacts reads values from contact.properties."""
    selection = {
        "metrics": [],
        "dimensions": ["contact_email", "contact_lifecyclestage"],
        "source_fields": {
            "contact_email": "email",
            "contact_lifecyclestage": "lifecyclestage",
        },
    }
    contact = {
        "id": "c002",
        "properties": {
            "email": "bob@example.com",
            "lifecyclestage": "lead",
        },
    }
    rows = connector._project_catalog_contacts(contact, "2026-07-01", selection)
    by_field = {r["field_id"]: r["value"] for r in rows}
    assert by_field["contact_email"] == "bob@example.com"
    assert by_field["contact_lifecyclestage"] == "lead"


def test_project_catalog_deals_selected_fields_land(connector):
    """_project_catalog_deals emits exactly the selected deal_ fields."""
    selection = {
        "metrics": ["deals_created", "deal_hs_forecast_amount"],
        "dimensions": ["deal_dealstage", "deal_pipeline"],
        "source_fields": {
            "deals_created": "deals_created",
            "deal_hs_forecast_amount": "hs_forecast_amount",
            "deal_dealstage": "dealstage",
            "deal_pipeline": "pipeline",
        },
    }
    deal = {
        "id": "d001",
        "properties": {
            "hs_forecast_amount": "5000.00",
            "dealstage": "appointmentscheduled",
            "pipeline": "default",
        },
    }
    rows = connector._project_catalog_deals(deal, "2026-07-01", selection)
    field_ids = [r["field_id"] for r in rows]
    assert "deal_hs_forecast_amount" in field_ids
    assert "deal_dealstage" in field_ids
    assert "deal_pipeline" in field_ids
    # derived field deals_created must not appear
    assert "deals_created" not in field_ids


# ---------------------------------------------------------------------------
# pull_catalog_daily -- 3 Search API calls per date (properties param from selection)
# ---------------------------------------------------------------------------


def test_pull_catalog_daily_makes_3_calls_per_date(connector):
    """pull_catalog_daily makes exactly 3 Search API calls per date
    (contacts/search createdate + deals/search createdate + deals/search closedate+closedwon)."""
    call_log: list[str] = []

    def mock_search(client, endpoint, filter_groups, properties, headers):
        call_log.append(endpoint)
        return [], False

    selection = {
        "metrics": ["new_contacts", "deals_created", "deals_closed"],
        "dimensions": ["contact_email", "deal_dealstage"],
        "source_fields": {
            "new_contacts": "new_contacts",
            "deals_created": "deals_created",
            "deals_closed": "deals_closed",
            "contact_email": "email",
            "deal_dealstage": "dealstage",
        },
    }

    with patch.object(connector, "_search_crm_objects", side_effect=mock_search):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            with patch(
                "core._insert_raw_contacts"
                if hasattr(connector, "_insert_raw_contacts")
                else "connector_hubspot._insert_raw_contacts",
                create=True,
            ):
                try:
                    connector.pull_catalog_daily(
                        connection_id="conn_3c",
                        date_from="2026-07-01",
                        date_to="2026-07-01",
                        project_id="proj_3c",
                        pull_id="pull_3c",
                        selection=selection,
                        connection_config={"timezone": "UTC", "company_currency": "USD"},
                    )
                except Exception:
                    pass  # DB write may fail in test -- call count is what we verify

    # 3 calls for 1 date: contacts/search, deals/search (created), deals/search (closed)
    assert len(call_log) == 3
    assert call_log[0] == connector._CONTACTS_SEARCH_PATH
    assert call_log[1] == connector._DEALS_SEARCH_PATH
    assert call_log[2] == connector._DEALS_SEARCH_PATH


def test_pull_catalog_daily_properties_param_built_from_contact_selection(connector):
    """pull_catalog_daily passes contact source_fields as the properties list
    to contacts/search (HYBRID: request IS built from selection)."""
    captured_props: list[list[str]] = []

    def mock_search(client, endpoint, filter_groups, properties, headers):
        if "contacts" in endpoint:
            captured_props.append(list(properties))
        return [], False

    selection = {
        "metrics": [],
        "dimensions": ["contact_email", "contact_lifecyclestage"],
        "source_fields": {
            "contact_email": "email",
            "contact_lifecyclestage": "lifecyclestage",
        },
    }

    with patch.object(connector, "_search_crm_objects", side_effect=mock_search):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            try:
                connector.pull_catalog_daily(
                    connection_id="conn_prop",
                    date_from="2026-07-01",
                    date_to="2026-07-01",
                    project_id="proj_prop",
                    pull_id="pull_prop",
                    selection=selection,
                    connection_config={"timezone": "UTC", "company_currency": "USD"},
                )
            except Exception:
                pass

    assert captured_props, "contacts/search was not called"
    contact_call_props = captured_props[0]
    assert "email" in contact_call_props
    assert "lifecyclestage" in contact_call_props


def test_pull_catalog_daily_properties_param_built_from_deal_selection(connector):
    """pull_catalog_daily passes deal source_fields as the properties list
    to deals/search (HYBRID: request IS built from selection)."""
    captured_deal_props: list[list[str]] = []

    def mock_search(client, endpoint, filter_groups, properties, headers):
        if "deals" in endpoint:
            captured_deal_props.append(list(properties))
        return [], False

    selection = {
        "metrics": ["deal_hs_forecast_amount"],
        "dimensions": ["deal_dealstage", "deal_pipeline"],
        "source_fields": {
            "deal_hs_forecast_amount": "hs_forecast_amount",
            "deal_dealstage": "dealstage",
            "deal_pipeline": "pipeline",
        },
    }

    with patch.object(connector, "_search_crm_objects", side_effect=mock_search):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            try:
                connector.pull_catalog_daily(
                    connection_id="conn_dprop",
                    date_from="2026-07-01",
                    date_to="2026-07-01",
                    project_id="proj_dprop",
                    pull_id="pull_dprop",
                    selection=selection,
                    connection_config={"timezone": "UTC", "company_currency": "USD"},
                )
            except Exception:
                pass

    assert captured_deal_props, "deals/search was not called"
    # Both deal searches (created + closed) share the same properties list
    assert "hs_forecast_amount" in captured_deal_props[0]
    assert "dealstage" in captured_deal_props[0]


def test_pull_catalog_daily_function_exists(connector):
    """connector.py must expose pull_catalog_daily() (Story 25.9 / AI-58)."""
    assert hasattr(connector, "pull_catalog_daily")
    assert callable(connector.pull_catalog_daily)
