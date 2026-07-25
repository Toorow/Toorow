"""Google Ads connector -- Story 26.2 (GAQL catalog-first, API v24).

Exposes a ``mcp_app: FastMCP`` instance that the core loader mounts under the
``google-ads`` namespace (AD-2). Built to the epic-25 industrial standard from
day one: generated api_catalog.json (v24 protos: 278 metrics + 151 segments +
26 structural dimensions, ZERO planned), enum-keyed error_map, MCC -> client
account topology, and a catalog_driven profile whose GAQL is built from the
datastream selection.

# AD-12: MCP server reads the fact_daily_kpi mart only -- no raw_* tables.
# AD-3:  token obtained via nango_client.get_fresh_token immediately before use
#        (the facade routes auth_path='google_direct' connections to the
#        encrypted Google token store, AD-21 -- NOT Nango). Never stored/logged.
# AD-7:  pull_id minted by the core scheduler and passed into pull().
# AD-2:  request field lists are DERIVED from the manifest/catalog -- no
#        hardcoded field lists outside the catalog. The API version is pinned
#        in manifest.json (provider_api_version) and read from there ONLY.
# AD-4:  ratio/derived metrics carry a non-additive note in the catalog and are
#        landed RAW at day grain when selected -- never summed downstream.
# AI-03: ASCII-only stdout/log strings.
#
# API facts (research dossier google-ads-catalog-research.md, 2026-07-21):
#   - POST {base}/{v24}/customers/{cid}/googleAds:search  body={"query": GAQL}
#     headers: Authorization Bearer, developer-token (GOOGLE_ADS_DEVELOPER_TOKEN
#     platform env -- a PLATFORM credential, never per-user), login-customer-id
#     (mandatory when the client account is reached through an MCC).
#   - search uses fixed 10,000-row pages via nextPageToken (searchStream is out
#     of scope v1). REST JSON keys are camelCase (protobuf JSON); GAQL tokens
#     are snake_case -- _flatten converts per path component.
#   - int64 values (micros, counts) arrive as STRINGS; doubles as numbers.
#     Monetary amounts (the int64 *_micros family AND the micros-denominated
#     DOUBLES: average_cpc/cpm/cost/cpe, active_view_cpm, cost_per_*) are
#     landed as value/1e6 on the catalog money_micros DECLARATION
#     (official_fields.json, review 26.2 F-2) -- never on a name suffix.
#   - Errors: GoogleAdsFailure enum NAMES inside error.details[].errors[]
#     .errorCode -- the manifest error_map is keyed '<status>:<ENUM_NAME>'.
#     429 RESOURCE_EXHAUSTED carries quotaErrorDetails.retryDelay -> the
#     RateLimitError breaker path (never the error_map).
#   - Managers hold no metrics: REQUESTED_METRICS_FOR_MANAGER. Pulls always
#     target leaf CIDs with login-customer-id resolved from the topology
#     (composite opaque account id '<client_cid>@<login_cid>').
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
mcp_app = FastMCP("google-ads")

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (no hardcoded paths).
#   TOOROW_DB_MODE     = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH = path to local .duckdb file
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# Manifest / catalog access (cached). The API version is pinned in ONE place:
# manifest.json provider_api_version (story 26.2) -- a bump forces a catalog
# regen because the module test asserts manifest pin == catalog api_version.
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
    """Return the manifest's ``error_map`` ('<status>:<ENUM_NAME>' keys), cached."""
    return _load_manifest().get("error_map") or {}


def _api_base_url() -> str:
    """Provider endpoint root, built from the manifest pin (single source)."""
    manifest = _load_manifest()
    base = manifest.get("provider_api_base", "https://googleads.googleapis.com")
    version = manifest.get("provider_api_version", "v24")
    return f"{base}/{version}"


def _load_catalog_sources() -> dict:
    global _CATALOG_SOURCES
    try:
        return _CATALOG_SOURCES  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "catalog_sources" / "catalog_sources.json"
    globals()["_CATALOG_SOURCES"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_CATALOG_SOURCES"]


def _load_api_catalog() -> dict:
    global _API_CATALOG
    try:
        return _API_CATALOG  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "api_catalog.json"
    globals()["_API_CATALOG"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_API_CATALOG"]


# ---------------------------------------------------------------------------
# Provider error handling (story 26.2, dossier section 7).
#
# Google REST failures carry a google.rpc.Status body whose details[] embed a
# GoogleAdsFailure with typed enum NAMES:
#   {"error": {"code": 401, "status": "UNAUTHENTICATED", "details": [
#       {"@type": ".../GoogleAdsFailure",
#        "errors": [{"errorCode": {"authenticationError": "OAUTH_TOKEN_EXPIRED"},
#                     "message": "..."}]}]}}
# core.pull_errors' generic extractor only reads error.code (the numeric HTTP
# echo), so this module extracts the enum name itself and applies the manifest
# error_map with the public canonical classes. Pure-HTTP classification stays
# the fallback (classify_http_error) so an unknown enum is never mis-typed.
# ---------------------------------------------------------------------------


def _extract_error_enum(body) -> str | None:
    """Best-effort extraction of the first GoogleAdsFailure enum name. Never raises."""
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if not isinstance(err, dict):
        return None
    for detail in err.get("details") or []:
        if not isinstance(detail, dict):
            continue
        for entry in detail.get("errors") or []:
            if not isinstance(entry, dict):
                continue
            error_code = entry.get("errorCode")
            if isinstance(error_code, dict):
                for value in error_code.values():
                    if isinstance(value, str) and value:
                        return value
    return None


def _extract_retry_delay_seconds(body) -> int | None:
    """Read quotaErrorDetails.retryDelay ('42s') from a 429 body. Never raises."""
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if not isinstance(err, dict):
        return None
    for detail in err.get("details") or []:
        if not isinstance(detail, dict):
            continue
        for entry in detail.get("errors") or []:
            if not isinstance(entry, dict):
                continue
            delay = (entry.get("details") or {}).get("quotaErrorDetails", {}).get(
                "retryDelay"
            ) or (entry.get("quotaErrorDetails") or {}).get("retryDelay")
            if isinstance(delay, str):
                match = re.match(r"^(\d+)", delay)
                if match:
                    return int(match.group(1))
    return None


def _raise_provider_error(status_code: int, resp: httpx.Response | None = None,
                          body=None) -> None:
    """Raise the canonical typed error for a non-2xx Google Ads response.

    429 -> core.quota.RateLimitError (breaker path, retry_after honored from
    quotaErrorDetails.retryDelay when present -- dossier section 4.3).
    Everything else -> the manifest error_map refined class when the extracted
    GoogleAdsFailure enum matches '<status>:<ENUM>', else the pure-HTTP
    classification (classify_http_error). Provider payload preserved.

    400:UNSUPPORTED_VERSION additionally logs the loud version-drift alert
    (the pinned provider_api_version has been sunset -- regen required).
    """
    if body is None and resp is not None:
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    if status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after = _extract_retry_delay_seconds(body)
        if retry_after is None and resp is not None:
            try:
                retry_after = int(resp.headers.get("Retry-After", "0")) or None
            except (ValueError, TypeError):
                retry_after = None
        raise RateLimitError("google-ads", retry_after)

    from core import pull_errors  # noqa: PLC0415

    enum_name = _extract_error_enum(body)
    if enum_name == "UNSUPPORTED_VERSION":
        # Version-sunset alarm (story 26.2): the ONE pinned version is dead.
        logger.error(
            "google_ads_unsupported_version_drift: pinned api version %s rejected "
            "by provider -- bump manifest provider_api_version and REGENERATE the "
            "catalog (conformance gate).",
            _load_manifest().get("provider_api_version"),
        )

    canonical_classes = {
        pull_errors.AUTH_EXPIRED: pull_errors.AuthExpiredError,
        pull_errors.AUTH_REVOKED: pull_errors.AuthRevokedError,
        pull_errors.PERMISSION_DENIED: pull_errors.PermissionDeniedError,
        pull_errors.INVALID_REQUEST: pull_errors.InvalidRequestError,
        pull_errors.PROVIDER_TRANSIENT: pull_errors.ProviderTransientError,
    }
    if enum_name is not None:
        refined = _load_error_map().get(f"{status_code}:{enum_name}")
        exc_cls = canonical_classes.get(refined)
        if exc_cls is not None:
            raise exc_cls(provider_status=status_code, provider_payload=body)

    raise pull_errors.classify_http_error(status_code, body)


# ---------------------------------------------------------------------------
# Auth headers. developer-token is a PLATFORM credential (env, never manifest,
# never per-user -- AD-21 / dossier section 4). login-customer-id is mandatory
# whenever the client account is reached through an MCC.
# ---------------------------------------------------------------------------

_DEVELOPER_TOKEN_ENV = "GOOGLE_ADS_DEVELOPER_TOKEN"


def _headers(token: str, login_customer_id: str | None = None) -> dict:
    developer_token = os.environ.get(_DEVELOPER_TOKEN_ENV)
    if not developer_token:
        raise ValueError(
            f"{_DEVELOPER_TOKEN_ENV} env var required (platform-level Google Ads "
            "developer token -- see dossier section 4.2; never per-user)"
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "developer-token": developer_token,
    }
    if login_customer_id:
        headers["login-customer-id"] = str(login_customer_id).replace("-", "")
    return headers


def _normalize_cid(customer_id: str) -> str:
    """'123-456-7890' / 'customers/1234567890' -> '1234567890'."""
    cid = str(customer_id).strip()
    if cid.startswith("customers/"):
        cid = cid.split("/", 1)[1]
    return cid.replace("-", "")


def _split_selected_account(account_id: str) -> tuple[str, str | None]:
    """Parse the opaque topology account id: '<cid>' or '<cid>@<login_cid>'.

    The composite form is produced by discover_accounts for client accounts
    reached through an MCC, so the pull site recovers login-customer-id without
    a re-discovery round-trip. Core never interprets the id (AD-2).
    """
    raw = str(account_id)
    if "@" in raw:
        cid, login = raw.split("@", 1)
        return _normalize_cid(cid), _normalize_cid(login)
    return _normalize_cid(raw), None


def _resolve_customer_selection(
    connection_id: str,
    customer_id: str | None,
    login_customer_id: str | None,
) -> tuple[str, str | None]:
    """Resolve the reporting customer for a pull.

    Explicit args win (tests / direct calls). Otherwise the ONLY source is the
    core topology scope (story 25.5 flow: pending_account_selection -> ready).
    There is NO env-var account fallback (GOOGLE_ADS_*_ID is forbidden by
    doctrine): an unselected account is a hard, actionable error.
    """
    if customer_id:
        cid, embedded_login = _split_selected_account(customer_id)
        return cid, (
            _normalize_cid(login_customer_id) if login_customer_id else embedded_login
        )

    selected: str | None = None
    try:
        from core import account_topology, token_service  # noqa: PLC0415

        resolved = token_service.resolve_connection_by_nango_id(connection_id)
        if resolved is not None:
            selected = account_topology.resolve_selected_account(resolved.id)
    except (LookupError, ValueError) as exc:
        # Review 26.2 F-10: only EXPECTED resolution misses are downgraded to
        # the actionable "no selected account" error below (unknown connection
        # id, malformed scope). Infrastructure failures (DB outage, driver
        # errors) PROPAGATE -- a bare except here would misdiagnose an outage
        # as a not-onboarded connection.
        logger.warning("google_ads_account_resolution_failed: %s", type(exc).__name__)

    if not selected:
        raise ValueError(
            "no selected reporting account for this google-ads connection -- run "
            "the account topology flow (discovery -> selection -> access check); "
            "env-var account fallbacks do not exist for this module"
        )
    return _split_selected_account(selected)


# ---------------------------------------------------------------------------
# GAQL build + REST search + response flattening.
# ---------------------------------------------------------------------------

# Pagination guard: 100 pages x 10,000 rows = 1M rows per pull window.
_MAX_SEARCH_PAGES = 100

# Report profile -> (FROM resource, data_level stamp). data_level (story 3.6
# lesson): each grain lands under its own marker so coexisting grains never
# double-count inside one series.
_PROFILE_SPECS: dict[str, tuple[str, str]] = {
    "campaign_daily": ("campaign", "CAMPAIGN"),
    "ad_group_daily": ("ad_group", "AD_GROUP"),
    "ad_daily": ("ad_group_ad", "AD"),
    "keyword_daily": ("keyword_view", "KEYWORD"),
    "search_term_daily": ("search_term_view", "SEARCH_TERM"),
    "catalog_daily": ("campaign", "CAMPAIGN"),
}

# Review 26.2 F-5: the FROM-resource WHITELIST for catalog_daily, with the
# data_level each resource's rows land under (rows always land at the RIGHT
# grain marker). An unknown resource is a typed pre-call refusal -- and the
# attribute validation NEVER silently deactivates, because every whitelisted
# resource has a generated selectable_set rule (field_compatibility).
_RESOURCE_DATA_LEVELS: dict[str, str] = {
    "campaign": "CAMPAIGN",
    "ad_group": "AD_GROUP",
    "ad_group_ad": "AD",
    "keyword_view": "KEYWORD",
    "search_term_view": "SEARCH_TERM",
}

# Review 26.2 F-6: GAQL is assembled from tokens -- every token must be a
# lexically-safe GAQL path AND a source_field the catalog declares. Dates are
# parsed (date.fromisoformat) and re-emitted, so no user string ever reaches
# the query verbatim.
_GAQL_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.]*$")

# Structure tokens ALWAYS present in a catalog_daily SELECT so every row lands
# with its grain key (field_id -> GAQL token). Mirrors meta-ads
# _CATALOG_STRUCTURE_FIELDS.
_CATALOG_STRUCTURE_TOKENS: dict[str, str] = {
    "date": "segments.date",
    "customer_id": "customer.id",
    "campaign_id": "campaign.id",
    "campaign_name": "campaign.name",
}


def _profile_request_fields(profile_id: str) -> list[tuple[str, str]]:
    """Return ordered (field_id, gaql_token) pairs for an exact_bundle profile.

    AD-2: derived from the manifest capability report (field_id ->
    source_field), never hardcoded here.
    """
    caps = _load_manifest().get("source_capabilities", {})
    source_by_id = {
        f["field_id"]: f.get("source_field", f["field_id"])
        for f in caps.get("fields", [])
    }
    report = next(
        (r for r in caps.get("reports", []) if r.get("id") == profile_id), None
    )
    if report is None:
        raise ValueError(f"Unknown google-ads report profile: {profile_id!r}")
    pairs: list[tuple[str, str]] = []
    for field_id in list(report.get("dimensions", [])) + list(
        report.get("metrics", [])
    ):
        pairs.append((field_id, source_by_id.get(field_id, field_id)))
    return pairs


def _known_gaql_tokens() -> frozenset:
    """Every GAQL token the module may legally SELECT (catalog-declared)."""
    global _KNOWN_TOKENS
    try:
        return _KNOWN_TOKENS  # type: ignore[name-defined]
    except NameError:
        pass
    tokens = {
        f.get("source_field", f.get("field_id", ""))
        for f in _load_api_catalog().get("fields", [])
    }
    tokens.update(_CATALOG_STRUCTURE_TOKENS.values())
    globals()["_KNOWN_TOKENS"] = frozenset(t for t in tokens if t)
    return globals()["_KNOWN_TOKENS"]


def _parse_iso_date(label: str, value) -> str:
    """ISO-parse a date bound and RE-EMIT it (typed refusal on anything else)."""
    from datetime import date as _date  # noqa: PLC0415

    try:
        return _date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        from core.pull_errors import InvalidRequestError  # noqa: PLC0415

        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                f"google-ads: {label} must be an ISO-8601 date (YYYY-MM-DD), "
                f"got {value!r} (pre-call refusal, review 26.2 F-6)"
            ),
        ) from None


def _build_gaql(
    select_tokens: list[str], resource: str, date_from: str, date_to: str
) -> str:
    """SELECT <tokens> FROM <resource> WHERE segments.date BETWEEN ... (26.2).

    segments.date is always selected (daily grain) so the finite-date-range
    rule (EXPECTED_FILTERS_ON_DATE_RANGE) holds by construction.

    Review 26.2 F-6 (GAQL injection): dates are fromisoformat-parsed and
    re-emitted; the FROM resource must be whitelisted (F-5); every SELECT
    token must match ^[a-z][a-z0-9_.]*$ AND be a catalog-declared
    source_field. Anything else is a typed pre-call refusal.
    """
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    date_from = _parse_iso_date("date_from", date_from)
    date_to = _parse_iso_date("date_to", date_to)

    if resource not in _RESOURCE_DATA_LEVELS:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                f"google-ads: unknown FROM resource {resource!r} -- supported: "
                + ", ".join(sorted(_RESOURCE_DATA_LEVELS))
                + " (pre-call refusal, review 26.2 F-5)"
            ),
        )

    known = _known_gaql_tokens()
    bad = sorted(
        {
            str(t)
            for t in select_tokens
            if not _GAQL_TOKEN_RE.match(str(t)) or str(t) not in known
        }
    )
    if bad:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                "google-ads: GAQL token(s) refused (not catalog-declared or "
                "lexically unsafe): " + ", ".join(bad)
                + " (pre-call refusal, review 26.2 F-6)"
            ),
        )

    tokens: list[str] = []
    for token in select_tokens:
        if token not in tokens:
            tokens.append(token)
    if "segments.date" not in tokens:
        tokens.insert(0, "segments.date")
    return (
        f"SELECT {', '.join(tokens)} FROM {resource} "
        f"WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'"
    )


def _snake_to_camel(component: str) -> str:
    parts = component.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _walk_camel_path(obj: dict, snake_path: str):
    """Read a snake_case GAQL path out of a camelCase protobuf-JSON row."""
    node = obj
    for component in snake_path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(_snake_to_camel(component))
        if node is None:
            return None
    return node


def _coerce_value(field_id: str, token: str, value):
    """Coerce a protobuf-JSON value for landing.

    int64 arrive as strings; doubles as numbers. Metrics become numeric;
    repeated fields land as canonical JSON; everything else becomes str.
    Micros stay RAW here -- transform() applies the /1e6 landing rule once.
    """
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    if token.startswith("metrics."):
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    if isinstance(value, bool):
        return value
    return str(value)


def _flatten_result_row(api_row: dict, pairs: list[tuple[str, str]]) -> dict:
    """One GAQL result row -> wide canonical dict keyed by catalog field_id."""
    row: dict = {}
    for field_id, token in pairs:
        row[field_id] = _coerce_value(field_id, token, _walk_camel_path(api_row, token))
    return row


def _search(
    connection_id: str,
    customer_id: str,
    query: str,
    login_customer_id: str | None,
) -> tuple[list[dict], int, bool]:
    """Run a paginated googleAds:search and return (rows, pages, truncated).

    AD-3: the token is fetched immediately before use (google_direct routing in
    the get_fresh_token facade) and falls out of scope after the loop.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="google-ads")
    url = f"{_api_base_url()}/customers/{_normalize_cid(customer_id)}/googleAds:search"
    headers = _headers(token, login_customer_id)

    results: list[dict] = []
    body: dict = {"query": query}
    pages = 0
    truncated = False
    while True:
        resp = httpx.post(url, json=body, headers=headers, timeout=60.0)
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)

        payload = resp.json()
        results.extend(payload.get("results") or [])
        pages += 1

        next_token = payload.get("nextPageToken")
        if not next_token:
            break
        if pages >= _MAX_SEARCH_PAGES:
            truncated = True
            logger.warning(
                "google_ads_search_truncated: page cap %d reached (%d rows)",
                _MAX_SEARCH_PAGES, len(results),
            )
            break
        body = {"query": query, "pageToken": next_token}

    return results, pages, truncated


# ---------------------------------------------------------------------------
# Field-compatibility validation (core 26.1 engine -- review 26.2 F-1).
#
# The rules live in catalog_sources.json `field_compatibility` (schema '1',
# rules GENERATED by catalog_sources/build_official_fields.py) and are
# enforced via core.field_compat.enforce_selection with
# context={"resource": <FROM resource>} -- STRICT context by default (a
# missing context is a caller bug, never a silent pass). A known-incompatible
# selection is refused BEFORE any API call so PROHIBITED_SEGMENT_WITH_METRIC
# ... never surfaces from the provider on a known rule. AUTHORITY once
# fetched = fieldservice_compat.json (live dump overlay, 25.6).
# ---------------------------------------------------------------------------


def _field_compat_rules() -> dict:
    """The module's VALIDATED field_compatibility rules (cached, fail-loud)."""
    global _COMPAT_RULES
    try:
        return _COMPAT_RULES  # type: ignore[name-defined]
    except NameError:
        pass
    from core import field_compat  # noqa: PLC0415

    rules = field_compat.load_module_rules(Path(__file__).parent)
    if rules is None:
        # The block is part of this module's contract: its absence is a broken
        # deployment, never a silent validation bypass (F-5: attribute
        # validation NEVER deactivates).
        raise RuntimeError(
            "google-ads: field_compatibility block missing from "
            "catalog_sources.json -- regenerate it with "
            "catalog_sources/build_official_fields.py"
        )
    globals()["_COMPAT_RULES"] = rules
    return rules


def _catalog_fields_by_id() -> dict:
    """api_catalog.json fields indexed by field_id (cached)."""
    global _CATALOG_BY_ID
    try:
        return _CATALOG_BY_ID  # type: ignore[name-defined]
    except NameError:
        pass
    globals()["_CATALOG_BY_ID"] = {
        f["field_id"]: f
        for f in _load_api_catalog().get("fields", [])
        if f.get("field_id")
    }
    return globals()["_CATALOG_BY_ID"]


_NUMERIC_PHYSICAL_TYPES = ("integer", "decimal")


def _row_restricting_matchers() -> tuple[frozenset, tuple]:
    """(exact segment names, prefixes) from _row_restricting_segments metadata.

    Keys ending in '*' are prefixes, others exact (catalog_sources contract).
    """
    block = _load_catalog_sources().get("field_compatibility", {})
    entries = block.get("_row_restricting_segments", {})
    exact = frozenset(k for k in entries if not k.endswith("*"))
    prefixes = tuple(k[:-1] for k in entries if k.endswith("*"))
    return exact, prefixes


def _resolved_core_default_selection() -> dict:
    """The catalog tier-core default AS THE CORE QUEUE RESOLVES IT (cached).

    Story 26.6 Part B: core/queue.py resolves a ``selection=None`` catalog_driven
    job to this exact shape (validate_selection(catalog, catalog_default_selection
    (catalog))) and passes it as an EXPLICIT selection -- so the module must
    recognise it to apply the SAME pruning it applies to selection=None. Without
    this, the core-resolved tier-core default (spanning every resource, time
    grain and non-numeric metric) reaches the GAQL builder verbatim and is
    refused/mis-shaped on every scheduled pull with no user selection.
    """
    global _CORE_DEFAULT_SELECTION
    try:
        return _CORE_DEFAULT_SELECTION  # type: ignore[name-defined]
    except NameError:
        pass
    from core.catalog_contract import (  # noqa: PLC0415
        catalog_default_selection,
        validate_selection,
    )

    catalog = _load_api_catalog()
    resolved, _issues = validate_selection(
        catalog, catalog_default_selection(catalog)
    )
    globals()["_CORE_DEFAULT_SELECTION"] = resolved
    return resolved


def _is_resolved_core_default(selection: dict) -> bool:
    """True when *selection* is the core-resolved tier-core default (26.6 B).

    Compares the SETS of metric/dimension ids (order-independent): a genuine
    USER selection with any different field membership is never treated as the
    prunable default -- it stays a typed refusal on incompatibility.
    """
    default = _resolved_core_default_selection()
    return (
        set(selection.get("metrics", []) or []) == set(default.get("metrics", []) or [])
        and set(selection.get("dimensions", []) or [])
        == set(default.get("dimensions", []) or [])
    )


def _prune_default_selection(selection: dict, resource: str) -> dict:
    """Prune the MODULE-RESOLVED default selection for *resource* (F-3).

    Used ONLY for the CORE-RESOLVED tier-core default: either ``selection is
    None`` OR (story 26.6 Part B) a selection byte-equal to the default the core
    queue resolves in place of None and passes as an explicit selection. The
    tier-core default spans the whole catalog, so the module prunes it to an
    honest, grain-stable daily default and logs what it dropped:

      (a) the TIME section is reduced to ``segments.date`` alone (hour /
          day_of_week / week / ... would explode the daily grain);
      (b) row-restricting segments (_row_restricting_segments metadata:
          click_type, keyword, conversion_action*, ...) are excluded -- a
          DEFAULT must never silently reshape row totals;
      (c) non-numeric metrics (enum/string/array/date) are excluded -- the
          long landing carries value_num only;
      (d) fields incompatible with the FROM resource (core field_compat
          probe) are excluded.

    A USER selection is NEVER pruned -- incompatibilities there are typed
    refusals (never-silently-dropped contract).
    """
    from core import field_compat  # noqa: PLC0415

    rules = _field_compat_rules()
    context = {"resource": resource}
    catalog_by_id = _catalog_fields_by_id()
    source_fields: dict = selection.get("source_fields", {})
    exact_restricting, prefix_restricting = _row_restricting_matchers()

    dropped: dict[str, str] = {}

    def _compat_ok(field_id: str) -> bool:
        probe = {"metrics": [], "dimensions": [field_id]}
        return not field_compat.validate_selection(probe, rules, context=context)

    kept_dims: list[str] = []
    for field_id in selection.get("dimensions", []):
        entry = catalog_by_id.get(field_id, {})
        token = str(source_fields.get(field_id, field_id))
        if entry.get("section") == "TIME" and field_id != "date":
            dropped[field_id] = "time_grain"
            continue
        if token.startswith("segments."):
            segment = token.split(".")[1]
            if segment in exact_restricting or segment.startswith(prefix_restricting):
                dropped[field_id] = "row_restricting"
                continue
        if not _compat_ok(field_id):
            dropped[field_id] = "resource_scope"
            continue
        kept_dims.append(field_id)

    kept_metrics: list[str] = []
    for field_id in selection.get("metrics", []):
        physical = catalog_by_id.get(field_id, {}).get("physical_type")
        if physical not in _NUMERIC_PHYSICAL_TYPES:
            dropped[field_id] = "non_numeric_metric"
            continue
        if not _compat_ok(field_id):
            dropped[field_id] = "resource_scope"
            continue
        kept_metrics.append(field_id)

    if dropped:
        logger.info(
            "google_ads_default_selection_pruned: dropped=%d fields from the "
            "tier-core default for FROM %s (default only, users get typed "
            "refusals): %s",
            len(dropped), resource,
            ",".join(f"{fid}:{why}" for fid, why in sorted(dropped.items())),
        )
    return {
        "metrics": kept_metrics,
        "dimensions": kept_dims,
        "source_fields": source_fields,
    }


# ---------------------------------------------------------------------------
# transform() -- manifest-driven canonical field mapping + the micros rule.
# ---------------------------------------------------------------------------

# Ratio metrics of the exact_bundle profiles: none are requested (the bundles
# carry only additive families), so nothing is dropped here. catalog_daily may
# select provider-computed ratios -- they land RAW at day grain (non-additive,
# GSC average_position pattern) and are NEVER summed downstream (AD-4).
_DROP_FIELDS: set[str] = set()


def _money_micros_field_ids() -> frozenset:
    """Field ids DECLARED money_micros in official_fields.json (cached).

    Review 26.2 F-2: the landing division is driven by this catalog
    declaration -- never by a name-suffix heuristic (the *_micros suffix
    misses the micros-denominated doubles: average_cpc/cpm/cost/cpe,
    active_view_cpm, the cost_per_* family -- a suffix rule lands them 1e6
    too big).
    """
    global _MONEY_MICROS_IDS
    try:
        return _MONEY_MICROS_IDS  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "catalog_sources" / "official_fields.json"
    fields = json.loads(path.read_text(encoding="utf-8"))
    globals()["_MONEY_MICROS_IDS"] = frozenset(
        f["field_id"] for f in fields if f.get("physical_type") == "money_micros"
    )
    return globals()["_MONEY_MICROS_IDS"]


def _descriptive_mutable_landed_names() -> frozenset:
    """Post-transform landed names of the DESCRIPTIVE-MUTABLE attributes (26.6).

    Story 26.6 Part A: fields DECLARED ``descriptive_mutable`` in the catalog
    (official_fields.json -- STATUS/TYPE enums like campaign.status) are MUTABLE
    states of an entity. A re-pull of the same window (26.1 refetch ladder) can
    return a CHANGED value for a past date; if it landed in segments_json (part
    of the dbt supersede grain) the changed value would fork the grain and the
    stale row would survive the QUALIFY, double-counting the metrics of the
    whole re-pulled window.

    So they land in a SEPARATE attributes_json column (latest-wins, exactly like
    campaign_name) that NEVER enters the supersede partition. This returns the
    CANONICAL landed names (post-transform) so the split at landing matches the
    keys transform() produced -- driven by the catalog DECLARATION, never a
    name/section heuristic in the connector.
    """
    global _MUTABLE_LANDED_NAMES
    try:
        return _MUTABLE_LANDED_NAMES  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "catalog_sources" / "official_fields.json"
    fields = json.loads(path.read_text(encoding="utf-8"))
    mutable_ids = [f["field_id"] for f in fields if f.get("descriptive_mutable")]
    dim_map = _load_manifest().get("canonical_dimension_mapping", {})
    globals()["_MUTABLE_LANDED_NAMES"] = frozenset(
        dim_map.get(fid, fid) for fid in mutable_ids
    )
    return globals()["_MUTABLE_LANDED_NAMES"]


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map flattened GAQL rows to canonical names using manifest mappings.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_mapping
    (cost_micros -> cost, search_term_text -> search_term, ...). Fields absent
    from both mappings pass through unchanged (pull_id, connector, catalog
    selections ...).

    Micros rule (story 26.2, review F-2): a field DECLARED money_micros in the
    catalog (official_fields.json) is a micro-amount on the wire (int64 string
    or double); it is divided by 1e6 HERE, exactly once, on the DECLARATION
    (never a suffix), so value_num always lands in currency units. Final unit
    ratification per field at the 25.6 live probe (ROLLOUT_NOTES.md).
    """
    manifest = _load_manifest()

    rename_map: dict[str, str] = {}
    for src, val in manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            rename_map[src] = val.get("canonical", src)
    rename_map.update(manifest.get("canonical_dimension_mapping", {}))

    money_micros = _money_micros_field_ids()

    result: list[dict] = []
    for row in raw_rows:
        canonical: dict = {}
        for key, value in row.items():
            if key in _DROP_FIELDS:
                continue
            if key in money_micros and value is not None:
                try:
                    value = float(value) / 1e6
                except (ValueError, TypeError):
                    pass
            canonical[rename_map.get(key, key)] = value
        result.append(canonical)
    return result


def _canonical_metric_names(metric_field_ids: list[str]) -> list[str]:
    """Post-transform landed names for the selected metric field ids."""
    mapping = _load_manifest().get("canonical_metric_mapping", {})
    names: list[str] = []
    for field_id in metric_field_ids:
        val = mapping.get(field_id, field_id)
        if isinstance(val, dict):
            val = val.get("canonical", field_id)
        names.append(val)
    return names


# ---------------------------------------------------------------------------
# Raw landing -- LONG format (story 26.2): one row per grain x metric.
# Wide columns cannot hold an arbitrary catalog selection (278 metrics), so the
# landing is (grain keys..., segments_json, metric, value_num). dbt staging
# supersedes per grain x metric (QUALIFY, AD-7).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_google_ads_daily (
    date                  VARCHAR,
    data_level            VARCHAR,
    customer_id           VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    ad_group_id           VARCHAR,
    ad_group_name         VARCHAR,
    ad_id                 VARCHAR,
    criterion_id          VARCHAR,
    keyword_text          VARCHAR,
    search_term           VARCHAR,
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

_RAW_INSERT_SQL = """
INSERT INTO raw_google_ads_daily
    (date, data_level, customer_id, campaign_id, campaign_name, ad_group_id,
     ad_group_name, ad_id, criterion_id, keyword_text, search_term,
     segments_json, attributes_json, metric, value_num, cost_source_currency,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Canonical (post-transform) keys that land in dedicated grain columns.
_STRUCTURE_COLUMNS = (
    "date", "customer_id", "campaign_id", "campaign_name", "ad_group_id",
    "ad_group_name", "ad_id", "criterion_id", "keyword_text", "search_term",
)
_NON_SEGMENT_KEYS = set(_STRUCTURE_COLUMNS) | {
    "customer_currency_code", "pull_id", "connector", "data_level",
}


def _insert_raw_rows(
    rows: list[dict],
    metric_names: list[str],
    data_level: str,
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Long-ify canonical wide rows and insert into raw_google_ads_daily.

    One long row per (grain, metric) with value_num. NULL honnete (AD-9): an
    absent metric lands NO row -- zero-metric rows are omitted by Google and
    absence must stay distinguishable from a recorded zero. Non-structural
    ROW-BEARING dimension values (segments: device, geo, ...) land in
    segments_json (canonical sorted-key JSON) so any catalog segment
    participates in the supersede grain without schema churn.

    Story 26.6 Part A: DESCRIPTIVE-MUTABLE attributes (status/type enums
    declared descriptive_mutable in the catalog) land instead in a SEPARATE
    attributes_json column (latest-wins) that NEVER enters the dbt supersede
    partition -- a re-pull with a changed status keeps the SAME grain, so the
    QUALIFY keeps one row per grain and metrics are never double-counted.
    """
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )
    from core import warehouse_write  # noqa: PLC0415

    metric_set = list(dict.fromkeys(metric_names))
    # Story 26.6 Part A: descriptive-mutable attributes (status/type enums) are
    # split OUT of segments_json into attributes_json (latest-wins, NEVER in the
    # dbt supersede partition) so a re-pull with a changed status can never fork
    # the grain nor double-count metrics.
    mutable_names = _descriptive_mutable_landed_names()
    values: list[tuple] = []
    for row in rows:
        segment_items = {
            k: row[k]
            for k in row
            if k not in _NON_SEGMENT_KEYS and k not in metric_set
            and k not in mutable_names
            and row[k] is not None
        }
        segments_json = (
            json.dumps(segment_items, sort_keys=True) if segment_items else None
        )
        attribute_items = {
            k: row[k]
            for k in row
            if k in mutable_names and row[k] is not None
        }
        attributes_json = (
            json.dumps(attribute_items, sort_keys=True) if attribute_items else None
        )
        currency = row.get("customer_currency_code") or None
        for metric in metric_set:
            value = row.get(metric)
            if value is None:
                continue
            try:
                value_num = float(value)
            except (ValueError, TypeError):
                # Defensive only (review 26.2 F-4): a non-numeric metric value
                # is DROPPED here, so this branch must stay unreachable for
                # catalog selections -- pull_catalog_daily refuses non-numeric
                # metric selections pre-call (typed InvalidRequestError), and
                # the exact_bundle profiles carry curated numeric metrics.
                logger.warning(
                    "google_ads_non_numeric_metric_value_dropped: metric=%s "
                    "(should have been refused pre-call)", metric,
                )
                continue
            values.append(
                (
                    row.get("date", ""),
                    data_level,
                    row.get("customer_id") or "",
                    row.get("campaign_id") or "",
                    row.get("campaign_name") or "",
                    row.get("ad_group_id") or "",
                    row.get("ad_group_name") or "",
                    row.get("ad_id") or "",
                    row.get("criterion_id") or "",
                    row.get("keyword_text") or "",
                    row.get("search_term") or "",
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

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_RAW_CREATE_DDL)
    if values:
        con.executemany(_RAW_INSERT_SQL, values)
    con.close()
    return len(values)


# ---------------------------------------------------------------------------
# Shared pull core (exact_bundle profiles).
# ---------------------------------------------------------------------------


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    customer_id: str | None = None,
    login_customer_id: str | None = None,
) -> dict:
    """Run one exact_bundle GAQL profile and land rows in raw_google_ads_daily.

    # AD-12: called by the queue worker only.
    # AD-3: token used immediately (inside _search) then discarded.
    # AD-7: pull_id minted by the caller.

    Raises
    ------
    ValueError
        Unknown profile, missing developer token, or no selected account
        (topology flow not completed -- no env-var fallback exists).
    core.quota.RateLimitError
        HTTP 429 (breaker path, retry_after from quotaErrorDetails).
    core.pull_errors.ConnectorError
        Typed canonical error for any other non-2xx (manifest error_map).
    """
    if profile not in _PROFILE_SPECS:
        raise ValueError(f"Unknown google-ads report profile: {profile!r}")
    resource, data_level = _PROFILE_SPECS[profile]

    cid, login_cid = _resolve_customer_selection(
        connection_id, customer_id, login_customer_id
    )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    pairs = _profile_request_fields(profile)
    query = _build_gaql([token for _fid, token in pairs], resource, date_from, date_to)

    api_rows, pages, truncated = _search(connection_id, cid, query, login_cid)

    wide_rows = [_flatten_result_row(r, pairs) for r in api_rows]
    canonical_rows = transform(wide_rows)

    metric_ids = [fid for fid, token in pairs if token.startswith("metrics.")]
    metric_names = _canonical_metric_names(metric_ids)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, metric_names, data_level, pull_id, loaded_at, project_id,
        db_mode, duckdb_path,
    )

    # AD-3: no token in log -- only pull_id / counts (safe metadata).
    logger.info(
        "google_ads_pull_completed: pull_id=%s profile=%s row_count=%d pages=%d",
        pull_id, profile, row_count, pages,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "pages": pages,
        "truncated": truncated,
    }


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    customer_id: str | None = None,
    login_customer_id: str | None = None,
) -> dict:
    """Default pull() = campaign grain (AI-58 profile-less default dispatch)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", customer_id=customer_id,
        login_customer_id=login_customer_id,
    )


# ---------------------------------------------------------------------------
# AI-58 per-profile dispatch: pull_<profile_id>, declared as dispatch.callable
# in the manifest capability reports. All delegate to the ONE code path.
# ---------------------------------------------------------------------------


def pull_campaign_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, customer_id: str | None = None,
    login_customer_id: str | None = None,
) -> dict:
    """AI-58 dispatch: campaign grain (FROM campaign, data_level=CAMPAIGN)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", customer_id=customer_id,
        login_customer_id=login_customer_id,
    )


def pull_ad_group_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, customer_id: str | None = None,
    login_customer_id: str | None = None,
) -> dict:
    """AI-58 dispatch: ad group grain (FROM ad_group, data_level=AD_GROUP)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="ad_group_daily", customer_id=customer_id,
        login_customer_id=login_customer_id,
    )


def pull_ad_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, customer_id: str | None = None,
    login_customer_id: str | None = None,
) -> dict:
    """AI-58 dispatch: ad grain (FROM ad_group_ad, data_level=AD)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="ad_daily", customer_id=customer_id,
        login_customer_id=login_customer_id,
    )


def pull_keyword_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, customer_id: str | None = None,
    login_customer_id: str | None = None,
) -> dict:
    """AI-58 dispatch: keyword grain (FROM keyword_view, data_level=KEYWORD)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="keyword_daily", customer_id=customer_id,
        login_customer_id=login_customer_id,
    )


def pull_search_term_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, customer_id: str | None = None,
    login_customer_id: str | None = None,
) -> dict:
    """AI-58 dispatch: search term grain (FROM search_term_view, data_level=SEARCH_TERM)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="search_term_daily", customer_id=customer_id,
        login_customer_id=login_customer_id,
    )


# ---------------------------------------------------------------------------
# Story 26.2 -- catalog_driven execution: pull_catalog_daily.
#
# Principle (Jean, 25.8): the catalog IS the execution contract -- any cataloged
# field a datastream selects must actually be extracted. core validates the
# selection against api_catalog.json and passes selection={"metrics",
# "dimensions", "source_fields"} into this callable; here the generic selection
# becomes a concrete GAQL: SELECT <selection tokens> FROM <profile resource>
# WHERE segments.date BETWEEN ... Compatibility is validated BEFORE the call
# from catalog_sources field_compatibility (26.1 socle contract, local
# fallback) so a known-illegal combination never wastes a provider round-trip.
# ---------------------------------------------------------------------------


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    customer_id: str | None = None,
    login_customer_id: str | None = None,
    resource: str | None = None,
) -> dict:
    """Catalog-driven daily pull: the GAQL is BUILT FROM the selection.

    core resolves+validates ``selection`` against api_catalog.json and passes
    ``{"metrics", "dimensions", "source_fields"}``. A ``None`` selection falls
    back to the catalog tier-core default (resolved here so the callable is
    honest standalone). Steps:

      1. whitelist the FROM resource (F-5: resource -> data_level map, an
         unknown resource is a typed refusal);
      2. resolve the selection (tier-core default when absent, pruned per
         F-3: TIME reduced to segments.date, row-restricting segments and
         non-numeric metrics excluded, FROM-resource compatibility);
      3. refuse pre-call (typed InvalidRequestError, BEFORE any token fetch):
         excluded-exposure fields (F-8), non-numeric metrics explicitly
         selected (F-4: never a silent landing drop), and any field the core
         field_compat engine flags for this resource (26.1 contract, strict
         context -- F-1);
      4. build ``SELECT <selection> FROM <resource> WHERE segments.date
         BETWEEN ...`` (structure tokens always included, zero hardcoded
         non-structure fields; tokens + dates validated, F-6);
      5. flatten camelCase protobuf-JSON rows on the selected snake paths,
         transform (manifest renames + declared-micros/1e6), and land LONG
         rows via the shared _insert_raw_rows (selected segments ->
         segments_json) at the resource's data_level.

    # AD-3: token used immediately then discarded. AD-7: pull_id passed in.
    """
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    # F-5: FROM-resource whitelist + per-resource data_level (rows land at
    # the RIGHT grain marker). Attribute validation never deactivates: every
    # whitelisted resource has a generated selectable_set rule.
    from_resource = resource or _PROFILE_SPECS["catalog_daily"][0]
    if from_resource not in _RESOURCE_DATA_LEVELS:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                f"google-ads catalog_daily: unknown FROM resource "
                f"{from_resource!r} -- supported: "
                + ", ".join(sorted(_RESOURCE_DATA_LEVELS))
                + " (pre-call refusal, review 26.2 F-5)"
            ),
        )
    data_level = _RESOURCE_DATA_LEVELS[from_resource]

    # Story 26.6 Part B: a catalog_driven job with NO explicit selection is
    # resolved by core/queue.py to the tier-core default (validate_selection
    # (catalog, catalog_default_selection(catalog))) and passed here as an
    # EXPLICIT selection. BOTH that resolved default AND selection=None must be
    # treated as the prunable module default (spanning every resource/time
    # grain/non-numeric metric); a genuine USER selection is NEVER pruned (typed
    # refusals on incompatibility below).
    if selection is None:
        # The module-resolved default spans the whole tier-core catalog; prune
        # it to an honest daily default for the FROM resource (F-3). User
        # selections are NEVER pruned -- they get typed refusals below.
        selection = _prune_default_selection(
            _resolved_core_default_selection(), from_resource
        )
    elif _is_resolved_core_default(selection):
        selection = _prune_default_selection(selection, from_resource)

    selected_metrics = list(selection.get("metrics", []))
    selected_dimensions = list(selection.get("dimensions", []))
    catalog_by_id = _catalog_fields_by_id()

    # F-8: a field cataloged exposure=excluded is not extractable -- typed
    # refusal listing the fields (never a silent pass-through).
    excluded = sorted(
        fid
        for fid in selected_metrics + selected_dimensions
        if catalog_by_id.get(fid, {}).get("exposure") == "excluded"
    )
    if excluded:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                "google-ads catalog selection refused before the API call: "
                "excluded catalog field(s) selected: " + ", ".join(excluded)
                + " (see exclusion_reason in api_catalog.json; review 26.2 F-8)"
            ),
        )

    # F-4: a non-numeric metric CANNOT land in value_num -- refuse pre-call
    # listing the fields instead of silently dropping rows at landing.
    non_numeric = sorted(
        fid
        for fid in selected_metrics
        if catalog_by_id.get(fid, {}).get("physical_type")
        not in _NUMERIC_PHYSICAL_TYPES
    )
    if non_numeric:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                "google-ads catalog selection refused before the API call: "
                "non-numeric metric(s) cannot land in value_num: "
                + ", ".join(non_numeric)
                + " (select them as dimensions of a future profile or drop "
                "them; review 26.2 F-4)"
            ),
        )

    # Pre-call compatibility refusal (core 26.1 engine, F-1) -- BEFORE token
    # fetch. Strict context by default: {"resource": <FROM resource>}.
    from core.field_compat import enforce_selection  # noqa: PLC0415

    enforce_selection(
        selection, _field_compat_rules(), context={"resource": from_resource}
    )

    cid, login_cid = _resolve_customer_selection(
        connection_id, customer_id, login_customer_id
    )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    source_fields: dict[str, str] = selection.get("source_fields", {})
    pairs: list[tuple[str, str]] = list(_CATALOG_STRUCTURE_TOKENS.items())
    seen_ids = {fid for fid, _ in pairs}
    for field_id in list(selection.get("dimensions", [])) + list(
        selection.get("metrics", [])
    ):
        if field_id in seen_ids:
            continue
        seen_ids.add(field_id)
        pairs.append((field_id, source_fields.get(field_id, field_id)))

    query = _build_gaql(
        [token for _fid, token in pairs], from_resource, date_from, date_to
    )

    api_rows, pages, truncated = _search(connection_id, cid, query, login_cid)

    wide_rows = [_flatten_result_row(r, pairs) for r in api_rows]
    canonical_rows = transform(wide_rows)
    metric_names = _canonical_metric_names(list(selection.get("metrics", [])))

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, metric_names, data_level, pull_id, loaded_at, project_id,
        db_mode, duckdb_path,
    )

    logger.info(
        "google_ads_pull_completed: pull_id=%s profile=catalog_daily row_count=%d "
        "pages=%d selected_metrics=%d selected_dimensions=%d",
        pull_id, row_count, pages,
        len(selection.get("metrics", [])), len(selection.get("dimensions", [])),
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "pages": pages,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Account topology (story 26.2, pattern 25.5): MCC -> client account.
# ---------------------------------------------------------------------------

# customer_client discovery query (dossier section 5). level <= 1 per seed;
# child managers are expanded recursively (nested MCCs, capped at 6 levels).
_CUSTOMER_CLIENT_QUERY = (
    "SELECT customer_client.client_customer, customer_client.level, "
    "customer_client.manager, customer_client.descriptive_name, "
    "customer_client.currency_code, customer_client.time_zone, "
    "customer_client.id, customer_client.status "
    "FROM customer_client WHERE customer_client.level <= 1"
)

_MAX_MCC_DEPTH = 6

# Access-check probe: a MINIMAL 1-day metrics query on the selected account.
# On a manager the provider rejects it with REQUESTED_METRICS_FOR_MANAGER
# (typed invalid_request via the error_map) -- the story's typed refusal.
_ACCESS_CHECK_QUERY = (
    "SELECT segments.date, metrics.impressions FROM customer "
    "WHERE segments.date DURING YESTERDAY LIMIT 1"
)


def _list_accessible_customers(connection_id: str) -> list[str]:
    """CustomerService.listAccessibleCustomers -> directly reachable CIDs."""
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="google-ads")
    resp = httpx.get(
        f"{_api_base_url()}/customers:listAccessibleCustomers",
        headers=_headers(token),
        timeout=30.0,
    )
    if resp.status_code != 200:
        _raise_provider_error(resp.status_code, resp)
    return [
        _normalize_cid(name)
        for name in resp.json().get("resourceNames") or []
        if name
    ]


def _expand_customer_clients(
    connection_id: str, root_cid: str, login_cid: str
) -> list[dict]:
    """customer_client rows (level <= 1) for *root_cid*, ENABLED only."""
    rows, _pages, _truncated = _search(
        connection_id, root_cid, _CUSTOMER_CLIENT_QUERY, login_cid
    )
    out: list[dict] = []
    for row in rows:
        client = row.get("customerClient") or {}
        status = client.get("status")
        if status and status != "ENABLED":
            continue
        out.append(
            {
                "id": _normalize_cid(str(client.get("id", ""))),
                "level": int(client.get("level", 0) or 0),
                "manager": bool(client.get("manager", False)),
                "name": client.get("descriptiveName") or "",
                "currency_code": client.get("currencyCode"),
                "time_zone": client.get("timeZone"),
            }
        )
    return out


def _build_subtree(
    connection_id: str,
    root_cid: str,
    login_cid: str,
    depth: int,
    visited: set[str],
    direct_seeds: frozenset[str] = frozenset(),
) -> tuple[dict | None, list[dict]]:
    """Return (root_node, children_nodes) for one seed/manager expansion.

    Review 26.2 F-9 dedup: a client account reachable BOTH directly (it is a
    listAccessibleCustomers seed) and through an MCC keeps its DIRECT id (the
    seed's own root node); the composite ``<cid>@<login_cid>`` child is
    dropped and the duplicate logged, so one physical account never appears
    under two selectable ids.
    """
    if root_cid in visited or depth > _MAX_MCC_DEPTH:
        return None, []
    visited.add(root_cid)

    rows = _expand_customer_clients(connection_id, root_cid, login_cid)
    self_row = next((r for r in rows if r["level"] == 0), None)
    children_rows = [r for r in rows if r["level"] >= 1]

    children: list[dict] = []
    for child in children_rows:
        if child["manager"]:
            node, _grand = _build_subtree(
                connection_id, child["id"], login_cid, depth + 1, visited,
                direct_seeds,
            )
            if node is not None:
                children.append(node)
        elif child["id"] in direct_seeds:
            # F-9: also directly accessible -- keep the direct id, log the dup.
            logger.info(
                "google_ads_topology_duplicate_account: customer %s reachable "
                "directly AND via MCC %s -- keeping the direct id",
                child["id"], login_cid,
            )
        else:
            children.append(
                {
                    # Composite opaque id: pulls recover login-customer-id
                    # without a re-discovery (see _split_selected_account).
                    "id": f"{child['id']}@{login_cid}",
                    "label": child["name"] or child["id"],
                    "level": "client_account",
                    "manager": False,
                    "currency_code": child.get("currency_code"),
                    "time_zone": child.get("time_zone"),
                }
            )

    is_manager = bool(self_row and self_row["manager"]) or bool(children)
    label = (self_row or {}).get("name") or root_cid
    node = {
        "id": root_cid,
        "label": f"{label} (MCC)" if is_manager else label,
        "level": "manager" if is_manager else "client_account",
        "manager": is_manager,
    }
    if not is_manager and self_row:
        node["currency_code"] = self_row.get("currency_code")
        node["time_zone"] = self_row.get("time_zone")
    if children:
        node["children"] = children
    return node, children


def discover_accounts(connection_id: str) -> list[dict]:
    """List the account hierarchy the connection's token can reach.

    Story 26.2 (pattern 25.5): listAccessibleCustomers -> per-seed
    customer_client expansion (level, manager flag, descriptive_name, currency,
    time zone; status filtered to ENABLED). Returns the generic nested list
    core's topology flow consumes:

        [{"id": "<mcc_cid>", "label": "Acme (MCC)", "manager": true,
          "children": [{"id": "<cid>@<mcc_cid>", "label": "Client", ...}]},
         {"id": "<cid>", "label": "Standalone client", "manager": false}]

    Client accounts reached through an MCC carry the composite opaque id
    '<client_cid>@<login_cid>'; ids are opaque to core (AD-2). Managers appear
    in the tree for labeling, but they hold NO metrics -- check_account_access
    is the typed guard (REQUESTED_METRICS_FOR_MANAGER).

    Review 26.2 F-9 -- per-seed tolerance and dedup:
      - a seed whose expansion fails (403 on a manager the token only
        partially reaches, transient provider error, ...) is logged as a
        WARNING and SKIPPED; the other seeds still discover. RateLimitError
        (429) still propagates (breaker path).
      - a client reachable directly AND through an MCC keeps its DIRECT id
        (the composite duplicate is dropped and logged).
      - KNOWN LIMIT: a sub-MCC reachable through TWO different root MCCs is
        expanded only under the first root visited (the ``visited`` guard
        prevents infinite loops); its clients then carry that first root as
        login_cid. Both routes are valid for Google (any ancestor MCC works
        as login-customer-id), so pulls are unaffected -- only the tree
        placement is first-root-wins.

    Raises core.quota.RateLimitError on 429; typed ConnectorError only when
    EVERY seed fails (no silent empty discovery).
    """
    from core import pull_errors  # noqa: PLC0415

    seeds = _list_accessible_customers(connection_id)
    direct_seeds = frozenset(seeds)
    accounts: list[dict] = []
    visited: set[str] = set()
    failed: list[str] = []
    last_error: Exception | None = None
    for seed in seeds:
        try:
            node, _children = _build_subtree(
                connection_id, seed, seed, 1, visited, direct_seeds
            )
        except pull_errors.ConnectorError as exc:
            # F-9: per-seed tolerance -- one unreachable seed must not kill
            # the whole discovery (typical: 403 USER_PERMISSION_DENIED on a
            # foreign manager the token lists but cannot query).
            failed.append(seed)
            last_error = exc
            logger.warning(
                "google_ads_discover_seed_failed: seed=%s error_class=%s -- "
                "skipped, discovery continues", seed, exc.error_class,
            )
            continue
        if node is not None:
            accounts.append(node)

    if seeds and failed and not accounts:
        # Every seed failed: surface the last typed error instead of returning
        # an empty tree that looks like "no accounts".
        raise last_error

    logger.info(
        "google_ads_discover_accounts: seeds=%d nodes=%d failed_seeds=%d",
        len(seeds), len(accounts), len(failed),
    )
    return accounts


def check_account_access(connection_id: str, account_id: str) -> dict:
    """Access-check the SELECTED reporting account (story 26.2 / 25.5 step 3).

    Runs the minimal 1-day metrics probe on the account. A selected MANAGER
    account is rejected by the provider with REQUESTED_METRICS_FOR_MANAGER
    -> typed invalid_request via the error_map (the story's typed refusal:
    managers hold no metrics; select a client account instead).

    Returns {"account_id", "customer_id", "login_customer_id", "ok": True}
    on success; raises the typed ConnectorError otherwise.
    """
    cid, login_cid = _split_selected_account(account_id)
    _rows, _pages, _truncated = _search(
        connection_id, cid, _ACCESS_CHECK_QUERY, login_cid
    )
    return {
        "account_id": account_id,
        "customer_id": cid,
        "login_customer_id": login_cid,
        "ok": True,
    }


# ---------------------------------------------------------------------------
# MCP tool -- reads from fact_daily_kpi mart (AD-12).
# ---------------------------------------------------------------------------


def _query_duckdb(sql: str, params: list, duckdb_path: str) -> list[dict]:
    """Execute parameterized *sql* against the local DuckDB warehouse (F-01)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
    """Execute parameterized *sql* against BigQuery (F-01: @named parameters)."""
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
    """Fully-qualified mart table reference per engine (F-02)."""
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


# SQL body shared by both engines; user-supplied values NEVER enter the string.
_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'google-ads'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    """Query fact_daily_kpi mart -- DuckDB or BigQuery depending on env.

    # AD-12: MCP server reads marts only -- never raw_* tables or CSV
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

    # NFR8: provenance is a full dict, not a scalar pull_id string.
    provenance = (
        {
            "source_system": "google-ads",
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
def get_google_ads_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Google Ads Performance Report -- reads from fact_daily_kpi mart.

    Report profiles: campaign_daily, ad_group_daily, ad_daily, keyword_daily,
                     search_term_daily, catalog_daily
    Metrics: cost, impressions, clicks, conversions, conversions_value,
             all_conversions, view_through_conversions (additive at day grain)
    Breakdown dimensions: campaign_id, ad_group_id, ad_id

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    # AD-4: cost lands in currency units (micros/1e6 at landing). Conversion
    # metrics are restated by Google for up to 90 days (refetch block).

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of the 6 declared profiles
        date_from: Start date ISO-8601 (e.g. '2026-04-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    # AD-12: MCP server reads fact_daily_kpi mart only -- never raw_* tables.
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
# Register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_google_ads_daily", provider="google-ads")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
