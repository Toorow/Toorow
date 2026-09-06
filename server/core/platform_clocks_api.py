"""The platform clocks, on the REST surface -- the console half of the same model.

Five routes, the exact counterparts of the five MCP tools in
`core/platform_clocks_mcp.py`, plus one that has no MCP counterpart yet (see the
last entry):

  GET   /api/platform/clocks                     -- every clock: declared,
        observed, drift verdict.
  GET   /api/platform/clocks/{clock_name}        -- one clock, same model.
  PATCH /api/platform/clocks/{clock_name}        -- edit the DECLARED cadence.
  POST  /api/platform/clocks/{clock_name}/apply  -- push the declared clock into
        GCP (create/update/pause/resume), idempotent.
  POST  /api/platform/clocks/{clock_name}/run    -- fire one clock immediately.
  GET   /api/platform/nightly-steps              -- last night's nightly STEPS:
        each declared step, its outcome, its duration, and the two absences a
        step can leave (never started, never recorded). `Incomplete if` 2 asks
        this one level below the clocks: the beat fired, and then what? Outside
        the `/clocks/` prefix on purpose -- under it, `nightly-steps` is a valid
        clock NAME.

PAS DE CONTREPARTIE MCP, ET C'EST LA REGLE PLUTOT QU'UN TROU (Jean, 2026-09-06 ;
cet en-tete disait l'inverse -- « it has no MCP counterpart yet; that is a gap,
not a decision » -- sans l'avoir mesure). Les cinq outils clocks existent parce
qu'un verdict de derive est une CONFIGURATION sur laquelle on agit
(`apply_platform_clock`). Les pas de la nuit sont une histoire d'EXECUTION de la
plateforme, et un agent travaille dans un Projet : la question qu'il se pose
vraiment -- « pourquoi les donnees d'hier manquent-elles ? » -- a deja ses portes
a la bonne echelle (`datastream_diagnose`, `datastream_pull_history`,
`list_datastream_runs`), et l'enveloppe qu'il recoit porte deja `stale_since`.
Lui faire lire le dispatch de la plateforme pour expliquer les donnees d'un
Projet serait l'inverse de l'isolement que le produit tient partout ailleurs. Un
outil de plus coute en outre un budget que Jean a du deplacer a la main pour en
ouvrir UN (130 -> 131, mcp-tool-surface.md).

BOTH SURFACES SERIALISE `core/platform_clocks_read_model.py` AND NEITHER RESHAPES
IT. That is the rule `inbound_health_api` states in its own header, for the same
reason: a handler that reshapes is a handler that can describe the same clock
differently from the other surface, and two answers to "is it in sync?" is the
same as none. Nothing below adds a field of its own to the read model.

READS NEVER REPAIR. The two GETs compose `list_declared` + `observe` +
`reconcile`. Drift is rendered and it stays until somebody POSTs `/apply`.

GATING. These clocks have NO organization in scope: there is no project to be a
member of, so `_enforce_org_manage` has nothing to check. The gate is the
platform allow-list -- the same `admin_api._enforce_platform_admin` every other
no-organization act uses, delegated rather than re-implemented so the two cannot
drift apart. It is imported lazily inside the guard because `admin_api` imports
these routes at module load; a top-level import would be a cycle. If that import
fails for any reason the request is refused. Fail-closed: an authorization seam
that could not run has not granted anything.

Refusal is 404, not 403, exactly like `_enforce_platform_admin`: a caller outside
the allow-list is told nothing about what exists here.

THE THREE WRITES REQUIRE AN EXPLICIT CONFIRMATION. A bounded `Idempotency-Key`
header (the convention `inbound_health_api` already uses for scan recovery), plus
a body that echoes the clock name in `confirm`. Firing a clock cannot be recalled
once dispatched, and repointing one changes what the whole platform does at a
given minute -- neither is something a mis-routed request should be able to do by
arriving.

No production identifier appears here: project and region are read from the
environment by the read model, and clock names are opaque data.
"""

from __future__ import annotations

import hashlib
import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_NOT_FOUND: dict = {"code": "not_found", "message": "Not found"}

#: An Idempotency-Key longer than this is refused rather than stored.
_MAX_IDEMPOTENCY_KEY = 200


def _clock_operation(
    conn,
    *,
    command: str,
    clock_name: str,
    actor: str,
    key: str,
    payload: dict,
    mutation,
):
    """Persist and replay a platform-clock command, including the exact response."""
    from core.operations import MutationResult, OperationSpec, execute_operation  # noqa: PLC0415

    spec = OperationSpec(
        command_type=command,
        actor=actor,
        effective_org_id=None,
        resource_path=("platform", "clocks", clock_name),
        idempotency_key=key,
        host_context={"host": "rest", "workspace_id": "platform-console"},
        versions={"policy": "v1", "catalog": "v1", "tool": "rest-v1"},
        request_payload=payload,
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"clock:{clock_name}",
        trace_id=None,
    )

    def apply(operation_conn, _operation_id):
        result = mutation(operation_conn)
        digest = hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=digest,
            result=result,
            outbox_payload={"command": command, "clock_name": clock_name},
        )

    return execute_operation(conn, spec, mutation=apply).result


def _no_store(response: Response) -> Response:
    """A clock's state is never cacheable: it is read to decide something now."""
    response.headers["Cache-Control"] = "no-store"
    return response


async def _authorize(request: Request, operation: str) -> tuple[Response | None, str]:
    """Authenticate, then require the PLATFORM allow-list. Returns (refusal, identity).

    Delegates to `admin_api._enforce_platform_admin` rather than re-deriving the
    rule: one allow-list, one audit row, one refusal shape. The import is lazy
    because `admin_api` splices these routes at module load.
    """
    from core.api_auth import authenticate_api_request  # noqa: PLC0415

    authorized, identity = await authenticate_api_request(request)
    if not authorized:
        return (
            _no_store(
                JSONResponse(
                    {"code": "unauthorized", "message": "Bearer token required"},
                    status_code=401,
                )
            ),
            "anonymous",
        )
    try:
        from core.admin_api import _enforce_platform_admin  # noqa: PLC0415

        denied = await _enforce_platform_admin(request, identity, operation)
    except Exception as exc:  # noqa: BLE001 -- fail closed, never unguarded.
        logger.error(
            "platform_clocks_api: platform role check failed op=%s: %s",
            operation,
            type(exc).__name__,
        )
        return _no_store(JSONResponse(_NOT_FOUND, status_code=404)), identity
    if denied is not None:
        return _no_store(denied), identity
    return None, identity


def _checked_clock_name(request: Request) -> str | None:
    from core.platform_clocks_read_model import valid_clock_name  # noqa: PLC0415

    return valid_clock_name(request.path_params.get("clock_name"))


async def _confirmation(request: Request, clock_name: str) -> Response | None:
    """Refuse a write that carries no bounded idempotency key or no echoed target."""
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key or len(idempotency_key) > _MAX_IDEMPOTENCY_KEY:
        return _no_store(
            JSONResponse(
                {
                    "code": "invalid_idempotency_key",
                    "message": "A bounded Idempotency-Key header is required",
                },
                status_code=400,
            )
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a malformed body is the caller's mistake.
        body = None
    if not isinstance(body, dict) or (body.get("confirm") or "") != clock_name:
        return _no_store(
            JSONResponse(
                {
                    "code": "confirmation_required",
                    "message": "Repeat the clock name in `confirm` to authorize this act",
                },
                status_code=400,
            )
        )
    return None


def _seam_failure(operation: str, exc: Exception) -> Response:
    from core.platform_clocks_read_model import (  # noqa: PLC0415
        PlatformClockSeamError,
    )

    logger.error(
        "platform_clocks_api: %s failed: %s", operation, type(exc).__name__
    )
    if isinstance(exc, PlatformClockSeamError):
        return _no_store(
            JSONResponse(
                {"code": "seam_unavailable", "message": "Clock surface unavailable"},
                status_code=503,
            )
        )
    return _no_store(
        JSONResponse(
            {"code": "server_error", "message": "Clock surface unavailable"},
            status_code=500,
        )
    )


# ---------------------------------------------------------------------------
# GET /api/platform/clocks  and  GET /api/platform/clocks/{clock_name}
# ---------------------------------------------------------------------------


async def _list_clocks(request: Request) -> Response:
    """Every platform clock: declared, observed, drift verdict. Repairs nothing."""
    refusal, _identity = await _authorize(request, "list_platform_clocks")
    if refusal is not None:
        return refusal

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.platform_clocks_read_model import (  # noqa: PLC0415
            collect_clock_states,
        )

        with get_connection() as conn:
            state = collect_clock_states(conn)
    except Exception as exc:  # noqa: BLE001
        return _seam_failure("list", exc)

    return _no_store(JSONResponse(state, status_code=200))


async def _get_clock(request: Request) -> Response:
    """One platform clock, same read model. Unknown clock -> 404."""
    refusal, _identity = await _authorize(request, "get_platform_clock")
    if refusal is not None:
        return refusal

    clock_name = _checked_clock_name(request)
    if clock_name is None:
        return _no_store(JSONResponse(_NOT_FOUND, status_code=404))

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.platform_clocks_read_model import (  # noqa: PLC0415
            collect_clock_states,
        )

        with get_connection() as conn:
            state = collect_clock_states(conn, clock_name=clock_name)
    except Exception as exc:  # noqa: BLE001
        return _seam_failure("get", exc)

    if not state.get("clocks"):
        return _no_store(JSONResponse(_NOT_FOUND, status_code=404))
    return _no_store(JSONResponse({**state, "clock": state["clocks"][0]}, status_code=200))


# ---------------------------------------------------------------------------
# PATCH /api/platform/clocks/{clock_name} -- edit the DECLARED cadence
# ---------------------------------------------------------------------------


async def _patch_clock(request: Request) -> Response:
    """Edit the declared cadence. GCP is untouched until /apply is posted.

    The response therefore reads `drifted` afterwards, and that is correct: the
    declaration and the running infrastructure are two facts.
    """
    refusal, identity = await _authorize(request, "set_platform_clock_cadence")
    if refusal is not None:
        return refusal

    clock_name = _checked_clock_name(request)
    if clock_name is None:
        return _no_store(JSONResponse(_NOT_FOUND, status_code=404))

    not_confirmed = await _confirmation(request, clock_name)
    if not_confirmed is not None:
        return not_confirmed

    body = await request.json()
    schedule = body.get("schedule")
    timezone = body.get("timezone")
    paused = body.get("paused")
    if schedule is None and timezone is None and paused is None:
        return _no_store(
            JSONResponse(
                {
                    "code": "invalid_param",
                    "message": "Nothing to change: give schedule, timezone or paused",
                },
                status_code=400,
            )
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.platform_clocks_read_model import (  # noqa: PLC0415
            set_declared_cadence,
        )

        with get_connection() as conn:
            result = _clock_operation(
                conn,
                command="platform_clock.cadence_set",
                clock_name=clock_name,
                actor=identity,
                key=(request.headers.get("Idempotency-Key") or "").strip(),
                payload={"schedule": schedule, "timezone": timezone, "paused": paused},
                mutation=lambda operation_conn: set_declared_cadence(
                    operation_conn,
                    clock_name=clock_name,
                    actor=identity,
                    schedule=schedule,
                    timezone=timezone,
                    paused=paused,
                ),
            )
            conn.commit()
    except ValueError as exc:
        return _no_store(
            JSONResponse(
                {"code": "invalid_cadence", "message": str(exc)}, status_code=400
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _seam_failure("patch", exc)

    return _no_store(JSONResponse(result, status_code=200))


# ---------------------------------------------------------------------------
# POST .../apply  and  POST .../run
# ---------------------------------------------------------------------------


async def _apply_clock(request: Request) -> Response:
    """Push the declared clock into GCP. Idempotent; returns the resulting state."""
    refusal, identity = await _authorize(request, "apply_platform_clock")
    if refusal is not None:
        return refusal

    clock_name = _checked_clock_name(request)
    if clock_name is None:
        return _no_store(JSONResponse(_NOT_FOUND, status_code=404))

    not_confirmed = await _confirmation(request, clock_name)
    if not_confirmed is not None:
        return not_confirmed

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.platform_clocks_read_model import apply_declared  # noqa: PLC0415

        with get_connection() as conn:
            result = _clock_operation(
                conn,
                command="platform_clock.applied",
                clock_name=clock_name,
                actor=identity,
                key=(request.headers.get("Idempotency-Key") or "").strip(),
                payload={},
                mutation=lambda operation_conn: apply_declared(
                    operation_conn, clock_name=clock_name, actor=identity
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        return _seam_failure("apply", exc)

    return _no_store(JSONResponse(result, status_code=200))


async def _run_clock(request: Request) -> Response:
    """Fire one clock immediately. Not undoable once dispatched -- hence 202."""
    refusal, identity = await _authorize(request, "run_platform_clock_now")
    if refusal is not None:
        return refusal

    clock_name = _checked_clock_name(request)
    if clock_name is None:
        return _no_store(JSONResponse(_NOT_FOUND, status_code=404))

    not_confirmed = await _confirmation(request, clock_name)
    if not_confirmed is not None:
        return not_confirmed

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.platform_clocks_read_model import run_clock_now  # noqa: PLC0415

        with get_connection() as conn:
            result = _clock_operation(
                conn,
                command="platform_clock.run_now",
                clock_name=clock_name,
                actor=identity,
                key=(request.headers.get("Idempotency-Key") or "").strip(),
                payload={},
                mutation=lambda operation_conn: run_clock_now(
                    operation_conn, clock_name=clock_name, actor=identity
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        return _seam_failure("run", exc)

    return _no_store(JSONResponse(result, status_code=202))


# ---------------------------------------------------------------------------
# GET /api/platform/nightly-steps -- the third locus of `Incomplete if` 2
# ---------------------------------------------------------------------------


async def _nightly_steps(request: Request) -> Response:
    """Last night's nightly steps: each step, its outcome, its duration.

    A SIBLING OF THE CLOCKS, NOT A CHILD OF ONE. The path is
    `/api/platform/nightly-steps`, deliberately outside `/api/platform/clocks/`:
    under that prefix it would be indistinguishable from a clock NAME
    (`_checked_clock_name` accepts `nightly-steps`), and a route that only works
    because it was declared before its neighbour is a route the next reordering
    breaks silently.

    Same gate, same read-only posture, same no-store as the clock routes: this
    answers "did the work inside the beat happen?", one level below "did the beat
    fire?", and nothing here re-dispatches a step.
    """
    refusal, _identity = await _authorize(request, "list_nightly_steps")
    if refusal is not None:
        return refusal

    raw_nights = (request.query_params.get("nights") or "").strip()
    try:
        nights = int(raw_nights) if raw_nights else 1
    except ValueError:
        return _no_store(
            JSONResponse(
                {"code": "invalid_param", "message": "nights must be a whole number"},
                status_code=400,
            )
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.platform_clocks_read_model import (  # noqa: PLC0415
            collect_nightly_step_runs,
        )

        with get_connection() as conn:
            state = collect_nightly_step_runs(conn, nights=nights)
    except Exception as exc:  # noqa: BLE001
        return _seam_failure("nightly_steps", exc)

    return _no_store(JSONResponse(state, status_code=200))


PLATFORM_CLOCK_ROUTES: list[Route] = [
    # More-specific paths first; Starlette resolves in declaration order.
    Route(
        "/api/platform/clocks/{clock_name}/apply",
        endpoint=_apply_clock,
        methods=["POST"],
        name="platform-clock-apply",
    ),
    Route(
        "/api/platform/clocks/{clock_name}/run",
        endpoint=_run_clock,
        methods=["POST"],
        name="platform-clock-run",
    ),
    Route(
        "/api/platform/clocks/{clock_name}",
        endpoint=_get_clock,
        methods=["GET"],
        name="platform-clock-read",
    ),
    Route(
        "/api/platform/clocks/{clock_name}",
        endpoint=_patch_clock,
        methods=["PATCH"],
        name="platform-clock-cadence",
    ),
    Route(
        "/api/platform/clocks",
        endpoint=_list_clocks,
        methods=["GET"],
        name="platform-clocks-list",
    ),
    Route(
        "/api/platform/nightly-steps",
        endpoint=_nightly_steps,
        methods=["GET"],
        name="platform-nightly-steps",
    ),
]
