"""Story 50.7 -- the Render Share domain: one frozen Render, one revocable grant.

WHAT THIS MODULE OWNS, and the boundary is deliberate: bearer minting, peppered
HMAC hashing, the atomic single-use exchange, session validation, revocation,
expiry and the append of access evidence. No HTTP, no HTML, no route, and no SQL
outside its own four tables (`app.render_shares`,
`app.render_share_exchange_sessions`, `app.render_share_access_events`,
`app.render_share_feedback`) plus the single scoped read of `app.renders` a Share
exists to open. `render_shares_api.py` is a thin translation of HTTP onto this.

THE DENIAL CONTRACT, which is the reason most of this file is shaped the way it
is. Every refusal -- unknown bearer, already-consumed bearer, revoked Share,
expired Share, missing Render, malformed input -- raises the SAME
`RenderShareUnavailable` carrying no detail, and every refusal path executes the
SAME number of SQL statements: one lookup, one appended access event. That is the
mechanism `core.query_specs_api` states at its lines 38-39 and 43 ("One envelope
for foreign, denied and absent"; "Denial answers before any work"), and it is
asserted by counting statements, never by a stopwatch. A wall-clock threshold on
shared CI hardware is an assertion any implementation can pass and any
implementation can be argued to fail, which is not an acceptance criterion.

THE BEARER IS SINGLE USE. `product-architecture-corrective-delivery.md:650` says
it is "exchanged once". `ARCHITECTURE-SPINE.md:231` would have permitted rotation
instead; the epic acceptance is the narrower contract and therefore the binding
one. The atomicity shape is copied verbatim from `core.invitations` (its
`WHERE ... AND bearer_consumed_at IS NULL` and its `rowcount != 1` abort): under
`FOR UPDATE`, an UPDATE whose predicate carries `AND exchanged_at IS NULL`, and an
abort when it changes anything other than exactly one row. Two concurrent
exchanges of one bearer therefore yield exactly one success.

THE ACCEPTED COST OF THAT CHOICE, stated rather than buried: a corporate mail
scanner or link previewer that EXECUTES the bootstrap script burns the link before
the human clicks. A scanner that merely fetches `GET /share` consumes nothing,
because the bearer lives in the URL fragment and a fragment is never sent to a
server. When it does happen the recipient meets an ordinary English refusal naming
re-issue as the next safe action, and re-issue is one action over the SAME
immutable Render -- nothing is re-rendered.

NOTHING HERE EVER LOGS BEARER MATERIAL. Not the bearer, not a prefix of it, not
the session value, not a prefix of it. The legacy path put `token[:8]` into an
error log, into a persisted audit row and into a rate-limit key -- and, worst of
all, into the request line itself, where no handler-level redaction can reach it.
Every identifier this module logs is a `share_id`, resolved AFTER the HMAC lookup.

Those three sites carried line numbers into `rendus_api.py` until 2026-08-07 and
the numbers had rotted, because the code they pointed at was DELETED rather than
moved -- that deletion WAS the repair. `rendus_api.py:570` now describes the same
defect in the past tense, which is the durable record; a citation cannot point at
an absence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from ulid import ULID

from core import render_app_payload
from core.operations import MutationResult, OperationSpec, execute_operation

logger = logging.getLogger(__name__)

#: The cookie the exchange mints. Path-scoped to the public API family only, so it
#: is never attached to a Console request even if a recipient also happens to be a
#: signed-in operator on the same origin.
# `__session`, and not a product-named cookie, because the share page is served
# behind Firebase Hosting (app.toorow.com), which forwards ONLY a cookie named
# `__session` to a Cloud Run rewrite and strips every other one from the request
# (the CDN cache key rule). Measured 2026-09-04: the exchange set the cookie, the
# browser held it, and every `GET /api/render-shares/session/*` through the
# hosting answered 401 while the same flow against the Cloud Run URL answered
# 200. The path keeps it away from every other cookie of the product.
RENDER_SHARE_COOKIE = "__session"
RENDER_SHARE_COOKIE_PATH = "/api/render-shares"

#: The tokenless landing URL the exchange returns. It identifies no Project, no
#: organization, no Result and no Render: everything is derived from the cookie.
RENDER_SHARE_LANDING_URL = "/share/view"

#: Story 50.4's responsive-profile enum member this story pins into every Render it
#: opens. The enum itself is declared once, in `core.visualization_specs`; naming a
#: second owner for it here is exactly the duplication that story exists to avoid.
SHARE_RESPONSIVE_PROFILE = "share"

_BEARER_PURPOSE = "render-share-bearer:"
_SESSION_PURPOSE = "render-share-session:"
_CLIENT_IP_PURPOSE = "render-share-client-ip:"

#: Bearer length bounds. `secrets.token_urlsafe(32)` is 256 bits and 43 characters;
#: the window is generous enough to survive a urlsafe alphabet change and tight
#: enough that an oversized body is refused before any hashing work.
_BEARER_MIN_LEN = 32
_BEARER_MAX_LEN = 512

_DEFAULT_SESSION_TTL_SECONDS = 3600
_DEFAULT_MAX_SHARE_LIFETIME_DAYS = 30
_DEFAULT_EXCHANGE_ATTEMPT_CEILING = 20
#: Three days for a colleague to look at what is about to leave the platform.
_DEFAULT_CONFIRMATION_WINDOW_HOURS = 72

#: Reason codes. They are recorded in the access event and NEVER returned to a
#: caller: telling a recipient whether a link is unknown, revoked or expired is the
#: enumeration oracle the common envelope exists to close.
REASON_BEARER_UNKNOWN = "bearer_unknown"
REASON_BEARER_ALREADY_CONSUMED = "bearer_already_consumed"
REASON_SHARE_REVOKED = "share_revoked"
REASON_SHARE_EXPIRED = "share_expired"
REASON_RENDER_UNREADABLE = "render_unreadable"
REASON_SESSION_UNKNOWN = "session_unknown"
REASON_SESSION_EXPIRED = "session_expired"
REASON_RATE_LIMITED = "rate_limited"
REASON_CEILING_EXCEEDED = "exchange_attempt_ceiling_exceeded"
REASON_GRANTED = "granted"


class RenderShareValidationError(Exception):
    """A caller-side input the Console must fix. Never raised on the public path."""


class RenderShareUnavailable(Exception):
    """THE public refusal. One type, no detail, no subclass.

    A subclass would be a distinction a handler could accidentally surface, and a
    message would be one an exception formatter could log. The reason code lives in
    the access event, where auditors can read it and recipients cannot.
    """


class RenderShareConfirmationRefused(RenderShareValidationError):
    """The two-person ceremony refused this confirmation, and says which half.

    A subclass of the validation error so every existing caller keeps mapping it
    to the same 422; the console reads `code` to tell "you cannot confirm your
    own request" from "this ticket has already been used", because the two are
    repaired by different people.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ShareNotRevocable(RenderShareValidationError):
    """The revoke matched nothing: unknown share, another project's, or already revoked.

    A subclass of the validation error so generic handlers keep their 422; the
    console DELETE maps it to 404, because answering `revoked` for an act that
    did not happen reports a closure nobody performed (review of fb073500,
    adjacent defect)."""


class RenderShareConflict(Exception):
    """One scoped retry key was already used for a different exact request."""


def _unavailable() -> RenderShareUnavailable:
    return RenderShareUnavailable("render share is unavailable")


# ---------------------------------------------------------------------------
# Peppered hashing. Three purposes, three digests, one pepper -- distinct from
# every other bearer class in the repository (D3).
# ---------------------------------------------------------------------------


def _pepper() -> bytes:
    """The Render-Share pepper, refused below 32 bytes.

    A DISTINCT pepper, not `TOOROW_INVITATION_PEPPER`. AD-30 requires bearers to be
    purpose-scoped: sharing a pepper means a bearer minted for one class produces a
    valid digest in another class's table, and one leak compromises both. The
    repository already keeps separate peppers for invitations, setup
    responsibilities and the first-value funnel for exactly this reason.
    """
    value = os.environ.get("TOOROW_RENDER_SHARE_PEPPER", "")
    if len(value.encode("utf-8")) < 32:
        raise RenderShareValidationError("TOOROW_RENDER_SHARE_PEPPER must contain 32 bytes")
    return value.encode("utf-8")


def _digest(purpose: str, value: str) -> str:
    return hmac.new(_pepper(), f"{purpose}{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def bearer_hash(bearer: str) -> str:
    return _digest(_BEARER_PURPOSE, bearer)


def session_hash(session_value: str) -> str:
    return _digest(_SESSION_PURPOSE, session_value)


def client_ip_hash(client_ip: str | None) -> str | None:
    """Peppered HMAC of the caller's IP, or None when there is none to hash.

    Stored so a burst can be recognised as one actor without the access log
    becoming a record of who read what from where.
    """
    if not client_ip:
        return None
    return _digest(_CLIENT_IP_PURPOSE, client_ip)


def mint_bearer() -> str:
    """256 bits. Never stored, never logged, returned exactly once."""
    return secrets.token_urlsafe(32)


def build_fragment_delivery_url(bearer: str) -> str:
    """`https://{origin}/share#render={bearer}` -- the ONLY place a bearer appears.

    The origin is validated the way `core.invitations.build_fragment_delivery_url`
    validates it: HTTPS, no credentials, no query, no fragment, and a path of `""`
    or `"/"`. A misconfigured origin that already carried a query string would
    otherwise turn the fragment into a query parameter, which is the one place the
    bearer must never be -- a query string reaches the server, the access log and
    the referrer; a fragment reaches none of them.
    """
    origin = os.environ.get("TOOROW_RENDER_SHARE_ORIGIN", "").rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RenderShareValidationError("TOOROW_RENDER_SHARE_ORIGIN must be an HTTPS origin")
    return f"{origin}/share#render={bearer}"


# ---------------------------------------------------------------------------
# Configuration. Defaults plus per-deployment override, never a literal buried in
# a handler (memory `prefer-per-project-preferences`).
# ---------------------------------------------------------------------------


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if low <= value <= high else default


def session_ttl_seconds() -> int:
    return _int_env(
        "TOOROW_RENDER_SHARE_SESSION_TTL_SECONDS",
        _DEFAULT_SESSION_TTL_SECONDS,
        low=60,
        high=86400,
    )


def max_share_lifetime_days() -> int:
    return _int_env(
        "TOOROW_RENDER_SHARE_MAX_LIFETIME_DAYS",
        _DEFAULT_MAX_SHARE_LIFETIME_DAYS,
        low=1,
        high=365,
    )


def exchange_attempt_ceiling() -> int:
    return _int_env(
        "TOOROW_RENDER_SHARE_ATTEMPT_CEILING",
        _DEFAULT_EXCHANGE_ATTEMPT_CEILING,
        low=1,
        high=1000,
    )


def confirmation_window_hours() -> int:
    """How long a requested Share waits for its SECOND role holder.

    A window and not an open invitation: a ticket that never lapses is a link
    somebody can mint months later over a Render frozen against a question
    nobody remembers asking. The effective deadline is the earlier of this window
    and the Share's own expiry -- confirming a Share that is already dead would
    hand out a link that opens nothing.
    """
    return _int_env(
        "TOOROW_RENDER_SHARE_CONFIRMATION_HOURS",
        _DEFAULT_CONFIRMATION_WINDOW_HOURS,
        low=1,
        high=336,
    )


# ---------------------------------------------------------------------------
# The public connection. AC6 layer 2.
# ---------------------------------------------------------------------------


@contextmanager
def render_share_connection():
    """The ONLY connection the public handlers open, with the reader role armed.

    `SET LOCAL ROLE` is transaction-scoped and reverts at commit or rollback, which
    is why it is issued inside the transaction the handler will use rather than
    once at connect time. `toorow_share_reader` holds only the bounded Share reads
    and event writes plus EXECUTE on the exact-feedback definer; direct feedback
    INSERT is revoked. It has SELECT on `app.renders`, and NO privilege at all on
    `app.query_results`, `app.query_result_payloads`, `app.query_specs*`,
    `app.projects` or `app.organizations` -- so a hand-crafted SELECT through this
    helper is refused by PostgreSQL with SQLSTATE 42501.

    This is the layer that makes "the page cannot reach Project data" a property of
    the server rather than an omission in the client. `snapshot_shares.py` promises
    the same restriction today, in a docstring -- named without a line, because the
    number this sentence carried pointed past the end of that file.
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL ROLE toorow_share_reader")
        yield conn


# ---------------------------------------------------------------------------
# Access evidence.
# ---------------------------------------------------------------------------


def classify_client(user_agent: str | None) -> str:
    """A COARSE class, and coarse on purpose: 'browser', 'non_browser', 'unknown'.

    Enough to tell a link previewer's burst from a human, not enough to be a
    fingerprint. Storing the raw user agent would turn the access log into a
    profile of people who never consented to one.
    """
    if not user_agent:
        return "unknown"
    lowered = user_agent.lower()
    if "mozilla" in lowered or "webkit" in lowered or "gecko" in lowered:
        return "browser"
    return "non_browser"


def append_access_event(
    conn,
    *,
    share_id: str | None,
    org_id: str | None,
    project_id: str | None,
    event: str,
    outcome: str,
    reason_code: str,
    ip_hash: str | None,
    client_class: str,
) -> str:
    """Append ONE evidence row. Exactly one statement, on every path.

    The statement count matters: it is what makes the four refusal paths
    indistinguishable. A caller that skipped this append on the unknown-bearer path
    would make that path one statement shorter than the others, and the difference
    is measurable by anyone with a stopwatch and patience.
    """
    event_id = f"rsev_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.render_share_access_events
                (id, share_id, org_id, project_id, event, outcome, reason_code,
                 client_ip_hash, client_class)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                share_id,
                org_id,
                project_id,
                event,
                outcome,
                reason_code,
                ip_hash,
                client_class,
            ),
        )
    return event_id


# ---------------------------------------------------------------------------
# Creation and revocation -- governed mutations.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShareCreation:
    """A REQUEST for a Share, not a Share anyone can open yet.

    NOTHING HERE IS A LINK, and the absence is the feature. Until migration 323
    this object carried `delivery_url`: a Share was live the instant one person
    with `edit` pressed a button. `proactive-assertions.md` decision 2 says the
    exit needs a second role holder, so the bearer is minted by the CONFIRMATION
    (`ShareConfirmation`) and there is no window in which a URL exists that only
    needs somebody else to agree.
    """

    share_id: str
    #: Exactly one of the two targets is set (migration 341's CHECK says the
    #: same thing where it cannot be argued with): one Render, or one immutable
    #: Dossier version whose sequence of Renders this grant opens.
    render_id: str | None
    state: str
    expires_at: datetime
    operation_id: str
    replayed: bool
    #: The ticket a second role holder consumes. Not a secret: it names a row
    #: whose confirmation is authorized by the caller's Project role, never by
    #: knowing its id.
    confirmation_id: str
    confirmation_requested_by: str
    confirmation_expires_at: datetime
    dossier_version_id: str | None = None


@dataclass(frozen=True)
class ShareConfirmation:
    share_id: str
    render_id: str
    state: str
    expires_at: datetime
    #: Returned ONCE, to the person who authorized the exit. It is never
    #: re-derivable, because only its HMAC was stored.
    delivery_url: str
    confirmed_by: str
    operation_id: str


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def create_share(
    conn,
    *,
    org_id: str,
    project_id: str,
    render_id: str | None = None,
    dossier_version_id: str | None = None,
    actor: str,
    expires_at: datetime,
    idempotency_key: str,
    trace_id: str | None = None,
) -> ShareCreation:
    """REQUEST one grant to one Render, with its audit row, in one transaction.

    Routed through `core.operations.execute_operation` so the grant, the audit row
    and the outbox event commit together or not at all. The legacy path wrote its
    audit in a `try/except Exception: pass` AFTER the grant had committed
    (`rendus_api.py:474-486`), which means a share that was created but not audited
    is indistinguishable from one that was never created.

    TWO GATES BEFORE ANYTHING IS WRITTEN, both from `proactive-assertions.md`
    decision 2 -- "external sharing requires a project-scoped capability plus
    confirmation by a *second* role holder":

      1. the PROJECT must allow the exit at all. `assert_external_sharing_allowed`
         runs first, ahead of the freeze: a refusal that has already frozen a
         payload has done work on behalf of an act the Project forbade.
      2. the Share is born `pending_confirmation` WITH NO BEARER. It opens
         nothing, it can be delivered nowhere, and `exchange_bearer` cannot reach
         it -- its UNIQUE lookup is on a column this row leaves NULL. The link is
         minted by `confirm_share`, by somebody else.

    The operation is `confirmation_mode="human"` carrying the ticket id as its
    real `confirmation_reference`. The matching `render_share.confirm` operation
    carries the same reference, so one `confirmation_reference_hash` joins the
    request to its confirmation in `app.audit_log`.
    """
    from core.project_external_sharing import (  # noqa: PLC0415
        assert_external_sharing_allowed,
    )

    # 73-2: one grant, one target -- the same rule migration 341 CHECKs.
    if bool(render_id) == bool(dossier_version_id):
        raise RenderShareValidationError(
            "a share opens exactly one target: a Render, or a Dossier version"
        )

    assert_external_sharing_allowed(conn, project_id=project_id)

    expiry = _aware_utc(expires_at)
    now = datetime.now(tz=timezone.utc)
    if expiry <= now:
        raise RenderShareValidationError("expires_at must be in the future")
    horizon = now + timedelta(days=max_share_lifetime_days())
    if expiry > horizon:
        raise RenderShareValidationError(
            f"expires_at must be within {max_share_lifetime_days()} days"
        )

    # Built BEFORE the mutation, exactly as the bearer used to be: a misconfigured
    # delivery origin must refuse the whole request now, rather than leave a
    # pending Share that nobody can ever turn into a deliverable link.
    build_fragment_delivery_url(mint_bearer())
    share_id = f"rsh_{ULID()}"
    confirmation_id = f"rsc_{ULID()}"
    # The earlier of the two deadlines. Confirming a Share that has already
    # expired would hand out a link that opens nothing.
    confirmation_expiry = min(
        now + timedelta(hours=confirmation_window_hours()), expiry
    )
    ceiling = exchange_attempt_ceiling()

    created: dict[str, Any] = {}

    def _mutation(mutation_conn, operation_id: str) -> MutationResult:
        # FREEZE FIRST, INSIDE THE SAME TRANSACTION. A grant that commits over a
        # Render with nothing frozen behind it is a link that opens a page with no
        # values on it -- which is what the first implementation shipped. Freezing
        # here means the refusal reaches the console operator instead. A Dossier
        # grant freezes EVERY Render of the pinned version, and a refusal names
        # the one that cannot be shared.
        for frozen_render_id in (
            [render_id]
            if render_id
            else dossier_version_render_ids(
                mutation_conn,
                org_id=org_id,
                project_id=project_id,
                dossier_version_id=dossier_version_id,
            )
        ):
            try:
                freeze_render_payload(
                    mutation_conn,
                    org_id=org_id,
                    project_id=project_id,
                    render_id=frozen_render_id,
                    actor=actor,
                )
            except RenderShareValidationError as exc:
                raise RenderShareValidationError(
                    f"Render {frozen_render_id}: {exc}"
                ) from exc
        with mutation_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.render_shares
                    (id, org_id, project_id, render_id, dossier_version_id,
                     bearer_hash, state, expires_at, created_by,
                     exchange_attempt_ceiling, created_operation_id)
                VALUES (%s, %s, %s, %s, %s, NULL, 'pending_confirmation', %s, %s, %s, %s)
                RETURNING id, state, expires_at
                """,
                (
                    share_id,
                    org_id,
                    project_id,
                    render_id,
                    dossier_version_id,
                    expiry,
                    actor,
                    ceiling,
                    operation_id,
                ),
            )
            row = cur.fetchone()
            cur.execute(
                """
                INSERT INTO app.render_share_confirmations
                    (id, share_id, org_id, project_id, requested_by,
                     requested_operation_id, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, requested_by, expires_at
                """,
                (
                    confirmation_id,
                    share_id,
                    org_id,
                    project_id,
                    actor,
                    operation_id,
                    confirmation_expiry,
                ),
            )
            ticket = cur.fetchone()
        created["id"] = row[0]
        created["state"] = row[1]
        created["expires_at"] = _aware_utc(row[2])
        created["confirmation_id"] = ticket[0]
        created["confirmation_requested_by"] = ticket[1]
        created["confirmation_expires_at"] = _aware_utc(ticket[2])
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=None,
            # The result payload is persisted in the operation row. It carries the
            # share id and the Render it opens -- and never the bearer or the URL.
            result={
                "share_id": row[0],
                "render_id": render_id,
                "dossier_version_id": dossier_version_id,
                "state": row[1],
                "expires_at": created["expires_at"].isoformat(),
                "confirmation_id": ticket[0],
            },
            outbox_payload={
                "share_id": row[0],
                "render_id": render_id,
                "dossier_version_id": dossier_version_id,
                "state": row[1],
            },
        )

    outcome = execute_operation(
        conn,
        OperationSpec(
            command_type="render_share.create",
            actor=actor,
            effective_org_id=org_id,
            resource_path=(
                f"project:{project_id}",
                f"render:{render_id}" if render_id else f"dossier_version:{dossier_version_id}",
                "share",
            ),
            idempotency_key=idempotency_key,
            host_context={},
            versions={},
            request_payload={
                "render_id": render_id,
                "dossier_version_id": dossier_version_id,
                "expires_at": expiry.isoformat(),
            },
            provider_references={},
            # `human`, and the reference is REAL: the ticket this request just
            # minted. `none` was the honest description of the old path -- nobody
            # confirmed anything -- and it is exactly what decision 2 refuses.
            confirmation_mode="human",
            confirmation_reference=confirmation_id,
            trace_id=trace_id,
        ),
        mutation=_mutation,
    )
    if outcome.replayed:
        # An idempotent replay hands back the ORIGINAL request. It is still in the
        # list, still waiting for its second role holder, and minting a second
        # ticket for the same Render would let one person collect two
        # confirmations for one exit.
        raise RenderShareValidationError(
            "this share was already requested; it is in the list below, waiting "
            "for a second person to confirm it."
        )
    logger.info(
        "render_shares: requested share_id=%s target=%s project_id=%s "
        "awaiting a second confirmation",
        created["id"],
        render_id or dossier_version_id,
        project_id,
    )
    return ShareCreation(
        share_id=created["id"],
        render_id=render_id,
        state=created["state"],
        expires_at=created["expires_at"],
        operation_id=outcome.operation_id,
        replayed=False,
        confirmation_id=created["confirmation_id"],
        confirmation_requested_by=created["confirmation_requested_by"],
        confirmation_expires_at=created["confirmation_expires_at"],
        dossier_version_id=dossier_version_id,
    )


def confirm_share(
    conn,
    *,
    org_id: str,
    project_id: str,
    share_id: str,
    actor: str,
    idempotency_key: str,
    trace_id: str | None = None,
) -> ShareConfirmation:
    """A SECOND role holder authorizes the exit, and only then does a link exist.

    THE STATE TRANSITION IS THE WHOLE MECHANISM: `pending_confirmation` ->
    `active`, on the same row, audited, never a delete and never a second row.
    `proactive-assertions.md` decision 4 states the same rule for a retraction --
    "a retraction is an audited state transition, never a DELETE" -- and the
    schema enforces it here (`trg_render_shares_no_delete`).

    THE SECOND HOLDER IS CHECKED IN THREE PLACES, on purpose. This function
    refuses `actor == requested_by` with a sentence that names the gesture; the
    console refuses it before offering the control; and
    `ck_render_share_confirmations_second_holder` refuses the row outright. The
    first two can be forgotten in a refactor. The third cannot.

    THE ROLE IS NOT CHECKED HERE, and that is the module boundary this file has
    kept since 50.7: `render_shares_console_api` owns "who may call this", the
    same way it owns `edit` on the request and `manage` on the revoke. What this
    function owns is that the caller is a DIFFERENT person.

    THE PROJECT'S POSTURE IS CHECKED HERE, AND IT IS THE FIRST STATEMENT. The
    exit does not happen when a request is filed -- a request has no bearer and
    no URL. It happens HERE, when the bearer is minted. Without this line a
    project could allow sharing, receive a request, switch back to `forbidden`,
    and still have a LIVE delivery URL minted afterwards by a second holder:
    replayed 2026-08-30 against the disposable base, the confirmation returned
    201 with a working `delivery_url` over a project that forbade the exit. The
    check runs on the SAME connection as the transition AND takes `FOR SHARE` on
    the preferences row, so a posture committed mid-transaction cannot outrun
    the mint: the posture read is the posture the transition commits against.

    A pending request is FROZEN, not destroyed: switching the posture off leaves
    the row `pending_confirmation`, refuses every confirmation while it stands,
    and lets the same request be confirmed again if the project re-allows the
    exit before the confirmation window closes. Documented in
    `docs/product-architecture/project-settings.md` and `proactive-assertions.md`.
    """
    from core.project_external_sharing import (  # noqa: PLC0415
        assert_external_sharing_allowed,
    )

    assert_external_sharing_allowed(conn, project_id=project_id)

    now = datetime.now(tz=timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.requested_by, c.expires_at, c.confirmed_by,
                   s.state, s.expires_at, s.render_id
              FROM app.render_share_confirmations c
              JOIN app.render_shares s
                ON s.id = c.share_id AND s.org_id = c.org_id
               AND s.project_id = c.project_id
             WHERE c.share_id = %s AND c.org_id = %s AND c.project_id = %s
             FOR UPDATE OF c, s
            """,
            (share_id, org_id, project_id),
        )
        row = cur.fetchone()

    if row is None:
        raise RenderShareConfirmationRefused(
            "confirmation_absent",
            "There is no share request to confirm here. Create one from the "
            "Render you want to share.",
        )
    (
        confirmation_id,
        requested_by,
        confirmation_expires_at,
        confirmed_by,
        share_state,
        share_expires_at,
        render_id,
    ) = row
    confirmation_expires_at = _aware_utc(confirmation_expires_at)
    share_expires_at = _aware_utc(share_expires_at)

    if confirmed_by is not None:
        raise RenderShareConfirmationRefused(
            "confirmation_already_used",
            f"{confirmed_by} already confirmed this share. Its link was shown "
            "once, to them.",
        )
    if share_state == "revoked":
        raise RenderShareConfirmationRefused(
            "share_revoked",
            "This share request was revoked and can no longer be confirmed. "
            "Create a new one over the same Render.",
        )
    if share_state != "pending_confirmation":
        raise RenderShareConfirmationRefused(
            "share_not_pending",
            "This share is not waiting for a confirmation.",
        )
    if actor == requested_by:
        raise RenderShareConfirmationRefused(
            "second_role_holder_required",
            "You requested this share, so you cannot confirm it. Someone else "
            "holding the Edit role on this project must confirm it before the "
            "link exists.",
        )
    if confirmation_expires_at <= now or share_expires_at <= now:
        raise RenderShareConfirmationRefused(
            "confirmation_window_closed",
            "The window to confirm this share has closed. Create a new share "
            "over the same Render.",
        )

    bearer = mint_bearer()
    # Built BEFORE the mutation: a misconfigured origin must refuse the whole
    # confirmation, not leave a live grant nobody can deliver.
    delivery_url = build_fragment_delivery_url(bearer)
    digest = bearer_hash(bearer)

    def _mutation(mutation_conn, operation_id: str) -> MutationResult:
        with mutation_conn.cursor() as cur:
            # CONSUMED ONCE, by predicate. The shape is `core.invitations`' and
            # `exchange_bearer`'s: an UPDATE whose WHERE carries
            # `AND confirmed_at IS NULL`, and an abort on anything but one row.
            # Two concurrent confirmations therefore yield exactly one link.
            cur.execute(
                """
                UPDATE app.render_share_confirmations
                   SET confirmed_by = %s, confirmed_at = %s,
                       confirmed_operation_id = %s
                 WHERE id = %s AND confirmed_at IS NULL
                """,
                (actor, now, operation_id, confirmation_id),
            )
            if cur.rowcount != 1:
                raise RenderShareConfirmationRefused(
                    "confirmation_already_used",
                    "This share was confirmed by someone else a moment ago.",
                )
            cur.execute(
                """
                UPDATE app.render_shares
                   SET bearer_hash = %s, state = 'active'
                 WHERE id = %s AND org_id = %s AND project_id = %s
                   AND state = 'pending_confirmation' AND bearer_hash IS NULL
                """,
                (digest, share_id, org_id, project_id),
            )
            if cur.rowcount != 1:
                raise RenderShareConfirmationRefused(
                    "share_not_pending",
                    "This share is not waiting for a confirmation.",
                )
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=None,
            # Never the bearer and never the URL, exactly as at request time.
            result={
                "share_id": share_id,
                "render_id": render_id,
                "state": "active",
                "confirmed_by": actor,
            },
            outbox_payload={"share_id": share_id, "render_id": render_id},
        )

    outcome = execute_operation(
        conn,
        OperationSpec(
            command_type="render_share.confirm",
            actor=actor,
            effective_org_id=org_id,
            resource_path=(f"project:{project_id}", f"share:{share_id}", "confirmation"),
            idempotency_key=idempotency_key,
            host_context={},
            versions={},
            request_payload={"share_id": share_id, "render_id": render_id},
            provider_references={},
            # The same real reference the request carried. One
            # `confirmation_reference_hash` therefore joins the two halves of the
            # ceremony in `app.audit_log`.
            confirmation_mode="human",
            confirmation_reference=confirmation_id,
            trace_id=trace_id,
        ),
        mutation=_mutation,
    )
    if outcome.replayed:
        raise RenderShareConfirmationRefused(
            "confirmation_already_used",
            "This share was already confirmed; its link was shown once and "
            "cannot be re-derived. Create a new share over the same Render.",
        )
    logger.info(
        "render_shares: confirmed share_id=%s project_id=%s by a second holder",
        share_id,
        project_id,
    )
    return ShareConfirmation(
        share_id=share_id,
        render_id=render_id,
        state="active",
        expires_at=share_expires_at,
        delivery_url=delivery_url,
        confirmed_by=actor,
        operation_id=outcome.operation_id,
    )


def revoke_share(
    conn,
    *,
    org_id: str,
    project_id: str,
    share_id: str,
    actor: str,
    idempotency_key: str,
    reason_code: str = "revoked_by_operator",
    trace_id: str | None = None,
) -> str:
    """Move one Share to `revoked`, governed. The row is never deleted.

    IT REACHES A PENDING SHARE TOO, since migration 323. Withdrawing a request
    nobody has confirmed yet is the ordinary way to say "never mind", and the
    only one: the request cannot be deleted, and leaving it in the list would
    keep an exit confirmable by anyone who walks past.

    IT STAYS `confirmation_mode="server"`, and that is a decision rather than an
    oversight of the two-person rule above. Decision 2 governs what LEAVES the
    platform; cutting a link off does not leave anything. Requiring a second
    person to revoke would keep a live grant open while somebody was found, which
    inverts the whole point.
    """
    now = datetime.now(tz=timezone.utc)

    def _mutation(mutation_conn, operation_id: str) -> MutationResult:
        with mutation_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.render_shares
                   SET state = 'revoked', revoked_at = %s, revoked_by = %s,
                       revoke_reason_code = %s, revoked_operation_id = %s
                 WHERE id = %s AND org_id = %s AND project_id = %s
                   AND state IN ('active', 'pending_confirmation')
                """,
                (now, actor, reason_code, operation_id, share_id, org_id, project_id),
            )
            changed = cur.rowcount
        return MutationResult(
            outcome="succeeded" if changed == 1 else "failed",
            before_hash=None,
            after_hash=None,
            result={"share_id": share_id, "revoked": changed == 1},
            outbox_payload={"share_id": share_id},
        )

    outcome = execute_operation(
        conn,
        OperationSpec(
            command_type="render_share.revoke",
            actor=actor,
            effective_org_id=org_id,
            resource_path=(f"project:{project_id}", f"share:{share_id}"),
            idempotency_key=idempotency_key,
            host_context={},
            versions={},
            request_payload={"share_id": share_id, "reason_code": reason_code},
            provider_references={},
            confirmation_mode="server",
            confirmation_reference=None,
            trace_id=trace_id,
        ),
        mutation=_mutation,
    )
    revoked = bool((outcome.result or {}).get("revoked"))
    if not revoked:
        # The UPDATE matched no active/pending row: unknown id, another project's
        # share, or one already revoked. Answering "revoked" here would report an
        # act that did not happen (review of fb073500, adjacent defect).
        raise ShareNotRevocable(
            "This share was not revoked: it does not exist in this project, or "
            "it was already revoked. Refresh the list to see its current state."
        )
    logger.info("render_shares: revoked share_id=%s project_id=%s", share_id, project_id)
    return outcome.operation_id


def list_shares(
    conn,
    *,
    org_id: str,
    project_id: str,
    render_id: str | None = None,
    dossier_id: str | None = None,
    viewer: str | None = None,
    viewer_may_confirm: bool = False,
) -> list[dict]:
    """Console-side listing: state and audit facts, and NO way to rebuild a link.

    The legacy listing returned `share_token` in every row and its docstring
    (`snapshot_shares.py:167-170`) argued that was fine because the caller is
    authenticated. It is not fine: it turns every console screenshot, every browser
    cache entry and every support-ticket paste into a live public grant.

    `can_confirm` IS ANSWERED HERE, NOT ON THE SCREEN. Whether this reader may be
    the second role holder of this exact request is three facts the server holds
    -- the requester's identity, the window, and whether the reader carries the
    role -- and a screen that inferred it would be inferring an authority. It
    offers the control only where the server says so, and the server refuses
    again when the control is used.
    """
    sql = """
        SELECT s.id, s.render_id, s.state, s.expires_at, s.created_by, s.created_at,
               s.revoked_at, s.revoked_by, s.revoke_reason_code, s.exchanged_at,
               s.exchange_attempt_count, s.exchange_attempt_ceiling,
               (SELECT count(*) FROM app.render_share_access_events e
                 WHERE e.share_id = s.id AND e.outcome = 'granted') AS granted_events,
               (SELECT count(*) FROM app.render_share_access_events e
                 WHERE e.share_id = s.id AND e.outcome = 'refused') AS refused_events,
               c.requested_by, c.requested_at, c.expires_at, c.confirmed_by,
               c.confirmed_at
          FROM app.render_shares s
          LEFT JOIN app.render_share_confirmations c
                 ON c.share_id = s.id AND c.org_id = s.org_id
                AND c.project_id = s.project_id
         WHERE s.org_id = %s AND s.project_id = %s
    """
    params: list[Any] = [org_id, project_id]
    if render_id:
        sql += " AND s.render_id = %s"
        params.append(render_id)
    if dossier_id:
        sql += (
            " AND s.dossier_version_id IN (SELECT id FROM app.analysis_dossier_versions"
            " WHERE dossier_id = %s AND org_id = %s AND project_id = %s)"
        )
        params.extend([dossier_id, org_id, project_id])
    sql += " ORDER BY s.created_at DESC LIMIT 200"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    now = datetime.now(tz=timezone.utc)
    return [
        _share_row(r, now=now, viewer=viewer, viewer_may_confirm=viewer_may_confirm)
        for r in rows
    ]


def _confirmation_state(
    *, requested_by: str | None, window_ends: datetime | None, confirmed_at: datetime | None
) -> str:
    """The ceremony's own state, separate from the Share's.

    Two independent facts, each named, because one word cannot carry both: a
    Share can be `expired` because its LINK lifetime ran out, and a request can
    be `expired` because nobody looked at it in time. Collapsing them would leave
    the reader guessing which deadline they missed, and the two are repaired the
    same way but for different reasons.
    """
    if requested_by is None:
        # Predates migration 323: the row was created when one person's `edit`
        # was the whole ceremony. Said by name rather than back-filled into
        # `confirmed`, which would claim a second person who never existed.
        return "absent"
    if confirmed_at is not None:
        return "confirmed"
    if window_ends is not None and window_ends <= datetime.now(tz=timezone.utc):
        return "expired"
    return "pending"


def _share_row(
    r, *, now: datetime, viewer: str | None, viewer_may_confirm: bool
) -> dict:
    requested_by = r[14]
    window_ends = _aware_utc(r[16]) if r[16] else None
    confirmed_at = _aware_utc(r[18]) if r[18] else None
    confirmation_state = _confirmation_state(
        requested_by=requested_by, window_ends=window_ends, confirmed_at=confirmed_at
    )
    return {
        "share_id": r[0],
        "render_id": r[1],
        # An `active` row whose expiry has passed IS expired, whatever the
        # column says: nothing sweeps the table, and reporting it as active
        # would be the screen lying about a grant that no longer opens.
        "state": "expired" if r[2] == "active" and _aware_utc(r[3]) <= now else r[2],
        "expires_at": _aware_utc(r[3]).isoformat(),
        "created_by": r[4],
        "created_at": _aware_utc(r[5]).isoformat(),
        "revoked_at": _aware_utc(r[6]).isoformat() if r[6] else None,
        "revoked_by": r[7],
        "revoke_reason_code": r[8],
        "bearer_exchanged": r[9] is not None,
        "exchange_attempt_count": r[10],
        "exchange_attempt_ceiling": r[11],
        "granted_access_count": r[12],
        "refused_access_count": r[13],
        "confirmation_state": confirmation_state,
        "confirmation_requested_by": requested_by,
        "confirmation_requested_at": _aware_utc(r[15]).isoformat() if r[15] else None,
        "confirmation_expires_at": window_ends.isoformat() if window_ends else None,
        "confirmed_by": r[17],
        "confirmed_at": confirmed_at.isoformat() if confirmed_at else None,
        "can_confirm": bool(
            viewer_may_confirm
            and viewer
            and r[2] == "pending_confirmation"
            and confirmation_state == "pending"
            and requested_by != viewer
            and _aware_utc(r[3]) > now
        ),
        # Said as a fact, so the screen states it instead of deducing it from an
        # absent button -- an absence explains nothing.
        "awaiting_your_own_request": bool(
            viewer and requested_by == viewer and confirmation_state == "pending"
        ),
    }


# ---------------------------------------------------------------------------
# The exchange. One lookup, one event append -- on every path, success included.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExchangeResult:
    session_value: str
    max_age_seconds: int
    next_url: str


def exchange_bearer(
    conn,
    *,
    bearer: str,
    ip_hash: str | None,
    client_class: str,
) -> ExchangeResult:
    """Consume ONE bearer into ONE narrow session, atomically. Raises on anything else.

    The statement budget, which is the falsifiable part of the indistinguishability
    claim, is fixed at four on every path that reaches the database:

        1. SELECT ... FOR UPDATE          the HMAC lookup
        2. UPDATE app.render_shares       consume, or count the failed attempt
        3. INSERT ... access_events       the evidence
        4. INSERT ... exchange_sessions   success only

    A refusal stops after (3) and therefore executes exactly three statements
    whether the bearer is unknown, already consumed, revoked, expired or points at
    a Render this Share can no longer read. `test_render_shares.py` installs a
    statement counter and asserts the four refusal paths are equal; nothing here
    asserts a duration.
    """
    if not isinstance(bearer, str) or not _BEARER_MIN_LEN <= len(bearer) <= _BEARER_MAX_LEN:
        # Refused before ANY database work and before any hashing: a body that is
        # not even bearer-shaped must not buy an attacker a database round trip.
        raise _unavailable()
    try:
        candidate = bearer_hash(bearer)
    except RenderShareValidationError as exc:
        raise _unavailable() from exc

    now = datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        # (1) The lookup. `FOR UPDATE OF s` locks the Share and nothing else, so a
        # concurrent read of the Render is not serialized behind an exchange.
        cur.execute(
            """
            SELECT s.id, s.org_id, s.project_id, s.render_id, s.bearer_hash,
                   s.state, s.expires_at, s.exchanged_at,
                   s.exchange_attempt_count, s.exchange_attempt_ceiling,
                   r.id, s.dossier_version_id, dv.id
              FROM app.render_shares s
              LEFT JOIN app.renders r
                     ON r.id = s.render_id AND r.org_id = s.org_id
                    AND r.project_id = s.project_id
              LEFT JOIN app.analysis_dossier_versions dv
                     ON dv.id = s.dossier_version_id AND dv.org_id = s.org_id
                    AND dv.project_id = s.project_id
             WHERE s.bearer_hash = %s
             FOR UPDATE OF s
            """,
            (candidate,),
        )
        row = cur.fetchone()

        if row is None:
            # (2) and (3) still run, so the unknown path costs the same as the
            # others. The UPDATE is a deliberate no-op against a hash that matches
            # nothing -- it is the statement, not its effect, that has to exist.
            cur.execute(
                "UPDATE app.render_shares SET exchange_attempt_count = exchange_attempt_count + 1"
                " WHERE bearer_hash = %s AND state = 'active'",
                (candidate,),
            )
            append_access_event(
                conn,
                share_id=None,
                org_id=None,
                project_id=None,
                event="denied",
                outcome="refused",
                reason_code=REASON_BEARER_UNKNOWN,
                ip_hash=ip_hash,
                client_class=client_class,
            )
            conn.commit()
            raise _unavailable()

        (
            share_id,
            org_id,
            project_id,
            render_id,
            stored_hash,
            state,
            expires_at,
            exchanged_at,
            attempt_count,
            ceiling,
            resolved_render,
            dossier_version_id,
            resolved_dossier_version,
        ) = row
        expires_at = _aware_utc(expires_at)

        reason: str | None = None
        if not hmac.compare_digest(stored_hash, candidate):
            # Unreachable through a UNIQUE index lookup; kept because the day the
            # lookup changes shape, a plain `==` here would become the weakness.
            reason = REASON_BEARER_UNKNOWN
        elif exchanged_at is not None:
            reason = REASON_BEARER_ALREADY_CONSUMED
        elif state == "revoked":
            reason = REASON_SHARE_REVOKED
        elif state == "expired" or expires_at <= now:
            reason = REASON_SHARE_EXPIRED
        elif resolved_render is None and resolved_dossier_version is None:
            # The Render is gone or belongs to another Project. `ON DELETE RESTRICT`
            # makes the first impossible and the composite FK makes the second
            # impossible -- this branch is the assertion that both still hold.
            # A Share opens exactly one target (migration 341): a Render OR a
            # Dossier version. Until 2026-09-04 only the Render was looked up, so
            # every dossier link a second holder had confirmed was refused here
            # -- measured by G15-T07 on the deployment.
            reason = REASON_RENDER_UNREADABLE

        if reason is not None:
            attempts = attempt_count + 1
            over_ceiling = attempts >= ceiling and state == "active"
            if over_ceiling:
                # (2) The ceiling bounds FAILED attempts, which is where brute force
                # and replay live -- successes are already bounded at one by
                # single-use consumption.
                cur.execute(
                    """
                    UPDATE app.render_shares
                       SET exchange_attempt_count = %s, state = 'revoked',
                           revoked_at = %s, revoked_by = 'system',
                           revoke_reason_code = %s
                     WHERE id = %s AND state = 'active'
                    """,
                    (attempts, now, REASON_CEILING_EXCEEDED, share_id),
                )
            else:
                cur.execute(
                    "UPDATE app.render_shares SET exchange_attempt_count = %s WHERE id = %s",
                    (attempts, share_id),
                )
            append_access_event(
                conn,
                share_id=share_id,
                org_id=org_id,
                project_id=project_id,
                event="denied",
                outcome="refused",
                reason_code=REASON_CEILING_EXCEEDED if over_ceiling else reason,
                ip_hash=ip_hash,
                client_class=client_class,
            )
            conn.commit()
            logger.info(
                "render_shares: exchange refused share_id=%s reason=%s",
                share_id,
                REASON_CEILING_EXCEEDED if over_ceiling else reason,
            )
            raise _unavailable()

        # (2) Consume. `AND exchanged_at IS NULL` plus the rowcount abort is the
        # atomicity proof, copied from `core.invitations`. Two concurrent exchanges
        # of one bearer: the loser's UPDATE changes zero rows and it refuses.
        cur.execute(
            """
            UPDATE app.render_shares
               SET exchanged_at = %s, exchange_attempt_count = exchange_attempt_count + 1
             WHERE id = %s AND exchanged_at IS NULL
            """,
            (now, share_id),
        )
        if cur.rowcount != 1:
            append_access_event(
                conn,
                share_id=share_id,
                org_id=org_id,
                project_id=project_id,
                event="denied",
                outcome="refused",
                reason_code=REASON_BEARER_ALREADY_CONSUMED,
                ip_hash=ip_hash,
                client_class=client_class,
            )
            conn.commit()
            raise _unavailable()

        # (3) Evidence, then (4) the session.
        append_access_event(
            conn,
            share_id=share_id,
            org_id=org_id,
            project_id=project_id,
            event="exchanged",
            outcome="granted",
            reason_code=REASON_GRANTED,
            ip_hash=ip_hash,
            client_class=client_class,
        )

        session_value = secrets.token_urlsafe(32)
        # The session can never outlive the grant: a Share that expires in 90
        # seconds hands out a 90-second cookie, not a one-hour one.
        session_expiry = min(expires_at, now + timedelta(seconds=session_ttl_seconds()))
        max_age = max(1, int((session_expiry - now).total_seconds()))
        cur.execute(
            """
            INSERT INTO app.render_share_exchange_sessions
                (id, share_id, org_id, project_id, session_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                f"rss_{ULID()}",
                share_id,
                org_id,
                project_id,
                session_hash(session_value),
                session_expiry,
            ),
        )
    conn.commit()
    logger.info(
        "render_shares: exchange granted share_id=%s target=%s",
        share_id,
        render_id or dossier_version_id,
    )
    return ExchangeResult(
        session_value=session_value,
        max_age_seconds=max_age,
        next_url=RENDER_SHARE_LANDING_URL,
    )


# ---------------------------------------------------------------------------
# Session resolution -- revalidated against the Share's LIVE state, every call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShareSession:
    share_id: str
    org_id: str
    project_id: str
    #: None when the grant opens a Dossier version -- the recipient then names
    #: which Render of the sequence each call reads, and `session_render_target`
    #: refuses one the version does not carry.
    render_id: str | None
    session_hash: str | None = None
    dossier_version_id: str | None = None


def resolve_session(conn, *, session_value: str | None) -> ShareSession:
    """Resolve a cookie to its Share, refusing unless the Share is STILL usable.

    Validated on EVERY call, not only at exchange. Revocation that only closed the
    door to new exchanges would leave every already-minted session running until it
    expired -- which is not what "revocation ends access immediately" means to the
    person who clicked revoke.
    """
    if not isinstance(session_value, str) or not 32 <= len(session_value) <= 512:
        raise _unavailable()
    try:
        digest = session_hash(session_value)
    except RenderShareValidationError as exc:
        raise _unavailable() from exc
    now = datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT x.share_id, x.org_id, x.project_id, x.expires_at,
                   s.render_id, s.state, s.expires_at, s.dossier_version_id
              FROM app.render_share_exchange_sessions x
              JOIN app.render_shares s
                ON s.id = x.share_id AND s.org_id = x.org_id
               AND s.project_id = x.project_id
             WHERE x.session_hash = %s
            """,
            (digest,),
        )
        row = cur.fetchone()
    if row is None:
        raise _unavailable()
    if _aware_utc(row[3]) <= now:
        raise _unavailable()
    if row[5] != "active" or _aware_utc(row[6]) <= now:
        raise _unavailable()
    return ShareSession(
        share_id=row[0],
        org_id=row[1],
        project_id=row[2],
        render_id=row[4],
        session_hash=digest,
        dossier_version_id=row[7],
    )


# ---------------------------------------------------------------------------
# The frozen payload: written once, at Share creation, from the Project side.
# ---------------------------------------------------------------------------

#: Named so a refusal can point at the exact absent object rather than say
#: "unavailable". `query_execution.py:214-227` sets the precedent: an unavailable
#: outcome names its missing link, always.
MISSING_FROZEN_PAYLOAD = "app.render_frozen_payloads"

_LEGACY_AI_PATH_EVIDENCE = {
    "schema_version": "observed-ai-path.v1",
    "state": "unavailable",
    "reason": "evidence_not_frozen",
}


def freeze_render_payload(conn, *, org_id: str, project_id: str, render_id: str, actor: str) -> str:
    """Copy the bytes this Render was drawn from into the Render's own storage.

    WHY A COPY EXISTS AT ALL. `app.renders` pins the Result's IDENTITY and its
    content hash; the rows live in `app.query_result_payloads`, and AC6.2 grants the
    public reader role NO privilege on that table -- deliberately, because a Share
    that could select from it could select any Result of the Project. So a Share
    that opens a Render needs the Render to own a frozen copy, which is the
    Render-payload relation AC6.2 already enumerates.

    WHY IT CANNOT DRIFT. `app.query_results` and `app.query_result_payloads` are
    insert-once and refuse UPDATE and DELETE by trigger (migration 151), and this
    function REFUSES unless the payload's `content_hash` equals the
    `result_content_hash` the Render pinned. Freezing at share time therefore
    copies exactly the bytes that were frozen at render time, and proves it.

    Every refusal is a `RenderShareValidationError` naming what is missing, raised
    BEFORE the grant exists: the console operator learns immediately, instead of a
    recipient meeting a page with nothing on it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.render_frozen_payloads "
            "WHERE render_id = %s AND org_id = %s AND project_id = %s",
            (render_id, org_id, project_id),
        )
        if cur.fetchone() is not None:
            # Insert-once, and idempotent for a second Share over the same Render.
            return render_id

        cur.execute(
            """
            SELECT result_id, result_content_hash, result_payload_retained,
                   visualization_spec_version_id
              FROM app.renders
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (render_id, org_id, project_id),
        )
        render = cur.fetchone()
        if render is None:
            raise RenderShareValidationError(
                "This Render does not exist in this project, so it cannot be shared."
            )
        result_id, pinned_hash, retained, spec_version_id = render
        if not retained:
            raise RenderShareValidationError(
                "This Render's Result payload was not retained, so a Share would show "
                "no values. Re-run the query to produce a Render whose payload is kept, "
                "then share that one."
            )

        cur.execute(
            """
            SELECT p.content_hash, p.result_schema, p.manifest, p.rows_chunk,
                   r.outcome, r.row_count, r.truncated,
                   r.ai_path_id, r.ai_path_absent_literal
              FROM app.query_result_payloads p
              JOIN app.query_results r
                ON r.id = p.result_id AND r.org_id = p.org_id
               AND r.project_id = p.project_id
             WHERE p.result_id = %s AND p.org_id = %s AND p.project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        stored = cur.fetchone()
        if stored is None:
            raise RenderShareValidationError(
                "The retained Result payload for this Render is not in "
                "app.query_result_payloads, so there is nothing frozen to share."
            )
        (
            stored_hash,
            result_schema,
            result_manifest,
            rows_chunk,
            outcome,
            row_count,
            truncated,
            ai_path_id,
            ai_path_absent_literal,
        ) = stored
        if stored_hash != pinned_hash:
            # Not a theoretical branch: it is the only thing standing between "the
            # same answer" and "an answer that looks the same".
            raise RenderShareValidationError(
                "The retained Result payload no longer matches the content hash this "
                "Render pinned, so a Share over it could not claim to show the same "
                "answer. The Render is not shareable."
            )

        # The immutable Result is the only authority for the identity projected
        # here. A Share request never supplies a path id, and the public replay
        # never invokes this projector: it reads only the frozen JSON below.
        from core.ai_paths import project_observed_ai_path  # noqa: PLC0415

        ai_path_evidence = project_observed_ai_path(
            conn,
            project_id=project_id,
            ai_path=ai_path_id or ai_path_absent_literal,
        )

        cur.execute(
            """
            SELECT spec_contract_version, schema_version, family, spec
              FROM app.visualization_spec_versions
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (spec_version_id, org_id, project_id),
        )
        spec_row = cur.fetchone()
        if spec_row is None:
            raise RenderShareValidationError(
                "The Visualization Spec version this Render pinned is not readable in "
                "this project, so the frozen presentation cannot be composed."
            )
        contract_version, schema_version, family, spec_document = spec_row

        cur.execute(
            """
            INSERT INTO app.render_frozen_payloads
                (render_id, org_id, project_id, result_id, result_content_hash,
                 outcome, result_schema, result_manifest, rows_chunk, row_count,
                 truncated, ai_path_evidence, visualization_spec_version_id,
                 spec_contract_version, schema_version, family, spec_document, frozen_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s,
                    %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (render_id) DO NOTHING
            """,
            (
                render_id,
                org_id,
                project_id,
                result_id,
                stored_hash,
                outcome,
                json.dumps(result_schema),
                json.dumps(result_manifest),
                json.dumps(rows_chunk),
                row_count,
                truncated,
                json.dumps(ai_path_evidence),
                spec_version_id,
                contract_version,
                schema_version,
                family,
                json.dumps(spec_document),
                actor,
            ),
        )
    return render_id


def _evidence_map(datum_evidence_keys: Any) -> dict | None:
    """`datum key -> EvidenceRef`, normalized. An absent mapping stays absent.

    The runtime's `EvidenceRef` is `{evidence_id, label?, source?}`. The Render may
    store either that object or the bare evidence key; both are accepted and the
    bare key is widened rather than dropped, because dropping it would detach a
    datum from its evidence -- which `visualization-and-rendering.md` forbids a
    responsive profile from doing and which a serializer must not do either.
    """
    if not isinstance(datum_evidence_keys, dict) or not datum_evidence_keys:
        return None
    out: dict[str, dict] = {}
    for datum_key, ref in datum_evidence_keys.items():
        if isinstance(ref, str) and ref:
            out[str(datum_key)] = {"evidence_id": ref}
        elif isinstance(ref, dict) and isinstance(ref.get("evidence_id"), str):
            entry = {"evidence_id": ref["evidence_id"]}
            for optional in ("label", "source"):
                if isinstance(ref.get(optional), str):
                    entry[optional] = ref[optional]
            out[str(datum_key)] = entry
    return out or None


def _compose_runtime_input(render_row, payload_row) -> tuple[dict | None, str | None]:
    """Build Story 50.5's `RenderInput`, or say exactly what is missing.

    FIVE KEYS, NO SIXTH. `validate.ts` refuses any other top-level field by name,
    so this function is written as a literal five-key dict rather than a merge:
    a merge is how a stray key arrives and how a valid Render starts rendering as a
    refusal.
    """
    if payload_row is None:
        return None, MISSING_FROZEN_PAYLOAD
    (
        outcome,
        result_schema,
        result_manifest,
        rows_chunk,
        row_count,
        truncated,
        spec_version_id,
        contract_version,
        schema_version,
        spec_document,
    ) = payload_row
    result: dict[str, Any] = {
        "result_id": render_row[1],
        "content_hash": render_row[2],
        "outcome": outcome,
        "schema": result_schema if isinstance(result_schema, dict) else {},
        "rows": rows_chunk if isinstance(rows_chunk, list) else [],
        "manifest": render_app_payload.project_result_manifest(result_manifest),
        "truncated": bool(truncated),
        "row_count": int(row_count),
    }
    evidence = _evidence_map(render_row[13])
    if evidence is not None:
        result["evidence"] = evidence
    return (
        render_app_payload.compose_render_input(
            result=result,
            spec={
                "visualization_spec_version_id": spec_version_id,
                "spec_contract_version": contract_version,
                "schema_version": int(schema_version),
                "document": spec_document,
            },
            pins={
                "theme_version": render_row[8],
                "formatter_version": render_row[9],
                "renderer_build": render_row[6],
                "runtime_build": render_row[7],
            },
            # A Share always opens under its own named profile. The composer is
            # structural only; MCP projection/guards never touch frozen Shares.
            profile=SHARE_RESPONSIVE_PROFILE,
            display=render_row[11] if isinstance(render_row[11], dict) else {},
        ),
        None,
    )


# ---------------------------------------------------------------------------
# The frozen Render, read through the Share and nothing else.
# ---------------------------------------------------------------------------


def dossier_version_render_ids(
    conn, *, org_id: str, project_id: str, dossier_version_id: str
) -> list[str]:
    """The Render ids the pinned Dossier version shows, in document order.

    Refuses -- before any grant exists -- when the version is not this
    Project's: a share request over somebody else's Dossier must die at the
    console operator, not at the recipient.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT blocks FROM app.analysis_dossier_versions
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (dossier_version_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise RenderShareValidationError(
            "This Dossier version does not exist in this project, so it cannot "
            "be shared."
        )
    return [
        str(block.get("render_id"))
        for block in (row[0] or [])
        if isinstance(block, dict) and block.get("kind") == "render"
    ]


def aim_dossier_feedback(conn, session: ShareSession, context_value: object) -> ShareSession:
    """The dossier session, aimed at the figure the submitted context binds.

    The context is the server's own minted handle: its HMAC is verified BEFORE
    any claim is read (an expired one still carries verified claims -- the
    refresh path downstream needs them), so the `render_id` used here cannot be
    forged, only replayed as minted. Membership is then re-read from the pinned
    version's blocks -- the same scope rule `session/dossier` serves under --
    and `record_targeted_feedback` re-verifies every claim against the aimed
    session's own sidecar afterwards. A single-Render session passes through
    untouched.
    """
    if session.dossier_version_id is None:
        return session
    from core.analyze_feedback import (  # noqa: PLC0415
        FeedbackContextError,
        verify_feedback_context,
    )

    try:
        claims = verify_feedback_context(context_value)
    except FeedbackContextError as exc:
        if exc.code != "feedback_context_expired" or not exc.verified_claims:
            raise _unavailable() from exc
        claims = exc.verified_claims
    render_id = str(claims.get("render_id") or "")
    if not render_id:
        raise _unavailable()
    granted = dossier_version_render_ids(
        conn,
        org_id=session.org_id,
        project_id=session.project_id,
        dossier_version_id=session.dossier_version_id,
    )
    if render_id not in granted:
        raise _unavailable()
    from dataclasses import replace as _replace  # noqa: PLC0415

    return _replace(session, render_id=render_id)


def dossier_sequence(conn, session: ShareSession) -> dict:
    """The pinned version's label and ordered blocks, for the share page.

    Serves the SEQUENCE only: each figure's values are then read through
    `session/render?render_id=...`, which goes through `session_render_target`
    below -- so a recipient can never read a Render the version does not show.
    """
    if not session.dossier_version_id:
        raise _unavailable()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT label, version_number, blocks
            FROM app.analysis_dossier_versions
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (session.dossier_version_id, session.org_id, session.project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise _unavailable()
    return {
        "label": row[0],
        "version_number": row[1],
        "blocks": row[2] or [],
    }



def load_frozen_render(conn, session: ShareSession) -> dict:
    """Return the Render's own pins and payload. Nothing is resolved from "current".

    Every identity in the returned envelope comes out of the `app.renders` row that
    was frozen at creation: the Visualization Spec version, the renderer and runtime
    builds, the theme and formatter versions, the responsive profile, the display
    state and the evidence manifest. If the pinned build is not the build serving
    this page, the runtime refuses and shows the accessible table fallback -- it
    does NOT redraw a frozen claim through newer code.

    Note what is NOT read here: `app.query_results` and
    `app.query_result_payloads`. The reader role has no privilege on either, so the
    rows the recipient sees come out of `app.render_frozen_payloads` -- the copy
    this Share froze at creation, whose content hash equals the hash the Render
    pinned. A Share that could reach back into the Result would be the browsing
    grant AD-20 forbids by name.

    `runtime_input` IS THE RUNTIME'S OWN CONTRACT, composed here. Story 50.5's
    `RenderInput` is five fields -- `result`, `spec`, `pins`, `profile`, `display`
    -- and its validator refuses a sixth. Handing the runtime this metadata dict
    instead, which is what the first implementation did, produced a field-by-field
    refusal panel on the recipient's screen: not a chart, not the table fallback,
    not an honest unavailable state. When no frozen payload exists, `runtime_input`
    is None and `missing_link` NAMES what is absent, so the page can say so in
    English rather than render a refusal that reads like a bug.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, result_id, result_content_hash, result_payload_retained,
                   visualization_spec_version_id, renderer_adapter, renderer_build_id,
                   runtime_build_id, theme_version, formatter_version,
                   responsive_profile, display_state, evidence_manifest,
                   datum_evidence_keys, content_hash, created_at
              FROM app.renders
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (session.render_id, session.org_id, session.project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise _unavailable()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT outcome, result_schema, result_manifest, rows_chunk, row_count,
                   truncated, visualization_spec_version_id, spec_contract_version,
                   schema_version, spec_document, ai_path_evidence
              FROM app.render_frozen_payloads
             WHERE render_id = %s AND org_id = %s AND project_id = %s
            """,
            (session.render_id, session.org_id, session.project_id),
        )
        payload = cur.fetchone()
    runtime_input, missing_link = _compose_runtime_input(
        row, payload[:10] if payload is not None else None
    )
    ai_path_evidence = payload[10] if payload is not None else None
    return {
        "runtime_input": runtime_input,
        "ai_path_evidence": (
            ai_path_evidence
            if isinstance(ai_path_evidence, dict)
            else dict(_LEGACY_AI_PATH_EVIDENCE)
        ),
        "missing_link": missing_link,
        "render_id": row[0],
        "result_id": row[1],
        "result_content_hash": row[2],
        "result_payload_retained": row[3],
        "visualization_spec_version_id": row[4],
        "renderer_adapter": row[5],
        "renderer_build": row[6],
        "runtime_build": row[7],
        "theme_version": row[8],
        "formatter_version": row[9],
        # The Render may have been created for the Console; a SHARE is always
        # opened under the `share` profile. `visualization-and-rendering.md:221-224`
        # allows a profile to rearrange controls, labels and legends -- never to
        # drop a disclosed filter, alter a colour's semantic direction, change data
        # or detach evidence.
        "responsive_profile": SHARE_RESPONSIVE_PROFILE,
        "render_responsive_profile": row[10],
        "display_state": row[11],
        "evidence_manifest": row[12],
        "datum_evidence_keys": row[13],
        "content_hash": row[14],
        "rendered_at": _aware_utc(row[15]).isoformat(),
        # Private producer input for exact feedback.  It is never copied into the
        # session JSON; it exists so target validation uses the immutable frozen
        # bytes rather than re-reading Result, Spec or AI Path owners.
        "_feedback_authority": (
            {
                "result_schema": payload[1],
                "result_manifest": payload[2],
                "rows_chunk": payload[3],
                "spec_document": payload[9],
            }
            if payload is not None
            else None
        ),
    }


def _share_feedback_projection(frozen: Mapping[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    """Return canonical claims and rows from the immutable Share snapshot only."""
    from core.analyze_feedback import feedback_fields_from_visualization_spec

    authority = frozen.get("_feedback_authority")
    runtime = frozen.get("runtime_input")
    if not isinstance(authority, Mapping) or not isinstance(runtime, Mapping):
        raise _unavailable()
    result = runtime.get("result")
    if not isinstance(result, Mapping):
        raise _unavailable()
    rows = result.get("rows")
    stored_rows = authority.get("rows_chunk")
    if not isinstance(rows, list) or not isinstance(stored_rows, list) or rows != stored_rows:
        raise _unavailable()
    fields = feedback_fields_from_visualization_spec(
        authority.get("result_schema"),
        authority.get("spec_document"),
        authority.get("result_manifest"),
    )
    path = frozen.get("ai_path_evidence")
    path_id: str | None = None
    ordinals: list[int] | None = None
    if (
        isinstance(path, Mapping)
        and path.get("lifecycle") == "finalized"
        and path.get("state") in {"completed", "failed"}
        and isinstance(path.get("path_id"), str)
        and isinstance(path.get("steps"), list)
    ):
        path_id = path["path_id"]
        ordinals = sorted(
            {
                step["ordinal"]
                for step in path["steps"]
                if isinstance(step, Mapping)
                and isinstance(step.get("ordinal"), int)
                and not isinstance(step.get("ordinal"), bool)
                and step["ordinal"] >= 0
            }
        )
    claims = {
        "org_id": frozen.get("org_id"),
        "project_id": frozen.get("project_id"),
        "surface": "share",
        "result_id": frozen.get("result_id"),
        "result_content_hash": frozen.get("result_content_hash"),
        "delivered_rows": {"start": 0, "count": len(rows), "fields": fields},
        "render_id": frozen.get("render_id"),
        "visualization_spec_version_id": frozen.get("visualization_spec_version_id"),
        "renderer_build_id": frozen.get("renderer_build"),
        "runtime_build_id": frozen.get("runtime_build"),
        "theme_version": frozen.get("theme_version"),
        "formatter_version": frozen.get("formatter_version"),
        "ai_path_id": path_id,
        "path_step_ordinals": ordinals,
    }
    return claims, rows


def _share_feedback_sidecar(
    session: ShareSession, frozen: Mapping[str, Any]
) -> dict[str, str]:
    """Reconstruct signed authority from frozen bytes without writing eligibility."""
    from core.analyze_feedback import (
        feedback_context_secret,
        mint_delivery_feedback_context,
        mint_feedback_context,
        verify_feedback_context,
    )

    if session.session_hash is None:
        raise _unavailable()

    claims, rows = _share_feedback_projection(
        {**frozen, "org_id": session.org_id, "project_id": session.project_id}
    )
    sidecar = mint_delivery_feedback_context(
        org_id=session.org_id,
        project_id=session.project_id,
        result_id=str(claims["result_id"]),
        result_content_hash=str(claims["result_content_hash"]),
        surface="share",
        stored_rows=rows,
        delivered_rows=rows,
        delivered_fields=list(claims["delivered_rows"]["fields"]),
        pins={
            name: claims[name]
            for name in (
                "render_id",
                "visualization_spec_version_id",
                "renderer_build_id",
                "runtime_build_id",
                "theme_version",
                "formatter_version",
            )
        },
        ai_path_id=claims["ai_path_id"],
        path_step_ordinals=claims["path_step_ordinals"],
    )
    signed = verify_feedback_context(sidecar)
    interaction_bytes = hmac.new(
        feedback_context_secret(),
        f"share-session:{session.session_hash}".encode(),
        hashlib.sha256,
    ).digest()[:16]
    signed["interaction_ref"] = f"afi_{ULID(interaction_bytes)}"
    return mint_feedback_context(signed)


def mint_share_feedback_context(
    conn, session: ShareSession, frozen: Mapping[str, Any]
) -> dict[str, str] | None:
    """Emit Share feedback authority only after its definer recorder succeeds."""
    from core.analyze_feedback import FeedbackContextError, verify_feedback_context
    from core.feedback_review import record_share_feedback_eligibility

    try:
        sidecar = _share_feedback_sidecar(session, frozen)
        claims = verify_feedback_context(sidecar)
    except FeedbackContextError:
        return None
    recorded = record_share_feedback_eligibility(
        conn,
        session_hash=str(session.session_hash),
        interaction_ref=str(claims["interaction_ref"]),
    )
    return sidecar if recorded is not None else None


def _assert_share_feedback_authority(
    session: ShareSession, frozen: Mapping[str, Any], claims: Mapping[str, Any]
) -> None:
    from core.analyze_feedback import verify_feedback_context

    expected = verify_feedback_context(_share_feedback_sidecar(session, frozen))
    compared = (
        "org_id",
        "project_id",
        "surface",
        "result_id",
        "result_content_hash",
        "delivered_rows",
        "render_id",
        "visualization_spec_version_id",
        "renderer_build_id",
        "runtime_build_id",
        "theme_version",
        "formatter_version",
        "ai_path_id",
        "path_step_ordinals",
        "interaction_ref",
    )
    if any(claims.get(name) != expected.get(name) for name in compared):
        raise _unavailable()


def record_targeted_feedback(
    conn,
    session: ShareSession,
    frozen: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Write one exact anonymous Share target through the narrow DB function."""
    from core.analyze_feedback import (
        FeedbackContextError,
        mint_feedback_context,
        normalize_feedback_request,
        request_fingerprint,
        verify_feedback_context,
    )

    if not isinstance(payload, Mapping) or set(payload) != {
        "context",
        "target",
        "polarity",
        "comment",
        "retry_key",
    }:
        raise _unavailable()
    try:
        claims = verify_feedback_context(payload["context"])
    except FeedbackContextError as exc:
        if exc.code != "feedback_context_expired" or not exc.verified_claims:
            raise _unavailable() from exc
        _assert_share_feedback_authority(session, frozen, exc.verified_claims)
        return {
            "schema_version": "exact-feedback-receipt.v1",
            "status": "refresh_required",
            "code": "feedback_context_expired",
            "message": "The feedback context expired. Retry with the refreshed context.",
            "feedback_context": mint_feedback_context(exc.verified_claims),
        }
    _assert_share_feedback_authority(session, frozen, claims)
    try:
        command = normalize_feedback_request(
            claims=claims,
            target=payload["target"],
            polarity=payload["polarity"],
            comment=payload["comment"],
            retry_key=payload["retry_key"],
        )
        retry_hash, request_hash = request_fingerprint(command, claims=claims)
    except FeedbackContextError as exc:
        raise _unavailable() from exc
    if session.session_hash is None:
        raise _unavailable()
    target = command["target"]
    feedback_id = f"rsfb_{ULID()}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, feedback_id
                  FROM app.record_render_share_feedback_v1(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                  )
                """,
                (
                    session.session_hash,
                    feedback_id,
                    claims["interaction_ref"],
                    target["kind"],
                    target.get("row_index"),
                    target.get("field"),
                    target.get("ordinal"),
                    command["polarity"],
                    command["comment"],
                    retry_hash,
                    request_hash,
                ),
            )
            row = cur.fetchone()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505" and "feedback_retry_conflict" in str(exc):
            raise RenderShareConflict("feedback retry conflict") from exc
        raise _unavailable() from exc
    if row is not None and row[0] == "conflict":
        raise RenderShareConflict("feedback retry conflict")
    if row is None or row[0] not in {"recorded", "replayed"}:
        raise _unavailable()
    return {
        "schema_version": "exact-feedback-receipt.v1",
        "status": row[0],
        "feedback_id": row[1],
        "interaction_ref": claims["interaction_ref"],
        "target": target,
    }


def share_disclosure(conn, session: ShareSession) -> dict:
    """The `Shared on` / `Data as of` facts, taken from frozen rows only.

    `Shared on` is the SHARE's creation date -- never "now", which is what a page
    that renders `new Date()` silently tells a recipient. `Data as of` comes out of
    the Render's own manifest and is never recomputed: recomputing it would make a
    frozen artifact claim a freshness it does not have.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT created_at, expires_at FROM app.render_shares WHERE id = %s",
            (session.share_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise _unavailable()
    return {
        "shared_on": _aware_utc(row[0]).isoformat(),
        "expires_at": _aware_utc(row[1]).isoformat(),
    }


def record_feedback(
    conn,
    session: ShareSession,
    frozen: dict,
    *,
    polarity: str,
    comment: str | None,
    selected_datum_key: str | None,
) -> str:
    """Persist ONE feedback row with every version identity pinned, all non-null.

    The actor is the Share grant, pseudonymously. A public recipient has no
    identity and none is invented -- which is why `app.feedback` (with its
    `created_by TEXT NOT NULL`) is not reused. Epic 51 reads these rows; this story
    writes them and stops.

    The comment is stored verbatim and never interpolated into an executable
    prompt, a tool description or HTML (AD-31). Its bound is a database CHECK, not
    a promise made here.
    """
    if polarity not in {"helpful", "not_helpful"}:
        raise _unavailable()
    if comment is not None:
        if not isinstance(comment, str):
            raise _unavailable()
        comment = comment.strip() or None
        if comment is not None and len(comment) > 2000:
            raise _unavailable()
    if selected_datum_key is not None and (
        not isinstance(selected_datum_key, str) or len(selected_datum_key) > 256
    ):
        raise _unavailable()

    manifest = frozen.get("evidence_manifest")
    manifest_hash = _evidence_manifest_hash(manifest)
    feedback_id = f"rsfb_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.render_share_feedback
                (id, share_id, org_id, project_id, render_id, result_id,
                 visualization_spec_version_id, renderer_build, runtime_build,
                 theme_version, formatter_version, responsive_profile,
                 evidence_manifest_hash, selected_datum_key, polarity, comment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                feedback_id,
                session.share_id,
                session.org_id,
                session.project_id,
                session.render_id,
                frozen["result_id"],
                frozen["visualization_spec_version_id"],
                frozen["renderer_build"],
                frozen["runtime_build"],
                frozen["theme_version"],
                frozen["formatter_version"],
                SHARE_RESPONSIVE_PROFILE,
                manifest_hash,
                selected_datum_key,
                polarity,
                comment,
            ),
        )
    return feedback_id


def _evidence_manifest_hash(manifest: Any) -> str:
    """sha256 of the frozen evidence manifest, canonically serialized.

    A hash rather than a copy: the feedback row must be able to say WHICH evidence
    manifest the recipient was looking at, without duplicating a document that the
    Render already owns and that must not diverge from it.
    """
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Rate limiting, keyed on HASHES.
# ---------------------------------------------------------------------------

_RATE_BUCKETS: dict[str, list[float]] = {}


def _rate_window() -> tuple[int, float]:
    return (
        _int_env("TOOROW_RENDER_SHARE_RATE_LIMIT", 30, low=1, high=10000),
        float(_int_env("TOOROW_RENDER_SHARE_RATE_WINDOW_SECONDS", 60, low=1, high=3600)),
    )


def check_rate_limit(*, ip_hash: str | None, subject_hash: str | None) -> tuple[bool, float]:
    """Return (allowed, retry_after_seconds), keyed on hashed identities only.

    Never on a prefix of the plaintext bearer. `rendus_api.py:82` and
    `admin_api.py#_shared_notebook_endpoint` both key on `token[:8]`, which puts eight characters of
    a live bearer into a process-memory dictionary and, from there, into every heap
    dump and every debugger session.

    The key is the HMAC's prefix, not the bearer's: it identifies the same caller
    across a burst without being reversible, and it is already a digest of a digest
    from the attacker's point of view.
    """
    limit, window = _rate_window()
    key = f"{ip_hash or 'no-ip'}|{(subject_hash or 'no-subject')[:16]}"
    now = time.monotonic()
    bucket = [t for t in _RATE_BUCKETS.get(key, []) if now - t < window]
    if len(bucket) >= limit:
        _RATE_BUCKETS[key] = bucket
        return False, max(0.0, window - (now - bucket[0]))
    bucket.append(now)
    _RATE_BUCKETS[key] = bucket
    if len(_RATE_BUCKETS) > 10000:
        # Bounded: an unbounded dict keyed on caller identity is a memory
        # amplification an anonymous caller controls.
        for stale_key in [k for k, v in _RATE_BUCKETS.items() if not v or now - v[-1] > window]:
            _RATE_BUCKETS.pop(stale_key, None)
    return True, 0.0


def reset_rate_limits() -> None:
    """Test seam. Production never calls it."""
    _RATE_BUCKETS.clear()


__all__: list[str] = [
    "MISSING_FROZEN_PAYLOAD",
    "RENDER_SHARE_COOKIE",
    "RENDER_SHARE_COOKIE_PATH",
    "RENDER_SHARE_LANDING_URL",
    "SHARE_RESPONSIVE_PROFILE",
    "ExchangeResult",
    "RenderShareConflict",
    "RenderShareUnavailable",
    "RenderShareValidationError",
    "ShareCreation",
    "ShareSession",
    "append_access_event",
    "bearer_hash",
    "build_fragment_delivery_url",
    "check_rate_limit",
    "classify_client",
    "client_ip_hash",
    "create_share",
    "exchange_attempt_ceiling",
    "exchange_bearer",
    "freeze_render_payload",
    "list_shares",
    "load_frozen_render",
    "max_share_lifetime_days",
    "mint_bearer",
    "mint_share_feedback_context",
    "record_feedback",
    "record_targeted_feedback",
    "render_share_connection",
    "reset_rate_limits",
    "resolve_session",
    "revoke_share",
    "session_hash",
    "session_ttl_seconds",
    "share_disclosure",
]

# Kept out of the public surface deliberately: `Callable` is imported only for the
# mutation signatures above.
_ = Callable
