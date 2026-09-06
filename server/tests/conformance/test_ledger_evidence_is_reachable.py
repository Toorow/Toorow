"""A completeness criterion may not be closed on a symbol nothing calls.

WHY THIS IS A TEST AND NOT ANOTHER HOOK — the choice was deliberate.

`scripts/ledger_evidence_reachability.py` already existed on 2026-07-31 and
already returned exit 1. It found nothing new for a day, because a script you
have to remember to run is not a guard. The obvious place to hang it was next to
`scripts/finished_work_audit.py --gate`, which runs from a `Stop` hook in
`.claude/settings.json`. Three measured reasons say otherwise:

1. **`.claude/settings.json` is untracked** (`git status --short .claude/` →
   `?? .claude/settings.json`). A hook declared there exists on one machine, for
   one agent. It is invisible to CI, to a human running the suite, and to every
   other session in a repository where several write at once.
2. **The `Stop` hook only fires when an agent stops.** Nothing about the moment
   an agent finishes a turn has anything to do with whether a ledger entry is
   true. A human who edits the ledger by hand never triggers it.
3. **`finished_work_audit --gate` structurally cannot carry this.** Its exit code
   is computed from `new_orphans` and `new_structural` only
   (`finished_work_audit.py:1946-1980`); the criteria ledger it reads feeds
   `open_criteria`, which the gate never consults. Making it carry the check
   would mean editing that file — held by another session — and would put the
   verdict behind `known-debt.json`, where a single `--baseline` run silences it.

A conformance test runs for everybody, in the sweep that is already ratified
(`cd server && uv run python -m pytest tests/conformance -q`), fails loudly with
the symbol named, and cannot be baselined away.

The repository sweep costs ~6 s (measured 2026-08-01). It is paid once: the
discrimination tests below use synthetic sources, because a guard that has never
been shown to go red is indistinguishable from one that cannot.
"""

from __future__ import annotations

import ast
import importlib.util
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DETECTOR = _REPO_ROOT / "scripts" / "ledger_evidence_reachability.py"


def _load_detector():
    """Import the script by path — `scripts/` is not an installed package."""
    assert _DETECTOR.exists(), f"detector missing: {_DETECTOR}"
    spec = importlib.util.spec_from_file_location("ledger_evidence_reachability", _DETECTOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


detector = _load_detector()

#: The two stems the synthetic sources below import from. `core_module_stems()`
#: reads the real tree; these fixtures must not depend on it.
_STEMS = {"report_timezone", "entity_bindings", "external_bq_registration"}


def _wired(source: str, module: str, name: str) -> bool:
    tree = ast.parse(textwrap.dedent(source))
    return detector._references(detector.scan(tree, _STEMS), module, name)


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def test_no_ledger_criterion_closes_on_a_symbol_nothing_calls() -> None:
    """Every `core/<module>.<fn>` cited as evidence has a production caller.

    An unrecorded criterion counts as still incomplete — that is the ledger's own
    rule. So the two honest answers to a failure here are: wire the symbol, or
    delete the ledger entry. Leaving the entry while knowing the citation is
    empty is the one answer this test exists to refuse.
    """
    findings = detector.unreachable_evidence()
    assert not findings, "\n".join(
        [
            f"{len(findings)} completeness criteria are closed on a symbol no "
            "production path calls:",
            *(
                f"  {f['criterion']:24s} {f['symbol']}  ({f['file']})"
                for f in findings
            ),
            "",
            "Reproduce: uv run python scripts/ledger_evidence_reachability.py",
            "Wire the symbol, or remove its entry from "
            "docs/product-architecture/completeness-ledger.json (textual edit in "
            "place — that file is shared, and re-serialising it destroyed another "
            "session's work on 2026-07-31).",
        ]
    )


# ---------------------------------------------------------------------------
# Discrimination — a guard nobody has seen go red proves nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "direct import",
            """
            from core.report_timezone import resolve_capture
            def go(d, z):
                return resolve_capture(d, z)
            """,
        ),
        (
            # The 2026-08-01 false positive: `server/modules/google-analytics/
            # connector.py:336` binds the MODULE under an alias. The import
            # handler used to read `_rtz` as "a name from package core" and
            # reported a wired function as dead.
            "module bound under an alias",
            """
            from core import report_timezone as _rtz
            def go(d, z):
                return _rtz.resolve_capture(d, z)["report_timezone"]
            """,
        ),
        (
            "fully qualified dotted call",
            """
            import core.report_timezone
            def go(d, z):
                return core.report_timezone.resolve_capture(d, z)
            """,
        ),
        (
            "aliased dotted module import",
            """
            import core.report_timezone as rtz
            def go(d, z):
                return rtz.resolve_capture(d, z)
            """,
        ),
        (
            # Bug 3: a regex missed the parenthesised multi-line form.
            "multi-line parenthesised import",
            """
            from core.report_timezone import (
                declared_zone,
                resolve_capture,
            )
            def go(d, z):
                return resolve_capture(d, z)
            """,
        ),
        (
            # Passing a function into a dispatch table IS wiring it. Declared as
            # a limitation in the script: a mere mention passes too.
            "handed to a dispatch table, never called by name",
            """
            from core.report_timezone import resolve_capture
            HANDLERS = {"tz": resolve_capture}
            """,
        ),
    ],
)
def test_a_real_caller_is_recognised(label: str, source: str) -> None:
    assert _wired(source, "report_timezone", "resolve_capture"), (
        f"{label}: a genuine production caller read as dead. A false positive is "
        "what gets an instrument switched off."
    )


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            # The defect that hid the original finding for two days:
            # `external_bq_registration` defines its OWN `record_observation`.
            "homonym in another module",
            """
            from core.external_bq_registration import record_observation
            def go(x):
                return record_observation(x)
            """,
        ),
        (
            "same bare name, no import at all",
            """
            def go(x):
                record_observation = x
                return record_observation
            """,
        ),
        (
            "imported and never used",
            """
            from core.entity_bindings import record_observation  # noqa: F401
            """,
        ),
        (
            "attribute of an unrelated object",
            """
            def go(client):
                return client.record_observation()
            """,
        ),
    ],
)
def test_a_non_caller_is_not_mistaken_for_one(label: str, source: str) -> None:
    assert not _wired(source, "entity_bindings", "record_observation"), (
        f"{label}: counted as a caller. This is exactly how a criterion gets "
        "closed over an empty table."
    )


def test_recursion_in_the_defining_file_is_not_wiring() -> None:
    """A function that only calls itself is recursive, not reachable."""
    tree = ast.parse(
        textwrap.dedent(
            """
            def record_observation(n):
                if n:
                    return record_observation(n - 1)
                return 0
            """
        )
    )
    scoped = detector.scan(tree, _STEMS, skip_def="record_observation")
    assert "record_observation" not in scoped.names

    tree_used = ast.parse(
        textwrap.dedent(
            """
            def record_observation(n):
                return n

            def caller():
                return record_observation(1)
            """
        )
    )
    used = detector.scan(tree_used, _STEMS, skip_def="record_observation")
    assert "record_observation" in used.names, (
        "a helper called by its own module IS wired — excluding the home file "
        "wholesale is what reported money_evidence.classify_gaps dead while its "
        "own line 161 called it"
    )


def test_a_decorated_handler_counts_as_mounted(tmp_path: Path) -> None:
    """`@router.post(...)` mounts a function nothing ever calls by name."""
    home = tmp_path / "fake_routes.py"
    home.write_text(
        textwrap.dedent(
            """
            @router.post("/api/things")
            async def create_thing(payload):
                return payload

            def plain_helper():
                return 1
            """
        ),
        encoding="utf-8",
    )
    assert detector.mounted_by_decorator(home, "create_thing")
    assert detector.mounted_by_decorator(home, "plain_helper") is None


# ---------------------------------------------------------------------------
# Search perimeter
# ---------------------------------------------------------------------------


def test_connectors_are_searched_for_callers() -> None:
    """`server/modules/` is production code.

    The 2026-08-01 false positive was only *visible* because the caller lived in
    a connector. Had the sweep stopped at `server/core`, the wrong verdict would
    have looked right.
    """
    swept = list(detector._sources())
    assert any("modules" in p.parts for p in swept), "server/modules/ is not swept"
    assert any(p.parts[-2:] == ("core", "time_boundary.py") for p in swept)
    assert not any("tests" in p.parts for p in swept), "tests are not callers"


def test_a_production_module_whose_name_contains_test_is_not_skipped() -> None:
    """`_is_test` used to be `"test" in path.name` — `latest_*.py` contains it."""
    assert not detector._is_test(Path("server/core/latest_boundary.py"))
    assert not detector._is_test(Path("server/core/attestation.py"))
    assert detector._is_test(Path("server/tests/core/anything.py"))
    assert detector._is_test(Path("server/core/test_helper.py"))
    assert detector._is_test(Path("server/core/conftest.py"))
