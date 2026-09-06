"""Search Ads 360 Reporting API v0 governed connector."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# Import au niveau module (et non paresseux comme les appels a `core` dans les
# fonctions) : les classes d'exception ci-dessous en HERITENT, donc il doit etre
# resolu au moment ou le fichier est lu.
from core import pull_errors
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("sa360")

API_VERSION = "v0"
PROVIDER_API_VERSION = "search-ads-360-reporting-v0"
API_BASE = "https://searchads360.googleapis.com/v0"
MAX_PAGE_SIZE = 10_000
MAX_IN_ITEMS = 20_000
STREAM_THRESHOLD_ROWS = 10_000
QUERIES_PER_MINUTE_PROJECT_USER = 3_000
QUERIES_PER_MINUTE_PROJECT = 3_000
QUERIES_PER_DAY_PROJECT = 150_000
REQUEST_TIMEOUT_SECONDS = 60

_quota_lock = threading.Lock()
_minute_usage: dict[tuple[str, str], int] = {}
_daily_usage: dict[tuple[str, str], int] = {}


class Sa360OnboardingError(pull_errors.PermissionDeniedError):
    """Typed customer discovery or routing failure.

    `permission_denied` : le credential est authentifie et n'atteint rien. L'action
    juste est de se reconnecter avec les bons droits, et c'est `permission_denied`
    qui la fait remonter a l'ecran (`user_action="reconnect"`).

    Avant le 2026-08-01 cette classe heritait d'un `RuntimeError` nu : le worker la
    voyait `unclassified`, la rejouait jusqu'au `dead_letter` contre un credential
    qui ne marchera jamais, et n'affichait aucune action.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


class Sa360NotConfiguredError(Sa360OnboardingError):
    """Le credential va bien -- c'est la requete qui ne peut pas etre formee.

    Derive de l'erreur d'onboarding pour qu'un `except Sa360OnboardingError` existant continue de
    l'attraper, mais porte `invalid_request` : dire << reconnecte-toi >> enverrait
    l'operateur au mauvais ecran, puisque le compte se choisit dans l'assistant
    Datastream et pas sur la connexion.
    """

    error_class = pull_errors.INVALID_REQUEST
    user_action = pull_errors.SELECT_SOURCE_ACCOUNT


class Sa360CompatibilityError(pull_errors.InvalidRequestError, ValueError):
    """An explicit field selection is incompatible with its SA360 resource.

    `invalid_request` : rejouer la meme requete redonne la meme reponse, et c'est
    aussi le signal `pull_invalid_request_drift` -- une forme devenue illegale est
    une derive du catalogue. `ValueError` reste dans les bases, des
    appelants et des tests l'attrapent sous ce nom.
    """

    #: -> `Mapping` : le plan demande ce que la source ne rend plus
    #: (datastream-workbench-and-wizard.md:107). L'operateur a un endroit
    #: ou aller, contrairement a une derive de version d'API.
    user_action = pull_errors.REVIEW_MAPPING

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


class Sa360MessageTooLargeError(RuntimeError):
    """The provider rejected a response at its transport message boundary."""


# ---------------------------------------------------------------------------
# The operator's selected account, and how it travels.
#
# The reporting call addresses ONE customer -- it is in the URL path
# (/customers/{id}/searchAds360:search). ``login-customer-id`` is not a second
# account: it is the ROUTE, the header that says through which manager the
# client is reached, and a directly accessible customer has none. The research
# says it in those terms (sa360-catalog-research.md §2): "For manager-to-client
# calls, send login-customer-id without hyphens and route the query to the
# client customer id."
#
# The core scope stores ONE opaque string per connection, so the route rides
# inside it when it exists: '<client_customer_id>@<login_customer_id>', the
# composite opaque id google-ads ratified in story 26.2 as '<cid>@<login_cid>'.
# A bare id means the direct route. Core never interprets it (AD-2).
# ---------------------------------------------------------------------------

ACCOUNT_ID_SEPARATOR = "@"

_SELECTION_HINT = (
    "The operator picks a customer in the Datastream wizard: discover_accounts "
    "lists what customers:listAccessibleCustomers returns, and the worker passes the "
    "chosen id back as the `client_customer_id` argument declared in manifest.json "
    "under account_topology.pull_parameter."
)


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def _compatibility() -> dict:
    return json.loads(
        (Path(__file__).parent / "catalog_sources" / "compatibility.json").read_text(
            encoding="utf-8"
        )
    )


def _reason(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error") or {}
    details = error.get("details") or []
    for detail in details:
        errors = detail.get("errors") if isinstance(detail, dict) else None
        if errors and isinstance(errors[0], dict):
            return errors[0].get("errorCode") or errors[0].get("message")
    return error.get("status") or payload.get("code")


def _raise_response(response) -> None:
    try:
        original = response.json()
    except Exception:
        original = response.text
    reason = _reason(original)
    message = json.dumps(original).lower() if isinstance(original, dict) else str(original).lower()
    # H-2 NOTE (validation_required): The substrings below are a heuristic for the gRPC 4 MB
    # transport boundary 429.  The exact provider error shape has NOT been confirmed against a live
    # SA360 response (live gate is blocked — see manifest verification.validation_required).
    # Assumed shape: HTTP 429, JSON body with an "error.message" field containing text like
    # "Received message larger than 4 MB" or equivalent.  If the provider instead encodes this in
    # a grpc-message/grpc-status header, or uses different wording (e.g. "message too large"), the
    # 429 will silently fall through to RateLimitError below rather than Sa360MessageTooLargeError.
    # Secondary signal to consider when unblocked: check response.headers.get("grpc-message").
    if response.status_code == 429 and any(
        marker in message for marker in ("message larger", "4 mb", "received message larger")
    ):
        raise Sa360MessageTooLargeError(
            "SA360 response exceeded the transport boundary; reduce fields/page size "
            "or use SearchStream"
        )
    if response.status_code == 429 or reason == "RESOURCE_EXHAUSTED":
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("sa360", retry_after)
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and reason:
        normalized["code"] = reason
        request_id = response.headers.get("request-id")
        if request_id:
            normalized["request_id"] = request_id[:64]
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _request(client, method: str, path: str, token: str, login_customer_id=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"}
    if login_customer_id:
        headers["login-customer-id"] = normalize_customer_id(login_customer_id)
    response = client.request(
        method,
        f"{API_BASE}{path}",
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
        **kwargs,
    )
    if response.status_code < 200 or response.status_code >= 300:
        _raise_response(response)
    return response


def normalize_customer_id(value: str) -> str:
    normalized = re.sub(r"\D", "", str(value))
    if not normalized:
        raise Sa360NotConfiguredError("SA360 customer id must contain digits")
    return normalized


def split_account_id(account_id: str) -> tuple[str, str | None]:
    """Parse ``'<client_customer_id>'`` or ``'<client_customer_id>@<login_customer_id>'``.

    Returns ``(client_customer_id, login_customer_id)`` with ``None`` for the
    route when the id carries none: that is the DIRECT route, which is a fact
    about the selection, not a missing piece. Inventing a manager here would
    send a ``login-customer-id`` header the operator never chose, and SA360
    answers such a call with someone else's data or a 403 -- never with a
    diagnosis.
    """
    raw = str(account_id)
    customer, separator, login = raw.partition(ACCOUNT_ID_SEPARATOR)
    return (
        normalize_customer_id(customer),
        normalize_customer_id(login) if separator and login else None,
    )


def _resolve_selected_account(client_customer_id: str | None) -> tuple[str, str | None]:
    """Resolve (client_customer_id, login_customer_id) from the selection ONLY.

    No environment fallback -- ``core/account_topology.py`` declares the
    ``*_ACCOUNT_ID`` pattern deprecated, and one deployment-wide variable would
    pull the same customer for every project. No ``selection`` fallback either:
    the selection the PLAN produces carries only selection_mode / metrics /
    dimensions / grain / filters (``datastream-intent.schema.json``,
    ``additionalProperties: false``), and ``core/queue.py`` never fills
    ``job["selection"]`` at all.
    """
    if not client_customer_id:
        raise Sa360NotConfiguredError(
            "SA360 pull has no selected account: `client_customer_id` is empty. " + _SELECTION_HINT
        )
    return split_account_id(client_customer_id)


def discover_accounts(connection_id: str, *, _client=None, _token: str | None = None) -> list[dict]:
    from core import nango_client  # noqa: PLC0415

    token = _token or nango_client.get_fresh_token(connection_id, provider="sa360")
    client = _client or httpx.Client()
    payload = _request(client, "GET", "/customers:listAccessibleCustomers", token).json()
    names = payload.get("resourceNames") or []
    if not names:
        raise Sa360OnboardingError(
            "No Search Ads 360 customer is accessible; verify direct or manager permissions"
        )
    return [
        {
            # The id core stores and hands back at pull time. It was a loop counter
            # ('sa360_selection_3'): unstable between two discoveries and carrying
            # no customer, so nothing it reached could act on it.
            # listAccessibleCustomers returns DIRECTLY accessible customers, so the
            # id is bare -- no manager route to encode.
            "id": normalize_customer_id(name),
            # core._label_for_account reads `label`; `display_name` was invisible
            # to it, so every account was offered unlabeled.
            "label": name,
            "customer_id": normalize_customer_id(name),
            "login_customer_id": None,
            "routing_mode": "direct",
            "display_name": name,
        }
        for name in names
    ]


def with_manager_route(account: dict, login_customer_id: str) -> dict:
    """Re-route a discovered ACCOUNT node through a manager, id included.

    The parameter is named ``account``, not ``selection``, and the rename is the
    whole point of this task rather than cosmetics: two different objects were
    called `selection` in this codebase -- the report selection the plan
    produces (selection_mode / metrics / dimensions / grain / filters) and an
    account object the plan cannot produce. This one is a node emitted by
    ``discover_accounts``. Calling it `selection` is how the confusion started.

    The ``id`` is rewritten too, not only the ``login_customer_id`` field: the id
    is the ONLY thing the core scope keeps and the only thing the pull receives.
    Leaving it bare produced an account that looked routed and pulled direct.
    """
    login = normalize_customer_id(login_customer_id)
    customer = normalize_customer_id(account["customer_id"])
    return {
        **account,
        "id": f"{customer}{ACCOUNT_ID_SEPARATOR}{login}",
        "login_customer_id": login,
        "routing_mode": "manager_client",
    }


def validate_fields(profile_id: str, fields: list[str]) -> None:
    legal = _compatibility()["profiles"].get(profile_id)
    if legal is None:
        raise Sa360CompatibilityError(f"Unknown SA360 profile: {profile_id}")
    invalid = sorted(set(fields) - set(legal["fields"]))
    if invalid:
        raise Sa360CompatibilityError(f"Fields incompatible with {legal['resource']}: {invalid}")


def validate_in_values(values: list[str]) -> None:
    if len(values) > MAX_IN_ITEMS:
        raise Sa360CompatibilityError(
            f"SA360 IN list contains {len(values)} values; maximum is {MAX_IN_ITEMS}"
        )


def build_saql(
    profile_id: str,
    fields: list[str],
    date_from: str,
    date_to: str,
    *,
    in_filter: tuple[str, list[str]] | None = None,
) -> str:
    validate_fields(profile_id, fields)
    legal = _compatibility()["profiles"][profile_id]
    predicates = []
    if "segments.date" in fields:
        predicates.append(f"segments.date BETWEEN '{date_from}' AND '{date_to}'")
    if in_filter:
        field, values = in_filter
        validate_in_values(values)
        if field not in legal["filterable"]:
            raise Sa360CompatibilityError(f"Field is not filterable for {profile_id}: {field}")
        escaped = ", ".join("'" + value.replace("'", "\\'") + "'" for value in values)
        predicates.append(f"{field} IN ({escaped})")
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    return f"SELECT {', '.join(fields)} FROM {legal['resource']}{where} ORDER BY segments.date"


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()


def _charge_query(project_id: str, *, now: datetime | None = None) -> None:
    from core import quota  # noqa: PLC0415
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    allowed, reason = quota.pre_check("sa360", 1)
    if not allowed:
        raise ProviderTransientError(message=f"SA360 minute quota unavailable: {reason}")
    instant = now or datetime.now(UTC)
    minute_key = (project_id, instant.strftime("%Y-%m-%dT%H:%M"))
    day_key = (project_id, instant.date().isoformat())
    with _quota_lock:
        minute_spent = _minute_usage.get(minute_key, 0)
        day_spent = _daily_usage.get(day_key, 0)
        if minute_spent >= QUERIES_PER_MINUTE_PROJECT:
            raise ProviderTransientError(message=f"SA360 minute budget exhausted for {project_id}")
        if day_spent >= QUERIES_PER_DAY_PROJECT:
            raise ProviderTransientError(message=f"SA360 daily budget exhausted for {project_id}")
        _minute_usage[minute_key] = minute_spent + 1
        _daily_usage[day_key] = day_spent + 1
    quota.record_spend("sa360", 1)


def search(
    client,
    token: str,
    customer_id: str,
    query: str,
    project_id: str,
    *,
    page_size: int = MAX_PAGE_SIZE,
    login_customer_id: str | None = None,
) -> tuple[list[dict], str | None]:
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise Sa360CompatibilityError(f"SA360 page size must be between 1 and {MAX_PAGE_SIZE}")
    _charge_query(project_id)
    rows: list[dict] = []
    page_token = None
    request_id = None
    while True:
        body = {"query": query, "pageSize": page_size}
        if page_token:
            body["pageToken"] = page_token
        response = _request(
            client,
            "POST",
            f"/customers/{normalize_customer_id(customer_id)}/searchAds360:search",
            token,
            login_customer_id,
            json=body,
        )
        request_id = response.headers.get("request-id") or request_id
        payload = response.json()
        rows.extend(payload.get("results") or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            return rows, request_id[:64] if request_id else None


def search_stream(
    client,
    token: str,
    customer_id: str,
    query: str,
    project_id: str,
    *,
    login_customer_id: str | None = None,
) -> tuple[list[dict], str | None]:
    _charge_query(project_id)
    response = _request(
        client,
        "POST",
        f"/customers/{normalize_customer_id(customer_id)}/searchAds360:searchStream",
        token,
        login_customer_id,
        json={"query": query},
    )
    batches = response.json()
    rows = [row for batch in batches for row in batch.get("results") or []]
    request_id = response.headers.get("request-id")
    return rows, request_id[:64] if request_id else None


def check_account_access(connection_id: str, account_id: str, *, _client=None, _token=None):
    """Access-check the SELECTED account, addressed by its opaque id.

    Takes the same one opaque string the scope stores and the pull receives --
    as google-ads, microsoft-ads, amazon-ads and pinterest-ads already do. It
    used to take a dict, so the verified thing and the pulled thing were
    addressed differently and could drift apart with nothing to notice.
    """
    from core import nango_client  # noqa: PLC0415

    customer_id, login_customer_id = _resolve_selected_account(account_id)
    token = _token or nango_client.get_fresh_token(connection_id, provider="sa360")
    client = _client or httpx.Client()
    query = "SELECT customer.id FROM customer LIMIT 1"
    rows, request_id = search(
        client,
        token,
        customer_id,
        query,
        "onboarding",
        page_size=1,
        login_customer_id=login_customer_id,
    )
    return {
        "accessible": True,
        "account_id": account_id,
        "rows": len(rows),
        "request_id": request_id,
    }


# NOTE on currency_code. That column is customer metadata; the one opaque string
# the scope stores is spent on the customer and its route, so it lands empty on a
# worker-driven pull. It is recoverable -- `SELECT customer.currency_code FROM
# customer` -- at the cost of one extra query per pull. Left empty rather than
# guessed, and named here so the gap is inventory rather than silence.
_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_sa360_daily (
    report_profile VARCHAR, manager_customer_id VARCHAR, customer_id VARCHAR,
    resource_id VARCHAR, date VARCHAR, campaign_id VARCHAR, ad_group_id VARCHAR,
    criterion_id VARCHAR, device VARCHAR, currency_code VARCHAR, dimensions_json VARCHAR,
    metric VARCHAR, value DOUBLE, provider_value VARCHAR, non_additive BOOLEAN,
    query_hash VARCHAR, request_id VARCHAR, pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_sa360_daily
    (report_profile, manager_customer_id, customer_id, resource_id, date, campaign_id,
    ad_group_id, criterion_id, device, currency_code, dimensions_json, metric, value,
    provider_value, non_additive, query_hash, request_id, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _extract_path(row: dict, path: str):
    source_paths = {
        item["field_id"]: item["source_field"]
        for item in _manifest()["source_capabilities"]["fields"]
    }
    path = source_paths.get(path, path)
    current: Any = row
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _land(rows: list[dict], fields: list[str], context: dict) -> int:
    if os.environ.get("TOOROW_DB_MODE", "duckdb") not in ("duckdb", "bigquery"):
        raise ValueError("sa360 landing supports duckdb and bigquery")
    from core import warehouse_write  # noqa: PLC0415

    path = os.environ.get(
        "TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "seeds" / "local.duckdb")
    )
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    metric_fields = {
        item["field_id"]
        for item in _manifest()["source_capabilities"]["fields"]
        if item["kind"] == "metric"
    }
    values = []
    non_additive_tokens = ("share", "rate", "average", "ratio")
    for row in rows:
        dims = {field: _extract_path(row, field) for field in fields if field not in metric_fields}
        resource_id = next(
            (
                str(value)
                for key, value in dims.items()
                if key.endswith(".id") and value is not None
            ),
            "",
        )
        for metric in fields:
            if metric not in metric_fields:
                continue
            provider_value = _extract_path(row, metric)
            try:
                value = float(provider_value)
                if metric.endswith("_micros"):
                    value /= 1_000_000
            except (TypeError, ValueError):
                value = None
            values.append(
                (
                    context["report_profile"],
                    context.get("login_customer_id") or "",
                    context["customer_id"],
                    resource_id,
                    dims.get("segments.date"),
                    dims.get("campaign.id"),
                    dims.get("adGroup.id"),
                    dims.get("adGroupCriterion.criterionId"),
                    dims.get("segments.device"),
                    context.get("currency_code", ""),
                    json.dumps(dims, sort_keys=True),
                    metric,
                    value,
                    str(provider_value),
                    any(token in metric for token in non_additive_tokens),
                    context["query_hash"],
                    context.get("request_id"),
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = warehouse_write.open_raw_writer(path, project_id=context["project_id"])
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(_RAW_INSERT_SQL, values)
    connection.close()
    return len(values)


def _pull_profile(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile_id: str,
    client_customer_id: str | None = None,
    selection: dict | None = None,
    *,
    _client=None,
    _token=None,
):
    """Run one report profile against the SELECTED account.

    ``client_customer_id`` is the opaque account id the worker passes (declared
    in manifest.json as ``account_topology.pull_parameter``). ``selection`` keeps
    its legitimate cargo -- here, ``estimated_rows``, a size hint that picks
    searchStream over search. See the note below: nothing fills it today.
    """
    from core import nango_client  # noqa: PLC0415

    customer_id, login_customer_id = _resolve_selected_account(client_customer_id)
    profile = next(
        item for item in _manifest()["source_capabilities"]["reports"] if item["id"] == profile_id
    )
    fields = profile["dimensions"] + profile["metrics"]
    query = build_saql(profile_id, fields, date_from, date_to)
    token = _token or nango_client.get_fresh_token(connection_id, provider="sa360")
    client = _client or httpx.Client()
    # KNOWN DEAD KNOB: `estimated_rows` has no producer. It is not part of the
    # plan's selection ($defs.selection is additionalProperties:false), and
    # queue.py never fills job["selection"], so a worker-driven pull always takes
    # the paged `search` path and never searchStream. Kept -- it is a reporting
    # choice, not an account, and direct callers use it -- but it is a switch
    # nothing can currently flip. Named rather than deleted: an absence leaves
    # almost no trace, and destroying the little it leaves erases the inventory.
    estimated_rows = int((selection or {}).get("estimated_rows", 0) or 0)
    extractor = search_stream if estimated_rows > STREAM_THRESHOLD_ROWS else search
    rows, request_id = extractor(
        client,
        token,
        customer_id,
        query,
        project_id,
        login_customer_id=login_customer_id,
    )
    # Built explicitly, never spread from `selection`: the account provenance of a
    # landed row comes from the verified scope, not from whatever a caller put in
    # a dict.
    context = {
        "customer_id": customer_id,
        "login_customer_id": login_customer_id,
        "report_profile": profile_id,
        "query_hash": query_hash(query),
        "request_id": request_id,
        "pull_id": pull_id,
        "project_id": project_id,
    }
    row_count = _land(rows, fields, context)
    logger.info(
        "sa360_pull_completed: pull_id=%s profile=%s rows=%d request_id=%s",
        pull_id,
        profile_id,
        row_count,
        request_id,
    )
    return {"pull_id": pull_id, "row_count": row_count, "date_from": date_from, "date_to": date_to}


def pull(
    connection_id,
    date_from,
    date_to,
    project_id,
    pull_id,
    client_customer_id=None,
    selection=None,
    *,
    _client=None,
    _token=None,
):
    """Default pull() = the campaign_daily grain.

    ``client_customer_id`` is OPTIONAL on purpose. The worker passes the account
    only when a selection exists (core/queue.py::_account_kwargs); a required
    positional would raise a bare ``TypeError`` -- outside every taxonomy -- for
    any Datastream whose account has not been chosen yet.
    """
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "campaign_daily",
        client_customer_id,
        selection,
        _client=_client,
        _token=_token,
    )


def pull_campaign_daily(
    connection_id,
    date_from,
    date_to,
    project_id,
    pull_id,
    client_customer_id=None,
    selection=None,
    *,
    _client=None,
    _token=None,
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "campaign_daily",
        client_customer_id,
        selection,
        _client=_client,
        _token=_token,
    )


def pull_ad_group_daily(
    connection_id,
    date_from,
    date_to,
    project_id,
    pull_id,
    client_customer_id=None,
    selection=None,
    *,
    _client=None,
    _token=None,
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "ad_group_daily",
        client_customer_id,
        selection,
        _client=_client,
        _token=_token,
    )


def pull_keyword_daily(
    connection_id,
    date_from,
    date_to,
    project_id,
    pull_id,
    client_customer_id=None,
    selection=None,
    *,
    _client=None,
    _token=None,
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "keyword_daily",
        client_customer_id,
        selection,
        _client=_client,
        _token=_token,
    )


def pull_catalog_daily(
    connection_id,
    date_from,
    date_to,
    project_id,
    pull_id,
    client_customer_id=None,
    selection=None,
    *,
    _client=None,
    _token=None,
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "catalog_daily",
        client_customer_id,
        selection,
        _client=_client,
        _token=_token,
    )


def _demicro(source_key: str, value):
    """Applique la regle micros de SA360 : les champs *_micros arrivent en
    micro-unites int64 (stringifiees) et se lisent en unite monetaire.

    C'est EXACTEMENT la regle que _pull_profile applique deja avant d'atterrir
    (``if metric.endswith("_micros"): value /= 1_000_000``). transform() ne la
    portait pas : il renommait metrics.cost_micros en 'cost' en laissant 1200000
    tel quel, si bien que le contrat declare (golden_pull -> expected_facts)
    annoncait un cout mille fois trop grand par rapport a ce que le connecteur
    ecrit reellement. Les connecteurs freres (google-ads, meta-ads) convertissent
    tous dans leur transform().
    """
    if not source_key.endswith("_micros"):
        return value
    try:
        return float(value) / 1_000_000
    except (TypeError, ValueError):
        return None


def transform(raw_rows: list[dict]) -> list[dict]:
    mappings = _manifest()["canonical_metric_mapping"] | _manifest()["canonical_dimension_mapping"]
    return [
        {
            (
                mappings.get(key, key)
                if isinstance(mappings.get(key, key), str)
                else mappings[key]["canonical"]
            ): _demicro(key, value)
            for key, value in row.items()
        }
        for row in raw_rows
    ]


@mcp_app.tool()
def get_sa360_report(
    project_id: str = "default",
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed SA360 report envelope; extraction remains queue-only."""
    return {
        "schema_version": "1",
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "metrics": {},
        },
    }
