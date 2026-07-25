"""Klaviyo connector — Story 15.8 (Epic 15, connecteurs vague 1).

Email/SMS marketing — source de metriques d'attribution canal email.
Expose une instance ``mcp_app: FastMCP`` que le loader du core monte sous
le namespace ``klaviyo`` (AD-2). Meme patron "drop-a-folder" que meta-ads /
shopify : zero edit sous server/core, un bloc UNION additif dans le mart.

# AD-12: le MCP server lit UNIQUEMENT le mart fact_daily_kpi -- pas de raw_*.
# AD-3: le token (cle API Klaviyo) vient de Nango juste avant usage -- jamais stocke.
# AD-7: pull_id frappe par le scheduler du core, passe dans pull().
# AD-14: parametre identity/project_id.

Decisions de story (15-8-connecteur-klaviyo.md) :
  * API KLAVIYO : Reporting API (POST /api/campaign-values-reports/ et
    POST /api/flow-series-reports/) -- donnees identiques a l'UI Klaviyo.
    Le endpoint metric-aggregates est DISTINCT (filtre par evenement, pas
    par date d'envoi) ; il est evite pour les metriques de campagne/flow.
  * ATTRIBUTION : les metriques attributed_conversions et attributed_revenue
    sont des CONVERSIONS REVENDIQUEES (fenetre last-touch 5j clic/ouverture
    email, configurable par compte). JAMAIS sommees avec les regies ni le
    revenue Shopify/Stripe sans la regle AD-4.
  * AUTH : cle API privee Klaviyo, header "Authorization: Klaviyo-API-Key <key>",
    stockee UNIQUEMENT dans Nango (AD-3). Revision API : 2024-10-15 (declaree,
    a confirmer en passe live Phase B -- AI-53).
  * PAGINATION : Klaviyo Reporting API retourne une page unique de resultats
    (pas de cursor) ; les pulls sont donc non-paginee mais bornes par date.
  * NULL HONNETE (AD-9) : metriques absentes dans la reponse -> NULL, jamais 0.

# AI-53 : tous les champs/endpoints API Klaviyo declares d'apres la doc officielle
# et annotes "a confirmer en passe live (Phase B)". Rien de non verifiable n'est
# affirme. Source principale : https://developers.klaviyo.com/en/reference/
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance — the public surface the loader mounts.
mcp_app = FastMCP("klaviyo")

# ---------------------------------------------------------------------------
# Database connection helpers — env-var driven (no hardcoded paths).
# Same dual-backend pattern que meta-ads / shopify.
#   TOOROW_DB_MODE     = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH = path to local .duckdb file
#   GCP_PROJECT           = GCP project ID (bigquery mode only)
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


def _query_duckdb(sql: str, params: list, duckdb_path: str) -> list[dict]:
    """Execute parameterized *sql* contre le warehouse DuckDB local (F-01)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
    """Execute parameterized *sql* contre BigQuery (F-01: @named parameters)."""
    from google.cloud import bigquery  # noqa: PLC0415

    project = os.environ.get("GCP_PROJECT", "")
    client = bigquery.Client(project=project or None)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, "STRING", value)
            for name, value in params.items()
        ]
    )
    result = client.query(sql, job_config=job_config).result()
    cols = [f.name for f in result.schema]
    return [dict(zip(cols, row)) for row in result]


def _get_mart_table(db_mode: str) -> str:
    """Reference qualifiee du mart fact_daily_kpi selon le moteur.

    DuckDB: dbt materialise les marts dans le schema main_marts.
    BigQuery (F-02): un qualificateur de dataset est obligatoire.
    """
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


# Filtre breakdown_dimension selon le report_profile (F-6) :
#   campaign_daily -> campaign_id seulement (stg_klaviyo_campaigns_daily)
#   flow_daily     -> flow_id seulement (stg_klaviyo_flows_daily)
#   absent/autre   -> les deux dimensions (aucun filtre)
# Les deux sous-dimensions sont MUTUELLEMENT EXCLUSIVES dans le mart (une ligne
# raw a soit campaign_id soit flow_id, jamais les deux). Sans filtre, le MCP
# outil retournerait campaign_id ET flow_id confondus dans la meme reponse, ce
# qui est ambigu pour le consommateur (un profil campaign_daily ne devrait pas
# voir des flow_id et vice-versa).
_PROFILE_TO_DIMENSION: dict[str, str | None] = {
    "campaign_daily": "campaign_id",
    "flow_daily": "flow_id",
}

_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'klaviyo'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
      {dim_filter}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _build_dim_filter(report_profile: str, db_mode: str) -> tuple[str, list]:
    """Retourne (clause SQL, params extra) pour filtrer breakdown_dimension.

    DuckDB : parametres positionnels (?).
    BigQuery : parametres nommes (@dim).
    Pas de filtre (retourne '' et []) si le profil n'est pas reconnu.
    """
    dim = _PROFILE_TO_DIMENSION.get(report_profile)
    if dim is None:
        return "", []
    if db_mode == "duckdb":
        return "AND breakdown_dimension = ?", [dim]
    # BigQuery : les params supplementaires sont passes via query_parameters.
    return "AND breakdown_dimension = @dim", [dim]


def _query_mart(
    date_from: str,
    date_to: str,
    project_id: str = "default",
    report_profile: str = "",
) -> list[dict]:
    """Query fact_daily_kpi mart — DuckDB ou BigQuery selon l'env.

    # AD-12: MCP server lit les marts uniquement -- jamais les tables raw_*.
    # F-6: filtre breakdown_dimension selon report_profile pour ne retourner que
    #      les lignes campaign_id (campaign_daily) ou flow_id (flow_daily).
    """
    db_mode = _get_db_mode()
    table = _get_mart_table(db_mode)
    dim_clause, dim_params = _build_dim_filter(report_profile, db_mode)

    if db_mode == "duckdb":
        sql = _MART_QUERY.format(
            table=table, p_project="?", p_from="?", p_to="?", dim_filter=dim_clause
        )
        params = [project_id, date_from, date_to] + dim_params
        return _query_duckdb(sql, params, _get_duckdb_path())
    elif db_mode == "bigquery":
        sql = _MART_QUERY.format(
            table=table,
            p_project="@project_id",
            p_from="@date_from",
            p_to="@date_to",
            dim_filter=dim_clause,
        )
        bq_params: dict = {
            "project_id": project_id, "date_from": date_from, "date_to": date_to,
        }
        if dim_params:
            bq_params["dim"] = dim_params[0]
        return _query_bigquery(sql, bq_params)
    else:
        raise ValueError(f"TOOROW_DB_MODE inconnu: {db_mode!r}")


def _build_envelope(
    rows: list[dict],
    report_profile: str,
    date_from: str,
    date_to: str,
    project_id: str,
) -> dict:
    """Construit l'envelope canonique AD-1 a partir des lignes du mart."""
    pull_ids = {r["pull_id"] for r in rows if r.get("pull_id")}
    freshness_values = [r["freshness"] for r in rows if r.get("freshness")]

    latest_pull_id = max(pull_ids) if pull_ids else None
    latest_freshness = max(freshness_values) if freshness_values else None

    data_by_metric: dict[str, list[dict]] = {}
    for r in rows:
        metric = r["metric"]
        if metric not in data_by_metric:
            data_by_metric[metric] = []
        data_by_metric[metric].append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    provenance = (
        {
            "source_system": "klaviyo",
            "source_field": "fact_daily_kpi",
            "pull_id": latest_pull_id,
        }
        if latest_pull_id is not None
        else None
    )

    return {
        "schema_version": "1",
        "meta": {
            "freshness": latest_freshness,
            "provenance": provenance,
            "alerts": [],
        },
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "metrics": data_by_metric,
        },
    }


@mcp_app.tool()
def get_klaviyo_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Klaviyo Email/SMS Marketing Report — lit depuis le mart fact_daily_kpi.

    Report profiles: campaign_daily, flow_daily
    Metriques: sends, opens, clicks, attributed_conversions, attributed_revenue
      (toutes additives au grain journalier, AD-4).

    REGLE DE NON-AGREGATION CRITIQUE (AD-4, Story 15.8) :
      attributed_conversions et attributed_revenue sont des metriques REVENDIQUEES
      par le canal email/SMS Klaviyo (fenetre last-touch 5j clic/ouverture email par
      defaut, configurable par compte -- AI-53 verifie le 2026-07-19).
      Elles ne doivent JAMAIS etre sommees avec :
        - les conversions des regies (Meta, TikTok, Google Ads) ;
        - le revenue Shopify ou Stripe dans un total croise.
      Leur usage legitime : alimentation de la deduplication (Epic 17, contribution
      canal email) et comparaison canal par canal. Toute agregation croisee requiert
      la regle AD-4 / cross_source_conversions.

    Breakdown: campaign_id / campaign_name (profil campaign_daily)
               flow_id / flow_name (profil flow_daily).

    Retourne l'envelope canonique AD-1.

    Parameters:
        project_id: Identifiant projet (defaut: 'default', AD-14 placeholder)
        report_profile: campaign_daily | flow_daily
        date_from: Date debut ISO-8601 (ex: '2026-04-01'). Defaut: 90 jours avant hier.
        date_to: Date fin ISO-8601 (ex: '2026-06-30'). Defaut: hier.
    """
    # AD-12: MCP server lit uniquement le mart fact_daily_kpi.
    from datetime import date, timedelta  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=90)).isoformat()

    try:
        rows = _query_mart(date_from, date_to, project_id, report_profile)
    except Exception as exc:
        return {
            "schema_version": "1",
            "meta": {
                "freshness": None,
                "provenance": None,
                "alerts": [{"level": "error", "message": str(exc)}],
            },
            "data": {
                "project_id": project_id,
                "report_profile": report_profile,
                "date_from": date_from,
                "date_to": date_to,
                "metrics": {},
            },
        }

    return _build_envelope(rows, report_profile, date_from, date_to, project_id)


# ---------------------------------------------------------------------------
# Klaviyo Reporting API pull — Story 15.8
#
# AI-53 A CONFIRMER EN STORY (live, Phase B) : les endpoints, champs et revision
# sont declares d'apres la doc officielle :
#   https://developers.klaviyo.com/en/reference/reporting_api_overview
# La passe live API Klaviyo = BLOCKED Phase B (AI-08/AI-13).
# ---------------------------------------------------------------------------

# AI-53 VERIFIE le 2026-07-19 : revision de l'API Klaviyo declaree dans les headers.
# Format YYYY-MM-DD ; valeur la plus recente connue a la date de story.
# A confirmer/mettre a jour en passe live (Phase B).
KLAVIYO_API_REVISION = os.environ.get("KLAVIYO_API_REVISION", "2024-10-15")

KLAVIYO_API_BASE = "https://a.klaviyo.com/api"

# AI-53 VERIFIE le 2026-07-19 : statistiques disponibles via la Reporting API
# (campaign-values-reports, flow-series-reports). Source :
# https://developers.klaviyo.com/en/reference/reporting_api_overview
# Noms de champs a confirmer en passe live (conventions exactes de la reponse JSON).
_REPORTING_STATISTICS = [
    "sends",
    "opens",
    "clicks",
    "conversions",
    "revenue",
]

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_klaviyo_daily (
    date                     VARCHAR,
    campaign_id              VARCHAR,
    campaign_name            VARCHAR,
    flow_id                  VARCHAR,
    flow_name                VARCHAR,
    sends                    DOUBLE,
    opens                    DOUBLE,
    clicks                   DOUBLE,
    attributed_conversions   DOUBLE,
    attributed_revenue       DOUBLE,
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_klaviyo_daily
    (date, campaign_id, campaign_name, flow_id, flow_name,
     sends, opens, clicks, attributed_conversions, attributed_revenue,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insere les lignes canoniques dans raw_klaviyo_daily (DuckDB uniquement a P-dev).

    NULL honnete (AD-9) : une metrique absente dans la reponse API reste NULL --
    jamais remplacee par 0. Cela permet de distinguer «donnee manquante» de «zero
    enregistre». La regle est appliquee dans _parse_reporting_row() avant l'insertion.

    REGLE DE NON-AGREGATION (AD-4) : attributed_conversions et attributed_revenue
    sont stockes dans leurs propres colonnes. Le staging dbt ne les additionne
    JAMAIS a des colonnes de revenue ou de conversions cross-source.
    """
    if db_mode == "duckdb":
        import duckdb  # noqa: PLC0415

        con = duckdb.connect(duckdb_path)
        con.execute(_RAW_CREATE_DDL)
        values = [
            (
                r.get("date", ""),
                r.get("campaign_id") or None,
                r.get("campaign_name") or None,
                r.get("flow_id") or None,
                r.get("flow_name") or None,
                # AD-9 NULL honnete : les metriques absentes restent None/NULL.
                (float(r["sends"]) if r.get("sends") is not None else None),
                (float(r["opens"]) if r.get("opens") is not None else None),
                (float(r["clicks"]) if r.get("clicks") is not None else None),
                (
                    float(r["attributed_conversions"])
                    if r.get("attributed_conversions") is not None
                    else None
                ),
                (
                    float(r["attributed_revenue"])
                    if r.get("attributed_revenue") is not None
                    else None
                ),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        if values:
            con.executemany(_RAW_INSERT_SQL, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows: db_mode {db_mode!r} non supporte a P-dev "
            "(BigQuery non encore implemente)"
        )


def _parse_reporting_row(
    result_item: dict,
    date: str,
    dimension_type: str,
) -> dict:
    """Mappe un item de la reponse Reporting API sur la forme canonique raw.

    AI-53 A CONFIRMER EN STORY : la structure de reponse supposee d'apres la doc :
      result_item = {
        "groupings": {"campaign_id": "...", "campaign_name": "..."},
        "statistics": {
          "sends": [...],        # valeurs alignees sur les dates
          "opens": [...],
          "clicks": [...],
          "conversions": [...],  # attributed_conversions
          "revenue": [...],      # attributed_revenue
          "dates": [...]         # ISO-8601 dates
        }
      }
    La structure exacte (notamment les noms de champs dans statistics et groupings)
    est a valider en passe live (Phase B, AI-13).

    NULL honnete (AD-9) : valeur absente ou None -> None (pas de remplacement par 0).
    """
    groupings = result_item.get("groupings", {})
    stats = result_item.get("statistics", {})

    # Retrouver l'index de la date dans le tableau dates de la reponse.
    dates_array = stats.get("dates", [])
    try:
        date_idx = dates_array.index(date)
    except ValueError:
        # La date n'est pas presente dans la reponse pour cet item.
        return {}

    def _get_stat(key: str) -> float | None:
        """Extraire la valeur d'un stat a l'index date_idx (NULL honnete, AD-9)."""
        arr = stats.get(key, [])
        if not arr or date_idx >= len(arr):
            return None
        val = arr[date_idx]
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    return {
        "date": date,
        "campaign_id": (groupings.get("campaign_id") if dimension_type == "campaign" else None),
        "campaign_name": (groupings.get("campaign_name") if dimension_type == "campaign" else None),
        "flow_id": (groupings.get("flow_id") if dimension_type == "flow" else None),
        "flow_name": (groupings.get("flow_name") if dimension_type == "flow" else None),
        "sends": _get_stat("sends"),
        "opens": _get_stat("opens"),
        "clicks": _get_stat("clicks"),
        # AI-53 : "conversions" dans la reponse API Klaviyo = attributed_conversions (canal)
        "attributed_conversions": _get_stat("conversions"),
        # AI-53 : "revenue" dans la reponse API Klaviyo = attributed_revenue (canal)
        "attributed_revenue": _get_stat("revenue"),
    }


def _build_reporting_request(
    date_from: str,
    date_to: str,
    report_type: str,
    conversion_metric_id: str,
) -> dict:
    """Construit le corps de la requete POST pour la Reporting API Klaviyo.

    AI-53 A CONFIRMER EN STORY : structure supposee d'apres la doc :
    https://developers.klaviyo.com/en/reference/reporting_api_overview
    Les noms de champs exacts (timeframe.start/end vs date_from/date_to, etc.)
    sont a valider en passe live (Phase B, AI-13).
    """
    return {
        "data": {
            "type": report_type,
            "attributes": {
                "statistics": _REPORTING_STATISTICS,
                "timeframe": {
                    "start": date_from,
                    "end": date_to,
                },
                "interval": "daily",
                "conversion_metric_id": conversion_metric_id,
            },
        }
    }


# Pagination bornee : la Reporting API retourne en principe une page unique.
# On ajoute un cap par precaution (AI-53 : a confirmer si la reponse est paginee).
_MAX_PAGES = 10


def _post_reporting(
    client: httpx.Client,
    endpoint: str,
    body: dict,
    headers: dict,
) -> list[dict]:
    """POST vers un endpoint de la Reporting API, retourne la liste des results.

    Pagination : la Reporting API retourne une page unique de resultats (non
    confirme live -- AI-53). On gere quand meme un cursor next au cas ou,
    avec un cap de _MAX_PAGES pour eviter toute boucle infinie.
    """
    url: str | None = f"{KLAVIYO_API_BASE}/{endpoint}"
    all_results: list[dict] = []
    pages = 0

    while url and pages < _MAX_PAGES:
        pages += 1
        resp = client.post(url, json=body, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            # Rate limit : RateLimitError pour que le worker trip le breaker.
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(retry_after_raw)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("klaviyo", retry_after)

        if resp.status_code != 200:
            # Story 25.2: canonical typed error; provider payload preserved.
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body)

        payload = resp.json()
        results = payload.get("data", {}).get("attributes", {}).get("results", [])
        all_results.extend(results)

        # Cursor next (precaution -- probablement absent en Reporting API).
        # AI-53 : a confirmer la structure de pagination en passe live.
        next_url = (
            payload.get("links", {}).get("next")
            or payload.get("data", {}).get("links", {}).get("next")
        )
        url = next_url
        body = {}  # les appels de page suivante n'ont pas de body (cursor dans l'URL)

    return all_results


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    conversion_metric_id: str | None = None,
    profile_id: str | None = None,
) -> dict:
    """Fetch les metriques Klaviyo (campagnes + flows) via la Reporting API.

    # AD-12: appele par le queue worker uniquement -- pas d'appel synchrone pendant
    #        une invocation d'outil LLM.
    # AD-3: la cle API est utilisee immediatement dans les headers puis sort de scope --
    #        JAMAIS stockee, loggee ni passee ailleurs.
    # AD-7: pull_id frappe par l'appelant ; cette fonction ne le forge pas.

    AI-53 A CONFIRMER EN STORY (live, Phase B) :
      - Structure exacte des endpoints Reporting API (noms de champs, pagination).
      - conversion_metric_id : ID de la metrique de conversion Klaviyo (ex: ID de la
        metrique "Placed Order"). A recuperer via GET /api/metrics/ et parametrable
        par projet. Defaut : valeur de la var d'env KLAVIYO_CONVERSION_METRIC_ID.
      - profile_id (dispatch AI-58) : si fourni, filtre les resultats pour ce profil.

    Parameters
    ----------
    connection_id:
        Identifiant de connexion Nango, passe a get_fresh_token(provider='klaviyo').
    date_from, date_to:
        Dates ISO-8601 (YYYY-MM-DD) pour la fenetre de reporting Klaviyo.
    project_id:
        Binding projet pour les lignes raw.
    pull_id:
        ULID de pull forge par le core (AD-7). Recu en parametre, jamais forge ici.
    conversion_metric_id:
        ID de la metrique de conversion Klaviyo (defaut: var env
        KLAVIYO_CONVERSION_METRIC_ID). A confirmer en passe live (AI-53).
    profile_id:
        Si fourni, dispatch pull_<profile_id> (AI-58) -- filtre par profil.
        Non supporte en Phase A (BLOCKED Phase B).

    Returns
    -------
    dict
        {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}

    Raises
    ------
    ValueError
        Si KLAVIYO_CONVERSION_METRIC_ID est manquant et conversion_metric_id est None.
    RuntimeError
        Si la Reporting API retourne une reponse non-200 (non-429).
    RateLimitError
        Si la Reporting API retourne HTTP 429.
    """
    # AI-58 : dispatch pull_<profile_id> -- BLOCKED Phase B.
    if profile_id is not None:
        logger.warning(
            "klaviyo_pull: profile_id dispatch (AI-58) non supporte en Phase A -- "
            "ignoring profile_id=%s (BLOCKED Phase B)",
            profile_id,
        )

    from core import nango_client  # noqa: PLC0415 -- AD-2: import au moment de l'appel

    # Resoudre le conversion_metric_id (requis par la Reporting API).
    # AI-53 : cet ID identifie la metrique "Placed Order" dans le compte Klaviyo.
    # Recuperable via GET /api/metrics/ (a confirmer en passe live, Phase B).
    if conversion_metric_id is None:
        conversion_metric_id = os.environ.get("KLAVIYO_CONVERSION_METRIC_ID")
    if not conversion_metric_id:
        raise ValueError(
            "KLAVIYO_CONVERSION_METRIC_ID env var requis pour pull "
            "(ID de la metrique de conversion Klaviyo, ex: ID 'Placed Order'). "
            "Recuperable via GET https://a.klaviyo.com/api/metrics/"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtenu juste avant usage ; sort de scope apres l'appel.
    token = nango_client.get_fresh_token(connection_id, provider="klaviyo")

    headers = {
        "Authorization": f"Klaviyo-API-Key {token}",
        "revision": KLAVIYO_API_REVISION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    all_raw_rows: list[dict] = []

    with httpx.Client() as http_client:
        # --- Campagnes (campaign-values-reports) ---
        # AI-53 A CONFIRMER EN STORY : endpoint et structure de reponse.
        campaign_body = _build_reporting_request(
            date_from, date_to, "campaign-values-report", conversion_metric_id
        )
        try:
            campaign_results = _post_reporting(
                http_client,
                "campaign-values-reports/",
                campaign_body,
                headers,
            )
        except RuntimeError as exc:
            # Story 25.7: les erreurs TYPEES (taxonomie 25.2 -- auth_expired,
            # provider_transient...) doivent remonter au worker (retry policy par
            # classe) ; un 401 avale ici produirait un pull "reussi" vide. Le
            # best-effort ne conserve que les RuntimeError legacy non typees.
            from core.pull_errors import ConnectorError  # noqa: PLC0415

            if isinstance(exc, ConnectorError):
                raise
            logger.error("klaviyo_pull: erreur campagnes: %s (pull_id=%s)", exc, pull_id)
            campaign_results = []

        # Extraire les lignes par date depuis la reponse de la Reporting API.
        # La Reporting API retourne des series temporelles : chaque result_item
        # couvre toute la periode, et les valeurs sont des tableaux alignes sur dates.
        # AI-53 : structure exacte a confirmer en passe live (Phase B).
        dates_range = _date_range(date_from, date_to)
        for result_item in campaign_results:
            for date_str in dates_range:
                row = _parse_reporting_row(result_item, date_str, "campaign")
                if row:
                    all_raw_rows.append(row)

        # --- Flows (flow-series-reports) ---
        # AI-53 A CONFIRMER EN STORY : endpoint et structure de reponse.
        flow_body = _build_reporting_request(
            date_from, date_to, "flow-series-report", conversion_metric_id
        )
        try:
            flow_results = _post_reporting(
                http_client,
                "flow-series-reports/",
                flow_body,
                headers,
            )
        except RuntimeError as exc:
            # Story 25.7: memes semantiques que le bloc campagnes ci-dessus.
            from core.pull_errors import ConnectorError  # noqa: PLC0415

            if isinstance(exc, ConnectorError):
                raise
            logger.error("klaviyo_pull: erreur flows: %s (pull_id=%s)", exc, pull_id)
            flow_results = []

        for result_item in flow_results:
            for date_str in dates_range:
                row = _parse_reporting_row(result_item, date_str, "flow")
                if row:
                    all_raw_rows.append(row)

    canonical_rows = transform(all_raw_rows)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: pas de token dans les logs -- seulement pull_id et row_count.
    logger.info("klaviyo_pull_completed: pull_id=%s row_count=%d", pull_id, row_count)

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# AI-58 per-profile dispatch: pull_<profile_id>. core.get_module_pull_fn resolves
# these by naming convention (source-agnostic, AD-2). review-15-9 F-3 (BLOQUANT):
# le loader cherche pull_campaign_daily / pull_flow_daily par convention AI-58 -- ils
# n'existaient pas, donc le dispatch par profil echouait silencieusement au grain. Ils
# delegent a pull() en passant le profile_id correspondant. Le filtrage live par profil
# est BLOCKED Phase B (pull() logge un warning et ignore profile_id en Phase A -- les
# deux grains campagne/flow sont pulled ensemble ; le mart/MCP filtre par dimension).
# ---------------------------------------------------------------------------


def pull_campaign_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    conversion_metric_id: str | None = None,
) -> dict:
    """AI-58 dispatch: grain campagne (breakdown campaign_id).

    Delegue a pull() avec profile_id='campaign_daily'. Le filtrage live par profil est
    BLOCKED Phase B (AI-08/AI-13) : en Phase A, pull() ramene campagnes + flows ensemble
    et le mart isole les deux dimensions (mutuellement exclusives) -- voir docstring pull().
    """
    return pull(
        connection_id=connection_id,
        date_from=date_from,
        date_to=date_to,
        project_id=project_id,
        pull_id=pull_id,
        conversion_metric_id=conversion_metric_id,
        profile_id="campaign_daily",
    )


def pull_flow_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    conversion_metric_id: str | None = None,
) -> dict:
    """AI-58 dispatch: grain flow (breakdown flow_id).

    Delegue a pull() avec profile_id='flow_daily'. Le filtrage live par profil est
    BLOCKED Phase B (AI-08/AI-13) : en Phase A, pull() ramene campagnes + flows ensemble
    et le mart isole les deux dimensions (mutuellement exclusives) -- voir docstring pull().
    """
    return pull(
        connection_id=connection_id,
        date_from=date_from,
        date_to=date_to,
        project_id=project_id,
        pull_id=pull_id,
        conversion_metric_id=conversion_metric_id,
        profile_id="flow_daily",
    )


def _date_range(date_from: str, date_to: str) -> list[str]:
    """Retourne la liste des dates ISO-8601 entre date_from et date_to inclus."""
    from datetime import date, timedelta  # noqa: PLC0415

    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    result = []
    current = start
    while current <= end:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# Story 25.9 — catalog_driven execution: pull_catalog_daily.
#
# Principle (Jean): the catalog IS the execution contract. For Klaviyo, the
# selection's source_fields carry the LEGACY manifest tokens (sends, revenue).
# The two hard drifts adjudicated by the 25.6 probe (ROLLOUT_NOTES.md):
#   sends   -> recipients        (request-time bridge; lands as 'sends' column)
#   revenue -> conversion_value  (request-time bridge; lands as 'revenue' column)
# The raw table keeps the legacy column names (unchanged _insert_raw_rows).
# ---------------------------------------------------------------------------

# Drift bridges: legacy manifest token -> official Klaviyo statistic name
# (applied at REQUEST time so the Reporting API receives the correct name).
_STAT_BRIDGE: dict[str, str] = {
    "sends": "recipients",
    "revenue": "conversion_value",
}

# Reverse: official Klaviyo statistic name -> legacy raw column name
# (applied at LANDING time so raw_klaviyo_daily keeps its legacy schema).
_STAT_BRIDGE_REVERSE: dict[str, str] = {
    "recipients": "sends",
    "conversion_value": "revenue",
}

# Valid grouping keys per endpoint (from official Klaviyo OpenAPI — ROLLOUT_NOTES.md).
# campaign-values-reports grouping dimensions.
_CAMPAIGN_GROUPING_KEYS: frozenset[str] = frozenset({
    "campaign_id",
    "campaign_message_id",
    "campaign_message_name",
    "group",
    "group_name",
    "send_channel",
    "tag_id",
    "tag_name",
    "text_message_format",
    "variation",
    "variation_name",
})

# flow-series-reports grouping dimensions.
_FLOW_GROUPING_KEYS: frozenset[str] = frozenset({
    "flow_id",
    "flow_message_id",
    "flow_message_name",
    "flow_name",
    "send_channel",
    "tag_id",
    "tag_name",
    "text_message_format",
    "variation",
    "variation_name",
})

# Structural dims that are never passed as grouping_keys (they come from the
# series interval, not from the groupings block).
_STRUCTURAL_DIMS: frozenset[str] = frozenset({"date"})


def _build_catalog_statistics(selection: dict) -> list[str]:
    """Build the official Klaviyo statistics list from the selection's source_fields.

    Applies _STAT_BRIDGE at request time:
      - source_field 'sends'   -> 'recipients'   (drift bridge, 25.6 adjudication)
      - source_field 'revenue' -> 'conversion_value' (drift bridge, 25.6 adjudication)
    Dimensions (kind=dimension by naming convention via _STRUCTURAL_DIMS /
    _CAMPAIGN_GROUPING_KEYS / _FLOW_GROUPING_KEYS) are excluded.

    Returns a de-duplicated, ordered list of official statistic names.
    """
    seen: set[str] = set()
    stats: list[str] = []
    source_fields: dict[str, str] = selection.get("source_fields", {})
    metrics: list[str] = list(selection.get("metrics", []))

    for field_id in metrics:
        raw_source = source_fields.get(field_id, field_id)
        # Apply the drift bridge if applicable.
        official = _STAT_BRIDGE.get(raw_source, raw_source)
        if official not in seen:
            seen.add(official)
            stats.append(official)

    return stats


def _build_catalog_groupings(selection: dict, endpoint_type: str) -> list[str]:
    """Return the grouping_keys list for the given endpoint ('campaign' or 'flow').

    Only keys valid for the endpoint are included; structural dims (date) and
    keys belonging to the opposite endpoint are excluded.
    """
    valid_keys = _CAMPAIGN_GROUPING_KEYS if endpoint_type == "campaign" else _FLOW_GROUPING_KEYS
    source_fields: dict[str, str] = selection.get("source_fields", {})
    dimensions: list[str] = list(selection.get("dimensions", []))

    seen: set[str] = set()
    groupings: list[str] = []
    for field_id in dimensions:
        raw_source = source_fields.get(field_id, field_id)
        if raw_source in _STRUCTURAL_DIMS:
            continue
        if raw_source not in valid_keys:
            continue
        if raw_source not in seen:
            seen.add(raw_source)
            groupings.append(raw_source)

    return groupings


def _selection_wants_campaign(selection: dict) -> bool:
    """True when the selection should trigger the campaign-values-reports endpoint.

    Logic: True unless the selection explicitly includes flow-exclusive grouping
    keys (flow_id, flow_name, etc.) with NO campaign-exclusive grouping keys.
    If only structural dims are present, both paths are triggered (default).
    """
    source_fields: dict[str, str] = selection.get("source_fields", {})
    dimensions: list[str] = list(selection.get("dimensions", []))

    has_campaign = False
    has_flow = False
    for field_id in dimensions:
        raw_source = source_fields.get(field_id, field_id)
        if raw_source in _STRUCTURAL_DIMS:
            continue
        if raw_source in _CAMPAIGN_GROUPING_KEYS:
            has_campaign = True
        elif raw_source in _FLOW_GROUPING_KEYS:
            has_flow = True

    if has_flow and not has_campaign:
        return False
    return True


def _selection_wants_flow(selection: dict) -> bool:
    """True when the selection should trigger the flow-series-reports endpoint.

    Logic: True unless the selection explicitly includes campaign-exclusive
    grouping keys (campaign_id, etc.) with NO flow-exclusive grouping keys.
    If only structural dims are present, both paths are triggered (default).
    """
    source_fields: dict[str, str] = selection.get("source_fields", {})
    dimensions: list[str] = list(selection.get("dimensions", []))

    has_campaign = False
    has_flow = False
    for field_id in dimensions:
        raw_source = source_fields.get(field_id, field_id)
        if raw_source in _STRUCTURAL_DIMS:
            continue
        if raw_source in _CAMPAIGN_GROUPING_KEYS:
            has_campaign = True
        elif raw_source in _FLOW_GROUPING_KEYS:
            has_flow = True

    if has_campaign and not has_flow:
        return False
    return True


def _parse_catalog_reporting_row(
    result_item: dict,
    date: str,
    endpoint_type: str,
    official_stats: list[str],
) -> dict:
    """Parse one result_item from the Reporting API for a specific date.

    Applies _STAT_BRIDGE_REVERSE at landing time:
      - 'recipients'     -> 'sends' column    (legacy raw schema)
      - 'conversion_value' -> 'revenue' column (legacy raw schema)

    NULL honest (AD-9): a statistic absent from the response stays None.
    Returns {} when the queried date is not present in the result_item's dates array.
    """
    stats = result_item.get("statistics", {})
    groupings = result_item.get("groupings", {})

    dates_array = stats.get("dates", [])
    try:
        date_idx = dates_array.index(date)
    except ValueError:
        return {}

    def _get_stat(key: str) -> float | None:
        arr = stats.get(key, [])
        if not arr or date_idx >= len(arr):
            return None
        val = arr[date_idx]
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    row: dict = {"date": date}

    # Grouping columns: set relevant dims, nullify the cross-entity ones.
    if endpoint_type == "campaign":
        row["campaign_id"] = groupings.get("campaign_id")
        row["campaign_name"] = groupings.get("campaign_name")
        row["flow_id"] = None
        row["flow_name"] = None
    else:  # flow
        row["flow_id"] = groupings.get("flow_id")
        row["flow_name"] = groupings.get("flow_name")
        row["campaign_id"] = None
        row["campaign_name"] = None

    # Statistics: apply reverse bridge for legacy column names.
    for official in official_stats:
        legacy_col = _STAT_BRIDGE_REVERSE.get(official, official)
        row[legacy_col] = _get_stat(official)

    return row


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    conversion_metric_id: str | None = None,
) -> dict:
    """Story 25.9 catalog_driven daily pull for Klaviyo.

    core resolves+validates selection against api_catalog.json and passes it as
    {"metrics", "dimensions", "source_fields"}. A None selection falls back to
    the catalog tier-core default (resolved here so the callable is safe standalone).

    The pull:
      1. Resolves conversion_metric_id (required by Reporting API).
      2. Builds the official statistics list from source_fields (drift-bridged).
      3. Selects campaign and/or flow endpoint path(s) from the dimensions.
      4. Builds grouping_keys per endpoint (only valid keys, structural dims excluded).
      5. Posts to campaign-values-reports and/or flow-series-reports.
      6. Parses rows per date, reverse-bridges stat names to legacy column names.
      7. Lands via the unchanged _insert_raw_rows (raw_klaviyo_daily schema stable).

    # AD-3: token used immediately then discarded. AD-7: pull_id passed in.
    # AD-22: existing pull() / pull_campaign_daily() / pull_flow_daily() untouched.
    # AI-58: pull_catalog_daily is a module-level callable (dispatch naming conv).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    # Resolve conversion_metric_id (required; raises clearly if absent).
    if conversion_metric_id is None:
        conversion_metric_id = os.environ.get("KLAVIYO_CONVERSION_METRIC_ID")
    if not conversion_metric_id:
        raise ValueError(
            "KLAVIYO_CONVERSION_METRIC_ID env var required for pull_catalog_daily "
            "(ID of the Klaviyo conversion metric, e.g. 'Placed Order'. "
            "Retrievable via GET https://a.klaviyo.com/api/metrics/)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # Build the statistics list and determine which endpoint paths to call.
    official_stats = _build_catalog_statistics(selection or {})
    wants_campaign = _selection_wants_campaign(selection or {})
    wants_flow = _selection_wants_flow(selection or {})

    # AD-3: token obtained just before use; out of scope after the with block.
    token = nango_client.get_fresh_token(connection_id, provider="klaviyo")
    headers = {
        "Authorization": f"Klaviyo-API-Key {token}",
        "revision": KLAVIYO_API_REVISION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    all_raw_rows: list[dict] = []
    dates_range = _date_range(date_from, date_to)

    with httpx.Client() as http_client:
        # --- Campaign path ---
        if wants_campaign:
            campaign_groupings = _build_catalog_groupings(selection or {}, "campaign")
            campaign_body: dict = {
                "data": {
                    "type": "campaign-values-report",
                    "attributes": {
                        "statistics": official_stats,
                        "timeframe": {
                            "start": date_from,
                            "end": date_to,
                        },
                        "interval": "daily",
                        "conversion_metric_id": conversion_metric_id,
                    },
                }
            }
            if campaign_groupings:
                campaign_body["data"]["attributes"]["grouping_keys"] = campaign_groupings

            try:
                campaign_results = _post_reporting(
                    http_client,
                    "campaign-values-reports/",
                    campaign_body,
                    headers,
                )
            except RuntimeError as exc:
                from core.pull_errors import ConnectorError  # noqa: PLC0415

                if isinstance(exc, ConnectorError):
                    raise
                logger.error(
                    "klaviyo_pull_catalog: erreur campagnes: %s (pull_id=%s)", exc, pull_id
                )
                campaign_results = []

            for result_item in campaign_results:
                for date_str in dates_range:
                    row = _parse_catalog_reporting_row(
                        result_item, date_str, "campaign", official_stats
                    )
                    if row:
                        all_raw_rows.append(row)

        # --- Flow path ---
        if wants_flow:
            flow_groupings = _build_catalog_groupings(selection or {}, "flow")
            flow_body: dict = {
                "data": {
                    "type": "flow-series-report",
                    "attributes": {
                        "statistics": official_stats,
                        "timeframe": {
                            "start": date_from,
                            "end": date_to,
                        },
                        "interval": "daily",
                        "conversion_metric_id": conversion_metric_id,
                    },
                }
            }
            if flow_groupings:
                flow_body["data"]["attributes"]["grouping_keys"] = flow_groupings

            try:
                flow_results = _post_reporting(
                    http_client,
                    "flow-series-reports/",
                    flow_body,
                    headers,
                )
            except RuntimeError as exc:
                from core.pull_errors import ConnectorError  # noqa: PLC0415

                if isinstance(exc, ConnectorError):
                    raise
                logger.error(
                    "klaviyo_pull_catalog: erreur flows: %s (pull_id=%s)", exc, pull_id
                )
                flow_results = []

            for result_item in flow_results:
                for date_str in dates_range:
                    row = _parse_catalog_reporting_row(
                        result_item, date_str, "flow", official_stats
                    )
                    if row:
                        all_raw_rows.append(row)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        all_raw_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    logger.info(
        "klaviyo_pull_catalog_completed: pull_id=%s row_count=%d wants_campaign=%s wants_flow=%s",
        pull_id, row_count, wants_campaign, wants_flow,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# transform() — mapping de champs canoniques via le manifest (AD-4)
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Mappe les champs source sur les noms canoniques via le manifest.

    AD-4 : mapping pilote par le manifest -- ne pas hardcoder les noms source.
    Les metriques attributed_conversions et attributed_revenue gardent leurs noms
    canoniques explicites (pas d'ambiguite avec 'conversions' des regies).

    NULL honnete (AD-9) : les valeurs None passent sans etre remplacees par 0.
    """
    _manifest_path = Path(__file__).parent / "manifest.json"
    _manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))

    rename_map: dict[str, str] = {}
    for src, val in _manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            rename_map[src] = val.get("canonical", src)
    rename_map.update(_manifest.get("canonical_dimension_mapping", {}))

    result: list[dict] = []
    for row in raw_rows:
        canonical_row: dict = {}
        for key, value in row.items():
            canonical_row[rename_map.get(key, key)] = value
        result.append(canonical_row)
    return result


# ---------------------------------------------------------------------------
# Enregistrement de la table raw aupres de core.verification (AD-2).
# Direction module -> core autorisee (seule la direction core -> module est
# interdite). Le nom de la table n'apparait jamais dans le code core (AD-2).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_klaviyo_daily", provider="klaviyo")
except Exception:
    pass  # best-effort ; verification logge un warning si le nom est manquant
