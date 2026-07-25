"""Tests unitaires du connecteur LinkedIn Ads -- Story 15.3 (Epic 15).

Couvre :
  - Pagination bornee (pas de while True infini) : adAnalytics ne pagine pas,
    le connecteur consomme la reponse unique et log un avertissement si elle
    approche la limite 15 000 elements.
  - 429 -> RateLimitError("linkedin-ads", retry_after) : AD-3 conforme.
  - Token AD-3 jamais stocke / jamais logge.
  - _parse_report_row : date, campaign_id extrait de l'URN, metriques, data_level.
  - _is_api_shaped : detecte les lignes API vs lignes parsees.
  - transform() : rename costInLocalCurrency -> cost via le manifest.
  - Seam build_asgi_app (AI-56) : appel get_linkedin_ads_report avec seed multi-jours,
    assertions sur les VALEURS (pas seulement HTTP 200).
  - pull_campaign_daily / pull_campaign_group_daily : dispatch AI-58.
  - Conformance AI-54 : golden fixture passee par _parse_report_row + transform().

LIVE API = BLOCKED Phase B (AI-08/AI-13) : OAuth LinkedIn non disponible en dev.
Toutes les assertions d'appel API sont mockees via respx/unittest.mock.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Variables d'environnement pour isoler les tests du scheduler/health-poller.
# ---------------------------------------------------------------------------
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# Import du module a tester
import sys as _sys

_sys.path.insert(0, str(Path(__file__).parents[2]))
import importlib.util as _ilu

_MODULE_PATH = Path(__file__).parents[2] / "modules" / "linkedin-ads" / "connector.py"
_spec = _ilu.spec_from_file_location("linkedin_ads_connector", _MODULE_PATH)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_parse_report_row = _mod._parse_report_row
_is_api_shaped = _mod._is_api_shaped
_parse_date_range = _mod._parse_date_range
transform = _mod.transform
DATA_LEVEL_CAMPAIGN = _mod.DATA_LEVEL_CAMPAIGN
DATA_LEVEL_CAMPAIGN_GROUP = _mod.DATA_LEVEL_CAMPAIGN_GROUP

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parents[1] / ".." / "modules" / "linkedin-ads" / "tests" / "fixtures"

_SAMPLE_API_ROW = {
    "pivotValues": ["urn:li:sponsoredCampaign:88000001"],
    "dateRange": {
        "start": {"year": 2026, "month": 6, "day": 9},
        "end": {"year": 2026, "month": 6, "day": 9},
    },
    "costInLocalCurrency": "42.31",
    "impressions": 2848,
    "clicks": 18,
    "externalWebsiteConversions": 1,
    "leadGenerationMailContactInfoShares": 3,
}


# ===========================================================================
# Tests : _parse_date_range
# ===========================================================================

def test_parse_date_range_standard():
    dr = {"start": {"year": 2026, "month": 6, "day": 9}}
    assert _parse_date_range(dr) == "2026-06-09"


def test_parse_date_range_empty():
    assert _parse_date_range({}) == ""
    assert _parse_date_range(None) == ""


# ===========================================================================
# Tests : _is_api_shaped
# ===========================================================================

def test_is_api_shaped_detects_raw_linkedin_row():
    """Une reponse API LinkedIn (pivot + dateRange) est reconnue comme api-shaped."""
    assert _is_api_shaped(_SAMPLE_API_ROW) is True


def test_is_api_shaped_rejects_parsed_row():
    """Une ligne deja parsee (noms source dict) n'est pas api-shaped."""
    parsed = _parse_report_row(_SAMPLE_API_ROW, "campaign_daily")
    assert _is_api_shaped(parsed) is False


# ===========================================================================
# Tests : _parse_report_row
# ===========================================================================

def test_parse_report_row_campaign_daily():
    """Verifie l'extraction structurelle d'une ligne API LinkedIn (campaign)."""
    row = _parse_report_row(_SAMPLE_API_ROW, "campaign_daily")

    assert row["date"] == "2026-06-09"
    assert row["data_level"] == DATA_LEVEL_CAMPAIGN
    assert row["campaign_id"] == "88000001"
    assert row["campaign_group_id"] is None  # grain campaign : group_id NULL
    assert row["costInLocalCurrency"] == pytest.approx(42.31, rel=1e-3)
    assert row["impressions"] == 2848
    assert row["clicks"] == 18
    assert row["externalWebsiteConversions"] == 1
    assert row["leadGenerationMailContactInfoShares"] == 3


def test_parse_report_row_campaign_group_daily():
    """Verifie l'extraction au grain campaign_group."""
    group_api_row = {
        "pivotValues": ["urn:li:sponsoredCampaignGroup:99000001"],
        "dateRange": {
            "start": {"year": 2026, "month": 6, "day": 9},
            "end": {"year": 2026, "month": 6, "day": 9},
        },
        "costInLocalCurrency": "101.08",
        "impressions": 6762,
        "clicks": 43,
        "externalWebsiteConversions": 3,
        "leadGenerationMailContactInfoShares": 8,
    }
    row = _parse_report_row(group_api_row, "campaign_group_daily")

    assert row["data_level"] == DATA_LEVEL_CAMPAIGN_GROUP
    assert row["campaign_group_id"] == "99000001"
    assert row["campaign_id"] is None  # grain group : campaign_id NULL


def test_parse_report_row_missing_lead_field():
    """Le champ leads peut etre absent de la reponse API -> None (pas 0)."""
    row_no_leads = {k: v for k, v in _SAMPLE_API_ROW.items()
                    if k != "leadGenerationMailContactInfoShares"}
    row = _parse_report_row(row_no_leads, "campaign_daily")
    assert row["leadGenerationMailContactInfoShares"] is None


def test_parse_report_row_empty_pivot():
    """pivotValues vide -> campaign_id None (valeur inconnue, not set)."""
    row_no_pivot = dict(_SAMPLE_API_ROW, pivotValues=[])
    row = _parse_report_row(row_no_pivot, "campaign_daily")
    assert row["campaign_id"] is None


# ===========================================================================
# Tests : transform()
# ===========================================================================

def test_transform_renames_cost_in_local_currency():
    """transform() renomme costInLocalCurrency -> cost via le manifest."""
    raw_rows = [_SAMPLE_API_ROW]
    result = transform(raw_rows)

    assert len(result) == 1
    r = result[0]
    assert "cost" in r, "costInLocalCurrency doit etre renomme en 'cost'"
    assert "costInLocalCurrency" not in r, "Le nom source ne doit pas survivre dans le canonique"
    assert r["cost"] == pytest.approx(42.31, rel=1e-3)


def test_transform_renames_external_conversions():
    """transform() renomme externalWebsiteConversions -> conversions via le manifest."""
    result = transform([_SAMPLE_API_ROW])
    r = result[0]
    assert "conversions" in r
    assert "externalWebsiteConversions" not in r
    assert r["conversions"] == 1


def test_transform_renames_leads():
    """transform() renomme leadGenerationMailContactInfoShares -> leads via le manifest."""
    result = transform([_SAMPLE_API_ROW])
    r = result[0]
    assert "leads" in r
    assert "leadGenerationMailContactInfoShares" not in r
    assert r["leads"] == 3


def test_transform_preserves_data_level():
    """transform() conserve le champ data_level (non renomme)."""
    result = transform([_SAMPLE_API_ROW])
    r = result[0]
    assert r.get("data_level") == DATA_LEVEL_CAMPAIGN


# ===========================================================================
# Test : conformance AI-54 -- golden fixture passee par parse + transform
# ===========================================================================

def test_golden_pull_through_parse_and_transform():
    """AI-54 : la fixture golden (shape API reel) passee par transform() produit
    les expected_facts canoniques.

    Le generateur (generate_linkedin_seed) garantit que les valeurs de la fixture
    correspondent au shape API LinkedIn reel. Ce test prouve que _parse_report_row +
    transform() donnent bien les valeurs canoniques attendues.
    """
    golden_path = FIXTURES_DIR / "golden_pull.json"
    expected_path = FIXTURES_DIR / "expected_facts.json"

    if not golden_path.exists() or not expected_path.exists():
        pytest.skip("Fixtures golden non generees -- regenerer via generate_linkedin_seed.py")

    golden_raw = json.loads(golden_path.read_text(encoding="utf-8"))
    expected_raw = json.loads(expected_path.read_text(encoding="utf-8"))

    # Filtrer les metadata rows (cles commencant par _)
    golden_rows = [r for r in golden_raw if not all(k.startswith("_") for k in r)]
    expected_rows = [r for r in expected_raw if not all(k.startswith("_") for k in r)]

    if not golden_rows:
        pytest.skip("Fixture golden vide -- regenerer")

    result = transform(golden_rows)
    assert len(result) == len(expected_rows), (
        f"Nombre de lignes transformees ({len(result)}) != expected ({len(expected_rows)})"
    )

    for i, (got, exp) in enumerate(zip(result, expected_rows)):
        # Verifier les champs canoniques cles
        for field in ("date", "data_level", "campaign_id"):
            assert got.get(field) == exp.get(field), (
                f"Row {i}: {field} got={got.get(field)!r} expected={exp.get(field)!r}"
            )
        if exp.get("cost") is not None:
            assert got.get("cost") == pytest.approx(exp["cost"], rel=1e-3), (
                f"Row {i}: cost got={got.get('cost')} expected={exp['cost']}"
            )


# ===========================================================================
# Tests : pagination bornee (adAnalytics ne pagine pas)
# ===========================================================================

def test_no_pagination_single_response(tmp_path):
    """Le connecteur consomme la reponse API unique sans boucle infinie.

    adAnalytics LinkedIn ne pagine pas -- la reponse est consommee en un seul appel.
    On mocke httpx.get pour retourner une liste d'elements et on verifie que
    _insert_raw_rows est appele une seule fois (un seul batch).
    """

    fake_elements = [
        {
            "pivotValues": ["urn:li:sponsoredCampaign:88000001"],
            "dateRange": {
                "start": {"year": 2026, "month": 6, "day": 9},
                "end": {"year": 2026, "month": 6, "day": 9},
            },
            "costInLocalCurrency": "42.31",
            "impressions": 2848,
            "clicks": 18,
            "externalWebsiteConversions": 1,
            "leadGenerationMailContactInfoShares": 3,
        }
    ]

    duckdb_path = str(tmp_path / "test.duckdb")
    with (
        patch.object(_mod, "_get_duckdb_path", return_value=duckdb_path),
        patch("core.nango_client.get_fresh_token", return_value="test-token"),
        patch("httpx.get") as mock_get,
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"elements": fake_elements}
        mock_get.return_value = mock_resp

        result = _mod._pull(
            connection_id="conn_test",
            date_from="2026-06-09",
            date_to="2026-06-09",
            project_id="default",
            pull_id="pull_TEST001",
            profile="campaign_daily",
            account_id="123456",
        )

    # Un seul appel httpx (pas de boucle de pagination)
    assert mock_get.call_count == 1, (
        f"Attendu 1 seul appel API (pas de pagination), got {mock_get.call_count}"
    )
    assert result["row_count"] == 1


def test_near_cap_warning_logged(tmp_path, caplog):
    """Un avertissement est logge si la reponse approche la limite 15 000 elements."""
    import logging

    # Generer 15 000 elements fictifs
    fake_elements = [
        {
            "pivotValues": [f"urn:li:sponsoredCampaign:{i}"],
            "dateRange": {
                "start": {"year": 2026, "month": 6, "day": 9},
                "end": {"year": 2026, "month": 6, "day": 9},
            },
            "costInLocalCurrency": "1.0",
            "impressions": 100,
            "clicks": 5,
            "externalWebsiteConversions": 0,
            "leadGenerationMailContactInfoShares": 0,
        }
        for i in range(15000)
    ]

    duckdb_path = str(tmp_path / "test_cap.duckdb")
    with (
        patch.object(_mod, "_get_duckdb_path", return_value=duckdb_path),
        patch("core.nango_client.get_fresh_token", return_value="test-token"),
        patch("httpx.get") as mock_get,
        caplog.at_level(logging.WARNING, logger="linkedin_ads_connector"),
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"elements": fake_elements}
        mock_get.return_value = mock_resp

        _mod._pull(
            connection_id="conn_test",
            date_from="2026-06-09",
            date_to="2026-06-09",
            project_id="default",
            pull_id="pull_CAP_TEST",
            profile="campaign_daily",
            account_id="123456",
        )

    # Un avertissement doit etre logge (approche de la limite)
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("15000" in str(m) or "cap" in str(m).lower() for m in warning_msgs), (
        f"Avertissement de limite 15000 elements non logue. Messages : {warning_msgs}"
    )


# ===========================================================================
# Tests : 429 -> RateLimitError (AD-3)
# ===========================================================================

def test_429_raises_rate_limit_error(tmp_path):
    """Un HTTP 429 de LinkedIn leve RateLimitError('linkedin-ads', retry_after)."""
    from core.quota import RateLimitError

    duckdb_path = str(tmp_path / "test_429.duckdb")
    with (
        patch.object(_mod, "_get_duckdb_path", return_value=duckdb_path),
        patch("core.nango_client.get_fresh_token", return_value="test-token"),
        patch("httpx.get") as mock_get,
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "60"}
        mock_get.return_value = mock_resp

        with pytest.raises(RateLimitError) as exc_info:
            _mod._pull(
                connection_id="conn_test",
                date_from="2026-06-09",
                date_to="2026-06-09",
                project_id="default",
                pull_id="pull_429_TEST",
                profile="campaign_daily",
                account_id="123456",
            )

    err = exc_info.value
    # Verifier que le provider est 'linkedin-ads' (pas hardcode dans core)
    assert "linkedin-ads" in str(err).lower() or err.args[0] == "linkedin-ads", (
        f"RateLimitError doit mentionner 'linkedin-ads', got: {err}"
    )


def test_429_no_retry_after_header(tmp_path):
    """HTTP 429 sans header Retry-After -> RateLimitError avec retry_after=None."""
    from core.quota import RateLimitError

    duckdb_path = str(tmp_path / "test_429_noheader.duckdb")
    with (
        patch.object(_mod, "_get_duckdb_path", return_value=duckdb_path),
        patch("core.nango_client.get_fresh_token", return_value="test-token"),
        patch("httpx.get") as mock_get,
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {}  # pas de Retry-After
        mock_get.return_value = mock_resp

        with pytest.raises(RateLimitError):
            _mod._pull(
                connection_id="conn_test",
                date_from="2026-06-09",
                date_to="2026-06-09",
                project_id="default",
                pull_id="pull_429_NOHEADER",
                profile="campaign_daily",
                account_id="123456",
            )


# ===========================================================================
# Test AD-3 : le token ne doit pas apparaitre dans les logs
# ===========================================================================

def test_token_not_in_logs(tmp_path, caplog):
    """AD-3 : le token OAuth n'apparait JAMAIS dans les logs (pull success)."""
    import logging

    fake_token = "SUPER_SECRET_LINKEDIN_TOKEN_XYZ"
    fake_elements = [
        {
            "pivotValues": ["urn:li:sponsoredCampaign:88000001"],
            "dateRange": {
                "start": {"year": 2026, "month": 6, "day": 9},
                "end": {"year": 2026, "month": 6, "day": 9},
            },
            "costInLocalCurrency": "42.31",
            "impressions": 2848,
            "clicks": 18,
            "externalWebsiteConversions": 1,
            "leadGenerationMailContactInfoShares": 3,
        }
    ]

    duckdb_path = str(tmp_path / "test_token_log.duckdb")
    with (
        patch.object(_mod, "_get_duckdb_path", return_value=duckdb_path),
        patch("core.nango_client.get_fresh_token", return_value=fake_token),
        patch("httpx.get") as mock_get,
        caplog.at_level(logging.DEBUG),
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"elements": fake_elements}
        mock_get.return_value = mock_resp

        _mod._pull(
            connection_id="conn_test",
            date_from="2026-06-09",
            date_to="2026-06-09",
            project_id="default",
            pull_id="pull_TOKEN_TEST",
            profile="campaign_daily",
            account_id="123456",
        )

    # Le token ne doit jamais apparaitre dans les messages de log
    all_log_text = " ".join(r.getMessage() for r in caplog.records)
    assert fake_token not in all_log_text, (
        "AD-3 VIOLATION : le token apparait dans les logs !"
    )


# ===========================================================================
# Test AI-58 : dispatch pull_campaign_daily / pull_campaign_group_daily
# ===========================================================================

def test_pull_campaign_daily_dispatches_campaign_profile(tmp_path):
    """pull_campaign_daily() dispatche vers le profil 'campaign_daily' (pivot=CAMPAIGN)."""
    called_profiles = []

    def _spy_pull(*args, profile, **kwargs):
        called_profiles.append(profile)
        return {"pull_id": "test", "row_count": 0, "date_from": "x", "date_to": "x"}

    with patch.object(_mod, "_pull", side_effect=_spy_pull):
        _mod.pull_campaign_daily(
            connection_id="c", date_from="2026-06-09", date_to="2026-06-09",
            project_id="default", pull_id="p", account_id="123",
        )

    assert called_profiles == ["campaign_daily"]


def test_pull_campaign_group_daily_dispatches_group_profile(tmp_path):
    """pull_campaign_group_daily() dispatche vers le profil 'campaign_group_daily'."""
    called_profiles = []

    def _spy_pull(*args, profile, **kwargs):
        called_profiles.append(profile)
        return {"pull_id": "test", "row_count": 0, "date_from": "x", "date_to": "x"}

    with patch.object(_mod, "_pull", side_effect=_spy_pull):
        _mod.pull_campaign_group_daily(
            connection_id="c", date_from="2026-06-09", date_to="2026-06-09",
            project_id="default", pull_id="p", account_id="123",
        )

    assert called_profiles == ["campaign_group_daily"]


def test_default_pull_dispatches_campaign_profile():
    """pull() bare dispatche vers 'campaign_daily' (fallback AI-58)."""
    called_profiles = []

    def _spy_pull(*args, profile, **kwargs):
        called_profiles.append(profile)
        return {"pull_id": "test", "row_count": 0, "date_from": "x", "date_to": "x"}

    with patch.object(_mod, "_pull", side_effect=_spy_pull):
        _mod.pull(
            connection_id="c", date_from="2026-06-09", date_to="2026-06-09",
            project_id="default", pull_id="p", account_id="123",
        )

    assert called_profiles == ["campaign_daily"]


# ===========================================================================
# Seam test build_asgi_app (AI-56) -- assertions sur VALEURS, pas seulement 200
# ===========================================================================

@pytest.mark.anyio
async def test_seam_linkedin_fastmcp_inprocess(tmp_path):
    """AI-56 : seam FastMCP in-process avec mart injecte -- assertions sur VALEURS reelles.

    Pattern identique au seam test Klaviyo post-fix (conforme AI-56) :
    1. Cree un mart minimal DuckDB avec des lignes linkedin-ads injectees directement.
    2. Appelle get_linkedin_ads_report via FastMCPTransport sur le mcp_app du module.
    3. Asserte des VALEURS (metriques presentes, total positif, provenance correcte).
    Ce test ne depend pas du pipeline dbt -- il teste le chemin lecture mart -> enveloppe.
    """
    from datetime import timedelta

    import duckdb
    from fastmcp.client import Client, FastMCPTransport

    db_path = str(tmp_path / "seam_inprocess.duckdb")
    con = duckdb.connect(db_path)
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute(
        """
        CREATE TABLE main_marts.fact_daily_kpi (
            project_id VARCHAR, date VARCHAR, connector VARCHAR,
            metric VARCHAR, breakdown_dimension VARCHAR, breakdown_value VARCHAR,
            value DOUBLE, pull_id VARCHAR, loaded_at VARCHAR
        )
        """
    )
    base = date(2026, 6, 15)
    rows_to_insert = []
    for i in range(1, 8):  # 7 jours : 2026-06-08 a 2026-06-14
        d = (base - timedelta(days=i)).isoformat()
        rows_to_insert.extend([
            ("default", d, "linkedin-ads", "cost", "campaign_id",
             "88000001", float(50.0 + i * 5), "pull_SEAM_LI_01",
             "2026-06-15T00:00:00Z"),
            ("default", d, "linkedin-ads", "impressions", "campaign_id",
             "88000001", float(3000 + i * 100), "pull_SEAM_LI_01",
             "2026-06-15T00:00:00Z"),
            ("default", d, "linkedin-ads", "clicks", "campaign_id",
             "88000001", float(20 + i), "pull_SEAM_LI_01",
             "2026-06-15T00:00:00Z"),
            ("default", d, "linkedin-ads", "conversions", "campaign_id",
             "88000001", float(1 + i % 3), "pull_SEAM_LI_01",
             "2026-06-15T00:00:00Z"),
            ("default", d, "linkedin-ads", "leads", "campaign_id",
             "88000001", float(i % 5), "pull_SEAM_LI_01",
             "2026-06-15T00:00:00Z"),
        ])
    con.executemany(
        "INSERT INTO main_marts.fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
        rows_to_insert,
    )
    con.close()

    with patch.dict(
        "os.environ",
        {"TOOROW_DB_MODE": "duckdb", "TOOROW_DUCKDB_PATH": db_path},
    ):
        mcp_app = _mod.mcp_app
        date_to = date(2026, 6, 14).isoformat()
        date_from = date(2026, 6, 8).isoformat()

        async with Client(FastMCPTransport(mcp_app)) as client:
            result = await client.call_tool(
                "get_linkedin_ads_report",
                {
                    "project_id": "default",
                    "report_profile": "campaign_daily",
                    "date_from": date_from,
                    "date_to": date_to,
                },
            )

    assert not result.is_error, f"Outil retourne une erreur: {result}"
    envelope = result.structured_content or result.data

    # Assertions enveloppe AD-1
    assert envelope["schema_version"] == "1"
    data = envelope["data"]
    metrics = data.get("metrics", {})

    # Assertions sur VALEURS (AI-56 : pas seulement HTTP 200)
    assert "cost" in metrics, f"Metrique 'cost' absente du mart: {list(metrics.keys())}"
    assert "impressions" in metrics, "Metrique 'impressions' absente"
    assert "conversions" in metrics, "Metrique 'conversions' absente"
    assert "leads" in metrics, "Metrique 'leads' absente"

    total_cost = sum(r["value"] for r in metrics["cost"] if r["value"] is not None)
    assert total_cost > 0, f"Total cost doit etre positif, got {total_cost}"

    total_conv = sum(r["value"] for r in metrics["conversions"] if r["value"] is not None)
    assert total_conv > 0, f"Total conversions doit etre positif, got {total_conv}"

    # Provenance AD-7
    prov = envelope["meta"]["provenance"]
    assert prov is not None, "meta.provenance doit etre non-null quand le mart a des donnees"
    assert prov["source_system"] == "linkedin-ads"
    assert prov["pull_id"] == "pull_SEAM_LI_01"

    # AD-4 : 'conversions' generique present (LinkedIn revendiquees, pas un total croise)
    # mais 'revenue' ne doit PAS etre emis par ce connecteur
    assert "revenue" not in metrics, (
        "linkedin-ads ne doit pas exposer metric='revenue' (pas un connecteur de revenu)"
    )


@pytest.mark.anyio
async def test_seam_linkedin_core_loader_mounts_namespace(tmp_path):
    """AI-56 : seam via core.main.mcp -- prouve que le loader core monte linkedin-ads.

    Verifie que 'linkedin-ads_get_linkedin_ads_report' est dans les outils montes.
    Pattern identique au seam core Klaviyo (conforme AI-56 + T15.3.A/B du test_module_loading).
    """
    import duckdb
    from fastmcp.client import Client, FastMCPTransport

    db_path = str(tmp_path / "core_seam_li.duckdb")
    con = duckdb.connect(db_path)
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute(
        """
        CREATE TABLE main_marts.fact_daily_kpi (
            project_id VARCHAR, date VARCHAR, connector VARCHAR,
            metric VARCHAR, breakdown_dimension VARCHAR, breakdown_value VARCHAR,
            value DOUBLE, pull_id VARCHAR, loaded_at VARCHAR
        )
        """
    )
    con.close()

    with patch.dict(
        "os.environ",
        {
            "TOOROW_DB_MODE": "duckdb",
            "TOOROW_DUCKDB_PATH": db_path,
            "HEALTH_POLLER_ENABLED": "false",
            "QUEUE_WORKER_ENABLED": "false",
            "SCHEDULER_ENABLED": "false",
        },
    ):
        from core.main import mcp

        async with Client(FastMCPTransport(mcp)) as client:
            tools = await client.list_tools()

    tool_names = [t.name for t in tools]
    linkedin_tools = [n for n in tool_names if "linkedin" in n.lower()]
    assert linkedin_tools, (
        f"Aucun outil linkedin-ads trouve dans les outils montes par core.main.mcp: {tool_names}"
    )
    assert any("get_linkedin_ads_report" in n for n in linkedin_tools), (
        f"linkedin-ads_get_linkedin_ads_report absent des outils montes: {linkedin_tools}"
    )


@pytest.mark.anyio
async def test_seam_linkedin_report_envelope_shape_only(tmp_path):
    """Seam shape-only (honnete) : prouve que l'envelope AD-1 est construite sans cracher
    meme sans mart disponible.

    Note : sans dbt build, fact_daily_kpi n'existe pas -> metrics={} ou alert.
    Les assertions de VALEURS completes sont dans test_seam_linkedin_fastmcp_inprocess
    (ci-dessus) et dans test_seed_to_mart_loop.py (pipeline dbt complet).
    """
    import importlib.util as _ilu
    from pathlib import Path

    seeds_dir = Path(__file__).parents[2] / "modules" / "linkedin-ads" / "seeds"
    loader_spec = _ilu.spec_from_file_location(
        "load_linkedin_seed", seeds_dir / "load_linkedin_seed.py"
    )
    loader_mod = _ilu.module_from_spec(loader_spec)
    loader_spec.loader.exec_module(loader_mod)

    db_path = str(tmp_path / "seam_shape.duckdb")

    # Charger 7 jours de donnees LinkedIn (seed deterministe dans raw uniquement)
    pull_id, count = loader_mod.run(duckdb_path=db_path, days=7, grains="campaign")
    assert count > 0, f"Le seed LinkedIn doit produire des lignes, got {count}"

    # Appel avec mart absent -> envelope honnete avec alert ou metrics={}
    with patch.object(
        _mod, "_get_duckdb_path", return_value=db_path
    ):
        envelope = _mod.get_linkedin_ads_report(
            project_id="default",
            report_profile="campaign_daily",
            date_from="2026-06-01",
            date_to="2026-06-07",
        )

    # Assertions de forme uniquement (AI-56 enveloppe honnete AD-9)
    assert "schema_version" in envelope
    assert "meta" in envelope
    assert "data" in envelope
    assert isinstance(envelope["meta"].get("alerts"), list)
    assert envelope["data"]["report_profile"] == "campaign_daily"
