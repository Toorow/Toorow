"""Story 41.4 (Epic 41) -- the STRUCTURAL half of AC3 / AC10 / AC11.

The numeric half lives in dbt/tests/test_epic41_verification_*.sql. This is the half a
number cannot prove.

KEEP_SEPARATE (Epic 27 invariant 4) says verification cost is an OVERLAY, never summed
into a native total. In ``server/core/metric_reconciliation.py`` that is made structural by
``_keep_separate_decision`` returning ``target_mart=None`` -- THERE IS NO PLACE TO PUT A
COMBINED NUMBER -- and by ``money_reconciliation._NO_AMOUNT_STATUSES`` listing
``KEEP_SEPARATE`` among the statuses that carry no combined figure at all. This module
reproduces that posture in the warehouse with THREE INDEPENDENT GUARDS, so defeating one
is not enough:

  Guard 1 -- DAG ISOLATION, BIDIRECTIONAL, asserted from the compiled manifest.
    ``fee_tax_verification_allocation`` does not ``ref()`` either ladder model, and neither
    ladder ``ref()``s it. There is therefore NO DATA PATH from the overlay into any ladder
    total, whatever anyone writes in either file -- which is exactly why the overlay reads
    ``fact_daily_kpi`` for the spend link rather than the ladder: it keeps the two
    subgraphs DISJOINT, not merely acyclic.

    A READER WILL WONDER ABOUT THE SHARED MACRO, so it is answered here: after Task 1 both
    models call ``fee_tax_condition_matcher``. A MACRO IS COMPILED TEXT, NOT A DATA EDGE --
    dbt inlines it into each model's own SQL and records no dependency between the two
    models. The DAG stays disjoint and Guard 1 is unaffected.

  Guard 2 -- THE MONEY-COLUMN BAN, a greppable property of the overlay's own output. Its
    only ``*_micros`` output column is ``verification_cost_micros``, and its executable SQL
    names none of the ladder's money columns. Cost rows are read for their KEYS ONLY. To
    sum verification into a total, someone must FIRST CREATE A COLUMN TO SUM IT INTO -- and
    that fails here before it can be wrong.

  Guard 3 -- the twin-project byte-identity differential. That one is numeric and lives in
    dbt/tests/test_epic41_verification_ladder_untouched.sql; it is named here so the three
    guards are documented in one place.

Plus AC10 (the matcher has exactly ONE definition site) and AC11 (file discipline).

Pattern: server/tests/conformance/test_epic41_additive_only.py, whose ``_OWNED_MODELS`` is
a FIXED tuple of three -- adding a fourth model does not break it, and this file is the
fourth model's own conformance suite rather than an edit to 41.3's.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DBT = _REPO_ROOT / "dbt"
_MANIFEST = _DBT / "target" / "manifest.json"

_OVERLAY = "fee_tax_verification_allocation"
_OVERLAY_UID = f"model.connector.{_OVERLAY}"
_OVERLAY_SQL = _DBT / "models" / "marts" / f"{_OVERLAY}.sql"
_MATCHER_MACRO = _DBT / "macros" / "fee_tax_condition_matcher.sql"
_LADDER_SQL = _DBT / "models" / "marts" / "fee_tax_ladder_daily.sql"

_LADDER_UIDS = (
    "model.connector.fee_tax_ladder_daily",
    "model.connector.fee_tax_ladder_rollup",
)

# A node belongs to Epic 41 when its unique_id carries one of these tokens (41.3's
# convention, which also catches dbt's auto-generated generic-test names).
_EPIC41_TOKENS = ("fee_tax", "epic41")

# What the overlay is allowed to read. mirror.plan_line_mappings is DELIBERATELY ABSENT:
# plan_version-scoped VERIFICATION rules are refused on purpose (applying a SPEND weight to
# an IMPRESSION count would invent an allocation) and the LINKED_VIA_PLAN_LINE rung is
# reserved-not-implemented (ruling Q2). Neither ruling is worth anything if the edge exists
# anyway, so its absence is asserted rather than assumed.
_ALLOWED_DEPENDENCIES = {
    "model.connector.fact_daily_kpi",
    "model.connector.fee_tax_country_resolution",
    "model.connector.fee_tax_rules_effective",
    "source.connector.mirror.project_preferences",
    # Story 48.4: the ONE Tax & Fees activation authority. It SUBSTITUTES for
    # project_preferences in the activation role -- that column was a second
    # boolean beside app.project_capabilities and these models believed it.
    "source.connector.mirror.project_tax_fee_activation",
    "source.connector.mirror.fee_tax_rule_conditions",
    "seed.connector.epic41_verification_fixture",
}

# §B.5, verbatim from 41.3's list. Applied to the overlay AND to the extracted matcher
# macro: the extraction must not smuggle a dialect-specific construct into a place the
# 41.3 conformance test does not look.
_BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"::", "the postfix :: cast is DuckDB/Postgres syntax, not BigQuery"),
    (r"\bDOUBLE\b", "DOUBLE is not a BigQuery type and destroys exact decimal arithmetic"),
    (r"\bVARCHAR\b", "VARCHAR is DuckDB; use STRING, which both engines accept"),
    (r"\bTEXT\b", "TEXT is not a BigQuery type"),
    (r"\bDECIMAL\s*\(", "a parameterised DECIMAL(p,s) must live inside a macro branch"),
    (r"\bNUMERIC\s*\(", "a parameterised NUMERIC(p,s) must live inside a macro branch"),
    (r"\bQUALIFY\b", "QUALIFY is not portable; use ROW_NUMBER() + WHERE _rn = 1"),
    (r"\blist_contains\b", "list_contains is DuckDB-only"),
    (r"\bstring_split\b", "string_split is DuckDB-only"),
    (r"\bjson_extract\b", "no JSON reaches dbt (decision D1) -- the mirror is relational"),
    (r"\bDATE\s*-", "DATE - DATE is DuckDB-only; use the days_between macro"),
)

# Guard 2. Every money column of fee_tax_ladder_daily. None may be NAMED in the overlay's
# executable SQL: the spend side is read for KEYS ONLY.
_LADDER_MONEY_TOKENS = (
    "net_media",
    "subtotal_ht",
    "total_ttc",
    "platform_fee",
    "regulatory_tax",
    "wht_gross_up",
    "agency_fee",
    "sales_tax",
)

# Ruling R3: 41.2 owns attribute resolution; the overlay owns none of it.
_R3_FORBIDDEN = (
    "dim_country",
    "datastream_country_binding_dim",
    "datastream_source_types",
    "market_bindings",
)

# AC10. The signature of cond_gap's CASE chain -- distinctive enough that a copy-paste of
# the matcher anywhere under dbt/ carries it.
_MATCHER_SIGNATURE = "attr_unresolved = 1 AND condition_key ="

# Shared YAML/config files this story may not add its entries to (AC11 / check 43).
# schema_fee_tax.yml is in the list because 41.4 must not write there either -- 41.3 owns
# it and 41.5 is concurrently editing it.
_SHARED_CONFIG_FILES = (
    "models/marts/schema.yml",
    "models/marts/schema_fee_tax.yml",
    "seeds/schema.yml",
    "seeds/schema_epic41.yml",
    "dbt_project.yml",
)

# AC11 / Q1. Files this story is forbidden to modify AND that no concurrently-running
# story was reported to be in, so a diff here is unambiguously 41.4's fault.
# DELIBERATELY EXCLUDED, and this is a scope decision rather than an oversight:
# schema_fee_tax.yml, fee_tax_rules.py, migration 119, fee_tax_country_resolution.sql --
# Story 41.5's dev is live in those, so asserting them byte-identical to HEAD would fail
# for someone else's reason and teach people to ignore this test.
_MUST_BE_UNCHANGED = (
    "dbt/seeds/feetax/seed_fee_tax_mirror.py",
    "dbt/dbt_project.yml",
    "dbt/models/marts/schema.yml",
    "dbt/seeds/schema.yml",
    "dbt/seeds/schema_epic41.yml",
    "dbt/seeds/epic41_cascade_fixture.csv",
    "dbt/macros/fee_tax_money_math.sql",
    "dbt/macros/fee_tax_to_micros.sql",
)

# 41.4's own fixture namespace. None of it may appear in 41.3's seeder (ruling Q1).
_STORY_41_4_FIXTURE_TOKENS = (
    "feetax_dev_verif",
    "ftr_dev_verif",
    "pull_feetax_41_4",
)

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_SQL_COMMENT = re.compile(r"--[^\n]*")


def _strip_comments(sql: str) -> str:
    """Return the file's EXECUTABLE text.

    The banned-construct, R3 and money-column checks are about the SQL that reaches the
    warehouse, not about prose: these headers necessarily NAME the constructs and the
    columns they forbid (that is what makes them useful to a reviewer), so grepping the raw
    file would forbid documenting the rule. Comments are stripped first; anything left is
    code.
    """
    return _SQL_COMMENT.sub("", _JINJA_COMMENT.sub("", sql))


def _load_manifest() -> dict:
    if not _MANIFEST.exists():
        pytest.skip(
            f"{_MANIFEST} is absent -- run `cd dbt && dbt build` (or at least `dbt parse`)"
            " before this conformance test; the isolation proof is a manifest proof."
        )
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _overlay_node(manifest: dict) -> dict:
    node = manifest["nodes"].get(_OVERLAY_UID)
    assert node is not None, (
        f"{_OVERLAY_UID} is absent from the manifest -- either the model was not created"
        " or the manifest predates it. Re-run `cd dbt && dbt build`."
    )
    return node


def _is_epic41(unique_id: str) -> bool:
    return any(token in unique_id for token in _EPIC41_TOKENS)


def _git_show_head(relative_path: str) -> str | None:
    """The committed content of a path, or None when git cannot answer.

    Returning None (-> a skip) rather than an empty string is deliberate: a missing git is
    an UNKNOWN, and an unknown must never be reported as a pass.
    """
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{relative_path}"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError):  # git absent, or not executable here
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# CHECK 0 -- the files exist (guards every check below).
# ---------------------------------------------------------------------------


def test_story_41_4_files_exist() -> None:
    missing = [
        str(path)
        for path in (
            _OVERLAY_SQL,
            _MATCHER_MACRO,
            _DBT / "macros" / "fee_tax_cpm_micros.sql",
            _DBT / "models" / "marts" / "schema_fee_tax_verification.yml",
            _DBT / "seeds" / "epic41_verification_fixture.csv",
            _DBT / "seeds" / "schema_epic41_verification.yml",
            _DBT / "seeds" / "feetax" / "seed_fee_tax_verification.py",
        )
        if not path.exists()
    ]
    assert not missing, f"Story 41.4 artifacts missing: {missing}"


# ---------------------------------------------------------------------------
# GUARD 1 -- DAG isolation.
# ---------------------------------------------------------------------------


def test_no_node_outside_epic41_depends_on_the_overlay() -> None:
    """CHECK 38 (Guard 1a) -- nothing pre-existing reads the overlay.

    If nothing outside the epic reads it, the overlay cannot move a pre-existing number
    whatever its own arithmetic does (E41-NFR01).
    """
    manifest = _load_manifest()
    offenders: list[str] = []
    for section in ("nodes", "exposures", "metrics"):
        for unique_id, node in manifest.get(section, {}).items():
            if _is_epic41(unique_id):
                continue
            if _OVERLAY_UID in node.get("depends_on", {}).get("nodes", []):
                offenders.append(unique_id)
    assert not offenders, (
        "a node outside the Epic-41 set now depends on the verification overlay, so the"
        f" overlay is no longer additive: {offenders}"
    )


def test_keep_separate_dag_edge_is_absent_in_both_directions() -> None:
    """CHECK 39 (Guard 1b) -- the KEEP_SEPARATE edge does not exist, either way round.

    A shared macro is COMPILED TEXT, NOT A DATA EDGE: Task 1's extraction gives the ladder
    and the overlay a common macro, and dbt inlines it into each model separately without
    recording any dependency between them. The DAG stays disjoint.
    """
    manifest = _load_manifest()
    overlay_deps = set(_overlay_node(manifest).get("depends_on", {}).get("nodes", []))

    forward = [uid for uid in _LADDER_UIDS if uid in overlay_deps]
    assert not forward, (
        "the verification overlay ref()s a cost-ladder model"
        f" ({forward}), which creates the very data path Epic 27 invariant 4 forbids."
        " The spend link must read fact_daily_kpi, not the ladder, precisely so the two"
        " subgraphs stay DISJOINT rather than merely acyclic."
    )

    backward: list[str] = []
    for uid in _LADDER_UIDS:
        node = manifest["nodes"].get(uid)
        if node is None:
            continue
        if _OVERLAY_UID in node.get("depends_on", {}).get("nodes", []):
            backward.append(uid)
    assert not backward, (
        f"a cost-ladder model now depends on the verification overlay ({backward}) --"
        " verification cost has a data path INTO the media cost ladder"
    )


def test_overlay_is_a_view() -> None:
    """CHECK 40 -- materialized: view (E41-NFR03, no state persisted).

    dbt_project.yml defaults marts to `table` and this story may not edit it, so the model
    must override in-file.
    """
    manifest = _load_manifest()
    materialized = _overlay_node(manifest).get("config", {}).get("materialized")
    assert materialized == "view", (
        f"{_OVERLAY} is materialized as {materialized!r}; E41-NFR03 requires a view"
    )


def test_overlay_depends_only_on_the_allowed_set() -> None:
    """CHECK 41 -- no accidental edge, and specifically no plan_line_mappings edge."""
    manifest = _load_manifest()
    deps = set(_overlay_node(manifest).get("depends_on", {}).get("nodes", []))

    offenders = sorted(deps - _ALLOWED_DEPENDENCIES)
    assert not offenders, (
        f"{_OVERLAY} reads a relation outside its declared dependency set: {offenders}"
    )

    assert "source.connector.mirror.plan_line_mappings" not in deps, (
        "the overlay depends on mirror.plan_line_mappings. That table is a MEDIA-PLAN"
        " mapping whose split_weight is a share of a campaign's SPEND -- applying it to an"
        " IMPRESSION count would invent an allocation, and it keys on the SPEND"
        " connector's campaign ref, so it would essentially never match and the project"
        " would report zero verification cost under a clean, complete-looking overlay."
        " plan_version scope is refused on purpose (§D.5.4) and LINKED_VIA_PLAN_LINE is"
        " reserved-not-implemented (ruling Q2)."
    )


# ---------------------------------------------------------------------------
# GUARD 2 -- the money-column ban.
# ---------------------------------------------------------------------------


def test_overlay_names_no_ladder_money_column() -> None:
    """CHECK 42a (Guard 2) -- the spend side is read for KEYS ONLY."""
    code = _strip_comments(_OVERLAY_SQL.read_text(encoding="utf-8"))
    offenders = [token for token in _LADDER_MONEY_TOKENS if token in code]
    assert not offenders, (
        f"{_OVERLAY}'s executable SQL names ladder money column(s) {offenders}."
        " Verification cost is a KEEP_SEPARATE overlay: cost rows may be read for their"
        " KEYS only, and no ladder money value may be projected. To sum verification into"
        " a total someone must first create a column to sum it into -- which is what this"
        " check exists to stop."
    )


def test_overlay_has_exactly_one_money_column() -> None:
    """CHECK 42b (Guard 2) -- one declared *_micros column, and it is the right one."""
    manifest = _load_manifest()
    columns = _overlay_node(manifest).get("columns", {})
    assert columns, (
        f"{_OVERLAY} declares no columns in the manifest -- schema_fee_tax_verification.yml"
        " is missing or was not parsed, so the money-column ban cannot be checked"
    )
    micros_columns = sorted(name for name in columns if name.endswith("_micros"))
    assert micros_columns == ["verification_cost_micros"], (
        f"{_OVERLAY} declares {micros_columns} as its *_micros columns; exactly ONE is"
        " allowed and it must be verification_cost_micros. A second money column is the"
        " first step towards a merged total."
    )


# ---------------------------------------------------------------------------
# PORTABILITY + R3.
# ---------------------------------------------------------------------------


def test_no_banned_dialect_construct_in_overlay_or_matcher() -> None:
    """CHECK 44 -- §B.5, applied to the overlay AND to the extracted matcher macro.

    The matcher is checked here because 41.3's conformance test scans only its own three
    models: an extraction that moved SQL into a macro would otherwise move it out of the
    ban list's reach. The matcher needs NO target.type branch at all -- it emits only
    CASE / MAX / GROUP BY / UNION ALL / LEFT JOIN, identical in both dialects.
    """
    offenders: list[str] = []
    for path in (_OVERLAY_SQL, _MATCHER_MACRO):
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for pattern, why in _BANNED_PATTERNS:
            if re.search(pattern, code):
                offenders.append(f"{path.name}: /{pattern}/ -- {why}")
    assert not offenders, f"banned dialect-specific construct: {offenders}"


def test_no_v_suffixed_source_name() -> None:
    """CHECK 44b (ruling R1) -- app.*_v views are the Postgres feed ONLY.

    The mirror entry, the dbt source name and the warehouse relation are all unsuffixed; a
    `_v` inside source() means dbt is reading a name the warehouse does not have.
    """
    pattern = re.compile(r"source\(\s*['\"]mirror['\"]\s*,\s*['\"]([a-z0-9_]+)['\"]\s*\)")
    offenders: list[str] = []
    for path in (_OVERLAY_SQL, _MATCHER_MACRO, _LADDER_SQL):
        for relation in pattern.findall(path.read_text(encoding="utf-8")):
            if relation.endswith("_v"):
                offenders.append(f"{path.name}: source('mirror', '{relation}')")
    assert not offenders, f"a _v-suffixed mirror relation reached dbt (R1): {offenders}"


def test_zero_resolution_logic_in_the_overlay() -> None:
    """CHECK 46 (ruling R3) -- 41.2 owns attribute resolution; the overlay owns none."""
    code = _strip_comments(_OVERLAY_SQL.read_text(encoding="utf-8"))
    offenders = [name for name in _R3_FORBIDDEN if name in code]
    assert not offenders, (
        f"{_OVERLAY} must contain ZERO country/market/source_type resolution logic (R3);"
        f" it reads {offenders}"
    )
    assert code.count("fee_tax_country_resolution") >= 1, (
        f"{_OVERLAY} does not ref() fee_tax_country_resolution -- the single LEFT JOIN on"
        " Story 41.2's frozen 14-column contract IS the attribute entry point, and without"
        " it a condition on country/market/source_type could never be evaluated at all"
    )


# ---------------------------------------------------------------------------
# AC10 -- the matcher has exactly ONE definition site.
# ---------------------------------------------------------------------------


def test_matcher_has_exactly_one_definition_site() -> None:
    """CHECK 45 (AC10 / ruling Q3) -- no second copy of the condition matcher.

    ~70 lines of MONEY condition-matching in three copies (the cost ladder, this overlay,
    41.5's revenue model) would mean one surface computing a different total than another
    from the same rules -- the exact failure Epic 41 exists to prevent, arriving by
    maintenance rather than by bug. So the definition site is asserted, not requested.

    dbt/target/ is excluded: it holds COMPILED artifacts, where the macro's text is
    legitimately inlined into every consuming model. That is the macro working, not a copy.
    """
    sites: list[str] = []
    for path in sorted(_DBT.rglob("*.sql")):
        if "target" in path.relative_to(_DBT).parts:
            continue
        if _MATCHER_SIGNATURE in path.read_text(encoding="utf-8"):
            # as_posix() so the assertion message and the comparison are identical on
            # Windows and POSIX checkouts.
            sites.append(path.relative_to(_REPO_ROOT).as_posix())

    assert sites == ["dbt/macros/fee_tax_condition_matcher.sql"], (
        "the condition matcher must have EXACTLY ONE definition site,"
        " dbt/macros/fee_tax_condition_matcher.sql. Found it in:"
        f" {sites}. If Story 41.5 needs a new condition key, that is a change to the"
        " single definition site -- and it must re-run 41.3's byte-identity gate -- not a"
        " third copy."
    )


def test_both_consumers_call_the_matcher_exactly_once() -> None:
    """CHECK 45b (AC10) -- the ladder and the overlay CONSUME the one definition."""
    for path in (_LADDER_SQL, _OVERLAY_SQL):
        calls = path.read_text(encoding="utf-8").count("fee_tax_condition_matcher(")
        assert calls == 1, (
            f"{path.name} calls fee_tax_condition_matcher() {calls} times; exactly one"
            " call is expected. Zero means the model reintroduced its own matching logic;"
            " more than one means the CTE names collide."
        )


def test_ladder_still_passes_the_conditions_relation_from_the_caller() -> None:
    """CHECK 45c -- each consumer keeps its OWN depends_on edge.

    ``conditions_relation`` is a macro PARAMETER rather than a ``source()`` call inside the
    macro, precisely so every consuming model still carries a greppable
    ``source('mirror', 'fee_tax_rule_conditions')`` and the per-model dependency-set
    assertions above remain meaningful. If the call ever moved inside the macro, both
    models would silently lose that edge.
    """
    for path in (_LADDER_SQL, _OVERLAY_SQL):
        text = path.read_text(encoding="utf-8")
        assert "fee_tax_rule_conditions" in text, (
            f"{path.name} no longer names mirror.fee_tax_rule_conditions -- the matcher's"
            " conditions relation must be passed BY THE CALLER so the model keeps its own"
            " explicit dependency edge"
        )
    macro_text = _MATCHER_MACRO.read_text(encoding="utf-8")
    macro_code = _strip_comments(macro_text)
    assert "source(" not in macro_code, (
        "fee_tax_condition_matcher calls source() itself. It must accept the conditions"
        " relation as a parameter instead, or its consumers lose their explicit"
        " depends_on edges and the per-model dependency assertions become vacuous."
    )


# ---------------------------------------------------------------------------
# AC11 -- file discipline.
# ---------------------------------------------------------------------------


def test_shared_config_files_do_not_mention_the_overlay() -> None:
    """CHECK 43 -- the 41.4 YAML lives in NEW files.

    Keeping the entries out of the shared files is what makes the "only NEW files plus one
    rewire" claim checkable with `git diff --name-only`.
    """
    for relative in _SHARED_CONFIG_FILES:
        path = _DBT / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert "verification_allocation" not in text, (
            f"dbt/{relative} mentions verification_allocation. Story 41.4's model entries"
            " belong in dbt/models/marts/schema_fee_tax_verification.yml and its seed"
            " entries in dbt/seeds/schema_epic41_verification.yml."
        )


def test_41_3_seeder_carries_no_41_4_fixture() -> None:
    """CHECK 47a (ruling Q1) -- the sibling seeder really is a sibling.

    This is the git-free half, and it is the half that matters: Task 1's whole gate is that
    41.3's singular tests stay BYTE-IDENTICAL after the matcher extraction, and that
    statement is only meaningful if 41.3's fixtures provably did not move underneath it.
    """
    seeder = (_DBT / "seeds" / "feetax" / "seed_fee_tax_mirror.py").read_text(encoding="utf-8")
    offenders = [token for token in _STORY_41_4_FIXTURE_TOKENS if token in seeder]
    assert not offenders, (
        "Story 41.3's seed_fee_tax_mirror.py carries Story 41.4 fixture tokens"
        f" {offenders}. 41.4 ships a SIBLING seeder"
        " (dbt/seeds/feetax/seed_fee_tax_verification.py) and imports _FEE_TAX_DDL from"
        " its sibling -- it never edits it. If 41.3's fixtures moved, the byte-identity"
        " gate on the matcher extraction proved nothing."
    )


#: Files where a later story may APPEND but may never alter what is already there.
#:
#: Added by Story 48.4. The gate below was written to prove that Story 41.4's diff was
#: narrow, and it proved it by comparing against a MOVING HEAD -- which quietly turned a
#: one-story scope check into a permanent prohibition on ever improving these files
#: again. 48.4 is required to "replace remaining binary-float divisions and multiple
#: rounding boundaries with one cross-adapter exact algorithm", and the exact-money
#: macros are where that lives: `fee_tax_pct_of_micros`' own docstring records the
#: defect (finding F11) as still open.
#:
#: The protection that actually mattered is kept and made precise: an existing
#: exact-money macro may not be silently altered, because its widths are load-bearing
#: and a changed one moves invoice totals. A NEW macro appended after them changes no
#: existing call site. So for these files the assertion is prefix-identity, not
#: identity -- strictly stronger than deleting the entry, which is the other way this
#: could have been made to pass.
_MAY_ONLY_BE_APPENDED_TO = frozenset({"dbt/macros/fee_tax_money_math.sql"})


def test_forbidden_files_are_unchanged_from_head() -> None:
    """CHECK 47b (AC11) -- the git half, and it SKIPS rather than lying when git is absent.

    Scope is deliberately narrow (see _MUST_BE_UNCHANGED): files 41.4 may not touch AND
    that no concurrently-running story was reported to be in. Asserting a wider set would
    fail for another agent's reason and train people to ignore this test, which is the same
    disease as a flag that is always on.
    """
    probe = _git_show_head("dbt/dbt_project.yml")
    if probe is None:
        pytest.skip(
            "git is unavailable (or HEAD cannot be read) in this harness, so the"
            " working-tree-vs-HEAD comparison cannot run. This is recorded as UNVERIFIED"
            " rather than passed; run `git diff --name-only` manually and expect only NEW"
            " files plus dbt/models/marts/fee_tax_ladder_daily.sql."
        )

    offenders: list[str] = []
    for relative in _MUST_BE_UNCHANGED:
        committed = _git_show_head(relative)
        if committed is None:
            offenders.append(f"{relative} (not readable at HEAD)")
            continue
        current = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        # Normalise line endings: this repo is checked out on Windows and a CRLF/LF
        # difference is a checkout artifact, not an edit.
        current_lf = current.replace("\r\n", "\n")
        committed_lf = committed.replace("\r\n", "\n")
        if relative in _MAY_ONLY_BE_APPENDED_TO:
            if not current_lf.startswith(committed_lf):
                offenders.append(f"{relative} (existing content altered, not appended to)")
            continue
        if current_lf != committed_lf:
            offenders.append(relative)

    assert not offenders, (
        "file(s) that must not be altered were altered:"
        f" {offenders}. Inside Epic 41 the ONLY file 41.4 may edit is"
        " dbt/models/marts/fee_tax_ladder_daily.sql, and that only for the Q3 matcher"
        " extraction. seed_fee_tax_mirror.py in particular must be byte-identical to HEAD."
        " The exact-money macros may be APPENDED to by a later story (see"
        " _MAY_ONLY_BE_APPENDED_TO) but never rewritten: their widths are load-bearing"
        " and a changed one moves invoice totals."
    )


def test_ladder_rewire_is_deletion_plus_one_call() -> None:
    """CHECK 47c -- the one authorised edit really is only the extraction.

    E41-NFR01 protects the INCUMBENT warehouse; it is not a vow of immutability over Epic
    41's own models, which is why editing the ladder is in scope here. But the edit must be
    the extraction and nothing else, so the properties that would reveal a stowaway change
    are asserted: the four CTE bodies are gone, the macro call is present, and the
    downstream references that make the rewire behaviour-preserving are untouched.
    """
    ladder = _LADDER_SQL.read_text(encoding="utf-8")
    code = _strip_comments(ladder)

    assert "row_attributes_long AS (" not in code, (
        "fee_tax_ladder_daily still defines row_attributes_long inline -- the extraction is"
        " incomplete and there are now two definition sites"
    )
    for cte in ("cond_eval AS (", "rule_state AS (", "cond_gap AS ("):
        assert cte not in code, (
            f"fee_tax_ladder_daily still defines `{cte}` inline -- the extraction is"
            " incomplete"
        )

    assert "fee_tax_condition_matcher(" in code, (
        "fee_tax_ladder_daily does not call fee_tax_condition_matcher -- the four CTEs were"
        " deleted without being replaced"
    )

    # The downstream references are what make "byte-identical" realistic rather than
    # hopeful: they must still name the macro's emitted CTEs.
    for reference in ("LEFT JOIN rule_state", "JOIN cond_gap"):
        assert reference in code, (
            f"fee_tax_ladder_daily no longer contains `{reference}` -- the rewire was"
            " supposed to be a pure deletion plus one call, leaving every downstream"
            " reference to the macro's emitted CTE names untouched"
        )
