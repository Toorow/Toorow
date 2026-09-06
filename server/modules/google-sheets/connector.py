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
      "date_column": "Date",             # Required
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

# Import au niveau module (et non paresseux comme les appels a `core` dans les
# fonctions) : les classes d'exception ci-dessous en HERITENT, donc il doit etre
# resolu au moment ou le fichier est lu.
from core import pull_errors

# review-15-9 F-1 (BLOQUANT integration) : on REEXPORTE core.quota.RateLimitError plutot
# qu'une classe locale distincte. Une classe locale ne serait JAMAIS attrapee par le
# worker core (qui catch core.quota.RateLimitError) -> le 429 ne declencherait pas le
# breaker. Le worker attrape donc bien le 429 comme pour meta/tiktok/... (signature
# (platform, retry_after)). Import au niveau module pour que les tests visent la classe
# core via le symbole reexporte ici (module -> core est autorise par AD-2).
from core.quota import RateLimitError
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance, kept as the conformance surface (AD-1 envelope,
# validated by server/tests/conformance/test_envelope.py). Since AD-42 the core
# no longer mounts it: execution uses the Datastream-parameterized core tools.
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


# ---------------------------------------------------------------------------
# Provider error refinements -- declared in manifest.json, never in core (AD-2).
# ---------------------------------------------------------------------------

_ERROR_MAP: dict[str, str] | None = None


def _load_error_map() -> dict[str, str]:
    """Return the manifest's ``error_map`` (status:code -> canonical class), cached.

    Keys are ``"<http_status>:<provider_code>"``; for Sheets the code is the
    ``error.errors[].reason`` string of the Google Workspace error envelope, or
    the ``error.status`` enum when the response carries no reason.
    ``core.pull_errors._extract_provider_codes`` offers both, most specific
    first, and never keys on ``error.code`` -- which merely repeats the HTTP
    status. The manifest ``_error_map_note`` names the published reference and
    says which entries change a verdict.
    """
    global _ERROR_MAP
    if _ERROR_MAP is None:
        manifest_path = Path(__file__).parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _ERROR_MAP = manifest.get("error_map") or {}
    return _ERROR_MAP


def _sheets_error(resp: httpx.Response):
    """The typed connector error a non-2xx Sheets response means.

    Reads the JSON body when there is one (the reason string lives there), falls
    back to the raw text, and hands both to the shared classifier together with
    the manifest map -- the documented consumption (server/modules/README.md,
    step 4). It classifies only; the caller decides what to raise, because two of
    the statuses carry a contract older than the taxonomy (see _raise_http_error).
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 -- an error page is evidence, not a parse target
        body = resp.text
    return pull_errors.classify_http_error(resp.status_code, body, _load_error_map())


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


def _get_mart_table(db_mode: str, project_id: str | None) -> str:
    """Fully-qualified mart table reference per engine."""
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(project_id)}fact_daily_kpi"
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
    table = _get_mart_table(db_mode, project_id)

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

# Ce que la description d'un classeur demande -- et surtout ce qu'elle ne demande
# pas. `includeGridData` est ABSENT : c'est lui qui rapatrierait les cellules, et
# decrire une feuille n'est pas la tirer (story 57.2).
_SHEET_METADATA_FIELD_MASK = (
    "properties.title,properties.locale,properties.timeZone,sheets.properties"
)

# Tokens de valeur vide/manquante normalises en None -> NULL.
_NOT_SET_TOKENS: frozenset[str] = frozenset(("", "-", "N/A", "n/a", "null", "none", "#N/A"))


# review-15-9 F-1 : RateLimitError est reexporte depuis core.quota (import en tete de
# fichier) -- PLUS de classe locale (voir le commentaire du bloc d'import). Le worker core
# attrape ainsi le 429 de ce module comme celui des autres connecteurs.


# ---------------------------------------------------------------------------
# Column mapping validation
# ---------------------------------------------------------------------------


class ColumnMappingError(pull_errors.InvalidRequestError):
    """Raised when the declared column_mapping references a column absent in the sheet.

    `invalid_request` : une colonne declaree qui a disparu de la feuille EST une
    derive, et c'est le seul evenement qui la signale. AD-9 : jamais un 0
    invente.
    """

    #: -> `Mapping` : le plan demande ce que la source ne rend plus
    #: (datastream-workbench-and-wizard.md:107). L'operateur a un endroit
    #: ou aller, contrairement a une derive de version d'API.
    user_action = pull_errors.REVIEW_MAPPING

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


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
    """Insert canonical records into raw_google_sheets_daily (DuckDB or BigQuery)."""
    if db_mode in ("duckdb", "bigquery"):
        # BOTH BACKENDS, ONE PATH. `open_raw_writer` resolves DuckDB or
        # BigQuery from TOOROW_DB_MODE itself, so this branch already covers
        # bigquery. An `elif db_mode == "bigquery"` used to sit below it,
        # unreachable because this test captures both modes -- dead code that
        # had quietly drifted to a different set of column names and would
        # have become live the day someone narrowed this condition.
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
        raise ValueError(f"_insert_raw_rows: unsupported db_mode {db_mode!r}")


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
        # The manifest error_map is consulted FIRST, and only a 403 it makes
        # RETRYABLE leaves as a typed error. Google returns an exhausted quota as a
        # 403 carrying a rate/daily-limit reason; raising PermissionError for that
        # made core/google_sheets_sync.py report "consent revoked" for a quota that
        # merely needed backing off. Every other 403 -- missing scope, withdrawn
        # consent -- keeps PermissionError, because that TYPE is the contract the
        # sync classifies on (_classify_adapter_error), not an implementation detail.
        error = _sheets_error(resp)
        if error.retryable:
            raise error
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

    if resp.status_code >= 400:
        # Every remaining non-2xx (401, 400, 5xx, ...) leaves as the canonical typed
        # error instead of httpx.HTTPStatusError, so the worker reads a class and a
        # retry policy rather than a status it has to re-interpret.
        raise _sheets_error(resp)

    resp.raise_for_status()

    payload = resp.json()
    values: list[list[str]] = payload.get("values", [])
    return values


def _fetch_sheet_metadata(
    token: str,
    spreadsheet_id: str,
    *,
    timeout: float = 30.0,
) -> dict:
    """Fetch the STRUCTURE of a spreadsheet -- tabs and grid bounds, no cell.

    `GET {SHEETS_API_BASE}/spreadsheets/{id}` (spreadsheets.get) with the field
    mask below and WITHOUT `includeGridData`. The response carries
    `sheets[].properties` -- `title`, `index`, `sheetType` and
    `gridProperties.{rowCount,columnCount,frozenRowCount}` -- and not one value.
    Asking for `includeGridData` is what would repatriate cells, so it is never
    sent: describing a sheet must not be a pull wearing another word.

    Story 57.2. This is the one call this module did not have; everything else
    the setup client needs is `_fetch_sheet_values` above. The error taxonomy is
    that function's, unchanged (429/403/404/timeout) and with no new class: the
    worker's breaker and the sync's classifier both match on these types.

    A CONFIRMER en passe live (Phase B) : le contrat du fournisseur (masque de
    champs accepte, `frozenRowCount` peuple) est sa moitie ; les tests hors
    ligne prouvent la notre -- ce qu'on demande, et ce qu'on ne demande pas.

    AD-3 : le token n'est utilise que dans le header Authorization.
    """
    url = f"{SHEETS_API_BASE}/spreadsheets/{spreadsheet_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {"fields": _SHEET_METADATA_FIELD_MASK}

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers, params=params)
    except httpx.TimeoutException as exc:
        raise TimeoutError(
            f"google-sheets: timeout reading spreadsheet metadata "
            f"spreadsheet={spreadsheet_id!r}"
        ) from exc

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "60"))
        raise RateLimitError("google-sheets", retry_after)

    if resp.status_code == 403:
        # Same rule as _fetch_sheet_values: a 403 the map makes retryable is a
        # spent quota, not a withdrawn consent.
        error = _sheets_error(resp)
        if error.retryable:
            raise error
        raise PermissionError(
            f"google-sheets: HTTP 403 pour spreadsheet={spreadsheet_id!r}. "
            "Verifiez que le scope 'spreadsheets.readonly' est consenti "
            "(AD-21 : doit faire partie du GOOGLE_STACK_SCOPES)."
        )

    if resp.status_code == 404:
        raise FileNotFoundError(
            f"google-sheets: spreadsheet introuvable (HTTP 404) : "
            f"spreadsheet_id={spreadsheet_id!r}. "
            "Verifiez l'ID et que le compte a acces a ce fichier."
        )

    if resp.status_code >= 400:
        raise _sheets_error(resp)

    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


class SheetsSetupReader:
    """Read-only sheet structure for Datastream setup (Story 57.2).

    Satisfies `inbound.adapters.datastream_setup.SheetsSetupClient`. It opens no
    second way into the Sheets API: the token comes from the same facade `pull`
    uses, the header row comes from `_fetch_sheet_values` above, and the only
    thing added is the metadata call the module did not have.

    WHAT IT READS, AND WHAT IT REFUSES TO READ. A header cell is the NAME of a
    column; a cell on row 2 or below is a VALUE. So exactly one value range is
    ever requested and it is bounded to one line -- `'{tab}'!1:1`. Two ranges,
    or a range like `A:Z`, would be a pull wearing the word "describe", and the
    offline test asserts the single bounded range for that reason.

    NO TYPE IS PRODUCED. A spreadsheet declares no schema, and reading values to
    guess one is the very pull this class refuses. The returned record therefore
    carries no `type` key at all -- the adapter states `unknown` and says why.
    """

    def __init__(self, connection_id: str) -> None:
        self._connection_id = connection_id

    def get_sheet_metadata(self, sheet_ref: str) -> dict:
        """Describe ONE tab of one workbook, named by `{spreadsheetId}!{tab}`.

        A reference with no `!` names a workbook, not a tab, and answering with
        the FIRST tab would be a choice nobody made -- a four-tab workbook would
        then describe four different schemas depending on order. It is reported
        as an incomplete designation instead, WITH the list of tabs the workbook
        carries, so the answer is a choice rather than a name to type from
        memory. Listing tabs reads titles, never a cell: the boundary is the
        same one, and no value range is requested on that path.
        """
        spreadsheet_id, separator, tab_title = str(sheet_ref).partition("!")
        spreadsheet_id = spreadsheet_id.strip()
        tab_title = tab_title.strip()
        if not spreadsheet_id:
            return {"tab_state": "unnamed", "headers": [], "tabs": []}

        from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

        token = nango_client.get_fresh_token(self._connection_id)
        metadata = _fetch_sheet_metadata(token, spreadsheet_id)
        properties = metadata.get("properties") or {}
        tabs = _addressable_tabs(metadata, spreadsheet_id)
        if not separator or not tab_title:
            return {
                "tab_state": "unnamed",
                "headers": [],
                "tabs": tabs,
                "locale": properties.get("locale"),
            }
        tab = _find_sheet_tab(metadata, tab_title)
        if tab is None:
            # The workbook WAS read, so access works; only the tab is missing --
            # and the ones it does carry are listed beside the refusal.
            return {
                "tab_state": "not_found",
                "headers": [],
                "tabs": tabs,
                "locale": properties.get("locale"),
                "tab_title": tab_title,
            }

        grid = tab.get("gridProperties") or {}
        frozen = _positive_int(grid.get("frozenRowCount"))
        # ONE range, ONE line. This is the whole boundary of the story.
        values = _fetch_sheet_values(token, spreadsheet_id, f"'{tab_title}'!1:1")
        header_row = [str(cell).strip() for cell in (values[0] if values else [])]
        headers = [cell for cell in header_row if cell]
        header_state, header_reason = _classify_header_row(header_row, frozen=frozen)
        return {
            "tab_state": "named",
            "tab_title": tab_title,
            "tabs": tabs,
            "headers": headers,
            "header_state": header_state,
            "header_reason": header_reason,
            "locale": properties.get("locale"),
            "row_count": _positive_int(grid.get("rowCount")),
            "column_count": _positive_int(grid.get("columnCount")),
            "frozen_row_count": frozen,
        }


def _addressable_tabs(metadata: dict, spreadsheet_id: str) -> list[dict]:
    """Every tab of the workbook, as something the wizard can be given.

    `object_ref` is the reference the next discovery would be run with -- the
    same `{spreadsheetId}!{tabTitle}` an operator would have typed -- and
    `label` is the tab's own title. Titles only: `spreadsheets.get` without
    `includeGridData` returns no cell, so listing the tabs takes nothing out of
    the workbook.

    A tab that is not a GRID (a chart sheet) carries no header row and is not
    offered: it would be a choice with nothing behind it.
    """
    tabs: list[dict] = []
    for sheet in metadata.get("sheets") or []:
        properties = (sheet or {}).get("properties") or {}
        title = str(properties.get("title") or "").strip()
        kind = str(properties.get("sheetType") or "GRID").strip().upper()
        if not title or kind != "GRID":
            continue
        tabs.append({"object_ref": f"{spreadsheet_id}!{title}", "label": title})
    return tabs


def _find_sheet_tab(metadata: dict, tab_title: str) -> dict | None:
    """The tab whose title matches, or None. Never `sheets[0]` as a fallback."""
    for sheet in metadata.get("sheets") or []:
        properties = (sheet or {}).get("properties") or {}
        if str(properties.get("title") or "") == tab_title:
            return properties
    return None


def _positive_int(value: object) -> int:
    """A count when the provider sent one, `0` when it sent nothing readable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def _classify_header_row(row: list[str], *, frozen: int) -> tuple[str, str]:
    """How much the header row is a FACT and how much it is an assumption.

    Three states, in decreasing order of honesty:

    * `declared` -- the tab freezes a header row. A frozen row is the sheet
      author's own declaration, and it is a PRESENTATION property, not a value.
    * `assumed` -- no frozen row, but row 1 reads complete and duplicate-free.
      The ordinary case, and the manifest has always said it in words: « La
      premiere ligne est supposee etre l'en-tete des colonnes ».
    * `uncertain` -- duplicates, empty cells, or an empty row. A sheet with no
      header is not a broken sheet: the reason is named and the rest of the
      evidence stays valid.

    The duplicate rule is the one `core.google_sheets_sync._validate_sheet_headers`
    already applies (its `duplicate_headers` half, `:210-218`). It is not imported
    here because a module never imports `core` at module scope (AD-2) and this
    function must stay pure; the loop below is that loop, and the error CODE is
    the same string on purpose.
    """
    filled = [cell for cell in row if cell]
    duplicates = sorted({cell for cell in filled if filled.count(cell) > 1})
    if duplicates:
        return "uncertain", "duplicate_headers"
    if not filled:
        return "uncertain", "header_row_empty"
    if len(filled) != len(row):
        return "uncertain", "header_row_incomplete"
    return ("declared", "") if frozen >= 1 else ("assumed", "")


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
            "google-sheets pull: spreadsheet_id is required "
            "(the Google Spreadsheet ID, taken from its URL)."
        )
    if not sheet_range:
        raise ValueError(
            "google-sheets pull: sheet_range is required "
            "(e.g. 'Sheet1!A:E' or 'Budget!A1:F100')."
        )
    if not column_mapping:
        raise ValueError(
            "google-sheets pull: column_mapping is required "
            "(mapping {date_column, metric_columns, ...})."
        )
    if "date_column" not in column_mapping:
        raise ValueError(
            "google-sheets pull: column_mapping.date_column is required."
        )
    if not column_mapping.get("metric_columns"):
        raise ValueError(
            "google-sheets pull: column_mapping.metric_columns is required "
            "and must declare at least one canonical metric."
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
        "row_count": rows_inserted,
        "unparsable_rows": unparsable_count,
        "pull_id": pull_id,
        "profile": profile,
        "date_from": date_from,
        "date_to": date_to,
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
