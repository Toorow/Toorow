"""The Trade Desk connector -- Story 28.2 (MyReports, REST v3, async).

Exposes a ``mcp_app: FastMCP`` instance the core loader mounts under the
``thetradedesk`` namespace (AD-2). Built to the epic-25/26 industrial standard:
generated api_catalog.json (275 Supermetrics columns, 263 exposed / 12 excluded
enrichment-only, ZERO planned), status-first error_map applied module side,
Partner -> Advertiser topology, and the 26.1 async-report socle
(core/async_reports.py) driving every extraction.

# AD-12: the MCP tool reads the fact_daily_kpi mart only -- no raw_* tables.
# AD-3:  the TTD-Auth token is obtained via POST /v3/authentication immediately
#        before use and cached only for its ExpirationDateUtc lifetime; the
#        Login/Password (platform env) are never stored/logged.
# AD-7:  pull_id minted by the core scheduler and passed into pull().
# AD-2:  request column lists are DERIVED from the manifest/catalog -- no
#        hardcoded field lists outside the catalog. The API pin
#        (provider_api_version) + host live in manifest.json, read from there.
# AD-4:  ratio/derived columns (Cpm/Cpc/Ctr/Cpa*) carry a NON-ADDITIVE note in
#        the catalog and land RAW at day grain when selected -- never summed.
# AI-03: ASCII-only stdout/log strings.
#
# API facts (research dossier thetradedesk-catalog-research.md, 2026-07-21):
#   - MyReports is NOT create-on-the-fly: a ReportTemplate (dimensions+metrics
#     +filters) must PRE-EXIST (My Reports app UI or a cloned predefined
#     template). The connector drives the schedule + download cycle only:
#       POST /v3/myreports/reportschedule (referencing a template) -> a
#       ReportScheduleId; executions run asynchronously; poll
#       POST /v3/myreports/reportexecution/query/advertisers until
#       ReportExecutionState == Complete; each completed execution carries
#       ReportDeliveries[].DownloadURL (a pre-signed link, ~1 h TTL) -> fetch
#       the CSV/gzip -> long-format kpi rows.
#   - Auth: POST /v3/authentication {Login, Password} -> {Token,
#     ExpirationDateUtc}; send TTD-Auth: <token> on every subsequent call;
#     refresh when ExpirationDateUtc is passed.
#   - Rate limits are UNDOCUMENTED (per partner/advertiser, no published
#     429/Retry-After contract) -> conservative pacing + breaker
#     (core.quota.RateLimitError). Never in the error_map.
#   - STATUS: DRAFT. The real ReportTemplate facet enum (the true field
#     authority) is transcribed at the live probe and diffed against the
#     Supermetrics baseline (ROLLOUT_NOTES.md).
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
mcp_app = FastMCP("thetradedesk")

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (no hardcoded paths).
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# Manifest / catalog access (cached). The API pin + host live in manifest.json.
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


def _api_host() -> str:
    """The single MyReports REST host, pinned in the manifest (AD-2)."""
    return _load_manifest()["provider_api_host"].rstrip("/")


def _template_specs() -> dict:
    """The managed ReportTemplate profiles (dimensions + metrics per template)."""
    return _load_catalog_sources()["report_template_compatibility"]["templates"]


def _field_compat_rules() -> dict:
    """The module's validated core 26.1 field_compatibility block (cached)."""
    cached = globals().get("_COMPAT_RULES")
    if cached is None:
        from core import field_compat  # noqa: PLC0415

        cached = field_compat.load_rules(_load_catalog_sources()["field_compatibility"])
        globals()["_COMPAT_RULES"] = cached
    return cached


# ---------------------------------------------------------------------------
# Provider error handling (dossier section 6).
#
# TTD publishes NO numeric error-code table: classification is by HTTP status +
# body token. DECISION (mirrors amazon-ads 26.5): the manifest error_map uses
# "<status>" and "<status>:<token>" keys and THIS MODULE applies them, with
# body-token refinements first and core's pure-HTTP classify_http_error as the
# final fallback. 429 -> core.quota.RateLimitError (breaker), never the map.
# ---------------------------------------------------------------------------

# Body-token refinements (dossier section 6), applied before status-level
# mapping. TTD bodies carry a free-text message; a revoked/disabled credential
# reads as auth_revoked (reconnect) rather than a transient auth_expired.
_BODY_REFINEMENTS: tuple[tuple[int, str, str], ...] = (
    (401, "revoked", "auth_revoked"),
    (401, "disabled", "auth_revoked"),
    (403, "revoked", "auth_revoked"),
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


def _extract_message(body) -> str:
    if isinstance(body, dict):
        msg = body.get("Message") or body.get("message") or body.get("detail")
        if isinstance(msg, str):
            return msg
        return json.dumps(body, sort_keys=True)
    return str(body or "")


def _raise_provider_error(status_code: int, resp: httpx.Response | None = None,
                          body=None) -> None:
    """Raise the canonical typed error for a non-2xx TTD response.

    429 -> core.quota.RateLimitError (breaker path; TTD publishes no Retry-After
    so the default backoff applies -- retry_after=None). Everything else:
    body-token refinement, then the manifest's status/status:token error_map,
    then core's pure-HTTP classify_http_error. Provider payload preserved.
    """
    if body is None and resp is not None:
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    if status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        # TTD publishes no Retry-After -> default backoff (conservative pacing).
        raise RateLimitError("thetradedesk", None)

    from core import pull_errors  # noqa: PLC0415

    classes = _canonical_error_classes()
    message = _extract_message(body).lower()
    for ref_status, token, canonical in _BODY_REFINEMENTS:
        if status_code == ref_status and token in message:
            raise classes[canonical](provider_status=status_code, provider_payload=body)

    error_map = _load_manifest().get("error_map") or {}
    refined = error_map.get(str(status_code))
    exc_cls = classes.get(refined)
    if exc_cls is not None:
        raise exc_cls(provider_status=status_code, provider_payload=body)

    raise pull_errors.classify_http_error(status_code, body)


# ---------------------------------------------------------------------------
# Authentication -- POST /v3/authentication -> TTD-Auth token (+ refresh).
#
# The Login/Password are PLATFORM credentials (env, never manifest, never
# per-user). The token is cached only until ExpirationDateUtc; a passed
# expiry forces a re-authentication before use (AD-3).
# ---------------------------------------------------------------------------

_LOGIN_ENV = "THETRADEDESK_API_LOGIN"
_PASSWORD_ENV = "THETRADEDESK_API_PASSWORD"

_HTTP_TIMEOUT = 60.0

# Refresh a little BEFORE ExpirationDateUtc so an in-flight call never races the
# boundary (mirrors the amazon-ads retention-floor UTC-margin lesson).
_TOKEN_REFRESH_MARGIN_SECONDS = 60


def _credentials() -> tuple[str, str]:
    login = os.environ.get(_LOGIN_ENV)
    password = os.environ.get(_PASSWORD_ENV)
    if not login or not password:
        raise ValueError(
            f"{_LOGIN_ENV} and {_PASSWORD_ENV} env vars are required (platform-level "
            "The Trade Desk API credentials for POST /v3/authentication; never "
            "per-user, never in the manifest)"
        )
    return login, password


def _parse_expiration(raw) -> datetime:
    """Parse ExpirationDateUtc into an aware UTC datetime (best-effort)."""
    text = str(raw or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        # Unknown format -> treat as immediately-expiring (forces a re-auth).
        return datetime.now(tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _authenticate() -> tuple[str, datetime]:
    """POST /v3/authentication -> (token, expiration_utc). Never logs the body."""
    login, password = _credentials()
    resp = httpx.post(
        f"{_api_host()}/v3/authentication",
        json={"Login": login, "Password": password},
        headers={"Content-Type": "application/json"},
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        _raise_provider_error(resp.status_code, resp)
    payload = resp.json()
    token = payload.get("Token")
    if not token:
        raise _invalid_request("thetradedesk authentication returned no Token", payload)
    expiration = _parse_expiration(payload.get("ExpirationDateUtc"))
    return str(token), expiration


def _get_token() -> str:
    """Return a live TTD-Auth token, (re-)authenticating on a passed expiry.

    Cached in a module-global with its ExpirationDateUtc; a 401 during use is
    still handled by the flow (re-auth on the retry) -- this cache only avoids a
    round-trip per call while the token is demonstrably valid.
    """
    cache = globals().get("_TOKEN_CACHE")
    now = datetime.now(tz=timezone.utc)
    if (
        cache is not None
        and cache["expiration"] - timedelta(seconds=_TOKEN_REFRESH_MARGIN_SECONDS) > now
    ):
        return cache["token"]
    token, expiration = _authenticate()
    globals()["_TOKEN_CACHE"] = {"token": token, "expiration": expiration}
    logger.info("thetradedesk_authenticated: token refreshed (expiry cached)")
    return token


def _clear_token_cache() -> None:
    globals()["_TOKEN_CACHE"] = None


def _headers(token: str) -> dict:
    return {"TTD-Auth": token, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Request-shape validation (pre-call refusals -- nothing is dropped silently).
# ---------------------------------------------------------------------------

# TTD column ids are alphanumeric (CamelCase / digit-suffixed, e.g.
# ClickConversion01). system_metadata.* dotted ids are enrichment-only and
# excluded -- never reach a report body -- so the request charset is strict.
_COLUMN_CHARSET = re.compile(r"^[a-zA-Z0-9]+$")


def _invalid_request(message: str, payload=None):
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    return InvalidRequestError(
        provider_status=None, provider_payload=payload, message=message
    )


def _validate_dates(date_from: str, date_to: str) -> None:
    """Validate the report window BEFORE any API call (typed invalid_request).

    ISO dates, from <= to. TTD windows are set on the ReportSchedule body (a
    rolling window), not an arbitrary API date picker; the schedule/template
    owns retention, so no per-type retention floor is enforced here (the probe
    confirms the real attribution window -- dossier section 5).
    """
    try:
        parsed_from = date.fromisoformat(str(date_from))
        parsed_to = date.fromisoformat(str(date_to))
    except (TypeError, ValueError) as exc:
        raise _invalid_request(
            f"thetradedesk dates must be ISO-8601 YYYY-MM-DD: {date_from!r}..{date_to!r}"
        ) from exc
    if parsed_from > parsed_to:
        raise _invalid_request(
            f"thetradedesk date_from {date_from} is after date_to {date_to}"
        )


def _validate_columns(report_template: str, columns: list[str]) -> None:
    """Refuse any column the ReportTemplate facet set does not carry (typed).

    Charset guard first (^[a-zA-Z0-9]+$ -- also refuses the dotted
    system_metadata.* enrichment-only ids), then the core 26.1 field_compat
    selectable_set enforcement with the {"report_template"} context. NO SILENT
    DROP: a selected column either lands or is refused typed here.
    """
    from core import field_compat  # noqa: PLC0415

    bad_charset = sorted(c for c in columns if not _COLUMN_CHARSET.match(str(c)))
    if bad_charset:
        raise _invalid_request(
            "thetradedesk column id(s) violate the request charset ^[a-zA-Z0-9]+$ "
            "(dotted system_metadata.* ids are enrichment-only, never requestable): "
            + ", ".join(repr(c) for c in bad_charset)
        )
    if not columns:
        raise _invalid_request("thetradedesk report request needs at least one column")

    field_compat.enforce_selection(
        list(columns), _field_compat_rules(), context={"report_template": report_template}
    )


def _refuse_excluded_columns(field_ids: list[str]) -> None:
    """A selected excluded (enrichment-only) column is a TYPED refusal + reason."""
    by_id = _catalog_fields_by_id()
    refusals = []
    for fid in field_ids:
        entry = by_id.get(fid)
        if entry is not None and entry.get("exposure") == "excluded":
            refusals.append(f"{fid} ({entry.get('exclusion_reason', 'excluded')})")
    if refusals:
        raise _invalid_request(
            "thetradedesk selection includes excluded catalog column(s) -- refused "
            "before any API call: " + "; ".join(sorted(refusals))
        )


def _build_schedule_body(
    report_template: str,
    advertiser_id: str,
    dimensions: list[str],
    metrics: list[str],
    date_from: str,
    date_to: str,
) -> dict:
    """Build the validated POST /v3/myreports/reportschedule body.

    MyReports is not create-on-the-fly: the body REFERENCES the pre-existing
    managed ReportTemplate (by name) and pins the advertiser scope, the rolling
    date range and a one-off recurrence. The selected dimensions + metrics are
    validated pre-call (charset, excluded, field_compat selectable_set) before
    the body is assembled. 'date' is force-included (daily grain requires it).
    """
    spec = _template_specs().get(report_template)
    if spec is None:
        raise _invalid_request(
            f"thetradedesk ReportTemplate {report_template!r} is not managed: the "
            "module ships one template per report profile "
            f"({', '.join(sorted(_template_specs()))})"
        )
    _validate_dates(date_from, date_to)

    ordered_dims: list[str] = []
    for column in dimensions:
        if column not in ordered_dims:
            ordered_dims.append(column)
    if "date" not in ordered_dims:
        ordered_dims.insert(0, "date")
    ordered_metrics: list[str] = []
    for column in metrics:
        if column not in ordered_metrics and column not in ordered_dims:
            ordered_metrics.append(column)

    all_columns = ordered_dims + ordered_metrics
    _refuse_excluded_columns(all_columns)
    _validate_columns(report_template, all_columns)

    return {
        "ReportScheduleName": f"toorow {report_template} {date_from} {date_to}",
        "ReportTemplateName": report_template,
        "AdvertiserFilters": [str(advertiser_id)],
        "ReportStartDateInclusive": date_from,
        "ReportEndDateExclusive": date_to,
        "ReportFrequency": "Once",
        "TimeframeType": "Custom",
        "Configuration": {
            "Dimensions": list(ordered_dims),
            "Metrics": list(ordered_metrics),
        },
    }


def _request_hash(advertiser_id: str, report_template: str, body: dict) -> str:
    """Deterministic hash of the FULL request shape (socle dedup key).

    ReportScheduleName is EXCLUDED (carries no shape) so re-runs of the same
    window hash identically and resume the in-flight report by re-poll.
    """
    shape = {
        "advertiser_id": str(advertiser_id),
        "report_template": report_template,
        "start": body["ReportStartDateInclusive"],
        "end": body["ReportEndDateExclusive"],
        "configuration": body["Configuration"],
    }
    canonical = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The async flow callables (26.1 socle: submit -> poll -> download).
# ---------------------------------------------------------------------------

# ReportExecutionState / failureReason substrings meaning CONFIGURATION (typed
# invalid_request instead of the expired->single-resubmission path): a bad
# template or an illegal dimension/metric combo. An UNDOCUMENTED reason takes
# the resubmit-once -> provider_transient route (never a silent success).
_FAILURE_CONFIG_MARKERS = (
    "invalid",
    "not supported",
    "unknown dimension",
    "unknown metric",
    "template",
)


def _build_flow(advertiser_id: str, report_template: str, body: dict):
    """Return an AsyncReportFlow for one TTD MyReports report request.

    submit:   POST /v3/myreports/reportschedule -> ReportScheduleId. (The socle
        claim/request-hash dedup re-polls any persisted in-flight ref BEFORE
        submit() runs, so a schedule is never created twice for one window.)
    poll:     POST /v3/myreports/reportexecution/query/advertisers filtered by
        the advertiser + schedule; map ReportExecutionState
        Pending/Running -> canonical pending/processing, Complete -> completed
        (stash the DownloadURL), Failed -> canonical expired (socle single
        resubmission) UNLESS the failureReason is configuration (invalid_request).
        A purged/unknown schedule (empty result on a previously valid ref) ->
        canonical expired (socle F-3).
    download: GET the pre-signed DownloadURL (NO TTD-Auth header), GZIP
        decompressed immediately when present, parsed as CSV or JSON rows. A
        dead link raises DownloadLinkExpired (socle single-resubmission policy).
    """
    from core import async_reports  # noqa: PLC0415

    host = _api_host()
    schedule_url = f"{host}/v3/myreports/reportschedule"
    execution_url = f"{host}/v3/myreports/reportexecution/query/advertisers"
    state: dict = {"download_url": None}

    def submit() -> str:
        resp = httpx.post(
            schedule_url, json=body, headers=_headers(_get_token()),
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 401:
            # Token invalidated between cache and use: re-auth once, retry.
            _clear_token_cache()
            resp = httpx.post(
                schedule_url, json=body, headers=_headers(_get_token()),
                timeout=_HTTP_TIMEOUT,
            )
        if resp.status_code in (200, 201):
            payload = resp.json()
            schedule_ref = payload.get("ReportScheduleId") or payload.get("Id")
            if not schedule_ref:
                raise _invalid_request(
                    "thetradedesk reportschedule creation returned no ReportScheduleId",
                    payload,
                )
            return str(schedule_ref)
        _raise_provider_error(resp.status_code, resp)
        raise AssertionError("unreachable")  # pragma: no cover

    def poll(report_ref: str) -> str:
        query_body = {
            "AdvertiserIds": [str(advertiser_id)],
            "ReportScheduleIds": [report_ref],
            "PageStartIndex": 0,
            "PageSize": 10,
        }
        resp = httpx.post(
            execution_url, json=query_body, headers=_headers(_get_token()),
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 401:
            _clear_token_cache()
            resp = httpx.post(
                execution_url, json=query_body, headers=_headers(_get_token()),
                timeout=_HTTP_TIMEOUT,
            )
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)
        payload = resp.json()
        executions = payload.get("Result") or payload.get("results") or []
        if not executions:
            # Unknown/purged schedule on a previously valid ref -> canonical
            # expired (socle F-3 single-resubmission).
            logger.warning("thetradedesk_poll_no_execution: report_ref=%s", report_ref)
            return async_reports.EXPIRED

        execution = executions[0]
        status = str(execution.get("ReportExecutionState", "")).lower()
        if status in ("pending", "queued", "scheduled"):
            return async_reports.PENDING
        if status in ("running", "processing", "generating"):
            return async_reports.PROCESSING
        if status == "complete":
            # F-1: write the download url UNCONDITIONALLY on every Complete poll.
            # A resubmission (ref A DownloadLinkExpired -> socle resubmit -> ref B)
            # may report Complete with an EMPTY ReportDeliveries; if we only
            # overwrote when deliveries were present, state["download_url"] would
            # keep ref A's stale (stashed) link and download() would silently
            # serve the OLD report's bytes. Overwriting to None here instead
            # forces DownloadLinkExpired (a clean expired/resubmit) over serving
            # stale bytes.
            deliveries = execution.get("ReportDeliveries") or []
            state["download_url"] = deliveries[0].get("DownloadURL") if deliveries else None
            return async_reports.COMPLETED
        if status == "failed":
            reason = str(execution.get("ReportExecutionErrorMessage")
                         or execution.get("failureReason") or "")
            if any(m in reason.lower() for m in _FAILURE_CONFIG_MARKERS):
                raise _invalid_request(
                    "thetradedesk report execution FAILED with a configuration "
                    f"reason: {reason}", execution,
                )
            logger.warning(
                "thetradedesk_execution_failed_resubmit_path: report_ref=%s reason=%s",
                report_ref, reason[:200],
            )
            return async_reports.EXPIRED
        raise _invalid_request(
            f"thetradedesk poll returned an unknown ReportExecutionState {status!r}",
            execution,
        )

    def download(report_ref: str):
        url = state.get("download_url")
        if not url:
            raise async_reports.DownloadLinkExpired(
                f"no download url for report_ref={report_ref}"
            )
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT)  # pre-signed: NO TTD-Auth
        if resp.status_code in (401, 403, 404):
            raise async_reports.DownloadLinkExpired(
                f"thetradedesk download link dead (HTTP {resp.status_code})"
            )
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)
        raw = resp.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return _parse_report_payload(raw)

    flow_request_hash = _request_hash(advertiser_id, report_template, body)
    return async_reports.AsyncReportFlow(
        submit=submit, poll=poll, download=download, request_hash=flow_request_hash
    )


def _parse_report_payload(raw: bytes) -> list[dict]:
    """Parse a completed report payload into row dicts (JSON array or CSV).

    MyReports delivers CSV by default; the probe confirms CSV vs gzip vs JSON
    (dossier open question #6). Both shapes are handled so a format flip at
    probe does not break the flow.
    """
    text = raw.decode("utf-8-sig")
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        rows = json.loads(stripped)
        if isinstance(rows, dict):
            rows = rows.get("Rows") or rows.get("rows") or []
        if not isinstance(rows, list):
            raise _invalid_request(
                "thetradedesk report JSON payload is not a row array", str(type(rows))
            )
        return rows
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


# ---------------------------------------------------------------------------
# transform() -- manifest-driven canonical field mapping (AD-2).
# ---------------------------------------------------------------------------

_DROP_FIELDS: set[str] = set()


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map report columns to canonical names using the manifest mappings.

    Columns absent from both mappings pass through unchanged (catalog selections
    keep their TTD column ids).
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
        cached = {f["field_id"]: f for f in _load_api_catalog().get("fields", [])}
        globals()["_CATALOG_BY_ID"] = cached
    return cached


def _numeric_field_ids(field_ids: list[str]) -> list[str]:
    """Column ids whose CATALOG physical_type is numeric (integer/decimal).

    Unit/coercion decisions are driven by the catalog physical_type, NEVER by an
    id suffix (26.2 lesson). Non-numeric selections land in segments_json --
    observable, never silently dropped.
    """
    by_id = _catalog_fields_by_id()
    return [
        fid for fid in field_ids
        if by_id.get(fid, {}).get("physical_type") in ("integer", "decimal")
    ]


# ---------------------------------------------------------------------------
# Raw landing -- LONG format: one row per grain x metric (Story 28.2).
# The landing is (grain keys..., segments_json, metric, value_num) so
# catalog_daily can select ANY exposed column of the 263-column surface. dbt
# staging supersedes per grain x metric (QUALIFY, AD-7).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_thetradedesk_daily (
    date                  VARCHAR,
    report_template       VARCHAR,
    partner_id            VARCHAR,
    advertiser_id         VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    ad_group_id           VARCHAR,
    ad_group_name         VARCHAR,
    creative_id           VARCHAR,
    segments_json         VARCHAR,
    metric                VARCHAR,
    value_num             DOUBLE,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_thetradedesk_daily
    (date, report_template, partner_id, advertiser_id, campaign_id,
     campaign_name, ad_group_id, ad_group_name, creative_id, segments_json,
     metric, value_num, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# (landed column, post-transform candidate keys -- canonical first, TTD id as
# fallback for catalog selections outside the manifest map).
_STRUCTURE_LOOKUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("campaign_id", ("campaign_id", "CampaignId")),
    ("campaign_name", ("campaign_name", "CampaignName")),
    ("ad_group_id", ("ad_group_id", "AdGroupId")),
    ("ad_group_name", ("ad_group_name", "AdGroupName")),
    ("creative_id", ("creative_id", "CreativeId")),
)

_NON_SEGMENT_KEYS = {
    "date", "campaign_id", "CampaignId", "campaign_name", "CampaignName",
    "ad_group_id", "AdGroupId", "ad_group_name", "AdGroupName",
    "creative_id", "CreativeId", "advertiser_id", "AdvertiserId",
    "partner_id", "PartnerId", "pull_id", "connector", "report_template",
}


def _first_present(row: dict, keys: tuple[str, ...]):
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def _coerce_str(value) -> str:
    if value is None:
        return ""
    return str(value)


def _insert_raw_rows(
    rows: list[dict],
    metric_names: list[str],
    report_template: str,
    partner_id: str,
    advertiser_id: str,
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Long-ify canonical wide rows and insert into raw_thetradedesk_daily.

    One long row per (grain, metric) with value_num. NULL honnete (AD-9): an
    absent metric lands NO row (absence stays distinguishable from a recorded
    zero). Non-structural dimension values land in segments_json (canonical
    sorted-key JSON) so any catalog column participates in the supersede grain
    without schema churn.
    """
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )
    from core import warehouse_write  # noqa: PLC0415

    metric_set = list(dict.fromkeys(metric_names))
    values: list[tuple] = []
    for row in rows:
        segment_items = {
            k: row[k]
            for k in row
            if k not in _NON_SEGMENT_KEYS and k not in metric_set
            and row[k] is not None
        }
        segments_json = (
            json.dumps(segment_items, sort_keys=True, default=str)
            if segment_items else None
        )
        structure = {
            landed: _first_present(row, keys) for landed, keys in _STRUCTURE_LOOKUPS
        }
        for metric in metric_set:
            value = row.get(metric)
            if value is None or value == "":
                continue
            try:
                value_num = float(value)
            except (ValueError, TypeError):
                logger.warning(
                    "thetradedesk_non_numeric_metric_value: metric=%s", metric
                )
                continue
            values.append(
                (
                    str(row.get("date", "")),
                    report_template,
                    str(partner_id),
                    str(advertiser_id),
                    _coerce_str(structure["campaign_id"]),
                    _coerce_str(structure["campaign_name"]),
                    _coerce_str(structure["ad_group_id"]),
                    _coerce_str(structure["ad_group_name"]),
                    _coerce_str(structure["creative_id"]),
                    segments_json,
                    metric,
                    value_num,
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


# ---------------------------------------------------------------------------
# Account topology (dossier section 2): Partner -> Advertiser.
#
# The reporting anchor is the ADVERTISER. The selected account id is the opaque
# '<PartnerId>:<AdvertiserId>' composite so every later call carries both
# without a re-discovery. Core never interprets it (AD-2).
# ---------------------------------------------------------------------------


def _split_selected_account(account_id: str) -> tuple[str, str]:
    """Parse the opaque topology account id '<PartnerId>:<AdvertiserId>'."""
    raw = str(account_id).strip()
    if ":" in raw:
        partner_id, advertiser_id = raw.split(":", 1)
        if partner_id.strip() and advertiser_id.strip():
            return partner_id.strip(), advertiser_id.strip()
    raise ValueError(
        "thetradedesk account id must be '<PartnerId>:<AdvertiserId>' "
        f"-- got {account_id!r} (run the topology discovery flow)"
    )


def _resolve_advertiser_selection(
    connection_id: str, account_id: str | None
) -> tuple[str, str]:
    """Resolve (partner_id, advertiser_id) for a pull.

    Explicit arg wins (tests / direct calls). Otherwise the ONLY source is the
    core topology scope (story 25.5 flow). There is NO env-var account fallback
    (THETRADEDESK_*_ID is forbidden by doctrine).
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
        logger.warning("thetradedesk_account_resolution_failed: %s", type(exc).__name__)

    if not selected:
        raise ValueError(
            "no selected reporting advertiser for this thetradedesk connection -- "
            "run the account topology flow (discovery -> selection -> access "
            "check); env-var account fallbacks do not exist for this module"
        )
    return _split_selected_account(selected)


def discover_accounts(connection_id: str) -> list[dict]:
    """POST /v3/advertiser/query/partner -> advertisers under the partner.

    Returns the generic nested list core's topology flow consumes:

        [{"id": "<PartnerId>", "label": "Partner <id>", "level": "partner",
          "children": [{"id": "<PartnerId>:<AdvertiserId>",
                        "label": "Acme (USD)", "level": "advertiser", ...}]}]

    The PartnerId is a platform-scoped credential fact; discovery lists the
    advertisers the token can reach (dossier section 2). Core owns selection /
    access-check / trial / backfill.
    """
    partner_id = os.environ.get("THETRADEDESK_PARTNER_ID")
    if not partner_id:
        raise ValueError(
            "THETRADEDESK_PARTNER_ID env var required for discovery (the partner "
            "seat the platform credential is scoped to; never per-user)"
        )
    query_body = {"PartnerId": partner_id, "PageStartIndex": 0, "PageSize": 1000}
    resp = httpx.post(
        f"{_api_host()}/v3/advertiser/query/partner",
        json=query_body, headers=_headers(_get_token()), timeout=30.0,
    )
    if resp.status_code == 401:
        _clear_token_cache()
        resp = httpx.post(
            f"{_api_host()}/v3/advertiser/query/partner",
            json=query_body, headers=_headers(_get_token()), timeout=30.0,
        )
    if resp.status_code != 200:
        _raise_provider_error(resp.status_code, resp)
    payload = resp.json()
    advertisers = payload.get("Result") or payload.get("results") or []

    children = []
    for adv in advertisers:
        adv_id = adv.get("AdvertiserId") or adv.get("Id")
        if adv_id is None:
            continue
        name = adv.get("AdvertiserName") or adv.get("Name") or str(adv_id)
        currency = adv.get("CurrencyCode") or "?"
        children.append(
            {
                "id": f"{partner_id}:{adv_id}",
                "label": f"{name} ({currency})",
                "level": "advertiser",
                "partner_id": partner_id,
                "currency_code": adv.get("CurrencyCode"),
            }
        )

    logger.info("thetradedesk_discover_accounts: advertisers=%d", len(children))
    if not children:
        return []
    return [
        {
            "id": partner_id,
            "label": f"Partner {partner_id}",
            "level": "partner",
            "children": children,
        }
    ]


def check_account_access(connection_id: str, account_id: str) -> dict:
    """Access-check the SELECTED advertiser (story 25.5 step 3) -- cheap probe.

    Resolves the advertiser (GET /v3/advertiser/{advertiserId}) on its partner
    scope. A 200 proves reporting-scope access; wrong/unauthorized advertisers
    surface as typed 4XX errors.
    """
    partner_id, advertiser_id = _split_selected_account(account_id)
    resp = httpx.get(
        f"{_api_host()}/v3/advertiser/{advertiser_id}",
        headers=_headers(_get_token()), timeout=30.0,
    )
    if resp.status_code == 401:
        _clear_token_cache()
        resp = httpx.get(
            f"{_api_host()}/v3/advertiser/{advertiser_id}",
            headers=_headers(_get_token()), timeout=30.0,
        )
    if resp.status_code != 200:
        _raise_provider_error(resp.status_code, resp)
    return {
        "account_id": account_id,
        "partner_id": partner_id,
        "advertiser_id": advertiser_id,
        "ok": True,
    }


# ---------------------------------------------------------------------------
# Shared pull core (26.1 socle: submit -> poll -> download).
# ---------------------------------------------------------------------------

# exact_bundle profile -> managed ReportTemplate id.
_PROFILE_SPECS: dict[str, str] = {
    "daily_performance": "daily_performance",
    "catalog_daily": "daily_performance",
}


def _run_report(
    connection_id: str,
    advertiser_id: str,
    report_template: str,
    body: dict,
    *,
    _store=None,
    _clock=None,
    _sleeper=None,
    _deadline_seconds=None,
) -> tuple[list[dict], str, bool]:
    """Drive the 26.1 socle for one report; return (rows, report_ref, resumed).

    A DEFERRED outcome (provider still generating past the per-run deadline, or a
    concurrent run holds the submit claim) raises a RETRYABLE
    ProviderTransientError: the reference stays persisted in flight, so the
    queue's normal retry resumes by RE-POLL (request-hash dedup) -- never a
    resubmission, never a silent empty success.
    """
    from core import async_reports  # noqa: PLC0415
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    if _store is None:
        _store = async_reports.PostgresReportRefStore()
    ledger_ref = f"{connection_id}:{advertiser_id}:{report_template}"
    flow = _build_flow(advertiser_id, report_template, body)

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
                "thetradedesk async report deferred (still generating or claim "
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
    """Run one exact_bundle report profile and land rows in raw_thetradedesk_daily."""
    if profile not in _PROFILE_SPECS:
        raise ValueError(f"Unknown thetradedesk report profile: {profile!r}")
    report_template = _PROFILE_SPECS[profile]

    partner_id, advertiser_id = _resolve_advertiser_selection(connection_id, account_id)
    spec = _template_specs()[report_template]
    dims = list(spec["dimensions"])
    metrics = list(spec["metrics"])
    body = _build_schedule_body(
        report_template, advertiser_id, dims, metrics, date_from, date_to
    )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    api_rows, report_ref, resumed = _run_report(
        connection_id, advertiser_id, report_template, body, **_test_hooks
    )

    canonical_rows = transform(api_rows)
    metric_names = _canonical_metric_names(_numeric_field_ids(metrics))

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, metric_names, report_template, partner_id, advertiser_id,
        pull_id, loaded_at, project_id, db_mode, duckdb_path,
    )

    logger.info(
        "thetradedesk_pull_completed: pull_id=%s profile=%s template=%s "
        "row_count=%d resumed=%s",
        pull_id, profile, report_template, row_count, resumed,
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
    """Default pull() = daily_performance exact bundle (AI-58 profile-less default)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="daily_performance", account_id=account_id, **_test_hooks,
    )


def pull_daily_performance(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None, **_test_hooks,
) -> dict:
    """AI-58 dispatch: daily_performance managed ReportTemplate (tier core)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="daily_performance", account_id=account_id, **_test_hooks,
    )


# ---------------------------------------------------------------------------
# catalog_driven execution: pull_catalog_daily (Story 28.2).
#
# The catalog IS the execution contract (Jean, 25.8): any cataloged EXPOSED
# column a datastream selects must actually be extracted. core validates the
# selection against api_catalog.json (existence) and passes selection=
# {"metrics", "dimensions", "source_fields"}; here the selection is resolved to
# ONE managed ReportTemplate + its dimension/metric split, validated pre-call
# (exposure, template membership, field_compat, dates), then driven through the
# async socle. Selection None = the tier-core default PRUNED to the default
# template; a genuine user selection is NEVER pruned.
# ---------------------------------------------------------------------------

# Deterministic resolution order: default template first, then the rest.
_TEMPLATE_PREFERENCE = (
    "daily_performance", "conversions", "creative", "geo_platform", "video_player",
)

_TYPE_NEUTRAL_COLUMNS = {"date"}


def _resolve_template_for_selection(
    field_ids: list[str], report_template: str | None
) -> str:
    """Resolve the managed ReportTemplate covering EVERY selected column.

    Explicit report_template wins (validated). Otherwise the first template of
    the deterministic preference order whose column set covers the whole
    selection is used. No candidate covering the selection => typed refusal
    listing the best candidate's missing columns (never a silent drop).
    """
    specs = _template_specs()
    deciding = [f for f in field_ids if f not in _TYPE_NEUTRAL_COLUMNS]

    if report_template is not None:
        if report_template not in specs:
            raise _invalid_request(
                f"thetradedesk ReportTemplate {report_template!r} is not managed"
            )
        return report_template

    best: tuple[int, str, list[str]] | None = None
    for candidate in _TEMPLATE_PREFERENCE:
        spec = specs.get(candidate)
        if spec is None:
            continue
        allowed = set(spec["dimensions"]) | set(spec["metrics"])
        missing = sorted(set(deciding) - allowed)
        if not missing:
            return candidate
        if best is None or len(missing) < best[0]:
            best = (len(missing), candidate, missing)

    assert best is not None
    raise _invalid_request(
        "thetradedesk: no single managed ReportTemplate covers the selection; "
        f"closest is {best[1]!r} missing column(s): " + ", ".join(best[2])
    )


def _split_selection_by_kind(field_ids: list[str]) -> tuple[list[str], list[str]]:
    """Split selected ids into (dimensions, metrics) by their catalog kind."""
    by_id = _catalog_fields_by_id()
    dims, metrics = [], []
    for fid in field_ids:
        kind = by_id.get(fid, {}).get("kind")
        if kind == "metric":
            metrics.append(fid)
        else:
            dims.append(fid)
    return dims, metrics


def _prune_default_selection(selection: dict, report_template: str) -> dict:
    """Prune the CORE-RESOLVED tier-core default to *report_template*'s columns.

    Used ONLY for the resolved tier-core default (selection None OR byte-set-
    identical to the catalog tier-core default). The default spans every
    template, so the module prunes it to the default template's dimension +
    metric set AND to numeric metrics, and logs what it dropped. A genuine USER
    selection is NEVER pruned -- incompatibilities there stay typed refusals.
    """
    metrics_in = list(selection.get("metrics", []))
    dims_in = list(selection.get("dimensions", []))
    spec = _template_specs()[report_template]
    allowed = set(spec["dimensions"]) | set(spec["metrics"])
    numeric = set(_numeric_field_ids(metrics_in))

    kept_metrics = [m for m in metrics_in if m in allowed and m in numeric]
    kept_dims = [d for d in dims_in if d in allowed]
    dropped = sorted(
        (set(metrics_in) - set(kept_metrics)) | (set(dims_in) - set(kept_dims))
    )
    if dropped:
        logger.info(
            "thetradedesk_default_selection_pruned: report_template=%s dropped=%d "
            "columns outside the template scope or non-numeric (default selection "
            "only): %s",
            report_template, len(dropped), ",".join(dropped),
        )
    return {
        "metrics": kept_metrics,
        "dimensions": kept_dims,
        "source_fields": dict(selection.get("source_fields", {})),
    }


def _resolved_core_default_selection() -> dict:
    """The catalog tier-core default AS THE CORE QUEUE RESOLVES IT (cached)."""
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
    """True when *selection* is the core-resolved tier-core default (F-1)."""
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
    report_template: str | None = None,
    **_test_hooks,
) -> dict:
    """Catalog-driven daily pull: the reportschedule body is BUILT FROM the selection.

    Steps:
      1. resolve the selection (catalog tier-core default when absent, pruned to
         the default template -- user selections are NEVER pruned);
      2. refuse excluded catalog columns typed (enrichment-only reasons travel
         in the refusal);
      3. resolve the managed ReportTemplate covering the WHOLE selection
         (deterministic preference order, typed refusal otherwise);
      4. build + validate the reportschedule body (dates, charset, field_compat
         selectable_set) BEFORE any API call;
      5. drive the 26.1 async socle (dedup/resume/deferred) and land LONG rows.
    """
    default_template = _PROFILE_SPECS["catalog_daily"]
    if selection is None:
        selection = _resolved_core_default_selection()
        selection = _prune_default_selection(selection, report_template or default_template)
    elif _is_resolved_core_default(selection):
        selection = _prune_default_selection(selection, report_template or default_template)

    metric_ids = list(selection.get("metrics", []))
    dim_ids = list(selection.get("dimensions", []))
    all_ids = dim_ids + metric_ids
    if not all_ids:
        raise _invalid_request("thetradedesk catalog selection is empty")

    _refuse_excluded_columns(all_ids)
    resolved_template = _resolve_template_for_selection(all_ids, report_template)

    # Re-split by catalog kind so the schedule body carries a clean
    # dimensions/metrics separation (a user may pass a metric under dimensions).
    resolved_dims, resolved_metrics = _split_selection_by_kind(all_ids)

    partner_id, advertiser_id = _resolve_advertiser_selection(connection_id, account_id)
    body = _build_schedule_body(
        resolved_template, advertiser_id, resolved_dims, resolved_metrics,
        date_from, date_to,
    )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    api_rows, report_ref, resumed = _run_report(
        connection_id, advertiser_id, resolved_template, body, **_test_hooks
    )

    canonical_rows = transform(api_rows)
    metric_names = _canonical_metric_names(_numeric_field_ids(resolved_metrics))

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, metric_names, resolved_template, partner_id, advertiser_id,
        pull_id, loaded_at, project_id, db_mode, duckdb_path,
    )

    logger.info(
        "thetradedesk_pull_completed: pull_id=%s profile=catalog_daily template=%s "
        "row_count=%d selected_metrics=%d selected_dimensions=%d resumed=%s",
        pull_id, resolved_template, row_count, len(resolved_metrics),
        len(resolved_dims), resumed,
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "report_template": resolved_template,
        "report_ref": report_ref,
        "resumed": resumed,
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
    WHERE connector = 'thetradedesk'
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
            "source_system": "thetradedesk",
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
def get_thetradedesk_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "daily_performance",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """The Trade Desk Performance Report -- reads from fact_daily_kpi mart.

    Report profiles: daily_performance, catalog_daily
    Metrics: impressions, clicks, bids, cost (advertiser/partner/TTD in
             advertiser/partner/USD currency), total click/view-through
             conversions (additive at day grain). Ratio metrics (cpm/cpc/ctr/
             cpa*) are NON-ADDITIVE (AD-4) -- recomputed in the semantic layer.
    Breakdown dimensions: advertiser_id, campaign_id, ad_group_id, creative_id

    Returns the canonical AD-1 envelope via structuredContent.

    # AD-4: conversion families (click/view-through/touch/time-weighted-decay,
    # pixels 01-06) restate as attribution windows close (last ~3-7 days
    # provisional -- refetch ladder 3/14/45). Recent figures are provisional.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of the declared profiles
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

    _register_raw("raw_thetradedesk_daily", provider="thetradedesk")
except Exception:
    pass  # best-effort; verification logs a warning if the table name is missing
