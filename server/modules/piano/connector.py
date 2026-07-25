"""Piano Analytics connector -- Story 28.1 (catalog-first, Data Query API v3).

Exposes a ``mcp_app: FastMCP`` instance that the core loader mounts under the
``piano`` namespace (AD-2). Piano Analytics (ex-AT Internet) is a digital
analytics source (GA4-equivalent) whose extraction surface is the SYNCHRONOUS
Data Query API v3: ``POST {base}/v3/data/getData`` with a JSON body
``{columns, space, period, sort, max-results, page-num}``.

THE central design fact (dossier section 2): Piano has NO fixed reporting
schema and NO public metadata endpoint. Properties (dimensions) and metrics are
per-site Data Model keys. The committed api_catalog.json is the STANDARD
BASELINE; ``field_discovery.mode = dynamic``. A columns key UNKNOWN to the
baseline is NOT refused in advance (it may be a valid custom key of the site):
it is SENT, and if the API returns a 400 InvalidColumns_* the connector raises
a typed InvalidRequestError carrying the pull_invalid_request drift signal --
NEVER a crash, NEVER a silent drop.

# AD-12: MCP server reads the fact_daily_kpi mart only -- no raw_* tables.
# AD-3:  API key (access + secret) read from env PIANO_ACCESS_KEY /
#        PIANO_SECRET_KEY immediately before use, sent as the x-api-key header
#        (value = <access>_<secret>). NEVER stored/logged (single API key, no
#        OAuth, inherits the creating user's site permissions).
# AD-7:  pull_id minted by the core scheduler and passed into pull().
# AD-2:  request column lists are DERIVED from the catalog/selection -- no
#        hardcoded column lists. The API version is pinned in manifest.json
#        (provider_api_version) and read from there ONLY.
# AD-4:  visit/visitor-scoped metrics (m_visits, m_unique_visitors, ...) are
#        NON-ADDITIVE across property breakdowns; marked in the catalog
#        (catalog_sources non_additive_metrics) and landed RAW at day grain.
# AI-03: ASCII-only stdout/log strings.
#
# Concurrency: Piano throttles CONCURRENT calls (QuotasExceeded_TooManyRequests
# HTTP 509). Pulls are SERIALIZED per connection here (paging loop is
# sequential; no parallel chunk fan-out). 509 and any 429 -> the
# core.quota.RateLimitError breaker path with backoff.
"""

from __future__ import annotations

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
mcp_app = FastMCP("piano")

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
# pinned in ONE place: manifest.json provider_api_version.
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
    base = manifest.get("provider_api_base", "https://api.atinternet.io")
    version = manifest.get("provider_api_version", "v3")
    return f"{base}/{version}/data"


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
    return _load_catalog_sources().get("_request_limits", {})


def _max_columns() -> int:
    """Piano getData column ceiling per call (catalog_sources _request_limits).

    A single getData call carries at most this many columns (dimensions +
    metrics together). A wider selection is CHUNK-MERGED across calls (metrics
    split, dimensions shared) unless it cannot be chunked (see
    _chunk_metric_columns / the un-chunkable refusal in _run_getdata_pull)."""
    return int(_request_limits().get("max_columns", 50))


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


def _non_additive_metric_ids() -> frozenset[str]:
    """The non-additive (visit/visitor-scoped) metric set FROM the catalog_sources
    non_additive_metrics block (AD-4). The connector reads it from the catalog,
    never from an id-suffix heuristic (google-ads 26.2 review lesson)."""
    global _NON_ADDITIVE
    try:
        return _NON_ADDITIVE  # type: ignore[name-defined]
    except NameError:
        pass
    block = _load_catalog_sources().get("non_additive_metrics", {})
    globals()["_NON_ADDITIVE"] = frozenset(block.get("keys", []))
    return globals()["_NON_ADDITIVE"]


# ---------------------------------------------------------------------------
# Provider error handling (dossier section 7).
#
# Piano v3 error bodies carry an API-Code string (Category_Subcode) plus an
# HTTP status. The core error_map keys on '<status>:<Category>' (the prefix),
# so we extract the API-Code, reduce it to its Category prefix, and feed a
# synthesized {"code": <prefix>} payload to classify_http_error. 509 / 429 stay
# on the core.quota.RateLimitError breaker path.
# ---------------------------------------------------------------------------


def _extract_api_code(body) -> str | None:
    """Best-effort Piano API-Code (Category_Subcode) from a v3 error body.

    Recognises the documented shapes without hardcoding any single code:
      {"ErrorCode": "BadAuthentication_NoHeader", ...}
      {"error": {"code": "...", ...}} / {"error": {"Code": "..."}}
      {"code": "..."} / {"Code": "..."}
    Returns the raw code string or None. Never raises.
    """
    if not isinstance(body, dict):
        return None
    for key in ("ErrorCode", "errorCode", "Code", "code"):
        val = body.get(key)
        if isinstance(val, str) and val:
            return val
    err = body.get("error") or body.get("Error")
    if isinstance(err, dict):
        for key in ("code", "Code", "ErrorCode"):
            val = err.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _code_prefix(api_code: str | None) -> str | None:
    """Reduce an API-Code (Category_Subcode) to its Category prefix.

    'InvalidColumns_UnknownColumn' -> 'InvalidColumns'; 'UnauthorizedSite'
    (no subcode) -> 'UnauthorizedSite'. This is what the error_map keys match.
    """
    if not api_code:
        return None
    return api_code.split("_", 1)[0]


# A 400 whose API-Code is in the InvalidColumns family is THE dynamic-catalog
# drift signal (an unknown Data Model property/metric key).
_DRIFT_CODE_PREFIX = "InvalidColumns"


def _raise_provider_error(
    status_code: int, resp: httpx.Response | None = None, body=None
) -> None:
    """Raise the canonical typed error for a non-2xx Piano response.

    509 QuotasExceeded_TooManyRequests (and any 429) -> the
    core.quota.RateLimitError breaker path. Everything else ->
    classify_http_error with the manifest error_map keyed '<status>:<Category>'.
    A 400 in the InvalidColumns family is logged as the pull_invalid_request
    drift signal (an unknown Data Model column reached the API and was refused).
    """
    if body is None and resp is not None:
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    if status_code in (429, 509):
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after = None
        if resp is not None:
            raw = resp.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = int(float(raw)) or None
                except (TypeError, ValueError):
                    retry_after = None
        raise RateLimitError("piano", retry_after)

    from core import pull_errors  # noqa: PLC0415

    api_code = _extract_api_code(body)
    prefix = _code_prefix(api_code)

    if status_code == 400 and prefix == _DRIFT_CODE_PREFIX:
        # Dynamic-catalog drift: an unknown Data Model column was SENT (it may be
        # a custom key that this site does not define) and the API refused it.
        # Typed invalid_request + an explicit drift log key (never a crash, never
        # a silent drop). The full API-Code is preserved as evidence.
        logger.warning(
            "pull_invalid_request_drift: piano getData refused column(s) --"
            " api_code=%s (unknown Data Model key; field_discovery=dynamic)",
            api_code,
        )

    # Feed classify_http_error a payload whose top-level 'code' is the Category
    # prefix so the '<status>:<Category>' error_map keys match generically.
    refined_payload = body
    if prefix is not None:
        refined_payload = {"code": prefix, "_provider_body": body}
    raise pull_errors.classify_http_error(
        status_code, refined_payload, _load_error_map()
    )


# ---------------------------------------------------------------------------
# Auth -- single API key (access + secret) from env, x-api-key header.
# ---------------------------------------------------------------------------


def _api_key() -> str:
    """Build the x-api-key value <access>_<secret> from platform env (AD-3).

    Never logged. Absent keys are a hard, actionable error (no manifest
    fallback, no silent empty header).
    """
    access = os.environ.get("PIANO_ACCESS_KEY", "")
    secret = os.environ.get("PIANO_SECRET_KEY", "")
    if not access or not secret:
        raise ValueError(
            "piano: PIANO_ACCESS_KEY and PIANO_SECRET_KEY env vars are required"
            " (single API key; access+secret sent as x-api-key: <access>_<secret>)"
        )
    return f"{access}_{secret}"


def _headers() -> dict:
    return {"x-api-key": _api_key(), "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Site (space) resolution -- the reporting entity is the Piano site id.
# ---------------------------------------------------------------------------


def _resolve_site_id(connection_id: str, site_id: str | None) -> str:
    """Resolve the reporting site for a pull.

    Explicit arg wins (tests / direct calls). Otherwise the ONLY source is the
    core topology scope (story 25.5 flow: discovery -> selection -> access
    check). There is NO env-var site fallback in the pull path (PIANO_*_SITE_ID
    is forbidden by doctrine): an unselected site is a hard, actionable error.
    """
    if site_id:
        return str(site_id)

    selected: str | None = None
    try:
        from core import account_topology, token_service  # noqa: PLC0415

        resolved = token_service.resolve_connection_by_nango_id(connection_id)
        if resolved is not None:
            selected = account_topology.resolve_selected_account(resolved.id)
    except (LookupError, ValueError) as exc:
        # Only EXPECTED resolution errors fall through to the actionable message;
        # anything else (import/DB/programming errors) must surface.
        logger.warning("piano_site_resolution_failed: %s", type(exc).__name__)

    if not selected:
        raise ValueError(
            "no selected reporting site for this piano connection -- run the"
            " account topology flow (discovery -> selection -> access check);"
            " env-var site fallbacks do not exist for this module"
        )
    return str(selected)


def _space(site_id: str) -> dict:
    """The space selector {"s": [<site_id>]} -- integer site id when numeric."""
    try:
        return {"s": [int(site_id)]}
    except (TypeError, ValueError):
        # A non-numeric id is still sent (the API validates it -> typed error).
        return {"s": [site_id]}


# ---------------------------------------------------------------------------
# Request validation: dates, windows, columns (pre-call refusals).
# ---------------------------------------------------------------------------


def _column_charset_re() -> re.Pattern:
    global _CHARSET_RE
    try:
        return _CHARSET_RE  # type: ignore[name-defined]
    except NameError:
        pass
    pattern = _request_limits().get("column_charset", r"^[A-Za-z][A-Za-z0-9_.]*$")
    globals()["_CHARSET_RE"] = re.compile(pattern)
    return globals()["_CHARSET_RE"]


def _invalid_request(message: str, payload=None):
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    return InvalidRequestError(
        provider_status=None, provider_payload=payload, message=message
    )


def _parse_window(date_from: str, date_to: str) -> tuple[date, date]:
    """Strict ISO dates via date.fromisoformat."""
    try:
        start = date.fromisoformat(str(date_from))
        end = date.fromisoformat(str(date_to))
    except (TypeError, ValueError) as exc:
        raise _invalid_request(
            f"piano: dates must be ISO-8601 YYYY-MM-DD"
            f" (date_from={date_from!r} date_to={date_to!r}): {exc}"
        ) from exc
    if end < start:
        raise _invalid_request(
            f"piano: date_to {date_to!r} precedes date_from {date_from!r}"
        )
    return start, end


def _check_window_limits(start: date, end: date, today: date | None = None) -> None:
    """Refuse a window beyond the (conservative) retention ceiling BEFORE the call."""
    limits = (_request_limits().get("date_window_limits") or {}).get("getdata_default")
    if not limits:
        return
    if today is None:
        today = datetime.now(tz=timezone.utc).date()
    max_back = int(limits["max_days_back"])
    max_range = int(limits["max_range_days"])
    if (today - start).days > max_back:
        raise _invalid_request(
            f"piano: date_from {start.isoformat()} is more than {max_back} days"
            " in the past (retention ceiling) -- refused before the API call"
        )
    if (end - start).days + 1 > max_range:
        raise _invalid_request(
            f"piano: window {start.isoformat()}..{end.isoformat()} exceeds the"
            f" {max_range}-day ceiling -- refused before the API call"
        )


def _checked_columns(field_ids: list[str], *, allow_unknown: bool) -> list[str]:
    """Map catalog field ids -> Piano column tokens.

    Every token is charset-validated (^[A-Za-z][A-Za-z0-9_.]*$ -- Piano keys are
    lower_snake for properties and m_-prefixed for metrics, and period-relative
    metrics carry a dotted prefix like p1.m_visits). Order is preserved,
    duplicates removed.

    ``allow_unknown`` (catalog_daily): a field id ABSENT from the baseline
    catalog is NOT refused in advance -- it may be a valid custom Data Model key
    of the site. It is charset-validated and SENT; if the API rejects it the
    connector surfaces the InvalidColumns drift (dynamic-catalog contract). For
    exact_bundle profiles ``allow_unknown`` is False: an unknown id is a hard
    catalog-drift refusal (the bundle is the committed contract).
    """
    by_id = _catalog_fields_by_id()
    charset = _column_charset_re()
    columns: list[str] = []
    for field_id in field_ids:
        entry = by_id.get(field_id)
        if entry is None and not allow_unknown:
            raise _invalid_request(
                f"piano: field {field_id!r} is not in api_catalog.json"
                " (catalog drift -- the baseline catalog is the execution"
                " contract for exact_bundle profiles)"
            )
        token = entry.get("source_field", field_id) if entry else field_id
        if not charset.match(token):
            raise _invalid_request(
                f"piano: column token {token!r} (field {field_id!r}) violates"
                f" the {charset.pattern} charset -- refused"
            )
        if token not in columns:
            columns.append(token)
    return columns


# ---------------------------------------------------------------------------
# getData transport (SYNC POST). Paging is sequential (no fan-out).
# ---------------------------------------------------------------------------

_GETDATA_TIMEOUT = 120.0


def _post_getdata(body: dict) -> dict:
    """POST /v3/data/getData and return the parsed JSON (raises typed on non-200)."""
    url = f"{_api_base_url()}/getData"
    resp = httpx.post(url, headers=_headers(), json=body, timeout=_GETDATA_TIMEOUT)
    if resp.status_code != 200:
        _raise_provider_error(resp.status_code, resp)
    return resp.json()


def _rows_from_datafeed(payload) -> list[dict]:
    """Normalize DataFeed[].Rows into a flat list of row dicts.

    Response: {"DataFeed": [{"Context":..., "Columns":..., "Rows":[{...}], ...}]}.
    Anything else is refused loudly (drift signal for the live probe).
    """
    if not isinstance(payload, dict):
        raise _invalid_request(
            f"piano: unexpected getData payload shape ({type(payload).__name__})"
            " -- live-probe drift signal"
        )
    feed = payload.get("DataFeed")
    if feed is None:
        # Some format variants return a bare rows list; accept it defensively.
        if isinstance(payload.get("Rows"), list):
            return [r for r in payload["Rows"] if isinstance(r, dict)]
        raise _invalid_request(
            "piano: getData payload carries no DataFeed/Rows -- drift signal",
            payload=payload if isinstance(payload, dict) else None,
        )
    rows: list[dict] = []
    for dataset in feed if isinstance(feed, list) else []:
        if isinstance(dataset, dict):
            for r in dataset.get("Rows") or []:
                if isinstance(r, dict):
                    rows.append(r)
    return rows


def _get_row_count(space: dict, period: dict, columns: list[str], sort: list[str]) -> int | None:
    """Best-effort getRowCount to plan paging. None when unavailable (fall back
    to the short-page loop)."""
    url = f"{_api_base_url()}/getRowCount"
    body = {"columns": columns, "space": space, "period": period, "sort": sort}
    resp = httpx.post(url, headers=_headers(), json=body, timeout=_GETDATA_TIMEOUT)
    if resp.status_code != 200:
        _raise_provider_error(resp.status_code, resp)
    payload = resp.json()
    # Documented shape: {"RowCounts":[{"RowCount": N}]} (harvested defensively).
    try:
        feed = payload.get("RowCounts") or payload.get("DataFeed") or []
        if feed and isinstance(feed, list) and isinstance(feed[0], dict):
            for key in ("RowCount", "rowCount", "count"):
                if key in feed[0]:
                    return int(feed[0][key])
    except (TypeError, ValueError, KeyError):
        pass
    return None


def _paged_getdata(
    columns: list[str], space: dict, period: dict, sort: list[str]
) -> tuple[list[dict], bool, int]:
    """Sequential paged getData (no fan-out): loop page-num until a short page or
    the 200k ceiling. Returns (rows, truncated, requests_made).

    ``truncated`` is True when the 200k ceiling was hit with more rows pending --
    surfaced in the pull result (never a silent partial landing)."""
    limits = _request_limits()
    max_results = int(limits.get("max_results_per_call", 10000))
    max_total = int(limits.get("max_rows_total", 200000))

    all_rows: list[dict] = []
    page = 1
    requests_made = 0
    truncated = False
    while True:
        body = {
            "columns": columns,
            "space": space,
            "period": period,
            "sort": sort,
            "max-results": max_results,
            "page-num": page,
        }
        payload = _post_getdata(body)
        requests_made += 1
        rows = _rows_from_datafeed(payload)
        all_rows.extend(rows)
        if len(all_rows) >= max_total:
            if len(rows) == max_results:
                truncated = True
                logger.warning(
                    "piano_paging_truncated: hit 200k ceiling (%d rows) with a"
                    " full last page -- more rows may exist (never silent)",
                    len(all_rows),
                )
            all_rows = all_rows[:max_total]
            break
        if len(rows) < max_results:
            break
        page += 1
    return all_rows, truncated, requests_made


# ---------------------------------------------------------------------------
# Chunk-merge (dossier _request_limits.paging): a selection wider than
# max_columns (50) is split into several getData calls SHARING the SAME
# dimension columns + a stable sort, then the per-chunk wide rows are MERGED on
# the grain key (period-window + dimension-token tuple). Union of metric columns
# per grain. Serialized (no fan-out -- the 509 concurrency guard).
# ---------------------------------------------------------------------------


def _grain_key(api_row: dict, dim_columns: list[str]) -> tuple:
    """Merge key for a wide getData row: the tuple of its dimension-token values.

    Every chunk carries the SAME dim_columns in the SAME order, so a grain's
    row from chunk A and chunk B share this key and their metric cells collapse
    into one merged row. The period window is identical across chunks (one pull)
    so it is implicit in the key. Missing dimension values become '' (stable)."""
    return tuple(str(api_row.get(col, "")) for col in dim_columns)


def _chunk_metric_columns(
    dim_columns: list[str], metric_columns: list[str], max_columns: int
) -> list[list[str]]:
    """Split metric_columns so each chunk (dim_columns + metric-chunk) stays at
    or under max_columns. Dimensions are carried in EVERY chunk (they form the
    merge grain). Returns a list of metric-column chunks (>= 1)."""
    room = max_columns - len(dim_columns)
    # room >= 1 is guaranteed by the caller's un-chunkable pre-check.
    return [metric_columns[i:i + room] for i in range(0, len(metric_columns), room)]


def _chunked_getdata(
    dim_columns: list[str],
    metric_columns: list[str],
    space: dict,
    period: dict,
) -> tuple[list[dict], bool, int]:
    """Run getData for a (dims, metrics) selection, chunk-merging when the total
    column count exceeds max_columns. Returns (api_rows, truncated, requests).

    Single-call fast path when dims+metrics <= max_columns. Otherwise: metrics
    are chunked (dims shared), each chunk paged sequentially with a STABLE sort
    (the first dimension token ASC -- identical across chunks so grains align),
    and the per-chunk wide rows are merged on the dimension-token grain key. The
    union of metric cells per grain is returned (no cell dropped)."""
    max_columns = _max_columns()
    total = len(dim_columns) + len(metric_columns)
    if total <= max_columns:
        columns = dim_columns + metric_columns
        sort = _stable_sort(dim_columns, metric_columns)
        return _paged_getdata(columns, space, period, sort)

    # Chunk-merge path. Stable sort = the first dimension ASC (shared by every
    # chunk -> deterministic, and a sort key must also be a column).
    metric_chunks = _chunk_metric_columns(dim_columns, metric_columns, max_columns)
    sort = _stable_sort(dim_columns, [])
    merged: dict[tuple, dict] = {}
    truncated = False
    requests_made = 0
    for metric_chunk in metric_chunks:  # serialized (no fan-out -- 509 guard)
        columns = dim_columns + metric_chunk
        rows, chunk_truncated, chunk_requests = _paged_getdata(
            columns, space, period, sort
        )
        truncated = truncated or chunk_truncated
        requests_made += chunk_requests
        for api_row in rows:
            key = _grain_key(api_row, dim_columns)
            entry = merged.get(key)
            if entry is None:
                # Seed with the dimension values of this grain.
                entry = {col: api_row.get(col) for col in dim_columns}
                merged[key] = entry
            for mcol in metric_chunk:
                if mcol in api_row:
                    entry[mcol] = api_row[mcol]
    logger.info(
        "piano_chunk_merge: dims=%d metrics=%d chunks=%d grains=%d requests=%d",
        len(dim_columns), len(metric_columns), len(metric_chunks),
        len(merged), requests_made,
    )
    return list(merged.values()), truncated, requests_made


# ---------------------------------------------------------------------------
# Row flattening + transform (catalog/manifest-driven, AD-2).
# ---------------------------------------------------------------------------


def _flatten_row(api_row: dict, field_ids: list[str]) -> dict:
    """One Piano row -> wide dict keyed by catalog field_id.

    Piano keys each row by the COLUMN token (property key or m_-metric key). The
    catalog maps field_id <-> source_field (identical for Piano baseline keys).
    A '(not set)'-type sentinel maps to an explicit 'unknown' bucket for
    dimensions (never dropped)."""
    by_id = _catalog_fields_by_id()
    row: dict = {}
    for field_id in field_ids:
        entry = by_id.get(field_id) or {}
        token = entry.get("source_field", field_id)
        value = api_row.get(token)
        if entry.get("kind") == "dimension" and value in ("(not set)", None, ""):
            value = "unknown" if value == "(not set)" else value
        row[field_id] = value
    return row


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map field_id-keyed rows to canonical names using manifest mappings.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_
    mapping. Fields absent from both mappings pass through unchanged. Piano
    metrics are plain counts/decimals -- there is NO micro-currency conversion
    (unlike the ads platforms); units are driven by the catalogued physical_type.
    """
    manifest = _load_manifest()
    rename_map: dict[str, str] = {}
    for src, val in manifest.get("canonical_metric_mapping", {}).items():
        rename_map[src] = val if isinstance(val, str) else val.get("canonical", src)
    rename_map.update(manifest.get("canonical_dimension_mapping", {}))

    result: list[dict] = []
    for row in raw_rows:
        canonical = {rename_map.get(k, k): v for k, v in row.items()}
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
# Raw landing -- LONG format: one row per grain x metric.
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_piano_daily (
    date          VARCHAR,
    site_id       VARCHAR,
    segments_json VARCHAR,
    metric        VARCHAR,
    value_num     DOUBLE,
    non_additive  BOOLEAN,
    pull_id       VARCHAR,
    loaded_at     VARCHAR,
    project_id    VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_piano_daily
    (date, site_id, segments_json, metric, value_num, non_additive,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Structural (non-segment) keys: the day grain + the site id. Everything else a
# selection carries (properties) lands in segments_json so ANY catalog/custom
# dimension participates in the supersede grain without schema churn.
_NON_SEGMENT_KEYS = {"date", "site_id", "pull_id", "connector"}


def _insert_raw_rows(
    rows: list[dict],
    metric_names: list[str],
    non_additive_names: set[str],
    site_id: str,
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> tuple[int, int]:
    """Long-ify canonical wide rows and insert into raw_piano_daily.

    One long row per (grain, metric) with value_num. NULL honnete (AD-9): an
    absent metric lands NO row -- absence stays distinguishable from a recorded
    zero. Non-structural dimension values land in segments_json (canonical
    sorted-key JSON). A NON-NUMERIC metric value is NEVER silently dropped: it
    lands in segments_json under the metric name, a warning is logged, and the
    count is returned (``non_numeric_landed``).

    Non-additive metrics (AD-4) carry the non_additive flag on their row so the
    mart / semantic layer never blindly sums them across breakdowns.

    Returns ``(row_count, non_numeric_landed)``.
    """
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev"
            " (BigQuery path not yet implemented)"
        )
    from core import warehouse_write  # noqa: PLC0415

    metric_set = list(dict.fromkeys(metric_names))
    values: list[tuple] = []
    non_numeric_landed = 0
    for row in rows:
        segment_items = {
            k: row[k]
            for k in row
            if k not in _NON_SEGMENT_KEYS
            and k not in metric_set
            and row[k] is not None
        }
        numeric_metrics: list[tuple[str, float]] = []
        for metric in metric_set:
            value = row.get(metric)
            if value is None:
                continue
            try:
                numeric_metrics.append((metric, float(value)))
            except (ValueError, TypeError):
                segment_items[metric] = value
                non_numeric_landed += 1
                logger.warning(
                    "piano_non_numeric_metric_landed: metric=%s pull_id=%s value"
                    " landed in segments_json (not value_num)",
                    metric, pull_id,
                )
        segments_json = (
            json.dumps(segment_items, sort_keys=True) if segment_items else None
        )
        for metric, value_num in numeric_metrics:
            values.append(
                (
                    row.get("date", ""),
                    row.get("site_id") or site_id,
                    segments_json,
                    metric,
                    value_num,
                    metric in non_additive_names,
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
# Shared getData pull core.
# ---------------------------------------------------------------------------


def _stable_sort(dim_columns: list[str], metric_columns: list[str]) -> list[str]:
    """Deterministic sort key over resolved COLUMN TOKENS (must be one of the
    call's columns): primary metric DESC when present, else the first dimension
    ASC. In the chunk-merge path the caller passes metrics=[] so the sort is the
    first-dimension ASC -- IDENTICAL across every chunk (grains stay aligned)."""
    if metric_columns:
        return [f"-{metric_columns[0]}"]
    if dim_columns:
        return [dim_columns[0]]
    return []


def _run_getdata_pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    *,
    profile: str,
    dim_ids: list[str],
    metric_ids: list[str],
    site_id: str | None,
    allow_unknown_columns: bool,
) -> dict:
    """Build the getData body from a resolved (dim, metric) selection, run the
    sequential paged pull, and land LONG rows. Shared by every profile."""
    start, end = _parse_window(date_from, date_to)
    _check_window_limits(start, end)

    site = _resolve_site_id(connection_id, site_id)
    field_ids = dim_ids + metric_ids
    # Resolve dimension and metric column tokens SEPARATELY so a >max_columns
    # selection can be chunk-merged (metrics split, dimensions shared).
    dim_columns = _checked_columns(dim_ids, allow_unknown=allow_unknown_columns)
    metric_columns = [
        c for c in _checked_columns(metric_ids, allow_unknown=allow_unknown_columns)
        if c not in dim_columns
    ]
    columns = dim_columns + metric_columns
    if not columns:
        raise _invalid_request(f"piano: empty column selection for {profile}")

    # F-1(c): a selection wider than max_columns that CANNOT be chunked (the
    # dimensions alone leave no room for even one metric, or there are no metrics
    # to split) is a typed pre-call refusal DISTINCT from the unknown-column
    # drift (this is not a 400 drift -- it is a shape refusal the caller fixes by
    # reducing or splitting the selection).
    max_columns = _max_columns()
    if len(columns) > max_columns and (
        not metric_columns or len(dim_columns) + 1 > max_columns
    ):
        raise _invalid_request(
            f"piano: selection exceeds max_columns ({len(columns)} > {max_columns})"
            f" and cannot be chunked ({len(dim_columns)} dimensions leave no room"
            " to split metrics across getData calls) -- reduce or split the"
            " selection (fewer dimensions per pull)",
            payload={
                "columns": len(columns),
                "max_columns": max_columns,
                "dimensions": len(dim_columns),
                "metrics": len(metric_columns),
            },
        )

    space = _space(site)
    period = {"p1": [{"type": "D", "start": date_from, "end": date_to}]}

    api_rows, truncated, requests_made = _chunked_getdata(
        dim_columns, metric_columns, space, period
    )

    wide_rows = [_flatten_row(r, field_ids) for r in api_rows]
    for r in wide_rows:
        r.setdefault("site_id", site)
    canonical_rows = transform(wide_rows)
    metric_names = _canonical_metric_names(metric_ids)
    non_additive_ids = _non_additive_metric_ids()
    non_additive_names = set(
        _canonical_metric_names([m for m in metric_ids if m in non_additive_ids])
    )

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count, non_numeric_landed = _insert_raw_rows(
        canonical_rows, metric_names, non_additive_names, site, pull_id,
        loaded_at, project_id, _get_db_mode(), _get_duckdb_path(),
    )

    # AD-3: no key in logs -- only pull_id / counts (safe metadata).
    logger.info(
        "piano_pull_completed: pull_id=%s profile=%s row_count=%d requests=%d"
        " truncated=%s non_numeric_landed=%d selected_metrics=%d"
        " selected_dimensions=%d",
        pull_id, profile, row_count, requests_made, truncated,
        non_numeric_landed, len(metric_ids), len(dim_ids),
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "status": "completed",
        "truncated": truncated,
        "requests_made": requests_made,
        "non_numeric_landed": non_numeric_landed,
    }


def _profile_field_ids(profile_id: str) -> tuple[list[str], list[str]]:
    """(dimension_ids, metric_ids) of an exact_bundle profile (AD-2: from the
    manifest capability report, never hardcoded here)."""
    caps = _load_manifest().get("source_capabilities", {})
    report = next(
        (r for r in caps.get("reports", []) if r.get("id") == profile_id), None
    )
    if report is None:
        raise ValueError(f"Unknown piano report profile: {profile_id!r}")
    return list(report.get("dimensions", [])), list(report.get("metrics", []))


def _pull_exact_bundle(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    site_id: str | None = None,
) -> dict:
    dim_ids, metric_ids = _profile_field_ids(profile)
    return _run_getdata_pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile=profile, dim_ids=dim_ids, metric_ids=metric_ids,
        site_id=site_id, allow_unknown_columns=False,
    )


# ---------------------------------------------------------------------------
# AI-58 default + per-profile dispatch callables.
# ---------------------------------------------------------------------------


def pull(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, site_id: str | None = None,
) -> dict:
    """Default pull() = baseline traffic grain (AI-58 profile-less default)."""
    return _pull_exact_bundle(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="baseline_daily", site_id=site_id,
    )


def pull_baseline_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, site_id: str | None = None,
) -> dict:
    """AI-58 dispatch: baseline traffic (date x device x country)."""
    return _pull_exact_bundle(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="baseline_daily", site_id=site_id,
    )


def pull_audience_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, site_id: str | None = None,
) -> dict:
    """AI-58 dispatch: audience by device and country."""
    return _pull_exact_bundle(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="audience_daily", site_id=site_id,
    )


def pull_sources_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, site_id: str | None = None,
) -> dict:
    """AI-58 dispatch: acquisition sources (date x source x medium)."""
    return _pull_exact_bundle(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="sources_daily", site_id=site_id,
    )


def pull_events_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, site_id: str | None = None,
) -> dict:
    """AI-58 dispatch: onsite events (date x event_name)."""
    return _pull_exact_bundle(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="events_daily", site_id=site_id,
    )


# ---------------------------------------------------------------------------
# catalog_driven execution: pull_catalog_daily (the dynamic-catalog surface).
# ---------------------------------------------------------------------------


# Preferred default breakdown dimensions (deterministic priority). 'date' is the
# grain/time key and is ALWAYS kept first; the rest are the canonical low-
# cardinality breakdowns a default traffic pull wants. Anything past the
# max_columns budget is dropped from the DEFAULT ONLY (a user selection is never
# pruned). Custom/extra dimensions can always be requested explicitly.
_DEFAULT_DIMENSION_PRIORITY = (
    "date",
    "device_type",
    "geo_country",
    "src_source",
    "src_medium",
    "page",
    "os",
    "browser",
    "geo_region",
    "day",
)


def _prune_default_selection(selection: dict) -> dict:
    """Prune the MODULE-RESOLVED tier-core default (None fallback) to what a
    single getData request shape can honor:
      1. drop non-numeric metric columns (the LONG landing carries value_num);
      2. BOUND the total column count to max_columns (50) -- the raw tier-core
         default is ~61 columns, which would ALWAYS exceed the ceiling. Metrics
         are kept (bounded, ~10); dimensions are trimmed to a deterministic
         priority set that fits the remaining budget ('date' always kept).

    A USER selection is NEVER pruned -- unknown keys there are SENT and validated
    live (dynamic catalog); a user who wants >50 columns gets the chunk-merge or
    the typed un-chunkable refusal (F-1)."""
    by_id = _catalog_fields_by_id()
    kept_metrics = [
        f for f in selection.get("metrics", [])
        if by_id.get(f, {}).get("physical_type") in ("integer", "decimal")
    ]
    dropped = sorted(set(selection.get("metrics", [])) - set(kept_metrics))
    if dropped:
        logger.info(
            "piano_default_selection_pruned: dropped=%d non-numeric metric(s)"
            " (default selection only): %s",
            len(dropped), ",".join(dropped),
        )

    all_dims = list(selection.get("dimensions", []))
    max_columns = _max_columns()
    dim_budget = max(1, max_columns - len(kept_metrics))
    if len(all_dims) <= dim_budget:
        kept_dims = all_dims
    else:
        # Order by the priority tuple, then any remaining default dims (stable),
        # and keep only up to the budget. 'date' is first in the priority list.
        prio = {d: i for i, d in enumerate(_DEFAULT_DIMENSION_PRIORITY)}
        ordered = sorted(
            all_dims, key=lambda d: (prio.get(d, len(prio)), d)
        )
        kept_dims = ordered[:dim_budget]
        trimmed = sorted(set(all_dims) - set(kept_dims))
        logger.info(
            "piano_default_selection_bounded: dimensions trimmed %d -> %d to fit"
            " max_columns=%d (default selection only; request them explicitly to"
            " land more): dropped=%s",
            len(all_dims), len(kept_dims), max_columns, ",".join(trimmed),
        )

    return {"metrics": kept_metrics, "dimensions": kept_dims}


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
            "piano catalog selection refused before the API call -- excluded"
            " column(s): "
            + "; ".join(f"{f}: {reason}" for f, reason in sorted(excluded.items())),
            payload={"excluded_fields": excluded},
        )


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    site_id: str | None = None,
) -> dict:
    """Catalog-driven daily pull: the getData columns are BUILT FROM the
    selection (any baseline OR custom Data Model key).

    Steps:
      1. validate the window (retention ceiling) -- typed refusal BEFORE any call;
      2. resolve the selection (None -> catalog tier-core default, PRUNED to
         numeric metrics; a USER selection is NEVER pruned);
      3. refuse excluded-exposure baseline columns (AV QoS / MV / consent);
      4. build columns (charset-validated; UNKNOWN baseline keys ARE ALLOWED and
         SENT -- they may be valid custom keys; the API validates them live);
      5. run the sequential paged getData and land LONG.

    Dynamic-catalog contract (field_discovery=dynamic): an unknown key that the
    site does not define returns a 400 InvalidColumns_* -> typed
    InvalidRequestError + pull_invalid_request drift signal (raised from
    _raise_provider_error). NEVER a crash, NEVER a silent drop.

    # AD-3: key read inside each transport call, immediately before use.
    # AD-7: pull_id passed in.
    """
    from core.catalog_contract import (  # noqa: PLC0415
        catalog_default_selection,
    )
    from core.catalog_contract import (
        validate_selection as _core_validate_selection,
    )

    catalog = _load_api_catalog()
    user_supplied = selection is not None
    if selection is None:
        resolved, issues = _core_validate_selection(
            catalog, catalog_default_selection(catalog)
        )
        if issues:  # pragma: no cover -- the default comes from the catalog
            raise _invalid_request(
                "piano: tier-core default selection failed catalog validation: "
                + "; ".join(i.message for i in issues)
            )
        selection = _prune_default_selection(resolved)
    else:
        # A user selection may reference CUSTOM keys absent from the baseline
        # catalog -- those are NOT a refusal here (dynamic catalog): only the
        # KNOWN baseline fields are resolved for excluded-exposure checks; the
        # unknown ones flow through to getData and are validated live.
        resolved, _issues = _core_validate_selection(catalog, selection)
        selection = {
            "metrics": list(selection.get("metrics", [])),
            "dimensions": list(selection.get("dimensions", [])),
        }

    metric_ids = list(selection.get("metrics", []))
    dim_ids = list(selection.get("dimensions", []))
    field_ids = dim_ids + metric_ids

    # Excluded-exposure refusal applies ONLY to known baseline fields; unknown
    # custom keys are never in the catalog so they cannot be "excluded".
    _refuse_excluded(field_ids)

    return _run_getdata_pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="catalog_daily", dim_ids=dim_ids, metric_ids=metric_ids,
        site_id=site_id, allow_unknown_columns=user_supplied,
    )


# ---------------------------------------------------------------------------
# Account topology (pattern 25.5): organization -> site.
# ---------------------------------------------------------------------------


def _candidate_site_ids() -> list[str]:
    """Candidate site ids for discovery (connection input).

    Piano has NO documented endpoint that lists the sites an API key can reach.
    v1 discovery verifies CANDIDATE site ids supplied at connection time. The
    candidates enter here from the PIANO_CANDIDATE_SITE_IDS env (a comma list) --
    this is a DISCOVERY input, not an account fallback: every candidate is
    access-checked and the SELECTION still flows through the core topology
    state machine (an unverified site never runs a pull, see _resolve_site_id).
    """
    raw = os.environ.get("PIANO_CANDIDATE_SITE_IDS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _access_check_reachable(site_id: str) -> bool:
    """1-day tier-core getData probe: 200 -> reachable; 403 -> not selectable."""
    from core.pull_errors import PermissionDeniedError  # noqa: PLC0415

    space = _space(site_id)
    period = {
        "p1": [
            {"type": "R", "granularity": "D", "startOffset": -1, "endOffset": -1}
        ]
    }
    body = {
        "columns": ["m_visits"],
        "space": space,
        "period": period,
        "sort": ["-m_visits"],
        "max-results": 1,
    }
    try:
        _post_getdata(body)
        return True
    except PermissionDeniedError:
        return False


def discover_accounts(
    connection_id: str, candidate_site_ids: list[str] | None = None
) -> list[dict]:
    """List the sites this connection's key can reach (topology discovery).

    Since Piano exposes no site-enumeration endpoint, discovery VERIFIES the
    supplied candidate site ids via a 1-day getData access-check probe and
    returns only the reachable ones (unreachable candidates dropped). Returns
    the generic flat list the core topology flow consumes:

        [{"id": "429023", "label": "Site 429023", "level": "site"}, ...]

    Raises core.quota.RateLimitError on 509/429; typed ConnectorError otherwise.
    """
    candidates = candidate_site_ids if candidate_site_ids is not None else _candidate_site_ids()
    accounts: list[dict] = []
    for site_id in candidates:  # serialized (no fan-out -- 509 concurrency guard)
        if _access_check_reachable(site_id):
            accounts.append(
                {"id": str(site_id), "label": f"Site {site_id}", "level": "site"}
            )
    logger.info(
        "piano_discover_accounts: candidates=%d reachable=%d",
        len(candidates), len(accounts),
    )
    return accounts


def check_account_access(connection_id: str, account_id: str) -> dict:
    """Access-check the SELECTED site (story 25.5 step 3).

    Minimal 1-day tier-core getData probe (yesterday UTC, m_visits, max-results
    1). A key without access to the site fails with the typed permission_denied
    from the error_map (403 UnauthorizedSite / InvalidSpace_NoActiveSite)."""
    space = _space(account_id)
    yesterday = (datetime.now(tz=timezone.utc).date() - timedelta(days=1)).isoformat()
    body = {
        "columns": ["m_visits"],
        "space": space,
        "period": {"p1": [{"type": "D", "start": yesterday, "end": yesterday}]},
        "sort": ["-m_visits"],
        "max-results": 1,
    }
    _post_getdata(body)
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


_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'piano'
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
    rows: list[dict], report_profile: str, date_from: str, date_to: str, project_id: str
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
        {"source_system": "piano", "source_field": "fact_daily_kpi", "pull_id": latest_pull_id}
        if latest_pull_id is not None
        else None
    )

    return {
        "schema_version": "1",
        "meta": {"freshness": latest_freshness, "provenance": provenance, "alerts": []},
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "metrics": data_by_metric,
        },
    }


@mcp_app.tool()
def get_piano_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "baseline_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Piano Analytics Report -- reads from fact_daily_kpi mart.

    Report profiles: baseline_daily, audience_daily, sources_daily,
                     events_daily, catalog_daily
    Metrics: visits, unique_visitors, page_loads, events (visits/unique_visitors
             are NON-ADDITIVE across breakdowns -- recompute totals via getTotal)
    Breakdown dimensions: device_type, geo_country, src_source, event_name

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of the declared profiles
        date_from: Start date ISO-8601 (e.g. '2026-06-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
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

    _register_raw("raw_piano_daily", provider="piano")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
