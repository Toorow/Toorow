"""The deploy-time projection of the shipped renderer registry, and its verdict.

WHY THIS SUITE EXISTS, measured 2026-08-24. `scripts/register_renderer_builds.py`
had no test at all, and it is the step that decides whether a deployment is allowed
to ship -- `infra/scripts/deploy.sh` runs it before `gcloud builds submit` and exits
1 when it refuses. A gate nothing tests is a gate that fails on the day it matters,
in a terminal, mid-deploy.

WHAT THE VERDICT MUST BE, and it is the ratified clause and nothing wider
(`docs/product-architecture/visualization-and-rendering.md:319-322`):

    a family is declared in code and absent from the ledger      -> MISSING, fatal
    a ledger row names a family or renderer the shipped registry
    does not declare                                             -> UNDECLARED, fatal

and, in the same paragraph, the rule that forbids a third: "a build id already
present is left exactly as it is, because a Render already pinned it". Migration
191 registered `waterfall/toorow-echarts-waterfall@1.0.0` at runtime
`+2e4aba0febbe` -- a real identity, shipped at commit 0bc6aded -- and the runtime
content hash has moved twice since. That row is TRUE about the build it names, the
table is insert-once (migration 160's trigger on UPDATE and DELETE), and the tool
used to exit 1 on it forever. The tests below fix the boundary: a frozen row of a
still-declared renderer is HISTORY, never a failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import register_renderer_builds as tool  # noqa: E402

REGISTRY_SOURCE = ROOT / "ui" / "cards" / "shell" / "src" / "viz" / "renderers" / "index.ts"


def _row(**overrides) -> dict:
    row = {
        "id": "bar/toorow-echarts-bar@1.0.0",
        "runtime_build": "@toorow/card-shell/viz@0.1.0+aaaaaaaaaaaa",
        "family": "bar",
        "renderer_id": "toorow-echarts-bar",
        "theme_version": "viz-theme@1",
        "formatter_version": "viz-formatters@1",
        "responsive_profiles": ["console", "mcp-inline", "mcp-fullscreen", "share"],
    }
    row.update(overrides)
    return row


def test_the_manifest_declares_every_renderer_the_shipped_registry_registers():
    """The projection is only honest if the manifest IS the registry.

    Read from the TypeScript source rather than restated here: a second list in a
    test drifts the day a renderer ships, and then the test defends the drift.
    """
    source = REGISTRY_SOURCE.read_text(encoding="utf-8")
    declared = {(build["family"], build["renderer_id"]) for build in tool.load_manifest()}

    assert declared, "the manifest declares no build"
    for family, renderer_id in sorted(declared):
        assert f'declare("{family}", "{renderer_id}"' in source, (
            f"the manifest names {family}/{renderer_id}, which "
            f"{REGISTRY_SOURCE.name} does not register"
        )
    assert len(declared) == source.count("declare("), (
        "renderers/index.ts registers a renderer the manifest does not carry -- "
        "regenerate with `pnpm --filter @toorow/card-shell generate:build-identity`"
    )


def test_the_manifest_carries_every_column_the_ledger_requires():
    for build in tool.load_manifest():
        missing = [column for column in tool.MANIFEST_COLUMNS if column not in build]
        assert missing == [], f"{build.get('id')} lacks {missing}"


def test_a_ledger_row_of_a_declared_renderer_is_never_undeclared():
    """Migration 191's row, by name, and the class it belongs to.

    Its build id is `waterfall/toorow-echarts-waterfall@1.0.0` and the manifest
    emits that same id today -- but the point holds even when it does not: the
    clause is keyed on the family and the renderer, not on the build id, so an
    identity retained from an earlier runtime or an earlier semver is still a
    renderer this build ships.
    """
    declared = {(build["family"], build["renderer_id"]) for build in tool.load_manifest()}
    stored = {
        "waterfall/toorow-echarts-waterfall@0.9.0": _row(
            id="waterfall/toorow-echarts-waterfall@0.9.0",
            family="waterfall",
            renderer_id="toorow-echarts-waterfall",
            runtime_build="@toorow/card-shell/viz@0.1.0+2e4aba0febbe",
        )
    }

    assert tool._undeclared(stored, declared) == []


def test_a_ledger_row_naming_a_renderer_this_build_does_not_declare_is_reported():
    declared = {(build["family"], build["renderer_id"]) for build in tool.load_manifest()}
    stored = {
        "bar/toorow-echarts-oldbar@0.9.0": _row(
            id="bar/toorow-echarts-oldbar@0.9.0", renderer_id="toorow-echarts-oldbar"
        )
    }

    assert tool._undeclared(stored, declared) == ["bar/toorow-echarts-oldbar@0.9.0"]


def test_a_frozen_row_that_no_longer_matches_the_manifest_is_described_not_judged():
    """`_differs` is a description of what has moved, and the caller prints it as
    HISTORY. The verdict lives in `_undeclared` and in the missing set."""
    declared = _row(
        runtime_build="@toorow/card-shell/viz@0.1.0+ffffffffffff",
        responsive_profiles=["console", "mcp-inline", "mcp-fullscreen", "mcp-pip", "share"],
    )

    lines = tool._differs(_row(), declared)

    assert len(lines) == 2
    assert any(line.startswith("runtime_build:") for line in lines)
    assert any(line.startswith("responsive_profiles:") for line in lines)


def test_an_empty_manifest_is_refused_rather_than_projected(tmp_path, monkeypatch):
    """A registry that failed to load would otherwise EMPTY nothing and pass."""
    empty = tmp_path / "rendererBuilds.generated.json"
    empty.write_text(json.dumps({"builds": []}), encoding="utf-8")
    monkeypatch.setattr(tool, "MANIFEST", empty)

    try:
        tool.load_manifest()
    except SystemExit as exit_error:
        assert "empty registry" in str(exit_error)
    else:  # pragma: no cover -- the assertion above is the test
        raise AssertionError("an empty manifest was accepted")


def test_the_deploy_path_runs_this_tool_and_refuses_on_its_exit_code():
    """The gate exists in the ONE script that deploys (CLAUDE.md: never GitHub).

    Measured 2026-08-24: `grep -rn register_renderer_builds infra/ .github/ Makefile`
    returned a comment and nothing else -- the tool worked and nothing called it.
    """
    deploy = (ROOT / "infra" / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "project_renderer_builds" in deploy
    assert "scripts/register_renderer_builds.py --dsn" in deploy
    assert "--check" in deploy.split("project_renderer_builds() {", 1)[1]
    assert "Refus de deployer" in deploy, "a failing check must refuse, not warn"
