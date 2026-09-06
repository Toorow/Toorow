"""Downstream Epic 37 contracts must follow the ratified Country capability."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EPIC_37 = REPO_ROOT / "_bmad-output/planning-artifacts/epic-37-geographic-reporting-posture.md"
EPICS = REPO_ROOT / "_bmad-output/planning-artifacts/epics.md"
REVIEW = REPO_ROOT / "_bmad-output/implementation-artifacts/reviews/review-epic-37.md"
REPORTS = REPO_ROOT / "server/core/reports.py"
CAPABILITY_COMPILERS = REPO_ROOT / "server/core/capability_compilers.py"
FEE_TAX_BRIDGE = REPO_ROOT / "server/core/fee_tax_geo_bridge.py"
FEE_TAX_CASCADE_SQL = REPO_ROOT / "dbt/models/marts/fee_tax_country_resolution.sql"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_country_activation_and_editor_keep_their_ratified_owners() -> None:
    epic = _read(EPIC_37)
    catalog = _read(EPICS)

    for contract in (epic, catalog):
        assert "Data > Modules > Country split" not in contract
        assert "Project Settings" in contract
        assert "Governance > Master Data" in contract


def test_qualified_presets_are_editable_drafts_not_platform_defaults() -> None:
    epic = _read(EPIC_37)

    assert "prequalified" in epic
    assert "editable Project draft" in epic
    assert "no market composition, preset or client-specific alias" not in epic


def test_epic_review_matrix_covers_the_country_capability_boundary() -> None:
    review = _read(REVIEW)

    assert "Country capability boundary" in review
    for layer in (
        "Project Settings",
        "Datastream compilation",
        "Governance > Master Data",
        "Analyze",
        "Context Hub",
        "Test",
        "MCP",
    ):
        assert layer in review


def test_analyze_and_tax_read_the_same_published_country_projection() -> None:
    """`capabilities/country.md`: Tax may not use a second geographic mapping.

    This guard named `server/core/fee_tax_mcp.py` until 2026-08-17, and it went red
    for the wrong reason: Story 48.4 removed the four Tax-specific MCP tools, so the
    module it watched no longer reads geography at all. Re-aiming it at the modules
    that DO is not the fix -- the fix was that the Tax CASCADE really did read a second
    mapping (`app.project_preferences`), which Story 37.9 removed. The assertions below
    are what that removal has to keep true.
    """

    reports = _read(REPORTS)
    compilers = _read(CAPABILITY_COMPILERS)

    shared_import = "from core.country_registry import load_projection"
    assert shared_import in reports
    assert shared_import in compilers
    assert "return load_projection(conn, project_id=project_id)" in reports
    # The Tax proposal path pins the exact hierarchy version it read.
    assert "projection = load_projection(" in compilers
    assert "version_id=hierarchy_version_id" in compilers
    assert "geographic_posture: object | None = None" in reports


def test_the_tax_cascade_reads_no_second_geographic_mapping() -> None:
    """Both engines of the cascade read the PUBLISHED hierarchy, never the preference.

    `project_preferences.geographic_mode` / `local_markets` are what
    `country_registry.py` says it "replaces outright", and the Country capability
    confirmation never writes them back -- so a Project governed through the ratified
    capability presented the cascade an empty posture and had every spend row reported
    unresolvable while its geography was fully published.
    """

    bridge = _read(FEE_TAX_BRIDGE)
    cascade = _read(FEE_TAX_CASCADE_SQL)

    # The Python twin reasons from the published projection and holds no posture type.
    # Checked on the IMPORTED module, not on the text: the docstring names the retired
    # type on purpose, to say what moved and why.
    from core import fee_tax_geo_bridge

    assert "GovernedGeography.from_projection" in bridge
    assert "geography: GovernedGeography" in bridge
    assert "from core.geographic_reporting import" not in bridge
    assert not hasattr(fee_tax_geo_bridge, "GeographicPosture")
    assert not hasattr(fee_tax_geo_bridge, "resolve_market_binding")

    # The SQL twin reads the mirrored projection and no preference column. Asserted on
    # the source() calls, which is the ONLY way a dbt model can read a mirror relation --
    # the header names the retired columns deliberately, to record what moved.
    assert "source('mirror', 'country_market_projection')" in cascade
    assert "source('mirror', 'project_preferences')" not in cascade
    for retired_column in ("p.local_markets", "p.geographic_mode", "l.geographic_mode"):
        assert retired_column not in cascade, retired_column


def test_both_engines_share_one_gap_vocabulary_for_the_country_model() -> None:
    """A gap must name a surface that can repair it, in the same words on both sides."""

    bridge = _read(FEE_TAX_BRIDGE)
    cascade = _read(FEE_TAX_CASCADE_SQL)

    for word in (
        "no_binding_and_country_model_absent",
        "no_binding_and_multiple_countries_governed",
    ):
        assert word in bridge, word
        assert word in cascade, word

    # The retired words named a posture no surface writes any more.
    for retired in ("no_binding_and_posture_global", "no_binding_and_posture_multi_country"):
        assert f'"{retired}"' not in bridge, retired
        assert f"'{retired}'" not in cascade, retired


#: The ONLY modules allowed to reach `app.project_preferences.geographic_mode` /
#: `local_markets`, and each is a LEGACY DOOR rather than a consumer:
#:
#: * `geographic_reporting` DEFINES the reader and owns the compatibility projection;
#: * `projects_api` is the PATCH endpoint that still writes the preference columns --
#:   closing that door is a product decision about an endpoint, not a repair, and it
#:   reads its own write back to return it.
#:
#: TWO. `geographic_change` used to be a third exception -- the pre-48.2 preview/confirm
#: path, mounted on no route -- and it was REMOVED rather than excepted, so the list is
#: one shorter and no entry here stands for a file that no longer exists. The review
#: narrative and the story-log said "three" until 2026-08-22; they now say what the code
#: says.
#:
#: A ratchet rather than prose: three live CONSUMERS were still on this reader after the
#: Tax cascade moved off it, and each failed silently for the Projects that govern
#: Country properly. The next one must fail this test instead.
_POSTURE_READER_EXCEPTIONS = frozenset(
    {"geographic_reporting.py", "projects_api.py"}
)

#: Where a consumer could live. The first version of this ratchet scanned `server/core`
#: alone, so `server/modules`, `server/inbound`, `scripts`, `dbt` and both front-ends
#: passed underneath it untested -- a guard written against one symbol in one subtree,
#: which is the class the 2026-08-21 review named. Widened 2026-08-22.
_CONSUMER_ROOTS = (
    "server/core",
    "server/inbound",
    "server/modules",
    "scripts",
    "dbt/models",
    "dbt/macros",
    "dbt/seeds",
    "dbt/tests",
    "ui/admin/src",
    "web/src",
)
_CONSUMER_SUFFIXES = {".py", ".sql", ".ts", ".tsx", ".astro", ".yml", ".yaml"}

#: Not a consumer: it declares the SHAPE of `mirror.project_preferences` so a local
#: warehouse can be built without Postgres. `mirror_sync` copies that table with
#: `SELECT *`, so the mirror carries the retired columns whatever anyone reads; the
#: seeder reproducing that shape derives no geography from them.
_SHAPE_DECLARATIONS = frozenset({"seed_fee_tax_mirror.py"})

#: A call to the helper, and a hand-written SELECT that reaches the same columns from
#: the same relation. Forbidding only the FUNCTION NAME leaves the second shape legal
#: everywhere, and a reader that spells its query out by hand is the same defect.
_POSTURE_CALL = re.compile(r"fetch_project_geographic_posture\s*\(")
_POSTURE_SELECT = re.compile(
    r"(?is)\bselect\b[^;]{0,600}?"
    r"\b(geographic_mode|local_markets|local_market_country_codes)\b"
    r"[^;]{0,600}?\bfrom\b[^;]{0,300}?\bproject_preferences\b"
)


def _without_comments(text: str, suffix: str) -> str:
    """CALL SITES AND QUERIES ONLY.

    Several modules name the retired reader in prose, on purpose, to record what moved
    and why -- and a guard that counted those would push the explanation out of the code
    to stay green. That is exactly what the first version of this ratchet did.
    """

    if suffix in {".py", ".yml", ".yaml"}:
        return re.sub(r"#[^\n]*", "", text)
    if suffix == ".sql":
        return re.sub(r"--[^\n]*", "", text)
    return re.sub(r"//[^\n]*", "", text)


def _consumer_files() -> list[Path]:
    files: list[Path] = []
    for root in _CONSUMER_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in _CONSUMER_SUFFIXES:
                continue
            parts = set(path.parts)
            if "__pycache__" in parts or "target" in parts or "node_modules" in parts:
                continue
            if path.name in _POSTURE_READER_EXCEPTIONS or path.name in _SHAPE_DECLARATIONS:
                continue
            files.append(path)
    return files


def test_no_live_consumer_reads_the_retired_geographic_posture() -> None:
    """One authority means one reader. `country_activation.governed_posture` is it."""

    offenders: list[str] = []
    for path in _consumer_files():
        code = _without_comments(path.read_text(encoding="utf-8", errors="replace"), path.suffix)
        relative = path.relative_to(REPO_ROOT).as_posix()
        if _POSTURE_CALL.search(code):
            offenders.append(f"{relative} (calls the retired reader)")
        if _POSTURE_SELECT.search(code):
            offenders.append(f"{relative} (selects the retired columns by hand)")

    assert not offenders, (
        "these modules read `project_preferences.geographic_mode` / `local_markets`, "
        "which the ratified Country capability replaced and never writes back: "
        f"{offenders}. A Project that governs Country properly presents an EMPTY "
        "preference row there, so the read returns Global and the consumer goes quiet "
        "-- a DQ monitor that never fires, a plan compiled without geography, a report "
        "described as consolidated. Read `country_activation.governed_posture` instead."
    )


def test_the_ratchet_reaches_past_server_core() -> None:
    """The guard is judged on its SCOPE, not on its verdict.

    The 2026-08-21 review's finding, in one assertion: a ratchet that scans one subtree
    reports a clean tree it never looked at. If someone narrows the roots again, or drops
    the hand-written-query shape, this fails before the silence does.
    """

    scanned = {path.relative_to(REPO_ROOT).parts[0] for path in _consumer_files()}
    assert {"server", "scripts", "dbt", "ui"} <= scanned, scanned

    scanned_paths = [path.relative_to(REPO_ROOT).as_posix() for path in _consumer_files()]
    assert scanned_paths, "the ratchet scans nothing"
    assert any(path.startswith("server/modules/") for path in scanned_paths), (
        "connector modules are outside the scan again"
    )

    # And both shapes are still refused, proven on text the scanner would meet.
    assert _POSTURE_CALL.search("posture = fetch_project_geographic_posture(pid, conn)")
    assert _POSTURE_SELECT.search(
        "SELECT geographic_mode, local_markets FROM app.project_preferences WHERE id = %s"
    )
    assert not _POSTURE_SELECT.search(
        "SELECT * FROM app.project_preferences"
    ), "a `SELECT *` mirror copy is not a geography consumer"


def test_the_one_governed_reader_gates_on_the_capability_state() -> None:
    """Global must be an ANSWER here, never the residue of a failed read.

    Three states have to be told apart and each has its own honest reply: the capability
    is off (Global -- deactivation returns reports to consolidated behaviour), it is on
    with nothing published yet (Global -- an empty `local_markets` posture is refused
    outright by `normalize_geographic_posture`), and it is on with a published version
    (the tracked markets). Collapsing any two would either un-group a governed Project
    or throw from inside a nightly monitor.
    """

    source = _read(REPO_ROOT / "server/core/country_activation.py")
    assert "def governed_posture(" in source
    assert "GOVERNED_CAPABILITY_STATES = frozenset({\"ready\", \"degraded\"})" in source
    assert "FROM app.project_capabilities" in source
    # The tracked set must exclude the catch-all, exactly as the cascade does.
    assert "from core.country_registry import MARKET" in source
