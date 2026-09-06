"""Validated reference lookups for the two always-present foundations (Story 48.3).

AC1 requires the reporting currency to use "a searchable validated ISO 4217
selector" and the reporting timezone "a searchable validated IANA identifier
selector; neither is a free-text or hard-coded subset". Project Settings shipped
two plain text inputs, so the only feedback on a typo was a failure much later,
and nothing on the screen could tell an operator which values were even legal.

These two routes serve the governed vocabularies -- the same immutable versions
``core.money_policy`` validates against, so the list an operator picks from and
the list the server accepts cannot drift apart.

Read-only, offline, and bounded. No ISO or IANA service is contacted; ranking is
deterministic; the result is capped so a selector cannot ask for 598 zones at once.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

#: A typeahead shows a page, never the whole vocabulary.
MAX_LIMIT = 50
DEFAULT_LIMIT = 25


async def _authorize(request: Request) -> Response | None:
    """Any authenticated operator may read the reference vocabularies.

    They are public standards, identical for every organization, and carry no
    Project data -- so there is nothing here to scope. Authentication is still
    required: an unauthenticated endpoint is a surface, however dull its content.
    """
    from core.admin_api import _check_auth  # noqa: PLC0415

    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
        )
    return None


def _limit(request: Request) -> int:
    try:
        requested = int(request.query_params.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(requested, MAX_LIMIT))


async def _currencies(request: Request) -> Response:
    """GET /api/reference/currencies?q=&limit= -- selectable reporting currencies."""

    denied = await _authorize(request)
    if denied is not None:
        return denied
    from core.currency_vocabulary import search_currencies, selectable_reporting_currencies
    from core.project_provenance import PLATFORM_FALLBACK_CURRENCY

    query = request.query_params.get("q")
    matches = search_currencies(query, limit=_limit(request))
    return JSONResponse(
        {
            "vocabulary": "currency",
            "authority": "ISO 4217",
            # What a Project gets when nobody chooses -- served, not guessed, so a
            # creation screen can NAME the default it is about to accept instead
            # of copying `project_provenance`'s constant into the front where the
            # two would silently drift.
            "platform_default": PLATFORM_FALLBACK_CURRENCY,
            "total_selectable": len(selectable_reporting_currencies()),
            "items": [
                {
                    "code": item.code,
                    "display_name": item.display_name,
                    "numeric_code": item.numeric_code,
                    # The selector shows it because it is the reason two totals in
                    # different currencies round differently.
                    "minor_unit": item.minor_unit,
                }
                for item in matches
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


async def _timezones(request: Request) -> Response:
    """GET /api/reference/timezones?q=&limit= -- selectable reporting boundaries."""

    denied = await _authorize(request)
    if denied is not None:
        return denied
    from core.project_provenance import PLATFORM_FALLBACK_TIMEZONE
    from core.timezone_vocabulary import search_timezones, selectable_timezones, tzdb_version

    query = request.query_params.get("q")
    matches = search_timezones(query, limit=_limit(request))
    return JSONResponse(
        {
            "vocabulary": "timezone",
            "authority": "IANA Time Zone Database",
            # The zone a Project gets when neither an operator nor a browser
            # said anything. Same reason as the currency default above.
            "platform_default": PLATFORM_FALLBACK_TIMEZONE,
            # The release every derivation in this deployment resolves against. A
            # selector that cannot say which one is offering an unversioned answer.
            "tzdb_version": tzdb_version(),
            "total_selectable": len(selectable_timezones()),
            "items": [
                {"code": item.zone, "display_name": item.zone.replace("_", " "), "area": item.area}
                for item in matches
            ],
        },
        headers={"Cache-Control": "no-store"},
    )



# --- Les pays, arrives ici en AD-43 (2026-08-13) --------------------------
#
# Meme sujet exactement que les devises et les fuseaux : une liste de reference
# gouvernee, lue hors ligne, bornee -- celle que l'ecran propose et que le
# serveur accepte. Son adresse dit `/api/vocabularies` et non `/api/reference` :
# c'est une adresse, pas une responsabilite, et elle ne change pas.


def _country_vocabulary_error() -> Response:
    return JSONResponse(
        {
            "code": "country_vocabulary_unavailable",
            "message": "Canonical country vocabulary is unavailable.",
        },
        status_code=503,
    )

async def _countries(request: Request) -> Response:
    """GET /api/vocabularies/countries -- la liste ISO, telle qu'elle etait.

    L'authentification passe par `_authorize` comme ses deux voisines : meme
    exigence, meme refus, meme phrase. Le 401 qu'elle rendait disait « Bearer
    token required » la ou les devises et les fuseaux disent « Authentication is
    required » -- deux phrases pour un seul refus, sur trois routes qui sont la
    meme porte.
    """
    from core.country_vocabulary import (  # noqa: PLC0415
        CountryVocabularyError,
        get_country_vocabulary,
    )

    if denied := await _authorize(request):
        return denied

    try:
        countries = [
            {"code": country.code, "display_name": country.display_name}
            for country in get_country_vocabulary()
        ]
    except CountryVocabularyError:
        logger.exception("reference_vocabulary: canonical country vocabulary unavailable")
        return _country_vocabulary_error()
    return JSONResponse({"countries": countries}, status_code=200)


COUNTRY_VOCABULARY_ROUTES = [
    Route(
        "/api/vocabularies/countries",
        endpoint=_countries,
        methods=["GET"],
    ),
]


REFERENCE_VOCABULARY_ROUTES: list[Route] = [
    Route("/api/reference/currencies", _currencies, methods=["GET"]),
    Route("/api/reference/timezones", _timezones, methods=["GET"]),
]
