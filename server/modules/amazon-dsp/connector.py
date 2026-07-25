"""Standalone Amazon DSP Reporting v3 connector."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("amazon-dsp")

PROVIDER_API_VERSION = "reporting-v3"
REGIONAL_HOSTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}
REFETCH_DAYS = (3, 14, 45)

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")

_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'amazon-dsp'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


class AmazonDspOnboardingError(RuntimeError):
    """The connection has no usable DSP seat/account/advertiser."""


class AmazonDspCompatibilityError(ValueError):
    """The explicit report shape is not legal for the DSP report type."""


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def _sources() -> dict:
    return json.loads(
        (Path(__file__).parent / "catalog_sources" / "catalog_sources.json").read_text(
            encoding="utf-8"
        )
    )


def _columns() -> dict[str, list[str]]:
    return json.loads(
        (Path(__file__).parent / "catalog_sources" / "report_type_columns.json").read_text(
            encoding="utf-8"
        )
    )


def _client_id(value: str | None = None) -> str:
    client_id = value or os.environ.get("AMAZON_ADS_CLIENT_ID", "")
    if not client_id:
        raise AmazonDspOnboardingError("Amazon DSP requires the platform Amazon-Ads-ClientId")
    return client_id


def _host(region: str) -> str:
    try:
        return REGIONAL_HOSTS[region.upper()]
    except KeyError as exc:
        raise AmazonDspOnboardingError(f"Unsupported Amazon DSP region: {region}") from exc


def _provider_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    return str(payload.get("code") or payload.get("error") or "") or None


def _raise_response(response) -> None:
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("amazon-dsp", retry_after)
    try:
        original = response.json()
    except Exception:
        original = response.text
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and (code := _provider_code(original)):
        normalized["code"] = code
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _headers(token: str, client_id: str, ads_account_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Amazon-Ads-ClientId": client_id,
        "Content-Type": "application/vnd.createasyncreportrequest.v3+json",
    }
    if ads_account_id:
        headers["Amazon-Ads-AccountId"] = ads_account_id
    return headers


def _request(client, method: str, host: str, path: str, headers: dict, **kwargs):
    response = client.request(method, f"{host}{path}", headers=headers, timeout=60, **kwargs)
    if response.status_code not in (200, 202, 425):
        _raise_response(response)
    return response


def _token(connection_id: str) -> str:
    from core import nango_client  # noqa: PLC0415

    return nango_client.get_fresh_token(connection_id, provider="amazon-dsp")


def discover_accounts(
    connection_id: str,
    *,
    regions: tuple[str, ...] = ("NA", "EU", "FE"),
    _client=None,
    _token_value: str | None = None,
    _client_id_value: str | None = None,
) -> list[dict]:
    """Probe regional DSP account APIs; an empty seat is a typed failure."""
    token = _token_value or _token(connection_id)
    client_id = _client_id(_client_id_value)
    client = _client or httpx.Client()
    selections: list[dict] = []
    for region in regions:
        payload = _request(
            client,
            "GET",
            _host(region),
            "/accounts",
            _headers(token, client_id),
        ).json()
        for account in payload.get("accounts") or []:
            account_id = str(account.get("adsAccountId") or account.get("id") or "")
            for advertiser in account.get("advertisers") or []:
                advertiser_id = str(advertiser.get("advertiserId") or advertiser.get("id") or "")
                selections.append(
                    {
                        "id": f"amazon_dsp_selection_{len(selections) + 1}",
                        "region": region,
                        "ads_account_id": account_id,
                        "advertiser_id": advertiser_id,
                        "display_name": advertiser.get("name") or advertiser_id,
                        "timezone": advertiser.get("timezone") or "UTC",
                        "currency": advertiser.get("currency") or "",
                    }
                )
    if not selections:
        raise AmazonDspOnboardingError(
            "No Amazon DSP seat/advertiser is accessible; Sponsored Ads profiles are not used"
        )
    return selections


def validate_selection(report_type: str, group_by: str, time_unit: str, columns: list[str]) -> dict:
    spec = _sources()["report_type_compatibility"]["report_types"].get(report_type)
    if spec is None:
        raise AmazonDspCompatibilityError(f"Unknown Amazon DSP report type: {report_type}")
    invalid = sorted(set(columns) - set(_columns().get(report_type, [])))
    if group_by not in spec["group_by"] or time_unit not in spec["time_units"] or invalid:
        raise AmazonDspCompatibilityError(
            f"Illegal DSP shape report_type={report_type} group_by={group_by} "
            f"time_unit={time_unit} fields={invalid}"
        )
    if report_type == "dspBenchmarks":
        raise AmazonDspCompatibilityError(
            "dspBenchmarks requires a dedicated non-additive benchmark grain"
        )
    return spec


def split_date_windows(date_from: str, date_to: str, max_days: int) -> list[tuple[str, str]]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must not precede date_from")
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=max_days - 1))
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)
    return windows


def build_report_request(
    report_type: str,
    group_by: str,
    columns: list[str],
    date_from: str,
    date_to: str,
    advertiser_id: str,
    *,
    time_unit: str = "DAILY",
    region: str,
    ads_account_id: str,
) -> dict:
    validate_selection(report_type, group_by, time_unit, columns)
    return {
        "startDate": date_from,
        "endDate": date_to,
        "configuration": {
            "adProduct": "AMAZON_DSP",
            "reportTypeId": report_type,
            "groupBy": [group_by],
            "columns": columns,
            "timeUnit": time_unit,
            "format": "GZIP_JSON",
            "filters": [{"field": "advertiserId", "values": [advertiser_id]}],
        },
        "routing": {
            "region": region.upper(),
            "adsAccountId": ads_account_id,
            "advertiserId": advertiser_id,
        },
    }


def canonical_request_hash(request: dict) -> str:
    stable = {key: value for key, value in request.items() if key not in {"name", "reportName"}}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class _DspReportFlow:
    def __init__(self, client, host, headers, request):
        self.client = client
        self.host = host
        self.headers = headers
        self.request = request
        self.download_url: str | None = None
        self.poll_401_count = 0

    def submit(self) -> str:
        body = {key: value for key, value in self.request.items() if key != "routing"}
        body["name"] = f"toorow-{canonical_request_hash(self.request)[:20]}"
        response = _request(
            self.client, "POST", self.host, "/reporting/reports", self.headers, json=body
        )
        payload = response.json()
        report_id = payload.get("reportId") or payload.get("existingReportId")
        if not report_id:
            raise AmazonDspCompatibilityError("Amazon DSP report creation returned no reportId")
        return str(report_id)

    def poll(self, report_id: str) -> str:
        response = self.client.request(
            "GET",
            f"{self.host}/reporting/reports/{report_id}",
            headers=self.headers,
            timeout=60,
        )
        if response.status_code == 401 and self.poll_401_count < 2:
            self.poll_401_count += 1
            return "pending"
        if response.status_code not in (200, 202):
            _raise_response(response)
        payload = response.json()
        status = str(payload.get("status") or "").upper()
        self.download_url = payload.get("url") or payload.get("location")
        return {
            "PENDING": "pending",
            "PROCESSING": "processing",
            "COMPLETED": "completed",
            "SUCCESS": "completed",
            "FAILED": "failed",
            "EXPIRED": "expired",
        }.get(status, "pending")

    def download(self, report_id: str) -> list[dict]:
        if not self.download_url:
            self.poll(report_id)
        response = httpx.get(self.download_url or "", timeout=60)
        if response.status_code in (401, 403, 404):
            self.download_url = None
            self.poll(report_id)
            response = httpx.get(self.download_url or "", timeout=60)
        response.raise_for_status()
        raw = response.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8-sig")
        content_type = response.headers.get("Content-Type", "")
        if "csv" in content_type or (text and text[0] not in "[{"):
            return list(csv.DictReader(io.StringIO(text)))
        payload = json.loads(text or "[]")
        return payload.get("rows") or payload if isinstance(payload, dict) else payload


def run_dsp_report(
    connection_id: str,
    request: dict,
    *,
    _client=None,
    _token_value: str | None = None,
    _client_id_value: str | None = None,
    **driver_kwargs,
) -> dict:
    from core.async_reports import AsyncReportFlow, run_async_report  # noqa: PLC0415

    routing = request["routing"]
    token = _token_value or _token(connection_id)
    headers = _headers(token, _client_id(_client_id_value), routing["adsAccountId"])
    implementation = _DspReportFlow(
        _client or httpx.Client(), _host(routing["region"]), headers, request
    )
    flow = AsyncReportFlow(
        submit=implementation.submit,
        poll=implementation.poll,
        download=implementation.download,
        request_hash=canonical_request_hash(request),
    )
    return run_async_report(flow, ledger_ref=connection_id, **driver_kwargs)


def check_account_access(
    connection_id: str,
    selection: dict,
    *,
    _client=None,
    _token_value: str | None = None,
    _client_id_value: str | None = None,
) -> str:
    request = build_report_request(
        "dspCampaign",
        "campaign",
        ["date", "impressions"],
        date.today().isoformat(),
        date.today().isoformat(),
        selection["advertiser_id"],
        region=selection["region"],
        ads_account_id=selection["ads_account_id"],
    )
    flow = _DspReportFlow(
        _client or httpx.Client(),
        _host(selection["region"]),
        _headers(
            _token_value or _token(connection_id),
            _client_id(_client_id_value),
            selection["ads_account_id"],
        ),
        request,
    )
    return flow.submit()


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_amazon_dsp_daily (
  region VARCHAR, ads_account_id VARCHAR, advertiser_id VARCHAR, report_type VARCHAR,
  group_by VARCHAR, date VARCHAR, dimensions_json VARCHAR, metric VARCHAR, value DOUBLE,
  provider_value VARCHAR, non_additive BOOLEAN, request_hash VARCHAR, pull_id VARCHAR,
  loaded_at VARCHAR, project_id VARCHAR
)
"""


def _land(rows: list[dict], context: dict) -> int:
    if os.environ.get("TOOROW_DB_MODE", "duckdb") != "duckdb":
        raise ValueError("amazon-dsp local landing currently requires duckdb")
    import duckdb  # noqa: PLC0415

    path = os.environ.get("TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "local.duckdb"))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    selected_columns = set(context["columns"])
    non_additive_tokens = ("rate", "ratio", "average", "reach", "frequency", "roas")
    values = []
    for row in rows:
        dimensions = {
            key: value
            for key, value in row.items()
            if key in selected_columns and not isinstance(value, (int, float))
        }
        row_date = str(row.get("date") or row.get("intervalStart") or context["date_from"])
        for metric, provider_value in row.items():
            if metric not in selected_columns or metric in dimensions:
                continue
            try:
                value = float(provider_value)
            except (TypeError, ValueError):
                continue
            values.append(
                (
                    context["region"],
                    context["ads_account_id"],
                    context["advertiser_id"],
                    context["report_type"],
                    context["group_by"],
                    row_date,
                    json.dumps(dimensions, sort_keys=True, separators=(",", ":")),
                    metric,
                    value,
                    str(provider_value),
                    any(token in metric.lower() for token in non_additive_tokens),
                    context["request_hash"],
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = duckdb.connect(path)
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(
            "INSERT INTO raw_amazon_dsp_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
    connection.close()
    return len(values)


def _pull_profile(connection_id, date_from, date_to, project_id, pull_id, profile, selection):
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    if not selection or not selection.get("ads_account_id"):
        raise AmazonDspOnboardingError("A regional Amazon DSP account and advertiser are required")
    report_type = selection.get("report_type", "dspCampaign")
    group_by = selection.get("group_by", "campaign")
    columns = selection.get("columns") or [
        "date",
        "advertiserId",
        "impressions",
        "clicks",
        "totalCost",
        "purchases",
        "sales",
    ]
    time_unit = selection.get("time_unit", "DAILY")
    spec = validate_selection(report_type, group_by, time_unit, columns)
    total = 0
    for window_from, window_to in split_date_windows(
        date_from, date_to, int(spec["max_range_days"])
    ):
        request = build_report_request(
            report_type,
            group_by,
            columns,
            window_from,
            window_to,
            selection["advertiser_id"],
            time_unit=time_unit,
            region=selection["region"],
            ads_account_id=selection["ads_account_id"],
        )
        outcome = run_dsp_report(connection_id, request)
        if outcome["status"] != "completed":
            raise ProviderTransientError(
                message=f"Amazon DSP report deferred for {outcome.get('report_ref')}"
            )
        context = {
            **selection,
            "report_type": report_type,
            "group_by": group_by,
            "columns": columns,
            "date_from": window_from,
            "request_hash": canonical_request_hash(request),
            "pull_id": pull_id,
            "project_id": project_id,
        }
        total += _land(outcome["rows"], context)
    return {
        "pull_id": pull_id,
        "row_count": total,
        "date_from": date_from,
        "date_to": date_to,
        "refetch_days": list(REFETCH_DAYS),
        "profile": profile,
    }


def pull(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "campaign_daily", selection
    )


def pull_campaign_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "campaign_daily", selection
    )


def pull_catalog_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "catalog_daily", selection
    )


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


def _get_mart_table(db_mode: str) -> str:
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


def _query_duckdb(sql: str, params: list, duckdb_path: str) -> list[dict]:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
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


def _query_mart_dsp(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    """Query the mart for Amazon DSP data (AD-12: reads fact_daily_kpi only)."""
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


def _build_envelope_dsp(
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
        data_by_metric.setdefault(r["metric"], []).append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    provenance = (
        {
            "source_system": "amazon-dsp",
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


def transform(raw_rows: list[dict]) -> list[dict]:
    mappings = _manifest()["canonical_metric_mapping"] | _manifest()["canonical_dimension_mapping"]
    return [
        {
            (
                mappings.get(key, key)
                if isinstance(mappings.get(key, key), str)
                else mappings[key]["canonical"]
            ): value
            for key, value in row.items()
        }
        for row in raw_rows
    ]


@mcp_app.tool()
def get_amazon_dsp_report(
    project_id: str = "default",
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed Amazon DSP report envelope -- reads from fact_daily_kpi mart.

    Report profiles: campaign_daily, catalog_daily
    Metrics: impressions, clicks, totalCost, purchases, sales (additive at day grain)
    Breakdown dimensions: advertiser_id

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    # AD-4: DSP conversion columns (purchases/sales) are restated by Amazon
    # after the conversion event (refetch ladder 3/14/45 days).

    Parameters:
        project_id: Project identifier (default: 'default')
        report_profile: One of the declared profiles (campaign_daily, catalog_daily)
        date_from: Start date ISO-8601 (e.g. '2026-06-01'). Defaults to 30 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=30)).isoformat()

    try:
        rows = _query_mart_dsp(date_from, date_to, project_id)
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

    return _build_envelope_dsp(rows, report_profile, date_from, date_to, project_id)


# ---------------------------------------------------------------------------
# Register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_amazon_dsp_daily", provider="amazon-dsp")
except Exception:
    pass  # best-effort; verification logs a warning if the table name is missing
