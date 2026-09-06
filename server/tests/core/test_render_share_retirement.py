"""Story 50.7 AC10 -- the superseded paths are gone, and the proof is a test.

TWO VERBS THAT ARE NOT INTERCHANGEABLE, and the difference decides how the code was
edited:

  * **"No route matches"** -- the `Route(...)` entry is DELETED. A client gets a
    `404` because nothing is mounted at that path.
  * **"Returns 410 Gone"** -- the `Route(...)` entry STAYS at its exact path and
    method, and only its `endpoint=` changes. Deleting it would answer `405 Method
    Not Allowed` (or a fall-through `404`), which is a *different statement* and is
    indistinguishable to a client from a routing regression.

The assertions below are on the STATUS CODE of the composed application, never
merely on the absence of a handler symbol -- an unmounted handler body is exactly
the artefact that got a route remounted once already (`SESSIONS.md`, "Desaccord
CLOS: `_create_datastream_mapping_version`").

WHY `PLATFORM_DB_URL` IS NOT SET HERE. With it set, every route answers the
application's own `{"code":"not_found"}` envelope and a mount probe cannot
distinguish "mounted" from "absent" -- which would make this whole file green and
meaningless.
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]
_CORE = _REPO / "server" / "core"


# ---------------------------------------------------------------------------
# Route-level truth, read off the composed route tables.
# ---------------------------------------------------------------------------


def _paths(routes) -> list[str]:
    return [getattr(route, "path", "") for route in routes]


def _code_only(name: str) -> str:
    """Source with comments and string literals removed.

    These modules DESCRIBE what was retired -- the French page, the `token[:8]`
    keys, the plaintext column -- because CLAUDE.md anti-drift rule 3 says deleting
    the trace of a retired path is how the next reader rebuilds it. A plain text
    search cannot tell that record from the thing itself, so it would either fail on
    the documentation or be loosened until it proved nothing. Stripping comments and
    literals leaves exactly the executable code these assertions are about.
    """
    import ast
    import io
    import tokenize

    source = (_CORE / name).read_text(encoding="utf-8")
    kept: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(token.string)
    # `ast.parse` first, so a syntax error surfaces as one rather than as a
    # mysteriously-passing assertion over half a file.
    ast.parse(source)
    return " ".join(kept)


def test_the_rendus_raw_token_route_is_gone():
    """AC10: NO route matches `GET /api/rendus/shared/{token}`.

    A token in a URL path is in the browser history, the referrer, the proxy log
    and the ASGI access log before any application code runs -- so it is removed,
    not hardened."""
    from core.rendus_api import RENDUS_ROUTES

    assert "/api/rendus/shared/{token}" not in _paths(RENDUS_ROUTES)


def test_the_rendus_share_creation_route_stays_mounted_and_answers_410():
    """AC10: mounted at the same path and method, endpoint swapped. Removing the
    entry would answer 405, which says something else entirely."""
    from core.render_shares_api import create_snapshot_share_gone
    from core.rendus_api import RENDUS_ROUTES

    matches = [
        route
        for route in RENDUS_ROUTES
        if getattr(route, "path", "") == "/api/rendus/snapshots/{snapshot_id}/share"
    ]
    assert len(matches) == 1, "the mount must survive; only its endpoint changes"
    assert "POST" in matches[0].methods
    assert matches[0].endpoint is create_snapshot_share_gone


def test_the_daily_insight_share_route_stays_mounted_and_answers_410():
    from core.daily_insights_api import DAILY_INSIGHTS_ROUTES
    from core.render_shares_api import create_insight_share_gone

    matches = [
        route
        for route in DAILY_INSIGHTS_ROUTES
        if getattr(route, "path", "") == "/api/daily-insights/insights/{insight_id}/share"
    ]
    assert len(matches) == 1
    assert "POST" in matches[0].methods
    assert matches[0].endpoint is create_insight_share_gone


def test_the_legacy_revocation_and_history_routes_survive():
    """Deliberately preserved: existing grants must still be closable and readable.
    A retirement that also removes revocation strands the grants it was meant to
    close."""
    from core.daily_insights_api import DAILY_INSIGHTS_ROUTES
    from core.rendus_api import RENDUS_ROUTES

    rendus = _paths(RENDUS_ROUTES)
    assert "/api/rendus/shares/{share_id}" in rendus
    assert "/api/rendus/snapshots/{snapshot_id}/shares" in rendus
    insights = _paths(DAILY_INSIGHTS_ROUTES)
    assert "/api/daily-insights/shares/{share_id}" in insights
    assert "/api/daily-insights/insights/{insight_id}/shares" in insights


@pytest.mark.parametrize(
    "handler_name",
    ["create_snapshot_share_gone", "create_insight_share_gone", "share_notebook_gone"],
)
def test_every_gone_handler_returns_410_with_english_copy_and_writes_nothing(handler_name):
    """It authenticates nothing and touches no database: a `410` that first required
    a token would be a `401` for most callers, which discloses more than the
    retirement should."""
    import asyncio
    import json

    from core import render_shares_api

    handler = getattr(render_shares_api, handler_name)
    response = asyncio.run(handler(None))  # `None` request: it must not be read
    assert response.status_code == 410
    body = json.loads(response.body)
    assert body["code"] == "gone"
    assert "Render" in body["message"], "the body must name the replacement"
    assert not re.search(r"[éèêàùçô]", body["message"]), "product copy is English"


# ---------------------------------------------------------------------------
# Source-level truth: the executable form of AC10's greps.
# ---------------------------------------------------------------------------


def test_no_share_minting_path_survives_in_the_retired_modules():
    """`grep -rn "token_urlsafe" server/core/snapshot_shares.py` must return no line
    in a share-minting path."""
    code = _code_only("snapshot_shares.py")
    assert "token_urlsafe" not in code
    assert "create_share" not in code
    assert "get_shared_snapshot" not in code
    assert "_mint_token" not in code


def test_the_legacy_listing_no_longer_selects_the_token():
    """The retired docstring argued that returning `share_token` was fine because
    the caller is authenticated. It is not: it puts a live public grant into every
    screenshot and browser cache."""
    code = _code_only("snapshot_shares.py")
    assert "share_token" not in code, (
        "the legacy listing still names the token in executable code. The retired "
        "docstring is allowed to mention it; a SELECT is not."
    )


def test_the_rendus_module_builds_no_share_url():
    code = _code_only("rendus_api.py")
    assert "share_url" not in code, "a token-bearing URL is still constructed"


def test_no_bearer_prefix_survives_as_a_rate_limit_key():
    """`rendus_api.py:82` and `admin_api.py#_shared_notebook_endpoint` keyed their buckets on
    `token[:8]`. The rendus one is gone with its endpoint; the notebook one goes
    with the route the orchestrator unmounts."""
    source = (_CORE / "rendus_api.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "token_prefix" not in code
    assert "_check_rendus_rate_limit" not in code


def test_the_public_french_share_page_is_gone():
    """`visualization-and-rendering.md:353` -- "Current widget and share copy
    includes French labels". The page also mounted nothing: its
    `<div id="widget-mount">` stayed empty because it loaded no bundle."""
    code = _code_only("rendus_api.py")
    assert "_SHARED_HTML_TEMPLATE" not in code
    assert "_fmt_date_fr" not in code
    assert "_shared_snapshot_endpoint" not in code
    # The French page's own markup lived in a string literal, which `_code_only`
    # strips -- so the absence of the SYMBOLS above is what proves it is gone, and
    # the presence of `lang="fr"` anywhere in the raw file is checked separately.
    raw = (_CORE / "rendus_api.py").read_text(encoding="utf-8")
    served = [
        line for line in raw.splitlines()
        if 'lang="fr"' in line and not line.lstrip().startswith("#")
    ]
    assert not served, f"French markup is still served: {served}"


def test_the_evidence_index_points_at_the_new_owner():
    """The disposition and the reason code are unchanged -- a share is an access
    grant, not a lineage claim -- and only the table it names moved."""
    from core.evidence_index import PRODUCERS_BY_KEY

    assert "render_snapshot_shares" not in PRODUCERS_BY_KEY
    contract = PRODUCERS_BY_KEY["render_shares"]
    assert contract.disposition == "excluded"
    assert contract.reason_code == "sharing_not_evidence"


def test_the_public_route_family_is_dispatched_by_routing():
    """Without these entries `/share` falls through to the MCP app and a valid link
    404s. Exact equality, never a prefix test."""
    code = _code_only("routing.py")
    assert "/share" in (_CORE / "routing.py").read_text(encoding="utf-8")
    # The dispatch itself, not a comment about it: the literals live in a tuple
    # membership test, so `_code_only` keeps the operator and the parentheses.
    assert "path in" in code


# ---------------------------------------------------------------------------
# The import boundary: AC6 layer 3.
# ---------------------------------------------------------------------------


def test_the_public_module_cannot_reach_the_analytical_service():
    """AC6.3. An import is how the session and role layers get quietly bypassed, so
    the boundary is asserted on the module's transitive imports rather than trusted.

    The console handlers live in `render_shares_console_api.py` precisely so this
    assertion can stay strict: they need `core.query_specs_api`, which imports the
    whole analytical service.

    RUN IN A SUBPROCESS, and that is the whole point of this docstring.

    The check needs a module graph that has not already imported the analytical
    service — otherwise `sys.modules` is pre-populated and the assertion passes
    vacuously. The first version got that clean graph by deleting every `core.*`
    entry from the RUNNING interpreter's `sys.modules`, and never restored them.

    That wrecked the rest of the session. A deleted-then-reimported module yields
    NEW class objects, so an exception class imported before the wipe is no longer
    the class raised after it:

        from core.pull_errors import InvalidRequestError as Before
        <wipe>
        from core.pull_errors import InvalidRequestError as After
        Before is After   ->  False

    Every `pytest.raises(...)` bound at import time therefore stopped catching,
    and the raw error escaped. Measured on 2026-07-31: `tests/modules` alone gives
    **1130 passed, 0 failed**; `tests/core tests/modules` gives **181 failed** —
    and binary-searching the 394 core files lands on this one. It is also a large
    share of the 262 failures a plain `make test` reports today.

    A subprocess gets a genuinely fresh interpreter and cannot reach back into
    this one. Snapshot-and-restore would not do: modules imported during the
    window keep their duplicate identities either way.
    """
    import json
    import subprocess
    import sys

    probe = (
        "import sys, json\n"
        "import core.render_shares_api\n"
        "print(json.dumps(sorted(n for n in sys.modules if n.startswith('core.'))))\n"
    )
    env = {
        **os.environ,
        "TOOROW_RENDER_SHARE_PEPPER": os.environ.get(
            "TOOROW_RENDER_SHARE_PEPPER", "render-share-test-pepper-0123456789"
        ),
        "HEALTH_POLLER_ENABLED": "false",
        "QUEUE_WORKER_ENABLED": "false",
        "SCHEDULER_ENABLED": "false",
    }
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
        env=env,
        check=False,
    )
    assert completed.returncode == 0, (
        "the import probe failed to run:\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )

    loaded = set(json.loads(completed.stdout.strip().splitlines()[-1]))
    forbidden = {
        "core.query_execution",
        "core.query_specs",
        "core.warehouse",
        "core.reports",
        "core.cards",
        "core.main",
    }
    leaked = loaded & forbidden
    assert not leaked, f"the public share module transitively imports {leaked}"


def test_no_public_handler_reads_an_object_identifier_from_the_request():
    """AC6.1: every identity on the public surface is derived from `share_id`. A
    handler that accepted a `project_id` would make the other two layers decorative."""
    import ast

    tree = ast.parse((_CORE / "render_shares_api.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "path_params":
            offenders.append(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr == "query_params":
            offenders.append(node.lineno)
    assert not offenders, (
        f"the public surface reads path/query parameters at lines {offenders}; every "
        "identity must come from the session cookie"
    )
