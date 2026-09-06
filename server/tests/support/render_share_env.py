"""The Render Share pepper and origin, set for ONE module's tests only.

WHY THIS EXISTS RATHER THAN A MODULE-LEVEL `os.environ.setdefault` (AI-377,
measured 2026-09-06). Four test modules opened with::

    os.environ.setdefault("TOOROW_RENDER_SHARE_PEPPER", ...)
    os.environ.setdefault("TOOROW_RENDER_SHARE_ORIGIN", ...)

A module-level write is a write for the WHOLE session, and one of those four --
`tests.integration.test_render_shares_postgres` -- is also imported as a helper
library by `tests.fixture_generators.analyze_feedback` (its `Chain`, `RESULT_ROWS`
and the build constants). That lazy import fires INSIDE the golden test of
`tests.integration.test_analyze_feedback_pg`, so that module ends owing the
environment two variables it never wrote, and
`conftest._environment_survives_the_module` errors at ITS teardown -- naming the
victim, never the writer. The same file also blocks the reverse repair: a module
that only READ the two variables could not be fixed by fixing its neighbours.

`monkeypatch.setenv` is restored by pytest by contract, so the value lives
exactly as long as the test that needs it and no longer.

Use it by importing the fixture into the module that needs the two variables::

    from tests.support.render_share_env import render_share_env  # noqa: F401

An autouse fixture bound into a test module's namespace applies to that module's
tests and to nothing else, which is the whole point.
"""

from __future__ import annotations

import os

import pytest

#: The values every Render Share suite has always used. Kept here so the four
#: modules cannot drift apart on the pepper a signature is computed with.
PEPPER = "render-share-test-pepper-0123456789"
ORIGIN = "https://share.example.com"


@pytest.fixture(autouse=True)
def render_share_env(monkeypatch):
    """Set the pepper and the origin for the importing module's tests only.

    An environment that already carries them wins: a run configured against a
    real deployment keeps its own pepper, exactly as `setdefault` used to.
    """
    monkeypatch.setenv(
        "TOOROW_RENDER_SHARE_PEPPER",
        os.environ.get("TOOROW_RENDER_SHARE_PEPPER") or PEPPER,
    )
    monkeypatch.setenv(
        "TOOROW_RENDER_SHARE_ORIGIN",
        os.environ.get("TOOROW_RENDER_SHARE_ORIGIN") or ORIGIN,
    )
