"""Pub/Sub push targets — the subscribers to `pull.landed`. Story 56.7 (AD-36).

WHAT THIS REPLACES. Everything downstream of a pull was called BY NAME, inside
the worker, wrapped in a `try/except` that turned its failure into one `WARNING`
line on a log stream shared with two other loops. Three consequences, and only
the first is obvious:

  * a failure was a log line, not a retryable delivery;
  * a slow consumer made the pull itself slower, because it ran in the pull's
    own invocation;
  * a NEW consumer could not exist without editing the producer.

One endpoint per consumer, one Pub/Sub subscription each on the same topic. That
is the AD-36 unit: one invocation, one job, one status.

AT-LEAST-ONCE IS THE CONTRACT, NOT AN ACCIDENT. Pub/Sub redelivers. Each
subscriber here is idempotent for a reason it can name:

  * verification writes one row per `pull_id` under a UNIQUE constraint, so a
    redelivery is refused by the database rather than duplicated -- and this
    module reports that refusal as DONE, not as an error to retry forever;
  * the context seed is declared best-effort and re-derives what it writes.

WHY THE INLINE CALLS STAY UNTIL THE TOPIC EXISTS. A deployment without
`PUBSUB_TOPIC` has no subscriber, so unplugging the producer's inline calls there
would silently stop verifying pulls. `facts_are_delivered()` is the single place
that decides, and the worker asks it rather than guessing.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


def facts_are_delivered() -> bool:
    """True when a published fact actually reaches a subscriber.

    Both halves are required. A topic without the push backend means nothing
    dispatches the tasks these subscribers are peers of; the backend without a
    topic means the fact is recorded and never leaves. In either case the
    producer must keep calling its consumers in-band, because the alternative is
    a pull that is never verified and nothing saying so.
    """
    return bool(
        os.environ.get("PUBSUB_TOPIC", "").strip()
        and os.environ.get("QUEUE_BACKEND", "local") == "cloud_tasks"
    )


def _decode_push(body: bytes) -> dict | None:
    """Return the fact payload from a Pub/Sub push envelope, or None.

    The envelope wraps the payload in base64 under `message.data`. A malformed
    delivery is NOT retryable -- redelivering the same broken bytes produces the
    same result forever -- so the caller answers 200 and drops it, having logged
    it. That is the one case where dropping is right.
    """
    try:
        envelope = json.loads(body or b"{}")
    except Exception:
        return None
    message = envelope.get("message") if isinstance(envelope, dict) else None
    if not isinstance(message, dict):
        return None
    data = message.get("data")
    if not isinstance(data, str):
        return None
    try:
        return json.loads(base64.b64decode(data).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


async def _authorize(request: Request):
    from core.admin_api import _authorize_internal  # noqa: PLC0415

    return await _authorize_internal(request)


async def _consume(request: Request, handler, name: str) -> Response:
    """Shared delivery frame: decode, run, and map the outcome to a status code.

    The status code is the instruction Pub/Sub reads, exactly as it is for Cloud
    Tasks: 200 stops the delivery, 5xx brings it back.
    """
    err = await _authorize(request)
    if err is not None:
        return err

    payload = _decode_push(await request.body())
    if payload is None:
        logger.warning("facts: undecodable delivery on %s -- dropped", name)
        return JSONResponse(
            {"code": "undecodable", "consumer": name, "dropped": True}, status_code=200
        )

    pull_id = str(payload.get("pull_id") or "")
    if not pull_id:
        logger.warning("facts: %s received a fact with no pull_id -- dropped", name)
        return JSONResponse(
            {"code": "no_pull_id", "consumer": name, "dropped": True}, status_code=200
        )

    try:
        outcome = handler(payload)
    except Exception as exc:  # noqa: BLE001 -- transient by assumption; Pub/Sub retries
        logger.exception("facts: %s failed pull_id=%s: %s", name, pull_id, exc)
        return JSONResponse(
            {"code": "consumer_failed", "consumer": name, "pull_id": pull_id},
            status_code=503,
        )
    return JSONResponse({"consumer": name, "pull_id": pull_id, "outcome": outcome}, status_code=200)


def run_verification_for(payload: dict) -> str:
    """Verify the pull the fact describes. Idempotent by the UNIQUE on pull_id."""
    from core.context_events import resolve_landing  # noqa: PLC0415

    # `_get_manifest_for_module` : la fonction a ete renommee et son SEUL
    # appelant ne l'a pas suivie, donc la verification de chaque pull levait
    # ImportError -- trois fois par run, en silence, derriere un except.
    from core.queue import _get_manifest_for_module  # noqa: PLC0415
    from core.verification import run_post_pull_verification  # noqa: PLC0415

    module = str(payload.get("module") or "")
    manifest = _get_manifest_for_module(module) or {}
    profiles = manifest.get("report_profiles") or [{}]

    # AI-302: THE PULL'S OWN PROFILE, not `report_profiles[0]`.
    #
    # `queue.py` passes `report_profile_id` to the very same function; THIS
    # caller did not, and the repair of 2026-08-12 landed on one of the two.
    # Whenever delivery is delegated it is this subscriber that verifies, so the
    # profile was lost exactly where it is load-bearing: `_count_raw_rows` uses
    # it to resolve WHICH relation the pull landed in, and without it falls back
    # to the module's single registered table -- empty for youtube-analytics.
    # The count then returned 0, the verdict was `empty`, and `empty` raises a
    # STICKY `populate_failed` that turns every later enqueue of the whole
    # authorization into `access_denied`. Measured on production 2026-08-17: a
    # pull of 344 rows, already readable seven seconds before the verdict, filed
    # empty -- and nine Datastreams denied behind it. The same shape closed the
    # door on 2026-08-12 and cost five days.
    #
    # Resolved from the Datastream when the fact predates the field, so a fact
    # already in flight is verified correctly rather than counted against the
    # wrong relation.
    profile_id = str(payload.get("report_profile_id") or "")
    if not profile_id and payload.get("datastream_id"):
        from core.db import get_connection  # noqa: PLC0415
        from core.queue import _resolve_datastream_profile  # noqa: PLC0415

        try:
            with get_connection() as conn:
                profile_id = _resolve_datastream_profile(conn, str(payload["datastream_id"])) or ""
        except Exception as exc:  # noqa: BLE001 -- verification never raises
            logger.warning(
                "facts: profile_resolve_failed pull_id=%s: %s", payload.get("pull_id"), exc
            )

    profile = next((p for p in profiles if p.get("id") == profile_id), None)
    if profile is None:
        profile = profiles[0] if profiles else {}
    if resolve_landing(profile, manifest.get("module_kind")) == "context_events":
        # A profile that lands in app.context_events has no raw table to count.
        return "skipped_context_events_landing"

    run_post_pull_verification(
        pull_id=str(payload["pull_id"]),
        connection_ref_id=str(payload.get("connection_ref_id") or ""),
        date_from=str(payload.get("date_from") or ""),
        date_to=str(payload.get("date_to") or ""),
        manifest=manifest,
        provider=module,
        project_id=str(payload.get("project_id") or "") or None,
        report_profile_id=profile_id or None,
    )
    return "verified"


def run_context_seed_for(payload: dict) -> str:
    """Seed day-0 context for the module that just landed."""
    from core.context_seed import seed_project_context_best_effort  # noqa: PLC0415

    project_id = str(payload.get("project_id") or "")
    module = str(payload.get("module") or "")
    if not project_id or not module:
        return "skipped_incomplete_scope"
    seed_project_context_best_effort(project_id, module_names=[module], ensure_schema=False)
    return "seeded"


async def _verification_push(request: Request) -> Response:
    """POST /internal/facts/pull-landed/verification"""
    return await _consume(request, run_verification_for, "verification")


async def _context_seed_push(request: Request) -> Response:
    """POST /internal/facts/pull-landed/context-seed"""
    return await _consume(request, run_context_seed_for, "context-seed")


FACT_ROUTES = [
    Route(
        "/internal/facts/pull-landed/verification", endpoint=_verification_push, methods=["POST"]
    ),
    Route(
        "/internal/facts/pull-landed/context-seed", endpoint=_context_seed_push, methods=["POST"]
    ),
]
