"""Pinterest Ads connector -- Story 26.3 (catalog-first, API v5, async reports).

Exposes a ``mcp_app: FastMCP`` instance that the core loader mounts under the
``pinterest-ads`` namespace (AD-2). Built to the epic-25 industrial standard
from day one: generated api_catalog.json (OpenAPI 5.28.0 enums: 626 async +
1 sync-only = 627 official columns + the ``date`` grain dimension, ZERO
planned), body-code-keyed error_map, flat ad-account topology, three sync
exact_bundle profiles and a catalog_driven profile executed through the 26.1
async-report socle (core.async_reports.run_async_report).

# AD-12: MCP server reads the fact_daily_kpi mart only -- no raw_* tables.
# AD-3:  token obtained via nango_client.get_fresh_token immediately before
#        use (Nango path -- Pinterest is a NON-Google provider). Never
#        stored/logged. NB the Pinterest refresh token is ROTATING (60-day
#        continuous refresh since 2025-09-25): Nango must re-persist it on
#        every refresh -- probe-only check, see ROLLOUT_NOTES.md.
# AD-7:  pull_id minted by the core scheduler and passed into pull().
# AD-2:  request column lists are DERIVED from the manifest/catalog -- no
#        hardcoded column lists outside the catalog. The API version is
#        pinned in manifest.json (provider_api_version) and read from there
#        ONLY.
# AD-4:  ratio/derived metrics carry a non-additive note in the catalog and
#        are landed RAW at day grain when selected -- never summed downstream.
# AI-03: ASCII-only stdout/log strings.
#
# API facts (research dossier pinterest-ads-catalog-research.md, 2026-07-21):
#   - Sync analytics: GET {base}/v5/ad_accounts/{aid}/campaigns|ad_groups|ads
#     /analytics with start_date/end_date/columns/granularity + explicit
#     attribution windows. Requires entity ids (max 250 per call) -- the
#     connector lists entities first (bookmark pagination) and batches.
#     Window limits: 90 days back / 90-day range (HOUR: 8/3 -- not used v1).
#   - Async reports: POST /ad_accounts/{aid}/reports (columns = ANY of the
#     626 async enum values), poll GET /reports?token=, download the returned
#     url IMMEDIATELY (valid 5 minutes; report 1 hour). Window limits:
#     914 days back / 186-day range at granularity != HOUR.
#   - Error bodies are {"code": <int>, "message": <str>}: the manifest
#     error_map keys are '<status>:<code>' and the generic core extractor
#     (core.pull_errors._extract_provider_code) reads the top-level "code".
#     THE trap: 400 code 12 ("Retry after ...") is TRANSIENT. 429 code 8 ->
#     RateLimitError breaker path with retry_after from x-ratelimit-reset.
#   - Micro-currency columns (*_IN_MICRO_DOLLAR) are landed as value/1e6.
#     The conversion set is derived FROM THE CATALOG (the 'value/1e6' marker
#     stamped per field by build_official_fields.py) -- never from an
#     id-suffix heuristic (google-ads 26.2 review lesson).
"""

from __future__ import annotations

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
mcp_app = FastMCP("pinterest-ads")

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (no hardcoded paths).
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# Manifest / catalog / catalog_sources access (cached). The API version is
# pinned in ONE place: manifest.json provider_api_version (story 26.3).
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


def _api_base_url() -> str:
    """Provider endpoint root, built from the manifest pin (single source)."""
    manifest = _load_manifest()
    base = manifest.get("provider_api_base", "https://api.pinterest.com")
    version = manifest.get("provider_api_version", "v5")
    return f"{base}/{version}"


def _load_api_catalog() -> dict:
    global _API_CATALOG
    try:
        return _API_CATALOG  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "api_catalog.json"
    globals()["_API_CATALOG"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_API_CATALOG"]


def _load_catalog_sources() -> dict:
    global _CATALOG_SOURCES
    try:
        return _CATALOG_SOURCES  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "catalog_sources" / "catalog_sources.json"
    globals()["_CATALOG_SOURCES"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_CATALOG_SOURCES"]


def _request_limits() -> dict:
    """The module-local request-parameter limits block (catalog_sources)."""
    return _load_catalog_sources().get("_request_limits", {})


def _catalog_fields_by_id() -> dict[str, dict]:
    global _FIELDS_BY_ID
    try:
        return _FIELDS_BY_ID  # type: ignore[name-defined]
    except NameError:
        pass
    globals()["_FIELDS_BY_ID"] = {
        f["field_id"]: f for f in _load_api_catalog().get("fields", [])
    }
    return globals()["_FIELDS_BY_ID"]


# Catalog marker driving the micro-currency landing rule. The set is derived
# FROM the catalog descriptions ('value/1e6' stamped per field by
# build_official_fields.py) -- NEVER from an id-suffix heuristic in the
# transform path (google-ads 26.2 review lesson).
_MICRO_MARKER = "value/1e6"


def _micro_field_ids() -> frozenset[str]:
    global _MICRO_FIELDS
    try:
        return _MICRO_FIELDS  # type: ignore[name-defined]
    except NameError:
        pass
    globals()["_MICRO_FIELDS"] = frozenset(
        f["field_id"]
        for f in _load_api_catalog().get("fields", [])
        if _MICRO_MARKER in f.get("description", "")
    )
    return globals()["_MICRO_FIELDS"]


# ---------------------------------------------------------------------------
# Provider error handling (story 26.3, dossier section 7).
#
# Pinterest v5 error bodies are {"code": <int>, "message": "<str>"} -- the
# generic core extractor reads the top-level "code", so classify_http_error
# with the manifest error_map ('<status>:<code>' keys) does the refinement.
# Trap encoded in the map: 400 code 12 ("Retry after ...") is TRANSIENT.
# ---------------------------------------------------------------------------

_RETRY_AFTER_RE = re.compile(r"retry after", re.IGNORECASE)


def _retry_after_seconds(resp: httpx.Response | None) -> int | None:
    """Best-effort seconds-to-wait from x-ratelimit-reset / Retry-After."""
    if resp is None:
        return None
    for header in ("x-ratelimit-reset", "Retry-After"):
        raw = resp.headers.get(header)
        if raw:
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def _raise_provider_error(status_code: int, resp: httpx.Response | None = None,
                          body=None) -> None:
    """Raise the canonical typed error for a non-2xx Pinterest response.

    429 (body code 8, 'This request exceeded a rate limit') -> the
    core.quota.RateLimitError breaker path, retry_after honored from the
    x-ratelimit-reset header. Everything else -> classify_http_error with the
    manifest error_map ('<status>:<body_code>' keys). Supplementary guard
    (Airbyte cross-check): a 400 whose message contains 'Retry after' is
    transient even if the body code drifted from 12.
    """
    if body is None and resp is not None:
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    if status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raise RateLimitError("pinterest-ads", _retry_after_seconds(resp))

    from core import pull_errors  # noqa: PLC0415

    if status_code == 400 and isinstance(body, dict):
        message = str(body.get("message", ""))
        if body.get("code") != 12 and _RETRY_AFTER_RE.search(message):
            # Body-code drift guard: the message still says transient.
            raise pull_errors.ProviderTransientError(
                provider_status=status_code, provider_payload=body
            )

    raise pull_errors.classify_http_error(status_code, body, _load_error_map())


# ---------------------------------------------------------------------------
# Auth -- Nango (non-Google provider). Token fetched immediately before use.
# ---------------------------------------------------------------------------


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get_token(connection_id: str) -> str:
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    return nango_client.get_fresh_token(connection_id, provider="pinterest-ads")


def _resolve_ad_account(connection_id: str, ad_account_id: str | None) -> str:
    """Resolve the reporting ad account for a pull.

    Explicit arg wins (tests / direct calls). Otherwise the ONLY source is the
    core topology scope (story 25.5 flow: pending_account_selection -> ready).
    There is NO env-var account fallback (PINTEREST_ADS_*_ID is forbidden by
    doctrine): an unselected account is a hard, actionable error.
    """
    if ad_account_id:
        return str(ad_account_id)

    selected: str | None = None
    try:
        from core import account_topology, token_service  # noqa: PLC0415

        resolved = token_service.resolve_connection_by_nango_id(connection_id)
        if resolved is not None:
            selected = account_topology.resolve_selected_account(resolved.id)
    except (LookupError, ValueError) as exc:
        # Review 26.3 F-9: only the EXPECTED resolution errors fall through to
        # the actionable no-selected-account message below; anything else
        # (import/DB/programming errors) must surface, not be swallowed.
        logger.warning(
            "pinterest_ads_account_resolution_failed: %s", type(exc).__name__
        )

    if not selected:
        raise ValueError(
            "no selected reporting ad account for this pinterest-ads connection"
            " -- run the account topology flow (discovery -> selection ->"
            " access check); env-var account fallbacks do not exist for this"
            " module"
        )
    return str(selected)


# ---------------------------------------------------------------------------
# Request validation: dates, windows, columns, levels (pre-call refusals).
# ---------------------------------------------------------------------------

_COLUMN_CHARSET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Attribution spec pinned by the story (API defaults made explicit -- stored
# as datastream metadata by the scheduler; returned in every pull result).
_ATTRIBUTION_SPEC = {
    "click_window_days": 30,
    "engagement_window_days": 30,
    "view_window_days": 1,
    "conversion_report_time": "TIME_OF_AD_ACTION",
}


def _invalid_request(message: str, payload=None):
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    return InvalidRequestError(
        provider_status=None, provider_payload=payload, message=message
    )


def _parse_window(date_from: str, date_to: str) -> tuple[date, date]:
    """Strict ISO dates via date.fromisoformat (26.2 review lesson)."""
    try:
        start = date.fromisoformat(str(date_from))
        end = date.fromisoformat(str(date_to))
    except (TypeError, ValueError) as exc:
        raise _invalid_request(
            f"pinterest-ads: dates must be ISO-8601 YYYY-MM-DD"
            f" (date_from={date_from!r} date_to={date_to!r}): {exc}"
        ) from exc
    if end < start:
        raise _invalid_request(
            f"pinterest-ads: date_to {date_to!r} precedes date_from {date_from!r}"
        )
    return start, end


def _check_window_limits(start: date, end: date, limits_key: str,
                         today: date | None = None) -> None:
    """Refuse a window the provider documents as illegal BEFORE the call."""
    limits = (_request_limits().get("date_window_limits") or {}).get(limits_key)
    if not limits:
        raise ValueError(
            f"pinterest-ads: unknown date_window_limits key {limits_key!r}"
            " (catalog_sources.json _request_limits)"
        )
    if today is None:
        today = datetime.now(tz=timezone.utc).date()
    max_back = int(limits["max_days_back"])
    max_range = int(limits["max_range_days"])
    if (today - start).days > max_back:
        raise _invalid_request(
            f"pinterest-ads: date_from {start.isoformat()} is more than"
            f" {max_back} days in the past for this request type"
            f" ({limits_key}) -- refused before the API call"
        )
    if (end - start).days + 1 > max_range:
        raise _invalid_request(
            f"pinterest-ads: window {start.isoformat()}..{end.isoformat()}"
            f" exceeds the {max_range}-day per-request limit ({limits_key})"
            " -- refused before the API call (core chunks windows)"
        )


def _checked_columns(field_ids: list[str]) -> list[str]:
    """Map catalog field ids -> provider column tokens, verified against the
    catalog enum + the ^[A-Z][A-Z0-9_]*$ charset. The 'date' grain dimension
    is NOT a requestable column (the DATE key comes back on every row).
    """
    by_id = _catalog_fields_by_id()
    columns: list[str] = []
    for field_id in field_ids:
        if field_id == "date":
            continue
        entry = by_id.get(field_id)
        if entry is None:
            raise _invalid_request(
                f"pinterest-ads: field {field_id!r} is not in api_catalog.json"
                " (catalog drift -- the catalog is the execution contract)"
            )
        token = entry.get("source_field", field_id)
        if not _COLUMN_CHARSET_RE.match(token):
            raise _invalid_request(
                f"pinterest-ads: column token {token!r} (field {field_id!r})"
                " violates the ^[A-Z][A-Z0-9_]*$ charset -- refused"
            )
        if token not in columns:
            columns.append(token)
    return columns


# ---------------------------------------------------------------------------
# HTTP helpers: entity listing (bookmark pagination) + sync analytics.
# ---------------------------------------------------------------------------

_ENTITY_BATCH_MAX = 250
_MAX_BOOKMARK_PAGES = 100

# profile -> (entity path segment, ids query param, data_level stamp,
#             async report level)
_PROFILE_SPECS: dict[str, tuple[str, str, str, str]] = {
    "campaign_daily": ("campaigns", "campaign_ids", "CAMPAIGN", "CAMPAIGN"),
    "ad_group_daily": ("ad_groups", "ad_group_ids", "AD_GROUP", "AD_GROUP"),
    "ad_daily": ("ads", "ad_ids", "AD", "PIN_PROMOTION"),
}


def _get_json(token: str, url: str, params: dict) -> dict | list:
    resp = httpx.get(url, headers=_headers(token), params=params, timeout=60.0)
    if resp.status_code != 200:
        _raise_provider_error(resp.status_code, resp)
    return resp.json()


def _list_entity_ids(
    token: str, ad_account_id: str, entity_path: str
) -> tuple[list[str], bool]:
    """List entity ids (campaigns/ad_groups/ads) via bookmark pagination.

    Returns ``(ids, truncated)`` -- truncated is True when the page cap was
    hit with a bookmark still pending (review 26.3 F-6, google-ads pattern):
    the caller propagates the flag in the pull result instead of silently
    landing a partial entity universe.
    """
    url = f"{_api_base_url()}/ad_accounts/{ad_account_id}/{entity_path}"
    ids: list[str] = []
    bookmark: str | None = None
    pages = 0
    truncated = False
    while True:
        params: dict = {"page_size": _ENTITY_BATCH_MAX}
        if bookmark:
            params["bookmark"] = bookmark
        payload = _get_json(token, url, params)
        items = payload.get("items") or [] if isinstance(payload, dict) else []
        ids.extend(str(item["id"]) for item in items if item.get("id"))
        bookmark = payload.get("bookmark") if isinstance(payload, dict) else None
        pages += 1
        if not bookmark:
            break
        if pages >= _MAX_BOOKMARK_PAGES:
            truncated = True
            logger.warning(
                "pinterest_ads_entity_list_truncated: page cap %d reached (%d ids)",
                _MAX_BOOKMARK_PAGES, len(ids),
            )
            break
    return ids, truncated


def _sync_analytics_rows(
    token: str,
    ad_account_id: str,
    entity_path: str,
    ids_param: str,
    entity_ids: list[str],
    columns: list[str],
    date_from: str,
    date_to: str,
) -> tuple[list[dict], int]:
    """Batched GET .../{entity_path}/analytics calls (max 250 ids each)."""
    url = f"{_api_base_url()}/ad_accounts/{ad_account_id}/{entity_path}/analytics"
    rows: list[dict] = []
    requests_made = 0
    for index in range(0, len(entity_ids), _ENTITY_BATCH_MAX):
        batch = entity_ids[index:index + _ENTITY_BATCH_MAX]
        params = {
            "start_date": date_from,
            "end_date": date_to,
            ids_param: ",".join(batch),
            "columns": ",".join(columns),
            "granularity": "DAY",
            **_ATTRIBUTION_SPEC,
        }
        payload = _get_json(token, url, params)
        if isinstance(payload, list):
            rows.extend(r for r in payload if isinstance(r, dict))
        requests_made += 1
    return rows, requests_made


# ---------------------------------------------------------------------------
# Row flattening + transform (manifest-driven, AD-2).
# ---------------------------------------------------------------------------


def _flatten_row(api_row: dict, field_ids: list[str]) -> dict:
    """One provider row -> wide dict keyed by catalog field_id.

    The provider keys rows by the COLUMN token (enum name) plus the DATE
    grain key; the catalog maps field_id <-> source_field ('date' <- 'DATE').
    """
    by_id = _catalog_fields_by_id()
    row: dict = {}
    for field_id in field_ids:
        entry = by_id.get(field_id) or {}
        token = entry.get("source_field", field_id)
        row[field_id] = api_row.get(token)
    return row


def _rename_map() -> dict[str, str]:
    """Field-id -> canonical landed name map (metrics + dimensions), cached.

    The SINGLE source of the enum-id -> landed-name mapping: transform() renames
    rows with it, and the descriptive_mutable landing (story 26.6 Part A) uses
    it to recognise a mutable attribute whether it lands under its enum id or
    its canonical name (e.g. CAMPAIGN_ENTITY_STATUS -> campaign_status).
    """
    try:
        return _RENAME_MAP  # type: ignore[name-defined]
    except NameError:
        pass
    manifest = _load_manifest()
    rename_map: dict[str, str] = {}
    for src, val in manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            rename_map[src] = val.get("canonical", src)
    rename_map.update(manifest.get("canonical_dimension_mapping", {}))
    globals()["_RENAME_MAP"] = rename_map
    return rename_map


def _descriptive_mutable_landed_names() -> frozenset[str]:
    """Story 26.6 Part A: landed column names that are entity STATE/TYPE
    attributes (latest-wins), so they go to attributes_json, NOT segments_json.

    Read from catalog_sources.json (the GENERATED descriptive_mutable block --
    api_catalog.json's core schema forbids extra field keys and is out of
    scope). Each enum id is expanded to BOTH itself AND its canonical landed
    name so the split works whether a mutable attribute lands under its enum id
    or its canonical name.
    """
    try:
        return _DESCRIPTIVE_MUTABLE  # type: ignore[name-defined]
    except NameError:
        pass
    block = _load_catalog_sources().get("descriptive_mutable") or {}
    field_ids = block.get("field_ids") or []
    rename_map = _rename_map()
    names: set[str] = set()
    for fid in field_ids:
        names.add(fid)
        names.add(rename_map.get(fid, fid))
    globals()["_DESCRIPTIVE_MUTABLE"] = frozenset(names)
    return globals()["_DESCRIPTIVE_MUTABLE"]


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map field_id-keyed rows to canonical names using manifest mappings.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_
    mapping. Fields absent from both mappings pass through unchanged.

    Micros rule (story 26.3): fields carrying the catalog 'value/1e6' marker
    (micro-currency columns) are divided by 1e6 HERE, exactly once, so
    value_num always lands in currency units. The conversion set comes FROM
    THE CATALOG -- never from an id-suffix heuristic (26.2 review lesson).
    """
    rename_map = _rename_map()

    micro_fields = _micro_field_ids()

    result: list[dict] = []
    for row in raw_rows:
        canonical: dict = {}
        for key, value in row.items():
            if key in micro_fields and value is not None:
                try:
                    value = float(value) / 1e6
                except (ValueError, TypeError):
                    pass
            canonical[rename_map.get(key, key)] = value
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
# Raw landing -- LONG format: one row per grain x metric (26.2 pattern).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_pinterest_ads_daily (
    date              VARCHAR,
    data_level        VARCHAR,
    ad_account_id     VARCHAR,
    campaign_id       VARCHAR,
    campaign_name     VARCHAR,
    ad_group_id       VARCHAR,
    ad_id             VARCHAR,
    pin_id            VARCHAR,
    product_group_id  VARCHAR,
    segments_json     VARCHAR,
    attributes_json   VARCHAR,
    metric            VARCHAR,
    value_num         DOUBLE,
    pull_id           VARCHAR,
    loaded_at         VARCHAR,
    project_id        VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_pinterest_ads_daily
    (date, data_level, ad_account_id, campaign_id, campaign_name, ad_group_id,
     ad_id, pin_id, product_group_id, segments_json, attributes_json, metric,
     value_num, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_STRUCTURE_COLUMNS = (
    "date", "ad_account_id", "campaign_id", "campaign_name", "ad_group_id",
    "ad_id", "pin_id", "product_group_id",
)
_NON_SEGMENT_KEYS = set(_STRUCTURE_COLUMNS) | {"pull_id", "connector", "data_level"}


def _insert_raw_rows(
    rows: list[dict],
    metric_names: list[str],
    data_level: str,
    ad_account_id: str,
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> tuple[int, int]:
    """Long-ify canonical wide rows and insert into raw_pinterest_ads_daily.

    One long row per (grain, metric) with value_num. NULL honnete (AD-9): an
    absent metric lands NO row -- absence stays distinguishable from a
    recorded zero. Non-structural dimension values split TWO ways:
      - GRAIN-BEARING dimensions (breakdowns that produce distinct provider
        rows) land in segments_json (canonical sorted-key JSON) -- part of the
        QUALIFY supersede grain key;
      - DESCRIPTIVE MUTABLE attributes (entity STATE/TYPE: statuses, objective/
        budget types -- story 26.6 Part A) land in attributes_json (latest-wins)
        which the dbt staging keeps OUT of the partition. This stops a refetch
        that re-reads a window with a changed status from producing a second
        segments_json for the same grain and DOUBLING the window's metrics.

    Review 26.3 F-5: a NON-NUMERIC metric value is never silently dropped --
    it lands in segments_json under the metric's field id (observable in the
    row's grain), a warning is logged, and the count is returned so the pull
    result reports it (``non_numeric_landed``).

    Returns ``(row_count, non_numeric_landed)``.
    """
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev"
            " (BigQuery path not yet implemented)"
        )
    from core import warehouse_write  # noqa: PLC0415

    metric_set = list(dict.fromkeys(metric_names))
    mutable_names = _descriptive_mutable_landed_names()
    values: list[tuple] = []
    non_numeric_landed = 0
    for row in rows:
        # Non-key, non-metric dimension values split into the supersede GRAIN
        # (segments_json) vs latest-wins ATTRIBUTES (attributes_json, story
        # 26.6 Part A). A non-numeric metric value NEVER goes to attributes:
        # it lands in segments_json (observable in the grain, F-5) so it is
        # never dropped and never mistaken for an entity attribute.
        segment_items: dict = {}
        attribute_items: dict = {}
        for k in row:
            if k in _NON_SEGMENT_KEYS or k in metric_set or row[k] is None:
                continue
            if k in mutable_names:
                attribute_items[k] = row[k]
            else:
                segment_items[k] = row[k]
        numeric_metrics: list[tuple[str, float]] = []
        for metric in metric_set:
            value = row.get(metric)
            if value is None:
                continue
            try:
                numeric_metrics.append((metric, float(value)))
            except (ValueError, TypeError):
                # Non-numeric metric value: land it in segments_json (key =
                # the metric field id) instead of corrupting value_num --
                # never a silent drop (review 26.3 F-5).
                segment_items[metric] = value
                non_numeric_landed += 1
                logger.warning(
                    "pinterest_ads_non_numeric_metric_landed: metric=%s"
                    " pull_id=%s value landed in segments_json (not value_num)",
                    metric, pull_id,
                )
        segments_json = (
            json.dumps(segment_items, sort_keys=True) if segment_items else None
        )
        attributes_json = (
            json.dumps(attribute_items, sort_keys=True) if attribute_items else None
        )
        for metric, value_num in numeric_metrics:
            values.append(
                (
                    row.get("date", ""),
                    data_level,
                    row.get("ad_account_id") or ad_account_id,
                    row.get("campaign_id") or "",
                    row.get("campaign_name") or "",
                    row.get("ad_group_id") or "",
                    row.get("ad_id") or "",
                    row.get("pin_id") or "",
                    row.get("product_group_id") or "",
                    segments_json,
                    attributes_json,
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
    return len(values), non_numeric_landed


# ---------------------------------------------------------------------------
# Shared sync pull core (exact_bundle profiles).
# ---------------------------------------------------------------------------


def _profile_field_ids(profile_id: str) -> tuple[list[str], list[str]]:
    """(dimension_ids, metric_ids) of an exact_bundle profile (AD-2: from the
    manifest capability report, never hardcoded here)."""
    caps = _load_manifest().get("source_capabilities", {})
    report = next(
        (r for r in caps.get("reports", []) if r.get("id") == profile_id), None
    )
    if report is None:
        raise ValueError(f"Unknown pinterest-ads report profile: {profile_id!r}")
    return list(report.get("dimensions", [])), list(report.get("metrics", []))


def _pull_sync(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    ad_account_id: str | None = None,
) -> dict:
    """Run one exact_bundle sync analytics profile and land LONG rows.

    # AD-12: called by the queue worker only.
    # AD-3: token used immediately then discarded.
    # AD-7: pull_id minted by the caller.

    Raises
    ------
    core.pull_errors.InvalidRequestError
        Window outside the documented sync limits (refused BEFORE any call),
        or a provider 400 on a cataloged column (drift signal).
    core.quota.RateLimitError
        HTTP 429 (breaker path, retry_after from x-ratelimit-reset).
    core.pull_errors.ConnectorError
        Typed canonical error for any other non-2xx (manifest error_map,
        body-code keys).
    """
    if profile not in _PROFILE_SPECS:
        raise ValueError(f"Unknown pinterest-ads report profile: {profile!r}")
    entity_path, ids_param, data_level, _level = _PROFILE_SPECS[profile]

    start, end = _parse_window(date_from, date_to)
    _check_window_limits(start, end, "sync_default")

    account = _resolve_ad_account(connection_id, ad_account_id)
    dim_ids, metric_ids = _profile_field_ids(profile)
    columns = _checked_columns(dim_ids + metric_ids)

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    token = _get_token(connection_id)
    entity_ids, truncated = _list_entity_ids(token, account, entity_path)
    api_rows: list[dict] = []
    requests_made = 0
    if entity_ids:
        api_rows, requests_made = _sync_analytics_rows(
            token, account, entity_path, ids_param, entity_ids, columns,
            date_from, date_to,
        )
    del token  # AD-3: out of scope immediately after use

    field_ids = dim_ids + metric_ids
    wide_rows = [_flatten_row(r, field_ids) for r in api_rows]
    canonical_rows = transform(wide_rows)
    metric_names = _canonical_metric_names(metric_ids)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count, non_numeric_landed = _insert_raw_rows(
        canonical_rows, metric_names, data_level, account, pull_id, loaded_at,
        project_id, db_mode, duckdb_path,
    )

    # AD-3: no token in logs -- only pull_id / counts (safe metadata).
    logger.info(
        "pinterest_ads_pull_completed: pull_id=%s profile=%s row_count=%d"
        " entities=%d requests=%d truncated=%s non_numeric_landed=%d",
        pull_id, profile, row_count, len(entity_ids), requests_made,
        truncated, non_numeric_landed,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "entities": len(entity_ids),
        "truncated": truncated,
        "non_numeric_landed": non_numeric_landed,
        "attribution": dict(_ATTRIBUTION_SPEC),
    }


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    ad_account_id: str | None = None,
) -> dict:
    """Default pull() = campaign grain (AI-58 profile-less default dispatch)."""
    return _pull_sync(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", ad_account_id=ad_account_id,
    )


# ---------------------------------------------------------------------------
# AI-58 per-profile dispatch: pull_<profile_id>, declared as dispatch.callable
# in the manifest capability reports. All delegate to the shared code paths.
# ---------------------------------------------------------------------------


def pull_campaign_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, ad_account_id: str | None = None,
) -> dict:
    """AI-58 dispatch: campaign grain (sync analytics, data_level=CAMPAIGN)."""
    return _pull_sync(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", ad_account_id=ad_account_id,
    )


def pull_ad_group_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, ad_account_id: str | None = None,
) -> dict:
    """AI-58 dispatch: ad group grain (sync analytics, data_level=AD_GROUP)."""
    return _pull_sync(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="ad_group_daily", ad_account_id=ad_account_id,
    )


def pull_ad_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, ad_account_id: str | None = None,
) -> dict:
    """AI-58 dispatch: ad grain (sync analytics, data_level=AD)."""
    return _pull_sync(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="ad_daily", ad_account_id=ad_account_id,
    )


# ---------------------------------------------------------------------------
# Story 26.3 -- catalog_driven execution: pull_catalog_daily (ASYNC path).
#
# The 627-column official space is reachable ONLY through the async report
# cycle (create -> poll -> download; link valid 5 minutes). The submit/poll/
# download callables below are driven EXCLUSIVELY by
# core.async_reports.run_async_report (26.1 socle): claim-row submit, re-poll
# resumption on the deterministic request_hash, single resubmission on
# EXPIRED/CANCELLED/DOES_NOT_EXIST or a dead download link, bounded deadline
# whose DEFERRED outcome is surfaced as a retryable ProviderTransientError
# (review 26.3 F-2: deferred is never a completed pull; the persisted
# in-flight reference resumes by re-poll on the queue retry).
# ---------------------------------------------------------------------------


def _level_map() -> dict[str, str]:
    """Whitelisted async report level -> data_level stamp (catalog_sources)."""
    levels = _request_limits().get("levels_whitelist")
    if not levels:
        raise ValueError(
            "pinterest-ads: catalog_sources.json _request_limits.levels_whitelist"
            " is missing (module contract)"
        )
    return dict(levels)


def _prune_default_selection(selection: dict, level: str) -> dict:
    """Prune the MODULE-RESOLVED tier-core default selection (None fallback).

    26.2 review lesson: the tier-core default spans the whole catalog, so for
    the ``selection is None`` fallback ONLY the module prunes it to what the
    async request shape can honor at *level*, and logs what it dropped:
      - dimensions incompatible with the level (field_compatibility rules);
      - non-numeric metric columns (physical_type outside integer/decimal --
        the LONG landing carries value_num).
    A USER selection is NEVER pruned -- incompatibilities there are typed
    refusals (enforce_selection), per the never-silently-dropped contract.
    """
    from core import field_compat  # noqa: PLC0415

    rules = _load_catalog_sources().get(field_compat.FIELD_COMPATIBILITY_KEY)
    by_id = _catalog_fields_by_id()
    context = {"level": level}

    def _level_ok(field_id: str) -> bool:
        probe = {"metrics": [], "dimensions": [field_id]}
        return not field_compat.validate_selection(probe, rules, context=context)

    kept_dims = [f for f in selection.get("dimensions", []) if _level_ok(f)]
    kept_metrics = [
        f
        for f in selection.get("metrics", [])
        if (by_id.get(f, {}).get("physical_type") in ("integer", "decimal"))
        and _level_ok(f)
    ]
    dropped = sorted(
        (set(selection.get("dimensions", [])) - set(kept_dims))
        | (set(selection.get("metrics", [])) - set(kept_metrics))
    )
    if dropped:
        logger.info(
            "pinterest_ads_default_selection_pruned: dropped=%d fields for"
            " level=%s (default selection only): %s",
            len(dropped), level, ",".join(dropped),
        )
    source_fields = selection.get("source_fields", {})
    return {
        "metrics": kept_metrics,
        "dimensions": kept_dims,
        "source_fields": {
            f: source_fields.get(f, f) for f in kept_metrics + kept_dims
        },
    }


def _resolved_core_default_selection() -> dict:
    """The catalog tier-core default AS THE CORE QUEUE RESOLVES IT (cached).

    Story 26.6 Part B (amazon-ads pattern, connector.py ~1286): core/queue.py
    resolves a ``selection=None`` catalog_driven job to this exact shape
    (validate_selection(catalog, catalog_default_selection(catalog))) and passes
    it to the pull as an EXPLICIT selection. The module must recognise it so it
    can apply the SAME prune it applies to selection=None -- otherwise every
    scheduled run with no user selection dies on the pre-call refusals (the
    tier-core default spans the whole catalog, deeper-level dims a CAMPAIGN
    report cannot honor). A genuine USER selection is NEVER pruned.
    """
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
    """True when *selection* is the core-resolved tier-core default (Part B).

    Compares the SETS of metric/dimension ids (order-independent): a genuine
    user selection with any different field membership is never treated as the
    prunable default -- it stays a typed refusal on incompatibility.
    """
    default = _resolved_core_default_selection()
    return (
        set(selection.get("metrics", []) or [])
        == set(default.get("metrics", []) or [])
        and set(selection.get("dimensions", []) or [])
        == set(default.get("dimensions", []) or [])
    )


def _refuse_excluded(field_ids: list[str]) -> None:
    """Typed pre-call refusal for exposure=excluded selections (no silent drop)."""
    by_id = _catalog_fields_by_id()
    excluded = {
        f: by_id[f].get("exclusion_reason", "excluded")
        for f in field_ids
        if by_id.get(f, {}).get("exposure") == "excluded"
    }
    if excluded:
        raise _invalid_request(
            "pinterest-ads catalog selection refused before the API call --"
            " excluded column(s): "
            + "; ".join(f"{f}: {reason}" for f, reason in sorted(excluded.items())),
            payload={"excluded_fields": excluded},
        )


def _report_request_hash(ad_account_id: str, spec: dict) -> str:
    """Deterministic hash of the FULL request shape (26.1 dedup key).

    ``columns`` is SORTED in the hashed projection (review 26.3 F-7): two
    selections differing only by column order are the same provider report,
    so they must dedup/resume on one reference instead of double-submitting.
    """
    hashed_spec = dict(spec)
    if isinstance(hashed_spec.get("columns"), list):
        hashed_spec["columns"] = sorted(hashed_spec["columns"])
    canonical = json.dumps(
        {"ad_account_id": ad_account_id, "spec": hashed_spec}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Provider report_status -> canonical async_reports status.
_ASYNC_STATUS_MAP = {
    "FINISHED": "completed",
    "IN_PROGRESS": "processing",
    "EXPIRED": "expired",
    "CANCELLED": "expired",
    "DOES_NOT_EXIST": "expired",  # F-3: unknown ref == expired (one resubmit)
    "FAILED": "failed",
}

_DOWNLOAD_EXPIRED_MARKER = re.compile(r"request has expired", re.IGNORECASE)


def _build_async_flow(connection_id: str, ad_account_id: str, spec: dict):
    """AsyncReportFlow closures for one report spec.

    The download URL returned by the poll is valid 5 MINUTES: poll caches it
    and download (invoked by core IMMEDIATELY after the COMPLETED poll) uses
    the cached fresh URL -- the URL is never persisted for later. A dead link
    raises DownloadLinkExpired -> core's single-resubmission policy.
    """
    from core.async_reports import AsyncReportFlow, DownloadLinkExpired  # noqa: PLC0415

    reports_url = f"{_api_base_url()}/ad_accounts/{ad_account_id}/reports"
    state: dict = {"url": None}

    def submit() -> str:
        token = _get_token(connection_id)
        resp = httpx.post(reports_url, headers=_headers(token), json=spec,
                          timeout=60.0)
        if resp.status_code not in (200, 201):
            _raise_provider_error(resp.status_code, resp)
        payload = resp.json()
        report_ref = payload.get("token")
        if not report_ref:
            _raise_provider_error(resp.status_code, resp, body=payload)
        return str(report_ref)

    def poll(report_ref: str) -> str:
        token = _get_token(connection_id)
        resp = httpx.get(reports_url, headers=_headers(token),
                         params={"token": report_ref}, timeout=60.0)
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)
        payload = resp.json()
        raw_status = str(payload.get("report_status", ""))
        status = _ASYNC_STATUS_MAP.get(raw_status)
        if status is None:
            raise _invalid_request(
                f"pinterest-ads: unknown report_status {raw_status!r}"
                " (BulkReportingJobStatus drift)", payload=payload,
            )
        state["url"] = payload.get("url")
        return status

    def download(report_ref: str):
        url = state.get("url")
        if not url:
            # Contract: core calls download right after a COMPLETED poll, so
            # a missing URL means the provider withheld it -> expired path.
            raise DownloadLinkExpired(
                f"pinterest-ads: COMPLETED report {report_ref!r} carried no"
                " download url"
            )
        resp = httpx.get(url, timeout=120.0)
        if resp.status_code == 403 and _DOWNLOAD_EXPIRED_MARKER.search(
            resp.text or ""
        ):
            raise DownloadLinkExpired(
                "pinterest-ads: download link expired (5-minute validity)"
            )
        if resp.status_code != 200:
            _raise_provider_error(resp.status_code, resp)
        return resp.json()

    return AsyncReportFlow(
        submit=submit,
        poll=poll,
        download=download,
        request_hash=_report_request_hash(ad_account_id, spec),
    )


def _report_rows(payload) -> list[dict]:
    """Normalize the downloaded JSON report payload into a flat row list.

    Observed shapes (report_format=JSON): a bare list of row objects, or a
    mapping whose values are lists of row objects (per-entity grouping).
    Anything else is refused loudly (drift signal for the live probe).
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        rows: list[dict] = []
        for value in payload.values():
            if isinstance(value, list):
                rows.extend(r for r in value if isinstance(r, dict))
        return rows
    raise _invalid_request(
        "pinterest-ads: unexpected async report payload shape"
        f" ({type(payload).__name__}) -- live-probe drift signal"
    )


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    ad_account_id: str | None = None,
    level: str = "CAMPAIGN",
    report_store=None,
    clock=None,
    sleeper=None,
    deadline_seconds: int | None = None,
) -> dict:
    """Catalog-driven daily pull: the async report spec is BUILT FROM the
    selection (any of the 626 async catalog columns).

    Steps:
      1. validate the window (async limits: 914 days back / 186-day range)
         and the level (whitelist + level -> data_level map) -- typed
         refusals BEFORE any API call;
      2. resolve the selection (None -> catalog tier-core default, PRUNED to
         granularity-DAY / level-compatible / numeric-metric columns; a user
         selection is NEVER pruned);
      3. refuse excluded-exposure columns and enforce the field_compatibility
         rules (core.field_compat.enforce_selection, context={'level': ...});
      4. build the report spec (columns verified against the catalog enum +
         charset; attribution windows PINNED: 30 click / 30 engagement /
         1 view, TIME_OF_AD_ACTION) and run it through
         core.async_reports.run_async_report EXCLUSIVELY;
      5. download happens INSIDE the flow immediately post-COMPLETED (URL
         valid 5 minutes); rows are flattened, transformed (catalog-driven
         micros rule) and landed LONG.

    A DEFERRED outcome (slow provider / lost claim) raises a RETRYABLE
    ``core.pull_errors.ProviderTransientError`` carrying the request_hash
    (review 26.3 F-2, amazon-ads pattern): the queue retry policy re-runs the
    pull, the persisted in-flight reference resumes by re-poll on the same
    request hash -- never a second submit, never a 0-row "done".

    # AD-3: token fetched inside each flow callable, immediately before use.
    # AD-7: pull_id passed in.
    """
    start, end = _parse_window(date_from, date_to)
    _check_window_limits(start, end, "async_default")

    level_map = _level_map()
    if level not in level_map:
        raise _invalid_request(
            f"pinterest-ads: level {level!r} is not in the v1 whitelist"
            f" {sorted(level_map)} (PRODUCT_ITEM / *_TARGETING need dedicated"
            " request shapes; KEYWORD carries no observable identity column"
            " -- catalog_sources.json _levels_note)"
        )
    data_level = level_map[level]

    granularities = _request_limits().get("granularities_supported_v1", ["DAY"])
    granularity = "DAY"
    if granularity not in granularities:
        raise ValueError(
            "pinterest-ads: catalog_sources granularities_supported_v1 must"
            " include DAY"
        )

    from core.catalog_contract import (  # noqa: PLC0415
        catalog_default_selection,
    )
    from core.catalog_contract import (
        validate_selection as _core_validate_selection,
    )

    catalog = _load_api_catalog()
    if selection is None:
        resolved, issues = _core_validate_selection(
            catalog, catalog_default_selection(catalog)
        )
        if issues:  # pragma: no cover -- the default comes from the catalog
            raise _invalid_request(
                "pinterest-ads: tier-core default selection failed catalog"
                " validation: " + "; ".join(i.message for i in issues)
            )
        selection = _prune_default_selection(resolved, level)
    else:
        resolved, issues = _core_validate_selection(catalog, selection)
        if issues:
            raise _invalid_request(
                "pinterest-ads catalog selection refused before the API call: "
                + "; ".join(i.message for i in issues)
            )
        # Story 26.6 Part B: core/queue.py resolves a selection=None job to the
        # tier-core default and passes it as an EXPLICIT selection. That resolved
        # default MUST take the SAME prune path as selection=None (deeper-level
        # dims a CAMPAIGN report cannot honor would otherwise be a hard refusal
        # on every scheduled default run). A genuine USER selection is NEVER
        # pruned -- incompatibilities there stay typed refusals below.
        if _is_resolved_core_default(resolved):
            selection = _prune_default_selection(resolved, level)
        else:
            selection = resolved

    field_ids = list(selection.get("dimensions", [])) + list(
        selection.get("metrics", [])
    )

    # Typed pre-call refusals: excluded exposure, then the declarative
    # field_compatibility rules (26.1 core engine, strict context).
    _refuse_excluded(field_ids)

    from core import field_compat  # noqa: PLC0415

    rules = _load_catalog_sources().get(field_compat.FIELD_COMPATIBILITY_KEY)
    if rules is None:
        raise ValueError(
            "pinterest-ads: field_compatibility block missing from"
            " catalog_sources.json (module contract)"
        )
    field_compat.enforce_selection(selection, rules, context={"level": level})

    columns = _checked_columns(field_ids)
    if not columns:
        raise _invalid_request(
            "pinterest-ads: empty column selection for catalog_daily"
        )

    account = _resolve_ad_account(connection_id, ad_account_id)

    spec = {
        "start_date": date_from,
        "end_date": date_to,
        "granularity": granularity,
        "level": level,
        "columns": columns,
        "report_format": "JSON",
        **_ATTRIBUTION_SPEC,
    }

    flow = _build_async_flow(connection_id, account, spec)

    from core import async_reports  # noqa: PLC0415

    run_kwargs: dict = {
        "ledger_ref": f"{connection_id}:{account}:catalog_daily",
        "deadline_seconds": deadline_seconds,
    }
    if report_store is not None:
        run_kwargs["store"] = report_store
    if clock is not None:
        run_kwargs["clock"] = clock
    if sleeper is not None:
        run_kwargs["sleeper"] = sleeper

    outcome = async_reports.run_async_report(flow, **run_kwargs)

    if outcome.get("status") == "deferred":
        # Review 26.3 F-2 (amazon-ads pattern, connector.py ~966): a deferred
        # outcome is NOT a completed pull -- raise the retryable typed error
        # so the queue policy retries; the persisted in-flight reference
        # resumes by re-poll (never a second submit) on the next run.
        from core.pull_errors import ProviderTransientError  # noqa: PLC0415

        logger.info(
            "pinterest_ads_pull_deferred: pull_id=%s request_hash=%s resumed=%s",
            pull_id, flow.request_hash, outcome.get("resumed"),
        )
        raise ProviderTransientError(
            provider_status=None,
            provider_payload={"report_ref": outcome.get("report_ref")},
            message=(
                "pinterest-ads async report deferred (provider still"
                " generating or claim held by a concurrent run); the"
                " persisted reference resumes by re-poll on retry"
                " (request_hash=" + flow.request_hash + ")"
            ),
        )

    api_rows = _report_rows(outcome["rows"])

    wide_rows = [_flatten_row(r, ["date"] + field_ids) for r in api_rows]
    canonical_rows = transform(wide_rows)
    metric_names = _canonical_metric_names(list(selection.get("metrics", [])))

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count, non_numeric_landed = _insert_raw_rows(
        canonical_rows, metric_names, data_level, account, pull_id, loaded_at,
        project_id, _get_db_mode(), _get_duckdb_path(),
    )

    logger.info(
        "pinterest_ads_pull_completed: pull_id=%s profile=catalog_daily"
        " row_count=%d level=%s selected_metrics=%d selected_dimensions=%d"
        " resumed=%s non_numeric_landed=%d",
        pull_id, row_count, level,
        len(selection.get("metrics", [])), len(selection.get("dimensions", [])),
        outcome.get("resumed"), non_numeric_landed,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "status": "completed",
        "request_hash": flow.request_hash,
        "resumed": bool(outcome.get("resumed")),
        "non_numeric_landed": non_numeric_landed,
        "attribution": dict(_ATTRIBUTION_SPEC),
    }


# ---------------------------------------------------------------------------
# Account topology (story 26.3, pattern 25.5): flat ad-account level.
# ---------------------------------------------------------------------------


def discover_accounts(connection_id: str) -> list[dict]:
    """List the ad accounts the connection's token can reach.

    GET /ad_accounts (bookmark pagination, include_shared_accounts=true so
    Business Access shares appear). Returns the generic flat list core's
    topology flow consumes:

        [{"id": "549755885175", "label": "Acme (USD)", "level": "ad_account",
          "currency": "USD", "country": "US"}, ...]

    Raises core.quota.RateLimitError on 429; typed ConnectorError otherwise.
    """
    token = _get_token(connection_id)
    url = f"{_api_base_url()}/ad_accounts"
    accounts: list[dict] = []
    bookmark: str | None = None
    pages = 0
    while True:
        params: dict = {"page_size": _ENTITY_BATCH_MAX,
                        "include_shared_accounts": "true"}
        if bookmark:
            params["bookmark"] = bookmark
        payload = _get_json(token, url, params)
        for item in payload.get("items") or []:
            if not item.get("id"):
                continue
            name = item.get("name") or str(item["id"])
            currency = item.get("currency")
            accounts.append(
                {
                    "id": str(item["id"]),
                    "label": f"{name} ({currency})" if currency else name,
                    "level": "ad_account",
                    "currency": currency,
                    "country": item.get("country"),
                }
            )
        bookmark = payload.get("bookmark")
        pages += 1
        if not bookmark or pages >= _MAX_BOOKMARK_PAGES:
            break

    logger.info("pinterest_ads_discover_accounts: accounts=%d", len(accounts))
    return accounts


def check_account_access(connection_id: str, account_id: str) -> dict:
    """Access-check the SELECTED ad account (story 25.5 step 3).

    Minimal 1-day tier-core probe on GET /ad_accounts/{id}/analytics
    (yesterday UTC, account-level aggregate, single SPEND column at
    granularity DAY) -- review 26.3 F-9: this is the ad-account ANALYTICS
    endpoint, not a campaigns list call. A token without Owner / Business
    Access Admin/Analyst/Campaign Manager role fails with the typed
    permission_denied from the error_map (403 body code 3/29 or bare 403).
    """
    token = _get_token(connection_id)
    yesterday = (datetime.now(tz=timezone.utc).date() - timedelta(days=1)).isoformat()
    url = f"{_api_base_url()}/ad_accounts/{account_id}/analytics"
    _get_json(
        token,
        url,
        {
            "start_date": yesterday,
            "end_date": yesterday,
            "columns": "SPEND_IN_MICRO_DOLLAR",
            "granularity": "DAY",
            **_ATTRIBUTION_SPEC,
        },
    )
    return {"account_id": str(account_id), "ok": True}


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
    WHERE connector = 'pinterest-ads'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    # AD-12: MCP server reads fact_daily_kpi mart only -- never raw_* tables.
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
        data_by_metric.setdefault(metric, []).append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    provenance = (
        {
            "source_system": "pinterest-ads",
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
def get_pinterest_ads_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Pinterest Ads Performance Report -- reads from fact_daily_kpi mart.

    Report profiles: campaign_daily, ad_group_daily, ad_daily, catalog_daily
    Metrics: cost, impressions, clicks, engagements, conversions, checkouts,
             checkout_value (additive at day grain; micro-currency landed /1e6)
    Breakdown dimensions: campaign_id, ad_group_id, ad_id

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    # AD-4: conversion metrics restate while the pinned attribution windows
    # run (30 click / 30 engagement / 1 view -- refetch ladder 7/30/60).

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of the 4 declared profiles
        date_from: Start date ISO-8601 (e.g. '2026-06-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    # Review 26.3 F-9: UTC dates (provider reporting days are UTC) -- never
    # the server's local calendar.
    today = datetime.now(timezone.utc).date()
    if not date_to:
        date_to = (today - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (today - timedelta(days=90)).isoformat()

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

    _register_raw("raw_pinterest_ads_daily", provider="pinterest-ads")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
