"""The LEVEL a plan match was obtained by -- story 61.3, migration 246.

WHAT THIS FILE HOLDS, and why each half of it exists:

  * the vocabulary is `dimension_conformance`'s, REUSED and not respelled. It is
    the fourth reuse of an existing set of words in this batch, and the guard
    below is what stops a fifth from appearing beside it;
  * two tiers `epic-61:41` names DO NOT EXIST, and this file says so with the
    measurement rather than leaving them quietly unimplemented. There is no
    regex-rule tier -- the neighbour of `exact` is a FIXED normalisation pipeline
    nobody configures -- and there is no AI tier: `similarity` is `difflib` at
    0.88. The word `AI` is asserted absent from the labels and from the payload;
  * a score is printed for `similarity` ALONE, because `exact` and `normalized`
    are 1.0 by construction and a tautology printed as a measurement is worse
    than no number;
  * an absent level is an ABSENCE and never `manual`. Every one of the 49 matches
    both databases carried was written before migration 246;
  * and the level SURVIVES a confirmation. `set_line_mappings` replaces a line's
    whole set (delete + insert), so a level that did not travel on the entry would
    die at the exact moment a person validates -- which is what the code did until
    this story.

The live half is opt-in on `TEST_POSTGRES_DSN`, on the pattern
`test_plan_mapping_suggest.py` established. It runs inside ONE transaction that is
rolled back, so it needs no ownership of the tables and leaves no row behind --
and it never points at anything but the disposable cluster.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.dimension_conformance import (  # noqa: E402
    METHOD_EXACT,
    METHOD_MANUAL,
    METHOD_NORMALIZED,
    METHOD_SIMILARITY,
)
from core.mediaplan_store import MediaPlanValidationError  # noqa: E402
from core.plan_matching_states import (  # noqa: E402
    MATCH_METHOD_LABELS,
    MATCH_METHOD_UNRECORDED_LABEL,
    MATCH_METHODS,
    MATCHING_STATE_AMBIGUOUS,
    MATCHING_STATE_MATCHED,
    MATCHING_STATE_UNMATCHED,
    SIMILARITY_THRESHOLD,
    campaign_matching_state,
    display_match_score,
    line_matching_state,
    match_method_label,
)

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "246_a_plan_match_says_how_it_was_obtained.sql"
)


# ===========================================================================
# The vocabulary, and the two tiers of the plan that do not exist
# ===========================================================================


def test_the_four_words_are_the_ones_dimension_conformance_already_writes() -> None:
    """Acceptance 2: reused, not recopied. A fifth scale is what this refuses.

    Four scales already existed when this axis was named -- the report confidence
    of `confidence.py`, the column confidence of `file_source_recognizer.py`, the
    worded evidence of `tax_evidence.py` and this one -- and only this one has the
    shape a plan match needs. So it is imported.
    """
    assert MATCH_METHODS == (
        METHOD_EXACT,
        METHOD_NORMALIZED,
        METHOD_SIMILARITY,
        METHOD_MANUAL,
    )
    assert set(MATCH_METHOD_LABELS) == set(MATCH_METHODS)


def test_the_database_refuses_the_same_four_words_and_no_others() -> None:
    """The CHECK of migration 246 mirrors migration 052's, word for word.

    Two spellings of one closed vocabulary is how a fifth word gets in: the
    database would accept what the module rejects, or the reverse.
    """
    sql = _MIGRATION.read_text(encoding="utf-8")
    assert "'exact', 'normalized', 'similarity', 'manual'" in sql
    assert "match_score IS NULL OR (match_score >= 0 AND match_score <= 1)" in sql
    # And the one invariant 052 does not carry, because it is about this axis:
    # a match somebody typed has nothing to measure.
    assert "match_method IS DISTINCT FROM 'manual' OR match_score IS NULL" in sql


def test_the_fuzzy_tier_is_difflib_and_is_never_called_ai() -> None:
    """The most expensive kind of sentence this repository can write.

    `epic-61:41` calls this tier a fuzzy AI proposal. It is
    `difflib.SequenceMatcher` over two normalised strings at 0.88, with no model
    and no network call, and naming it an intelligence is a promise nothing behind
    it keeps.
    """
    assert SIMILARITY_THRESHOLD == 0.88
    for label in MATCH_METHOD_LABELS.values():
        assert "AI" not in label
        assert "intelligence" not in label.lower()
    assert MATCH_METHOD_LABELS[METHOD_SIMILARITY] == "Name similarity"

    import core.plan_mapping_suggest as engine

    source = Path(engine.__file__).read_text(encoding="utf-8")
    # The engine imports difflib's ratio through `dimension_conformance.similarity`
    # and calls nothing else -- no client, no endpoint, no key.
    assert "similarity" in source
    for token in ("openai", "anthropic", "http://", "https://api"):
        assert token not in source.lower()


def test_the_regex_rule_tier_of_the_plan_is_carried_by_nothing() -> None:
    """The second tier `epic-61:41` names does not exist, and this is the measure.

    Two ratified documents place "the ordered matching rules" in Governance. No
    table, no module and no route carries one, so the tier delivered here is
    `normalized` -- a FIXED pipeline -- and this story does not open that store.
    """
    root = Path(__file__).resolve().parents[3]
    docs = sorted(
        path.relative_to(root).as_posix()
        for path in (root / "docs").rglob("*.md")
        if "ordered matching rules" in path.read_text(encoding="utf-8", errors="replace")
    )
    assert docs, docs

    # A MENTION IS NOT A STORE, and this measures the store. Several modules cite
    # the ratified sentence -- including the one that had to say the tier does not
    # exist -- so what is counted is whether any migration creates a table for it.
    tables = sorted(
        path.name
        for path in (root / "infra" / "nango" / "migrations").glob("*.sql")
        if "matching_rule" in path.read_text(encoding="utf-8", errors="replace").lower()
    )
    assert tables == [], tables

    # And the pipeline it is NOT: `normalize_value` takes no rule and reads none.
    from core.dimension_conformance import NormalizeOptions, normalize_value

    assert normalize_value("[FR] Summer-Sale 2026", NormalizeOptions()) == normalize_value(
        "summer sale", NormalizeOptions()
    )


# ===========================================================================
# What a level is CALLED, and when a number is allowed beside it
# ===========================================================================


def test_an_unrecorded_level_is_a_named_absence_and_never_manual() -> None:
    """Acceptance: `NULL` renders as an absence, never as a person's act."""
    assert match_method_label(None) == MATCH_METHOD_UNRECORDED_LABEL
    assert match_method_label("") == MATCH_METHOD_UNRECORDED_LABEL
    assert match_method_label(None) != MATCH_METHOD_LABELS[METHOD_MANUAL]
    # A word nobody named renders as nothing rather than leaking a schema.
    assert match_method_label("psychic") is None


def test_a_score_is_rendered_for_similarity_alone() -> None:
    """Acceptance 3: `exact`, `normalized` and `manual` carry no number at all."""
    assert display_match_score(METHOD_SIMILARITY, 0.9123) == 0.9123
    assert display_match_score(METHOD_EXACT, 1.0) is None
    assert display_match_score(METHOD_NORMALIZED, 1.0) is None
    assert display_match_score(METHOD_MANUAL, None) is None
    # Nor for an unrecorded level: a number with no level beside it says nothing.
    assert display_match_score(None, 0.9) is None
    # And a `similarity` with no stored score is an absence, NEVER a zero.
    assert display_match_score(METHOD_SIMILARITY, None) is None


# ===========================================================================
# The fourth state, both ways round
# ===========================================================================


def test_ambiguous_needs_two_candidates_and_no_arbitration() -> None:
    active = [{"status": "active"}]
    assert line_matching_state(active, candidate_count=5) == MATCHING_STATE_MATCHED
    assert line_matching_state([], candidate_count=2) == MATCHING_STATE_AMBIGUOUS
    assert line_matching_state([], candidate_count=1) == MATCHING_STATE_UNMATCHED
    assert line_matching_state([], candidate_count=0) == MATCHING_STATE_UNMATCHED
    # NO candidate set computed is not "there are none": the tab reads this way,
    # and a line it drew as `unmatched` must not be read as arbitrated.
    assert line_matching_state([]) == MATCHING_STATE_UNMATCHED
    assert line_matching_state([{"status": "orphaned"}]) == MATCHING_STATE_UNMATCHED


def test_a_campaign_two_lines_claim_is_ambiguous_too() -> None:
    """A9, the amendment: the engine is N:M in both directions."""
    assert campaign_matching_state(2) == MATCHING_STATE_AMBIGUOUS
    assert campaign_matching_state(7) == MATCHING_STATE_AMBIGUOUS
    assert campaign_matching_state(1) is None
    assert campaign_matching_state(0) is None


# ===========================================================================
# Live Postgres (opt-in) -- the column, its CHECK, and the survival
# ===========================================================================


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_LINES = [
    {
        "line_key": "social/summer",
        "label": "Summer Sale",
        "channel": "Social",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "budget": "10000.00",
    },
    {
        "line_key": "social/winter",
        "label": "Winter Sale",
        "channel": "Social",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "budget": "5000.00",
    },
]


@pytest.fixture
def a_plan():
    """One project, one published plan, inside ONE transaction that is rolled back.

    No `DISABLE TRIGGER`, no `DELETE` cleanup and therefore no ownership of the
    tables: the fixture leaves nothing behind because nothing is ever committed.
    """
    import psycopg
    from core.mediaplan_store import (
        create_plan,
        create_version_with_lines,
        publish_version,
    )

    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    suffix = uuid.uuid4().hex[:8]
    org_id, project_id = f"org_61_3_{suffix}", f"proj_61_3_{suffix}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by, status)"
                " VALUES (%s, %s, %s, 'system', 'active')",
                (org_id, org_id, org_id),
            )
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, status, created_by, org_id)"
                " VALUES (%s, %s, %s, 'active', 'system', %s)",
                (project_id, "Resolution level", project_id, org_id),
            )
        plan = create_plan(
            conn, project_id=project_id, name=f"Plan {uuid.uuid4().hex[:6]}", created_by="tester"
        )
        version = create_version_with_lines(
            conn, plan_id=plan["id"], lines=_LINES, created_by="tester"
        )
        publish_version(conn, version_id=version["id"], published_by="tester")
        yield {"conn": conn, "project_id": project_id, "plan_id": plan["id"]}
    finally:
        conn.rollback()
        conn.close()


def _level(conn, plan_id, line_key, campaign_ref):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT match_method, match_score FROM app.plan_line_mappings"
            " WHERE plan_id = %s AND line_key = %s AND campaign_ref = %s",
            (plan_id, line_key, campaign_ref),
        )
        return cur.fetchone()


@pg_available
def test_the_level_is_written_and_read_back(a_plan) -> None:
    from core.mediaplan_mapping import list_mappings, set_line_mappings

    conn, plan_id = a_plan["conn"], a_plan["plan_id"]
    set_line_mappings(
        conn,
        plan_id=plan_id,
        line_key="social/summer",
        entries=[
            {
                "connector": "meta-ads",
                "campaign_ref": "camp_EXAMPLE_1",
                "match_method": METHOD_SIMILARITY,
                "match_score": 0.91,
            }
        ],
        actor="owner@example.com",
    )

    assert _level(conn, plan_id, "social/summer", "camp_EXAMPLE_1") == (METHOD_SIMILARITY, 0.91)
    stored = list_mappings(conn, plan_id=plan_id)["lines"][0]["mappings"][0]
    assert stored["match_method"] == METHOD_SIMILARITY
    assert stored["match_score"] == 0.91


@pg_available
def test_a_match_nobody_qualified_is_manual_and_carries_no_number(a_plan) -> None:
    """An entry that does not mention a level came from somebody who typed it."""
    from core.mediaplan_mapping import set_line_mappings

    conn, plan_id = a_plan["conn"], a_plan["plan_id"]
    set_line_mappings(
        conn,
        plan_id=plan_id,
        line_key="social/summer",
        entries=[{"connector": "meta-ads", "campaign_ref": "camp_EXAMPLE_1"}],
        actor="owner@example.com",
    )

    assert _level(conn, plan_id, "social/summer", "camp_EXAMPLE_1") == (METHOD_MANUAL, None)


@pg_available
def test_an_explicitly_unknown_level_stays_unknown(a_plan) -> None:
    """The distinction that lets a match older than migration 246 survive a rewrite.

    `match_method` absent means "a person typed it"; `match_method: None` means
    "nobody recorded how this was made". Collapsing the two would claim an author
    for the 49 rows nobody can name.
    """
    from core.mediaplan_mapping import set_line_mappings

    conn, plan_id = a_plan["conn"], a_plan["plan_id"]
    set_line_mappings(
        conn,
        plan_id=plan_id,
        line_key="social/summer",
        entries=[
            {
                "connector": "meta-ads",
                "campaign_ref": "camp_EXAMPLE_1",
                "match_method": None,
                "match_score": None,
            }
        ],
        actor="owner@example.com",
    )

    assert _level(conn, plan_id, "social/summer", "camp_EXAMPLE_1") == (None, None)
    assert match_method_label(None) == MATCH_METHOD_UNRECORDED_LABEL


@pg_available
@pytest.mark.parametrize(
    "entry",
    [
        {"match_method": "psychic"},
        {"match_method": METHOD_SIMILARITY, "match_score": 1.5},
        {"match_method": METHOD_SIMILARITY, "match_score": -0.1},
        {"match_method": METHOD_SIMILARITY},
        {"match_method": METHOD_MANUAL, "match_score": 1.0},
        {"match_method": None, "match_score": 0.9},
    ],
)
def test_a_level_the_vocabulary_refuses_never_reaches_a_row(a_plan, entry) -> None:
    """Six ways a fabricated level would get in, and all six are 422 at the door.

    Typed here rather than left to the CHECK: an IntegrityError reaches the API as
    an unnamed 500, and a refusal nobody can name is a refusal nobody repairs.
    """
    from core.mediaplan_mapping import set_line_mappings

    conn, plan_id = a_plan["conn"], a_plan["plan_id"]
    with pytest.raises(MediaPlanValidationError):
        set_line_mappings(
            conn,
            plan_id=plan_id,
            line_key="social/summer",
            entries=[
                {"connector": "meta-ads", "campaign_ref": "camp_EXAMPLE_1", **entry}
            ],
            actor="owner@example.com",
        )

    assert _level(conn, plan_id, "social/summer", "camp_EXAMPLE_1") is None


@pg_available
def test_the_database_refuses_a_manual_match_carrying_a_score(a_plan) -> None:
    """The store refuses it; so does the column. A guard on one side is half a guard."""
    import psycopg

    conn, plan_id = a_plan["conn"], a_plan["plan_id"]
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_bad_row")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO app.plan_line_mappings"
                " (plan_id, line_key, connector, campaign_ref, split_weight, status,"
                "  match_method, match_score, created_by)"
                " VALUES (%s, 'social/summer', 'meta-ads', 'camp_EXAMPLE_9', 1.0, 'active',"
                "         'manual', 1.0, 'tester')",
                (plan_id,),
            )
        cur.execute("ROLLBACK TO SAVEPOINT before_bad_row")


@pg_available
def test_the_level_survives_a_later_write_on_the_same_line(a_plan) -> None:
    """ACCEPTANCE 5, and the fault this story exists to repair.

    `set_line_mappings` REPLACES a line's whole set. Until story 61.3 the entries
    carried `{connector, campaign_ref}` alone, so a `similarity` confirmed on
    Monday came back `NULL` the moment anybody added a second campaign to the same
    line on Tuesday. The level survives because it travels on the entry -- and the
    neighbour added by hand is `manual`, which is a different fact and stays one.
    """
    from core.mediaplan_mapping import list_mappings, set_line_mappings

    conn, plan_id = a_plan["conn"], a_plan["plan_id"]
    set_line_mappings(
        conn,
        plan_id=plan_id,
        line_key="social/summer",
        entries=[
            {
                "connector": "meta-ads",
                "campaign_ref": "camp_EXAMPLE_1",
                "match_method": METHOD_SIMILARITY,
                "match_score": 0.89,
            }
        ],
        actor="owner@example.com",
    )

    # A LATER write on the SAME line, adding a campaign typed by hand.
    kept = [
        {
            "connector": entry["connector"],
            "campaign_ref": entry["campaign_ref"],
            "match_method": entry["match_method"],
            "match_score": entry["match_score"],
        }
        for entry in list_mappings(conn, plan_id=plan_id)["lines"][0]["mappings"]
    ]
    set_line_mappings(
        conn,
        plan_id=plan_id,
        line_key="social/summer",
        entries=[*kept, {"connector": "meta-ads", "campaign_ref": "camp_EXAMPLE_2"}],
        actor="owner@example.com",
    )

    assert _level(conn, plan_id, "social/summer", "camp_EXAMPLE_1") == (METHOD_SIMILARITY, 0.89)
    assert _level(conn, plan_id, "social/summer", "camp_EXAMPLE_2") == (METHOD_MANUAL, None)


@pg_available
def test_a_confirmation_re_derives_the_level_and_replaces_what_was_there(a_plan) -> None:
    """The full path, against real rows: propose, confirm, read the level back.

    The engine is asked again inside the confirmation, so the stored level is the
    one it computed and not one the caller stated. `replaced` is the count taken
    BEFORE the write, which is what the confirmation names to a person.
    """
    from core.datastream_workbench_placements import (
        PlacementMatchNotProposed,
        confirm_placement_match,
    )
    from core.plan_mapping_suggest import suggest_line_mappings_for_plan

    conn, plan_id = a_plan["conn"], a_plan["plan_id"]

    def _suggest(target_plan_id, connector):
        import contextlib

        import core.db as db

        @contextlib.contextmanager
        def _open():
            yield conn

        original = db.get_connection
        db.get_connection = _open
        try:
            return suggest_line_mappings_for_plan(
                target_plan_id,
                connector=connector,
                actuals_by_connector={"meta-ads": ["Summer Sale"], "google-ads": ["Summer Sale"]},
            )
        finally:
            db.get_connection = original

    result = confirm_placement_match(
        conn,
        connector="meta-ads",
        plan_id=plan_id,
        line_key="social/summer",
        campaign_ref="Summer Sale",
        actor="owner@example.com",
        suggest_fn=_suggest,
    )

    assert result["match_method"] == METHOD_EXACT
    # `exact` is 1.0 by construction, so nothing is offered to a screen.
    assert result["match_score"] is None
    assert result["replaced"] == 0
    assert _level(conn, plan_id, "social/summer", "Summer Sale") == (METHOD_EXACT, 1.0)

    # And a campaign of a connector this scope does not reach is not a candidate:
    # the engine was asked for `meta-ads`, so `google-ads` is refused here.
    with pytest.raises(PlacementMatchNotProposed):
        confirm_placement_match(
            conn,
            connector="google-ads",
            plan_id=plan_id,
            line_key="social/winter",
            campaign_ref="Summer Sale",
            actor="owner@example.com",
            suggest_fn=_suggest,
        )


@pg_available
def test_asking_for_suggestions_writes_not_one_row(a_plan) -> None:
    """ACCEPTANCE 4, counted rather than asserted.

    `epic-61:111-112` refuses a match written without a human act. This counts
    `app.plan_line_mappings` before and after the reading and requires equality --
    the refusal proven, not promised.
    """
    import contextlib

    import core.db as db
    from core.plan_mapping_suggest import suggest_line_mappings_for_plan

    conn, plan_id = a_plan["conn"], a_plan["plan_id"]

    def _count():
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app.plan_line_mappings")
            return int(cur.fetchone()[0])

    before = _count()

    @contextlib.contextmanager
    def _open():
        yield conn

    original = db.get_connection
    db.get_connection = _open
    try:
        out = suggest_line_mappings_for_plan(
            plan_id,
            connector="meta-ads",
            actuals_by_connector={"meta-ads": ["Summer Sale", "Winter Sale"]},
        )
    finally:
        db.get_connection = original

    assert len(out["suggestions"]) == 2
    assert _count() == before
