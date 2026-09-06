"""Story 50.5 AC16(c) -- the standard rendering path is core-owned, and cannot be
decided by a connector again.

The honest state this story creates is PINNED here, not merely described: after the
connector scan was deleted, `ui://core/daily-report` serves its not-built
placeholder to every host until Story 50.6 attaches
`ui://core/visualization-runtime`. If someone re-enables a connector scan, the
second test below turns red.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from core import visualization_runtime_resource as runtime_resource

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_resource_uri_is_the_exact_core_scoped_literal():
    """Story 50.6's render tool imports this constant rather than retyping it."""
    assert runtime_resource.VISUALIZATION_RUNTIME_URI == "ui://core/visualization-runtime"


def test_the_served_bytes_are_the_built_runtime_or_a_placeholder_that_says_so():
    bundle = runtime_resource.runtime_bundle_path()
    served = runtime_resource.read_runtime_bundle()
    if bundle.exists():
        assert served == bundle.read_text(encoding="utf-8")
        # A self-contained MCP App resource: one document, its own mount point.
        assert '<div id="root">' in served
        # AD-11 -- nothing LOAD-BEARING is fetched at runtime.
        # `node ui/scripts/bundle-check.mjs` is the authority and is run in the
        # story's evidence; this is the cheap restatement of the part that can be
        # asserted here. A bare `"http" not in served` would be wrong, not strict:
        # React's own code carries the SVG and MathML XML namespace URIs, which are
        # identifiers, not fetches.
        for loader in (
            '<script src="http',
            "<script src='http",
            '<link rel="stylesheet" href="http',
            "<img src='http",
            '<img src="http',
            "@import url(http",
        ):
            assert loader not in served, loader
    else:
        assert "not built" in served
        assert "pnpm --filter @toorow/card-shell build" in served


def test_no_connector_manifest_can_select_a_bundle_for_a_standard_family():
    """AD-2, asserted over the real manifests rather than over an intention.

    Twenty-six connector manifests declare `widget_ref == "ui://core/daily-report"`.
    Before this story, `_resolve_widget_dist()` scanned them and served
    `ui/widgets/<connector>/dist/index.html` from whichever loaded first. The scan
    is gone; the manifests are untouched, because they are the inventory of what
    still has to move (CLAUDE.md anti-drift rule 3).
    """
    from core import main

    declaring = [
        path.parent.name
        for path in sorted((REPO_ROOT / "server" / "modules").glob("*/manifest.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("widget_ref")
        == main.DAILY_REPORT_WIDGET_URI
    ]
    # The manifests still declare it -- that is the inventory, and it is expected.
    assert len(declaring) > 1, (
        "the premise of this test is that MANY connectors declare the same core "
        f"widget ref; found {declaring}"
    )

    # The FUNCTION's own source, not a file's: the resolver moved out of the
    # entrypoint into `core.widget_resources`, and an invariant pinned to a path
    # stops being checked the day the code is split.
    resolver = inspect.getsource(main._resolve_widget_dist)
    # The body may not consult loaded connectors any more. A comment explaining the
    # deletion is fine; a loop over them is not.
    executable = "\n".join(
        line for line in resolver.splitlines() if not line.strip().startswith("#")
    )
    body = executable.split('"""')[-1]
    assert "_loaded_modules" not in body, (
        "the connector scan is back in _resolve_widget_dist: whichever connector "
        "loads first would decide how the standard report looks (AD-2)"
    )
    assert "widget_ref" not in body


def test_the_daily_report_placeholder_names_its_replacement(monkeypatch):
    """The intended honest state, pinned.

    Without the scan and without the explicit override, the path does not exist,
    so the placeholder is served -- and it says what replaces it.
    """
    from core import main

    monkeypatch.delenv("TOOROW_DAILY_REPORT_WIDGET_DIST", raising=False)
    resolved = main._resolve_widget_dist()
    assert resolved.parent.parent.name == "_unresolved"
    assert not resolved.exists()

    # The placeholder text lives in `daily_report_widget`; read it from that
    # function's source so this test does not depend on a FastMCP resource-call
    # harness -- and follows the function out of the entrypoint.
    served = inspect.getsource(main.daily_report_widget)
    assert "ui://core/visualization-runtime" in served
    assert "This widget is not built." in served


def test_an_explicit_override_still_wins(monkeypatch, tmp_path):
    """Core stays source-agnostic: a deployment may name a path, never a connector."""
    from core import main

    target = tmp_path / "index.html"
    target.write_text("<!doctype html><html></html>", encoding="utf-8")
    monkeypatch.setenv("TOOROW_DAILY_REPORT_WIDGET_DIST", str(target))
    assert main._resolve_widget_dist() == target


def test_the_runtime_bundle_path_is_overridable_and_never_names_a_connector(monkeypatch, tmp_path):
    target = tmp_path / "mcp-app.html"
    target.write_text('<!doctype html><html><body><div id="root"></div></body></html>', "utf-8")
    monkeypatch.setenv("TOOROW_VISUALIZATION_RUNTIME_DIST", str(target))
    assert runtime_resource.runtime_bundle_path() == target
    assert '<div id="root">' in runtime_resource.read_runtime_bundle()

    # Only the EXECUTABLE lines: the module docstring names the connector scan it
    # replaced, and erasing that explanation to satisfy a grep would delete the
    # only record of why this file exists.
    source = (REPO_ROOT / "server" / "core" / "visualization_runtime_resource.py").read_text(
        encoding="utf-8"
    )
    code = source.split('"""', 2)[2]
    code = chr(10).join(line for line in code.splitlines() if not line.strip().startswith("#"))
    assert "server/modules" not in code
    assert "_loaded_modules" not in code


@pytest.mark.parametrize("uri", ["ui://core/visualization-runtime"])
def test_the_uri_is_registered_exactly_once_across_the_server(uri):
    """Two modules registering the same URI is a silent last-writer-wins."""
    # The DEFINITION lives in one module; the BINDING happens at one call site.
    definitions = [
        path.name
        for path in sorted((REPO_ROOT / "server" / "core").glob("*.py"))
        if "@mcp.resource(VISUALIZATION_RUNTIME_URI)" in path.read_text(encoding="utf-8")
    ]
    assert definitions == ["visualization_runtime_resource.py"], definitions

    call_sites = [
        path.name
        for path in sorted((REPO_ROOT / "server" / "core").glob("*.py"))
        if "_visualization_runtime.register(mcp)" in path.read_text(encoding="utf-8")
    ]
    assert call_sites == ["main.py"], call_sites

    # And no other module may bind the literal itself.
    others = [
        path.name
        for path in sorted((REPO_ROOT / "server" / "core").glob("*.py"))
        if uri in path.read_text(encoding="utf-8")
        # `widget_resources.py` carries the literal only inside the daily-report
        # PLACEHOLDER PROSE that names its replacement -- the text the test above
        # pins. It binds no resource to it; the `call_sites` assertion above is
        # what keeps that true.
        and path.name not in {
            "visualization_runtime_resource.py",
            "main.py",
            "widget_resources.py",
        }
    ]
    assert others == [], others
