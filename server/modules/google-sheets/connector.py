"""Google Sheets connector -- Story 15.6 (Epic 15, connecteurs vague 1).

Saisie manuelle d'objectifs et de budgets via Google Sheets.
Expose une instance ``mcp_app: FastMCP`` que le loader du core monte sous
le namespace ``google-sheets`` (AD-2). Meme patron "drop-a-folder" que les
autres connecteurs : zero edit sous server/core, un bloc UNION additif dans
le mart.

# AD-12: le MCP server lit UNIQUEMENT le mart fact_daily_kpi -- pas de raw_*.
# AD-3: le token vient de get_fresh_token (facade 18.3) -- jamais stocke ni logge.
#        Pour les connexions google_direct, la facade route vers token_service (18.1/18.3).
#        Le module ne distingue PAS l'auth_path -- la facade s'en charge.
# AD-21 : auth Google direct (scope spreadsheets.readonly inclu dans GOOGLE_STACK_SCOPES).
#          Le module appelle get_fresh_token(connection_id) sans provider -- la facade route
#          automatiquement sur auth_path='google_direct'. PAS de Nango pour le stack Google.
# AD-7 : pull_id frappe par le scheduler du core, passe dans pull().
# AD-14 : parametre identity/project_id.
# AD-9 : colonne manquante = erreur explicite, jamais 0 invente.
#         Lignes non parsables comptees et loggees, jamais silencieuses.

SPECIFICITE SHEETS (mapping declaratif) :
  Une feuille Google Sheets est une source ARBITRAIRE -- le mapping
  colonnes->metriques/dimensions est declare PAR DATASTREAM (config), pas
  encode dans le code (lecon story). Le pull() recoit un
  ``column_mapping`` qui declare quelles colonnes de la feuille correspondent
  a quelles metriques/dimensions canoniques.

  Structure du column_mapping :
    {
      "date_column": "Date",             # Obligatoire
      "row_id_column": "Campagne",       # Optionnel (defaut: index de la ligne)
      "metric_columns": {
        "Budget": "budget_declared",     # {nom_colonne_sheet: metrique_canonique}
        "Objectif CA": "target_revenue"
      }
    }

AUTH (AD-21, Epic 18) :
  get_fresh_token(connection_id) -- la facade nango_client route sur
  auth_path='google_direct' (store 18.1 + refresh 18.2) automatiquement.
  Le module ne connait PAS l'auth_path : c'est le contrat source-agnostic AD-2.
  LIVE OAuth = BLOCKED Phase B (AI-08).

QUOTA (AI-53 verifie le 2026-07-19) :
  Google Sheets API v4 : 300 req/min par projet, 60 req/min par utilisateur.
  Source : https://developers.google.com/sheets/api/limits
  Une extraction = 1 appel batchGet. HTTP 429 -> RateLimitError('google-sheets', retry_after).
  Confirmed : pas de pagination -- la plage est retournee en une seule reponse.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# review-15-9 F-1 (BLOQUANT integration) : on REEXPORTE core.quota.RateLimitError plutot
# qu'une classe locale distincte. Une classe locale ne serait JAMAIS attrapee par le
# worker core (qui catch core.quota.RateLimitError) -> le 429 ne declencherait pas le
# breaker. Le worker attrape donc bien le 429 comme pour meta/tiktok/... (signature
# (platform, retry_after)). Import au niveau module pour que les tests visent la classe
# core via le symbole reexporte ici (module -> core est autorise par AD-2).
from core.quota import RateLimitError
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
mcp_app = FastMCP("google-sheets")

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (no hardcoded paths).
# Same dual-backend pattern as meta-ads / shopify / linkedin-ads.
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
    """Execute parameterized *sql* against the local DuckDB warehouse."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
    """Execute parameterized *sql* against BigQuery (@named parameters)."""
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
    """Fully-qualified mart table reference per engine."""
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'google-sheets'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    """Query fact_daily_kpi mart -- DuckDB or BigQuery depending on env.

    # AD-12: MCP server reads marts only -- never raw_* tables or CSV.
    """
    db_mode = _get_db_mode()
    table = _get_mart_table(db_mode)

    if db_mode == "duckdb":
        sql = _MART_QUERY.format(table=table, p_project="?", p_from="?", p_to="?")
        return _query_duckdb(sql, [project_id, date_from, date_to], _get_duckdb_path())
    elif db_mode == "bigquery":
        sql = _MART_QUERY.format(
            table=table, p_project="@project_id", p_from="@date_from", p_to="@date_to"
        )
        return _query_bigquery(
            sql, {"project_id": project_id, "date_from": date_from, "date_to": date_to}
        )
    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {db_mode!r}")


def _build_envelope(
    rows: list[dict],
    report_profile: str,
    date_from: str,
    date_to: str,
    project_id: str,
) -> dict:
    """Build the canonical AD-1 envelope from mart rows."""
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
            "source_system": "google-sheets",
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
def get_google_sheets_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "sheet_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Google Sheets Data Report -- reads from fact_daily_kpi mart.

    Report profiles: sheet_daily (objectifs / budgets declares en saisie manuelle).
    Metrics: budget_declared, target_revenue, target_conversions (all ADDITIVE, AD-4).
      Ces metriques sont des OBJECTIFS saisis manuellement -- pas des realisations.
      Elles ne participent PAS a cross_source_conversions (pas de dedup required).

    Returns the canonical AD-1 envelope via structuredContent.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of sheet_daily
        date_from: Start date ISO-8601 (e.g. '2026-04-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    from datetime import date, timedelta  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=90)).isoformat()

    try:
        rows = _query_mart(date_from, date_to, project_id)
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
# Google Sheets API v4 pull -- Story 15.6
#
# AI-53 verifie le 2026-07-19 (doc officielle Google Sheets API v4) :
#   Endpoint : GET https://sheets.googleapis.com/v4/spreadsheets/{id}/values:batchGet
#   Scopes : spreadsheets.readonly (inclus dans GOOGLE_STACK_SCOPES, Epic 18 AD-21)
#   Quota : 300 req/min par projet, 60 req/min par utilisateur.
#   Response shape : { "valueRanges": [{ "values": [[...header...], [row], ...] }] }
#   La premiere ligne est traitee comme en-tete (noms de colonnes).
#   PAS de pagination -- batchGet retourne toute la plage en une reponse.
#   Source : https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values
# ---------------------------------------------------------------------------

SHEETS_API_BASE = os.environ.get(
    "SHEETS_API_BASE", "https://sheets.googleapis.com/v4"
)

# Tokens de valeur vide/manquante normalises en None -> NULL.
_NOT_SET_TOKENS: frozenset[str] = frozenset(("", "-", "N/A", "n/a", "null", "none", "#N/A"))


# review-15-9 F-1 : RateLimitError est reexporte depuis core.quota (import en tete de
# fichier) -- PLUS de classe locale (voir le commentaire du bloc d'import). Le worker core
# attrape ainsi le 429 de ce module comme celui des autres connecteurs.


# ---------------------------------------------------------------------------
# Column mapping validation
# ---------------------------------------------------------------------------


class ColumnMappingError(Exception):
    """Raised when the declared column_mapping references a column absent in the sheet.

    AD-9: missing column = explicit error, NEVER a silent zero.
    """


def _validate_column_mapping(
    headers: list[str],
    date_column: str,
    metric_columns: dict[str, str],
    row_id_column: str | None,
) -> None:
    """Validate that all declared columns exist in the sheet headers.

    Raises ColumnMappingError with a descriptive message for every missing column.
    AD-9: a missing column is an explicit error -- NEVER a silent 0.
    """
    missing: list[str] = []
    if date_column not in headers:
        missing.append(f"date_column={date_column!r}")
    for col in metric_columns:
        if col not in headers:
            missing.append(f"metric_column={col!r}")
    if row_id_column is not None and row_id_column not in headers:
        missing.append(f"row_id_column={row_id_column!r}")

    if missing:
        raise ColumnMappingError(
            f"google-sheets: colonnes declarees absentes de la feuille "
            f"(AD-9 -- jamais 0 invente) : {', '.join(missing)}. "
            f"Colonnes disponibles : {headers!r}"
        )


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------


def _parse_sheet_row(
    row: list[str],
    headers: list[str],
    date_column: str,
    metric_columns: dict[str, str],
    row_id_column: str | None,
    row_index: int,
) -> dict | None:
    """Parse one data row from the sheet into the canonical raw record shape.

    Returns a dict with keys : date, sheet_row_id, + one key per canonical metric.
    Returns None if the date cell is empty/invalid (row is skipped + logged).
    Metric cells that cannot be parsed as float are set to None (NULL -- AD-9 honest)
    and counted in the returned unparsable_metrics count via the _parse_warnings key.

    AI-54: le shape retourne ici EST le shape canonique que le staging dbt attend.
    La fixture golden est generee en appelant ce parseur sur des payloads API reels.
    """
    # Build a dict from headers -> values (short rows padded with "").
    cell: dict[str, str] = {
        h: (row[i] if i < len(row) else "")
        for i, h in enumerate(headers)
    }

    # Date column.
    raw_date = cell.get(date_column, "").strip()
    if not raw_date or raw_date in _NOT_SET_TOKENS:
        logger.debug(
            '{"event": "google_sheets_skip_row", "reason": "empty_date", "row_index": %d}',
            row_index,
        )
        return None

    # row_id : declared column value or row index.
    if row_id_column is not None:
        row_id = cell.get(row_id_column, "").strip() or f"row_{row_index}"
    else:
        row_id = f"row_{row_index}"

    # Metric columns : parse as float, set None if unparsable.
    parsed: dict[str, Any] = {
        "date": raw_date,
        "sheet_row_id": row_id,
        "_parse_warnings": [],
    }

    for col, canonical_metric in metric_columns.items():
        raw_val = cell.get(col, "").strip()
        if not raw_val or raw_val in _NOT_SET_TOKENS:
            parsed[canonical_metric] = None
        else:
            # Handle locale-formatted numbers.
            #
            # Heuristique documentee (F-4, review-15-6) :
            #   Le dernier separateur (point ou virgule) est le separateur decimal.
            #   Le separateur precedent est le separateur de milliers.
            #
            #   Cas 1 : "1.234,56"  -> derniere sep = ','  -> FR : retire '.',
            #           remplace ',' par '.' -> "1234.56" -> 1234.56
            #   Cas 2 : "1,234.56"  -> derniere sep = '.'  -> EN : retire ','
            #           -> "1234.56" -> 1234.56
            #   Cas 3 : "5 000,50"  -> seule virgule        -> FR : retire '.',
            #           remplace ',' par '.' -> "5000.50" -> 5000.50
            #   Cas 4 : "1234.56"   -> seul point           -> EN direct
            #
            # Ce format attendu est documente dans manifest.json
            # (column_mapping_schema._number_format).
            stripped = raw_val.replace("\xa0", "").replace(" ", "")
            has_dot = "." in stripped
            has_comma = "," in stripped
            if has_dot and has_comma:
                # Determine which comes last: that is the decimal separator.
                if stripped.rfind(",") > stripped.rfind("."):
                    # FR/EU format: last separator is ',' (decimal)
                    # '.' = thousands separator
                    cleaned = stripped.replace(".", "").replace(",", ".")
                else:
                    # EN format: last separator is '.' (decimal)
                    # ',' = thousands separator
                    cleaned = stripped.replace(",", "")
            elif has_comma:
                # Only comma present -> decimal separator (FR single-digit decimal)
                cleaned = stripped.replace(",", ".")
            else:
                # Only dot or no separator -> EN decimal or integer
                cleaned = stripped
            try:
                parsed[canonical_metric] = float(cleaned)
            except (ValueError, TypeError):
                logger.warning(
                    '{"event": "google_sheets_unparsable_metric", '
                    '"col": %s, "raw": %s, "row_index": %d}',
                    col,
                    raw_val[:50],  # truncate for safety -- no PII in logs
                    row_index,
                )
                parsed[canonical_metric] = None
                parsed["_parse_warnings"].append(
                    f"col={col!r} raw={raw_val!r} -> None (not a number)"
                )

    return parsed


def _parse_sheet_response(
    values: list[list[str]],
    column_mapping: dict,
) -> tuple[list[dict], int]:
    """Parse the full sheet values response into canonical raw records.

    Args:
        values: The 'values' array from the Sheets API response
                (list of rows, first row = headers).
        column_mapping: {date_column, row_id_column?, metric_columns}.

    Returns:
        (records, unparsable_count) where records is a list of parsed dicts
        and unparsable_count is the number of rows with parse warnings.

    AD-9: lignes non parsables comptees et loggees -- jamais silencieuses.
    """
    if not values or len(values) < 2:
        # No data rows (only header or empty sheet).
        logger.info(
            '{"event": "google_sheets_empty_sheet", "rows": %d}',
            len(values) if values else 0,
        )
        return [], 0

    headers = [str(h).strip() for h in values[0]]
    date_column = column_mapping["date_column"]
    metric_columns = column_mapping.get("metric_columns", {})
    row_id_column = column_mapping.get("row_id_column")

    # Validate columns FIRST (AD-9: explicit error if column missing).
    _validate_column_mapping(headers, date_column, metric_columns, row_id_column)

    records: list[dict] = []
    unparsable_count = 0

    for i, row in enumerate(values[1:], start=1):  # skip header
        parsed = _parse_sheet_row(
            row, headers, date_column, metric_columns, row_id_column, i
        )
        if parsed is None:
            continue

        warnings = parsed.pop("_parse_warnings", [])
        if warnings:
            unparsable_count += 1

        records.append(parsed)

    if unparsable_count > 0:
        logger.warning(
            '{"event": "google_sheets_parse_summary", '
            '"unparsable_metric_rows": %d, "total_data_rows": %d}',
            unparsable_count,
            len(values) - 1,
        )

    return records, unparsable_count


# ---------------------------------------------------------------------------
# Raw table DDL (DuckDB) -- module-owned.
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_google_sheets_daily (
    date                VARCHAR,
    sheet_row_id        VARCHAR,
    budget_declared     DOUBLE,
    target_revenue      DOUBLE,
    target_conversions  DOUBLE,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR,
    spreadsheet_id      VARCHAR,
    sheet_name          VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_google_sheets_daily
    (date, sheet_row_id, budget_declared, target_revenue, target_conversions,
     pull_id, loaded_at, project_id, spreadsheet_id, sheet_name)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows(
    records: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    spreadsheet_id: str,
    sheet_name: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical records into raw_google_sheets_daily (DuckDB only at P-dev)."""
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL)
        values = [
            (
                r.get("date", ""),
                r.get("sheet_row_id", ""),
                r.get("budget_declared"),
                r.get("target_revenue"),
                r.get("target_conversions"),
                pull_id,
                loaded_at,
                project_id,
                spreadsheet_id,
                sheet_name,
            )
            for r in records
        ]
        if values:
            con.executemany(_RAW_INSERT_SQL, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )


# ---------------------------------------------------------------------------
# Google Sheets API client
# ---------------------------------------------------------------------------


def _fetch_sheet_values(
    token: str,
    spreadsheet_id: str,
    sheet_range: str,
    *,
    timeout: float = 30.0,
) -> list[list[str]]:
    """Fetch values from a Google Sheet range via the Sheets API v4.

    AI-53 verifie le 2026-07-19 :
      GET {SHEETS_API_BASE}/spreadsheets/{id}/values/{range}
      Header : Authorization: Bearer {token}
      Response : { "range": "...", "majorDimension": "ROWS",
                   "values": [["header1", ...], ["val1", ...], ...] }
      HTTP 429 : rate limit -> RateLimitError('google-sheets', retry_after).

    AD-3 : le token est utilise uniquement dans le header Authorization.
           Il ne doit jamais apparaitre dans les logs, exceptions ou fixtures.
    """
    url = f"{SHEETS_API_BASE}/spreadsheets/{spreadsheet_id}/values/{sheet_range}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise TimeoutError(
            f"google-sheets: timeout fetching spreadsheet={spreadsheet_id!r} "
            f"range={sheet_range!r}"
        ) from exc

    if resp.status_code == 429:
        # AI-53 : HTTP 429 -> core.quota.RateLimitError(platform, retry_after) pour que
        # le worker core attrape le 429 et declenche le breaker (review-15-9 F-1).
        # Defaut 60s conserve quand l'en-tete Retry-After est absent.
        retry_after = int(resp.headers.get("Retry-After", "60"))
        raise RateLimitError("google-sheets", retry_after)

    if resp.status_code == 403:
        # Permission denied -- likely missing scope or revoked consent.
        raise PermissionError(
            f"google-sheets: HTTP 403 pour spreadsheet={spreadsheet_id!r}. "
            "Verifiez que le scope 'spreadsheets.readonly' est consenti "
            "(AD-21 : doit faire partie du GOOGLE_STACK_SCOPES). "
            "LIVE OAuth = BLOCKED Phase B (AI-08)."
        )

    if resp.status_code == 404:
        raise FileNotFoundError(
            f"google-sheets: spreadsheet introuvable (HTTP 404) : "
            f"spreadsheet_id={spreadsheet_id!r}, range={sheet_range!r}. "
            "Verifiez l'ID et que le compte a acces a ce fichier."
        )

    resp.raise_for_status()

    payload = resp.json()
    values: list[list[str]] = payload.get("values", [])
    return values


# ---------------------------------------------------------------------------
# Pull function -- called by the queue worker (AD-7 / AD-12).
# ---------------------------------------------------------------------------


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str = "sheet_daily",
    spreadsheet_id: str = "",
    sheet_range: str = "",
    sheet_name: str = "",
    column_mapping: dict | None = None,
) -> dict:
    """Pull data from a Google Sheet and ingest into raw_google_sheets_daily.

    # AD-12: called by queue worker only -- pas d'appel API synchrone lors d'un
    #         appel d'outil LLM.
    # AD-3: le token est utilise immediatement comme header Authorization, puis
    #        tombe hors de portee. JAMAIS stocke, logge, ni passe plus loin.
    # AD-21: la connexion google-sheets utilise auth_path='google_direct' -- la
    #         facade nango_client.get_fresh_token route automatiquement sur
    #         token_service (18.1/18.3). Le module n'a pas a connaitre l'auth_path.
    # AD-7: pull_id fourni par l'appelant ; cette fonction ne mint jamais le sien.
    # AD-9: colonne manquante = ColumnMappingError (jamais 0 invente).
    #        Lignes non parsables = comptees et loggees, incluses dans le resultat.

    LIVE API = BLOCKED Phase B (AI-08/AI-13) : OAuth Google direct et donnees
    reelles non disponibles en dev.

    Args:
        connection_id: Identifiant de connexion (lookup auth_path dans connection_ref).
        date_from: Date de debut ISO-8601 (incluse). Utilise pour filtrer les lignes
                   dont la colonne date est dans la plage.
        date_to: Date de fin ISO-8601 (incluse).
        project_id: Project identifier.
        pull_id: Pull ID fourni par le scheduler core (AD-7).
        profile: Report profile ('sheet_daily').
        spreadsheet_id: ID du Google Spreadsheet (extrait de l'URL Sheets).
        sheet_range: Plage a lire (ex: 'Feuille1!A:E', 'Budget 2026!A1:F100').
        sheet_name: Nom de la feuille pour la traceabilite dans le raw.
        column_mapping: Mapping colonnes->metriques (voir docstring du module).

    Returns:
        dict with keys: rows_inserted, unparsable_rows, pull_id, profile.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if not spreadsheet_id:
        raise ValueError(
            "google-sheets pull: spreadsheet_id est obligatoire "
            "(ID du Google Spreadsheet, extrait de l'URL)."
        )
    if not sheet_range:
        raise ValueError(
            "google-sheets pull: sheet_range est obligatoire "
            "(ex: 'Feuille1!A:E' ou 'Budget!A1:F100')."
        )
    if not column_mapping:
        raise ValueError(
            "google-sheets pull: column_mapping est obligatoire "
            "(mapping {date_column, metric_columns, ...})."
        )
    if "date_column" not in column_mapping:
        raise ValueError(
            "google-sheets pull: column_mapping.date_column est obligatoire."
        )
    if not column_mapping.get("metric_columns"):
        raise ValueError(
            "google-sheets pull: column_mapping.metric_columns est obligatoire "
            "et doit declarer au moins une metrique canonique."
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtenu immediatement avant usage ; tombe hors de portee apres l'appel.
    # AD-21 / 18.3: la facade route sur auth_path='google_direct' automatiquement.
    # Le module ne passe pas provider='google-sheets' : la facade est source-agnostique.
    token = nango_client.get_fresh_token(connection_id)

    # Fetch sheet values (AI-53 verifie 2026-07-19).
    values = _fetch_sheet_values(token, spreadsheet_id, sheet_range)

    # AD-9: parse + validation explicite des colonnes.
    records, unparsable_count = _parse_sheet_response(values, column_mapping)

    # Filtre sur la plage de dates du pull (les sheets peuvent contenir des lignes
    # hors de la fenetre demandee).
    filtered = [
        r for r in records
        if date_from <= r.get("date", "") <= date_to
    ]

    loaded_at = datetime.now(tz=timezone.utc).isoformat()

    rows_inserted = _insert_raw_rows(
        filtered,
        pull_id=pull_id,
        loaded_at=loaded_at,
        project_id=project_id,
        spreadsheet_id=spreadsheet_id,
        sheet_name=sheet_name or sheet_range,
        db_mode=db_mode,
        duckdb_path=duckdb_path,
    )

    logger.info(
        '{"event": "google_sheets_pull_complete", '
        '"profile": %s, "rows_fetched": %d, "rows_filtered": %d, '
        '"rows_inserted": %d, "unparsable_rows": %d, "pull_id": "[REDACTED]"}',
        profile,
        len(records),
        len(filtered),
        rows_inserted,
        unparsable_count,
    )

    return {
        "rows_inserted": rows_inserted,
        "unparsable_rows": unparsable_count,
        "pull_id": pull_id,
        "profile": profile,
    }


# ---------------------------------------------------------------------------
# transform() -- mapping canonique manifest-driven (Layer 4 conformance, AD-4).
#
# Contrat : transform(raw_rows) recoit des dicts au shape _parse_sheet_response()
# (cles : date, sheet_row_id, budget_declared, target_revenue, target_conversions)
# et retourne les memes dicts avec les cles renommees selon canonical_metric_mapping
# et canonical_dimension_mapping declares dans manifest.json.
#
# Pour Google Sheets les noms canoniques sont identiques aux noms source (mapping
# declare 1:1 dans le manifest). La fonction applique quand meme le manifest pour
# rester conforme au patron des modules pairs (klaviyo, hubspot).
#
# NULL honnete (AD-9) : les valeurs None passent sans etre remplacees par 0.
# ---------------------------------------------------------------------------

def transform(raw_rows: list[dict]) -> list[dict]:
    """Mappe les champs source sur les noms canoniques via le manifest (AD-4).

    Layer 4 conformance : le test_golden_pull.py charge golden_pull.json,
    appelle transform() et compare le resultat a expected_facts.json.

    Args:
        raw_rows: Lignes au shape _parse_sheet_response() output :
                  {date, sheet_row_id, budget_declared, target_revenue,
                   target_conversions}. Peut contenir des valeurs None (AD-9).

    Returns:
        Memes lignes avec cles renommees selon le manifest. Pour google-sheets
        le mapping est 1:1 -- les cles restent identiques.
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
# AI-58 : dispatcher pull_sheet_daily (profil unique -- pas de multi-grain).
# ---------------------------------------------------------------------------


def pull_sheet_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    spreadsheet_id: str = "",
    sheet_range: str = "",
    sheet_name: str = "",
    column_mapping: dict | None = None,
) -> dict:
    """Dispatcher AI-58 pour le profil sheet_daily.

    Appele par le scheduler core via dispatch('google-sheets', 'pull_sheet_daily', ...).
    """
    return pull(
        connection_id=connection_id,
        date_from=date_from,
        date_to=date_to,
        project_id=project_id,
        pull_id=pull_id,
        profile="sheet_daily",
        spreadsheet_id=spreadsheet_id,
        sheet_range=sheet_range,
        sheet_name=sheet_name,
        column_mapping=column_mapping,
    )
