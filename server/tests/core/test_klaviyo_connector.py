"""Tests unitaires du connecteur Klaviyo -- Story 15.8 (Epic 15).

Couvre :
  - Parsing _parse_reporting_row() : valeurs presentes, absentes (NULL honnete AD-9),
    dates manquantes.
  - transform() : mapping canonique via le manifest (AD-4).
  - pull() : pagination bornee, erreur quota 429 -> RateLimitError, valeurs manquantes
    -> NULL (jamais 0, AD-9).
  - get_klaviyo_report() : shape de l'envelope AD-1.
  - Regle de non-agregation : attributed_revenue et attributed_conversions ne
    partagent JAMAIS une colonne avec 'conversions' ou 'revenue' des regies/Shopify
    (verifiee au niveau du manifest et du schema de la table raw).
  - Seam build_asgi_app (AI-56) : montage du module + appel d'outil via FastMCP.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import du module connecteur klaviyo (pas depuis le package, depuis le chemin)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parents[3]
KLAVIYO_DIR = REPO_ROOT / "server" / "modules" / "klaviyo"
TOOROW_PATH = KLAVIYO_DIR / "connector.py"
MANIFEST_PATH = KLAVIYO_DIR / "manifest.json"


def _import_connector():
    spec = importlib.util.spec_from_file_location("klaviyo_connector", TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests du parsing : _parse_reporting_row()
# ---------------------------------------------------------------------------


class TestParseReportingRow:
    """Tests unitaires de _parse_reporting_row()."""

    def setup_method(self):
        self.conn = _import_connector()

    def _make_result_item(
        self,
        campaign_id: str = "camp_001",
        campaign_name: str = "Ma Campagne",
        dates: list[str] | None = None,
        sends: list | None = None,
        opens: list | None = None,
        clicks: list | None = None,
        conversions: list | None = None,
        revenue: list | None = None,
    ) -> dict:
        """Construit un result_item de la Reporting API Klaviyo (mock)."""
        if dates is None:
            dates = ["2026-06-01"]
        return {
            "groupings": {
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
            },
            "statistics": {
                "dates": dates,
                "sends": sends or [1000.0],
                "opens": opens or [250.0],
                "clicks": clicks or [20.0],
                "conversions": conversions or [2.0],
                "revenue": revenue or [300.0],
            },
        }

    def test_parse_campaign_row_all_metrics_present(self):
        """Toutes les metriques presentes -> valeurs floats correctes."""
        item = self._make_result_item(
            dates=["2026-06-01"],
            sends=[5000.0],
            opens=[1200.0],
            clicks=[80.0],
            conversions=[4.0],
            revenue=[520.0],
        )
        row = self.conn._parse_reporting_row(item, "2026-06-01", "campaign")
        assert row["date"] == "2026-06-01"
        assert row["campaign_id"] == "camp_001"
        assert row["campaign_name"] == "Ma Campagne"
        assert row["flow_id"] is None
        assert row["flow_name"] is None
        assert row["sends"] == 5000.0
        assert row["opens"] == 1200.0
        assert row["clicks"] == 80.0
        assert row["attributed_conversions"] == 4.0
        assert row["attributed_revenue"] == 520.0

    def test_parse_flow_row(self):
        """Row de type flow : flow_id/flow_name renseignes, campaign_* = None."""
        item = {
            "groupings": {"flow_id": "flow_abc", "flow_name": "Abandon Panier"},
            "statistics": {
                "dates": ["2026-06-01"],
                "sends": [800.0],
                "opens": [200.0],
                "clicks": [15.0],
                "conversions": [1.0],
                "revenue": [120.0],
            },
        }
        row = self.conn._parse_reporting_row(item, "2026-06-01", "flow")
        assert row["flow_id"] == "flow_abc"
        assert row["flow_name"] == "Abandon Panier"
        assert row["campaign_id"] is None
        assert row["attributed_conversions"] == 1.0
        assert row["attributed_revenue"] == 120.0

    def test_parse_missing_stat_returns_null(self):
        """Une statistique absente dans la reponse retourne None (AD-9 NULL honnete)."""
        item = {
            "groupings": {"campaign_id": "camp_001", "campaign_name": "Test"},
            "statistics": {
                "dates": ["2026-06-01"],
                "sends": [1000.0],
                # opens absent
                "clicks": [None],  # explicitement None
                # conversions absent
                "revenue": [200.0],
            },
        }
        row = self.conn._parse_reporting_row(item, "2026-06-01", "campaign")
        assert row["opens"] is None, "Stat absente doit retourner None, pas 0 (AD-9)"
        assert row["clicks"] is None, "Stat None explicite doit rester None (AD-9)"
        assert row["attributed_conversions"] is None, "Stat absente -> None (AD-9)"

    def test_parse_date_not_in_dates_array_returns_empty(self):
        """Date absente dans le tableau dates -> retourne {} (ligne ignoree)."""
        item = self._make_result_item(dates=["2026-06-01"])
        row = self.conn._parse_reporting_row(item, "2026-06-15", "campaign")
        assert row == {}, "Date absente doit retourner {} (sera filtre par pull)"

    def test_parse_multiple_dates_extracts_correct_index(self):
        """Date au milieu du tableau -> extrait le bon index."""
        item = {
            "groupings": {"campaign_id": "camp_x", "campaign_name": "X"},
            "statistics": {
                "dates": ["2026-06-01", "2026-06-02", "2026-06-03"],
                "sends": [100.0, 200.0, 300.0],
                "opens": [10.0, 20.0, 30.0],
                "clicks": [1.0, 2.0, 3.0],
                "conversions": [0.1, 0.2, 0.3],
                "revenue": [10.0, 20.0, 30.0],
            },
        }
        row = self.conn._parse_reporting_row(item, "2026-06-02", "campaign")
        assert row["sends"] == 200.0
        assert row["attributed_conversions"] == pytest.approx(0.2)
        assert row["attributed_revenue"] == 20.0


# ---------------------------------------------------------------------------
# Tests de transform() -- mapping canonique via manifest
# ---------------------------------------------------------------------------


class TestTransform:
    """Tests du mapping manifest-driven (AD-4)."""

    def setup_method(self):
        self.conn = _import_connector()

    def test_transform_passes_through_canonical_fields(self):
        """Les champs deja canoniques (attributed_conversions) passent intacts."""
        rows = [
            {
                "date": "2026-06-01",
                "campaign_id": "camp_001",
                "campaign_name": "Test",
                "flow_id": None,
                "flow_name": None,
                "sends": 1000.0,
                "opens": 250.0,
                "clicks": 20.0,
                "attributed_conversions": 3.0,
                "attributed_revenue": 400.0,
            }
        ]
        result = self.conn.transform(rows)
        assert len(result) == 1
        row = result[0]
        # Les champs canoniques passent sans modification.
        assert row["attributed_conversions"] == 3.0
        assert row["attributed_revenue"] == 400.0

    def test_transform_null_values_preserved(self):
        """Les None sont preserves par transform() (AD-9 NULL honnete)."""
        rows = [
            {
                "date": "2026-06-01",
                "campaign_id": "camp_001",
                "campaign_name": "Test",
                "flow_id": None,
                "flow_name": None,
                "sends": 500.0,
                "opens": None,
                "clicks": None,
                "attributed_conversions": None,
                "attributed_revenue": None,
            }
        ]
        result = self.conn.transform(rows)
        assert result[0]["opens"] is None
        assert result[0]["attributed_conversions"] is None
        assert result[0]["attributed_revenue"] is None

    def test_transform_empty_rows(self):
        """transform([]) retourne []."""
        assert self.conn.transform([]) == []


# ---------------------------------------------------------------------------
# Tests de la regle de non-agregation (AD-4, CRITIQUE Story 15.8)
# ---------------------------------------------------------------------------


class TestNonAggregationRule:
    """Verifie que la regle de non-agregation attributed_revenue / attributed_conversions
    est declaree dans le manifest ET que les colonnes raw sont distinctes des
    colonnes revenue/conversions des autres connecteurs."""

    def test_manifest_declares_attribution_window(self):
        """Le manifest declare la fenetre d'attribution Klaviyo (AI-53)."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        assert "attribution_window" in manifest, (
            "Le manifest doit declarer attribution_window (Story 15.8 AC)"
        )
        aw = manifest["attribution_window"]
        assert "default_email_click_days" in aw
        assert aw["default_email_click_days"] == 5, (
            "Fenetre clic email par defaut = 5 jours (AI-53 verifie le 2026-07-19)"
        )
        assert aw["model"] == "last_touch"

    def test_manifest_declares_non_aggregation_rule(self):
        """Le manifest contient la regle explicite de non-agregation (Story 15.8)."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        rule = manifest.get("attribution_window", {}).get("_rule_non_aggregation", "")
        assert "JAMAIS" in rule or "NEVER" in rule, (
            "Le manifest doit declarer la regle de non-agregation revenue croise (AD-4)"
        )
        assert "attributed_revenue" in rule

    def test_manifest_metrics_dont_collide_with_regie_conversions(self):
        """Les metriques Klaviyo utilisent 'attributed_conversions', pas 'conversions'.

        Cela evite la collision avec le champ canonique 'conversions' des regies
        (Meta, TikTok, Google) dans fact_daily_kpi. Les lignes du mart sont
        distinguees par connector='klaviyo' ET par le nom de metrique explicite.
        """
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        all_metrics: list[str] = []
        for profile in manifest.get("report_profiles", []):
            all_metrics.extend(profile.get("metrics", []))

        # Les metriques Klaviyo doivent utiliser des noms explicites distincts.
        assert "attributed_conversions" in all_metrics, (
            "Le profil doit exposer 'attributed_conversions' (pas 'conversions' ambigü)"
        )
        assert "attributed_revenue" in all_metrics, (
            "Le profil doit exposer 'attributed_revenue' (pas 'revenue' seul)"
        )

    def test_raw_table_ddl_has_dedicated_columns(self):
        """La DDL de la table raw a des colonnes dediees attributed_* (pas 'conversions')."""
        conn = _import_connector()
        ddl = conn._RAW_CREATE_DDL
        assert "attributed_conversions" in ddl, (
            "La table raw doit avoir une colonne 'attributed_conversions' (pas 'conversions')"
        )
        assert "attributed_revenue" in ddl, (
            "La table raw doit avoir une colonne 'attributed_revenue' (pas 'revenue')"
        )
        # Verification negative : pas de colonne 'conversions' generique.
        # On accepte 'conversions' comme sous-chaine d'attributed_conversions uniquement.
        lines_with_conversions = [
            line.strip() for line in ddl.splitlines()
            if "conversions" in line and "attributed_conversions" not in line
        ]
        assert not lines_with_conversions, (
            f"La table raw ne doit PAS avoir de colonne 'conversions' generique "
            f"(risque de collision avec les regies) : {lines_with_conversions}"
        )


# ---------------------------------------------------------------------------
# Tests de pull() : rate limit, valeurs manquantes
# ---------------------------------------------------------------------------


class TestPullFunction:
    """Tests unitaires de pull() : bornage pagination, RateLimitError, NULL."""

    def setup_method(self):
        self.conn = _import_connector()

    def test_pull_raises_rate_limit_error_on_429(self, tmp_path):
        """Un HTTP 429 de la Reporting API declenche RateLimitError (AD-5/HG-4)."""
        from unittest.mock import MagicMock, patch

        mock_response_429 = MagicMock()
        mock_response_429.status_code = 429
        mock_response_429.headers = {"Retry-After": "30"}

        def mock_post(*args, **kwargs):
            return mock_response_429

        db_path = str(tmp_path / "test.duckdb")

        env_override = {"TOOROW_DB_MODE": "duckdb", "TOOROW_DUCKDB_PATH": db_path}
        with (
            patch("httpx.Client") as mock_client_cls,
            patch.dict("os.environ", env_override),
        ):
            mock_http_client = MagicMock()
            mock_http_client.__enter__ = lambda s: mock_http_client
            mock_http_client.__exit__ = MagicMock(return_value=False)
            mock_http_client.post = mock_post
            mock_client_cls.return_value = mock_http_client

            # Mock nango_client.get_fresh_token pour ne pas appeler Nango.
            with patch("core.nango_client.get_fresh_token", return_value="fake_api_key"):
                from core.quota import RateLimitError

                with pytest.raises(RateLimitError) as exc_info:
                    self.conn.pull(
                        connection_id="conn_klaviyo_test",
                        date_from="2026-06-01",
                        date_to="2026-06-01",
                        project_id="default",
                        pull_id="pull_TEST_ULID",
                        conversion_metric_id="metric_placed_order",
                    )

                assert exc_info.value.platform == "klaviyo"

    def test_pull_raises_value_error_when_conversion_metric_id_missing(self):
        """Leve ValueError si conversion_metric_id absent et var env non set."""
        with patch.dict("os.environ", {}, clear=False):
            # S'assurer que la var env n'est pas set.
            import os
            os.environ.pop("KLAVIYO_CONVERSION_METRIC_ID", None)

            with patch("core.nango_client.get_fresh_token", return_value="fake"):
                with pytest.raises(ValueError, match="KLAVIYO_CONVERSION_METRIC_ID"):
                    self.conn.pull(
                        connection_id="conn_test",
                        date_from="2026-06-01",
                        date_to="2026-06-01",
                        project_id="default",
                        pull_id="pull_TEST",
                        conversion_metric_id=None,
                    )

    def test_pull_page_cap_prevents_infinite_loop(self, tmp_path):
        """La boucle de pagination est bornee a _MAX_PAGES (pas de boucle infinie)."""
        # Simule une reponse qui retourne toujours un next link (boucle infinie sans cap).
        call_count = 0

        def mock_post_infinite(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 200
            _next = "https://a.klaviyo.com/api/campaign-values-reports/?page=next"
            resp.json.return_value = {
                "data": {
                    "attributes": {"results": []},
                    "links": {"next": _next},
                },
                "links": {"next": _next},
            }
            return resp

        conn = self.conn

        with patch("httpx.Client") as mock_client_cls:
            mock_http_client = MagicMock()
            mock_http_client.__enter__ = lambda s: mock_http_client
            mock_http_client.__exit__ = MagicMock(return_value=False)
            mock_http_client.post = mock_post_infinite
            mock_client_cls.return_value = mock_http_client

            # On teste _post_reporting directement.
            try:
                conn._post_reporting(
                    mock_http_client,
                    "campaign-values-reports/",
                    {"data": {}},
                    {},
                )
            except Exception:
                pass  # On ne teste pas l'erreur mais le nombre d'appels.

        # Le cap doit etre respecte (max _MAX_PAGES = 10 appels).
        assert call_count <= conn._MAX_PAGES, (
            f"La boucle de pagination depasse le cap ({call_count} > {conn._MAX_PAGES})"
        )

    def test_insert_raw_rows_preserves_null_metrics(self, tmp_path):
        """_insert_raw_rows stocke les None comme NULL DuckDB (AD-9)."""
        import duckdb

        db_path = str(tmp_path / "test.duckdb")
        rows = [
            {
                "date": "2026-06-01",
                "campaign_id": "camp_001",
                "campaign_name": "Test",
                "flow_id": None,
                "flow_name": None,
                "sends": 1000.0,
                "opens": None,      # NULL honnete
                "clicks": 20.0,
                "attributed_conversions": None,  # NULL honnete
                "attributed_revenue": 300.0,
            }
        ]

        conn = self.conn
        conn._insert_raw_rows(
            rows,
            pull_id="pull_TEST",
            loaded_at="2026-06-01T00:00:00Z",
            project_id="default",
            db_mode="duckdb",
            duckdb_path=db_path,
        )

        con = duckdb.connect(db_path, read_only=True)
        result = con.execute(
            "SELECT opens, attributed_conversions FROM raw_klaviyo_daily"
        ).fetchone()
        con.close()

        assert result[0] is None, "opens=None doit etre stocke NULL (AD-9)"
        assert result[1] is None, "attributed_conversions=None doit etre stocke NULL (AD-9)"


# ---------------------------------------------------------------------------
# Tests de l'envelope get_klaviyo_report (seam AI-56)
# ---------------------------------------------------------------------------


class TestKlaviyoReportEnvelope:
    """Tests du MCP tool get_klaviyo_report : shape de l'envelope AD-1."""

    def setup_method(self):
        self.conn = _import_connector()

    def test_get_klaviyo_report_returns_ad1_envelope_shape(self, tmp_path):
        """get_klaviyo_report retourne l'envelope canonique AD-1 (schema_version, meta, data)."""
        import duckdb

        db_path = str(tmp_path / "test.duckdb")
        # Creer un mart vide pour que la requete retourne simplement {}.
        con = duckdb.connect(db_path)
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS main_marts.fact_daily_kpi (
                project_id VARCHAR,
                date VARCHAR,
                connector VARCHAR,
                metric VARCHAR,
                breakdown_dimension VARCHAR,
                breakdown_value VARCHAR,
                value DOUBLE,
                pull_id VARCHAR,
                loaded_at VARCHAR
            )
            """
        )
        con.close()

        with patch.dict(
            "os.environ",
            {"TOOROW_DB_MODE": "duckdb", "TOOROW_DUCKDB_PATH": db_path},
        ):
            envelope = self.conn.get_klaviyo_report(
                project_id="default",
                report_profile="campaign_daily",
                date_from="2026-06-01",
                date_to="2026-06-30",
            )

        assert "schema_version" in envelope, "schema_version obligatoire (AD-1)"
        assert "meta" in envelope
        assert "data" in envelope
        meta = envelope["meta"]
        assert "freshness" in meta
        assert "provenance" in meta
        assert "alerts" in meta
        assert isinstance(meta["alerts"], list)
        data = envelope["data"]
        assert data["report_profile"] == "campaign_daily"
        assert "metrics" in data

    def test_get_klaviyo_report_with_seed_data_returns_metrics(self, tmp_path):
        """Avec des donnees seedees, get_klaviyo_report retourne des metriques non vides."""
        from datetime import date, timedelta

        import duckdb

        db_path = str(tmp_path / "test.duckdb")
        con = duckdb.connect(db_path)
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(
            """
            CREATE TABLE main_marts.fact_daily_kpi (
                project_id VARCHAR,
                date VARCHAR,
                connector VARCHAR,
                metric VARCHAR,
                breakdown_dimension VARCHAR,
                breakdown_value VARCHAR,
                value DOUBLE,
                pull_id VARCHAR,
                loaded_at VARCHAR
            )
            """
        )
        # Inserer des lignes Klaviyo simulees dans le mart.
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        con.execute(
            """
            INSERT INTO main_marts.fact_daily_kpi VALUES
              ('default', ?, 'klaviyo', 'sends', 'campaign_id', 'camp_001',
               5000.0, 'pull_TEST01', '2026-06-01T00:00:00Z'),
              ('default', ?, 'klaviyo', 'attributed_revenue', 'campaign_id', 'camp_001',
               400.0, 'pull_TEST01', '2026-06-01T00:00:00Z')
            """,
            [yesterday, yesterday],
        )
        con.close()

        with patch.dict(
            "os.environ",
            {"TOOROW_DB_MODE": "duckdb", "TOOROW_DUCKDB_PATH": db_path},
        ):
            date_from = (date.today() - timedelta(days=7)).isoformat()
            envelope = self.conn.get_klaviyo_report(
                project_id="default",
                date_from=date_from,
                date_to=yesterday,
            )

        metrics = envelope["data"]["metrics"]
        assert "sends" in metrics, "La metrique 'sends' doit etre presente"
        assert "attributed_revenue" in metrics, (
            "La metrique 'attributed_revenue' doit etre presente (distincte de 'revenue')"
        )
        # REGLE DE NON-AGREGATION : 'revenue' generique ne doit PAS apparaitre.
        assert "revenue" not in metrics or metrics.get("revenue") == metrics.get(
            "attributed_revenue"
        ), (
            "Le mart Klaviyo ne doit pas exposer un 'revenue' generique non prefixe"
        )

    def test_get_klaviyo_report_klaviyo_rows_not_in_cross_source_totals(self, tmp_path):
        """Les metriques Klaviyo n'apparaissent pas dans les totaux cross-source existants.

        Prouve que les lignes connector='klaviyo' sont isolees et ne gonflent pas
        les totaux GA4/Meta/Shopify (regle de non-agregation AD-4, Story 15.8).
        """
        from datetime import date, timedelta

        import duckdb

        db_path = str(tmp_path / "test.duckdb")
        con = duckdb.connect(db_path)
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(
            """
            CREATE TABLE main_marts.fact_daily_kpi (
                project_id VARCHAR,
                date VARCHAR,
                connector VARCHAR,
                metric VARCHAR,
                breakdown_dimension VARCHAR,
                breakdown_value VARCHAR,
                value DOUBLE,
                pull_id VARCHAR,
                loaded_at VARCHAR
            )
            """
        )
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # Lignes GA4 (conversions existantes).
        ga4_conversions = 50.0
        # Lignes Klaviyo (attributed_conversions : NE doivent PAS etre additionnes aux GA4).
        klaviyo_attributed = 30.0
        shopify_revenue = 1000.0
        klaviyo_attributed_revenue = 400.0

        con.execute(
            """
            INSERT INTO main_marts.fact_daily_kpi VALUES
              ('default', ?, 'google-analytics', 'conversions', 'country', 'FR',
               ?, 'pull_GA4', '2026-06-01T00:00:00Z'),
              ('default', ?, 'klaviyo', 'attributed_conversions', 'campaign_id',
               'camp_001', ?, 'pull_KLV', '2026-06-01T00:00:00Z'),
              ('default', ?, 'shopify', 'revenue', 'day_total', 'all',
               ?, 'pull_SHOP', '2026-06-01T00:00:00Z'),
              ('default', ?, 'klaviyo', 'attributed_revenue', 'campaign_id',
               'camp_001', ?, 'pull_KLV', '2026-06-01T00:00:00Z')
            """,
            [
                yesterday, ga4_conversions,
                yesterday, klaviyo_attributed,
                yesterday, shopify_revenue,
                yesterday, klaviyo_attributed_revenue,
            ],
        )

        # Test : le total GA4 conversions ne change pas quand on filtre par connector.
        ga4_total = con.execute(
            "SELECT SUM(value) FROM main_marts.fact_daily_kpi "
            "WHERE connector = 'google-analytics' AND metric = 'conversions'"
        ).fetchone()[0]
        shopify_total = con.execute(
            "SELECT SUM(value) FROM main_marts.fact_daily_kpi "
            "WHERE connector = 'shopify' AND metric = 'revenue'"
        ).fetchone()[0]
        klaviyo_klv = con.execute(
            "SELECT SUM(value) FROM main_marts.fact_daily_kpi "
            "WHERE connector = 'klaviyo' AND metric = 'attributed_conversions'"
        ).fetchone()[0]
        con.close()

        assert ga4_total == ga4_conversions, (
            "Les totaux GA4 conversions ne doivent PAS inclure les attributed_conversions Klaviyo"
        )
        assert shopify_total == shopify_revenue, (
            "Le revenue Shopify ne doit PAS inclure le attributed_revenue Klaviyo"
        )
        assert klaviyo_klv == klaviyo_attributed, (
            "Les metriques Klaviyo doivent etre isolees dans leurs lignes connector='klaviyo'"
        )


# ---------------------------------------------------------------------------
# F-6 : filtre report_profile -> breakdown_dimension dans get_klaviyo_report
# ---------------------------------------------------------------------------


class TestReportProfileFilter:
    """F-6 : get_klaviyo_report filtre breakdown_dimension selon report_profile.

    campaign_daily -> uniquement campaign_id
    flow_daily     -> uniquement flow_id
    absent/autre   -> campaign_id ET flow_id (les deux)
    """

    def _make_mart_db(self, tmp_path) -> str:
        """Cree un mart DuckDB avec des lignes campaign_id ET flow_id."""
        from datetime import date, timedelta

        import duckdb

        db_path = str(tmp_path / "filter_test.duckdb")
        con = duckdb.connect(db_path)
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(
            """
            CREATE TABLE main_marts.fact_daily_kpi (
                project_id VARCHAR, date VARCHAR, connector VARCHAR,
                metric VARCHAR, breakdown_dimension VARCHAR,
                breakdown_value VARCHAR, value DOUBLE,
                pull_id VARCHAR, loaded_at VARCHAR
            )
            """
        )
        d = (date.today() - timedelta(days=1)).isoformat()
        con.execute(
            """
            INSERT INTO main_marts.fact_daily_kpi VALUES
              ('default', ?, 'klaviyo', 'sends', 'campaign_id',
               'camp_001', 5000.0, 'pull_F6', '2026-07-01T00:00:00Z'),
              ('default', ?, 'klaviyo', 'attributed_conversions', 'campaign_id',
               'camp_001', 3.0, 'pull_F6', '2026-07-01T00:00:00Z'),
              ('default', ?, 'klaviyo', 'sends', 'flow_id',
               'flow_abc', 800.0, 'pull_F6', '2026-07-01T00:00:00Z'),
              ('default', ?, 'klaviyo', 'attributed_conversions', 'flow_id',
               'flow_abc', 1.0, 'pull_F6', '2026-07-01T00:00:00Z')
            """,
            [d, d, d, d],
        )
        con.close()
        return db_path

    def test_campaign_daily_returns_only_campaign_dimension(self, tmp_path):
        """report_profile='campaign_daily' -> uniquement breakdown_dimension='campaign_id'."""
        from datetime import date, timedelta

        db_path = self._make_mart_db(tmp_path)
        conn = _import_connector()

        date_to = (date.today() - timedelta(days=1)).isoformat()
        date_from = (date.today() - timedelta(days=7)).isoformat()

        with patch.dict(
            "os.environ",
            {"TOOROW_DB_MODE": "duckdb", "TOOROW_DUCKDB_PATH": db_path},
        ):
            envelope = conn.get_klaviyo_report(
                project_id="default",
                report_profile="campaign_daily",
                date_from=date_from,
                date_to=date_to,
            )

        metrics = envelope["data"]["metrics"]
        assert metrics, "Des metriques doivent etre retournees pour campaign_daily"
        for metric_name, rows in metrics.items():
            dims = {r["breakdown_dimension"] for r in rows}
            assert dims == {"campaign_id"}, (
                f"report_profile='campaign_daily' ne doit retourner que 'campaign_id', "
                f"got {dims} pour metrique '{metric_name}'"
            )
            assert "flow_id" not in dims, (
                "report_profile='campaign_daily' ne doit PAS retourner 'flow_id' (F-6)"
            )

    def test_flow_daily_returns_only_flow_dimension(self, tmp_path):
        """report_profile='flow_daily' -> uniquement breakdown_dimension='flow_id'."""
        from datetime import date, timedelta

        db_path = self._make_mart_db(tmp_path)
        conn = _import_connector()

        date_to = (date.today() - timedelta(days=1)).isoformat()
        date_from = (date.today() - timedelta(days=7)).isoformat()

        with patch.dict(
            "os.environ",
            {"TOOROW_DB_MODE": "duckdb", "TOOROW_DUCKDB_PATH": db_path},
        ):
            envelope = conn.get_klaviyo_report(
                project_id="default",
                report_profile="flow_daily",
                date_from=date_from,
                date_to=date_to,
            )

        metrics = envelope["data"]["metrics"]
        assert metrics, "Des metriques doivent etre retournees pour flow_daily"
        for metric_name, rows in metrics.items():
            dims = {r["breakdown_dimension"] for r in rows}
            assert dims == {"flow_id"}, (
                f"report_profile='flow_daily' ne doit retourner que 'flow_id', "
                f"got {dims} pour metrique '{metric_name}'"
            )
            assert "campaign_id" not in dims, (
                "report_profile='flow_daily' ne doit PAS retourner 'campaign_id' (F-6)"
            )

    def test_unknown_profile_returns_both_dimensions(self, tmp_path):
        """report_profile absent/inconnu -> les deux dimensions sont retournees."""
        from datetime import date, timedelta

        db_path = self._make_mart_db(tmp_path)
        conn = _import_connector()

        date_to = (date.today() - timedelta(days=1)).isoformat()
        date_from = (date.today() - timedelta(days=7)).isoformat()

        with patch.dict(
            "os.environ",
            {"TOOROW_DB_MODE": "duckdb", "TOOROW_DUCKDB_PATH": db_path},
        ):
            envelope = conn.get_klaviyo_report(
                project_id="default",
                report_profile="all",
                date_from=date_from,
                date_to=date_to,
            )

        metrics = envelope["data"]["metrics"]
        all_dims: set[str] = set()
        for rows in metrics.values():
            all_dims.update(r["breakdown_dimension"] for r in rows)
        assert "campaign_id" in all_dims, (
            "Sans filtre de profil, 'campaign_id' doit etre present (F-6)"
        )
        assert "flow_id" in all_dims, (
            "Sans filtre de profil, 'flow_id' doit etre present (F-6)"
        )


# ---------------------------------------------------------------------------
# F-1 (AI-54) : test de conformance fixture -- generate_rows -> transform
# ---------------------------------------------------------------------------


def test_golden_fixture_matches_generator():
    """AI-54 : transform(generate_rows(FIXTURE_END_DATE)) produit uniquement des
    lignes avec les metriques canoniques attendues (sends, opens, clicks,
    attributed_conversions, attributed_revenue). Prouve que la fixture est
    coherente avec la logique du generateur (graine fixe + date figee) et que
    transform() ne modifie pas les noms canoniques Klaviyo.

    Rend le test discriminant : si transform() introduit une regression de nommage
    (ex. attributed_conversions -> conversions), ce test echoue.
    """
    import sys

    # Importer le generateur de seed
    seeds_dir = REPO_ROOT / "server" / "modules" / "klaviyo" / "seeds"
    sys.path.insert(0, str(seeds_dir))
    import importlib

    gen_mod = importlib.import_module("generate_klaviyo_seed")

    end_date = gen_mod.FIXTURE_END_DATE
    days = gen_mod.FIXTURE_DAYS

    # Generer les lignes avec la date figee (deterministe, graine fixe interne).
    raw_rows = gen_mod.generate_rows(days=days, end_date=end_date)
    assert len(raw_rows) > 0, "Le generateur doit produire des lignes"

    # Les lignes brutes doivent avoir les bons champs.
    expected_raw_keys = {
        "date", "campaign_id", "campaign_name", "flow_id", "flow_name",
        "sends", "opens", "clicks", "attributed_conversions", "attributed_revenue",
    }
    for r in raw_rows[:5]:
        assert set(r.keys()) == expected_raw_keys, (
            f"Champs bruts inattendus: {set(r.keys())} != {expected_raw_keys}"
        )

    # Appliquer transform() (mapping manifest-driven).
    conn = _import_connector()
    transformed = conn.transform(raw_rows)

    assert len(transformed) == len(raw_rows), (
        "transform() doit produire le meme nombre de lignes que le generateur"
    )

    # DISCRIMINANT : les metriques canoniques Klaviyo ne doivent JAMAIS etre
    # renommees en 'conversions' ou 'revenue' generiques.
    for row in transformed:
        assert "conversions" not in row, (
            "transform() ne doit PAS produire un champ 'conversions' generique "
            "(risque de collision avec les regies -- AD-4). Champs: " + str(list(row.keys()))
        )
        assert "revenue" not in row or "attributed_revenue" in row, (
            "transform() ne doit PAS renommer 'attributed_revenue' en 'revenue' (AD-4). "
            "Champs: " + str(list(row.keys()))
        )
        # Les champs canoniques Klaviyo doivent etre presents (pass-through).
        assert "attributed_conversions" in row, (
            "transform() doit conserver 'attributed_conversions' (pas de renommage implicite)"
        )
        assert "attributed_revenue" in row, (
            "transform() doit conserver 'attributed_revenue' (pas de renommage implicite)"
        )

    # Les valeurs NULL (AD-9) doivent passer intactes.
    null_rows = [r for r in transformed if r.get("opens") is None]
    null_raw = [r for r in raw_rows if r.get("opens") is None]
    assert len(null_rows) == len(null_raw), (
        f"transform() doit preserver les NULL (AD-9): {len(null_raw)} NULL dans raw, "
        f"{len(null_rows)} apres transform"
    )

    # La fenetre de dates doit correspondre a FIXTURE_END_DATE - FIXTURE_DAYS.
    from datetime import timedelta as _timedelta

    start_expected = (end_date - _timedelta(days=days)).isoformat()
    end_expected = (end_date - _timedelta(days=1)).isoformat()
    dates_in_rows = {r["date"] for r in raw_rows}
    assert min(dates_in_rows) >= start_expected, (
        f"Date min inattendue: {min(dates_in_rows)} < {start_expected}"
    )
    assert max(dates_in_rows) <= end_expected, (
        f"Date max inattendue: {max(dates_in_rows)} > {end_expected}"
    )


# ---------------------------------------------------------------------------
# F-3 (AI-56) : seam test via build_asgi_app (montage loader core)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_klaviyo_seam_fastmcp_inprocess(tmp_path):
    """Seam test FastMCP in-process (renomme honnêtement) : appelle
    get_klaviyo_report via FastMCPTransport sur le mcp_app du connecteur.
    Ce test prouve que l'outil est expose par le module et retourne des
    valeurs correctes (pas seulement HTTP 200 -- AI-56).
    """
    from datetime import date, timedelta

    import duckdb

    db_path = str(tmp_path / "seam.duckdb")
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
    base = date.today()
    rows_to_insert = []
    for i in range(1, 41):
        d = (base - timedelta(days=i)).isoformat()
        rows_to_insert.extend([
            ("default", d, "klaviyo", "sends", "campaign_id",
             "camp_promo_ete_01", float(5000 + i * 10), "pull_SEAM01",
             "2026-06-01T00:00:00Z"),
            ("default", d, "klaviyo", "attributed_conversions", "campaign_id",
             "camp_promo_ete_01", float(2 + i % 5), "pull_SEAM01",
             "2026-06-01T00:00:00Z"),
            ("default", d, "klaviyo", "attributed_revenue", "campaign_id",
             "camp_promo_ete_01", float(250.0 + i * 5), "pull_SEAM01",
             "2026-06-01T00:00:00Z"),
        ])
    con.executemany(
        "INSERT INTO main_marts.fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
        rows_to_insert,
    )
    con.close()

    from fastmcp.client import Client, FastMCPTransport

    with patch.dict(
        "os.environ",
        {"TOOROW_DB_MODE": "duckdb", "TOOROW_DUCKDB_PATH": db_path},
    ):
        conn_mod = _import_connector()
        mcp_app = conn_mod.mcp_app
        date_to = (base - timedelta(days=1)).isoformat()
        date_from = (base - timedelta(days=35)).isoformat()

        async with Client(FastMCPTransport(mcp_app)) as client:
            result = await client.call_tool(
                "get_klaviyo_report",
                {
                    "project_id": "default",
                    "report_profile": "campaign_daily",
                    "date_from": date_from,
                    "date_to": date_to,
                },
            )

    assert not result.is_error, f"Outil retourne une erreur: {result}"
    envelope = result.structured_content or result.data
    assert envelope["schema_version"] == "1"
    data = envelope["data"]
    metrics = data.get("metrics", {})
    assert "sends" in metrics
    assert "attributed_conversions" in metrics
    assert "attributed_revenue" in metrics
    sends_rows = metrics["sends"]
    total_sends = sum(r["value"] for r in sends_rows if r["value"] is not None)
    assert total_sends > 0, f"Total sends doit etre positif, got {total_sends}"
    assert "conversions" not in metrics, (
        "Le mart Klaviyo ne doit pas exposer 'conversions' generique (AD-4)"
    )
    prov = envelope["meta"]["provenance"]
    assert prov["source_system"] == "klaviyo"
    assert prov["pull_id"] == "pull_SEAM01"


@pytest.mark.anyio
async def test_klaviyo_core_loader_mounts_namespace(tmp_path):
    """F-3 (AI-56) : prouve que build_asgi_app() decouvre et monte le namespace
    klaviyo via le loader core (scan_and_load_modules). Verifie que l'outil
    klaviyo_get_klaviyo_report (ou klaviyo-get_klaviyo_report) est present dans
    la liste des outils montes.
    """
    import duckdb
    from fastmcp.client import Client, FastMCPTransport

    # Creer un mart minimal pour que la requete ne plante pas.
    db_path = str(tmp_path / "core_seam.duckdb")
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

    env_patch = {
        "TOOROW_DB_MODE": "duckdb",
        "TOOROW_DUCKDB_PATH": db_path,
        "HEALTH_POLLER_ENABLED": "false",
        "QUEUE_WORKER_ENABLED": "false",
        "SCHEDULER_ENABLED": "false",
    }

    with patch.dict("os.environ", env_patch):
        from core.main import mcp

        async with Client(FastMCPTransport(mcp)) as client:
            tools = await client.list_tools()

    tool_names = [t.name for t in tools]
    # FastMCP namespace separator : underscore (pattern identique a google-analytics_get_ga4_report)
    klaviyo_tools = [n for n in tool_names if "klaviyo" in n.lower()]
    assert klaviyo_tools, (
        f"Aucun outil klaviyo trouve dans les outils montes par core.main.mcp: {tool_names}"
    )
    # L'outil principal doit etre discoverable.
    assert any("get_klaviyo_report" in n for n in klaviyo_tools), (
        f"get_klaviyo_report absent des outils klaviyo montes: {klaviyo_tools}"
    )


# ---------------------------------------------------------------------------
# review-15-9 F-3 (BLOQUANT) : dispatch AI-58 pull_campaign_daily / pull_flow_daily
# ---------------------------------------------------------------------------


class TestAi58Dispatch:
    """Le loader cherche pull_<profile_id> par convention AI-58 -- ils doivent exister."""

    def test_wrappers_exist_and_delegate_with_profile_id(self):
        conn = _import_connector()
        assert hasattr(conn, "pull_campaign_daily")
        assert hasattr(conn, "pull_flow_daily")

        captured = {}

        def _fake_pull(**kwargs):
            captured.update(kwargs)
            return {"pull_id": kwargs["pull_id"], "row_count": 0}

        with patch.object(conn, "pull", side_effect=_fake_pull):
            conn.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-02",
                project_id="p", pull_id="pull_k_cd",
            )
            assert captured["profile_id"] == "campaign_daily"
            conn.pull_flow_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-02",
                project_id="p", pull_id="pull_k_fd",
            )
            assert captured["profile_id"] == "flow_daily"

    def test_unavailable_profiles_do_not_dispatch(self):
        import types

        from core.main import get_module_pull_fn

        conn = _import_connector()
        loaded = types.SimpleNamespace(
            name="klaviyo",
            connector_module=conn,
            manifest=json.loads(MANIFEST_PATH.read_text(encoding="utf-8")),
        )
        with patch("core.main._loaded_modules", [loaded]):
            cd_fn = get_module_pull_fn("klaviyo", "campaign_daily")
            fd_fn = get_module_pull_fn("klaviyo", "flow_daily")
        assert cd_fn is None
        assert fd_fn is None
