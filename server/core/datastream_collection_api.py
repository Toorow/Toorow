"""WHEN a Datastream collects, and WHICH days -- the four doors of one subject.

AD-40, second extraction (2026-08-12). Grouped by responsibility rather than by
address: the schedule (`/schedule`), the run asked for by hand (`/run`), the
day-grain extract ledger (`/ledger`) and the re-collection of named days
(`/api/projects/{project_id}/datastreams/{datastream_id}/refetch`). The last one
carries a Project-scoped address and a Datastream-scoped subject; it comes here
because the question it answers -- which days does this Datastream hold, and how
do I ask for one again -- is the same question as the other three, and splitting
them by URL prefix would have put the ledger and the button that repairs it in
two different files.

Its route is spliced back at the position it occupied, between `/ledger` and
`/mapping/profile`, so Starlette's resolution order is untouched.

The gates and the run declaration these doors go through are NOT here: they are
`core.datastream_dispatch`, shared with the nightly and hourly dispatchers, so a
run is the same object whoever asked for it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core import pull_job_states as _pull_job_states
from core.audit import (
    ACTION_DATASTREAM_RUN,
    write_audit_row,
)

logger = logging.getLogger("core.admin_api")

_DEFAULT_LEDGER_DAYS = 35

#: THE ADDRESS OF THE RE-COLLECTION, declared once (story 58.4).
#:
#: Named the way `DAILY_BREAKDOWN_ROUTE_PATH` is, so the integration test asserts
#: the mounted path against the constant the router uses rather than against a
#: second copy of the string typed into a test. `admin_api` re-imports it, so the
#: name a caller already knew still resolves at its old address.
REFETCH_ROUTE_PATH = (
    "/api/projects/{project_id}/datastreams/{datastream_id}/refetch"
)

#: ISO-8601 day. Local to this module rather than borrowed from `admin_api`: a
#: module-level import back would close a cycle, and the four other copies in
#: `server/core` show this one-line pattern is vocabulary, not a shared object.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _enforce_datastream_project_scope(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _enforce_datastream_project_scope as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _project_not_found_response(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _project_not_found_response as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _strict_project_capability_allowed(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _strict_project_capability_allowed as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _datastream_schedule(request: Request) -> Response:
    """GET/PUT /api/datastreams/{id}/schedule -- WHEN this Datastream runs (AI-119).

    The console's door onto the same row the MCP tools write and the dispatcher
    reads. Deliberately delegating to ``core.schedule_mcp`` rather than
    re-implementing the SQL: three doors onto one schedule is the point, and two
    implementations of "what is the effective window" would drift into two
    answers within a release.

    ``PATCH /api/datastreams/{id}`` already accepted `schedule_mode` and
    `date_window_days`, but nothing accepted `next_run_at` -- so the console
    could change the cadence and never the moment, which is the setting that
    actually decides when work happens.

    Lot D1 (issue #68) adds the setting that decides whether the moment means
    anything: ``enabled``. It was READ by ``read_schedule`` and put on this
    payload from the first day, and this door refused it -- so the console could
    show a Datastream a complete schedule and had no way to say, or change, that
    it would never run. It goes through THIS handler rather than a second route:
    three doors, one row.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )

    datastream_id = (request.path_params.get("id") or "").strip()
    project_id = (request.query_params.get("project_id") or "").strip()
    body: dict = {}
    if request.method == "PUT":
        try:
            raw = await request.body()
            body = json.loads(raw) if raw.strip() else {}
        except Exception:
            return JSONResponse(
                {"code": "invalid_body", "message": "Invalid JSON body"}, status_code=400
            )
        project_id = project_id or str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"}, status_code=400
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.schedule_mcp import UNSET, read_schedule, set_schedule  # noqa: PLC0415

    def _schedule_unset():
        return UNSET

    try:
        with get_connection() as conn:
            if not _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit" if request.method == "PUT" else "view",
            ):
                return _project_not_found_response()
            if request.method == "PUT":
                result = set_schedule(
                    conn,
                    project_id=project_id,
                    datastream_id=datastream_id,
                    cadence=body.get("cadence"),
                    window_days=body.get("window_days"),
                    window_offset_days=body.get("window_offset_days"),
                    next_run_at=body.get("next_run_at"),
                    # Story 57.8. The arrival hour crosses this seam like every
                    # other setting, so the console and the model write the same
                    # column rather than the console owning a fourth door.
                    # `"arrival_hour": null` is an ERASURE and `body.get` cannot
                    # tell it from an absent key -- the console announced
                    # "06:00 -> not set", the key was dropped, and the row kept
                    # 06:00 under a "Schedule saved."
                    arrival_hour=(
                        body["arrival_hour"] if "arrival_hour" in body else _schedule_unset()
                    ),
                    # Lot D1. An absent key means "leave it alone"; `true` and
                    # `false` are both decisions and both are written. No
                    # coercion -- `bool("false")` is `True`, and a string here
                    # would arm a Datastream a caller asked to stop, so
                    # `set_schedule` refuses anything that is not a boolean.
                    enabled=body.get("enabled"),
                    identity=identity,
                )
            else:
                result = read_schedule(
                    conn, project_id=project_id, datastream_id=datastream_id
                )
    except ValueError as exc:
        # The refusals schedule_mcp raises are ALL caller-fixable and each names
        # what to do (an unknown cadence, an absurd window, a datastream that was
        # never activated). Passing the message through is the point.
        return JSONResponse(
            {"code": "invalid_schedule", "message": str(exc)}, status_code=422
        )
    except Exception as exc:  # noqa: BLE001
        # Starting a paused Datastream consumes the org's trial allowance exactly
        # as creating one does, and the refusal carries its own count and limit.
        # Reported as the 409 every other surface gives it -- flattening it into
        # the 503 below would tell a person "the schedule is unavailable" about a
        # rule that names the number they are over.
        from core.trial_enforcement import TrialDatastreamLimitError  # noqa: PLC0415

        if isinstance(exc, TrialDatastreamLimitError):
            return JSONResponse(exc.to_dict(), status_code=409)
        logger.error("admin_api: datastream_schedule error ds=%s: %s", datastream_id, exc)
        return JSONResponse(
            {"code": "schedule_unavailable", "message": "Schedule is unavailable"},
            status_code=503,
        )

    if result is None:
        return JSONResponse(
            {"code": "not_found", "message": "Datastream not found"}, status_code=404
        )
    return JSONResponse(result)

async def _run_datastream(request: Request) -> Response:
    """POST /api/datastreams/{id}/run -- run this Datastream NOW, by hand.

    THE SAME RUN THE CLOCK WOULD HAVE STARTED. Whoever asks, a run passes the
    same gates and gets the same declaration (`core.datastream_dispatch`), and
    covers the same window (`core.pull_window`). Rewritten 2026-08-12, measured:

      * this door read `refetch_days` alone and `date.today()`, so a Datastream
        whose retrieval window says thirty days ran over three, on the
        deployment's calendar, ignoring the extraction offset of a lagging
        source -- three divergences from the schedule it claims to replay;
      * it enqueued straight into `queue.enqueue_pull` with NO gate, so a
        Datastream someone had deliberately STOPPED ran anyway and spent the
        source account's quota;
      * it opened no execution, so the run it started appeared on no screen.
        Measured on the live base: **0** rows of `app.pull_jobs` carry an
        `execution_id`;
      * and it refused every `versioned` Datastream with `dispatch_not_available`
        while the dispatchers select `schedule_mode IN ('nightly','weekly',
        'hourly')`. Measured: of 89 Datastreams, exactly ONE is active, enabled
        and mapped -- and its cadence is `manual`. No clock would ever pick it
        up and this door turned it away, so nothing in this build could collect
        at all. The refusal is retired; the gates below say what actually stops
        a run, by name.

    Body (JSON): {"project_id": str, "date_from": str?, "date_to": str?}
    An omitted window is the one the Datastream DECLARES, ending on the
    PROJECT's last complete day.
    Response (202): {"job_id", "pull_id", "state", "deduplicated"?, "execution_id"?}
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        422 -- a named gate refusal (why this Datastream will not run)
        500 -- DB/queue error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id is required in the body"},
            status_code=400,
        )

    from core import datastream_dispatch, pull_window, scheduler  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            row = datastream_dispatch.load_dispatch_row(conn, ds_id, project_id)
            if row is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                row["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role="member",
            )
            if scope_err is not None:
                return scope_err
            # Resolved HERE because both halves need the connection: what the
            # row declares, and the PROJECT's last complete day (AI-117). The
            # dispatch row carries the four window columns, so nothing is
            # re-queried.
            declared_window = pull_window.resolve_window(
                row,
                end_reference=scheduler.project_yesterday(
                    scheduler.project_timezone(conn, row["project_id"])
                ),
                cadence=row.get("schedule_mode"),
            )
    except Exception as exc:
        logger.error("admin_api: run_datastream_db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    # THE GATES the clock applies, applied to the hand. Each names the gesture
    # that repairs it, so "it will not run" is never the end of the sentence.
    refusal = datastream_dispatch.gate_refusal(row)
    if refusal is not None:
        return JSONResponse(
            {"code": refusal.code, "message": refusal.message}, status_code=422
        )

    # THE WINDOW: pinned by the caller, or the one the Datastream DECLARES. The
    # arbitration is not re-decided here -- it is CALLED (`core.pull_window`,
    # AI-46/AI-145/AI-217), like every other door.
    date_from = (body.get("date_from") or "").strip()
    date_to = (body.get("date_to") or "").strip()
    if not date_from or not date_to:
        interval = declared_window.as_interval()
        date_from, date_to = interval["date_from"], interval["date_to"]

    try:
        from core import queue  # noqa: PLC0415

        outcome = datastream_dispatch.dispatch_windows(
            get_connection,
            row=row,
            windows=[{"date_from": date_from, "date_to": date_to}],
            queue=queue,
            actor=identity or "anonymous",
            origin=datastream_dispatch.ORIGIN_MANUAL,
            run_key=f"{ds_id}:{date_from}:{date_to}",
        )
    except Exception as exc:
        logger.error("admin_api: run_datastream_enqueue_error: %s", exc)
        return JSONResponse(
            {"code": "queue_error", "message": f"Erreur de mise en file d'attente: {exc}"},
            status_code=500,
        )

    if not outcome.jobs:
        return JSONResponse(
            {
                "code": "queue_error",
                "message": "The run could not be queued. Try again in a moment.",
            },
            status_code=500,
        )

    job = dict(outcome.jobs[0])
    if outcome.execution_id:
        job["execution_id"] = outcome.execution_id
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_DATASTREAM_RUN,
        provider_account="",
        connection_ref=row["connection_ref_id"],
        metadata={
            "datastream_id": ds_id,
            "project_id": project_id,
            "job_id": job.get("job_id"),
            "execution_id": outcome.execution_id,
            "date_from": date_from,
            "date_to": date_to,
        },
    )
    return JSONResponse(job, status_code=202)

async def _preview_datastream_run(request: Request) -> Response:
    """GET /api/datastreams/{id}/run/preview -- what `POST …/run` would collect.

    A READ, AND IT WRITES NOTHING. Added 2026-08-18 for one reason: the surface
    requires that a confirmation name the connector, the account and the window
    BEFORE anything is spent, and until now the window was knowable only by
    running. A console that wanted to say "this asks for 2026-07-20 → 2026-08-17"
    had two options and both were wrong -- spend first and report after, or
    re-implement `pull_window.resolve_window` in TypeScript and let the two
    copies drift. `SchedulePanel` offers the cadence `manual`, "only runs when
    someone asks it to", and nothing in the console asked; this is what lets it
    ask honestly.

    IT DECIDES NOTHING OF ITS OWN. Every line below is the same call the POST
    makes, in the same order -- `load_dispatch_row`, then `resolve_window`
    against the PROJECT's last complete day, then `gate_refusal`. If this answers
    a window, that is the window; if it answers a refusal, the POST answers the
    same refusal with the same code and the same sentence. A second arbitration
    here would be a second answer to "which days", which is the defect
    `core.pull_window` exists to prevent.

    THE REFUSAL IS PART OF THE ANSWER, NOT AN ERROR. It comes back `200` with
    `refusal` populated, because "this Datastream will not run, and here is the
    gesture that releases it" is a successful reading. A `422` here would make
    the console treat a knowable, actionable state as a failed request.

    Query params: project_id (required)
    Response (200): {"date_from", "date_to", "window_days", "window_source",
                     "window_widened_for", "offset_days", "cadence",
                     "refusal": {"code", "message"} | null}
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        500 -- DB error

    Viewer-scoped: reading what a run WOULD cost must not require the right to
    start one, or the person who has to approve the spend cannot see it first.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    from core import datastream_dispatch, pull_window, scheduler  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            row = datastream_dispatch.load_dispatch_row(conn, ds_id, project_id)
            if row is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                row["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role="viewer",
            )
            if scope_err is not None:
                return scope_err
            declared_window = pull_window.resolve_window(
                row,
                end_reference=scheduler.project_yesterday(
                    scheduler.project_timezone(conn, row["project_id"])
                ),
                cadence=row.get("schedule_mode"),
            )
    except Exception as exc:
        logger.error("admin_api: run_preview_db_error ds=%s: %s", ds_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    refusal = datastream_dispatch.gate_refusal(row)
    return JSONResponse(
        {
            "datastream_id": ds_id,
            "project_id": project_id,
            "date_from": declared_window.date_from,
            "date_to": declared_window.date_to,
            "window_days": declared_window.length.days,
            # WHICH DECLARATION ANSWERED. `defensive_default` means the row
            # declares no window at all and three days is a fallback, not a
            # setting -- a person about to spend deserves to know which of those
            # they are looking at. Story 63.6's rule, applied to a window.
            "window_source": declared_window.length.source,
            "window_widened_for": declared_window.length.widened_for,
            "offset_days": declared_window.offset_days,
            "cadence": row.get("schedule_mode"),
            "refusal": (
                None
                if refusal is None
                else {"code": refusal.code, "message": refusal.message}
            ),
        }
    )


async def _get_datastream_ledger(request: Request) -> Response:
    """GET /api/datastreams/{id}/ledger -- extract ledger for a datastream.

    Query params:
        project_id  (required) -- project scope (AD-5)
        from        (optional) -- start date YYYY-MM-DD (default today - 35 days)
        to          (optional) -- end date YYYY-MM-DD (default yesterday)

    Response (200):
        {"ledger": [{date, status, row_count, expected_rows, completeness_ratio,
                     pull_id, loaded_at}, ...]}
        Ordered date ASC. One entry per calendar day in the window.

    Error responses:
        400 -- missing project_id, or invalid date format (French)
        401 -- unauthorized
        404 -- datastream not found or wrong project
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")
    project_id = request.query_params.get("project_id") or ""
    if not project_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "project_id est requis en parametre de requete",
            },
            status_code=400,
        )

    # Default window: last 35 days (yesterday back 35 days).
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    default_from = (date.today() - timedelta(days=_DEFAULT_LEDGER_DAYS)).isoformat()

    raw_from = (request.query_params.get("from") or default_from).strip()
    raw_to = (request.query_params.get("to") or yesterday).strip()

    if not _ISO_DATE_RE.match(raw_from):
        return JSONResponse(
            {
                "code": "invalid_date",
                "message": (
                    f"Invalid 'from' parameter (expected YYYY-MM-DD): {raw_from!r}"
                ),
            },
            status_code=400,
        )
    if not _ISO_DATE_RE.match(raw_to):
        return JSONResponse(
            {
                "code": "invalid_date",
                "message": (f"Invalid 'to' parameter (expected YYYY-MM-DD): {raw_to!r}"),
            },
            status_code=400,
        )

    try:
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.extract_ledger import get_extract_ledger  # noqa: PLC0415

        with get_connection() as conn:
            ds = get_datastream(ds_id, project_id, conn)
            if ds is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                ds["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
            )
            if scope_err is not None:
                return scope_err

            ledger = get_extract_ledger(ds_id, raw_from, raw_to, conn)
    except Exception as exc:
        logger.error("admin_api: get_datastream_ledger_error ds=%s: %s", ds_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    return JSONResponse({"ledger": ledger})

class RefetchRefused(Exception):
    """Un refus de re-collecte, avec le code et la phrase que les DEUX portes rendent.

    Story 67.23, 2026-08-22. `run_refetch` en dessous est le moteur, et il l'est
    devenu parce qu'il n'y en avait qu'un : la porte REST le portait dans son
    handler, et la porte MCP -- qui a exactement les memes pre-conditions, toutes
    deja verifiees (`operations_mcp.py:800-832` : versions, policy, fenetre
    bornee, exposition, quota, verrou) -- rendait `run_origins.NO_ENGINE` au
    dernier moment. Le registre declare pourtant `refetch` avec
    `has_engine=True` (`run_origins.py:93`), des deux cotes du fil
    (`runOrigins.ts:39`). L'audit l'appelle << le trou de parite le plus net
    entre les portes >>, et il avait raison : un modele ne pouvait pas re-collecter
    un jour qu'une personne re-collecte d'un clic.

    ECRIRE UN SECOND MOTEUR AURAIT ETE LE VRAI DEFAUT. Le refus que
    `operations_mcp` porte depuis la story 63.7 dit exactement pourquoi : la
    version d'avant mintait une execution et n'enfilait RIEN, l'execution restait
    en `created` -- un etat ACTIF -- et le flux perdait sa collecte recurrente
    pour de bon, d'un seul appel d'outil, en silence. Le seul moteur qui n'a pas
    ce defaut est celui de la porte REST, parce qu'il enfile ses fenetres et
    referme son run. Donc il est appele, pas recopie.
    """

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def expand_refetch_days(
    dates: object = None, date_from: object = None, date_to: object = None
) -> list[str]:
    """Le jour-a-jour d'une re-collecte, depuis une liste OU une plage.

    Une seule grammaire de dates pour les deux portes : la console envoie
    `dates`, l'outil MCP porte un intervalle `{from, to}` deja borne par
    `_bounded_interval`. Les deux arrivent ici et repartent en une liste de jours
    validee, ou en un `RefetchRefused` qui nomme le champ fautif.
    """
    days: list[str] = []
    if dates is not None:
        if not isinstance(dates, list):
            raise RefetchRefused(
                "invalid_field", "dates must be an array of dates", 400
            )
        days = [str(d).strip() for d in dates]
    elif date_from is not None or date_to is not None:
        raw_from = str(date_from or "").strip()
        raw_to = str(date_to or "").strip()
        if not raw_from or not raw_to:
            raise RefetchRefused(
                "missing_field",
                "from and to are required when dates is not supplied",
                400,
            )
        for label, raw in (("from", raw_from), ("to", raw_to)):
            if not _ISO_DATE_RE.match(raw):
                raise RefetchRefused(
                    "invalid_date",
                    f"'{label}' invalide (format YYYY-MM-DD) : {raw!r}",
                    400,
                )
        try:
            d_from = date.fromisoformat(raw_from)
            d_to = date.fromisoformat(raw_to)
        except Exception:
            raise RefetchRefused("invalid_date", "Dates invalides", 400) from None
        if d_to < d_from:
            raise RefetchRefused(
                "invalid_date", "'to' must be later than or equal to 'from'", 400
            )
        cur_d = d_from
        while cur_d <= d_to:
            days.append(cur_d.isoformat())
            cur_d += timedelta(days=1)
    else:
        raise RefetchRefused(
            "missing_field",
            "Fournir 'dates' (tableau) ou 'from'/'to' (plage de dates)",
            400,
        )

    if not days:
        raise RefetchRefused("missing_field", "No date selected", 400)
    for d_str in days:
        if not _ISO_DATE_RE.match(d_str):
            raise RefetchRefused(
                "invalid_date", f"Date invalide (format YYYY-MM-DD) : {d_str!r}", 400
            )
    if len(days) > MAX_REFETCH_DAYS:
        raise RefetchRefused(
            "too_many_dates",
            f"Maximum {MAX_REFETCH_DAYS} jours par requete de re-fetch",
            400,
        )
    return days


#: Le plafond d'une re-collecte, nomme une fois. Les deux portes le partagent,
#: donc un modele et une personne se heurtent au meme mur au meme jour.
MAX_REFETCH_DAYS = 365


def run_refetch(*, datastream_id: str, project_id: str, days: list[str], actor: str) -> dict:
    """Re-collecter les jours nommes. LE moteur, appele par les deux portes.

    La PORTEE N'EST PAS PROUVEE ICI, deliberement : chaque porte la prouve avec
    son propre instrument -- `_enforce_datastream_project_scope` cote REST,
    `_authorize_datastream` plus la comparaison d'organisation cote MCP -- et
    une troisieme preuve au milieu serait une troisieme reponse a la question
    << ce flux est-il a ce projet >>. Ce qui est ici est ce que les deux portes
    doivent faire IDENTIQUEMENT : les memes gates que l'horloge, les memes
    fenetres, la meme file, la meme ligne d'execution.

    Rend le meme payload que la route rendait : `{"jobs": [...], "execution_id"?,
    "active_run"?}`. Leve `RefetchRefused` pour tout refus.
    """
    from core import datastream_dispatch  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            ds = datastream_dispatch.load_dispatch_row(conn, datastream_id, project_id)
    except RefetchRefused:
        raise
    except Exception as exc:
        logger.error(
            "admin_api: refetch_datastream_db_error ds=%s: %s", datastream_id, exc
        )
        raise RefetchRefused("db_error", f"Erreur base de donnees: {exc}", 500) from None
    if ds is None:
        raise RefetchRefused("not_found", "Flux de donnees introuvable", 404)

    # LES MEMES GATES QUE L'HORLOGE, MOINS UN. `require_armed=False` : la surface
    # ratifiee dit qu'arreter un Datastream perd des jours qui << ne reviennent
    # que par une re-collecte jour par jour >>, donc cette porte reste ouverte
    # sur un flux arrete -- elle EST cette reparation.
    refusal = datastream_dispatch.gate_refusal(ds, require_armed=False)
    if refusal is not None:
        raise RefetchRefused(refusal.code, refusal.message, 422)

    try:
        windows = _group_dates_into_windows(days)
    except Exception as exc:
        raise RefetchRefused(
            "invalid_date", f"Erreur de calcul des fenetres: {exc}", 400
        ) from None

    connection_ref_id = ds["connection_ref_id"]
    subject = actor or "anonymous"
    jobs: list[dict] = []
    # Story 63.1 : une re-collecte est un run, et elle a sa ligne comme un autre.
    execution_id = _open_refetch_run(datastream_id, project_id, windows, subject)
    try:
        from core import queue  # noqa: PLC0415

        for win_from, win_to in windows:
            job = queue.enqueue_pull(
                connection_ref_id,
                win_from,
                win_to,
                requested_by=subject,
                datastream_id=datastream_id,
                execution_id=execution_id,
            )
            # AN ENTRY THAT IS NOT A JOB CARRIES NO JOB ID (story 58.4).
            queued = job.get("state") in _pull_job_states.BY_NAME
            job_entry = {
                "state": job.get("state"),
                "date_from": win_from,
                "date_to": win_to,
            }
            if queued:
                job_entry["job_id"] = job.get("job_id")
                job_entry["pull_id"] = job.get("pull_id")
            if job.get("deduplicated"):
                job_entry["deduplicated"] = True
            # Story 58.4 : la fenetre REELLEMENT enfilee, quand ce n'est pas
            # celle qui a ete demandee (plafond d'essai de l'org).
            if job.get("backfill_clamp"):
                job_entry["backfill_clamp"] = job["backfill_clamp"]
            # Story 58.4 : et le refus propre de la file, quand elle en ecrit un.
            for key in ("code", "message"):
                if job.get(key):
                    job_entry[key] = job[key]
            jobs.append(job_entry)

            write_audit_row(
                identity=subject,
                action=ACTION_DATASTREAM_RUN,
                provider_account="",
                connection_ref=connection_ref_id,
                metadata={
                    "datastream_id": datastream_id,
                    "project_id": project_id,
                    "job_id": job.get("job_id"),
                    "date_from": win_from,
                    "date_to": win_to,
                    "source": "refetch",
                },
            )
    except Exception as exc:
        logger.error(
            "admin_api: refetch_datastream_enqueue_error ds=%s: %s", datastream_id, exc
        )
        raise RefetchRefused(
            "queue_error", f"Erreur de mise en file d'attente: {exc}", 500
        ) from None

    # Story 63.1 : une re-collecte dont chaque fenetre etait deja en vol ne
    # possede aucun run, et un run laisse ouvert tiendrait
    # `uq_datastream_executions_active` et repondrait 409 a toute publication.
    _close_refetch_run(execution_id, subject)

    payload: dict = {"jobs": jobs}
    if execution_id:
        payload["execution_id"] = execution_id
    else:
        active = _read_active_run(datastream_id, project_id)
        if active is not None:
            payload["active_run"] = active
    return payload


async def _refetch_datastream(request: Request) -> Response:
    """POST …/datastreams/{datastream_id}/refetch -- re-collect the named days.

    THE ADDRESS IS PROJECT-SCOPED SINCE STORY 58.4, and there is only ONE of
    them. It used to be `/api/datastreams/{id}/refetch` with the project in the
    BODY; the project now travels in the path like every other read of this
    surface, and the old address is gone rather than kept beside it. It had a
    single console caller, so nothing forced two doors onto one gesture -- and
    two addresses for one gesture is what this lot refuses everywhere else. It
    is not a security repair: `_enforce_datastream_project_scope` proved the
    (stream, project) pair on the old address too.

    Body (JSON):
        {"dates": ["YYYY-MM-DD", ...]}                 -- explicit day list, OR
        {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}     -- range, expanded to days

    Contiguous date selections are grouped into minimal pull windows.
    Each window calls enqueue_pull() with datastream_id; dedup index applies.
    Writes ACTION_DATASTREAM_RUN per window.

    Response (202):
        {"jobs": [{job_id, pull_id, state, date_from, date_to, deduplicated?}, ...],
         "execution_id"?,
         "active_run"?: {execution_id, state, since, code, message}}

    `active_run` IS A FACT, NEVER A REFUSAL (Jean, 2026-08-06). A run already in
    flight holds `uq_datastream_executions_active`, so this re-collection gets no
    execution of its own -- the windows are still enqueued and still land. The
    answer says which run holds the Datastream so the console can say it out
    loud; refusing here would trade the collection for its instrumentation.

    NO COST TRAVELS ON THIS ANSWER. `core/quota.py` publishes `open|closed` and
    nothing else, its counter lives in a per-process singleton no route reaches,
    and how many provider requests one day costs is known only at the worker. A
    key carrying points, bytes or currency would be a figure nobody measured.

    Error responses:
        400 -- missing project_id, no dates, invalid dates
        401 -- unauthorized
        404 -- datastream not found or wrong project
        422 -- datastream has no connection_ref_id
        500 -- DB/queue error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("datastream_id", "")

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    # ONE SOURCE FOR THE PROJECT: the path. Reading it from the body as well
    # would give the same gesture two spellings and a rule about which one wins.
    project_id = str(request.path_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id est requis"},
            status_code=400,
        )

    # UNE SEULE GRAMMAIRE DE DATES, ET UN SEUL MOTEUR. Ce handler portait les
    # deux ; ils vivent maintenant dans `expand_refetch_days` et `run_refetch`
    # au-dessus, parce que la porte MCP a exactement les memes pre-conditions et
    # rendait `NO_ENGINE` faute d'avoir un moteur a appeler (story 67.23). Ce qui
    # reste ici est ce qui appartient a CETTE porte : son authentification, sa
    # preuve de portee, et la traduction d'un refus en code HTTP.
    try:
        days = expand_refetch_days(
            dates=body.get("dates") if "dates" in body else None,
            date_from=body.get("from") if ("from" in body or "to" in body) else None,
            date_to=body.get("to") if ("from" in body or "to" in body) else None,
        )
    except RefetchRefused as refused:
        return JSONResponse(refused.as_dict(), status_code=refused.status_code)

    # LA PORTEE, PROUVEE PAR CETTE PORTE. `run_refetch` ne la prouve pas : chaque
    # porte a son instrument, et une troisieme preuve au milieu serait une
    # troisieme reponse a la meme question.
    try:
        from core import datastream_dispatch  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            ds = datastream_dispatch.load_dispatch_row(conn, ds_id, project_id)
            if ds is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                ds["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role="member",
            )
            if scope_err is not None:
                return scope_err
    except Exception as exc:
        logger.error("admin_api: refetch_datastream_db_error ds=%s: %s", ds_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    try:
        payload = run_refetch(
            datastream_id=ds_id,
            project_id=project_id,
            days=days,
            actor=identity or "anonymous",
        )
    except RefetchRefused as refused:
        return JSONResponse(refused.as_dict(), status_code=refused.status_code)
    return JSONResponse(payload, status_code=202)

def _open_refetch_run(
    ds_id: str,
    project_id: str,
    windows: list[tuple[str, str]],
    actor: str,
) -> str | None:
    """Story 63.1: mint the execution a refetch's windows belong to, or None.

    None when a run is already in flight for this Datastream, or when the row has
    no active plan/mapping version. It is never a reason to refuse the refetch:
    the pull is the point, the progress line is the instrumentation.
    """
    if not windows:
        return None
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.execution_progress import open_collection_run  # noqa: PLC0415
        from core.run_origins import REFETCH  # noqa: PLC0415

        window_dicts = [{"date_from": f, "date_to": t} for f, t in windows]
        with get_connection() as conn:
            execution = open_collection_run(
                conn,
                datastream_id=ds_id,
                project_id=project_id,
                # Both resolved from app.datastreams by open_collection_run:
                # get_datastream does not project the mapping version, and
                # widening it for one caller would be the wrong repair.
                windows=window_dicts,
                actor=actor,
                idempotency_key=(
                    f"refetch:{ds_id}:{windows[0][0]}:{windows[-1][1]}:"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
                ),
                # Story 63.7: the registry's key, so the Workbench can say "Day
                # re-collection" rather than show an unexplained treatment.
                origin=REFETCH,
            )
            if execution is None:
                conn.rollback()
                return None
            conn.commit()
            return str(execution["id"])
    except Exception as exc:  # noqa: BLE001 -- instrumentation never blocks a pull
        logger.warning("admin_api: open_refetch_run_failed ds=%s: %s", ds_id, exc)
        return None

def _close_refetch_run(execution_id: str | None, actor: str) -> None:
    """Story 63.1: close a refetch run whose windows are all terminal (or absent)."""
    if not execution_id:
        return
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.execution_progress import (  # noqa: PLC0415
            close_collection_run_if_complete,
        )

        with get_connection() as conn:
            close_collection_run_if_complete(
                conn, execution_id=execution_id, actor=actor
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- instrumentation never blocks a pull
        logger.warning(
            "admin_api: close_refetch_run_failed execution=%s: %s", execution_id, exc
        )

def _read_active_run(ds_id: str, project_id: str) -> dict | None:
    """The run holding this Datastream, on its own connection. Never raises."""
    try:
        from core.datastream_active_run import read_active_run  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            return read_active_run(conn, datastream_id=ds_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 -- annotation never blocks a pull
        logger.warning("admin_api: read_active_run_failed ds=%s: %s", ds_id, exc)
        return None

def _group_dates_into_windows(dates: list[str]) -> list[tuple[str, str]]:
    """Group a sorted list of YYYY-MM-DD strings into contiguous (from, to) windows.

    Example: ["2026-07-01", "2026-07-02", "2026-07-04"] ->
             [("2026-07-01", "2026-07-02"), ("2026-07-04", "2026-07-04")]
    """
    if not dates:
        return []

    parsed = sorted({date.fromisoformat(d) for d in dates})
    windows: list[tuple[str, str]] = []
    window_start = parsed[0]
    window_end = parsed[0]

    for d in parsed[1:]:
        if d == window_end + timedelta(days=1):
            window_end = d
        else:
            windows.append((window_start.isoformat(), window_end.isoformat()))
            window_start = d
            window_end = d

    windows.append((window_start.isoformat(), window_end.isoformat()))
    return windows


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : meme
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

DATASTREAM_SCHEDULE_ROUTES = [
    # AI-119: the console's door onto the schedule. Same row as the MCP tools.
    Route(
        "/api/datastreams/{id}/schedule",
        endpoint=_datastream_schedule,
        methods=["GET", "PUT"],
    ),
]

DATASTREAM_COLLECTION_ROUTES = [
    # BEFORE `/run`, and that is not a preference. Starlette resolves in
    # declaration order and `/api/datastreams/{id}/run` cannot swallow
    # `/api/datastreams/{id}/run/preview` -- different segment counts -- but the
    # rule this file's header states is that a longer literal path is declared
    # first, so the ordering never depends on remembering that.
    Route(
        "/api/datastreams/{id}/run/preview",
        endpoint=_preview_datastream_run,
        methods=["GET"],
    ),
    Route(
        "/api/datastreams/{id}/run",
        endpoint=_run_datastream,
        methods=["POST"],
    ),
    # Story 8.3: extract ledger + refetch endpoints.
    Route(
        "/api/datastreams/{id}/ledger",
        endpoint=_get_datastream_ledger,
        methods=["GET"],
    ),
    # Story 58.4: the re-collection is project-scoped like the rest of this
    # surface, and the un-scoped address is GONE rather than kept beside it.
    # One gesture, one door -- `REFETCH_ROUTE_PATH` is what the test asserts
    # is mounted, so the path exists once in this repository.
    Route(
        REFETCH_ROUTE_PATH,
        endpoint=_refetch_datastream,
        methods=["POST"],
    ),
]
