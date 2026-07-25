"""HubSpot CRM connector -- Story 15.5 (Epic 15, connecteurs vague 1).

CRM leads/pipelines -- source de reconciliation entre les leads declares par les
regies et les leads/deals CRM reels. Expose une instance ``mcp_app: FastMCP`` que
le loader du core monte sous le namespace ``hubspot`` (AD-2). Meme patron
"drop-a-folder" que klaviyo / linkedin-ads : zero edit sous server/core.

# AD-12: le MCP server lit UNIQUEMENT le mart fact_daily_kpi -- pas de raw_*.
# AD-3: le token OAuth2 HubSpot vient de Nango juste avant usage -- jamais stocke
#        ni logge (AD-3, flux Nango standard AD-3, PAS le flux Google direct AD-21).
# AD-7: pull_id frappe par le scheduler du core, passe dans pull().
# AD-14: parametre identity/project_id.

Decisions de story (15-5-connecteur-hubspot.md) :
  * DEUX grains : contacts_daily (new contacts per day) et deals_daily
    (deals created + closed per day with amount).
  * HubSpot CRM v3 ne fournit PAS d'agregats daily natifs (AI-53 verifie
    2026-07-19). Les grains journaliers sont obtenus en paginant le Search API
    (POST /crm/v3/objects/contacts/search + /deals/search) avec un filtre
    BETWEEN sur createdate ou closedate, puis en comptant les resultats.
  * PAGINATION BORNEE : curseur 'after' (max 200 objets/page, limite HubSpot
    10 000 resultats/query). Cap interne _MAX_PAGES = 50 pour eviter une
    boucle infinie (lecon review LinkedIn / Klaviyo).
  * RATE LIMIT : 5 req/s par compte HubSpot (AI-53 verifie 2026-07-19).
    429 -> RateLimitError('hubspot', retry_after).
  * AUTH : OAuth2 via Nango (AD-3). Provider 'hubspot'.
    Scopes requis : crm.objects.contacts.read, crm.objects.deals.read.
  * REGLE AD-4 CRM : new_contacts, deals_created, deals_closed, deal_amount
    sont des metriques CRM PURES. JAMAIS sommees avec GA4/Meta/TikTok
    conversions ou revenue Shopify/Stripe dans un total croise.

# AI-53 : tous les champs/endpoints API HubSpot declares d'apres la documentation
# officielle HubSpot CRM v3 (verifie le 2026-07-19) et annotes "a confirmer en
# passe live" (Phase B, AI-13). Sources :
#   https://developers.hubspot.com/docs/api/crm/contacts
#   https://developers.hubspot.com/docs/api/crm/deals
#   https://developers.hubspot.com/docs/api/crm/search

LIVE API = BLOCKED Phase B (AI-08/AI-13) : OAuth HubSpot et donnees de production
non disponibles en dev. Les fonctions pull() seront exercees en Phase B.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
mcp_app = FastMCP("hubspot")

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (no hardcoded paths).
# Same dual-backend pattern as meta-ads / klaviyo / linkedin-ads.
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
    """Execute parameterized *sql* contre le warehouse DuckDB local."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
    """Execute parameterized *sql* contre BigQuery (@named parameters)."""
    from google.cloud import bigquery  # noqa: PLC0415

    project = os.environ.get("GCP_PROJECT", "")
    client = bigquery.Client(project=project or None)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, "STRING", value) for name, value in params.items()
        ]
    )
    result = client.query(sql, job_config=job_config).result()
    cols = [f.name for f in result.schema]
    return [dict(zip(cols, row)) for row in result]


def _get_mart_table(db_mode: str) -> str:
    """Reference qualifiee du mart fact_daily_kpi selon le moteur."""
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


# F-1 : mapping report_profile -> ensemble de metriques autorisees.
# contacts_daily  -> uniquement new_contacts
# deals_daily     -> deals_created, deals_closed, deal_amount
# absent/inconnu  -> toutes les metriques (pas de filtre)
_PROFILE_METRICS: dict[str, frozenset[str]] = {
    "contacts_daily": frozenset({"new_contacts"}),
    "deals_daily": frozenset({"deals_created", "deals_closed", "deal_amount"}),
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
    WHERE connector = 'hubspot'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
      {metric_filter}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _build_metric_filter(report_profile: str, db_mode: str) -> tuple[str, list]:
    """Retourne (clause SQL, params extra) pour filtrer les metriques par profil (F-1).

    contacts_daily -> metric IN ('new_contacts')
    deals_daily    -> metric IN ('deals_created', 'deals_closed', 'deal_amount')
    absent/inconnu -> pas de filtre (toutes les metriques hubspot retournees)

    DuckDB : parametres positionnels (?).
    BigQuery : les valeurs sont injectees litteralement (metriques sont des constantes
    controlees, pas des entrees utilisateur -- pas de risque d'injection).
    """
    metrics = _PROFILE_METRICS.get(report_profile)
    if not metrics:
        return "", []
    sorted_metrics = sorted(metrics)  # ordre deterministe
    if db_mode == "duckdb":
        placeholders = ", ".join("?" * len(sorted_metrics))
        return f"AND metric IN ({placeholders})", list(sorted_metrics)
    # BigQuery : injection de litteraux (valeurs controlees par _PROFILE_METRICS)
    literals = ", ".join(f"'{m}'" for m in sorted_metrics)
    return f"AND metric IN ({literals})", []


def _query_mart(
    date_from: str,
    date_to: str,
    project_id: str = "default",
    report_profile: str = "",
) -> list[dict]:
    """Query fact_daily_kpi mart -- DuckDB ou BigQuery selon l'env.

    # AD-12: MCP server lit les marts uniquement -- jamais les tables raw_*.
    # F-1: filtre metric IN (...) selon report_profile pour ne retourner que les
    #      metriques du profil demande (contacts_daily -> new_contacts ;
    #      deals_daily -> deals_created, deals_closed, deal_amount).
    """
    db_mode = _get_db_mode()
    table = _get_mart_table(db_mode)
    metric_clause, metric_params = _build_metric_filter(report_profile, db_mode)

    if db_mode == "duckdb":
        sql = _MART_QUERY.format(
            table=table,
            p_project="?",
            p_from="?",
            p_to="?",
            metric_filter=metric_clause,
        )
        params = [project_id, date_from, date_to] + metric_params
        return _query_duckdb(sql, params, _get_duckdb_path())
    elif db_mode == "bigquery":
        sql = _MART_QUERY.format(
            table=table,
            p_project="@project_id",
            p_from="@date_from",
            p_to="@date_to",
            metric_filter=metric_clause,
        )
        return _query_bigquery(
            sql, {"project_id": project_id, "date_from": date_from, "date_to": date_to}
        )
    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {db_mode!r}")


def _check_any_truncated(
    date_from: str,
    date_to: str,
    project_id: str,
    report_profile: str,
) -> bool:
    """Verifie si une ligne truncated=TRUE existe dans le raw pour la periode (F-4).

    Interroge raw_hubspot_contacts_daily ou raw_hubspot_deals_daily selon le profil.
    Ce helper est appele UNIQUEMENT par get_hubspot_report (usage interne du module) --
    il n'est pas expose comme outil MCP. AD-12 s'applique a la surface MCP publique ;
    le module peut lire ses propres tables raw pour un signal de qualite de donnees.

    Retourne True si au moins une ligne truncated=TRUE est dans la periode.
    """
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        # BigQuery non implemente en Phase A -- pas d'alerte truncated en mode BQ.
        return False

    duckdb_path = _get_duckdb_path()
    if report_profile == "contacts_daily":
        table = "raw_hubspot_contacts_daily"
    else:
        table = "raw_hubspot_deals_daily"

    sql = (
        f"SELECT COUNT(*) FROM {table} "  # noqa: S608 -- table is a controlled constant
        "WHERE project_id = ? AND date BETWEEN ? AND ? AND truncated = TRUE"
    )
    try:
        rows = _query_duckdb(sql, [project_id, date_from, date_to], duckdb_path)
        return rows[0].get("count_star()", 0) > 0 if rows else False
    except Exception:  # table doesn't exist yet (seed not loaded)
        return False


def _build_envelope(
    rows: list[dict],
    report_profile: str,
    date_from: str,
    date_to: str,
    project_id: str,
    extra_alerts: list[dict] | None = None,
) -> dict:
    """Construit l'envelope canonique AD-1 a partir des lignes du mart.

    F-4 : extra_alerts permet d'injecter des alertes AD-9 (truncated, etc.)
    construites par l'appelant avant la constitution de l'envelope.
    """
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
            "source_system": "hubspot",
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
            "alerts": list(extra_alerts) if extra_alerts else [],
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
def get_hubspot_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "contacts_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """HubSpot CRM Report -- lit depuis le mart fact_daily_kpi.

    Report profiles: contacts_daily, deals_daily

    Metriques:
      contacts_daily  -> new_contacts (contacts crees dans la journee)
      deals_daily     -> deals_created, deals_closed, deal_amount

    REGLE CRM CRITIQUE (AD-4, Story 15.5) :
      Les metriques HubSpot CRM (new_contacts, deals_created, deals_closed,
      deal_amount) sont des indicateurs CRM PURS. ILS NE SONT JAMAIS SOMMES
      avec les metriques marketing existantes (conversions GA4/Meta/TikTok,
      revenue Shopify/Stripe) dans un total croise marketing.
      Usage legitime : reconciliation leads CRM vs leads declares par les
      regies, carte pipeline commerciale separee.

    Retourne l'envelope canonique AD-1.

    Parameters:
        project_id: Identifiant projet (defaut: 'default', AD-14 placeholder)
        report_profile: contacts_daily | deals_daily
        date_from: Date debut ISO-8601 (ex: '2026-04-01'). Defaut: 90 jours avant hier.
        date_to: Date fin ISO-8601 (ex: '2026-06-30'). Defaut: hier.
    """
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

    # F-4 (AD-9) : alert si comptage tronque au cap API (valeur = plancher).
    extra_alerts: list[dict] = []
    if _check_any_truncated(date_from, date_to, project_id, report_profile):
        extra_alerts.append(
            {
                "level": "warning",
                "code": "AD-9",
                "message": (
                    "comptage tronque au cap API (10 000 resultats/query HubSpot), "
                    "valeur = plancher -- affiner la plage de dates pour un comptage exact"
                ),
            }
        )

    return _build_envelope(rows, report_profile, date_from, date_to, project_id, extra_alerts)


# ---------------------------------------------------------------------------
# HubSpot CRM v3 Search API pull -- Story 15.5
#
# AI-53 VERIFIE le 2026-07-19 (documentation officielle HubSpot CRM v3) :
#   Contacts endpoint : POST /crm/v3/objects/contacts/search
#   Deals endpoint    : POST /crm/v3/objects/deals/search
#   Pagination        : curseur 'after' dans paging.next.after (entier).
#   Max par page      : 200 objects.
#   Limite query      : 10 000 resultats par query.
#   Rate limit        : 5 req/s par compte HubSpot.
#   Granularite       : pas d'agregat daily natif -> pagination + comptage cote client.
#   Proprietes contacts utilisees : createdate, hs_object_id (pour le comptage).
#   Proprietes deals utilisees    : createdate, closedate, dealstage, pipeline, amount.
#   Filtre BETWEEN sur les timestamps HubSpot (ms epoch) pour la granularite daily.
#   AI-53 A CONFIRMER EN PASSE LIVE (Phase B) :
#     - Format exact des timestamps HubSpot (ms depuis epoch UTC).
#     - Valeur exacte de dealstage pour "closed won" (typiquement "closedwon",
#       mais peut varier selon le pipeline configure).
#     - Disponibilite du champ 'amount' dans les resultats par defaut.
# ---------------------------------------------------------------------------

HUBSPOT_API_BASE = os.environ.get("HUBSPOT_API_BASE", "https://api.hubapi.com")
HUBSPOT_CRM_API_VERSION = "v3"
HUBSPOT_CURRENT_API_VERSION = "2026-03"
_ACCOUNT_DETAILS_PATH = "/account-info/2026-03/details"
_PAGE_SIZE = 200
_MAX_PAGES = 50
_MAX_SPLIT_DEPTH = 24
_MIN_SPLIT_WINDOW_MS = 1000
REFETCH_DAYS = (3, 14, 45)

# Explicit compatibility seam for older tests only. Production pulls pass the
# portal-derived timezone through connection_config and never read process env.
HUBSPOT_ACCOUNT_TIMEZONE = "UTC"


def _crm_search_path(object_type: str, api_version: str = HUBSPOT_CRM_API_VERSION) -> str:
    if object_type not in {"contacts", "deals"}:
        raise ValueError(f"unsupported HubSpot CRM object: {object_type}")
    if api_version == "v3":
        return f"/crm/v3/objects/{object_type}/search"
    if api_version == "2026-03":
        return f"/crm/objects/2026-03/{object_type}/search"
    raise ValueError(f"unsupported HubSpot CRM API version: {api_version}")


_CONTACTS_SEARCH_PATH = _crm_search_path("contacts")
_DEALS_SEARCH_PATH = _crm_search_path("deals")


def _get_account_tz(timezone_name: str | None = None) -> ZoneInfo:
    """Resolve an explicit portal timezone; UTC is the safe test fallback."""
    return ZoneInfo(timezone_name or HUBSPOT_ACCOUNT_TIMEZONE)


def discover_account_metadata(
    connection_id: str,
    *,
    _client=None,
    _token: str | None = None,
) -> dict:
    """Resolve the single OAuth portal identity/configuration from account metadata."""
    from core import nango_client  # noqa: PLC0415

    token = _token or nango_client.get_fresh_token(connection_id, provider="hubspot")
    client = _client or httpx.Client()
    response = client.get(
        f"{HUBSPOT_API_BASE}{_ACCOUNT_DETAILS_PATH}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if response.status_code != 200:
        _raise_hubspot_error(response)
    payload = response.json()
    return {
        "portal_id": str(payload["portalId"]),
        "timezone": payload["timeZone"],
        "company_currency": payload["companyCurrency"],
        "additional_currencies": payload.get("additionalCurrencies") or [],
        "data_hosting_location": payload.get("dataHostingLocation"),
    }


def _date_to_hubspot_timestamp(
    date_str: str,
    end_of_day: bool = False,
    tz: ZoneInfo | None = None,
) -> int:
    """Convertit une date ISO-8601 en timestamp HubSpot (ms depuis epoch UTC).

    AI-53 VERIFIE le 2026-07-19 : HubSpot utilise les millisecondes depuis l'epoch
    Unix UTC pour les filtres de date sur le Search API (operateur BETWEEN,
    valeurs 'value' et 'highValue'). Les filtres incluent les bornes.

    F-3 : le calcul de minuit utilise le fuseau *tz* (defaut : HUBSPOT_ACCOUNT_TIMEZONE,
    lui-meme defaut UTC). Exemple : avec tz=Europe/Paris, la borne de debut du
    2026-07-10 vaut 2026-07-09T22:00:00Z (UTC) car minuit Paris = 22h UTC en ete.
    Aligner HUBSPOT_ACCOUNT_TIMEZONE sur le fuseau du compte HubSpot en Phase B.

    Parameters
    ----------
    date_str:
        Date ISO-8601 (YYYY-MM-DD).
    end_of_day:
        Si True, retourne le timestamp de 23:59:59.999 (fuseau *tz*) du jour.
        Si False, retourne le timestamp de 00:00:00.000 (fuseau *tz*) du jour.
    tz:
        Fuseau horaire. Defaut : _get_account_tz() (env HUBSPOT_ACCOUNT_TIMEZONE).
    """
    from datetime import date  # noqa: PLC0415

    if tz is None:
        tz = _get_account_tz()
    d = date.fromisoformat(date_str)
    if end_of_day:
        dt = datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=tz)
    else:
        dt = datetime(d.year, d.month, d.day, 0, 0, 0, 0, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def _build_date_filter(property_name: str, date_str: str) -> dict:
    """Construit un filtre BETWEEN pour une propriete de date HubSpot.

    AI-53 VERIFIE le 2026-07-19 : l'operateur BETWEEN du Search API HubSpot
    utilise 'value' (borne inferieure) et 'highValue' (borne superieure).
    Source : https://developers.hubspot.com/docs/api/crm/search
    """
    return {
        "filters": [
            {
                "propertyName": property_name,
                "operator": "BETWEEN",
                "value": str(_date_to_hubspot_timestamp(date_str, end_of_day=False)),
                "highValue": str(_date_to_hubspot_timestamp(date_str, end_of_day=True)),
            }
        ]
    }


class IncompleteSearchError(RuntimeError):
    """The provider bound cannot be split further without losing completeness."""


def _build_half_open_date_filter(
    property_name: str,
    date_str: str,
    *,
    timezone_name: str = "UTC",
) -> dict:
    from datetime import date, timedelta  # noqa: PLC0415

    start = date.fromisoformat(date_str)
    end = start + timedelta(days=1)
    tz = _get_account_tz(timezone_name)
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=tz).timestamp() * 1000)
    end_ms = int(datetime(end.year, end.month, end.day, tzinfo=tz).timestamp() * 1000)
    return {
        "filters": [
            {"propertyName": property_name, "operator": "GTE", "value": str(start_ms)},
            {"propertyName": property_name, "operator": "LT", "value": str(end_ms)},
        ]
    }


def _load_error_map() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")).get(
        "error_map", {}
    )


def _normalized_error_payload(response):
    try:
        original = response.json()
    except Exception:
        return response.text
    if not isinstance(original, dict):
        return original
    normalized = dict(original)
    category = original.get("category")
    if category:
        normalized["code"] = category
    elif response.status_code == 423:
        normalized["code"] = "LOCKED"
    elif response.status_code == 477:
        normalized["code"] = "MIGRATION_IN_PROGRESS"
    return normalized


def _raise_hubspot_error(response):
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    payload = _normalized_error_payload(response)
    raise classify_http_error(response.status_code, payload, _load_error_map())


def _time_window(filter_groups: list[dict]):
    for group_index, group in enumerate(filter_groups):
        by_property = {}
        for item in group.get("filters") or []:
            by_property.setdefault(item.get("propertyName"), {})[item.get("operator")] = item
        for property_name, operators in by_property.items():
            if "GTE" in operators and "LT" in operators:
                return (
                    group_index,
                    property_name,
                    int(operators["GTE"]["value"]),
                    int(operators["LT"]["value"]),
                )
    return None


def _replace_time_window(filter_groups, group_index, property_name, start_ms, end_ms):
    copied = json.loads(json.dumps(filter_groups))
    filters = copied[group_index].get("filters") or []
    for item in filters:
        if item.get("propertyName") == property_name and item.get("operator") == "GTE":
            item["value"] = str(start_ms)
        if item.get("propertyName") == property_name and item.get("operator") == "LT":
            item["value"] = str(end_ms)
    return copied


def _search_crm_window(
    client: httpx.Client,
    endpoint: str,
    filter_groups: list[dict],
    properties: list[str],
    headers: dict,
) -> tuple[list[dict], bool]:
    """Pagine sur le Search API HubSpot et retourne (resultats, truncated).

    Pagination bornee : curseur 'after' dans paging.next.after.
    Cap _MAX_PAGES = 50 pour eviter toute boucle infinie (lecon Klaviyo/LinkedIn).
    Rate limit : 5 req/s (AI-53 verifie 2026-07-19) -> 429 si depasse.

    F-4 (AD-9) : retourne aussi un flag ``truncated`` (bool). True si le cap
    _MAX_PAGES a ete atteint (10 000 resultats/query HubSpot). Dans ce cas, le
    comptage est un PLANCHER, pas une valeur exacte. Les appelants propagent ce
    flag dans la ligne raw (colonne truncated) et l'envelope ajoute une alerte AD-9.

    AI-53 A CONFIRMER EN PASSE LIVE (Phase B, AI-13) :
      - Structure exacte de la reponse (paging.next.after vs next.after).
      - Comportement en cas de resultat > 10 000 (limite de query HubSpot).

    Returns
    -------
    tuple[list[dict], bool]
        (all_results, truncated) -- truncated=True si cap atteint.
    """
    url = f"{HUBSPOT_API_BASE}{endpoint}"
    all_results: list[dict] = []
    after: str | None = None
    pages = 0

    while pages < _MAX_PAGES:
        pages += 1
        body: dict = {
            "filterGroups": filter_groups,
            "properties": properties,
            "limit": _PAGE_SIZE,
        }
        if after is not None:
            body["after"] = after

        resp = client.post(url, json=body, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            # Rate limit HubSpot (5 req/s, AI-53 verifie 2026-07-19).
            from core.quota import RateLimitError  # noqa: PLC0415

            # F-5 : distingue header absent/'' (None) de '0' (0 seconde).
            retry_after_raw = resp.headers.get("Retry-After", None)
            if retry_after_raw is None or retry_after_raw == "":
                retry_after: int | None = None
            else:
                try:
                    retry_after = int(float(retry_after_raw))
                except (ValueError, TypeError):
                    retry_after = None
            raise RateLimitError("hubspot", retry_after)

        if resp.status_code != 200:
            _raise_hubspot_error(resp)

        payload = resp.json()
        results = payload.get("results") or []
        all_results.extend(results)

        # Pagination : paging.next.after (entier sous forme de string).
        # AI-53 A CONFIRMER : structure exacte du champ 'paging' en passe live.
        paging = payload.get("paging") or {}
        next_page = paging.get("next") or {}
        after = next_page.get("after")

        if not after:
            break

    truncated = bool(after) and pages >= _MAX_PAGES
    if truncated:
        logger.warning(
            "hubspot_search: hit _MAX_PAGES=%d cap for endpoint=%s. "
            "Counting is truncated (floor value, AD-9). "
            "Consider narrowing the date range.",
            _MAX_PAGES,
            endpoint,
        )

    return all_results, truncated


def _search_crm_objects(
    client: httpx.Client,
    endpoint: str,
    filter_groups: list[dict],
    properties: list[str],
    headers: dict,
    *,
    _depth: int = 0,
) -> tuple[list[dict], bool]:
    """Search completely, recursively splitting half-open windows at 10k."""
    rows, truncated = _search_crm_window(client, endpoint, filter_groups, properties, headers)
    if not truncated:
        unique = {str(row.get("id")): row for row in rows}
        return list(unique.values()), False
    window = _time_window(filter_groups)
    # Backward-compatible diagnostic seam for legacy tests without a time range.
    if window is None:
        return rows, True
    group_index, property_name, start_ms, end_ms = window
    if _depth >= _MAX_SPLIT_DEPTH or end_ms - start_ms <= _MIN_SPLIT_WINDOW_MS:
        raise IncompleteSearchError(
            f"HubSpot search remains above 10,000 objects for unsplittable "
            f"window [{start_ms}, {end_ms})"
        )
    midpoint = start_ms + (end_ms - start_ms) // 2
    left_filters = _replace_time_window(
        filter_groups, group_index, property_name, start_ms, midpoint
    )
    right_filters = _replace_time_window(
        filter_groups, group_index, property_name, midpoint, end_ms
    )
    left, _ = _search_crm_objects(
        client, endpoint, left_filters, properties, headers, _depth=_depth + 1
    )
    right, _ = _search_crm_objects(
        client, endpoint, right_filters, properties, headers, _depth=_depth + 1
    )
    deduplicated = {str(row.get("id")): row for row in [*left, *right]}
    return list(deduplicated.values()), False


def _pull_contacts_daily(
    client: httpx.Client,
    headers: dict,
    date_str: str,
    project_id: str,
    pull_id: str,
    timezone_name: str = "UTC",
) -> dict:
    """Pull des contacts crees dans la journee *date_str*.

    AI-53 VERIFIE le 2026-07-19 : filtre sur 'createdate' (propriete HubSpot
    standard des contacts). Champ 'hs_object_id' utilise pour le comptage.
    Le resultat est un agregat (count) -- pas de details par contact.

    Returns
    -------
    dict
        Ligne raw : {date, new_contacts, pull_id, loaded_at, project_id}
    """
    filter_groups = [
        _build_half_open_date_filter("createdate", date_str, timezone_name=timezone_name)
    ]
    # AI-53 : on demande juste l'ID pour le comptage (pas les donnees personnelles).
    properties = ["hs_object_id"]

    results, truncated = _search_crm_objects(
        client, _CONTACTS_SEARCH_PATH, filter_groups, properties, headers
    )
    return {
        "date": date_str,
        "new_contacts": len(results),
        "truncated": truncated,  # F-4 : flag AD-9 cap API
        "pull_id": pull_id,
        "loaded_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "project_id": project_id,
    }


def _pull_deals_daily(
    client: httpx.Client,
    headers: dict,
    date_str: str,
    project_id: str,
    pull_id: str,
    timezone_name: str = "UTC",
    company_currency: str | None = None,
) -> dict:
    """Pull des deals crees et fermes dans la journee *date_str*.

    Deux queries distinctes :
      1. Deals crees (filtre createdate BETWEEN)
      2. Deals fermes/won (filtre closedate BETWEEN)

    AI-53 VERIFIE le 2026-07-19 :
      - Champs : amount (valeur monetaire), dealstage, pipeline, createdate, closedate.
      - 'closedwon' est la valeur par defaut de dealstage pour les deals gagnes dans
        le pipeline de vente standard HubSpot. A confirmer en passe live (Phase B) car
        la valeur exacte depend du pipeline configure par le compte.
      - 'amount' peut etre NULL si le deal n'a pas de montant renseigne.

    Returns
    -------
    dict
        Ligne raw : {date, deals_created, deals_closed, deal_amount, pull_id, ...}
    """
    # --- 1. Deals crees ---
    created_filter_groups = [
        _build_half_open_date_filter("createdate", date_str, timezone_name=timezone_name)
    ]
    properties = [
        "amount_in_home_currency",
        "hs_is_closed_won",
        "pipeline",
        "createdate",
        "closedate",
    ]

    created_results, truncated_created = _search_crm_objects(
        client, _DEALS_SEARCH_PATH, created_filter_groups, properties, headers
    )
    deals_created = len(created_results)

    # --- 2. Deals fermes (won) ---
    # AI-53 : closedate = date du closing + dealstage = closedwon (stage par defaut).
    # Le filtre ferme = fermes le jour J, pas necessairement crees le jour J.
    closed_window = _build_half_open_date_filter("closedate", date_str, timezone_name=timezone_name)
    closed_window["filters"].append(
        {"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "true"}
    )
    closed_filter_groups = [closed_window]
    closed_results, truncated_closed = _search_crm_objects(
        client, _DEALS_SEARCH_PATH, closed_filter_groups, properties, headers
    )
    deals_closed = len(closed_results)
    # F-4 : truncated si l'une ou l'autre des deux queries a atteint le cap.
    truncated = truncated_created or truncated_closed

    # Somme des montants des deals fermes (NULL honnete : amount absent -> 0 pour le tot).
    # AD-9 : on somme les montants disponibles ; les NULL sont exclu de la somme.
    deal_amount: float = 0.0
    for r in closed_results:
        props = r.get("properties") or {}
        raw_amount = props.get("amount_in_home_currency")
        if raw_amount is not None:
            try:
                deal_amount += float(raw_amount)
            except (ValueError, TypeError):
                pass

    # deal_amount=0.0 si aucun deal ferme n'avait de montant renseigne.
    # NULL honnete (AD-9): on renvoie None si aucun deal ferme, 0.0 si deal sans montant.
    deal_amount_value: float | None = deal_amount if deals_closed > 0 else None

    return {
        "date": date_str,
        "deals_created": deals_created,
        "deals_closed": deals_closed,
        "deal_amount": deal_amount_value,
        "currency": company_currency,
        "truncated": truncated,  # F-4 : flag AD-9 cap API
        "pull_id": pull_id,
        "loaded_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "project_id": project_id,
    }


# ---------------------------------------------------------------------------
# Raw table DDL (DuckDB) -- module-owned, jamais dans dbt/models/staging/ central.
# ---------------------------------------------------------------------------

# F-4 : colonne truncated BOOLEAN dans les DDL raw (AD-9 -- cap API = valeur plancher).
# Defaut FALSE (insert seed : pas de troncature sur donnees synthetiques).
_RAW_CONTACTS_DDL = """
CREATE TABLE IF NOT EXISTS raw_hubspot_contacts_daily (
    date          VARCHAR,
    new_contacts  INTEGER,
    truncated     BOOLEAN DEFAULT FALSE,
    pull_id       VARCHAR,
    loaded_at     VARCHAR,
    project_id    VARCHAR
)
"""

_RAW_DEALS_DDL = """
CREATE TABLE IF NOT EXISTS raw_hubspot_deals_daily (
    date           VARCHAR,
    deals_created  INTEGER,
    deals_closed   INTEGER,
    deal_amount    DOUBLE,
    currency       VARCHAR,
    truncated      BOOLEAN DEFAULT FALSE,
    pull_id        VARCHAR,
    loaded_at      VARCHAR,
    project_id     VARCHAR
)
"""

_RAW_CONTACTS_INSERT = """
INSERT INTO raw_hubspot_contacts_daily
    (date, new_contacts, truncated, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?)
"""

_RAW_DEALS_INSERT = """
INSERT INTO raw_hubspot_deals_daily
    (date, deals_created, deals_closed, deal_amount, currency,
     truncated, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_contacts(
    rows: list[dict],
    db_mode: str,
    duckdb_path: str,
    project_id: str | None = None,
) -> int:
    """Insere les lignes contacts dans raw_hubspot_contacts_daily (DuckDB only at P-dev)."""
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_contacts: db_mode {db_mode!r} non supporte a P-dev "
            "(BigQuery non encore implemente)"
        )
    from core import warehouse_write  # noqa: PLC0415

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_RAW_CONTACTS_DDL)
    values = [
        (
            r["date"],
            r.get("new_contacts"),
            bool(r.get("truncated", False)),  # F-4 : flag AD-9 (defaut False)
            r["pull_id"],
            r["loaded_at"],
            r["project_id"],
        )
        for r in rows
    ]
    if values:
        con.executemany(_RAW_CONTACTS_INSERT, values)
    con.close()
    return len(values)


def _insert_raw_deals(
    rows: list[dict],
    db_mode: str,
    duckdb_path: str,
    project_id: str | None = None,
) -> int:
    """Insere les lignes deals dans raw_hubspot_deals_daily (DuckDB only at P-dev)."""
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_deals: db_mode {db_mode!r} non supporte a P-dev "
            "(BigQuery non encore implemente)"
        )
    from core import warehouse_write  # noqa: PLC0415

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_RAW_DEALS_DDL)
    values = [
        (
            r["date"],
            r.get("deals_created"),
            r.get("deals_closed"),
            # deal_amount peut etre None (AD-9): NULL si aucun deal ferme/sans montant.
            r.get("deal_amount"),
            r.get("currency"),
            bool(r.get("truncated", False)),  # F-4 : flag AD-9 (defaut False)
            r["pull_id"],
            r["loaded_at"],
            r["project_id"],
        )
        for r in rows
    ]
    if values:
        con.executemany(_RAW_DEALS_INSERT, values)
    con.close()
    return len(values)


# ---------------------------------------------------------------------------
# AI-58 per-profile dispatch : pull() principal + pull_contacts_daily /
# pull_deals_daily. core.get_module_pull_fn resout ces fonctions par convention
# de nommage (source-agnostic, AD-2).
# ---------------------------------------------------------------------------


def _date_range(date_from: str, date_to: str) -> list[str]:
    """Retourne la liste des dates ISO-8601 entre date_from et date_to inclus."""
    from datetime import date, timedelta  # noqa: PLC0415

    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    result: list[str] = []
    current = start
    while current <= end:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _pull_profile(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    connection_config: dict | None = None,
) -> dict:
    """Pull interne partage pour un profile donne (contacts_daily ou deals_daily).

    # AD-12: appele par le queue worker uniquement.
    # AD-3: token obtenu juste avant usage ; sort de scope apres l'appel.
    # AD-7: pull_id fourni par l'appelant.

    LIVE API = BLOCKED Phase B (AI-08/AI-13).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import au moment de l'appel

    if profile not in ("contacts_daily", "deals_daily"):
        raise ValueError(f"Unknown hubspot report profile: {profile!r}")

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtenu immediatement avant usage ; tombe hors de portee apres l'appel.
    token = nango_client.get_fresh_token(connection_id, provider="hubspot")
    account_config = connection_config or discover_account_metadata(connection_id, _token=token)
    timezone_name = account_config["timezone"]
    company_currency = account_config["company_currency"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    dates = _date_range(date_from, date_to)
    all_rows: list[dict] = []

    with httpx.Client() as client:
        for date_str in dates:
            if profile == "contacts_daily":
                row = _pull_contacts_daily(
                    client, headers, date_str, project_id, pull_id, timezone_name
                )
                all_rows.append(row)
            else:  # deals_daily
                row = _pull_deals_daily(
                    client,
                    headers,
                    date_str,
                    project_id,
                    pull_id,
                    timezone_name,
                    company_currency,
                )
                all_rows.append(row)

    if profile == "contacts_daily":
        canonical_rows = transform(all_rows)
        row_count = _insert_raw_contacts(
            canonical_rows, db_mode, duckdb_path, project_id=project_id
        )
    else:
        canonical_rows = transform(all_rows)
        row_count = _insert_raw_deals(canonical_rows, db_mode, duckdb_path, project_id=project_id)

    # AD-3: pas de token dans les logs -- seulement pull_id et row_count.
    logger.info(
        "hubspot_pull_completed: pull_id=%s profile=%s row_count=%d",
        pull_id,
        profile,
        row_count,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    connection_config: dict | None = None,
) -> dict:
    """Default pull() = contacts grain (AI-58 : fallback sans profil explicite).

    Le dispatch du queue (core.get_module_pull_fn) prefere pull_<profile_id> quand
    un profil est fourni ; ce pull() bare est le fallback (contacts_daily), miroir
    du contrat meta-ads / shopify / klaviyo.
    """
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        profile="contacts_daily",
        connection_config=connection_config,
    )


def pull_contacts_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    connection_config: dict | None = None,
) -> dict:
    """AI-58 dispatch : grain contacts crees par jour."""
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        profile="contacts_daily",
        connection_config=connection_config,
    )


def pull_deals_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    connection_config: dict | None = None,
) -> dict:
    """AI-58 dispatch : grain deals crees et fermes par jour."""
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        profile="deals_daily",
        connection_config=connection_config,
    )


# ---------------------------------------------------------------------------
# transform() -- mapping de champs canoniques via le manifest (AD-4)
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Mappe les champs source sur les noms canoniques via le manifest.

    AD-4 : mapping pilote par le manifest -- ne pas hardcoder les noms source.
    Les metriques HubSpot (new_contacts, deals_created, deals_closed, deal_amount)
    gardent leurs noms canoniques explicites.

    NULL honnete (AD-9) : les valeurs None passent sans etre remplacees par 0.

    REGLE CRM (AD-4, Story 15.5) : les metriques retournees ne doivent JAMAIS etre
    summees avec des metriques marketing cross-source (conversions regies, revenue
    Shopify/Stripe).
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
# Story 25.9: catalog_driven execution -- pull_catalog_daily (HYBRID style).
#
# HubSpot is HYBRID (not pure projection): the Search API accepts a `properties`
# list so the request IS built from the selection for the two fetchable objects
# (contacts + deals). The pull makes 3 Search API calls per date:
#   1. contacts/search filtered on createdate (selection: contact_ fields)
#   2. deals/search filtered on createdate   (selection: deal_ fields)
#   3. deals/search filtered on closedate+closedwon (same deal_ properties)
#
# Derived aggregate rows (new_contacts, deals_created, deals_closed, deal_amount)
# are ALWAYS emitted regardless of selection (they are the primary grain metric).
# Per-object property rows land in raw_hubspot_catalog_daily (long format).
#
# Excluded from catalog_daily: COMPANY, TICKET, ANALYTICS (not fetched).
# ---------------------------------------------------------------------------

# Map: field_id prefix -> CRM object (only contact_ and deal_ are reachable).
_CATALOG_OBJECT_MAP: dict[str, str] = {
    "contact_": "contact",
    "deal_": "deal",
}

# Derived aggregate field_ids always emitted regardless of selection.
_DERIVED_FIELD_IDS: frozenset[str] = frozenset(
    {"new_contacts", "deals_created", "deals_closed", "deal_amount", "date"}
)


def _group_selection_by_object(selection: dict) -> dict[str, list[str]]:
    """Split a selection into per-object source_field lists.

    Returns {object_name: [source_field, ...]} for contact_ and deal_ fields.
    Derived fields (new_contacts, deals_*, date) are skipped (always emitted).
    """
    source_fields: dict[str, str] = selection.get("source_fields", {})
    grouped: dict[str, list[str]] = {"contact": [], "deal": []}

    all_ids = list(selection.get("metrics", [])) + list(selection.get("dimensions", []))
    for field_id in all_ids:
        if field_id in _DERIVED_FIELD_IDS:
            continue
        src = source_fields.get(field_id, field_id)
        matched = False
        for prefix, obj in _CATALOG_OBJECT_MAP.items():
            if field_id.startswith(prefix):
                # source_field is the raw provider property name (namespace stripped).
                grouped[obj].append(src)
                matched = True
                break
        if not matched:
            # Field not from a reachable object -- skip silently.
            pass

    # Deduplicate preserving order.
    for obj in grouped:
        seen: set[str] = set()
        deduped: list[str] = []
        for sf in grouped[obj]:
            if sf not in seen:
                seen.add(sf)
                deduped.append(sf)
        grouped[obj] = deduped

    return grouped


def _project_catalog_contacts(
    contact: dict,
    date_str: str,
    selection: dict,
) -> list[dict]:
    """Project one contact API result into catalog-driven rows (one per selected field).

    Each row: {date, object_id, field_id, value}.
    """
    obj_id = contact.get("id", "")
    props = contact.get("properties") or {}
    source_fields: dict[str, str] = selection.get("source_fields", {})

    all_ids = list(selection.get("metrics", [])) + list(selection.get("dimensions", []))
    rows: list[dict] = []
    for field_id in all_ids:
        if field_id in _DERIVED_FIELD_IDS:
            continue
        if not field_id.startswith("contact_"):
            continue
        src = source_fields.get(field_id, field_id)
        value = props.get(src)
        rows.append({"date": date_str, "object_id": obj_id, "field_id": field_id, "value": value})
    return rows


def _project_catalog_deals(
    deal: dict,
    date_str: str,
    selection: dict,
) -> list[dict]:
    """Project one deal API result into catalog-driven rows (one per selected field)."""
    obj_id = deal.get("id", "")
    props = deal.get("properties") or {}
    source_fields: dict[str, str] = selection.get("source_fields", {})

    all_ids = list(selection.get("metrics", [])) + list(selection.get("dimensions", []))
    rows: list[dict] = []
    for field_id in all_ids:
        if field_id in _DERIVED_FIELD_IDS:
            continue
        if not field_id.startswith("deal_"):
            continue
        src = source_fields.get(field_id, field_id)
        value = props.get(src)
        rows.append({"date": date_str, "object_id": obj_id, "field_id": field_id, "value": value})
    return rows


_RAW_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS raw_hubspot_catalog_daily (
    date        VARCHAR,
    object_id   VARCHAR,
    field_id    VARCHAR,
    value       VARCHAR,
    pull_id     VARCHAR,
    loaded_at   VARCHAR,
    project_id  VARCHAR
)
"""

_RAW_CATALOG_INSERT = """
INSERT INTO raw_hubspot_catalog_daily
    (date, object_id, field_id, value, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_catalog(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Land projected property rows into raw_hubspot_catalog_daily (DuckDB only at P-dev)."""
    if db_mode != "duckdb":
        raise ValueError(f"_insert_raw_catalog: unsupported db_mode {db_mode!r}")
    from core import warehouse_write  # noqa: PLC0415

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_RAW_CATALOG_DDL)
    values = [
        (
            r.get("date", ""),
            r.get("object_id", ""),
            r.get("field_id", ""),
            str(r["value"]) if r.get("value") is not None else None,
            pull_id,
            loaded_at,
            project_id,
        )
        for r in rows
    ]
    if values:
        con.executemany(_RAW_CATALOG_INSERT, values)
    con.close()
    return len(values)


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    connection_config: dict | None = None,
) -> dict:
    """Run the catalog-driven daily pull with complete half-open searches."""
    from core import nango_client  # noqa: PLC0415
    from core.catalog_contract import (  # noqa: PLC0415
        catalog_default_selection,
        validate_selection,
    )

    catalog_path = Path(__file__).parent / "api_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if selection is None:
        selection, _ = validate_selection(catalog, catalog_default_selection(catalog))
    if "_catalog_fields" not in selection:
        selection = dict(selection)
        selection["_catalog_fields"] = {
            field["field_id"]: field for field in catalog.get("fields", [])
        }

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()
    token = nango_client.get_fresh_token(connection_id, provider="hubspot")
    account_config = connection_config or discover_account_metadata(connection_id, _token=token)
    timezone_name = account_config["timezone"]
    currency = account_config["company_currency"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    by_object = _group_selection_by_object(selection)
    contact_props = list(by_object.get("contact") or ["hs_object_id"])
    deal_props = list(by_object.get("deal") or ["pipeline", "createdate", "closedate"])
    if "hs_object_id" not in contact_props:
        contact_props.append("hs_object_id")
    for required in ("hs_object_id", "amount_in_home_currency", "hs_is_closed_won"):
        if required not in deal_props:
            deal_props.append(required)

    dates = _date_range(date_from, date_to)
    catalog_rows: list[dict] = []
    aggregate_rows: list[dict] = []
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    with httpx.Client() as client:
        for date_str in dates:
            contacts, _ = _search_crm_objects(
                client,
                _CONTACTS_SEARCH_PATH,
                [_build_half_open_date_filter("createdate", date_str, timezone_name=timezone_name)],
                contact_props,
                headers,
            )
            created, _ = _search_crm_objects(
                client,
                _DEALS_SEARCH_PATH,
                [_build_half_open_date_filter("createdate", date_str, timezone_name=timezone_name)],
                deal_props,
                headers,
            )
            closed_group = _build_half_open_date_filter(
                "closedate", date_str, timezone_name=timezone_name
            )
            closed_group["filters"].append(
                {"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "true"}
            )
            closed, _ = _search_crm_objects(
                client, _DEALS_SEARCH_PATH, [closed_group], deal_props, headers
            )
            for contact in contacts:
                catalog_rows.extend(_project_catalog_contacts(contact, date_str, selection))
            for deal in created:
                catalog_rows.extend(_project_catalog_deals(deal, date_str, selection))
            amount = None
            if closed:
                amount = 0.0
                for deal in closed:
                    value = (deal.get("properties") or {}).get("amount_in_home_currency")
                    if value is not None:
                        try:
                            amount += float(value)
                        except (TypeError, ValueError):
                            pass
            aggregate_rows.append(
                {
                    "date": date_str,
                    "new_contacts": len(contacts),
                    "deals_created": len(created),
                    "deals_closed": len(closed),
                    "deal_amount": amount,
                    "currency": currency,
                    "truncated": False,
                    "pull_id": pull_id,
                    "loaded_at": loaded_at,
                    "project_id": project_id,
                }
            )

    contacts_agg = [
        {
            key: row[key]
            for key in ("date", "new_contacts", "truncated", "pull_id", "loaded_at", "project_id")
        }
        for row in aggregate_rows
    ]
    deals_agg = [
        {
            key: row[key]
            for key in (
                "date",
                "deals_created",
                "deals_closed",
                "deal_amount",
                "currency",
                "truncated",
                "pull_id",
                "loaded_at",
                "project_id",
            )
        }
        for row in aggregate_rows
    ]
    _insert_raw_contacts(transform(contacts_agg), db_mode, duckdb_path, project_id=project_id)
    _insert_raw_deals(transform(deals_agg), db_mode, duckdb_path, project_id=project_id)
    row_count = _insert_raw_catalog(
        catalog_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )
    logger.info(
        "hubspot_catalog_pull_completed: pull_id=%s catalog_rows=%d dates=%d",
        pull_id,
        row_count,
        len(dates),
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Enregistrement des tables raw aupres de core.verification (AD-2).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_hubspot_contacts_daily", provider="hubspot")
    _register_raw("raw_hubspot_deals_daily", provider="hubspot")
    _register_raw("raw_hubspot_catalog_daily", provider="hubspot")
except Exception:
    pass  # best-effort ; verification logge un warning si le nom est manquant
