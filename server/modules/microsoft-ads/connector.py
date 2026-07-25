"""Microsoft Ads connector -- Story 26.4 (Bing Ads Reporting v13 REST, async).

Exposes a ``mcp_app: FastMCP`` instance that the core loader mounts under the
``microsoft-ads`` namespace (AD-2). Built to the epic-25 industrial standard:
generated api_catalog.json (192 distinct columns across the 8 integrally
covered report types + 40 excluded report surfaces, ZERO planned), body-code
keyed error_map, Customer -> Account topology, and a catalog_driven profile
whose SubmitGenerateReport body is built from the datastream selection.

# AD-12: MCP server reads the fact_daily_kpi mart only -- no raw_* tables.
# AD-3:  token obtained via nango_client.get_fresh_token immediately before
#        use (Nango, Microsoft Identity). Never stored/logged.
# AD-7:  pull_id minted by the core scheduler and passed into pull().
# AD-2:  request column lists are DERIVED from the manifest/catalog -- no
#        hardcoded field lists outside the catalog. The API version is pinned
#        in manifest.json (provider_api_version) and read from there ONLY.
# AD-4:  ratio/derived statistics carry a non-additive note in the catalog and
#        land RAW at day grain when selected -- never summed downstream.
# AI-03: ASCII-only stdout/log strings.
#
# API facts (research dossier microsoft-ads-catalog-research.md, 2026-07-21):
#   - ASYNC reporting: POST {reporting}/Reporting/v13/GenerateReport/Submit
#     -> ReportRequestId (valid 2 days) -> POST .../GenerateReport/Poll
#     (Status Pending|Success|Error; official guidance: poll every 2-15 min,
#     generations can exceed 60 min) -> GET ReportDownloadUrl (URL valid
#     5 MINUTES: download IMMEDIATELY after the completing poll). The file is
#     a ZIP containing one CSV (utf-8-sig; "" and "--" are null).
#     The whole mechanic runs EXCLUSIVELY through
#     core.async_reports.run_async_report (26.1 socle): claim-row submit,
#     re-poll resumption on the persisted ReportRequestId (= report_ref),
#     single resubmission after expiry, bounded deadline -> retryable
#     ProviderTransientError (cross-review fix: core/queue.py never reads
#     result["status"], so a deferred RETURN would be recorded done at 0
#     rows and an off-ladder window would never be re-pulled).
#   - Headers on every reporting/customer-management call: Authorization
#     Bearer, DeveloperToken (MICROSOFT_ADS_DEVELOPER_TOKEN platform env --
#     NEVER manifest, never per-user), CustomerId (parent customer),
#     CustomerAccountId (selected account), Content-Type: application/json.
#   - Errors: application errors ship with generic HTTP 400/401/403; the
#     classification key is the numeric ``Code`` inside
#     ApiFaultDetail/AdApiFaultDetail Errors[] (body-first, HTTP fallback).
#     Codes 117/207/4204 (manifest rate_limit_codes) -> RateLimitError
#     (breaker + re-queue) whatever the HTTP status -- 207
#     ConcurrentRequestOverLimit is the undocumented concurrent-report cap.
#   - Column Restrictions (guide #columnrestrictions): impression-share
#     statistics are mutually exclusive with restricted attributes, and every
#     request needs >=1 attribute + >=1 non-impression-share statistic.
#     Declared in catalog_sources field_compatibility (26.1 core contract)
#     and refused BEFORE any API call (the reason Airbyte had to split its
#     streams becomes a declarative rule here).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
mcp_app = FastMCP("microsoft-ads")

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (no hardcoded paths).
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# Manifest / catalog / compat-rules access (cached). The API version is pinned
# in ONE place: manifest.json provider_api_version (story 26.4).
# ---------------------------------------------------------------------------

_MANIFEST: dict | None = None


def _load_manifest() -> dict:
    global _MANIFEST
    if _MANIFEST is None:
        _MANIFEST = json.loads(
            (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
    return _MANIFEST


def _load_error_map() -> dict:
    return _load_manifest().get("error_map") or {}


def _rate_limit_codes() -> set[str]:
    """Provider throttling body codes (manifest rate_limit_codes, AD-2)."""
    return {str(c) for c in _load_manifest().get("rate_limit_codes") or []}


def _reporting_base_url() -> str:
    manifest = _load_manifest()
    base = manifest.get("provider_api_base", "https://reporting.api.bingads.microsoft.com")
    version = manifest.get("provider_api_version", "v13")
    return f"{base}/Reporting/{version}"


def _customer_mgmt_base_url() -> str:
    manifest = _load_manifest()
    base = manifest.get(
        "provider_customer_api_base", "https://clientcenter.api.bingads.microsoft.com"
    )
    version = manifest.get("provider_api_version", "v13")
    return f"{base}/CustomerManagement/{version}"


def _load_api_catalog() -> dict:
    global _API_CATALOG
    try:
        return _API_CATALOG  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "api_catalog.json"
    globals()["_API_CATALOG"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_API_CATALOG"]


def _catalog_fields_by_id() -> dict[str, dict]:
    global _CATALOG_BY_ID
    try:
        return _CATALOG_BY_ID  # type: ignore[name-defined]
    except NameError:
        pass
    globals()["_CATALOG_BY_ID"] = {
        f["field_id"]: f for f in _load_api_catalog().get("fields", [])
    }
    return globals()["_CATALOG_BY_ID"]


def _catalog_fields_by_source() -> dict[str, dict]:
    global _CATALOG_BY_SOURCE
    try:
        return _CATALOG_BY_SOURCE  # type: ignore[name-defined]
    except NameError:
        pass
    globals()["_CATALOG_BY_SOURCE"] = {
        f["source_field"]: f for f in _load_api_catalog().get("fields", [])
    }
    return globals()["_CATALOG_BY_SOURCE"]


def _load_compat_rules() -> dict:
    """The VALIDATED 26.1 field_compatibility rules (real core loader)."""
    global _COMPAT_RULES
    try:
        return _COMPAT_RULES  # type: ignore[name-defined]
    except NameError:
        pass
    from core.field_compat import load_module_rules  # noqa: PLC0415

    rules = load_module_rules(Path(__file__).parent)
    if rules is None:
        raise ValueError(
            "microsoft-ads: catalog_sources field_compatibility block missing "
            "or not opted into the core contract (schema_version)"
        )
    globals()["_COMPAT_RULES"] = rules
    return rules


def _impression_share_field_ids() -> set[str]:
    """Impression-share statistic field ids (declared with the compat rules)."""
    return set(_load_compat_rules().get("_impression_share_statistics") or [])


def _non_additive_statistic_field_ids() -> set[str]:
    """NON-ADDITIVE statistic field ids (ratios, averages, shares, cost-per,
    historical quality -- AD-4), generated into the field_compatibility block
    by build_official_fields.py from the same column tuples as the enums.
    Superset of the impression-share family."""
    return set(_load_compat_rules().get("_non_additive_statistics") or [])


def _load_catalog_sources() -> dict:
    """The module's catalog_sources.json (cached). Home of the GENERATED
    descriptive_mutable block (Story 26.6 Part A)."""
    global _CATALOG_SOURCES
    try:
        return _CATALOG_SOURCES  # type: ignore[name-defined]
    except NameError:
        pass
    globals()["_CATALOG_SOURCES"] = json.loads(
        (Path(__file__).parent / "catalog_sources" / "catalog_sources.json").read_text(
            encoding="utf-8"
        )
    )
    return globals()["_CATALOG_SOURCES"]


def _descriptive_mutable_field_ids() -> set[str]:
    """DESCRIPTIVE-MUTABLE attribute field ids (Story 26.6 Part A).

    Entity STATUS/TYPE columns (campaign/ad group/ad/keyword status,
    campaign/ad group/ad type, ...) generated into the top-level
    descriptive_mutable block of catalog_sources.json by
    build_official_fields.py (api_catalog.json's core schema forbids extra
    field keys and is out of scope). They are MUTABLE across the refetch ladder
    re-pulls, so at landing they leave segments_json (part of the dbt supersede
    grain) for a separate attributes_json column (latest-wins). Grain ids/names
    and segmenting dimensions are NOT here."""
    block = _load_catalog_sources().get("descriptive_mutable") or {}
    return set(block.get("field_ids") or [])


def _allowed_fields_for_report_type(report_type: str) -> set[str]:
    """The INTEGRAL column enum (field ids) of a covered report type."""
    for rule in _load_compat_rules().get("rules", []):
        if (
            rule.get("kind") == "selectable_set"
            and rule.get("scope", {}).get("report_type") == report_type
        ):
            return set(rule["allowed_fields"])
    raise ValueError(
        f"microsoft-ads: no selectable_set rule for report type {report_type!r} "
        "(not one of the 8 covered report types)"
    )


# ---------------------------------------------------------------------------
# Provider error handling (story 26.4, dossier section 10).
# ---------------------------------------------------------------------------


def _extract_error_code(body) -> str | None:
    """Best-effort extraction of the first numeric body Code. Never raises.

    Bing REST failures nest ``Errors: [{Code, ErrorCode, Message}]`` (or
    BatchErrors/OperationErrors) inside ApiFaultDetail / AdApiFaultDetail /
    ApplicationFault wrappers whose exact envelope varies per service, so the
    walk is recursive and shape-tolerant.
    """
    if isinstance(body, dict):
        for key in ("Errors", "BatchErrors", "OperationErrors"):
            entries = body.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("Code") is not None:
                        return str(entry["Code"])
        for value in body.values():
            code = _extract_error_code(value)
            if code is not None:
                return code
    elif isinstance(body, list):
        for value in body:
            code = _extract_error_code(value)
            if code is not None:
                return code
    return None


def _raise_provider_error(status_code: int, resp: httpx.Response | None = None,
                          body=None) -> None:
    """Raise the canonical typed error for a non-2xx Bing Ads response.

    Classification is BODY-CODE FIRST (Microsoft returns application errors
    with generic HTTP 400): the numeric ``Code`` is matched against the
    manifest rate_limit_codes (117 CallRateExceeded / 207
    ConcurrentRequestOverLimit / 4204 bulk throttling -> RateLimitError,
    breaker + re-queue), then against error_map '<status>:<code>' keys; the
    pure-HTTP classification (classify_http_error) is the fallback so an
    unknown code is never mis-typed. Provider payload preserved as evidence.
    """
    if body is None and resp is not None:
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    provider_code = _extract_error_code(body)

    if status_code == 429 or (
        provider_code is not None and provider_code in _rate_limit_codes()
    ):
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after = None
        if resp is not None:
            try:
                retry_after = int(resp.headers.get("Retry-After", "0")) or None
            except (ValueError, TypeError):
                retry_after = None
        raise RateLimitError("microsoft-ads", retry_after)

    from core import pull_errors  # noqa: PLC0415

    canonical_classes = {
        pull_errors.AUTH_EXPIRED: pull_errors.AuthExpiredError,
        pull_errors.AUTH_REVOKED: pull_errors.AuthRevokedError,
        pull_errors.PERMISSION_DENIED: pull_errors.PermissionDeniedError,
        pull_errors.INVALID_REQUEST: pull_errors.InvalidRequestError,
        pull_errors.PROVIDER_TRANSIENT: pull_errors.ProviderTransientError,
    }
    if provider_code is not None:
        refined = _load_error_map().get(f"{status_code}:{provider_code}")
        exc_cls = canonical_classes.get(refined)
        if exc_cls is not None:
            raise exc_cls(provider_status=status_code, provider_payload=body)

    raise pull_errors.classify_http_error(status_code, body)


# ---------------------------------------------------------------------------
# Auth headers. DeveloperToken is a PLATFORM credential (env, never manifest,
# never per-user). CustomerId + CustomerAccountId are mandatory on every call.
# ---------------------------------------------------------------------------

_DEVELOPER_TOKEN_ENV = "MICROSOFT_ADS_DEVELOPER_TOKEN"


def _developer_token() -> str:
    developer_token = os.environ.get(_DEVELOPER_TOKEN_ENV)
    if not developer_token:
        raise ValueError(
            f"{_DEVELOPER_TOKEN_ENV} env var required (platform-level Microsoft "
            "Ads developer token -- dossier section 7; never per-user)"
        )
    return developer_token


def _headers(token: str, customer_id: str | None = None,
             account_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "DeveloperToken": _developer_token(),
        "Content-Type": "application/json",
    }
    if customer_id:
        headers["CustomerId"] = str(customer_id)
    if account_id:
        headers["CustomerAccountId"] = str(account_id)
    return headers


def _split_selected_account(account_id: str) -> tuple[str, str | None]:
    """Parse the opaque topology account id '<account_id>@<customer_id>'.

    The composite form is produced by discover_accounts so the pull site
    recovers the mandatory CustomerId header without a re-discovery
    round-trip. Core never interprets the id (AD-2).
    """
    raw = str(account_id).strip()
    if "@" in raw:
        acct, cust = raw.split("@", 1)
        return acct.strip(), cust.strip() or None
    return raw, None


def _resolve_account_selection(
    connection_id: str, account_id: str | None
) -> tuple[str, str | None]:
    """Resolve (account_id, customer_id) for a pull.

    Explicit args win (tests / direct calls). Otherwise the ONLY source is the
    core topology scope (story 25.5 flow). There is NO env-var account
    fallback (MICROSOFT_ADS_*_ID is forbidden by doctrine): an unselected
    account is a hard, actionable error.
    """
    if account_id:
        return _split_selected_account(account_id)

    selected: str | None = None
    try:
        from core import account_topology, token_service  # noqa: PLC0415

        resolved = token_service.resolve_connection_by_nango_id(connection_id)
        if resolved is not None:
            selected = account_topology.resolve_selected_account(resolved.id)
    except Exception as exc:  # noqa: BLE001 -- fail closed with a clear message
        logger.warning("microsoft_ads_account_resolution_failed: %s", type(exc).__name__)

    if not selected:
        raise ValueError(
            "no selected reporting account for this microsoft-ads connection -- "
            "run the account topology flow (discovery -> selection -> access "
            "check); env-var account fallbacks do not exist for this module"
        )
    return _split_selected_account(selected)


# ---------------------------------------------------------------------------
# Report profiles (AI-58) and report types (the 8-type execution whitelist).
# ---------------------------------------------------------------------------

# profile id -> (ReportRequest type, data_level stamp). data_level (story 3.6
# lesson): each grain lands under its own marker so coexisting grains never
# double-count inside one series.
_PROFILE_SPECS: dict[str, tuple[str, str]] = {
    "campaign_daily": ("CampaignPerformanceReportRequest", "CAMPAIGN"),
    "ad_group_daily": ("AdGroupPerformanceReportRequest", "AD_GROUP"),
    "ad_daily": ("AdPerformanceReportRequest", "AD"),
    "keyword_daily": ("KeywordPerformanceReportRequest", "KEYWORD"),
    "search_query_daily": ("SearchQueryPerformanceReportRequest", "SEARCH_QUERY"),
    "catalog_daily": ("CampaignPerformanceReportRequest", "CAMPAIGN"),
}

# The 8 whitelisted report types catalog_daily may execute -> data_level.
_REPORT_TYPE_DATA_LEVEL: dict[str, str] = {
    "CampaignPerformanceReportRequest": "CAMPAIGN",
    "AdGroupPerformanceReportRequest": "AD_GROUP",
    "AdPerformanceReportRequest": "AD",
    "KeywordPerformanceReportRequest": "KEYWORD",
    "SearchQueryPerformanceReportRequest": "SEARCH_QUERY",
    "AccountPerformanceReportRequest": "ACCOUNT",
    "GeographicPerformanceReportRequest": "GEOGRAPHIC",
    "AgeGenderAudienceReportRequest": "AGE_GENDER",
}

# Deterministic report-type inference order when a catalog selection does not
# name one explicitly ("the selection chooses the report type"): first type
# whose integral enum covers every selected field wins.
_REPORT_TYPE_PREFERENCE: tuple[str, ...] = (
    "CampaignPerformanceReportRequest",
    "AdGroupPerformanceReportRequest",
    "AdPerformanceReportRequest",
    "KeywordPerformanceReportRequest",
    "SearchQueryPerformanceReportRequest",
    "AccountPerformanceReportRequest",
    "GeographicPerformanceReportRequest",
    "AgeGenderAudienceReportRequest",
)

# Grain-carrier field ids ALWAYS included in a catalog_daily request so every
# row lands with its grain key (all are attributes of the report type's enum).
_CATALOG_STRUCTURE_FIELDS: dict[str, tuple[str, ...]] = {
    "CampaignPerformanceReportRequest": (
        "time_period", "account_id", "campaign_id", "campaign_name",
    ),
    "AdGroupPerformanceReportRequest": (
        "time_period", "account_id", "campaign_id", "ad_group_id", "ad_group_name",
    ),
    "AdPerformanceReportRequest": (
        "time_period", "account_id", "campaign_id", "ad_group_id", "ad_id",
    ),
    "KeywordPerformanceReportRequest": (
        "time_period", "account_id", "campaign_id", "ad_group_id",
        "keyword_id", "keyword",
    ),
    "SearchQueryPerformanceReportRequest": (
        "time_period", "account_id", "campaign_id", "ad_group_id", "search_query",
    ),
    "AccountPerformanceReportRequest": ("time_period", "account_id"),
    "GeographicPerformanceReportRequest": (
        "time_period", "account_id", "campaign_id",
    ),
    "AgeGenderAudienceReportRequest": (
        "time_period", "account_id", "campaign_id", "age_group", "gender",
    ),
}

# Column charset accepted by SubmitGenerateReport (defense in depth on top of
# the enum check -- a non-matching token can never reach the wire).
_COLUMN_CHARSET = re.compile(r"^[A-Za-z0-9]+$")

_REPORT_TIME_ZONE = "GreenwichMeanTimeDublinEdinburghLisbonLondon"


# ---------------------------------------------------------------------------
# Pre-call validation (26.1 contract + Column Restrictions + invariants).
# ---------------------------------------------------------------------------


def _refuse_excluded_fields(field_ids: list[str]) -> None:
    """No silent drops: a selected EXCLUDED catalog field is a typed refusal."""
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    by_id = _catalog_fields_by_id()
    offending = [
        (fid, by_id[fid].get("exclusion_reason", ""))
        for fid in field_ids
        if fid in by_id and by_id[fid].get("exposure") == "excluded"
    ]
    if offending:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload={
                "violations": [
                    {
                        "rule_id": "exposure-excluded",
                        "kind": "exposure",
                        "message": f"field {fid!r} is excluded: {reason}",
                        "fields": [fid],
                    }
                    for fid, reason in sorted(offending)
                ]
            },
            message=(
                "microsoft-ads selection refused before the API call: "
                "excluded catalog field(s) "
                + ", ".join(sorted(fid for fid, _ in offending))
                + " (exclusion reasons preserved in provider_payload)"
            ),
        )


def _enforce_request_invariants(
    metric_ids: list[str], dimension_ids: list[str]
) -> None:
    """The >=1 attribute + >=1 non-impression-share statistic invariant.

    Not expressible in the typed field_compatibility rule kinds (26.1 schema),
    so the request builder enforces it here as a typed pre-call refusal with a
    stable rule id -- same evidence shape as core.field_compat.enforce_selection.
    """
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    violations: list[dict] = []
    if not dimension_ids:
        violations.append({
            "rule_id": "request-invariants-min-attr-min-stat",
            "kind": "request_invariant",
            "message": "a report request needs at least 1 attribute column",
            "fields": [],
        })
    real_statistics = [
        m for m in metric_ids if m not in _impression_share_field_ids()
    ]
    if not real_statistics:
        violations.append({
            "rule_id": "request-invariants-min-attr-min-stat",
            "kind": "request_invariant",
            "message": (
                "a report request needs at least 1 performance statistic that "
                "is NOT an impression-share column"
            ),
            "fields": sorted(metric_ids),
        })
    if violations:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload={"violations": violations},
            message=(
                "microsoft-ads selection refused before the API call: "
                "rules=request-invariants-min-attr-min-stat; "
                + "; ".join(v["message"] for v in violations)
            ),
        )


def _validate_dates(date_from: str, date_to: str) -> tuple[date, date]:
    """ISO dates, from <= to -- validated BEFORE any submit is built."""
    try:
        start = date.fromisoformat(str(date_from))
        end = date.fromisoformat(str(date_to))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"microsoft-ads: invalid ISO date window {date_from!r}..{date_to!r}"
        ) from exc
    if start > end:
        raise ValueError(
            f"microsoft-ads: date_from {date_from!r} is after date_to {date_to!r}"
        )
    return start, end


def _resolve_columns(report_type: str, field_ids: list[str]) -> list[str]:
    """field ids -> provider Columns, verified against the report type enum.

    Every column must belong to the report type's INTEGRAL XSD enum (the
    selectable_set rule) and match the provider charset. A selected field
    either lands or is refused typed BEFORE the call -- never dropped.
    """
    allowed = _allowed_fields_for_report_type(report_type)
    by_id = _catalog_fields_by_id()
    columns: list[str] = []
    unknown = sorted(set(fid for fid in field_ids if fid not in by_id))
    outside = sorted(
        set(fid for fid in field_ids if fid in by_id and fid not in allowed)
    )
    if unknown or outside:
        from core.pull_errors import InvalidRequestError  # noqa: PLC0415

        parts = []
        if unknown:
            parts.append("unknown field id(s): " + ", ".join(unknown))
        if outside:
            parts.append(
                f"field(s) not in the {report_type} column enum: "
                + ", ".join(outside)
            )
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                "microsoft-ads selection refused before the API call: "
                + "; ".join(parts)
            ),
        )
    for fid in field_ids:
        column = by_id[fid]["source_field"]
        if not _COLUMN_CHARSET.match(column):
            raise ValueError(
                f"microsoft-ads: column {column!r} violates the provider "
                "charset ^[A-Za-z0-9]+$ (catalog corruption -- refuse loudly)"
            )
        if column not in columns:
            columns.append(column)
    return columns


def build_submit_request(
    report_type: str,
    columns: list[str],
    account_id: str,
    date_from: str,
    date_to: str,
    report_name: str,
) -> dict:
    """Build the SubmitGenerateReport body (validated inputs only).

    Airbyte-proven shape: Csv 2.0, headers-only CSV (report header/footer
    excluded), ReturnOnlyCompleteData false (dossier: partial-freshness of the
    current day is assumed; the refetch ladder restates).
    """
    if report_type not in _REPORT_TYPE_DATA_LEVEL:
        raise ValueError(
            f"microsoft-ads: report type {report_type!r} is not one of the 8 "
            "whitelisted covered report types"
        )
    start, end = _validate_dates(date_from, date_to)
    for column in columns:
        if not _COLUMN_CHARSET.match(column):
            raise ValueError(
                f"microsoft-ads: column {column!r} violates ^[A-Za-z0-9]+$"
            )
    return {
        "ReportRequest": {
            "Type": report_type,
            "ReportName": report_name,
            "Format": "Csv",
            "FormatVersion": "2.0",
            "Aggregation": "Daily",
            "Columns": list(columns),
            "ExcludeColumnHeaders": False,
            "ExcludeReportHeader": True,
            "ExcludeReportFooter": True,
            "ReturnOnlyCompleteData": False,
            "Scope": {"AccountIds": [int(account_id)]},
            "Time": {
                "CustomDateRangeStart": {
                    "Day": start.day, "Month": start.month, "Year": start.year,
                },
                "CustomDateRangeEnd": {
                    "Day": end.day, "Month": end.month, "Year": end.year,
                },
                "ReportTimeZone": _REPORT_TIME_ZONE,
            },
        }
    }


def _request_hash(submit_body: dict, customer_id: str | None) -> str:
    """Deterministic hash of the FULL request shape (26.1 dedup key).

    Covers report type, columns, aggregation, window, account scope and the
    customer header -- but NOT ReportName (a per-run label derived from the
    pull_id): two runs asking for the same data MUST share the hash so the
    second run resumes the first run's ReportRequestId by re-poll instead of
    resubmitting (the 26.1 no-blind-resubmission invariant). Columns are
    hashed ORDER-INSENSITIVE (F-4): the Submit body keeps the caller's order,
    but two reordered selections ask for the same data and must share one
    hash so they resume each other's reports.
    """
    request = dict(submit_body.get("ReportRequest", {}))
    request.pop("ReportName", None)
    if isinstance(request.get("Columns"), list):
        request["Columns"] = sorted(request["Columns"])
    payload = json.dumps(
        {"customer_id": customer_id, "request": request}, sort_keys=True
    )
    return "msads_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Async flow callables (submit / poll / download) -- run through
# core.async_reports.run_async_report EXCLUSIVELY.
# ---------------------------------------------------------------------------

# Poll statuses -> canonical async_reports statuses. Any other value fails
# loudly (contract violation). Poll body codes 2100/2101 (invalid/unknown
# report id on a previously valid reference) -> canonical 'expired' (F-3).
_POLL_STATUS_MAP = {"Pending": "pending", "Success": "completed", "Error": "failed"}
_EXPIRED_POLL_CODES = {"2100", "2101"}


class _ReportFlow:
    """One report request's submit/poll/download closures.

    Keeps the last ReportDownloadUrl per report_ref so ``download`` runs
    IMMEDIATELY after the completing poll (the URL dies in 5 minutes). A dead
    URL raises core.async_reports.DownloadLinkExpired -> single resubmission.
    """

    def __init__(self, connection_id: str, submit_body: dict,
                 customer_id: str | None, account_id: str) -> None:
        self._connection_id = connection_id
        self._submit_body = submit_body
        self._customer_id = customer_id
        self._account_id = account_id
        self._download_urls: dict[str, str | None] = {}

    def _fresh_headers(self) -> dict:
        # AD-3: token fetched immediately before use, never stored.
        from core import nango_client  # noqa: PLC0415

        token = nango_client.get_fresh_token(
            self._connection_id, provider="microsoft-ads"
        )
        return _headers(token, self._customer_id, self._account_id)

    def submit(self) -> str:
        resp = httpx.post(
            f"{_reporting_base_url()}/GenerateReport/Submit",
            json=self._submit_body,
            headers=self._fresh_headers(),
            timeout=60.0,
        )
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)
        report_ref = (resp.json() or {}).get("ReportRequestId")
        if not report_ref:
            from core.pull_errors import ProviderTransientError  # noqa: PLC0415

            raise ProviderTransientError(
                provider_status=200,
                provider_payload=resp.text,
                message="microsoft-ads: Submit returned no ReportRequestId",
            )
        return str(report_ref)

    def poll(self, report_ref: str) -> str:
        resp = httpx.post(
            f"{_reporting_base_url()}/GenerateReport/Poll",
            json={"ReportRequestId": report_ref},
            headers=self._fresh_headers(),
            timeout=60.0,
        )
        if resp.status_code != 200:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            code = _extract_error_code(body)
            if code in _EXPIRED_POLL_CODES:
                # F-3 contract: a reference the provider no longer knows maps
                # to canonical 'expired' (single-resubmission policy applies).
                logger.warning(
                    "microsoft_ads_report_ref_purged: code=%s -> expired", code
                )
                return "expired"
            _raise_provider_error(resp.status_code, resp, body=body)
        status_obj = (resp.json() or {}).get("ReportRequestStatus") or {}
        raw_status = status_obj.get("Status")
        canonical = _POLL_STATUS_MAP.get(raw_status)
        if canonical is None:
            raise ValueError(
                f"microsoft-ads: Poll returned unknown status {raw_status!r}"
            )
        self._download_urls[report_ref] = status_obj.get("ReportDownloadUrl")
        return canonical

    def download(self, report_ref: str) -> list[dict]:
        url = self._download_urls.get(report_ref)
        if not url:
            # Success without a URL = report generated with zero data rows.
            return []
        resp = httpx.get(url, timeout=120.0, follow_redirects=True)
        if resp.status_code in (403, 404, 410):
            from core.async_reports import DownloadLinkExpired  # noqa: PLC0415

            raise DownloadLinkExpired(
                f"microsoft-ads: download URL dead (HTTP {resp.status_code}; "
                "URLs expire after 5 minutes)"
            )
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)
        return _parse_report_zip(resp.content)


def _parse_report_zip(content: bytes) -> list[dict]:
    """ZIP -> CSV rows keyed by catalog field_id (raw string values).

    utf-8-sig CSV, column headers = provider column names; '' and '--'
    normalize to None (Airbyte-verified provider convention). A header the
    catalog does not know lands under its raw provider name -- observable,
    never silently dropped (and a drift signal in tests).
    """
    by_source = _catalog_fields_by_source()
    rows: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if not names:
            return []
        with archive.open(names[0]) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            for raw in reader:
                row: dict = {}
                for header, value in raw.items():
                    if header is None:
                        continue
                    if isinstance(value, str):
                        value = value.strip()
                        if value in ("", "--"):
                            value = None
                    entry = by_source.get(header)
                    key = entry["field_id"] if entry else header
                    row[key] = value
                rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# transform() -- catalog-typed coercion + manifest-driven canonical renames.
# ---------------------------------------------------------------------------


def _coerce_value(field_id: str, value):
    """Coerce a raw CSV string by the CATALOG physical_type (26.4 rule:
    conversions are driven by the catalog's physical_type, NEVER by a
    column-name suffix). Percent-valued cells carry a trailing '%' which is a
    formatting artifact, stripped before decimal parsing."""
    if value is None or not isinstance(value, str):
        return value
    entry = _catalog_fields_by_id().get(field_id)
    if entry is None:
        return value
    physical_type = entry.get("physical_type")
    text = value.replace(",", "")  # thousand separators in some locales
    if text.endswith("%"):
        text = text[:-1]
    try:
        if physical_type == "integer":
            return int(float(text))
        if physical_type == "decimal":
            return float(text)
    except (ValueError, TypeError):
        return value  # keep raw evidence rather than corrupt it
    return value


def transform(raw_rows: list[dict]) -> list[dict]:
    """Coerce by catalog physical_type, then rename to canonical names.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_mapping
    (spend -> cost, time_period -> date, ...). Fields absent from both mappings
    pass through unchanged under their field_id (pull_id, connector, catalog
    selections ...). NOTHING is dropped here: ratio statistics selected via
    catalog_daily land RAW at day grain (non-additive, AD-4) -- absence of a
    field in a mapping never deletes it.
    """
    manifest = _load_manifest()

    rename_map: dict[str, str] = {}
    for src, val in manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            rename_map[src] = val.get("canonical", src)
    rename_map.update(manifest.get("canonical_dimension_mapping", {}))

    result: list[dict] = []
    for row in raw_rows:
        canonical: dict = {}
        for key, value in row.items():
            canonical[rename_map.get(key, key)] = _coerce_value(key, value)
        result.append(canonical)
    return result


def _canonical_metric_names(metric_field_ids: list[str]) -> list[str]:
    mapping = _load_manifest().get("canonical_metric_mapping", {})
    names: list[str] = []
    for field_id in metric_field_ids:
        val = mapping.get(field_id, field_id)
        if isinstance(val, dict):
            val = val.get("canonical", field_id)
        names.append(val)
    return names


# ---------------------------------------------------------------------------
# Raw landing -- LONG format: one row per grain x metric (story 26.2 pattern).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_microsoft_ads_daily (
    date                  VARCHAR,
    data_level            VARCHAR,
    account_id            VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    ad_group_id           VARCHAR,
    ad_group_name         VARCHAR,
    ad_id                 VARCHAR,
    keyword_id            VARCHAR,
    keyword               VARCHAR,
    search_query          VARCHAR,
    segments_json         VARCHAR,
    attributes_json       VARCHAR,
    metric                VARCHAR,
    value_num             DOUBLE,
    cost_source_currency  VARCHAR,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

# Story 26.6 Part A: attributes_json holds the DESCRIPTIVE-MUTABLE entity
# status/type attributes (latest-wins), kept OUT of segments_json so a status
# change on a re-pull of the same day never forks the dbt supersede grain.

_RAW_INSERT_SQL = """
INSERT INTO raw_microsoft_ads_daily
    (date, data_level, account_id, campaign_id, campaign_name, ad_group_id,
     ad_group_name, ad_id, keyword_id, keyword, search_query, segments_json,
     attributes_json, metric, value_num, cost_source_currency, pull_id,
     loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Canonical (post-transform) keys that land in dedicated grain columns.
_STRUCTURE_COLUMNS = (
    "date", "account_id", "campaign_id", "campaign_name", "ad_group_id",
    "ad_group_name", "ad_id", "keyword_id", "keyword", "search_query",
)
_NON_SEGMENT_KEYS = set(_STRUCTURE_COLUMNS) | {
    "currency_code", "pull_id", "connector", "data_level",
}


def _descriptive_mutable_landed_keys() -> set[str]:
    """Story 26.6 Part A: the descriptive-mutable field ids projected onto the
    POST-TRANSFORM row keys they land under (cached).

    A row reaching _insert_raw_rows has already been through transform(), so a
    status/type column appears under its CANONICAL name when the manifest
    renames it (e.g. field id ``status`` -> canonical ``ad_group_status``) and
    under its field id otherwise. We recognise BOTH forms so the split is exact
    whatever the manifest mapping."""
    cached = globals().get("_DESC_MUTABLE_KEYS")
    if cached is not None:
        return cached
    manifest = _load_manifest()
    rename: dict[str, str] = {}
    for src, val in manifest.get("canonical_metric_mapping", {}).items():
        canonical = val.get("canonical", src) if isinstance(val, dict) else val
        if isinstance(canonical, str):
            rename[src] = canonical
    rename.update(manifest.get("canonical_dimension_mapping", {}))
    keys: set[str] = set()
    for field_id in _descriptive_mutable_field_ids():
        keys.add(field_id)
        keys.add(rename.get(field_id, field_id))
    globals()["_DESC_MUTABLE_KEYS"] = keys
    return keys


def _insert_raw_rows(
    rows: list[dict],
    metric_names: list[str],
    data_level: str,
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> tuple[int, int]:
    """Long-ify canonical wide rows and insert into raw_microsoft_ads_daily.

    Returns ``(row_count, non_numeric_landed)``. One long row per
    (grain, metric) with value_num. NULL honnete (AD-9): an absent metric
    lands NO row -- absence stays distinguishable from a recorded zero.
    Non-structural SEGMENTING dimension values (age/gender/device/geo, ...)
    land in segments_json (canonical sorted-key JSON) so any catalog dimension
    participates in the supersede grain without schema churn.

    Story 26.6 Part A: DESCRIPTIVE-MUTABLE attributes (entity status/type --
    campaign_status, ad_status, campaign_type, ...) are split OUT of
    segments_json into a separate ``attributes_json`` column (latest-wins).
    They mutate across the refetch ladder re-pulls (a campaign Active on
    July 1 re-pulled as Paused for the SAME July 1), and segments_json is part
    of the dbt QUALIFY supersede grain -- keeping them there would fork the
    grain and let the stale row survive, doubling metrics (finding F-1).
    attributes_json is NEVER in the supersede partition.

    F-3 (review 26.4): a NON-NUMERIC statistic value never lands as a
    value_num row, but it is NOT dropped either -- the raw value is
    reinjected into the row's segments_json keyed by the catalog field_id,
    a warning is logged and the ``non_numeric_landed`` counter reports it.
    """
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )
    from core import warehouse_write  # noqa: PLC0415

    # Reverse canonical-name -> catalog field_id map (F-3: the segments_json
    # key for a reinjected non-numeric value is the FIELD ID, the stable
    # catalog identifier, not the per-manifest canonical alias).
    field_id_by_canonical: dict[str, str] = {}
    for src, val in _load_manifest().get("canonical_metric_mapping", {}).items():
        canonical = val.get("canonical", src) if isinstance(val, dict) else val
        if isinstance(canonical, str):
            field_id_by_canonical[canonical] = src

    # Story 26.6 Part A: keys that leave segments_json for attributes_json.
    descriptive_mutable_keys = _descriptive_mutable_landed_keys()

    metric_set = list(dict.fromkeys(metric_names))
    values: list[tuple] = []
    non_numeric_landed = 0
    for row in rows:
        # Story 26.6 Part A: descriptive-mutable status/type attributes go to
        # attributes_json (out of the supersede grain); every other non-key,
        # non-metric dimension stays a grain-bearing segment.
        segment_items = {
            k: row[k]
            for k in row
            if k not in _NON_SEGMENT_KEYS and k not in metric_set
            and k not in descriptive_mutable_keys
            and row[k] is not None
        }
        attribute_items = {
            k: row[k]
            for k in row
            if k in descriptive_mutable_keys and row[k] is not None
        }
        # F-3: split the selected metrics into numeric (value_num rows) and
        # non-numeric (reinjected into segments_json, never silently lost).
        numeric_values: dict[str, float] = {}
        for metric in metric_set:
            value = row.get(metric)
            if value is None:
                continue
            try:
                numeric_values[metric] = float(value)
            except (ValueError, TypeError):
                field_id = field_id_by_canonical.get(metric, metric)
                segment_items[field_id] = value
                non_numeric_landed += 1
        segments_json = (
            json.dumps(segment_items, sort_keys=True) if segment_items else None
        )
        attributes_json = (
            json.dumps(attribute_items, sort_keys=True) if attribute_items else None
        )
        currency = row.get("currency_code") or None
        for metric, value_num in numeric_values.items():
            values.append(
                (
                    row.get("date", ""),
                    data_level,
                    row.get("account_id") or "",
                    row.get("campaign_id") or "",
                    row.get("campaign_name") or "",
                    row.get("ad_group_id") or "",
                    row.get("ad_group_name") or "",
                    row.get("ad_id") or "",
                    row.get("keyword_id") or "",
                    row.get("keyword") or "",
                    row.get("search_query") or "",
                    segments_json,
                    attributes_json,
                    metric,
                    value_num,
                    currency,
                    pull_id,
                    loaded_at,
                    project_id,
                )
            )

    if non_numeric_landed:
        logger.warning(
            "microsoft_ads_non_numeric_statistics: pull_id=%s count=%d -- "
            "non-numeric statistic values reinjected into segments_json "
            "(keyed by field_id, raw value preserved); value_num rows were "
            "NOT emitted for them", pull_id, non_numeric_landed,
        )

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_RAW_CREATE_DDL)
    if values:
        con.executemany(_RAW_INSERT_SQL, values)
    con.close()
    return len(values), non_numeric_landed


# ---------------------------------------------------------------------------
# Shared pull core (async submit -> poll -> download -> land).
# ---------------------------------------------------------------------------


def _profile_bundle(profile_id: str) -> tuple[list[str], list[str]]:
    """(metric_ids, dimension_ids) of an exact_bundle profile (AD-2: manifest)."""
    caps = _load_manifest().get("source_capabilities", {})
    report = next(
        (r for r in caps.get("reports", []) if r.get("id") == profile_id), None
    )
    if report is None:
        raise ValueError(f"Unknown microsoft-ads report profile: {profile_id!r}")
    return list(report.get("metrics", [])), list(report.get("dimensions", []))


def _run_report(
    connection_id: str,
    report_type: str,
    metric_ids: list[str],
    dimension_ids: list[str],
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    data_level: str,
    account_id: str | None,
    *,
    report_store=None,
    clock=None,
    sleeper=None,
    deadline_seconds: int | None = None,
) -> dict:
    """Validate -> submit -> poll -> download -> transform -> land.

    Every pre-call gate fires BEFORE any token fetch or HTTP round-trip:
    excluded exposure, core field_compat rules (Column Restrictions +
    integral report enum, context={'report_type': ...}), the >=1 attribute /
    >=1 non-impression-share statistic invariant, date validation, column
    charset. The async mechanic runs EXCLUSIVELY through run_async_report
    (26.1): ReportRequestId persisted as report_ref, re-poll resumption,
    single resubmission, deadline -> retryable ProviderTransientError (the
    reference stays in flight; the queue retry resumes by re-poll).
    """
    from core.async_reports import run_async_report  # noqa: PLC0415
    from core.field_compat import enforce_selection  # noqa: PLC0415

    all_field_ids = list(dict.fromkeys(dimension_ids + metric_ids))

    # 1) Typed pre-call refusals (never a silent drop).
    _refuse_excluded_fields(all_field_ids)
    enforce_selection(
        {"metrics": metric_ids, "dimensions": dimension_ids},
        _load_compat_rules(),
        context={"report_type": report_type},
    )
    _enforce_request_invariants(metric_ids, dimension_ids)

    # 2) Resolve account + build the validated Submit body.
    acct, cust = _resolve_account_selection(connection_id, account_id)
    columns = _resolve_columns(report_type, all_field_ids)
    report_name = f"toorow{pull_id.replace('_', '')}"[:64]
    submit_body = build_submit_request(
        report_type, columns, acct, date_from, date_to, report_name
    )

    flow_impl = _ReportFlow(connection_id, submit_body, cust, acct)
    from core.async_reports import AsyncReportFlow  # noqa: PLC0415

    flow = AsyncReportFlow(
        submit=flow_impl.submit,
        poll=flow_impl.poll,
        download=flow_impl.download,
        request_hash=_request_hash(submit_body, cust),
    )

    kwargs: dict = {"ledger_ref": connection_id}
    if report_store is not None:
        kwargs["store"] = report_store
    if clock is not None:
        kwargs["clock"] = clock
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    if deadline_seconds is not None:
        kwargs["deadline_seconds"] = deadline_seconds

    outcome = run_async_report(flow, **kwargs)

    if outcome["status"] == "deferred":
        # CROSS-REVIEW FIX (pinterest review, orchestrator-imposed):
        # core/queue.py NEVER reads result["status"] -- a deferred RETURN of
        # {"status": "deferred", "row_count": 0} would be recorded as a done
        # job at 0 rows, and an off-ladder backfill window would NEVER be
        # re-pulled (permanent hole). Deferral is therefore a RETRYABLE
        # ProviderTransientError (amazon-ads pattern): the ReportRequestId
        # stays persisted in flight, the queue's standard retry re-enters
        # with the SAME request_hash and resumes by RE-POLL -- never a
        # resubmission, never a silent empty success.
        from core.pull_errors import ProviderTransientError  # noqa: PLC0415

        logger.info(
            "microsoft_ads_pull_deferred: pull_id=%s report_type=%s "
            "report_ref=%s", pull_id, report_type, outcome.get("report_ref"),
        )
        raise ProviderTransientError(
            provider_status=None,
            provider_payload={
                "report_ref": outcome.get("report_ref"),
                "request_hash": flow.request_hash,
            },
            message=(
                "microsoft-ads async report deferred (still generating past "
                "the per-run deadline or claim held by a concurrent run); "
                "the persisted ReportRequestId resumes by re-poll on retry "
                "(request_hash=" + flow.request_hash + ")"
            ),
        )

    raw_rows = outcome["rows"] or []
    canonical_rows = transform(raw_rows)
    metric_names = _canonical_metric_names(metric_ids)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count, non_numeric_landed = _insert_raw_rows(
        canonical_rows, metric_names, data_level, pull_id, loaded_at, project_id,
        _get_db_mode(), _get_duckdb_path(),
    )

    # AD-3: no token in logs -- only pull_id / counts (safe metadata).
    logger.info(
        "microsoft_ads_pull_completed: pull_id=%s report_type=%s row_count=%d "
        "resumed=%s", pull_id, report_type, row_count, outcome.get("resumed"),
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "status": "completed",
        "report_ref": outcome.get("report_ref"),
        "non_numeric_landed": non_numeric_landed,
    }


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    account_id: str | None = None,
    **injection,
) -> dict:
    """Run one exact_bundle profile through the shared async report core."""
    if profile not in _PROFILE_SPECS:
        raise ValueError(f"Unknown microsoft-ads report profile: {profile!r}")
    report_type, data_level = _PROFILE_SPECS[profile]
    metric_ids, dimension_ids = _profile_bundle(profile)
    return _run_report(
        connection_id, report_type, metric_ids, dimension_ids,
        date_from, date_to, project_id, pull_id, data_level, account_id,
        **injection,
    )


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    account_id: str | None = None,
    **injection,
) -> dict:
    """Default pull() = campaign grain (AI-58 profile-less default dispatch)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", account_id=account_id, **injection,
    )


# ---------------------------------------------------------------------------
# AI-58 per-profile dispatch: pull_<profile_id>, declared as dispatch.callable
# in the manifest capability reports. All delegate to the ONE code path.
# ---------------------------------------------------------------------------


def pull_campaign_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None, **injection,
) -> dict:
    """AI-58 dispatch: campaign grain (CampaignPerformance, data_level=CAMPAIGN)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", account_id=account_id, **injection,
    )


def pull_ad_group_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None, **injection,
) -> dict:
    """AI-58 dispatch: ad group grain (AdGroupPerformance, data_level=AD_GROUP)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="ad_group_daily", account_id=account_id, **injection,
    )


def pull_ad_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None, **injection,
) -> dict:
    """AI-58 dispatch: ad grain (AdPerformance, data_level=AD)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="ad_daily", account_id=account_id, **injection,
    )


def pull_keyword_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None, **injection,
) -> dict:
    """AI-58 dispatch: keyword grain (KeywordPerformance, data_level=KEYWORD)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="keyword_daily", account_id=account_id, **injection,
    )


def pull_search_query_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None, **injection,
) -> dict:
    """AI-58 dispatch: search query grain (SearchQueryPerformance)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="search_query_daily", account_id=account_id, **injection,
    )


# ---------------------------------------------------------------------------
# Story 26.4 -- catalog_driven execution: pull_catalog_daily.
# ---------------------------------------------------------------------------


def _infer_report_type(field_ids: list[str], metric_ids: list[str]) -> str:
    """The selection chooses the report type (25.8): the whitelisted types
    whose INTEGRAL column enums cover every selected field are the candidates.

    - No candidate: typed refusal listing the per-type missing fields (never
      a silent drop).
    - One candidate, or several sharing the SAME grain: the first candidate
      in the fixed preference order wins; the inferred type AND the discarded
      candidates are logged (F-2: the inference stays observable).
    - Several candidates of DIFFERENT grains AND at least one NON-ADDITIVE
      statistic selected (impression share, ratios, averages): a silent grain
      reinterpretation would change what the statistic MEANS (an
      impression-share at campaign grain is not the account-grain number), so
      an explicit ``report_type`` is required -- typed refusal listing the
      candidates (F-2, review 26.4). Purely additive selections keep the
      deterministic inference: the numbers sum identically whatever the grain.
    """
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    wanted = set(field_ids)
    candidates: list[str] = []
    misses: dict[str, list[str]] = {}
    for report_type in _REPORT_TYPE_PREFERENCE:
        allowed = _allowed_fields_for_report_type(report_type)
        missing = sorted(wanted - allowed)
        if missing:
            misses[report_type] = missing
        else:
            candidates.append(report_type)

    if not candidates:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload={"missing_by_report_type": misses},
            message=(
                "microsoft-ads catalog selection refused before the API call: "
                "no covered report type exposes every selected column "
                "together; closest misses: "
                + "; ".join(
                    f"{rt}: missing {', '.join(m[:5])}"
                    for rt, m in misses.items()
                )
            ),
        )

    if len(candidates) > 1:
        grains = {_REPORT_TYPE_DATA_LEVEL[rt] for rt in candidates}
        non_additive_selected = sorted(
            set(metric_ids) & _non_additive_statistic_field_ids()
        )
        if len(grains) > 1 and non_additive_selected:
            raise InvalidRequestError(
                provider_status=None,
                provider_payload={
                    "candidate_report_types": candidates,
                    "violations": [
                        {
                            "rule_id": "ambiguous-report-type-non-additive",
                            "kind": "request_invariant",
                            "message": (
                                "selection is covered by several report types "
                                "of different grains and carries non-additive "
                                "statistic(s); pass an explicit report_type"
                            ),
                            "fields": non_additive_selected,
                        }
                    ],
                },
                message=(
                    "microsoft-ads catalog selection refused before the API "
                    "call: rule=ambiguous-report-type-non-additive; the "
                    "selection is covered by SEVERAL report types of "
                    "different grains (" + ", ".join(candidates) + ") and "
                    "contains non-additive statistic(s) ("
                    + ", ".join(non_additive_selected)
                    + ") whose meaning depends on the grain -- pass an "
                    "explicit report_type to disambiguate"
                ),
            )

    chosen = candidates[0]
    if len(candidates) > 1:
        logger.info(
            "microsoft_ads_report_type_inferred: chosen=%s "
            "discarded_candidates=%s (purely additive or single-grain "
            "selection -- deterministic preference order applies)",
            chosen, ",".join(candidates[1:]),
        )
    return chosen


def _prune_default_selection(selection: dict, report_type: str) -> dict:
    """Prune the MODULE-RESOLVED tier-core default to the report type's enum.

    Only for the ``selection is None`` fallback: the tier-core default spans
    the whole catalog (ids of other grains, e.g. ad_group_id on a campaign
    report), so the module prunes it to the report enum and to numeric
    additive metrics, and logs what it dropped. A USER selection is NEVER
    pruned -- incompatibilities there are typed refusals.
    """
    allowed = _allowed_fields_for_report_type(report_type)
    by_id = _catalog_fields_by_id()

    kept_dims = [f for f in selection.get("dimensions", []) if f in allowed]
    kept_metrics = []
    for f in selection.get("metrics", []):
        entry = by_id.get(f, {})
        if (
            f in allowed
            and entry.get("physical_type") in ("integer", "decimal")
            and f not in _impression_share_field_ids()
        ):
            kept_metrics.append(f)
    dropped = sorted(
        set(selection.get("dimensions", []) + selection.get("metrics", []))
        - set(kept_dims) - set(kept_metrics)
    )
    if dropped:
        logger.info(
            "microsoft_ads_default_selection_pruned: dropped=%d fields outside "
            "%s or non-additive (default selection only): %s",
            len(dropped), report_type, ",".join(dropped),
        )
    return {"metrics": kept_metrics, "dimensions": kept_dims}


def _resolved_core_default_selection() -> dict:
    """The catalog tier-core default AS THE CORE QUEUE RESOLVES IT (cached).

    Story 26.6 Part B (amazon-ads F-1 pattern): core/queue.py:573-574 resolves a
    ``selection=None`` catalog_driven job to this exact shape
    (validate_selection(catalog, catalog_default_selection(catalog))) and passes
    it to the module as an EXPLICIT selection. The module must recognise it so
    it applies the SAME pruning it applies to selection=None (the tier-core
    default spans the whole catalog -- ids of other grains, non-additive
    ratios). Without this, the core-resolved default is treated as a genuine
    user selection and dies on the pre-call refusals at every scheduled run
    with no selection (proven on amazon-ads)."""
    cached = globals().get("_CORE_DEFAULT_SELECTION")
    if cached is None:
        from core.catalog_contract import (  # noqa: PLC0415
            catalog_default_selection,
            validate_selection,
        )

        catalog = _load_api_catalog()
        cached, _issues = validate_selection(
            catalog, catalog_default_selection(catalog)
        )
        globals()["_CORE_DEFAULT_SELECTION"] = cached
    return cached


def _is_resolved_core_default(selection: dict) -> bool:
    """True when *selection* is the core-resolved tier-core default (Part B).

    Compares the SETS of metric/dimension ids (order-independent): a genuine
    user selection with any different field membership is never treated as the
    prunable default (it stays a typed refusal on incompatibility)."""
    default = _resolved_core_default_selection()
    return (
        set(selection.get("metrics", []) or []) == set(default.get("metrics", []) or [])
        and set(selection.get("dimensions", []) or [])
        == set(default.get("dimensions", []) or [])
    )


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    account_id: str | None = None,
    report_type: str | None = None,
    **injection,
) -> dict:
    """Catalog-driven daily pull: the SubmitGenerateReport is BUILT FROM the
    selection (Jean, 25.8: the catalog IS the execution contract).

    core resolves+validates ``selection`` against api_catalog.json and passes
    ``{"metrics", "dimensions", "source_fields"}``. A ``None`` selection -- AND
    (Story 26.6 Part B) the tier-core default the core queue resolves in place
    of None and passes as an EXPLICIT selection (core/queue.py:573-574) -- fall
    back to the catalog tier-core default (pruned to the report type's enum). A
    genuine USER selection is NEVER pruned (typed refusals on incompatibility).
    Steps:

      1. resolve the selection (tier-core default when absent OR core-resolved,
         pruned);
      2. choose the report type: explicit ``report_type`` wins, else the
         selection chooses it (first covered type whose integral enum covers
         every selected field; several covering types of DIFFERENT grains +
         a non-additive statistic = typed refusal listing the candidates,
         F-2 -- silent grain reinterpretation would change the numbers);
      3. typed pre-call refusals BEFORE any token fetch: excluded exposure,
         core.field_compat.enforce_selection with context={'report_type': ...}
         (Column Restrictions mutually_exclusive + integral selectable_set),
         the >=1 attribute / >=1 non-impression-share statistic invariant,
         column charset, ISO dates;
      4. run the async report (run_async_report EXCLUSIVELY), parse the ZIP
         CSV, coerce by catalog physical_type, land LONG rows (selected
         non-structure dimensions -> segments_json).
    """
    if report_type is not None and report_type not in _REPORT_TYPE_DATA_LEVEL:
        raise ValueError(
            f"microsoft-ads: report type {report_type!r} is not one of the 8 "
            "whitelisted covered report types"
        )

    # Story 26.6 Part B (amazon-ads F-1 pattern): a catalog_driven job with NO
    # explicit selection is resolved by core/queue.py:573-574 to the tier-core
    # default (validate_selection(catalog, catalog_default_selection(catalog)))
    # and passed here as an EXPLICIT selection. Both selection=None AND that
    # resolved default must be treated as the prunable module default (spanning
    # every report type); a genuine USER selection is NEVER pruned.
    if selection is None or _is_resolved_core_default(selection):
        default_type = report_type or _PROFILE_SPECS["catalog_daily"][0]
        selection = _prune_default_selection(
            _resolved_core_default_selection(), default_type
        )
        report_type = default_type

    metric_ids = list(selection.get("metrics", []))
    dimension_ids = list(selection.get("dimensions", []))

    # Choose the report type FIRST (explicit wins, else the selection infers
    # it), THEN append that type's grain carriers (F-7: carriers are added
    # AFTER inference -- the guarantee is that every carrier belongs to the
    # chosen type's integral enum by construction of _CATALOG_STRUCTURE_FIELDS,
    # and _run_report re-checks the FULL column list against that enum via
    # the selectable_set rule before any API call).
    if report_type is None:
        report_type = _infer_report_type(
            list(dict.fromkeys(dimension_ids + metric_ids)), metric_ids
        )
    for fid in _CATALOG_STRUCTURE_FIELDS[report_type]:
        if fid not in dimension_ids and fid not in metric_ids:
            dimension_ids.append(fid)

    return _run_report(
        connection_id, report_type, metric_ids, dimension_ids,
        date_from, date_to, project_id, pull_id,
        _REPORT_TYPE_DATA_LEVEL[report_type], account_id, **injection,
    )


# ---------------------------------------------------------------------------
# Account topology (story 26.4, pattern 25.5): Customer -> Account.
# ---------------------------------------------------------------------------

_ACCOUNTS_PAGE_SIZE = 1000
_MAX_ACCOUNT_PAGES = 20


def _customer_headers(connection_id: str) -> dict:
    """Discovery headers: bearer + developer token (no account scope yet --
    CustomerId/CustomerAccountId are unknown before discovery; live behaviour
    of the Customer Management endpoints without them is a ROLLOUT_NOTES
    probe item)."""
    from core import nango_client  # noqa: PLC0415

    token = nango_client.get_fresh_token(connection_id, provider="microsoft-ads")
    return _headers(token)


def discover_accounts(connection_id: str) -> list[dict]:
    """List the Customer -> Account hierarchy the connection's token can reach.

    Customer Management v13 REST (dossier section 7):
      1. POST {cc}/User/Query {"UserId": null} -> current user id.
      2. POST {cc}/Accounts/Search predicate UserId Equals, paginated
         (PageInfo Size 1000) -> all accessible accounts (Id, Name, Number,
         ParentCustomerId, AccountLifeCycleStatus).

    Returns the generic nested list core's topology flow consumes: one node
    per parent customer with the advertiser accounts as children carrying the
    composite opaque id '<account_id>@<customer_id>' (ids opaque to core,
    AD-2) so pulls resolve the mandatory CustomerId header without
    re-discovery. Inactive accounts (AccountLifeCycleStatus not Active/Draft)
    are filtered out.
    """
    headers = _customer_headers(connection_id)
    base = _customer_mgmt_base_url()

    resp = httpx.post(
        f"{base}/User/Query", json={"UserId": None}, headers=headers, timeout=30.0
    )
    if resp.status_code != 200:
        _raise_provider_error(resp.status_code, resp)
    user_id = ((resp.json() or {}).get("User") or {}).get("Id")
    if user_id is None:
        raise ValueError("microsoft-ads: User/Query returned no User.Id")

    accounts: list[dict] = []
    page_index = 0
    while page_index < _MAX_ACCOUNT_PAGES:
        resp = httpx.post(
            f"{base}/Accounts/Search",
            json={
                "PageInfo": {"Index": page_index, "Size": _ACCOUNTS_PAGE_SIZE},
                "Predicates": [
                    {"Field": "UserId", "Operator": "Equals", "Value": str(user_id)}
                ],
            },
            headers=headers,
            timeout=30.0,
        )
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)
        page = (resp.json() or {}).get("Accounts") or []
        accounts.extend(a for a in page if isinstance(a, dict))
        if len(page) < _ACCOUNTS_PAGE_SIZE:
            break
        page_index += 1
    else:
        # F-5: page cap reached with the LAST PAGE STILL FULL -- the account
        # list is very likely incomplete. The topology return shape is a bare
        # node list (consumed as-is by core's topology flow), so there is no
        # side channel for a truncated flag: this explicit warning is the
        # observable signal (live >1000-account pagination is a ROLLOUT_NOTES
        # probe item).
        logger.warning(
            "microsoft_ads_discover_accounts_truncated: page cap %d reached "
            "with a full last page (%d accounts fetched) -- the account list "
            "is likely INCOMPLETE; raise _MAX_ACCOUNT_PAGES",
            _MAX_ACCOUNT_PAGES, len(accounts),
        )

    by_customer: dict[str, list[dict]] = {}
    for account in accounts:
        status = account.get("AccountLifeCycleStatus")
        if status and status not in ("Active", "Draft"):
            continue
        customer_id = str(account.get("ParentCustomerId") or "")
        by_customer.setdefault(customer_id, []).append(account)

    nodes: list[dict] = []
    for customer_id in sorted(by_customer):
        children = [
            {
                # Composite opaque id: pulls recover the CustomerId header
                # without a re-discovery (see _split_selected_account).
                "id": f"{account.get('Id')}@{customer_id}",
                "label": account.get("Name") or str(account.get("Id")),
                "level": "account",
                "account_number": account.get("Number"),
                "currency_code": account.get("CurrencyCode"),
                "status": account.get("AccountLifeCycleStatus"),
            }
            for account in sorted(
                by_customer[customer_id], key=lambda a: str(a.get("Id"))
            )
        ]
        nodes.append(
            {
                "id": customer_id,
                "label": f"Customer {customer_id}",
                "level": "customer",
                "children": children,
            }
        )

    logger.info(
        "microsoft_ads_discover_accounts: customers=%d accounts=%d",
        len(nodes), sum(len(n["children"]) for n in nodes),
    )
    return nodes


def check_account_access(connection_id: str, account_id: str) -> dict:
    """Access-check the SELECTED reporting account (story 26.4 / 25.5 step 3).

    Submits a 1-day tier-core CampaignPerformance report on the account
    (yesterday's window): a successful SubmitGenerateReport proves reporting
    access without waiting for the async generation (the ReportRequestId is
    returned as evidence and the report itself is discarded -- it expires
    after 2 days). Errors surface typed (105/109 auth, 106/2003 permission,
    207 rate-limited re-queue).
    """
    from datetime import timedelta  # noqa: PLC0415

    from core import nango_client  # noqa: PLC0415

    acct, cust = _split_selected_account(account_id)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    metric_ids, dimension_ids = _profile_bundle("catalog_daily")
    columns = _resolve_columns(
        "CampaignPerformanceReportRequest",
        list(dict.fromkeys(dimension_ids + metric_ids)),
    )
    submit_body = build_submit_request(
        "CampaignPerformanceReportRequest", columns, acct,
        yesterday, yesterday, "toorowaccesscheck",
    )
    token = nango_client.get_fresh_token(connection_id, provider="microsoft-ads")
    resp = httpx.post(
        f"{_reporting_base_url()}/GenerateReport/Submit",
        json=submit_body,
        headers=_headers(token, cust, acct),
        timeout=60.0,
    )
    if resp.status_code != 200:
        _raise_provider_error(resp.status_code, resp)
    return {
        "account_id": account_id,
        "customer_account_id": acct,
        "customer_id": cust,
        "ok": True,
        "report_request_id": (resp.json() or {}).get("ReportRequestId"),
    }


# ---------------------------------------------------------------------------
# MCP tool -- reads from fact_daily_kpi mart (AD-12).
# ---------------------------------------------------------------------------


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


def _get_mart_table(db_mode: str) -> str:
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
    WHERE connector = 'microsoft-ads'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    # AD-12: MCP server reads marts only -- never raw_* tables or CSV.
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
        data_by_metric.setdefault(r["metric"], []).append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    provenance = (
        {
            "source_system": "microsoft-ads",
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
def get_microsoft_ads_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Microsoft Ads Performance Report -- reads from fact_daily_kpi mart.

    Report profiles: campaign_daily, ad_group_daily, ad_daily, keyword_daily,
                     search_query_daily, catalog_daily
    Metrics: cost, impressions, clicks, conversions, revenue
             (additive at day grain)
    Breakdown dimensions: campaign_id, ad_group_id, ad_id

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    # AD-4: conversions/revenue are credited to the CLICK date and restated
    # by Microsoft for up to 90 days (refetch ladder 3/30/90).

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of the 6 declared profiles
        date_from: Start date ISO-8601 (e.g. '2026-04-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    from datetime import timedelta  # noqa: PLC0415

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
# Register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_microsoft_ads_daily", provider="microsoft-ads")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
