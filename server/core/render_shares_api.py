"""Story 50.7 -- the PUBLIC Render Share surface. Nothing authenticated lives here.

THE BEARER NEVER TOUCHES A URL. Not the path, not the query string. It arrives in
the URL FRAGMENT, which browsers do not send to servers, is removed by
`history.replaceState` before the first network access of any kind, and is
delivered once in a same-origin `POST` body. The retired shape --
`GET /api/rendus/shared/{token}` and `GET /api/notebooks/shared/{token}` -- put a
live bearer in the request line, where it lands in the browser history, the
referrer, every proxy log and the ASGI access log BEFORE a line of application code
runs. That is why redacting a handler could never have fixed it (AD-30,
`ARCHITECTURE-SPINE.md:227-231`).

ROUTE ORDER IS LOAD-BEARING. Starlette matches in declaration order, so literal
segments precede parameterized ones. `query_specs_api.py:433-452` is the in-repo
precedent and says the same thing.

WHAT THE PUBLIC HANDLERS CANNOT DO, and it is enforced in three places rather than
promised in one:

  1. No handler below reads a `project_id`, `org_id`, `render_id`, `result_id` or
     `query_spec_id` from the request. Every identity is derived from the session
     cookie. Grep this file for `path_params` on an `/api/render-shares/` route:
     there are none.
  2. Every public handler opens `core.render_shares.render_share_connection()`,
     which issues `SET LOCAL ROLE toorow_share_reader` inside its transaction. That
     role has NO privilege on `app.query_results`, `app.query_specs*`,
     `app.projects` or `app.organizations`, so a stray SELECT is refused by
     PostgreSQL with SQLSTATE 42501.
  3. This module imports neither `core.query_execution`, `core.query_specs`,
     `core.warehouse`, `core.reports`, `core.cards` nor `core.main` -- asserted by
     test on the transitive import set, because an import is how the second and
     third layers get quietly bypassed.

ONE RUNTIME, MOUNTED -- NOT REIMPLEMENTED. `/share/view` serves the Story 50.5
share bundle (`ui/cards/shell/dist/viz/share.html`, built from
`src/viz/entries/share.main.tsx`) with the frozen Render injected into
`window.__TOOROW_FROZEN_RENDER__`, which is the global that entry already reads.
The accessible table fallback, the unavailable-build state and every renderer live
in that bundle. This module adds the surrounding disclosure chrome and export
action, then publishes frozen feedback authority before mount; the shared runtime
owns the sole feedback composer. Nothing here draws data.

DIVERGENCE FROM THE STORY, DECLARED RATHER THAN APPLIED IN SILENCE (CLAUDE.md §2).
Task 5 and decision D14 call for a new `ui/share/` package with its own Vite build.
It is NOT created. D14's stated reason for a separate package was that "a public
bundle that can import the Console's API client is one careless refactor away from
calling /api/projects/...; a package boundary makes that a compile error". Story
50.5 landed that boundary already, in a different place: the share entry lives in
`@toorow/card-shell`, which does not depend on `@toorow/admin` at all, and its
bundle is emitted single-file and verified self-contained. Adding `ui/share/` on
top would be a THIRD package whose only content is a second copy of the chrome --
and, since the table fallback belongs to the runtime, a strong invitation to write
the second rendering path the architecture forbids. The invariant D14 protects is
satisfied; the package it proposed is not built.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.render_shares import (  # noqa: I001
    RENDER_SHARE_COOKIE,
    RENDER_SHARE_COOKIE_PATH,
    RenderShareConflict,
    RenderShareUnavailable,
    RenderShareValidationError,
    aim_dossier_feedback,
    append_access_event,
    check_rate_limit,
    classify_client,
    client_ip_hash,
    dossier_sequence,
    exchange_bearer,
    load_frozen_render,
    mint_share_feedback_context,
    record_targeted_feedback,
    render_share_connection,
    resolve_session,
    session_hash,
    share_disclosure,
)

logger = logging.getLogger(__name__)

#: THE common envelope. Unknown, revoked, expired, consumed, foreign and absent all
#: answer with this exact object and this exact status. A recipient learns that the
#: link does not open; they learn nothing about why, which is the difference between
#: a refusal and an enumeration oracle.
_UNAVAILABLE = {
    "code": "share_unavailable",
    "message": "This shared result is not available.",
}

_RATE_LIMITED = {
    "code": "share_unavailable",
    "message": "This shared result is not available.",
}

_REPO_ROOT = Path(__file__).parent.parent.parent
_SHARE_BUNDLE = _REPO_ROOT / "ui" / "cards" / "shell" / "dist" / "viz" / "share.html"


def share_bundle_path() -> Path:
    override = os.environ.get("TOOROW_RENDER_SHARE_BUNDLE")
    return Path(override) if override else _SHARE_BUNDLE


# ---------------------------------------------------------------------------
# Headers. The same set on success AND on every error, because a header set that
# differs by outcome is itself a signal.
# ---------------------------------------------------------------------------


def _security_headers(nonce: str | None = None, *, allow_styles: bool = False) -> dict[str, str]:
    if nonce is None:
        script_src = "'none'"
        style_src = "'none'"
    else:
        script_src = "'nonce-" + nonce + "'"
        style_src = ("'nonce-" + nonce + "'") if allow_styles else "'none'"
    # `data:` is the canvas the ECharts adapter draws into, and it is allowed ONLY
    # on the page that actually mounts the runtime. No remote origin appears in any
    # directive on either page, and no analytics, tag manager, unfurl hint or
    # prefetch hint is emitted anywhere on this surface.
    img_src = "'self' data:" if allow_styles else "'none'"
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            f"default-src 'none'; script-src {script_src}; "
            f"connect-src 'self'; style-src {style_src}; img-src {img_src}; "
            "font-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'none'"
        ),
    }


def _with_headers(response: Response, nonce: str | None = None, *, allow_styles: bool = False):
    for key, value in _security_headers(nonce, allow_styles=allow_styles).items():
        response.headers[key] = value
    return response


def _unavailable_response(status: int = 404) -> Response:
    return _with_headers(JSONResponse(_UNAVAILABLE, status_code=status))


def _client_ip(request: Request) -> str | None:
    client = request.client
    return client.host if client else None


def _client_class(request: Request) -> str:
    return classify_client(request.headers.get("user-agent"))


# ---------------------------------------------------------------------------
# GET /share -- the AD-30 bootstrap shell.
# ---------------------------------------------------------------------------

#: The ordering property this page exists to guarantee is asserted directly by
#: `ui`-side test AND readable here: `history.replaceState` is the FIRST statement
#: after the hash is read, and it precedes the `fetch` textually and temporally. No
#: image, font, preconnect, prefetch or beacon appears anywhere in this document,
#: so there is no earlier network access for the fragment to leak into.
_BOOTSTRAP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Opening a shared result</title>
</head>
<body>
<main>
<h1>Opening a shared result</h1>
<p id="state" role="status">Opening the shared result&hellip;</p>
<noscript>This shared result needs JavaScript to open securely.</noscript>
</main>
<script nonce="__NONCE__">
'use strict';
(function () {
  var hash = window.location.hash || '';
  var bearer = hash.indexOf('#render=') === 0 ? hash.slice(8) : '';
  // FIRST, before any network access of any kind. Nothing above this line
  // fetches, preloads, preconnects or requests an image.
  window.history.replaceState(null, '', window.location.pathname);
  var state = document.getElementById('state');
  function stop(message) { state.textContent = message; }
  if (!bearer) {
    stop('This link is incomplete. Ask the sender for a new one.');
    return;
  }
  fetch('/api/render-shares/exchange', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bearer: bearer })
  }).then(function (response) {
    if (!response.ok) {
      stop('This shared result is not available. It may have expired, been '
         + 'revoked, or already been opened. Ask the sender for a new link.');
      return null;
    }
    return response.json();
  }).then(function (body) {
    if (body && typeof body.next_url === 'string') {
      window.location.replace(body.next_url);
    }
  }).catch(function () {
    stop('This shared result could not be opened. Ask the sender for a new link.');
  });
})();
</script>
</body>
</html>
"""


async def _share_bootstrap(_request: Request) -> Response:
    nonce = secrets.token_urlsafe(18)
    return _with_headers(
        Response(
            _BOOTSTRAP_HTML.replace("__NONCE__", nonce),
            media_type="text/html; charset=utf-8",
        ),
        nonce,
    )


# ---------------------------------------------------------------------------
# POST /api/render-shares/exchange
# ---------------------------------------------------------------------------


async def _exchange(request: Request) -> Response:
    ip_hash = client_ip_hash(_client_ip(request))
    client_class = _client_class(request)

    # Malformed body and wrong content type answer the SAME envelope as an unknown
    # bearer, and answer it before any database work.
    try:
        body = await request.json()
    except Exception:
        return _unavailable_response()
    if not isinstance(body, dict):
        return _unavailable_response()
    bearer = body.get("bearer")
    if not isinstance(bearer, str):
        return _unavailable_response()

    try:
        subject = session_hash(bearer)
    except RenderShareValidationError:
        # A missing or short pepper is a DEPLOYMENT fault, not a caller fault. It
        # must not be reported as one, and it must not be silently treated as a
        # denial either -- so it is logged loudly here and answered with the common
        # envelope, which is the only thing safe to show a public caller.
        logger.error("render_shares_api: TOOROW_RENDER_SHARE_PEPPER is not configured")
        return _unavailable_response()

    allowed, retry_after = check_rate_limit(ip_hash=ip_hash, subject_hash=subject)
    if not allowed:
        try:
            with render_share_connection() as conn:
                append_access_event(
                    conn,
                    share_id=None,
                    org_id=None,
                    project_id=None,
                    event="rate_limited",
                    outcome="refused",
                    reason_code="bearer_unknown",
                    ip_hash=ip_hash,
                    client_class=client_class,
                )
                conn.commit()
        except Exception:
            logger.warning("render_shares_api: could not append rate-limit evidence")
        response = _with_headers(JSONResponse(_RATE_LIMITED, status_code=429))
        response.headers["Retry-After"] = str(max(1, int(retry_after) + 1))
        return response

    try:
        with render_share_connection() as conn:
            exchanged = exchange_bearer(
                conn, bearer=bearer, ip_hash=ip_hash, client_class=client_class
            )
    except RenderShareUnavailable:
        return _unavailable_response()
    except Exception as exc:  # noqa: BLE001
        # Never leak an exception message on a public route, and never let an
        # infrastructure fault look different from a denial. The TYPE goes to
        # the log, as `invitations_api` does: on 2026-09-04 a dossier share
        # failed here for forty minutes and this line said nothing to look at.
        logger.error(
            "render_shares_api: exchange failed with an unexpected error: %s",
            type(exc).__name__,
        )
        return _unavailable_response()

    response = _with_headers(
        JSONResponse(
            # Nothing here identifies the Project, the organization, the Result or
            # the Render by an id a recipient could use anywhere else.
            {"next_url": exchanged.next_url},
            status_code=200,
        )
    )
    response.set_cookie(
        RENDER_SHARE_COOKIE,
        exchanged.session_value,
        max_age=exchanged.max_age_seconds,
        path=RENDER_SHARE_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


# ---------------------------------------------------------------------------
# The session-scoped reads. Identity comes from the cookie and only the cookie.
# ---------------------------------------------------------------------------


def _session_or_none(request: Request):
    """Resolve the cookie, or return None. Never raises to the caller."""
    return request.cookies.get(RENDER_SHARE_COOKIE)


async def _session_render(request: Request) -> Response:
    ip_hash = client_ip_hash(_client_ip(request))
    client_class = _client_class(request)
    try:
        with render_share_connection() as conn:
            session = resolve_session(conn, session_value=_session_or_none(request))
            frozen = load_frozen_render(conn, session)
            feedback_context = (
                mint_share_feedback_context(conn, session, frozen)
                if frozen.get("runtime_input") is not None
                else None
            )
            disclosure = share_disclosure(conn, session)
            append_access_event(
                conn,
                share_id=session.share_id,
                org_id=session.org_id,
                project_id=session.project_id,
                event="read",
                outcome="granted",
                reason_code="granted",
                ip_hash=ip_hash,
                client_class=client_class,
            )
            conn.commit()
    except RenderShareUnavailable:
        return _unavailable_response(401)
    except Exception:
        logger.error("render_shares_api: session render read failed")
        return _unavailable_response(401)
    # `render` IS Story 50.5's `RenderInput` -- the five-field envelope
    # `ui/cards/shell/src/viz/entries/share.tsx` reads out of
    # `window.__TOOROW_FROZEN_RENDER__` and hands straight to the runtime. It used
    # to be this module's metadata dict, which the runtime's validator refused
    # field by field: the recipient met a refusal panel instead of a chart, the
    # table fallback or an honest unavailable state.
    #
    # When nothing was frozen, `render` is null and `unavailable` NAMES the missing
    # link, so the page says what is absent rather than rendering a refusal that
    # reads like a bug.
    body: dict = {
        "render": frozen.get("runtime_input"),
        # Adjacent application data. It is deliberately outside the five-field
        # RenderInput, whose validator rejects a sixth field by name.
        "ai_path_evidence": frozen.get("ai_path_evidence"),
        "disclosure": disclosure,
        # Formatted server-side by the SAME function the export uses, so the page
        # and the exported file can never make different claims about the same
        # frozen artifact. The page renders these with `textContent`, so there is
        # no escaping question on the client.
        "facts": [
            {"label": label, "value": value}
            for label, value in _disclosure_rows(frozen, disclosure)
        ],
    }
    if feedback_context is not None:
        body["feedback_context"] = feedback_context
    if frozen.get("runtime_input") is None:
        body["unavailable"] = {
            "title": "This shared result carries no frozen values",
            "detail": (
                "The Render this link opens has no frozen payload stored with it, so "
                "there is nothing to draw. Nothing has been substituted for it."
            ),
            "missing_link": frozen.get("missing_link"),
        }
    return _with_headers(JSONResponse(body))


async def _session_dossier(request: Request) -> Response:
    """GET /api/render-shares/session/dossier -- the granted sequence (73-2).

    Serves the pinned version's label and ordered blocks, never a figure's
    values: each render block is then read through `session/render?render_id=`,
    which re-checks membership against the version on every call. A session
    whose grant opens a single Render answers 401-unavailable here, exactly as
    a dossier session would on a render call that names no figure.
    """
    ip_hash = client_ip_hash(_client_ip(request))
    client_class = _client_class(request)
    try:
        with render_share_connection() as conn:
            session = resolve_session(conn, session_value=_session_or_none(request))
            sequence = dossier_sequence(conn, session)
            #  Each figure's frozen envelope rides INLINE. The Render identities
            #  come from the pinned version's own blocks -- the retirement rule
            #  of this surface (no object identifier read from the request)
            #  holds, and the page makes ONE round trip for the whole document.
            #  Figure-level feedback rides inline too, one minted context PER
            #  figure (73-2, last half). Each context binds the Render its own
            #  block names, so the server aims the write at that figure from a
            #  verified handle and never from a request parameter -- which is
            #  what lets one page praise one figure and fault another
            #  (`ui/cards/shell/src/viz/entries/share.tsx:139-184`).
            from dataclasses import replace as _replace  # noqa: PLC0415

            for block in sequence["blocks"]:
                if not isinstance(block, dict) or block.get("kind") != "render":
                    continue
                aimed = _replace(session, render_id=str(block.get("render_id")))
                frozen = load_frozen_render(conn, aimed)
                block["render"] = frozen.get("runtime_input")
                block["ai_path_evidence"] = frozen.get("ai_path_evidence")
                #  The provenance the amendment wants carried onto paper, per
                #  figure, formatted by the SAME function the single page and
                #  the export use.
                disclosure = share_disclosure(conn, aimed)
                block["facts"] = [
                    {"label": label, "value": value}
                    for label, value in _disclosure_rows(frozen, disclosure)
                ]
                block["feedback_context"] = (
                    mint_share_feedback_context(conn, aimed, frozen)
                    if frozen.get("runtime_input") is not None
                    else None
                )
                if frozen.get("runtime_input") is None:
                    block["unavailable"] = {
                        "title": "This shared figure carries no frozen values",
                        "missing_link": frozen.get("missing_link"),
                    }
            append_access_event(
                conn,
                share_id=session.share_id,
                org_id=session.org_id,
                project_id=session.project_id,
                event="read",
                outcome="granted",
                reason_code="granted",
                ip_hash=ip_hash,
                client_class=client_class,
            )
            conn.commit()
    except RenderShareUnavailable:
        return _unavailable_response(401)
    except Exception:
        logger.error("render_shares_api: session dossier read failed")
        return _unavailable_response(401)
    return _with_headers(JSONResponse({"dossier": sequence}))


async def _session_rows(request: Request) -> Response:
    """Bounded paging over the Render's OWN frozen payload chunks (D10).

    It does not reach a Result. `app.query_results` is unreadable through this
    connection by role, so a Render whose payload was not retained answers with an
    honest not-retained state rather than silently re-reading live data.
    """
    ip_hash = client_ip_hash(_client_ip(request))
    client_class = _client_class(request)
    try:
        with render_share_connection() as conn:
            session = resolve_session(conn, session_value=_session_or_none(request))
            frozen = load_frozen_render(conn, session)
            append_access_event(
                conn,
                share_id=session.share_id,
                org_id=session.org_id,
                project_id=session.project_id,
                event="rows",
                outcome="granted",
                reason_code="granted",
                ip_hash=ip_hash,
                client_class=client_class,
            )
            conn.commit()
    except RenderShareUnavailable:
        return _unavailable_response(401)
    except Exception:
        logger.error("render_shares_api: session rows read failed")
        return _unavailable_response(401)
    return _with_headers(
        JSONResponse(
            {
                "result_payload_retained": bool(frozen.get("result_payload_retained")),
                "display_state": frozen.get("display_state"),
                "next_cursor": None,
            }
        )
    )


def _build_dossier_export_document(sequence: dict, figures: list[dict]) -> str:
    """The dossier as ONE self-contained file: no script, zero network requests.

    The same parts as the single-result export, once per figure and in document
    order -- the disclosure facts by `_disclosure_block`'s own formatting, the
    values by `_export_table`, the narrative verbatim as text. A file built from
    any other source would be the second rendering path the amendment forbids.
    """
    parts: list[str] = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="referrer" content="no-referrer">',
        "<title>Shared dossier (export)</title></head><body><main>",
        f"<h1>{html.escape(str(sequence.get('label') or 'Shared dossier'))}</h1>",
        "<p>This is an export of a frozen dossier. It contains no live data and "
        "makes no network requests.</p>",
    ]
    figure_number = 0
    for block in sequence.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if block.get("kind") == "narrative":
            parts.append(f"<p>{html.escape(str(block.get('text') or ''))}</p>")
            continue
        figure_number += 1
        figure = figures[figure_number - 1] if figure_number <= len(figures) else {}
        frozen = figure.get("frozen") or {}
        disclosure = figure.get("disclosure") or {}
        parts.append(f"<section><h2>Figure {figure_number}</h2>")
        parts.append(_disclosure_block(frozen, disclosure))
        parts.append(_export_table(frozen.get("runtime_input")))
        parts.append("</section>")
    parts.append("</main></body></html>")
    return "".join(parts)


async def _session_export(request: Request) -> Response:
    """A DISTINCT action over the frozen Render (AD-20 O1, AC11).

    The file is built only from the frozen payload, its pinned presentation and its
    evidence manifest: no query, no execution, no evidence resolution against a live
    owner. It carries no bearer, no session value, no share URL, and no Project,
    organization, Result, Query Spec or Datastream identifier -- and opening it
    performs zero network requests, because it embeds no script and no remote
    reference at all.
    """
    ip_hash = client_ip_hash(_client_ip(request))
    client_class = _client_class(request)
    try:
        with render_share_connection() as conn:
            session = resolve_session(conn, session_value=_session_or_none(request))
            if session.dossier_version_id is not None:
                # 73-3: the dossier exports as ONE file, figure by figure, in
                # document order -- the identities come from the pinned version's
                # own blocks, exactly as `session/dossier` reads them.
                from dataclasses import replace as _replace  # noqa: PLC0415

                sequence = dossier_sequence(conn, session)
                figures = []
                for block in sequence.get("blocks") or []:
                    if not isinstance(block, dict) or block.get("kind") != "render":
                        continue
                    aimed = _replace(session, render_id=str(block.get("render_id")))
                    figures.append(
                        {
                            "frozen": load_frozen_render(conn, aimed),
                            "disclosure": share_disclosure(conn, aimed),
                        }
                    )
            else:
                frozen = load_frozen_render(conn, session)
                disclosure = share_disclosure(conn, session)
            append_access_event(
                conn,
                share_id=session.share_id,
                org_id=session.org_id,
                project_id=session.project_id,
                event="exported",
                outcome="granted",
                reason_code="granted",
                ip_hash=ip_hash,
                client_class=client_class,
            )
            conn.commit()
    except RenderShareUnavailable:
        return _unavailable_response(401)
    except Exception:
        logger.error("render_shares_api: export failed")
        return _unavailable_response(401)

    if session.dossier_version_id is not None:
        document = _build_dossier_export_document(sequence, figures)
        filename = "shared-dossier.html"
    else:
        document = _build_export_document(frozen, disclosure)
        filename = "shared-result.html"
    response = _with_headers(Response(document, media_type="text/html; charset=utf-8"))
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


async def _session_feedback(request: Request) -> Response:
    ip_hash = client_ip_hash(_client_ip(request))
    client_class = _client_class(request)
    try:
        body = await request.json()
    except Exception:
        return _unavailable_response(400)
    if not isinstance(body, dict):
        return _unavailable_response(400)
    try:
        with render_share_connection() as conn:
            session = resolve_session(conn, session_value=_session_or_none(request))
            #  73-2: a dossier session is aimed at ONE figure by the submitted
            #  minted context -- a server-minted, HMAC-verified handle, never a
            #  request parameter -- and membership is re-read from the pinned
            #  version's blocks before anything else runs.
            session = aim_dossier_feedback(conn, session, body.get("context"))
            frozen = load_frozen_render(conn, session)
            receipt = record_targeted_feedback(conn, session, frozen, payload=body)
            append_access_event(
                conn,
                share_id=session.share_id,
                org_id=session.org_id,
                project_id=session.project_id,
                event="feedback",
                outcome="granted",
                reason_code="granted",
                ip_hash=ip_hash,
                client_class=client_class,
            )
            conn.commit()
    except RenderShareConflict:
        return _with_headers(
            JSONResponse(
                {
                    "code": "idempotency_conflict",
                    "message": "This retry key was already used for different feedback.",
                },
                status_code=409,
            )
        )
    except RenderShareUnavailable:
        return _unavailable_response(401)
    except Exception:
        logger.error("render_shares_api: feedback failed")
        return _unavailable_response(401)
    return _with_headers(JSONResponse(receipt))


# ---------------------------------------------------------------------------
# GET /share/view -- the tokenless host page. It MOUNTS the Story 50.5 runtime.
# ---------------------------------------------------------------------------


def _disclosure_rows(frozen: dict, disclosure: dict) -> list[tuple[str, str]]:
    """The English disclosure, built from frozen facts only. THE single source.

    `Shared on` is the Share's creation date, never "now" -- a page that rendered
    `new Date()` would silently tell a recipient the answer is from today. `Stale
    since` is the Render's own frozen freshness, never recomputed: recomputing it
    would make a frozen artifact claim a currency it does not have. Filters, time
    boundary, comparison, grain and truncation come out of the frozen manifest,
    because a page that omitted one would show a filtered number as an unfiltered
    one.

    The page and the export BOTH read this, so an exported artifact can never make a
    weaker claim than the page it came from.
    """
    # TWO frozen manifests, and both are disclosure. The Result's manifest carries
    # the applied filters, the time boundary, the comparison, the grain and the
    # truncation, exactly as the answer was produced; the Render's own manifest
    # carries what it froze about evidence and freshness. A page that read only the
    # second would show a filtered number as an unfiltered one -- so the first is
    # the base and the Render's declarations win where both speak.
    manifest: dict = {}
    runtime_input = frozen.get("runtime_input")
    if isinstance(runtime_input, dict):
        result_manifest = (runtime_input.get("result") or {}).get("manifest")
        if isinstance(result_manifest, dict):
            manifest.update(result_manifest)
    render_manifest = frozen.get("evidence_manifest")
    if isinstance(render_manifest, dict):
        manifest.update(render_manifest)
    rows: list[tuple[str, str]] = [
        ("Shared on", _fmt_date(disclosure.get("shared_on"))),
        # The WRITER's key, not a key of our own (AI-296's lesson, met again on
        # 2026-09-04): `query_execution` records `freshness.output_created_at`,
        # a dict, and this line formatted the dict as a string -- so every share
        # page ever served said "Not disclosed by this Render". The value is the
        # instant the data was produced, which is what a recipient needs to know:
        # "as of", not "stale since".
        ("Data as of", _fmt_date(_frozen_as_of(manifest)) or "Not disclosed by this Render"),
    ]
    for label, key in (
        ("Grain", "grain"),
        ("Comparison", "comparison"),
        ("Truncation", "truncation"),
        ("Time window", "time_window"),
        ("Filters", "filters"),
    ):
        value = manifest.get(key)
        if value is None or value == [] or value == {}:
            continue
        rows.append((label, _fmt_manifest_value(value)))
    return [(label, value) for label, value in rows if value]


def _disclosure_block(frozen: dict, disclosure: dict) -> str:
    """The HTML form, for the EXPORT. The page renders the same rows client-side."""
    items = "".join(
        f'<div class="share-fact"><dt>{html.escape(label)}</dt>'
        f"<dd>{html.escape(value)}</dd></div>"
        for label, value in _disclosure_rows(frozen, disclosure)
    )
    return f'<dl class="share-facts">{items}</dl>'


def _fmt_date(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value.split("T")[0]


def _frozen_as_of(manifest: dict) -> str | None:
    """The as-of instant a frozen manifest records, in the writer's own key.

    `query_execution` writes `freshness.output_created_at`; a later writer may
    say `freshness.as_of`; a Render frozen before either wrote a bare string.
    Same resolution as `analyze_render_mcp.project_freshness`, and nothing is
    recomputed: a value absent from the frozen rows stays absent.
    """
    freshness = manifest.get("freshness")
    if isinstance(freshness, str):
        return freshness or None
    if isinstance(freshness, dict):
        value = freshness.get("as_of") or freshness.get("output_created_at")
        return str(value) if value else None
    return None


def _fmt_manifest_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in sorted(value.items()) if v is not None)
    return str(value)


#: Injected into the runtime bundle. Two blocks, and the ORDER matters: the frozen
#: envelope must be defined before the bundle's own module script runs, because
#: that script reads `window.__TOOROW_FROZEN_RENDER__` on mount.
#: THE HOST PAGE, and why it fetches instead of being server-rendered.
#:
#: The session cookie is path-scoped to `/api/render-shares` (AC5), which means the
#: browser DOES NOT SEND IT to `/share/view`. A server-rendered page could therefore
#: never see the session, and widening the cookie to `/` to fix that would hand the
#: session to every path on the origin -- the opposite of what a narrow path is for.
#: Measured, not assumed: an earlier server-rendered version of this page answered
#: 401 to a browser that had just completed a successful exchange.
#:
#: So the shell is PUBLIC and carries no data. It fetches
#: `/api/render-shares/session/render`, which IS inside the cookie's path, and the
#: request is same-origin so `SameSite=Strict` is satisfied. Nothing about the frozen
#: Render reaches an unauthenticated caller: without a valid session the shell
#: renders the same English denial state as every other refusal.
_VIEW_CHROME = """
<header class="share-header">
<h1>Shared result</h1>
<p class="share-note">This is a frozen result. It shows exactly the values,
formatting and evidence that were shared, and it does not update.</p>
<dl class="share-facts" id="share-facts"></dl>
</header>
<div id="widget-mount"></div>
<section class="share-actions" aria-labelledby="share-actions-title" id="share-actions" hidden>
<h2 id="share-actions-title">Actions</h2>
<button type="button" id="share-export">Export this result</button>
<button type="button" id="share-print">Save as PDF (print)</button>
</section>
<p id="share-status" role="status"></p>
<footer id="share-print-footer" hidden></footer>
"""

#: 73-3 -- THE PDF IS THE PAGE, PRINTED. The amendment names the path itself:
#: "print stylesheet over the share page is the natural path", so the PDF shows
#: exactly what the share shows -- the same runtime, the same frozen envelopes,
#: the same per-figure provenance facts -- and there is never a second rendering
#: path to drift. The stylesheet hides the controls, keeps each figure whole on
#: its page, and reveals the print footer the button stamps with the date.
_PRINT_STYLE = """
<style media="print" nonce="__NONCE__">
@page { margin: 15mm; }
.share-actions, #share-status { display: none !important; }
[data-viz-entry="share-dossier"] section, .widget-frame, #widget-mount section {
  break-inside: avoid;
}
#share-print-footer[hidden] { display: block !important; }
#share-print-footer { border-top: 1px solid #999; margin-top: 8mm; padding-top: 2mm; }
</style>
"""

#: The bootstrap. The ORDER is the point and is asserted by test: the frozen envelope
#: and its adjacent AI Path evidence are assigned to their globals BEFORE the runtime
#: script is allowed to execute, because `ui/cards/shell/src/viz/entries/share.tsx`
#: reads both on mount and would otherwise render an incomplete frozen answer.
#:
#: The runtime script is therefore served INERT (`type="text/plain"`) and promoted to
#: a real module afterwards. The alternative -- letting the bundle mount first and
#: re-rendering it once data arrives -- would draw the page twice, and the first draw
#: is a state the recipient should never see.
_VIEW_CHROME_SCRIPT = """
'use strict';
(function () {
  var status = document.getElementById('share-status');
  var facts = document.getElementById('share-facts');
  var actions = document.getElementById('share-actions');
  var isDossier = false;
  function say(message) { status.textContent = message; }

  function deny() {
    document.title = 'Shared result unavailable';
    var main = document.createElement('main');
    var h1 = document.createElement('h1');
    h1.textContent = 'This shared result is not available';
    var p1 = document.createElement('p');
    p1.setAttribute('role', 'status');
    p1.textContent = 'The link may have expired, been revoked, or already been '
                   + 'opened. Each share link opens once.';
    var p2 = document.createElement('p');
    p2.textContent = 'Ask the person who shared it to send a new link. The result '
                   + 'itself is unchanged - a new link opens exactly the same '
                   + 'frozen result.';
    main.appendChild(h1); main.appendChild(p1); main.appendChild(p2);
    document.body.textContent = '';
    document.body.appendChild(main);
  }

  function unavailable(state) {
    var mount = document.getElementById('widget-mount');
    var box = document.createElement('section');
    box.setAttribute('role', 'status');
    var h2 = document.createElement('h2');
    h2.textContent = state.title || 'This shared result carries no frozen values';
    var p1 = document.createElement('p');
    p1.textContent = state.detail
      || 'There is nothing to draw, and nothing has been substituted for it.';
    box.appendChild(h2); box.appendChild(p1);
    if (state.missing_link) {
      var p2 = document.createElement('p');
      p2.textContent = 'Missing link: ' + state.missing_link;
      box.appendChild(p2);
    }
    var p3 = document.createElement('p');
    p3.textContent = 'The facts above are the ones this Render did freeze. Ask the '
                   + 'person who shared it to share a Render whose values were kept.';
    box.appendChild(p3);
    mount.textContent = '';
    mount.appendChild(box);
    say('');
  }

  function renderFacts(list) {
    for (var i = 0; i < list.length; i += 1) {
      var wrap = document.createElement('div');
      wrap.className = 'share-fact';
      var dt = document.createElement('dt');
      var dd = document.createElement('dd');
      dt.textContent = list[i].label;
      dd.textContent = list[i].value;
      wrap.appendChild(dt); wrap.appendChild(dd);
      facts.appendChild(wrap);
    }
  }

  function startRuntime() {
    var inert = document.querySelectorAll('script[data-toorow-runtime]');
    for (var i = 0; i < inert.length; i += 1) {
      var live = document.createElement('script');
      live.type = 'module';
      live.setAttribute('nonce', '__NONCE__');
      live.textContent = inert[i].textContent;
      inert[i].parentNode.replaceChild(live, inert[i]);
    }
  }

  function mountSingle() {
    fetch('/api/render-shares/session/render', {
      method: 'GET', credentials: 'same-origin', cache: 'no-store'
    }).then(function (response) {
      if (!response.ok) { deny(); return null; }
      return response.json();
    }).then(afterRender).catch(function () { deny(); });
  }

  say('Loading the shared result...');
  // 73-2: a grant may open a Dossier SEQUENCE. The dossier door answers 401
  // for a single-Render grant (disclosing nothing), so this order costs one
  // request and decides the page shape in exactly one place -- the server.
  fetch('/api/render-shares/session/dossier', {
    method: 'GET', credentials: 'same-origin', cache: 'no-store'
  }).then(function (response) {
    if (!response.ok) { mountSingle(); return null; }
    return response.json();
  }).then(function (body) {
    if (!body) return;
    if (!body.dossier) { mountSingle(); return; }
    document.title = 'Shared dossier';
    window.__TOOROW_FROZEN_DOSSIER__ = body.dossier;
    isDossier = true;
    actions.hidden = false;
    say('');
    startRuntime();
  }).catch(function () { deny(); });

  function afterRender(body) {
    if (!body) return;
    renderFacts(body.facts || []);
    if (!body.render) {
      // NOT a refusal panel, and not an empty chart. The Render carries no frozen
      // payload, so the page says exactly that and NAMES the missing link -- the
      // same shape `query_execution.py` uses for an unavailable outcome. The
      // runtime is deliberately not started: mounting it over nothing would draw
      // the field-by-field validator refusal, which reads to a recipient like a
      // bug in the product rather than an honest absence.
      unavailable(body.unavailable || {});
      return;
    }
    window.__TOOROW_FROZEN_RENDER__ = body.render;
    window.__TOOROW_FROZEN_AI_PATH_EVIDENCE__ = body.ai_path_evidence;
    window.__TOOROW_FROZEN_FEEDBACK_CONTEXT__ = body.feedback_context;
    actions.hidden = false;
    say('');
    startRuntime();
  }

  document.getElementById('share-export').addEventListener('click', function () {
    say('Preparing the export...');
    fetch('/api/render-shares/session/export', {
      method: 'POST', credentials: 'same-origin'
    }).then(function (r) {
      if (!r.ok) { say('The export is not available. The link may have expired.'); return null; }
      return r.blob();
    }).then(function (blob) {
      if (!blob) return;
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = isDossier ? 'shared-dossier.html' : 'shared-result.html';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      say('Export downloaded.');
    }).catch(function () { say('The export could not be produced.'); });
  });

  // 73-3: the PDF is this page, printed. The footer carries the export date the
  // amendment wants on the document; everything else printed is exactly what
  // the page shows, through the print stylesheet -- never a second renderer.
  document.getElementById('share-print').addEventListener('click', function () {
    var footer = document.getElementById('share-print-footer');
    footer.textContent = 'Exported to PDF from a frozen share on '
      + new Date().toISOString().slice(0, 10)
      + '. The values, formatting and provenance above are exactly what the share shows.';
    window.print();
  });

})();
"""

#: The two tags Vite's single-file build emits, named exactly. See `_compose_view`
#: for why these are literals rather than a pattern.
_BUNDLE_MODULE_TAG = '<script type="module" crossorigin>'
_BUNDLE_STYLE_TAG = '<style rel="stylesheet" crossorigin>'

#: Served when the runtime bundle has not been built. It names the command, so a
#: reader meets an instruction instead of a blank page -- and it never falls back to
#: a different bundle, because serving *some* renderer because the pinned one is
#: absent is precisely the substitution AC6 forbids.
_BUNDLE_NOT_BUILT = (
    "<p>The shared Visualization runtime bundle is not built. Run "
    "<code>pnpm --filter @toorow/card-shell build</code>, which emits "
    "<code>ui/cards/shell/dist/viz/share.html</code>.</p>"
)


def _compose_view(nonce: str) -> str:
    """The Story 50.5 bundle, served INERT, plus the chrome and the bootstrap.

    No frozen data is embedded here -- the page is public and carries none. It mounts
    the same runtime the Console and the MCP App resource mount; it does not write a
    second renderer, and the accessible table fallback is the runtime's.
    """
    path = share_bundle_path()
    if not path.exists():
        logger.warning("render_shares_api: share runtime bundle missing at %s", path)
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            "<title>Shared result</title></head><body>"
            f"{_BUNDLE_NOT_BUILT}</body></html>"
        )
    else:
        body = path.read_text(encoding="utf-8")
        # TARGETED replacements, on the two exact tags Vite's single-file build
        # emits -- NOT a blanket `body.replace("<script", ...)`.
        #
        # Measured, and it is why this is written the long way: the bundle contains
        # TWO occurrences of `<script`, and the second is inside React's own
        # minified source, in the string literal `o.innerHTML='<script><\\/script>'`
        # that React uses to normalize script elements. A blanket replace rewrites
        # that string too -- editing the internals of a vendored library to say
        # something its authors did not write. It happened here and a test caught
        # it; the fix is to name the tags instead of pattern-matching them.
        if _BUNDLE_MODULE_TAG not in body:
            # The bundle's shape changed. Fail loudly rather than serve a page whose
            # runtime silently never mounts.
            raise RuntimeError(
                "the share bundle no longer contains "
                f"{_BUNDLE_MODULE_TAG!r}; Story 50.7's inert-then-promote mount "
                "needs that exact tag. Re-check ui/cards/shell/vite.config.ts."
            )
        body = body.replace(
            _BUNDLE_MODULE_TAG,
            # Inert: it must not execute before `window.__TOOROW_FROZEN_RENDER__`
            # exists. No nonce is needed or wanted -- the bootstrap re-creates it as
            # a real module and sets the nonce then.
            '<script type="text/plain" data-toorow-runtime="1">',
            1,
        )
        body = body.replace(
            _BUNDLE_STYLE_TAG, f'<style nonce="{nonce}" rel="stylesheet" crossorigin>', 1
        )

    injection = (
        f"{_VIEW_CHROME}"
        f'{_PRINT_STYLE.replace("__NONCE__", nonce)}'
        f'<script nonce="{nonce}">{_VIEW_CHROME_SCRIPT.replace("__NONCE__", nonce)}</script>'
    )

    marker = '<div id="widget-mount"></div>'
    if marker in body:
        return body.replace(marker, injection, 1)
    if "</body>" in body:
        return body.replace("</body>", f"{injection}</body>", 1)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Shared result</title></head><body>{injection}{body}</body></html>"
    )


async def _share_view(_request: Request) -> Response:
    """The tokenless host page. PUBLIC by construction, and carrying no data.

    It reads no cookie and touches no database: everything it shows arrives from
    `/api/render-shares/session/render`, which is inside the session cookie's path. A
    caller with no session gets this same shell and its script renders the English
    denial state -- so the page's existence discloses nothing, and access is decided
    in exactly one place.
    """
    nonce = secrets.token_urlsafe(18)
    return _with_headers(
        Response(_compose_view(nonce), media_type="text/html; charset=utf-8"),
        nonce,
        allow_styles=True,
    )


# `_denied_page` is gone: the denial state now lives in the host page's own
# bootstrap (`_VIEW_CHROME_SCRIPT.deny`), because the page is public and has no
# server-side session to decide against. One state, one place, same English copy.


def _cell(value: object) -> str:
    """One cell, as TEXT. Never markup, never a link, never a formatter."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return html.escape(str(value))


def _export_table(runtime_input: dict | None) -> str:
    """The VALUES, in native table semantics, built only from the frozen slice.

    THIS IS THE HALF THAT WAS MISSING. The exported file used to carry the
    disclosure and the evidence manifest and nothing else -- an "export of a shared
    result" containing no result. The passing test proved the file was inert; it
    never proved it carried anything.

    The columns come from the Result's own `schema.fields`, falling back to the
    keys of the first row when a Result declared no schema, because a Result that
    returned rows without a schema still returned rows. No identifier of the
    Project, the organization, the Result, the Query Spec, the Render or the
    Datastream appears here: an export is a table of values and their disclosure.
    """
    if runtime_input is None:
        return (
            "<p>This Render carries no frozen values, so this export has no table. "
            "Nothing has been substituted for them.</p>"
        )
    result = runtime_input.get("result") or {}
    rows = result.get("rows") or []
    outcome = result.get("outcome")
    schema_fields = ((result.get("schema") or {}).get("fields")) or []
    columns = [f.get("name") for f in schema_fields if isinstance(f, dict) and f.get("name")]
    if not columns and rows and isinstance(rows[0], dict):
        columns = list(rows[0].keys())

    if not rows:
        reason = {
            "empty": "The query ran and returned no rows.",
            "refused": "This result was refused, so it carries no rows.",
            "unavailable": "This result could not be produced, so it carries no rows.",
        }.get(str(outcome), "This result carries no rows.")
        return f"<h2>Values</h2><p>{html.escape(reason)}</p>"

    head = "".join(f"<th scope=\"col\">{html.escape(str(c))}</th>" for c in columns)
    body_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = "".join(f"<td>{_cell(row.get(c))}</td>" for c in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    caption = (
        f"{len(body_rows)} row(s), exactly as frozen"
        + (" (the server returned part of the rows)" if result.get("truncated") else "")
    )
    return (
        "<h2>Values</h2>"
        f"<table><caption>{html.escape(caption)}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
    )


def _build_export_document(frozen: dict, disclosure: dict) -> str:
    """A self-contained file: no script, no remote reference, zero network requests.

    It carries the frozen VALUES (`_export_table`), repeats the page's disclosure
    verbatim so an exported artifact cannot make a weaker claim than the page it
    came from, and carries the frozen evidence manifest as text rather than as
    links -- an evidence entry whose owner the recipient cannot reach is shown as
    an honest reference, never as a link, and never with an owner name.
    """
    facts = _disclosure_block(frozen, disclosure)
    manifest = frozen.get("evidence_manifest")
    evidence_text = html.escape(
        json.dumps(manifest, indent=2, sort_keys=True, default=str)
        if manifest is not None
        else "This Render declared no evidence manifest."
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        "<title>Shared result (export)</title></head><body><main>"
        "<h1>Shared result</h1>"
        "<p>This is an export of a frozen result. It contains no live data and "
        "makes no network requests.</p>"
        f"{facts}"
        f"{_export_table(frozen.get('runtime_input'))}"
        "<h2>Evidence</h2>"
        f"<pre>{evidence_text}</pre>"
        "</main></body></html>"
    )


# ---------------------------------------------------------------------------
# The `410 Gone` handlers for the retired mounts.
#
# They live HERE, and are imported by the modules that own those route lists, so
# the replacement is named in one place. Each REPLACES the `endpoint=` of a mount
# that stays at its exact path and method: deleting the entry would answer `405`
# (or a fall-through `404`), which is a different statement and is
# indistinguishable to a client from a routing regression.
# ---------------------------------------------------------------------------


def _gone(replacement: str) -> Response:
    return JSONResponse(
        {
            "code": "gone",
            "message": (
                "Public sharing by mutable token is retired. " + replacement
            ),
        },
        status_code=410,
    )


async def share_notebook_gone(_request: Request) -> Response:
    """PATCH /api/notebooks/{notebook_id}/share -- authenticates nothing, writes nothing."""
    return _gone(
        "A Notebook is now shared by sharing a Render of one specific run, from the "
        "Render Workbench Sharing tab. Sharing never follows the latest run."
    )


async def create_snapshot_share_gone(_request: Request) -> Response:
    """POST /api/rendus/snapshots/{snapshot_id}/share -- writes nothing."""
    return _gone(
        "Create a Share over one immutable Render from the Render Workbench Sharing "
        "tab. Existing shares can still be listed and revoked."
    )


async def create_insight_share_gone(_request: Request) -> Response:
    """POST /api/daily-insights/insights/{insight_id}/share -- writes nothing."""
    return _gone(
        "An insight is shared by sharing the Render its publication froze, from the "
        "Render Workbench Sharing tab."
    )


# ---------------------------------------------------------------------------
# Routes. Literal segments before parameterized ones.
# ---------------------------------------------------------------------------

render_share_routes: list[Route] = [
    Route("/share", endpoint=_share_bootstrap, methods=["GET"]),
    Route("/share/view", endpoint=_share_view, methods=["GET"]),
    Route("/api/render-shares/exchange", endpoint=_exchange, methods=["POST"]),
    Route("/api/render-shares/session/render", endpoint=_session_render, methods=["GET"]),
    Route("/api/render-shares/session/dossier", endpoint=_session_dossier, methods=["GET"]),
    Route("/api/render-shares/session/rows", endpoint=_session_rows, methods=["GET"]),
    Route("/api/render-shares/session/export", endpoint=_session_export, methods=["POST"]),
    Route("/api/render-shares/session/feedback", endpoint=_session_feedback, methods=["POST"]),
    # The authenticated, Project-scoped console routes are deliberately NOT here.
    # They live in `render_shares_console_api.render_share_console_routes`, because
    # they need `core.query_specs_api` and importing it would drag the whole
    # analytical service into this module's transitive import set -- which is
    # exactly what AC6 layer 3 forbids and what the boundary test asserts.
]

__all__ = [
    "create_insight_share_gone",
    "create_snapshot_share_gone",
    "render_share_routes",
    "share_bundle_path",
    "share_notebook_gone",
]


# Import-boundary assertion aid: these names are deliberately absent from this
# module's imports. `test_render_shares_api.py` asserts the transitive import set,
# because an import is how the role and session layers get quietly bypassed.
FORBIDDEN_TRANSITIVE_IMPORTS = (
    "core.query_execution",
    "core.query_specs",
    "core.warehouse",
    "core.reports",
    "core.cards",
    "core.main",
)

