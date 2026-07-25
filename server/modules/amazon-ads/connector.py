"""Amazon Ads connector -- Story 26.5 (Reporting v3 async, 3 regions, 740 columns).

Exposes a ``mcp_app: FastMCP`` instance that the core loader mounts under the
``amazon-ads`` namespace (AD-2). Built to the epic-25 industrial standard:
generated api_catalog.json (740 official dictionary columns, ZERO planned;
DSP families excluded dsp-seat-gated), status-only error_map applied module
side, region -> profile topology, and the 26.1 async-report socle
(core/async_reports.py) driving every extraction.

# AD-12: the MCP tool reads the fact_daily_kpi mart only -- no raw_* tables.
# AD-3:  token obtained via nango_client.get_fresh_token immediately before
#        use (Nango, Login with Amazon). Never stored/logged.
# AD-7:  pull_id minted by the core scheduler and passed into pull().
# AD-2:  request column lists are DERIVED from the manifest/catalog -- no
#        hardcoded field lists outside the catalog. The API pin
#        (provider_api_version) and the 3 regional hosts live in manifest.json
#        and are read from there ONLY.
# AD-4:  ratio/derived columns carry a NON-ADDITIVE note in the catalog and
#        land RAW at day grain when selected -- never summed downstream.
# AI-03: ASCII-only stdout/log strings.
#
# API facts (research dossier amazon-ads-catalog-research.md, 2026-07-21):
#   - POST {region_host}/reporting/reports
#     (Content-Type: application/vnd.createasyncreportrequest.v3+json)
#     -> {"reportId", "status": "PENDING"}; 425 when an IDENTICAL request is
#     already in flight (reuse the pending reportId, NEVER blind-resubmit --
#     the 26.1 claim/request-hash dedup is the primary guard, a residual
#     provider 425 re-reads the persisted in-flight ref).
#   - GET {region_host}/reporting/reports/{id} -> PENDING | PROCESSING |
#     COMPLETED (+ pre-signed S3 url) | FAILED (+failureReason). Generation
#     can take up to 3 hours; transient 401s during polling are a DOCUMENTED
#     quirk (retried x5 before classifying auth_expired).
#   - GET {url} (NO auth headers) -> GZIP file -> JSON array of row objects.
#     Decompressed immediately at download.
#   - The 3 regional hosts are ISOLATED SILOS: a profileId is only valid on
#     its own region's host (cross-region = 4XX). Every call routes on the
#     host of the profile's stored region ('<REGION>:<profileId>' opaque id).
#   - 429 is dynamic (queue-size regional tiers) + Retry-After header ->
#     core.quota.RateLimitError(retry_after). Never in the error_map.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
mcp_app = FastMCP("amazon-ads")

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (no hardcoded paths).
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# Manifest / catalog access (cached). The API pin and the 3 regional hosts
# live in ONE place: manifest.json (story 26.5).
# ---------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).parent


def _load_json_cached(cache_key: str, relpath: str):
    cached = globals().get(cache_key)
    if cached is None:
        cached = json.loads((_MODULE_DIR / relpath).read_text(encoding="utf-8"))
        globals()[cache_key] = cached
    return cached


def _load_manifest() -> dict:
    return _load_json_cached("_MANIFEST", "manifest.json")


def _load_api_catalog() -> dict:
    return _load_json_cached("_API_CATALOG", "api_catalog.json")


def _load_catalog_sources() -> dict:
    return _load_json_cached("_CATALOG_SOURCES", "catalog_sources/catalog_sources.json")


def _load_report_type_columns() -> dict:
    return _load_json_cached(
        "_REPORT_TYPE_COLUMNS", "catalog_sources/report_type_columns.json"
    )


def _descriptive_mutable_fields() -> frozenset[str]:
    """Story 26.6 Part A: entity state/type attributes that land latest-wins.

    Read from the generator-emitted module-local artifact (the shared merge
    pipeline's OfficialField dataclass does not pass this flag through to
    api_catalog.json, and this module must not touch shared catalog_gen). Both
    the OFFICIAL dictionary id AND its post-transform canonical name are
    included so the split is correct whichever spelling a landed row carries.
    """
    cached = globals().get("_DESCRIPTIVE_MUTABLE")
    if cached is None:
        ids = _load_json_cached(
            "_DESCRIPTIVE_MUTABLE_IDS",
            "catalog_sources/descriptive_mutable_fields.json",
        )
        manifest = _load_manifest()
        dim_map = manifest.get("canonical_dimension_mapping", {})
        names: set[str] = set()
        for fid in ids:
            names.add(fid)
            mapped = dim_map.get(fid)
            if isinstance(mapped, str):
                names.add(mapped)
        cached = frozenset(names)
        globals()["_DESCRIPTIVE_MUTABLE"] = cached
    return cached


def _hosts() -> dict:
    """Regional API hosts, pinned in the manifest (single source, AD-2)."""
    return _load_manifest()["provider_api_hosts"]


def _report_type_specs() -> dict:
    """The 26 WHITELISTED sponsored report types (adProduct/groupBy/retention).

    dsp*/conversionPath/benchmark reportTypeIds are NOT in this map: they are
    seat-gated or non-daily shapes -- requesting one is a typed refusal.
    """
    return _load_catalog_sources()["report_type_compatibility"]["report_types"]


def _field_compat_rules() -> dict:
    """The module's validated core 26.1 field_compatibility block (cached)."""
    cached = globals().get("_COMPAT_RULES")
    if cached is None:
        from core import field_compat  # noqa: PLC0415

        cached = field_compat.load_rules(
            _load_catalog_sources()["field_compatibility"]
        )
        globals()["_COMPAT_RULES"] = cached
    return cached


# ---------------------------------------------------------------------------
# Provider error handling (story 26.5, dossier section 1.8).
#
# Amazon reporting errors carry NO stable numeric provider sub-codes: the body
# is {"code": ..., "details": "<free text>"}. DECISION (closes the dossier's
# open point for core/pull_errors.py): the manifest error_map uses HTTP-STATUS-
# ONLY keys ("401", "425", ...) and THIS MODULE applies them itself, with
# details-substring refinements first and core's pure-HTTP classify_http_error
# as the final fallback. Core's '<status>:<provider_code>' contract is
# untouched (no wildcard was added to core).
# ---------------------------------------------------------------------------

# Details-substring refinements (dossier 1.8 + Airbyte cross-check), applied
# in declaration order before any status-level mapping.
_DETAILS_REFINEMENTS: tuple[tuple[str, str], ...] = (
    ("KDP authors do not have access to Sponsored Brands functionality", "permission_denied"),
    ("Not authorized to access scope", "permission_denied"),
    ("Tactic T00020 is not supported for report API in marketplace", "invalid_request"),
    ("Report date is too far in the past", "invalid_request"),
)


def _canonical_error_classes() -> dict:
    from core import pull_errors  # noqa: PLC0415

    return {
        pull_errors.AUTH_EXPIRED: pull_errors.AuthExpiredError,
        pull_errors.AUTH_REVOKED: pull_errors.AuthRevokedError,
        pull_errors.PERMISSION_DENIED: pull_errors.PermissionDeniedError,
        pull_errors.INVALID_REQUEST: pull_errors.InvalidRequestError,
        pull_errors.PROVIDER_TRANSIENT: pull_errors.ProviderTransientError,
    }


def _extract_details(body) -> str:
    if isinstance(body, dict):
        details = body.get("details") or body.get("detail") or body.get("message")
        if isinstance(details, str):
            return details
        return json.dumps(body, sort_keys=True)
    return str(body or "")


def _raise_provider_error(status_code: int, resp: httpx.Response | None = None,
                          body=None) -> None:
    """Raise the canonical typed error for a non-2xx Amazon Ads response.

    429 -> core.quota.RateLimitError (breaker path, Retry-After honored --
    Amazon's throttling is dynamic with regional report-queue tiers).
    Everything else: details-substring refinement, then the manifest's
    STATUS-ONLY error_map ('<status>' keys, module-side decision), then
    core's pure-HTTP classify_http_error. Provider payload preserved.

    NOTE: 425 (duplicate in-flight report) must be handled by the SUBMIT
    callable (dedup, not an error); if it reaches here it maps to
    provider_transient per the manifest map.
    """
    if body is None and resp is not None:
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    if status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after = None
        if resp is not None:
            try:
                retry_after = int(resp.headers.get("Retry-After", "0")) or None
            except (ValueError, TypeError):
                retry_after = None
        raise RateLimitError("amazon-ads", retry_after)

    from core import pull_errors  # noqa: PLC0415

    classes = _canonical_error_classes()
    details = _extract_details(body)
    for substring, canonical in _DETAILS_REFINEMENTS:
        if substring in details:
            raise classes[canonical](provider_status=status_code, provider_payload=body)

    refined = (_load_manifest().get("error_map") or {}).get(str(status_code))
    exc_cls = classes.get(refined)
    if exc_cls is not None:
        raise exc_cls(provider_status=status_code, provider_payload=body)

    raise pull_errors.classify_http_error(status_code, body)


# ---------------------------------------------------------------------------
# Auth headers + regional routing.
# ---------------------------------------------------------------------------

# The LwA client id is a PLATFORM credential (env, never manifest, never
# per-user). The token comes from Nango right before use (AD-3).
_CLIENT_ID_ENV = "AMAZON_ADS_CLIENT_ID"

_CREATE_REPORT_CONTENT_TYPE = "application/vnd.createasyncreportrequest.v3+json"


def _client_id() -> str:
    client_id = os.environ.get(_CLIENT_ID_ENV)
    if not client_id:
        raise ValueError(
            f"{_CLIENT_ID_ENV} env var required (platform-level Login-with-Amazon "
            "client id for the Amazon-Ads-ClientId header; never per-user)"
        )
    return client_id


def _headers(token: str, profile_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Amazon-Ads-ClientId": _client_id(),
    }
    if profile_id:
        headers["Amazon-Advertising-API-Scope"] = str(profile_id)
    return headers


def _split_selected_account(account_id: str) -> tuple[str, str]:
    """Parse the opaque topology account id '<REGION>:<profileId>'.

    The region prefix is produced by discover_accounts so every later call
    routes on the profile's OWN regional host without a re-discovery (the 3
    hosts are isolated silos; cross-region profileId = 4XX). Core never
    interprets the id (AD-2).
    """
    raw = str(account_id).strip()
    if ":" in raw:
        region, profile_id = raw.split(":", 1)
        region = region.strip().upper()
        if region in _hosts() and profile_id.strip():
            return region, profile_id.strip()
    raise ValueError(
        "amazon-ads account id must be '<REGION>:<profileId>' with REGION in "
        + "/".join(sorted(_hosts()))
        + f" -- got {account_id!r} (run the topology discovery flow)"
    )


def _host_for_region(region: str) -> str:
    hosts = _hosts()
    if region not in hosts:
        raise ValueError(f"unknown amazon-ads region {region!r}; expected one of {sorted(hosts)}")
    return hosts[region]


def _resolve_profile_selection(
    connection_id: str, account_id: str | None
) -> tuple[str, str]:
    """Resolve (region, profile_id) for a pull.

    Explicit arg wins (tests / direct calls). Otherwise the ONLY source is the
    core topology scope (story 25.5 flow). There is NO env-var account
    fallback (AMAZON_ADS_*_ID is forbidden by doctrine).
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
        logger.warning("amazon_ads_account_resolution_failed: %s", type(exc).__name__)

    if not selected:
        raise ValueError(
            "no selected reporting profile for this amazon-ads connection -- run "
            "the account topology flow (discovery -> selection -> access check); "
            "env-var account fallbacks do not exist for this module"
        )
    return _split_selected_account(selected)


# ---------------------------------------------------------------------------
# Request-shape validation (pre-call refusals -- nothing is dropped silently).
# ---------------------------------------------------------------------------

# Official column charset (dictionary ids are strictly alphanumeric; e.g.
# '3pFeeAutomotive' starts with a digit but stays alphanumeric).
_COLUMN_CHARSET = re.compile(r"^[a-zA-Z0-9]+$")

# Review 26.5 F-4: the max window is PER REPORT TYPE (dossier section 2), NOT a
# uniform 31 (and the old uniform 95-day retention was wrong too). Both bounds
# are read from report_type_compatibility.report_types[<type>]. This is the v3
# DEFAULT window used ONLY when a type omits max_range_days (all sponsored types
# now declare it -- the fallback exists so a future type can't be unbounded).
_DEFAULT_MAX_DATE_RANGE_DAYS = 31

# Review 26.5 F-8: the retention floor is computed in UTC (Amazon report dates
# are day-grained and the provider's "too far in the past" boundary rolls at
# UTC midnight) with a 1-day safety margin so a pull launched near the boundary
# is never refused for a date the provider would still accept.
_RETENTION_FLOOR_MARGIN_DAYS = 1


def _max_range_days(spec: dict) -> int:
    return int(spec.get("max_range_days", _DEFAULT_MAX_DATE_RANGE_DAYS))


def _invalid_request(message: str, payload=None):
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    return InvalidRequestError(
        provider_status=None, provider_payload=payload, message=message
    )


def _validate_dates(date_from: str, date_to: str, report_type: str) -> None:
    """Validate the window BEFORE any API call (typed invalid_request).

    - ISO dates (date.fromisoformat), from <= to;
    - max window PER REPORT TYPE (dossier section 2: 31 for most, but
      spGrossAndInvalids 365, sbPurchasedProduct 731, prompt-ad-extension 90);
    - retention floor PER REPORT TYPE (SB 60 / SD 65 / SP-ST 95 /
      spGrossAndInvalids 365 / sbPurchasedProduct 731), computed in UTC with a
      1-day margin (F-8): 'Report date is too far in the past' is refused HERE,
      never round-tripped.
    """
    spec = _report_type_specs()[report_type]
    try:
        parsed_from = date.fromisoformat(str(date_from))
        parsed_to = date.fromisoformat(str(date_to))
    except (TypeError, ValueError) as exc:
        raise _invalid_request(
            f"amazon-ads dates must be ISO-8601 YYYY-MM-DD: {date_from!r}..{date_to!r}"
        ) from exc
    if parsed_from > parsed_to:
        raise _invalid_request(
            f"amazon-ads date_from {date_from} is after date_to {date_to}"
        )
    max_range = _max_range_days(spec)
    if (parsed_to - parsed_from).days + 1 > max_range:
        raise _invalid_request(
            f"amazon-ads window {date_from}..{date_to} exceeds the {report_type} "
            f"maximum of {max_range} days per report request (dossier section 2)"
        )
    retention_days = int(spec["retention_days"])
    # F-8: UTC 'today' + a 1-day margin (the boundary rolls at UTC midnight; the
    # margin keeps a near-boundary pull from being refused for an accepted date).
    today_utc = datetime.now(tz=timezone.utc).date()
    floor = today_utc - timedelta(days=retention_days + _RETENTION_FLOOR_MARGIN_DAYS)
    if parsed_from < floor:
        raise _invalid_request(
            f"amazon-ads {report_type} retention floor is {retention_days} days "
            f"(UTC, +{_RETENTION_FLOOR_MARGIN_DAYS}d margin): date_from {date_from} "
            f"is older than {floor.isoformat()} (refused pre-call; the provider "
            "would 400 'Report date is too far in the past')"
        )


def _validate_report_type_and_group_by(report_type: str, group_by: list[str]) -> dict:
    """Whitelist the reportTypeId and its groupBy values (typed refusal)."""
    specs = _report_type_specs()
    spec = specs.get(report_type)
    if spec is None:
        raise _invalid_request(
            f"amazon-ads reportTypeId {report_type!r} is not extractable: the "
            "module whitelists the sponsored report types "
            f"({', '.join(sorted(specs))}); dsp*/conversionPath/benchmark shapes "
            "are seat-gated or non-daily (catalog exclusion reasons)"
        )
    legal = spec["groupBy"]
    illegal = [g for g in group_by if g not in legal]
    if not group_by or illegal:
        raise _invalid_request(
            f"amazon-ads {report_type} groupBy must be a non-empty subset of "
            f"{legal}; got {group_by!r}"
        )
    return spec


def _validate_columns(report_type: str, group_by: list[str], columns: list[str]) -> None:
    """Refuse any column the official dictionary does not list for the type.

    Charset guard first (^[a-zA-Z0-9]+$), then the core 26.1 field_compat
    selectable_set enforcement with the {'report_type', 'group_by'} context
    (single-groupBy configs also get the narrower per-groupBy page rules).
    NO SILENT DROP: a selected column either lands or is refused typed here.
    """
    from core import field_compat  # noqa: PLC0415

    bad_charset = sorted(c for c in columns if not _COLUMN_CHARSET.match(str(c)))
    if bad_charset:
        raise _invalid_request(
            "amazon-ads column id(s) violate the dictionary charset "
            "^[a-zA-Z0-9]+$: " + ", ".join(repr(c) for c in bad_charset)
        )
    if not columns:
        raise _invalid_request("amazon-ads report request needs at least one column")

    context = {"report_type": report_type}
    if len(group_by) == 1:
        context["group_by"] = group_by[0]
    field_compat.enforce_selection(list(columns), _field_compat_rules(), context=context)


def _build_report_body(
    report_type: str,
    group_by: list[str],
    columns: list[str],
    date_from: str,
    date_to: str,
) -> dict:
    """Build the validated createasyncreportrequest v3 body (dossier 1.2)."""
    spec = _validate_report_type_and_group_by(report_type, group_by)
    _validate_dates(date_from, date_to, report_type)

    ordered: list[str] = []
    for column in columns:
        if column not in ordered:
            ordered.append(column)
    if "date" not in ordered:
        # timeUnit DAILY REQUIRES the 'date' column (v3 rule).
        ordered.insert(0, "date")
    _validate_columns(report_type, group_by, ordered)

    return {
        "name": f"toorow {report_type} {date_from} {date_to}",
        "startDate": date_from,
        "endDate": date_to,
        "configuration": {
            "adProduct": spec["adProduct"],
            "reportTypeId": report_type,
            "groupBy": list(group_by),
            "columns": ordered,
            "timeUnit": "DAILY",
            "format": "GZIP_JSON",
        },
    }


def _request_hash(region: str, profile_id: str, body: dict) -> str:
    """Deterministic hash of the FULL request shape (socle dedup key).

    The 'name' field is EXCLUDED (it carries no shape) so re-runs of the same
    window hash identically and resume the in-flight report by re-poll.
    """
    shape = {
        "region": region,
        "profile_id": str(profile_id),
        "startDate": body["startDate"],
        "endDate": body["endDate"],
        "configuration": body["configuration"],
    }
    canonical = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The async flow callables (26.1 socle: submit -> poll -> download).
# ---------------------------------------------------------------------------

# Poll-time transient-401 budget (documented provider quirk; Airbyte retries 5).
_POLL_401_MAX_RETRIES = 5

# failureReason substrings that mean CONFIGURATION (typed invalid_request
# instead of the expired->single-resubmission path). Review 26.5 F-9: RESTRICTED
# to the forms DOCUMENTED in the dossier (section 1.8 error taxonomy + section 5
# Airbyte creation-config substrings) -- the request-shape tokens Amazon uses in
# a FAILED report's failureReason ("column"/"groupBy"/"filter") plus the exact
# documented config phrases. Generic "invalid"/"not supported"/"unsupported"
# were REMOVED: they over-matched transient provider messages ("temporarily
# unavailable", "invalid state") and wrongly denied the single-resubmission
# path. An undocumented reason now correctly takes the resubmit-once ->
# provider_transient route (never a silent success).
_FAILURE_CONFIG_MARKERS = (
    "column",
    "groupBy",
    "filter",
    "Report date is too far in the past",
    "Tactic T00020 is not supported for report API in marketplace",
    "Not authorized to access scope",
    "KDP authors do not have access to Sponsored Brands functionality",
)

_HTTP_TIMEOUT = 60.0


def _build_flow(
    connection_id: str,
    region: str,
    profile_id: str,
    body: dict,
):
    """Return an AsyncReportFlow for one report request.

    submit: POST /reporting/reports. A residual provider 425 reuses the
        reportId the 425 BODY carries -- never a blind resubmission (F-6: the
        socle's own claim/request-hash dedup re-polls any persisted in-flight
        ref before submit() runs, so submit() only fires when this run holds
        the claim). When no reportId is recoverable the 425 is a typed
        provider_transient (retryable; the next run resumes through the store).
    poll:   GET /reporting/reports/{id} with the transient-401 budget (x5),
        mapping PENDING/PROCESSING/COMPLETED to the canonical statuses,
        purged refs (404) to 'expired' (socle F-3 contract) and FAILED to
        'expired' (socle single-resubmission, then provider_transient) unless
        the failureReason is configuration (typed invalid_request).
    download: GET the pre-signed S3 url (NO auth headers), GZIP decompressed
        IMMEDIATELY, parsed as the JSON row array. A dead link raises
        DownloadLinkExpired (socle single-resubmission policy).
    """
    from core import async_reports, nango_client  # noqa: PLC0415

    host = _host_for_region(region)
    reports_url = f"{host}/reporting/reports"
    state: dict = {"download_url": None, "poll_401_count": 0}

    def _token() -> str:
        # AD-3: fetched immediately before use, never stored.
        return nango_client.get_fresh_token(connection_id, provider="amazon-ads")

    def submit() -> str:
        headers = _headers(_token(), profile_id)
        headers["Content-Type"] = _CREATE_REPORT_CONTENT_TYPE
        resp = httpx.post(reports_url, json=body, headers=headers, timeout=_HTTP_TIMEOUT)
        if resp.status_code in (200, 202):
            payload = resp.json()
            report_ref = payload.get("reportId")
            if not report_ref:
                raise _invalid_request(
                    "amazon-ads report creation returned no reportId", payload
                )
            return str(report_ref)
        if resp.status_code == 425:
            # Duplicate in-flight report. Review 26.5 F-6: the socle's OWN
            # claim/request-hash dedup is the primary guard -- an in-flight ref
            # for this (ledger_ref, request_hash) is RE-POLLED by the socle
            # BEFORE submit() is ever called, so submit() only runs when THIS
            # run holds the claim and the store row is 'submitting' (never
            # 'in_flight'). A residual provider 425 at that point means the
            # provider knows an identical request from a crashed/external owner
            # whose reference we never persisted. The ONLY recoverable, TESTED
            # reference is the reportId the 425 body itself carries -- reuse it
            # (never a blind resubmission). The earlier store re-read branch was
            # removed: it was unreachable in single-writer runs (the socle
            # already re-polls in_flight refs) and untestable deterministically.
            try:
                payload = resp.json()
            except Exception:
                payload = resp.text
            duplicate_ref = payload.get("reportId") if isinstance(payload, dict) else None
            if duplicate_ref:
                logger.warning(
                    "amazon_ads_425_reused_provider_ref: region=%s profile_id=%s",
                    region, profile_id,
                )
                return str(duplicate_ref)
            from core.pull_errors import ProviderTransientError  # noqa: PLC0415

            raise ProviderTransientError(
                provider_status=425,
                provider_payload=payload,
                message=(
                    "amazon-ads 425 duplicate report with no recoverable "
                    "reference -- retry resumes through the report-ref store"
                ),
            )
        _raise_provider_error(resp.status_code, resp)
        raise AssertionError("unreachable")  # pragma: no cover

    def poll(report_ref: str) -> str:
        resp = httpx.get(
            f"{reports_url}/{report_ref}",
            headers=_headers(_token(), profile_id),
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 401:
            # DOCUMENTED provider quirk: transient 401s during report-status
            # polling. Retry up to 5 before classifying auth_expired.
            state["poll_401_count"] += 1
            if state["poll_401_count"] <= _POLL_401_MAX_RETRIES:
                logger.warning(
                    "amazon_ads_poll_transient_401: attempt=%d/%d report_ref=%s",
                    state["poll_401_count"], _POLL_401_MAX_RETRIES, report_ref,
                )
                return async_reports.PROCESSING
            _raise_provider_error(401, resp)
        state["poll_401_count"] = 0
        if resp.status_code == 404:
            # Purged/unknown reference on a previously valid ref: canonical
            # 'expired' (socle F-3 -> single resubmission policy).
            logger.warning("amazon_ads_poll_unknown_ref: report_ref=%s", report_ref)
            return async_reports.EXPIRED
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)
        payload = resp.json()
        status = str(payload.get("status", "")).upper()
        if status == "PENDING":
            return async_reports.PENDING
        if status == "PROCESSING":
            return async_reports.PROCESSING
        if status == "COMPLETED":
            state["download_url"] = payload.get("url")
            return async_reports.COMPLETED
        if status == "FAILED":
            reason = str(payload.get("failureReason") or "")
            if any(marker.lower() in reason.lower() for marker in _FAILURE_CONFIG_MARKERS):
                raise _invalid_request(
                    f"amazon-ads report FAILED with a configuration failureReason: {reason}",
                    payload,
                )
            # Non-configuration failure: canonical 'expired' -> the socle
            # resubmits ONCE, then raises provider_transient (story contract:
            # FAILED -> single resubmission, then provider_transient).
            logger.warning(
                "amazon_ads_report_failed_resubmit_path: report_ref=%s reason=%s",
                report_ref, reason[:200],
            )
            return async_reports.EXPIRED
        raise _invalid_request(
            f"amazon-ads poll returned an unknown status {status!r}", payload
        )

    def download(report_ref: str):
        url = state.get("download_url")
        if not url:
            # Resumed run whose COMPLETED poll response carried no url.
            raise async_reports.DownloadLinkExpired(
                f"no download url for report_ref={report_ref}"
            )
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT)  # pre-signed: NO auth headers
        if resp.status_code in (401, 403, 404):
            # Pre-signed link is short-lived: dead link == expired (socle
            # single-resubmission policy).
            raise async_reports.DownloadLinkExpired(
                f"amazon-ads download link dead (HTTP {resp.status_code})"
            )
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)
        raw = resp.content
        # GZIP_JSON: decompress IMMEDIATELY (httpx already strips any
        # transport Content-Encoding; the FILE itself is gzip -- magic check
        # keeps already-decoded bodies parseable).
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        rows = json.loads(raw.decode("utf-8"))
        if not isinstance(rows, list):
            raise _invalid_request(
                "amazon-ads report payload is not a JSON array", str(type(rows))
            )
        return rows

    flow_request_hash = _request_hash(region, profile_id, body)
    return async_reports.AsyncReportFlow(
        submit=submit, poll=poll, download=download, request_hash=flow_request_hash
    )


# ---------------------------------------------------------------------------
# transform() -- manifest-driven canonical field mapping (AD-2).
# ---------------------------------------------------------------------------

# No fields are dropped at transform: ratio/derived columns land RAW at day
# grain with their catalog NON-ADDITIVE note (AD-4) -- semantic layer computes.
_DROP_FIELDS: set[str] = set()


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map report columns to canonical names using the manifest mappings.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_mapping
    (unitsSold -> units_sold, campaignId -> campaign_id, ...). Columns absent
    from both mappings pass through unchanged (catalog selections keep their
    official dictionary ids).
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
            if key in _DROP_FIELDS:
                continue
            canonical[rename_map.get(key, key)] = value
        result.append(canonical)
    return result


def _canonical_metric_names(metric_field_ids: list[str]) -> list[str]:
    """Post-transform landed names for the selected metric column ids."""
    mapping = _load_manifest().get("canonical_metric_mapping", {})
    names: list[str] = []
    for field_id in metric_field_ids:
        val = mapping.get(field_id, field_id)
        if isinstance(val, dict):
            val = val.get("canonical", field_id)
        names.append(val)
    return names


def _catalog_fields_by_id() -> dict:
    cached = globals().get("_CATALOG_BY_ID")
    if cached is None:
        cached = {
            f["field_id"]: f for f in _load_api_catalog().get("fields", [])
        }
        globals()["_CATALOG_BY_ID"] = cached
    return cached


def _numeric_field_ids(field_ids: list[str]) -> list[str]:
    """Column ids whose CATALOG physical_type is numeric (integer/decimal).

    Unit/coercion decisions are driven by the catalog physical_type, NEVER by
    an id suffix (26.2 review lesson). Non-numeric selections land in
    segments_json -- observable, never silently dropped.
    """
    by_id = _catalog_fields_by_id()
    return [
        fid for fid in field_ids
        if by_id.get(fid, {}).get("physical_type") in ("integer", "decimal")
    ]


# ---------------------------------------------------------------------------
# Raw landing -- LONG format: one row per grain x metric (story 26.5).
# Wide columns cannot hold an arbitrary 740-column selection, so the landing
# is (grain keys..., segments_json, metric, value_num) with data_level =
# reportTypeId + ad_product + region/profile. dbt staging supersedes per
# grain x metric (QUALIFY, AD-7).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_amazon_ads_daily (
    date                  VARCHAR,
    data_level            VARCHAR,
    ad_product            VARCHAR,
    region                VARCHAR,
    profile_id            VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    ad_group_id           VARCHAR,
    ad_group_name         VARCHAR,
    ad_id                 VARCHAR,
    keyword_id            VARCHAR,
    search_term           VARCHAR,
    advertised_asin       VARCHAR,
    purchased_asin        VARCHAR,
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

# Story 26.6 Part A: attributes_json holds entity STATE/TYPE mutable attributes
# (campaignStatus/adStatus/...). It is landed latest-wins and is NEVER part of
# the dbt supersede grain (stg PARTITION BY excludes it) -- a status flip on a
# re-pulled day must NOT mint a second grain key (F-2 double-count).
_RAW_INSERT_SQL = """
INSERT INTO raw_amazon_ads_daily
    (date, data_level, ad_product, region, profile_id, campaign_id,
     campaign_name, ad_group_id, ad_group_name, ad_id, keyword_id, search_term,
     advertised_asin, purchased_asin, segments_json, attributes_json, metric,
     value_num, cost_source_currency, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# (landed column, post-transform candidate keys -- canonical first, official
# dictionary id as fallback for catalog selections outside the manifest map).
_STRUCTURE_LOOKUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("campaign_id", ("campaign_id", "campaignId")),
    ("campaign_name", ("campaign_name", "campaignName")),
    ("ad_group_id", ("adGroupId", "ad_group_id")),
    ("ad_group_name", ("adGroupName", "ad_group_name")),
    ("ad_id", ("adId", "ad_id")),
    ("keyword_id", ("keywordId", "keyword_id")),
    ("search_term", ("searchTerm", "search_term")),
    ("advertised_asin", ("advertisedAsin", "advertised_asin")),
    ("purchased_asin", ("purchasedAsin", "purchased_asin")),
)

_NON_SEGMENT_KEYS = {
    "date", "campaign_id", "campaignId", "campaign_name", "campaignName",
    "adGroupId", "ad_group_id", "adGroupName", "ad_group_name", "adId",
    "ad_id", "keywordId", "keyword_id", "searchTerm", "search_term",
    "advertisedAsin", "advertised_asin", "purchasedAsin", "purchased_asin",
    "cost_source_currency", "campaignBudgetCurrencyCode", "pull_id",
    "connector", "data_level", "startDate", "endDate",
}


def _first_present(row: dict, keys: tuple[str, ...]):
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def _insert_raw_rows(
    rows: list[dict],
    metric_names: list[str],
    data_level: str,
    ad_product: str,
    region: str,
    profile_id: str,
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Long-ify canonical wide rows and insert into raw_amazon_ads_daily.

    One long row per (grain, metric) with value_num. NULL honnete (AD-9): an
    absent metric lands NO row (absence stays distinguishable from a recorded
    zero). Non-structural dimension values land in segments_json (canonical
    sorted-key JSON) so any catalog column participates in the supersede grain
    without schema churn -- EXCEPT the entity STATE/TYPE mutable attributes
    (Story 26.6 Part A / F-2: campaignStatus/adStatus/...), which land in a
    SEPARATE attributes_json column (latest-wins) and are kept OUT of the dbt
    supersede grain so a status flip on a re-pulled day does not double-count.
    """
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )
    from core import warehouse_write  # noqa: PLC0415

    metric_set = list(dict.fromkeys(metric_names))
    mutable_attrs = _descriptive_mutable_fields()
    values: list[tuple] = []
    for row in rows:
        # Non-key, non-metric dimension values. Story 26.6 Part A splits them:
        # entity STATE/TYPE mutable attributes -> attributes_json (latest-wins,
        # OUT of the supersede grain); everything else -> segments_json (grain).
        segment_items = {}
        attribute_items = {}
        for k in row:
            if k in _NON_SEGMENT_KEYS or k in metric_set or row[k] is None:
                continue
            if k in mutable_attrs:
                attribute_items[k] = row[k]
            else:
                segment_items[k] = row[k]
        segments_json = (
            json.dumps(segment_items, sort_keys=True, default=str)
            if segment_items else None
        )
        attributes_json = (
            json.dumps(attribute_items, sort_keys=True, default=str)
            if attribute_items else None
        )
        currency = _first_present(
            row, ("cost_source_currency", "campaignBudgetCurrencyCode")
        )
        structure = {
            landed: _first_present(row, keys) for landed, keys in _STRUCTURE_LOOKUPS
        }
        for metric in metric_set:
            value = row.get(metric)
            if value is None:
                continue
            try:
                value_num = float(value)
            except (ValueError, TypeError):
                # Provider drift: a catalog-numeric column arrived non-numeric.
                # Keep it observable in segments_json, never silently dropped.
                logger.warning(
                    "amazon_ads_non_numeric_metric_value: metric=%s", metric
                )
                continue
            values.append(
                (
                    str(row.get("date", "")),
                    data_level,
                    ad_product,
                    region,
                    str(profile_id),
                    _coerce_str(structure["campaign_id"]),
                    _coerce_str(structure["campaign_name"]),
                    _coerce_str(structure["ad_group_id"]),
                    _coerce_str(structure["ad_group_name"]),
                    _coerce_str(structure["ad_id"]),
                    _coerce_str(structure["keyword_id"]),
                    _coerce_str(structure["search_term"]),
                    _coerce_str(structure["advertised_asin"]),
                    _coerce_str(structure["purchased_asin"]),
                    segments_json,
                    attributes_json,
                    metric,
                    value_num,
                    _coerce_str(currency) or None,
                    pull_id,
                    loaded_at,
                    project_id,
                )
            )

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_RAW_CREATE_DDL)
    if values:
        con.executemany(_RAW_INSERT_SQL, values)
    con.close()
    return len(values)


def _coerce_str(value) -> str:
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# Shared pull core.
# ---------------------------------------------------------------------------

# Profile -> (reportTypeId, groupBy). Column lists come from the manifest
# capability reports (AD-2), never hardcoded here.
_PROFILE_SPECS: dict[str, tuple[str, list[str]]] = {
    "sp_campaigns_daily": ("spCampaigns", ["campaign"]),
    "sb_campaigns_daily": ("sbCampaigns", ["campaign"]),
    "sd_campaigns_daily": ("sdCampaigns", ["campaign"]),
    "catalog_daily": ("spCampaigns", ["campaign"]),
}


def _profile_columns(profile_id: str) -> tuple[list[str], list[str]]:
    """(dimension_ids, metric_ids) of an exact_bundle profile (manifest-driven)."""
    caps = _load_manifest().get("source_capabilities", {})
    report = next(
        (r for r in caps.get("reports", []) if r.get("id") == profile_id), None
    )
    if report is None:
        raise ValueError(f"Unknown amazon-ads report profile: {profile_id!r}")
    return list(report.get("dimensions", [])), list(report.get("metrics", []))


def _run_report(
    connection_id: str,
    region: str,
    profile_id: str,
    body: dict,
    *,
    _store=None,
    _clock=None,
    _sleeper=None,
    _deadline_seconds=None,
) -> tuple[list[dict], str, bool]:
    """Drive the 26.1 socle for one report; return (rows, report_ref, resumed).

    A DEFERRED outcome (provider still generating past the per-run deadline,
    or a concurrent run holds the submit claim) raises a RETRYABLE
    ProviderTransientError: the reference stays persisted in flight, so the
    queue's normal retry resumes by RE-POLL (request-hash dedup) -- never a
    resubmission, never a silent empty success.
    """
    from core import async_reports  # noqa: PLC0415
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    if _store is None:
        _store = async_reports.PostgresReportRefStore()
    ledger_ref = f"{connection_id}:{region}:{profile_id}"
    flow = _build_flow(connection_id, region, profile_id, body)

    kwargs: dict = {"ledger_ref": ledger_ref, "store": _store}
    if _clock is not None:
        kwargs["clock"] = _clock
    if _sleeper is not None:
        kwargs["sleeper"] = _sleeper
    if _deadline_seconds is not None:
        kwargs["deadline_seconds"] = _deadline_seconds

    result = async_reports.run_async_report(flow, **kwargs)
    if result["status"] == "deferred":
        raise ProviderTransientError(
            provider_status=None,
            provider_payload={"report_ref": result.get("report_ref")},
            message=(
                "amazon-ads async report deferred (still generating or claim "
                "held by a concurrent run); the persisted reference resumes by "
                "re-poll on retry (request_hash=" + flow.request_hash + ")"
            ),
        )
    return result["rows"], result["report_ref"], bool(result.get("resumed"))


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    account_id: str | None = None,
    **_test_hooks,
) -> dict:
    """Run one exact_bundle report profile and land rows in raw_amazon_ads_daily.

    # AD-12: called by the queue worker only.
    # AD-3: token used immediately (inside the flow callables) then discarded.
    # AD-7: pull_id minted by the caller.
    """
    if profile not in _PROFILE_SPECS:
        raise ValueError(f"Unknown amazon-ads report profile: {profile!r}")
    report_type, group_by = _PROFILE_SPECS[profile]

    region, amazon_profile_id = _resolve_profile_selection(connection_id, account_id)
    dims, metrics = _profile_columns(profile)
    body = _build_report_body(report_type, group_by, dims + metrics, date_from, date_to)

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    api_rows, report_ref, resumed = _run_report(
        connection_id, region, amazon_profile_id, body, **_test_hooks
    )

    canonical_rows = transform(api_rows)
    metric_names = _canonical_metric_names(_numeric_field_ids(metrics))
    spec = _report_type_specs()[report_type]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, metric_names, report_type, spec["adProduct"], region,
        amazon_profile_id, pull_id, loaded_at, project_id, db_mode, duckdb_path,
    )

    # AD-3: no token in log -- only pull_id / counts (safe metadata).
    logger.info(
        "amazon_ads_pull_completed: pull_id=%s profile=%s report_type=%s "
        "row_count=%d resumed=%s",
        pull_id, profile, report_type, row_count, resumed,
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "report_ref": report_ref,
        "resumed": resumed,
    }


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    account_id: str | None = None,
    **_test_hooks,
) -> dict:
    """Default pull() = SP campaign grain (AI-58 profile-less default dispatch)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="sp_campaigns_daily", account_id=account_id, **_test_hooks,
    )


def pull_sp_campaigns_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None, **_test_hooks,
) -> dict:
    """AI-58 dispatch: spCampaigns x campaign (data_level=spCampaigns)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="sp_campaigns_daily", account_id=account_id, **_test_hooks,
    )


def pull_sb_campaigns_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None, **_test_hooks,
) -> dict:
    """AI-58 dispatch: sbCampaigns x campaign (data_level=sbCampaigns)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="sb_campaigns_daily", account_id=account_id, **_test_hooks,
    )


def pull_sd_campaigns_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None, **_test_hooks,
) -> dict:
    """AI-58 dispatch: sdCampaigns x campaign (data_level=sdCampaigns)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="sd_campaigns_daily", account_id=account_id, **_test_hooks,
    )


# ---------------------------------------------------------------------------
# catalog_driven execution: pull_catalog_daily (story 26.5).
#
# The catalog IS the execution contract (Jean, 25.8): any cataloged EXPOSED
# column a datastream selects must actually be extracted. core validates the
# selection against api_catalog.json (existence) and passes selection=
# {"metrics", "dimensions", "source_fields"}; here the selection is resolved
# to ONE reportTypeId + groupBy + columns, validated pre-call (exposure,
# report-type membership, dates), then driven through the async socle.
# ---------------------------------------------------------------------------

# Deterministic resolution order: default bundle grains first, then the rest
# of the sponsored whitelist (declaration order of catalog_sources.json).
_REPORT_TYPE_PREFERENCE = (
    "spCampaigns", "sbCampaigns", "sdCampaigns", "spTargeting", "spSearchTerm",
    "spAdvertisedProduct", "spPurchasedProduct", "sbAdGroup", "sbAds",
    "sbCampaignPlacement", "sbTargeting", "sbSearchTerm", "sbPurchasedProduct",
    "sdAdGroup", "sdTargeting", "sdAdvertisedProduct", "sdPurchasedProduct",
    "stCampaigns", "stTargeting", "spGrossAndInvalids", "sbGrossAndInvalids",
    "sdGrossAndInvalids", "spPromptAdExtension", "sbPromptAdExtension",
    "spAudiences", "sbAudiences",
)

# Time/window columns never decide the report type (present everywhere).
_TYPE_NEUTRAL_COLUMNS = {"date", "startDate", "endDate"}


def _refuse_excluded_columns(field_ids: list[str]) -> None:
    """A selected excluded column is a TYPED refusal carrying its reason."""
    by_id = _catalog_fields_by_id()
    refusals = []
    for fid in field_ids:
        entry = by_id.get(fid)
        if entry is not None and entry.get("exposure") == "excluded":
            refusals.append(f"{fid} ({entry.get('exclusion_reason', 'excluded')})")
    if refusals:
        raise _invalid_request(
            "amazon-ads selection includes excluded catalog column(s) -- "
            "refused before any API call: " + "; ".join(sorted(refusals))
        )


def _resolve_report_type_for_selection(
    field_ids: list[str], report_type: str | None
) -> tuple[str, list[str]]:
    """Resolve (reportTypeId, groupBy) covering EVERY selected column.

    Explicit report_type wins (validated). Otherwise the first report type of
    the deterministic preference order whose official dictionary set covers
    the whole selection is used. No candidate covering the selection => typed
    refusal listing the best candidate's missing columns (never a silent drop).
    """
    specs = _report_type_specs()
    columns_by_type = _load_report_type_columns()
    deciding = [f for f in field_ids if f not in _TYPE_NEUTRAL_COLUMNS]

    if report_type is not None:
        spec = specs.get(report_type)
        if spec is None:
            raise _invalid_request(
                f"amazon-ads reportTypeId {report_type!r} is not in the sponsored whitelist"
            )
        return report_type, [spec["groupBy"][0]]

    best: tuple[int, str, list[str]] | None = None
    for candidate in _REPORT_TYPE_PREFERENCE:
        allowed = set(columns_by_type.get(candidate, ()))
        missing = sorted(set(deciding) - allowed)
        if not missing:
            return candidate, [specs[candidate]["groupBy"][0]]
        if best is None or len(missing) < best[0]:
            best = (len(missing), candidate, missing)

    assert best is not None
    raise _invalid_request(
        "amazon-ads: no single sponsored report type covers the selection; "
        f"closest is {best[1]!r} missing column(s): " + ", ".join(best[2])
    )


def _covering_group_by(report_type: str, field_ids: list[str]) -> str:
    """Pick the groupBy that UNLOCKS the most of *field_ids* for *report_type*.

    Mirrors the field_compat context ``_build_report_body`` uses: when a report
    type has per-groupBy selectable_set narrowings (e.g. spCampaigns
    campaign/adGroup/campaignPlacement), a blind ``group_by[0]`` can refuse a
    default column that a DIFFERENT groupBy would allow. We therefore choose the
    legal groupBy whose {report_type, group_by} allowed set covers the MOST
    selected fields (ties broken by the spec's declaration order = the first,
    canonical groupBy). Report types without per-groupBy rules fall through to
    the declaration-order default unchanged.
    """
    spec = _report_type_specs()[report_type]
    legal = list(spec["groupBy"])
    rules = _field_compat_rules().get("rules", [])
    per_gb = {
        r["scope"]["group_by"]: set(r["allowed_fields"])
        for r in rules
        if r.get("kind") == "selectable_set"
        and r.get("scope", {}).get("report_type") == report_type
        and "group_by" in r.get("scope", {})
    }
    if not per_gb:
        return legal[0]
    wanted = set(field_ids)
    best_gb = legal[0]
    best_cover = -1
    for gb in legal:  # declaration order = tie-break toward the canonical grain
        allowed = per_gb.get(gb)
        cover = len(wanted & allowed) if allowed is not None else len(wanted)
        if cover > best_cover:
            best_cover = cover
            best_gb = gb
    return best_gb


def _prune_default_selection(selection: dict, report_type: str) -> dict:
    """Prune the CORE-RESOLVED default selection to *report_type*'s columns.

    Used ONLY for the resolved tier-core default (selection is None OR the
    selection received is byte-identical to the catalog tier-core default the
    core queue resolves in place of None -- review 26.5 F-1). The catalog
    tier-core default spans EVERY report type (SB/SD purchase families,
    structure/keyword/asin of other grains), so the module prunes it to the
    default report type's DICTIONARY set (report_type_columns.json) AND to the
    per-groupBy narrowing of the COVERING groupBy (same field_compat context
    _build_report_body enforces -- never a blind group_by[0]) AND to NUMERIC
    metrics, and logs what it dropped. A genuine USER selection is NEVER pruned
    -- incompatibilities there stay typed refusals.
    """
    metrics_in = list(selection.get("metrics", []))
    dims_in = list(selection.get("dimensions", []))

    # Same context as _build_report_body: the report-type dictionary set AND the
    # covering groupBy's per-page narrowing (intersection of both rules).
    group_by = _covering_group_by(report_type, metrics_in + dims_in)
    allowed = set(_load_report_type_columns().get(report_type, ()))
    rules = _field_compat_rules().get("rules", [])
    for r in rules:
        if (
            r.get("kind") == "selectable_set"
            and r.get("scope", {}) == {"report_type": report_type, "group_by": group_by}
        ):
            allowed &= set(r["allowed_fields"])

    numeric = set(_numeric_field_ids(metrics_in))

    kept_metrics = [m for m in metrics_in if m in allowed and m in numeric]
    kept_dims = [d for d in dims_in if d in allowed]
    dropped = sorted(
        (set(metrics_in) - set(kept_metrics)) | (set(dims_in) - set(kept_dims))
    )
    if dropped:
        logger.info(
            "amazon_ads_default_selection_pruned: report_type=%s group_by=%s "
            "dropped=%d columns outside the covering scope or non-numeric "
            "(default selection only): %s",
            report_type, group_by, len(dropped), ",".join(dropped),
        )
    return {
        "metrics": kept_metrics,
        "dimensions": kept_dims,
        "source_fields": dict(selection.get("source_fields", {})),
    }


def _resolved_core_default_selection() -> dict:
    """The catalog tier-core default AS THE CORE QUEUE RESOLVES IT (cached).

    core/queue.py resolves a ``selection=None`` catalog_driven job to this exact
    shape (validate_selection(catalog, catalog_default_selection(catalog))) and
    passes it as an EXPLICIT selection -- so the module must recognise it to
    apply the same pruning it applies to selection=None (review 26.5 F-1).
    """
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
    """True when *selection* is the core-resolved tier-core default (F-1).

    Compares the SETS of metric/dimension ids (order-independent): a genuine
    user selection with any different field membership is never treated as the
    prunable default (it stays a typed refusal on incompatibility).
    """
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
    **_test_hooks,
) -> dict:
    """Catalog-driven daily pull: the v3 report request is BUILT FROM the selection.

    Steps:
      1. resolve the selection (catalog tier-core default when absent, pruned
         to the default report type -- user selections are NEVER pruned);
      2. refuse excluded catalog columns typed (dsp-seat-gated / non-daily /
         probe-to-confirm reasons travel in the refusal);
      3. resolve reportTypeId + groupBy covering the WHOLE selection
         (deterministic preference order, typed refusal otherwise);
      4. build + validate the createasyncreportrequest body (dates, retention
         floor, charset, field_compat selectable_set) BEFORE any API call;
      5. drive the 26.1 async socle (dedup/resume/deferred) and land LONG rows.
    """
    # Review 26.5 F-1: a catalog_driven job with NO explicit selection is
    # resolved by core/queue.py to the tier-core default (validate_selection
    # (catalog, catalog_default_selection(catalog))) and passed here as an
    # EXPLICIT selection. Both that resolved default AND selection=None must be
    # treated as the prunable module default (spanning every report type); a
    # genuine USER selection is NEVER pruned (typed refusals on incompatibility).
    if selection is None:
        selection = _resolved_core_default_selection()
        default_type = _PROFILE_SPECS["catalog_daily"][0]
        selection = _prune_default_selection(selection, report_type or default_type)
    elif _is_resolved_core_default(selection):
        default_type = _PROFILE_SPECS["catalog_daily"][0]
        selection = _prune_default_selection(selection, report_type or default_type)

    metric_ids = list(selection.get("metrics", []))
    dim_ids = list(selection.get("dimensions", []))
    all_ids = dim_ids + metric_ids
    if not all_ids:
        raise _invalid_request("amazon-ads catalog selection is empty")

    _refuse_excluded_columns(all_ids)
    resolved_type, group_by = _resolve_report_type_for_selection(all_ids, report_type)

    region, amazon_profile_id = _resolve_profile_selection(connection_id, account_id)
    body = _build_report_body(resolved_type, group_by, all_ids, date_from, date_to)

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    api_rows, report_ref, resumed = _run_report(
        connection_id, region, amazon_profile_id, body, **_test_hooks
    )

    canonical_rows = transform(api_rows)
    metric_names = _canonical_metric_names(_numeric_field_ids(metric_ids))
    spec = _report_type_specs()[resolved_type]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, metric_names, resolved_type, spec["adProduct"], region,
        amazon_profile_id, pull_id, loaded_at, project_id, db_mode, duckdb_path,
    )

    logger.info(
        "amazon_ads_pull_completed: pull_id=%s profile=catalog_daily "
        "report_type=%s row_count=%d selected_metrics=%d selected_dimensions=%d "
        "resumed=%s",
        pull_id, resolved_type, row_count, len(metric_ids), len(dim_ids), resumed,
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "report_type": resolved_type,
        "report_ref": report_ref,
        "resumed": resumed,
    }


# ---------------------------------------------------------------------------
# Account topology (story 26.5, pattern 25.5): region -> profile.
# ---------------------------------------------------------------------------

_PROFILES_PARAMS = {
    "apiProgram": "report",
    "accessLevel": "view",
    "profileTypeFilter": "seller,vendor",
}

_REGION_LABELS = {
    "NA": "North America (NA)",
    "EU": "Europe (EU)",
    "FE": "Far East (FE)",
}


def _profile_node(region: str, profile: dict) -> dict:
    account_info = profile.get("accountInfo") or {}
    profile_id = str(profile.get("profileId", ""))
    name = account_info.get("name") or account_info.get("id") or profile_id
    country = profile.get("countryCode") or "?"
    acc_type = account_info.get("type") or "?"
    sub_type = account_info.get("subType")
    label = f"{name} ({country}, {acc_type})"
    node = {
        # Composite opaque id: every later call routes on the profile's OWN
        # regional host (isolated silos; cross-region profileId = 4XX).
        "id": f"{region}:{profile_id}",
        "label": label,
        "level": "profile",
        "region": region,
        "country_code": profile.get("countryCode"),
        "currency_code": profile.get("currencyCode"),
        "timezone": profile.get("timezone"),
        "account_type": account_info.get("type"),
    }
    if sub_type:
        node["sub_type"] = sub_type
        if sub_type == "KDP_AUTHOR":
            # KDP authors have NO Sponsored Brands access (hard provider error
            # on sbX report creation) -- flagged for the selection UI.
            node["label"] += " [KDP author: no Sponsored Brands]"
    return node


def discover_accounts(connection_id: str) -> list[dict]:
    """Fan-out GET /v2/profiles over the 3 regional hosts (story 26.5).

    PER-HOST TOLERANT: a failing host logs a WARNING and contributes no
    profiles -- discovery only fails when EVERY host fails (a single-region
    advertiser legitimately returns [] on the other two hosts). Returns the
    generic nested list core's topology flow consumes:

        [{"id": "EU", "label": "Europe (EU)", "level": "region",
          "children": [{"id": "EU:1234", "label": "Acme (FR, seller)", ...}]}]

    Review 26.5 F-7: a RateLimitError from ANY host is RE-RAISED IMMEDIATELY
    (the breaker path -- Amazon's 429 carries Retry-After and the throttle is
    account-wide across the regional hosts, so continuing to hammer the other
    hosts would only deepen the throttle). Per-host tolerance covers only
    non-quota failures (a down/absent-region host). When every host fails with
    typed errors, the first typed failure is surfaced (never a silent []).
    """
    from core import nango_client  # noqa: PLC0415
    from core.quota import RateLimitError  # noqa: PLC0415

    token = nango_client.get_fresh_token(connection_id, provider="amazon-ads")
    regions: list[dict] = []
    failures: list[Exception] = []
    reached_any = False

    for region in sorted(_hosts()):
        host = _hosts()[region]
        try:
            resp = httpx.get(
                f"{host}/v2/profiles",
                params=_PROFILES_PARAMS,
                headers=_headers(token),
                timeout=30.0,
            )
            if resp.status_code != 200:
                _raise_provider_error(resp.status_code, resp)
            profiles = resp.json() or []
            reached_any = True
        except RateLimitError:
            # F-7: never swallowed per-host -- immediate breaker (Retry-After).
            logger.warning(
                "amazon_ads_discovery_rate_limited: region=%s -- re-raising "
                "immediately (breaker path, account-wide throttle)",
                region,
            )
            raise
        except Exception as exc:  # noqa: BLE001 -- per-host tolerance (story AC)
            failures.append(exc)
            logger.warning(
                "amazon_ads_discovery_host_failed: region=%s error=%s",
                region, type(exc).__name__,
            )
            continue
        children = [
            _profile_node(region, p) for p in profiles if p.get("profileId") is not None
        ]
        if children:
            regions.append(
                {
                    "id": region,
                    "label": _REGION_LABELS.get(region, region),
                    "level": "region",
                    "children": children,
                }
            )

    if not reached_any and failures:
        # Every host failed: surface the first typed failure (never silent).
        raise failures[0]

    logger.info(
        "amazon_ads_discover_accounts: regions_with_profiles=%d host_failures=%d",
        len(regions), len(failures),
    )
    return regions


def check_account_access(connection_id: str, account_id: str) -> dict:
    """Access-check the SELECTED profile (story 25.5 step 3) -- cheap probe.

    Submits (POST only -- no wait for generation) a minimal 1-day spCampaigns
    DAILY report on the profile via its OWN regional host. 200/202 proves
    report access; a 425 ALSO proves it (an identical probe is already in
    flight). Wrong-region/unauthorized profiles surface as typed 4XX errors.
    """
    from core import nango_client  # noqa: PLC0415

    region, profile_id = _split_selected_account(account_id)
    probe_to = date.today() - timedelta(days=1)
    body = _build_report_body(
        "spCampaigns", ["campaign"],
        ["date", "campaignId", "impressions", "clicks", "cost"],
        probe_to.isoformat(), probe_to.isoformat(),
    )
    body["name"] = f"toorow access check {account_id}"

    token = nango_client.get_fresh_token(connection_id, provider="amazon-ads")
    headers = _headers(token, profile_id)
    headers["Content-Type"] = _CREATE_REPORT_CONTENT_TYPE
    resp = httpx.post(
        f"{_host_for_region(region)}/reporting/reports",
        json=body, headers=headers, timeout=30.0,
    )
    if resp.status_code not in (200, 202, 425):
        _raise_provider_error(resp.status_code, resp)
    return {
        "account_id": account_id,
        "region": region,
        "profile_id": profile_id,
        "ok": True,
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
    WHERE connector = 'amazon-ads'
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
            "source_system": "amazon-ads",
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
def get_amazon_ads_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "sp_campaigns_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Amazon Ads Performance Report -- reads from fact_daily_kpi mart.

    Report profiles: sp_campaigns_daily, sb_campaigns_daily, sd_campaigns_daily,
                     catalog_daily
    Metrics: cost, impressions, clicks, purchases(_14d/_30d), sales(_14d/_30d),
             units_sold, viewable_impressions (additive at day grain)
    Breakdown dimensions: campaign_id

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    # AD-4: conversion columns are restated by Amazon 1/7/28 days after the
    # conversion event (interaction-date attribution: values move up to 42
    # days -- refetch ladder 3/14/45). Recent figures are provisional.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of the 4 declared profiles
        date_from: Start date ISO-8601 (e.g. '2026-06-01'). Defaults to 30 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=30)).isoformat()

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

    _register_raw("raw_amazon_ads_daily", provider="amazon-ads")
except Exception:
    pass  # best-effort; verification logs a warning if the table name is missing
