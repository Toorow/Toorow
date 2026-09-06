"""The server-minted handle an append-only OBSERVATION must carry.

WHY THIS MODULE EXISTS. `mcp-tool-surface.md` clause 8 says a tool that mutates
must not be reachable under `insights`. Two tools were declared `effect="read"`
under `insights` and appended a row anyway -- `submit_feedback` and
`app_record_evidence_inspection`. The declaration was defended by a real
argument (an append-only observation transitions no DOMAIN state, so AD-24's
`effect` still reads `read`), and the argument holds -- but only under a
condition that nothing enforced: **the append must be impossible for a caller
who merely knows the tool's name**. Both accepted their arguments on the
caller's word, so the app-only discovery filter was the whole guard, and a
discovery filter is not a guard: `mcp-tool-surface.md` says so itself
("visibility metadata is not authorization").

WHAT THE CONDITION IS. The same grant `app_read_result_manifest` and
`app_read_result_slice` already require: a row of `app.result_app_grants`
(migration 161), minted server-side by the render path, delivered to the widget
in result `_meta` -- a channel the model does not read -- and never spoken in a
tool answer. `core.result_slices.resolve_handle` is the one verifier and
`core.result_app_grants.touch_handle` the one consumer; neither is re-derived
here. This module adds exactly two things the observation writers need and the
slice readers do not:

  * a refusal that NAMES THE GESTURE. `resolve_handle` raises the deliberately
    mute `not_found` envelope, which is right for a reader probing for a
    Result's existence and useless to a widget that simply forgot to forward
    the handle it was given.
  * the BINDING check. A grant carries the `result_id` it was issued over, so an
    observation that names a result is refused when the handle was issued for a
    different one -- an append is only possible against a handle the server
    issued for that very result.

NON-DISCLOSURE IS PRESERVED. Absent, foreign, revoked, expired and idle all
converge on ONE code and ONE sentence, exactly as `resolve_handle` makes them
converge. The sentence names a gesture, and the gesture is the same in every
one of those cases, so naming it distinguishes nothing. Only two refusals stand
apart, and neither says anything about the server's data: a handle the caller
did not send at all, and a handle whose result disagrees with the result the
caller itself named.

ASCII-only, English copy, lazy `core.*` imports (no cycle with `core.main`).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The one gesture that produces a handle. Named in every refusal below, because
#: a widget that meets `not_found` learns nothing it can act on.
_GESTURE = (
    "Read the Result first: call analyze_result or render_analyze_result, then "
    "pass the result_handle it returns in _meta."
)

#: Missing entirely -- the caller's own shape, not a fact about stored data.
CODE_REQUIRED = "result_handle_required"

#: Absent, foreign, revoked, expired, idle. ONE code, ONE sentence, on purpose.
CODE_NOT_USABLE = "result_handle_not_usable"

#: The handle is live and the caller's, and it was issued over another Result.
CODE_WRONG_RESULT = "result_handle_names_another_result"


class ObservationHandleRefused(Exception):
    """An observation could not be attached to a grant. Carries a safe code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def require_observation_handle(handle: Any) -> str:
    """Return the trimmed handle, or refuse with the gesture that produces one.

    Separate from :func:`resolve_observation_handle` so a tool can refuse an
    empty handle BEFORE it opens a connection: a handle the caller never sent is
    a fact about the caller's own payload, so answering it early discloses
    nothing and spends nothing.
    """
    if not isinstance(handle, str) or not handle.strip():
        raise ObservationHandleRefused(
            CODE_REQUIRED,
            "This observation must name the Result it was made on. " + _GESTURE,
        )
    return handle.strip()


def resolve_observation_handle(
    conn,
    *,
    handle: Any,
    identity: str,
    project_id: str,
    result_ref: Any = None,
) -> dict[str, Any]:
    """Verify *handle* for this caller and Project, and return the live grant.

    The caller's access to the Project is resolved by `resolve_handle` BEFORE
    the grant row is loaded, and the grant can only narrow that decision -- the
    order `core.result_slices` states as its contract, inherited rather than
    restated.

    `result_ref`, when the caller supplies one, must be the `result_id` the
    grant was issued over. An empty ref is not a mismatch: the grant then names
    the Result, which is the stronger direction.
    """
    from core.result_slices import SliceRefused, resolve_handle  # noqa: PLC0415

    handle_id = require_observation_handle(handle)
    try:
        grant = resolve_handle(
            conn, handle_id=handle_id, identity=identity, project_id=project_id
        )
    except SliceRefused as exc:
        # `resolve_handle` already collapsed absent / foreign / revoked /
        # expired / denied into one envelope. It is re-labelled, never split.
        logger.info("app_observation_handle: handle refused (%s)", exc.code)
        raise ObservationHandleRefused(
            CODE_NOT_USABLE,
            "This handle cannot be used for an observation. " + _GESTURE,
        ) from exc
    if isinstance(result_ref, str) and result_ref.strip():
        if result_ref.strip() != grant.get("result_id"):
            raise ObservationHandleRefused(
                CODE_WRONG_RESULT,
                "The handle was issued over another Result. " + _GESTURE,
            )
    return grant


def consume_observation_handle(conn, *, handle: str) -> None:
    """Record the observation against the grant, or refuse it.

    The write on `app.result_app_grants` lives in `core.result_app_grants` and
    nowhere else -- the reason `touch_handle` was put there rather than in the
    reader is that a reader which can write is a reader whose read cannot be
    proved side-effect free. The same discipline applies here: this module
    verifies and delegates, it issues no SQL of its own.

    A handle that stopped being live between the resolve and this call (revoked
    in another session, or idled out) refuses with the same sentence, so the
    observation and its grant commit together or not at all.
    """
    from core.result_app_grants import touch_handle  # noqa: PLC0415

    if not touch_handle(conn, handle_id=handle.strip()):
        raise ObservationHandleRefused(
            CODE_NOT_USABLE,
            "This handle cannot be used for an observation. " + _GESTURE,
        )
