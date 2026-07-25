"""Integral Ad Science (IAS) connector.

IAS Signal / IAS Reporting API -- ad verification and media-quality measurement
(viewability, invalid traffic / IVT, brand safety & suitability). Exposes a
module-level ``mcp_app: FastMCP`` the core loader mounts under the ``ias``
namespace (AD-2).

AD-2: module name never hardcoded in core/.
AD-3: token obtained immediately before use, never stored or logged.
AD-7: pull_id minted by the caller (core scheduler), passed in here.
AD-12: MCP tool reads the fact_daily_kpi mart only -- never raw_* tables.
AI-03: ASCII-only stdout / log strings.

LIVE CONTRACT CAVEAT (AI-13, deferred): the IAS Reporting API official
reference is login-gated and not statically fetchable. The exact date-range
query-parameter names, the JSON response envelope, and the discovery endpoint
path are DECLARED assumptions here (marked ASSUMED below) and are proven by the
live ratification probe once a real IAS Signal account connects. The module
stays public_catalog.verification='blocked' until then. See ROLLOUT_NOTES.md.
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

# Module-level FastMCP instance -- core loader mounts this under the 'ias' namespace.
mcp_app = FastMCP("ias")

# IAS Reporting API base (confirmed: https://api.integralplatform.com).
IAS_API_BASE = "https://api.integralplatform.com"
# Product/platform code in the request path: 'CM' = Campaign Management
# (advertiser/agency side), 'FW' = Firewall. Default to Campaign Management.
_DEFAULT_PLATFORM = "CM"
# Teams-discovery endpoint (ASSUMED path -- confirmed at live ratification).
_IAS_TEAMS_URL = f"{IAS_API_BASE}/reportingservice/api/teams"


# ---------------------------------------------------------------------------
# Database helpers (same dual-backend pattern as gsc/connector.py)
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# error_map (taxonomy 25.2): read from manifest.json, passed to
# classify_http_error. IAS declares no numeric provider codes, so the map is
# empty and the pure-HTTP taxonomy classifies every actionable case (see the
# manifest _error_map_note). Cached at module level.
# ---------------------------------------------------------------------------

_ERROR_MAP: dict | None = None


def _load_error_map() -> dict:
    global _ERROR_MAP
    if _ERROR_MAP is None:
        _manifest = json.loads(
            (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
        _ERROR_MAP = _manifest.get("error_map") or {}
    return _ERROR_MAP


# ---------------------------------------------------------------------------
# Raw table DDL (wide: one row per (date, campaign) with the exposed metric
# columns). Staging UNPIVOTs to the long fact_daily_kpi grain downstream.
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_ias_daily (
    date                     VARCHAR,
    campaign_id              VARCHAR,
    campaign_name            VARCHAR,
    measured_impressions     BIGINT,
    viewable_impressions     BIGINT,
    eligible_impressions     BIGINT,
    invalid_traffic_ads      BIGINT,
    brand_safety_passed_ads  BIGINT,
    brand_safety_failed_ads  BIGINT,
    report_profile           VARCHAR,
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_ias_daily
    (date, campaign_id, campaign_name,
     measured_impressions, viewable_impressions, eligible_impressions,
     invalid_traffic_ads, brand_safety_passed_ads, brand_safety_failed_ads,
     report_profile, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Metric columns landed by the wide raw table (canonical field_ids).
_METRIC_COLUMNS = (
    "measured_impressions",
    "viewable_impressions",
    "eligible_impressions",
    "invalid_traffic_ads",
    "brand_safety_passed_ads",
    "brand_safety_failed_ads",
)


def _to_int(value) -> int:
    """Coerce a metric value (IAS returns numeric strings) to int; blank -> 0."""
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    project_id: str,
    report_profile: str,
) -> int:
    """Insert canonical (post-transform) rows into raw_ias_daily (DuckDB)."""
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(f"_insert_raw_rows: unsupported db_mode {db_mode!r}")

    import duckdb  # noqa: PLC0415

    duckdb_path = _get_duckdb_path()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    con = duckdb.connect(duckdb_path)
    con.execute(_RAW_CREATE_DDL)
    values = [
        (
            r.get("date", ""),
            r.get("campaign_id", ""),
            r.get("campaign_name", ""),
            _to_int(r.get("measured_impressions")),
            _to_int(r.get("viewable_impressions")),
            _to_int(r.get("eligible_impressions")),
            _to_int(r.get("invalid_traffic_ads")),
            _to_int(r.get("brand_safety_passed_ads")),
            _to_int(r.get("brand_safety_failed_ads")),
            report_profile,
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


# ---------------------------------------------------------------------------
# Team resolution. Core topology owns the selected Team; an IAS_TEAM_ID env
# fallback keeps ad-hoc pulls working until core topology is wired (mirrors
# gsc's GSC_SITE_URL fallback + deprecation note -- do not remove until core
# account_topology.resolve_selected_account passes the team in at dispatch).
# ---------------------------------------------------------------------------


def _resolve_team_id(team_id: str | None, profile: str) -> str:
    if team_id is None:
        team_id = os.environ.get("IAS_TEAM_ID")
    if not team_id:
        raise ValueError(
            f"IAS team id required for {profile} "
            "(core topology resolves the selected Team; IAS_TEAM_ID env is the "
            "interim fallback)."
        )
    return team_id


# ---------------------------------------------------------------------------
# pull() -- called by the queue worker only (AD-12).
# ---------------------------------------------------------------------------


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    metrics: list[str],
    dimensions: list[str],
    report_profile: str,
    team_id: str | None = None,
    platform: str = _DEFAULT_PLATFORM,
    campaign_ids: str = "all",
) -> dict:
    """Fetch IAS Reporting data and land rows into raw_ias_daily.

    AD-3: token obtained immediately, falls out of scope after the call.
    AD-7: pull_id is passed in by the caller; never minted here.

    Parameters
    ----------
    metrics / dimensions:
        Canonical field_ids for the report profile. Translated to IAS source
        tokens (camelCase) via the manifest mappings before the request.
    report_profile:
        Profile id stamped on every raw row (viewability_daily / ...).
    team_id:
        IAS Team (reporting entity). Resolved by core topology; IAS_TEAM_ID env
        is the interim fallback.
    platform / campaign_ids:
        Product code ('CM' | 'FW') and campaign filter for the request path.

    Returns {"pull_id", "row_count", "date_from", "date_to"}.

    Raises a typed core.pull_errors.ConnectorError on non-200 / non-429
    (401 -> auth_expired, 403 -> permission_denied, 400 -> invalid_request,
    5xx -> provider_transient). Raises RateLimitError on HTTP 429.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    team_id = _resolve_team_id(team_id, f"pull_{report_profile}")

    # Canonical field_id -> IAS source token (camelCase) via manifest mappings.
    metric_tokens = [_field_to_source(m) for m in metrics]
    dimension_tokens = [_field_to_source(d) for d in dimensions]

    # AD-3: token used immediately; falls out of scope after the httpx call.
    token = nango_client.get_fresh_token(connection_id, provider="ias")

    # ASSUMED request contract (confirmed at live ratification, ROLLOUT_NOTES):
    #   GET /reportingservice/api/teams/{teamId}/platform/{platform}/campaigns/{ids}/report
    #   with startDate/endDate (YYYY-MM-DD) + comma-joined metrics/dimensions params.
    url = (
        f"{IAS_API_BASE}/reportingservice/api/teams/{team_id}"
        f"/platform/{platform}/campaigns/{campaign_ids}/report"
    )
    params = {
        "startDate": date_from,
        "endDate": date_to,
        "metrics": ",".join(metric_tokens),
        "dimensions": ",".join(dimension_tokens),
    }

    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30.0,
    )

    if resp.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("ias", retry_after)

    if resp.status_code != 200:
        # Taxonomy 25.2: never a generic RuntimeError. classify_http_error types
        # the error and the manifest error_map (empty for IAS) refines by code.
        # Provider payload preserved as evidence.
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body, _load_error_map())

    payload = resp.json()
    # ASSUMED envelope: rows live under 'rows' (fallback 'data'); confirmed live.
    api_rows = payload.get("rows")
    if api_rows is None:
        api_rows = payload.get("data") or []

    canonical_rows = transform(api_rows)

    row_count = _insert_raw_rows(canonical_rows, pull_id, project_id, report_profile)

    # AD-3: no token in log.
    logger.info(
        "ias_pull_completed: pull_id=%s profile=%s row_count=%d",
        pull_id, report_profile, row_count,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Per-profile dispatch shims (AI-58 pattern). The queue passes
# (connection_id, date_from, date_to, project_id, pull_id) by keyword; each
# shim pins the profile's metrics + dimensions.
# ---------------------------------------------------------------------------


def _make_profile_pull(profile_id: str, metrics: list[str], dimensions: list[str]):
    def _profile_pull(
        connection_id: str,
        date_from: str,
        date_to: str,
        project_id: str,
        pull_id: str,
        team_id: str | None = None,
        platform: str = _DEFAULT_PLATFORM,
        campaign_ids: str = "all",
    ) -> dict:
        return pull(
            connection_id=connection_id,
            date_from=date_from,
            date_to=date_to,
            project_id=project_id,
            pull_id=pull_id,
            metrics=metrics,
            dimensions=dimensions,
            report_profile=profile_id,
            team_id=team_id,
            platform=platform,
            campaign_ids=campaign_ids,
        )

    _profile_pull.__name__ = f"pull_{profile_id}"
    _profile_pull.__qualname__ = f"pull_{profile_id}"
    _profile_pull.__doc__ = (
        f"Profile pull for the '{profile_id}' IAS report profile. Pins "
        f"metrics={metrics} dimensions={dimensions}. Same return shape as pull()."
    )
    return _profile_pull


_GRAIN_DIMENSIONS = ["date", "campaign_id", "campaign_name"]

pull_viewability_daily = _make_profile_pull(
    "viewability_daily",
    ["measured_impressions", "viewable_impressions", "eligible_impressions"],
    _GRAIN_DIMENSIONS,
)
pull_brand_safety_daily = _make_profile_pull(
    "brand_safety_daily",
    ["brand_safety_passed_ads", "brand_safety_failed_ads"],
    _GRAIN_DIMENSIONS,
)
pull_invalid_traffic_daily = _make_profile_pull(
    "invalid_traffic_daily",
    ["invalid_traffic_ads"],
    _GRAIN_DIMENSIONS,
)


# ---------------------------------------------------------------------------
# transform() -- manifest-driven canonical field mapping (AD-2).
# IAS returns camelCase source tokens; rename them to canonical field_ids and
# drop ratio metrics (recomputed downstream from their additive counts).
# ---------------------------------------------------------------------------

# Ratio / percentage source tokens that are NEVER stored (AD-4 ratio rule).
_DROP_FIELDS = {
    "viewableRate",
    "measuredRate",
    "fraudulentPct",
    "givtPct",
    "passedPct",
    "failedPct",
    "blockedPct",
}


def _rename_map() -> dict[str, str]:
    """Build the source-token -> canonical field_id rename map from the manifest."""
    _manifest = json.loads(
        (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
    )
    rename: dict[str, str] = {}
    for src, val in _manifest.get("canonical_metric_mapping", {}).items():
        rename[src] = val if isinstance(val, str) else val.get("canonical", src)
    rename.update(_manifest.get("canonical_dimension_mapping", {}))
    return rename


def _field_to_source(field_id: str) -> str:
    """Reverse of the rename map: canonical field_id -> IAS source token.

    Used to build the request's metrics/dimensions params. Falls back to the
    field_id itself for fields that already equal their source token (e.g. date).
    """
    for source, canonical in _rename_map().items():
        if canonical == field_id:
            return source
    return field_id


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw IAS API rows (camelCase tokens) to canonical field_ids.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_mapping.
    AD-4: ratio metrics are dropped -- never stored; recomputed at the semantic
    layer from their additive numerator/denominator counts.
    """
    rename = _rename_map()
    result: list[dict] = []
    for row in raw_rows:
        canonical: dict = {}
        for key, value in row.items():
            if key in _DROP_FIELDS:
                continue
            canonical[rename.get(key, key)] = value
        result.append(canonical)
    return result


# ---------------------------------------------------------------------------
# Account topology discovery (playbook section 5). IAS topology is a single
# flat level: the Teams the token can reach. Core owns selection / access-check
# / trial / backfill.
# ---------------------------------------------------------------------------


def discover_accounts(connection_id: str) -> list[dict]:
    """List the IAS Teams the connection's token can reach.

    Returns the generic flat-level list core's topology flow consumes:
        [{"id": "<teamId>", "label": "<teamName>"}, ...]

    ASSUMED endpoint: GET /reportingservice/api/teams (confirmed at live
    ratification). Response is assumed to carry a 'teams' array of
    {id, name} objects (fallback: a bare list).

    Raises RateLimitError on 429; a typed ConnectorError on any other non-2xx
    (401 -> auth_expired via the pure-HTTP taxonomy).

    AD-3: token used immediately as a Bearer header; never stored or logged.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    token = nango_client.get_fresh_token(connection_id, provider="ias")

    resp = httpx.get(
        _IAS_TEAMS_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )

    if resp.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("ias", retry_after)

    if resp.status_code != 200:
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body, _load_error_map())

    payload = resp.json()
    teams = payload.get("teams") if isinstance(payload, dict) else payload
    teams = teams or []

    logger.info("ias_discover_accounts: teams=%d", len(teams))

    accounts: list[dict] = []
    for entry in teams:
        team_id = str(entry.get("id") or entry.get("teamId") or "")
        if not team_id:
            continue
        label = entry.get("name") or entry.get("teamName") or team_id
        accounts.append({"id": team_id, "label": label})
    return accounts


# ---------------------------------------------------------------------------
# MCP tool -- reads from fact_daily_kpi mart (AD-12).
# ---------------------------------------------------------------------------

_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'ias'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _get_mart_table(db_mode: str) -> str:
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


def _query_mart(date_from: str, date_to: str, project_id: str) -> list[dict]:
    db_mode = _get_db_mode()
    table = _get_mart_table(db_mode)
    if db_mode == "duckdb":
        import duckdb  # noqa: PLC0415

        sql = _MART_QUERY.format(table=table, p_project="?", p_from="?", p_to="?")
        con = duckdb.connect(_get_duckdb_path(), read_only=True)
        try:
            rel = con.execute(sql, [project_id, date_from, date_to])
            cols = [d[0] for d in rel.description]
            return [dict(zip(cols, row)) for row in rel.fetchall()]
        finally:
            con.close()
    raise ValueError(f"Unknown TOOROW_DB_MODE: {db_mode!r}")


@mcp_app.tool()
def get_ias_report(
    project_id: str = "default",
    report_profile: str = "viewability_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Integral Ad Science report -- reads from fact_daily_kpi mart.

    Report profiles: viewability_daily, brand_safety_daily, invalid_traffic_daily.
    Metrics are additive impression/ad counts (viewability, IVT, brand safety);
    rates are recomputed downstream from these counts (never summed).

    Returns the canonical AD-1 envelope via structuredContent. Text channel
    (this docstring) is the lean LLM summary.

    Parameters:
        project_id: Project identifier (AD-14 placeholder).
        report_profile: Report profile id from the manifest.
        date_from: Start date ISO-8601. Defaults to 90 days ago.
        date_to: End date ISO-8601. Defaults to yesterday.
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

    pull_ids = {r["pull_id"] for r in rows if r.get("pull_id")}
    freshness_values = [r["freshness"] for r in rows if r.get("freshness")]
    latest_pull_id = max(pull_ids) if pull_ids else None
    latest_freshness = max(freshness_values) if freshness_values else None

    data_by_metric: dict[str, list[dict]] = {}
    for r in rows:
        data_by_metric.setdefault(r["metric"], []).append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    provenance = (
        {"source_system": "ias", "source_field": "fact_daily_kpi", "pull_id": latest_pull_id}
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


# ---------------------------------------------------------------------------
# Register this module's raw table name with core.verification (module->core
# direction is allowed by AD-2; the table name never appears in core source).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_ias_daily", provider="ias")
except Exception:
    pass  # best-effort; verification logs a warning if the table name is missing
