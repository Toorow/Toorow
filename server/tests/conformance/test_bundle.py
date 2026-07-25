"""Layer 3: Single-file bundle check — Story 1.8 (T4.1–T4.6).

If the module declares a ``widget_ref``, this layer checks that:
  1. The built widget artifact exists at ``ui/widgets/<module-name>/dist/index.html``.
  2. The artifact contains no ``http(s)://`` references (AD-11 / NFR4).
  3. The artifact has no external ``<link>`` or ``<script src="http...">`` tags.

Skips if:
  - ``widget_ref`` is absent or null in the manifest (module declares no widget).
  - The dist file does not exist yet (signals the UI build hasn't run; CI ensures
    the build job precedes the conformance job).
  - Layer 1 failed (_layer1_status[0] is False).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.conformance_layer_3

# Root of the repository — resolved from this file's location.
# Layout: server/tests/conformance/test_bundle.py → repo root is ../../../../
_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def test_bundle_artifact(manifest: dict, _layer1_status: list[bool]) -> None:
    """Check single-file widget bundle integrity (AC4).

    Skips for context modules (module_kind='context'): they have no widget.
    Also skips when widget_ref is absent or null (any module with no widget).

    HG-8: the skip condition uses manifest.get('module_kind') generically —
    no module-specific name appears here (Story 4.5, AC5).
    """
    if not _layer1_status[0]:
        pytest.skip("Layer 1 (manifest) failed — skipping bundle layer")

    # Story 4.5: context modules have no widget (module_kind='context').
    if manifest.get("module_kind", "kpi") == "context":
        pytest.skip(
            "[bundle] module_kind='context' — Layer 3 (bundle) is N/A for context modules"
        )

    widget_ref: str | None = manifest.get("widget_ref")
    if not widget_ref:
        pytest.skip("module declares no widget")

    module_name: str = manifest["name"]
    artifact_path = _REPO_ROOT / "ui" / "widgets" / module_name / "dist" / "index.html"

    # T4.4 — artifact existence check (skip if missing; CI build job runs first)
    if not artifact_path.exists():
        pytest.skip(
            f"[bundle] artifact not found at {artifact_path} — run pnpm build for this widget first"
        )

    content = artifact_path.read_text(encoding="utf-8", errors="replace")

    failures: list[str] = []

    # T4.5 — external http(s) references (AD-11/NFR4), aligned with
    # ui/scripts/bundle-check.mjs (review-1-8 F-02): load-bearing contexts
    # always fail; other occurrences must match a known-inert pattern.
    inert_patterns = [
        re.compile(
            r"^http://www\.w3\.org/(2000/svg|1998/Math/MathML|1999/(xlink|xhtml)|XML/1998/namespace)/?$"
        ),
        re.compile(r"^https://react\.dev/errors(/|$)"),
        re.compile(r"^https://mui\.com/production-error(/|\?|$)"),
    ]
    load_bearing = re.findall(
        r"(?:src|href|srcset|action|poster)\s*=\s*[\"']https?://"
        r"|url\(\s*[\"']?https?://"
        r"|@import\s+[\"']https?://",
        content,
        re.IGNORECASE,
    )
    for ref in load_bearing:
        failures.append(f"[bundle] load-bearing external reference: {ref} — violates AD-11/NFR4")
    external_urls = re.findall(r"https?://[^\s\"'`)<>\\]+", content)
    for url in external_urls:
        if not any(pat.match(url) for pat in inert_patterns):
            failures.append(f"[bundle] external URL found: {url} — violates AD-11/NFR4")

    # T4.6 — no external <link> or <script src="http..."> tags
    link_tags = re.findall(r'<link[^>]+href=["\']https?://[^"\']*["\']', content)
    for tag in link_tags:
        failures.append(f"[bundle] external <link> tag found: {tag} — violates AD-11/NFR4")

    script_tags = re.findall(r'<script[^>]+src=["\']https?://[^"\']*["\']', content)
    for tag in script_tags:
        failures.append(f"[bundle] external <script> tag found: {tag} — violates AD-11/NFR4")

    if failures:
        pytest.fail("\n".join(failures))
