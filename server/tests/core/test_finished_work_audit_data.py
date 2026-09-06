from __future__ import annotations

import runpy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
AUDIT = runpy.run_path(str(REPO_ROOT / "scripts" / "finished_work_audit.py"))


def _audit_fixture(tmp_path: Path) -> tuple[Path, Path]:
    ui = tmp_path / "ui"
    navigation = ui / "shell" / "navigation.ts"
    router = ui / "shell" / "ContentRouter.tsx"
    navigation.parent.mkdir(parents=True)
    navigation.write_text(
        "\n".join(
            [
                'section("data-overview", "Data Overview", [])',
                'section("datastreams", "Datastreams", [])',
                (
                    'section("events", "Events", [{ type: "event-configuration", '
                    'tabs: ["overview", "source-mapping", "collection", "usage"] }])'
                ),
                (
                    'section("sources", "Sources", [{ type: "source-account", '
                    'tabs: ["overview", "accounts", "health", "used-by"] }])'
                ),
                (
                    'section("imports", "Imports", [{ type: "import", '
                    'tabs: ["overview", "raw-evidence", "validation", "publication"] }])'
                ),
                (
                    'section("connectors", "Connectors", [{ type: "connector", '
                    'tabs: ["overview", "capabilities", "coverage", "versions"] }])'
                ),
            ]
        ),
        encoding="utf-8",
    )
    collections = ("data-overview", "datastreams", "events", "sources", "imports", "connectors")
    object_types = ("source-account", "import", "event-configuration", "connector")
    router.write_text(
        'import DataObjectWorkbench from "../data/DataObjectWorkbench"\n'
        + "\n".join(f'"data/{slug}"' for slug in collections)
        + "\n"
        + "\n".join(f'type: "{object_type}"' for object_type in object_types),
        encoding="utf-8",
    )
    for relative in AUDIT["DATA_MIGRATED_FILES"]:
        target = ui / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export default function Surface() { return null }\n", encoding="utf-8")
    return ui, router


def test_data_completeness_contract_accepts_the_six_lenses_and_four_workbenches(tmp_path):
    ui, router = _audit_fixture(tmp_path)

    assert AUDIT["data_surface_contract_findings"](ui, router) == []


def test_data_completeness_contract_rejects_a_legacy_collection_join(tmp_path):
    ui, router = _audit_fixture(tmp_path)
    tree = ui / "shell" / "DataTree.tsx"
    tree.write_text('fetch("/api/datastreams?project_id=p1")\n', encoding="utf-8")

    findings = AUDIT["data_surface_contract_findings"](ui, router)

    assert any("legacy Datastream collection join" in finding for finding in findings)


# ---------------------------------------------------------------------------
# THE INSTRUMENT READ ITS OWN COMMENTS (repaired 2026-09-02).
#
# `ui/admin/src/shell/DataTree.tsx` imports no stylesheet at all, and its header
# comment at line 6 says so in as many words -- "`application.css` is not
# loaded". The scan matched raw file text, so that sentence WAS the finding, and
# it stood as one of the repository's two structural findings. Each test below
# is a pair: the prose must go quiet, and a fabricated real offender must still
# be named, because a masker that silences both is worse than no masker.
# ---------------------------------------------------------------------------


def test_a_banned_pattern_quoted_in_a_comment_is_not_a_finding(tmp_path):
    ui, router = _audit_fixture(tmp_path)
    tree = ui / "shell" / "DataTree.tsx"
    tree.write_text(
        "// The mark had no size wherever `application.css` is not loaded --\n"
        "// which is every sandbox capture. That component is now gone.\n"
        "/* The legacy join `/api/datastreams?project_id=` went with it. */\n"
        "export default function DataTree() { return null }\n",
        encoding="utf-8",
    )

    assert AUDIT["data_surface_contract_findings"](ui, router) == []


def test_the_same_pattern_in_code_is_still_named(tmp_path):
    """The mutation: put the defect back, as code, and the rule must speak."""
    ui, router = _audit_fixture(tmp_path)
    tree = ui / "shell" / "DataTree.tsx"
    tree.write_text(
        "// `application.css` is not loaded here, and never was.\n"
        'import "../shell/application.css";\n'
        "export default function DataTree() { return null }\n",
        encoding="utf-8",
    )

    findings = AUDIT["data_surface_contract_findings"](ui, router)

    assert any("legacy application stylesheet dependency" in f for f in findings)


def test_a_double_slash_inside_a_url_does_not_blank_the_call_after_it(tmp_path):
    """Why the TSX branch is a scanner and not a regex.

    `//` opens the middle of every URL. A regex that blanks from it eats the rest
    of the line -- and the rest of the line is exactly where the banned call is.
    """
    ui, router = _audit_fixture(tmp_path)
    tree = ui / "shell" / "DataTree.tsx"
    tree.write_text(
        'fetch("https://console.example.com/api/datastreams?project_id=p1");\n',
        encoding="utf-8",
    )

    findings = AUDIT["data_surface_contract_findings"](ui, router)

    assert any("legacy Datastream collection join" in f for f in findings)


def _colour_sweep(tmp_path: Path, files: dict[str, str]) -> list[str]:
    """`stylesheet_drift` against a throwaway UI tree instead of the real one."""
    ui = tmp_path / "ui"
    for relative, text in files.items():
        target = ui / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    namespace = AUDIT["stylesheet_drift"].__globals__
    saved = namespace["UI"]
    try:
        namespace["UI"] = ui
        return AUDIT["stylesheet_drift"]()
    finally:
        namespace["UI"] = saved


def test_an_exempt_colour_is_not_counted_but_a_new_one_in_the_same_file_is(tmp_path):
    """The exemption names COLOURS, never a path.

    Exempting `shell/AuthGate.tsx` wholesale would make every future colour in
    it invisible -- a silent baseline wearing a reason. The boot screen may spell
    the four it needs before any theme exists; a fifth is a finding.
    """
    boot = 'const s = { background: "#0f1115", color: "#f5f5f5" };\n'

    quiet = _colour_sweep(tmp_path / "a", {"shell/AuthGate.tsx": boot})
    assert not any("hardcoded colour" in f for f in quiet)

    loud = _colour_sweep(
        tmp_path / "b",
        {"shell/AuthGate.tsx": boot + 'const extra = { color: "#c0ffee" };\n'},
    )
    assert any(f.startswith("1 hardcoded colour") for f in loud)


def test_a_colour_written_only_in_a_comment_is_not_a_violation(tmp_path):
    """`ui/NavItem.tsx:20-29` writes the token table it REPLACED, hex by hex."""
    quiet = _colour_sweep(
        tmp_path / "a",
        {"ui/NavItem.tsx": "/* .subnav-item.active background #f4f3f8 -> surface-subtle */\n"},
    )
    assert not any("hardcoded colour" in f for f in quiet)

    loud = _colour_sweep(
        tmp_path / "b",
        {"ui/NavItem.tsx": 'const style = { background: "#f4f3f8" };\n'},
    )
    assert any(f.startswith("1 hardcoded colour") for f in loud)


def test_an_exemption_that_outlived_its_cause_is_reported(tmp_path):
    """An allowance nobody is watching would cover the day the value comes back."""
    ui = tmp_path / "ui"
    (ui / "shell").mkdir(parents=True)
    (ui / "shell" / "AuthGate.tsx").write_text("export default null\n", encoding="utf-8")
    namespace = AUDIT["self_check"].__globals__
    saved = namespace["UI"]
    try:
        namespace["UI"] = ui
        findings = AUDIT["self_check"]()
    finally:
        namespace["UI"] = saved

    assert any("no longer carries exempt colour" in f for f in findings)


def test_the_mount_scan_reads_the_import_shape_the_workbench_pages_use(tmp_path):
    """`known-debt.json` carried `MappingVersionLedger.tsx` as mounted nowhere.

    It is imported and rendered at
    `ui/admin/src/datastreams/workbench/pages/WorkbenchMappingPage.tsx:38` and
    `:341`, through a plain default import -- the shape reproduced here. The live
    sweep already reads it (`orphan_components()` -> `[]`), so the defect was the
    RECORD, not the rule; this pins the rule so the record cannot be wrong again
    without a red test, and the second file is the mutation.
    """
    ui = tmp_path / "ui"
    pages = ui / "datastreams" / "workbench" / "pages"
    mapping = ui / "datastreams" / "workbench" / "mapping"
    pages.mkdir(parents=True)
    mapping.mkdir(parents=True)
    (mapping / "MappingVersionLedger.tsx").write_text(
        "export default function MappingVersionLedger() { return null }\n", encoding="utf-8"
    )
    (mapping / "NeverMounted.tsx").write_text(
        "export default function NeverMounted() { return null }\n", encoding="utf-8"
    )
    (pages / "WorkbenchMappingPage.tsx").write_text(
        'import MappingVersionLedger from "../mapping/MappingVersionLedger";\n'
        "export default function WorkbenchMappingPage() {\n"
        "  return <MappingVersionLedger />;\n"
        "}\n",
        encoding="utf-8",
    )
    namespace = AUDIT["orphan_components"].__globals__
    saved_ui, saved_root = namespace["UI"], namespace["ROOT"]
    try:
        namespace["UI"], namespace["ROOT"] = ui, tmp_path
        orphans = AUDIT["orphan_components"]()
    finally:
        namespace["UI"], namespace["ROOT"] = saved_ui, saved_root

    assert not any("MappingVersionLedger" in o for o in orphans)
    assert any("NeverMounted" in o for o in orphans)


def test_an_accent_folding_table_is_data_and_a_french_sentence_is_still_copy():
    """The one "non-English string on screen" left was `str.maketrans`'s left side.

    Copy is made of words, and a word carries an ASCII letter or a space beside
    it. A run of accented characters with neither is a character class.
    """
    is_table = AUDIT["_is_character_table"]

    assert is_table("àáâãäåçèéêëìíîïñòóôõöùúûüýÿ")
    assert not is_table("Aucune donnée disponible")
    assert not is_table("Données")


# ---------------------------------------------------------------------------
# The gate and the baseline, repaired 2026-07-31. Both defects were the same
# shape: a mechanism written, documented in a comment, and never actually
# reached -- one by an unread flag, one by a rewrite that dropped what it did
# not know about.
# ---------------------------------------------------------------------------


def _gate(payload: dict, orphans: list[str]) -> tuple[int, str]:
    """Run gate() with a synthetic finding and a given Stop-hook payload."""
    import contextlib
    import io
    import json
    import sys

    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            code = AUDIT["gate"]([], orphans)
    finally:
        sys.stdin = saved
    return code, err.getvalue()


FAKE_FINDING = ["ui/admin/src/__does_not_exist__/Widget.tsx"]


def test_the_gate_blocks_the_first_time_it_sees_a_new_finding() -> None:
    code, out = _gate({}, FAKE_FINDING)

    assert code == 2
    assert "Widget.tsx" in out


def test_the_gate_reports_and_lets_go_once_it_has_already_blocked() -> None:
    """`stop_hook_active` was computed and never read (F841).

    The comment in gate() always promised the second pass "reports and lets go";
    it did not, so the gate blocked every stop until someone acted. When the
    finding belongs to another session's in-flight file, nobody present CAN act
    -- on 2026-07-31 that cost a session five consecutive turns.
    """
    code, out = _gate({"stop_hook_active": True}, FAKE_FINDING)

    assert code == 0
    # Letting go is not going quiet: the finding is still printed in full.
    assert "Widget.tsx" in out
    assert "not blocking again" in out


def test_the_gate_names_the_pending_route_before_the_destructive_one() -> None:
    """The advice used to be one line pointing straight at --baseline."""
    _, out = _gate({}, FAKE_FINDING)

    assert out.index("pending") < out.index("--baseline")
    assert "another session" in out.lower()
    assert "accepts EVERY current finding" in out


def _baseline_on(debt: Path, orphans: list[str]) -> dict:
    """Run baseline() against a throwaway file, never the repository's own.

    `runpy.run_path` returns a COPY of the module globals, so assigning into
    AUDIT does nothing to what the function reads -- the first version of these
    tests did exactly that and wrote to the real `known-debt.json` twice. The
    live namespace is `func.__globals__`, and the assertion at the end is the
    belt: if the redirection ever stops working, the test fails instead of
    quietly editing the repository.
    """
    import json

    real = REPO_ROOT / "docs" / "product-architecture" / "known-debt.json"
    real_before = real.read_text(encoding="utf-8") if real.exists() else None

    namespace = AUDIT["baseline"].__globals__
    saved = namespace["BASELINE"]
    try:
        namespace["BASELINE"] = debt
        AUDIT["baseline"]([], orphans)
    finally:
        namespace["BASELINE"] = saved

    assert real.read_text(encoding="utf-8") == real_before, (
        "the test wrote to the repository's known-debt.json instead of its fixture"
    )
    return json.loads(debt.read_text(encoding="utf-8"))


def test_baseline_preserves_the_prose_and_other_sessions_pending_findings(tmp_path: Path) -> None:
    """`--baseline` rewrote the file from four keys and dropped the rest.

    `_README`, `_NOTES` and the whole `pending` bucket went with every run --
    and `pending` is where sessions record each other's unfinished work so it
    does NOT become permanent. The command the gate recommends was a way to
    erase that record.
    """
    import json

    debt = tmp_path / "known-debt.json"
    debt.write_text(
        json.dumps(
            {
                "_README": "prose that must survive",
                "_NOTES": "more prose",
                "orphans": [],
                "structural": [],
                "plumbing": [],
                "reimplemented_count": 0,
                "pending": [
                    {"finding": "someone/elses/InFlight.tsx", "owner": "epic-50",
                     "reason": "in flight", "seen": "2026-07-31"},
                ],
            }
        ),
        encoding="utf-8",
    )

    after = _baseline_on(debt, [])

    assert after["_README"] == "prose that must survive"
    assert after["_NOTES"] == "more prose"
    assert [e["finding"] for e in after["pending"]] == ["someone/elses/InFlight.tsx"]


def test_baseline_answers_a_pending_finding_instead_of_listing_it_twice(tmp_path: Path) -> None:
    """Accepting a finding answers its pending entry; it never sits in both."""
    import json

    debt = tmp_path / "known-debt.json"
    debt.write_text(
        json.dumps(
            {
                "orphans": [],
                "structural": [],
                "plumbing": [],
                "reimplemented_count": 0,
                "pending": [
                    {"finding": "mine/Orphan.tsx", "owner": "me", "reason": "x",
                     "seen": "2026-07-31"},
                ],
            }
        ),
        encoding="utf-8",
    )

    after = _baseline_on(debt, ["mine/Orphan.tsx"])

    assert after["orphans"] == ["mine/Orphan.tsx"]
    assert after["pending"] == []


def test_a_shrinking_list_finding_is_not_a_new_finding() -> None:
    """A finding whose payload is a SET is compared member by member.

    `known-debt.json` had accumulated twenty-six exemptions for the legacy
    analytics census alone: every time one locator was classified the joined
    list changed, so the same standing finding read as brand new and the gate
    demanded a fresh exemption for work that was ADVANCING.
    """
    is_regression = AUDIT["_is_regression"]
    known = {"historical-reader census has unclassified locators: a.py, b.py, c.py"}

    assert not is_regression(
        "historical-reader census has unclassified locators: a.py, c.py", known
    )


def test_a_new_member_still_blocks_even_when_the_list_shrank() -> None:
    """Member-wise comparison does not soften the gate, only its churn."""
    is_regression = AUDIT["_is_regression"]
    known = {"historical-reader census has unclassified locators: a.py, b.py, c.py"}

    assert is_regression(
        "historical-reader census has unclassified locators: a.py, d.py", known
    )


def test_a_list_finding_under_another_phrase_is_new() -> None:
    """Coverage is granted by the phrase that names WHAT is listed, not by any list."""
    is_regression = AUDIT["_is_regression"]
    known = {"historical-reader census has unclassified locators: a.py, b.py"}

    assert is_regression(
        "mcp-binding census has unclassified locators: a.py, b.py", known
    )


def test_the_legacy_inventory_finding_no_longer_announces_itself_by_a_count() -> None:
    """The blocking sentence must not move when the census moves."""
    checker = runpy.run_path(
        str(REPO_ROOT / "scripts" / "check_legacy_analytics_migration.py")
    )
    summary = checker["InventorySummary"](
        paths=490,
        by_disposition={"blocked": 217, "migrated": 0, "retained": 273},
        by_mode={"delegated": 0, "removed": 0, "retained": 490},
        retirement_ready=False,
    )
    moved = checker["InventorySummary"](
        paths=502,
        by_disposition={"blocked": 217, "migrated": 0, "retained": 285},
        by_mode={"delegated": 0, "removed": 0, "retained": 502},
        retirement_ready=False,
    )

    assert summary.stable_line() == moved.stable_line()
    assert not any(character.isdigit() for character in summary.stable_line().split("65.10")[0])
    # The counters stay visible where a number belongs -- the report note.
    assert "490" in summary.line()
