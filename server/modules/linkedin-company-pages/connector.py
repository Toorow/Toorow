"""Organic LinkedIn Company Pages Community Management connector."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx

# Import au niveau module (et non paresseux comme les appels a `core` dans les
# fonctions) : les classes d'exception ci-dessous en HERITENT, donc il doit etre
# resolu au moment ou le fichier est lu.
from core import pull_errors
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("linkedin-company-pages")
API_BASE = "https://api.linkedin.com/rest"
LINKEDIN_VERSION = os.environ.get("LINKEDIN_COMPANY_PAGES_API_VERSION", "202604")
RESTLI_VERSION = "2.0.0"
MAX_VERSION_AGE_MONTHS = 12


class LinkedInPagesOnboardingError(pull_errors.PermissionDeniedError):
    """Community Management approval/scope/admin access is unavailable.

    `permission_denied` : le credential est authentifie et n'atteint rien. L'action
    juste est de se reconnecter avec les bons droits, et c'est `permission_denied`
    qui la fait remonter a l'ecran (`user_action="reconnect"`).

    Avant le 2026-08-01 cette classe heritait d'un `RuntimeError` nu : le worker la
    voyait `unclassified`, la rejouait jusqu'au `dead_letter` contre un credential
    qui ne marchera jamais, et n'affichait aucune action.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


class LinkedInPagesNotConfiguredError(LinkedInPagesOnboardingError):
    """Le credential va bien -- c'est la requete qui ne peut pas etre formee.

    Derive de l'erreur d'onboarding pour qu'un `except LinkedInPagesOnboardingError`
    existant continue de l'attraper, mais porte `invalid_request` : dire
    << reconnecte-toi >> enverrait l'operateur au mauvais ecran, puisque le compte se
    choisit dans l'assistant Datastream et pas sur la connexion.
    """

    error_class = pull_errors.INVALID_REQUEST
    user_action = pull_errors.SELECT_SOURCE_ACCOUNT


class LinkedInPagesCompatibilityError(pull_errors.InvalidRequestError, ValueError):
    """The requested profile/window/facet combination is illegal.

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


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def check_version_support(today: date | None = None) -> int:
    current = today or datetime.now(UTC).date()
    year, month = int(LINKEDIN_VERSION[:4]), int(LINKEDIN_VERSION[4:])
    age = (current.year - year) * 12 + current.month - month
    if age < 0 or age >= MAX_VERSION_AGE_MONTHS:
        raise RuntimeError(
            f"Pinned Linkedin-Version {LINKEDIN_VERSION} is outside the supported rollout window"
        )
    return age


def _provider_code(payload):
    if not isinstance(payload, dict):
        return None
    return str(payload.get("code") or payload.get("serviceErrorCode") or "") or None


def _raise_response(response):
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("linkedin-company-pages", retry_after)
    try:
        original = response.json()
    except Exception:
        original = response.text
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and (code := _provider_code(original)):
        normalized["code"] = code
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _headers(token: str) -> dict[str, str]:
    check_version_support()
    return {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": RESTLI_VERSION,
    }


def _request(client, method, path, token, **kwargs):
    response = client.request(
        method, f"{API_BASE}{path}", headers=_headers(token), timeout=60, **kwargs
    )
    if response.status_code < 200 or response.status_code >= 300:
        _raise_response(response)
    return response


def _token(connection_id: str) -> str:
    from core import nango_client  # noqa: PLC0415

    return nango_client.get_fresh_token(connection_id, provider="linkedin-company-pages")


def discover_accounts(connection_id: str, *, _client=None, _token_value=None) -> list[dict]:
    token = _token_value or _token(connection_id)
    client = _client or httpx.Client()
    payload = _request(
        client,
        "GET",
        "/organizationAcls",
        token,
        params={"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED"},
    ).json()
    # `id` et `label` sont les DEUX seules cles que core retient
    # (core/account_topology.py::_flatten_account_ids / _label_for_account) : `id`
    # est stocke verbatim dans app.connection_account_scope puis repasse a pull()
    # sous le nom declare par account_topology.pull_parameter. Il doit donc etre
    # l'URN COMPLETE (`urn:li:organization:<id>`) : c'est la valeur que LinkedIn
    # attend pour organizationalEntity, et la seule qui ne demande aucune
    # reconstruction cote connecteur. Un id synthetique de rang
    # ('linkedin_pages_selection_1') produirait un INVALID_URN a chaque pull.
    selections = []
    for item in payload.get("elements") or []:
        urn = str(item.get("organization") or item.get("organizationalTarget") or "")
        if not urn:
            continue
        selections.append(
            {
                "id": urn,
                "label": item.get("organizationName") or urn,
                "organization_urn": urn,
                "organization_path": quote(urn, safe=""),
                "display_name": item.get("organizationName") or urn,
            }
        )
    if not selections:
        raise LinkedInPagesOnboardingError(
            "No approved administered organization; verify rw_organization_admin "
            "and Community Management access"
        )
    return selections


def validate_window(
    date_from: str | None,
    date_to: str | None,
    *,
    lifetime: bool,
    facet: str | None,
    today: date | None = None,
) -> None:
    if lifetime:
        return
    if facet:
        raise LinkedInPagesCompatibilityError(
            "Time-bound follower/page demographic facets are unsupported"
        )
    current = today or datetime.now(UTC).date()
    start, end = date.fromisoformat(date_from or ""), date.fromisoformat(date_to or "")
    latest = current - timedelta(days=2)
    earliest = latest - timedelta(days=365)
    if start < earliest or end > latest or end < start:
        raise LinkedInPagesCompatibilityError(
            "Time-bound LinkedIn Page statistics require the rolling 12 months ending two days ago"
        )


def paginate_restli(client, token, path, *, params, count=100) -> list[dict]:
    start = 0
    rows = []
    while True:
        query = {**params, "start": start, "count": count}
        payload = _request(client, "GET", path, token, params=query).json()
        page = payload.get("elements") or []
        rows.extend(page)
        paging = payload.get("paging") or {}
        total = int(paging.get("total") or len(rows))
        if not page or start + len(page) >= total:
            return rows
        start += len(page)


PROFILE_PATHS = {
    "follower_statistics": "/organizationalEntityFollowerStatistics",
    "page_statistics": "/organizationPageStatistics",
    "organic_share_statistics": "/organizationalEntityShareStatistics",
}


#: Prefixe d'URN accepte pour organizationalEntity. LinkedIn adresse les trois
#: endpoints organiques par une URN COMPLETE ; les ACL renvoient
#: `urn:li:organization:<id>` et, pour certaines pages, `urn:li:organizationBrand:<id>`.
_ORGANIZATION_URN_PREFIX = "urn:li:"


def require_organization_urn(organization_urn):
    """Refuse l'absence -- et refuse un id nu plutot que de fabriquer une URN.

    Reconstruire `urn:li:organization:` + id serait une invention : un
    identifiant nu ne dit pas de quel TYPE d'entite il vient, et une page
    `organizationBrand` traitee comme une `organization` interroge une autre
    entite sans que rien ne le signale. La discovery renvoie deja l'URN complete
    (`discover_accounts` -> node id), donc l'absence de prefixe signale une
    selection abimee en amont, pas un format a completer ici.

    Pas de repli d'environnement : `core/account_topology.py:514` les declare
    deprecies, et une variable unique pour tout le deploiement ferait tirer la
    meme page pour tous les projets.
    """
    if not organization_urn:
        raise LinkedInPagesNotConfiguredError(
            "No administered LinkedIn organization selected for this connection: "
            "the operator picks one in the Datastream wizard (discover_accounts "
            "lists the APPROVED ADMINISTRATOR organizationAcls the token can "
            "reach) and the worker passes it as `organization_urn` "
            "(account_topology.pull_parameter). There is no deployment-wide "
            "default -- one would pull the same Page for every project."
        )
    value = str(organization_urn)
    if not value.startswith(_ORGANIZATION_URN_PREFIX):
        raise LinkedInPagesCompatibilityError(
            f"organization_urn must be the full LinkedIn URN the ACL returned "
            f"(e.g. {_ORGANIZATION_URN_PREFIX}organization:<id>), not a bare id: "
            f"got {value!r}. A bare id does not say which entity type it belongs "
            f"to, so it is refused rather than rebuilt into an URN."
        )
    return value


def fetch_statistics(
    client, token, profile, selection, date_from, date_to, *, organization_urn=None
):
    if profile not in PROFILE_PATHS:
        raise LinkedInPagesCompatibilityError(f"Unsupported organic profile: {profile}")
    # L'organisation arrive par le parametre declare, plus par `selection`. La
    # selection que le PLAN fournit (core/schemas/datastream-intent.schema.json,
    # $defs.selection, additionalProperties: false) ne porte que selection_mode /
    # metrics / dimensions / grain / filters, et core/queue.py ne remplit jamais
    # job["selection"]. Le reste de `selection` (lifetime, facet, grain) reste lu.
    organization_urn = require_organization_urn(organization_urn)
    selection = selection or {}
    lifetime = bool(selection.get("lifetime"))
    facet = selection.get("facet")
    validate_window(date_from, date_to, lifetime=lifetime, facet=facet)
    params = {"q": "organizationalEntity", "organizationalEntity": organization_urn}
    if not lifetime:
        params["timeIntervals.timeRange.start"] = int(
            datetime.fromisoformat(date_from).replace(tzinfo=UTC).timestamp() * 1000
        )
        params["timeIntervals.timeRange.end"] = int(
            (datetime.fromisoformat(date_to).replace(tzinfo=UTC) + timedelta(days=1)).timestamp()
            * 1000
        )
        params["timeIntervals.timeGranularityType"] = selection.get("grain", "DAY")
    if facet:
        params["facet"] = facet
    return paginate_restli(client, token, PROFILE_PATHS[profile], params=params)


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_linkedin_company_pages_daily (
 profile VARCHAR, organization_urn VARCHAR, entity_id VARCHAR, interval_start VARCHAR,
 interval_end VARCHAR, facet VARCHAR, facet_value VARCHAR, metric VARCHAR, value DOUBLE,
 non_additive BOOLEAN, lifetime BOOLEAN, payload_json VARCHAR, pull_id VARCHAR,
 loaded_at VARCHAR, project_id VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_linkedin_company_pages_daily
    (profile, organization_urn, entity_id, interval_start, interval_end, facet, facet_value,
    metric, value, non_additive, lifetime, payload_json, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _extract_page_statistics_metrics(row: dict) -> list[tuple[str, float]]:
    """Flatten the nested totalPageStatistics structure into (metric_name, value) pairs.

    LinkedIn organizationPageStatistics nests page view counts at:
      totalPageStatistics.views.allPageViews.pageViews
      totalPageStatistics.views.allDesktopPageViews.pageViews
      totalPageStatistics.views.allMobilePageViews.pageViews
    Clicks are nested inside totalPageStatistics.clicks (a dict keyed by click type);
    the total is the sum across all click-type keys whose values are numeric.
    """
    tps = row.get("totalPageStatistics") or {}
    views = tps.get("views") or {}
    pairs: list[tuple[str, float]] = []

    def _view(key: str) -> float | None:
        section = views.get(key) or {}
        v = section.get("pageViews")
        return float(v) if isinstance(v, (int, float)) else None

    for metric_name, view_key in (
        ("page_views", "allPageViews"),
        ("desktop_page_views", "allDesktopPageViews"),
        ("mobile_page_views", "allMobilePageViews"),
    ):
        val = _view(view_key)
        if val is not None:
            pairs.append((metric_name, val))

    # clicks: totalPageStatistics.clicks is a dict of click-type → count
    clicks_section = tps.get("clicks") or {}
    if isinstance(clicks_section, dict):
        total_clicks = sum(v for v in clicks_section.values() if isinstance(v, (int, float)))
        if clicks_section:  # only emit if the dict was non-empty
            pairs.append(("clicks", float(total_clicks)))
    elif isinstance(clicks_section, (int, float)):
        pairs.append(("clicks", float(clicks_section)))

    return pairs


def _land(rows, context):
    if os.environ.get("TOOROW_DB_MODE", "duckdb") not in ("duckdb", "bigquery"):
        raise ValueError("linkedin-company-pages landing supports duckdb and bigquery")
    from core import warehouse_write  # noqa: PLC0415

    path = os.environ.get("TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "local.duckdb"))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    profile = context["profile"]
    values = []
    for row in rows:
        entity_id = str(row.get("share") or row.get("organizationalEntity") or "")
        interval_start = str(row.get("timeRange", {}).get("start") or context["date_from"])
        interval_end = str(row.get("timeRange", {}).get("end") or context["date_to"])
        facet_value = str(row.get("facetValue") or "")
        payload_json = json.dumps(row, sort_keys=True, separators=(",", ":"))

        if profile == "page_statistics":
            metric_pairs = _extract_page_statistics_metrics(row)
        else:
            stats = (
                row.get("followerGains")
                or row.get("totalShareStatistics")
                or row.get("statistics")
                or {}
            )
            metric_pairs = [
                (metric, float(value))
                for metric, value in stats.items()
                if isinstance(value, (int, float))
            ]

        for metric, value in metric_pairs:
            values.append(
                (
                    profile,
                    context["organization_urn"],
                    entity_id,
                    interval_start,
                    interval_end,
                    context.get("facet", ""),
                    facet_value,
                    metric,
                    value,
                    context["lifetime"]
                    or any(
                        token in metric.lower()
                        for token in ("engagement", "unique", "rate", "distribution")
                    ),
                    context["lifetime"],
                    payload_json,
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = warehouse_write.open_raw_writer(path, project_id=context["project_id"])
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(
            _RAW_INSERT_SQL,
            values,
        )
    connection.close()
    return len(values)


def _pull_profile(
    connection_id, date_from, date_to, project_id, pull_id, profile, organization_urn, selection
):
    organization_urn = require_organization_urn(organization_urn)
    selection = selection or {}
    token = _token(connection_id)
    rows = fetch_statistics(
        httpx.Client(),
        token,
        profile,
        selection,
        date_from,
        date_to,
        organization_urn=organization_urn,
    )
    context = {
        **selection,
        "organization_urn": organization_urn,
        "profile": profile,
        "date_from": date_from,
        "date_to": date_to,
        "lifetime": bool(selection.get("lifetime")),
        "pull_id": pull_id,
        "project_id": project_id,
    }
    count = _land(rows, context)
    return {"pull_id": pull_id, "row_count": count, "date_from": date_from, "date_to": date_to}


# `organization_urn` est OPTIONNEL, jamais positionnel requis : la signature
# ratifiee est pull(connection_id, date_from, date_to, project_id, pull_id) et le
# worker ne passe le compte QUE si une selection existe (core/queue.py:636-637).
# Requis, il leverait un TypeError nu -- hors de toute taxonomie -- des que la
# selection est vide, au lieu de l'erreur typee que le produit sait afficher.


def pull(
    connection_id, date_from, date_to, project_id, pull_id, organization_urn=None, selection=None
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "follower_statistics",
        organization_urn,
        selection,
    )


def pull_follower_statistics(
    connection_id, date_from, date_to, project_id, pull_id, organization_urn=None, selection=None
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "follower_statistics",
        organization_urn,
        selection,
    )


def pull_page_statistics(
    connection_id, date_from, date_to, project_id, pull_id, organization_urn=None, selection=None
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "page_statistics",
        organization_urn,
        selection,
    )


def pull_organic_share_statistics(
    connection_id, date_from, date_to, project_id, pull_id, organization_urn=None, selection=None
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "organic_share_statistics",
        organization_urn,
        selection,
    )


def transform(raw_rows):
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
def get_linkedin_company_pages_report(
    project_id: str = "default",
    report_profile: str = "follower_statistics",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed organic LinkedIn Company Pages envelope."""
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
