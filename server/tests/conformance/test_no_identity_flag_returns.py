"""Canonical identity has no off switch, and nothing may give it one back.

WHY THIS FILE EXISTS. `TOOROW_CANONICAL_IDENTITY_ENABLED` selected, at eight
sites across five modules, between resolving a request to its canonical
``person_<ULID>`` and a legacy path that authorized on the raw OIDC ``sub``.

Its code default was ``"0"`` (`api_auth.py:57`, `admin_api.py#_check_auth` before
2026-08-24). That is the whole defect, and it is a defect of SHAPE, not of
value: an environment that simply never set the variable did not get a warning
or a refusal, it got the legacy path -- authorizing against a key
``app.org_members`` no longer stores, and leaving `POST
/api/organizations/{org_id}/members` able to write a hand-typed identity string.
A security flag whose ABSENCE opens the door is fail-open by omission, and no
amount of setting it correctly in one place fixes the shape.

`infra/scripts/deploy.sh` had already been made to REFUSE a deploy that did not
carry the variable (2026-08-23). That closed the one known environment; it could
not close the next one. Removing the branch closes the class: there is one
authorization key, and no configuration can select another.

WHAT THIS GUARD REFUSES. Any Python under `server/` that names the variable or
the helper that read it. It does not care whether the new branch would be
written "the safe way round" -- a second key reachable by configuration is the
thing being refused, in either polarity.

IT READS THE REAL TREE, NOT A COPY. `_ROOT` is resolved from this file's own
location, and the file count is asserted against a floor: a glob that silently
stopped matching would otherwise make this pass by finding nothing.
"""

from __future__ import annotations

import pathlib

#: The two names the removed branch was written with. Either one reappearing in
#: server code means the flag is back, whatever it is spelled.
_FORBIDDEN = (
    "TOOROW_CANONICAL_IDENTITY_ENABLED",
    "_canonical_identity_enabled",
)

_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Measured 2026-08-24: `server/**/*.py` minus caches counts well above this.
#: The floor exists so a broken glob fails loudly instead of passing silently.
_MINIMUM_FILES_SCANNED = 400


def _server_sources() -> list[pathlib.Path]:
    """Every Python file under `server/`, except caches and this guard."""
    here = pathlib.Path(__file__).resolve()
    return [
        path
        for path in sorted(_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts and path.resolve() != here
    ]


def test_the_guard_reads_a_real_and_complete_tree():
    """An instrument that measures nothing reports no defect. Refuse that first."""
    sources = _server_sources()
    assert len(sources) >= _MINIMUM_FILES_SCANNED, (
        f"only {len(sources)} Python files found under {_ROOT}. This guard is "
        "not reading the server tree; fix the guard before trusting it green."
    )
    # The seam it protects must be among them, or the scope is wrong.
    assert (_ROOT / "core" / "api_auth.py") in sources


def test_no_server_module_branches_on_an_identity_flag():
    """One authorization key. No configuration selects another."""
    offenders: list[str] = []
    for path in _server_sources():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in _FORBIDDEN:
            if name in text:
                line = next(
                    (
                        number
                        for number, content in enumerate(text.splitlines(), start=1)
                        if name in content
                    ),
                    0,
                )
                offenders.append(f"{path.relative_to(_ROOT).as_posix()}:{line} names {name}")

    assert not offenders, (
        "canonical identity is not optional and has no flag. Remove the branch; "
        "if an environment genuinely needs a different authorization key, that "
        "is a product decision and belongs in docs/product-architecture/ first.\n"
        + "\n".join(offenders)
    )
