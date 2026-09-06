"""Story 75-4 -- the AI settings cascade, proved against a real PostgreSQL.

WHY NONE OF THIS CAN BE A UNIT TEST. Every property below is a property of the
database or of a read that crosses three rows: the append-only trigger, the
CHECKs of migration 346, the composite foreign key that refuses a project filed
under a foreign organization, and a resolution that reads PLATFORM, ORG and
PROJECT in ONE query. A mocked cursor accepts all of them happily -- which is
exactly how a schema promise becomes a comment.

WHAT THE AMENDMENT ASKS, AND WHAT IS PROVED HERE (context-hub.md, 2026-09-05,
"Incomplete if"):

  * project > org > platform, field by field, with the SOURCE of each field;
  * clearing the project override returns the ORGANIZATION's value;
  * clearing the organization's returns the PLATFORM's;
  * a version is APPENDED, never rewritten -- and the database refuses the
    rewrite, not a convention;
  * an invalid language, an impossible fiscal month and an unknown query scope
    are refused BY NAME, naming the field;
  * a project of another organization is refused.

Every test builds its own org and project inside its own transaction and rolls
back, so the file runs on an empty disposable database and leaves nothing.
"""

from __future__ import annotations

import pytest
from core.ai_settings import (
    ENVELOPE_BLOCK_MAX_BYTES,
    MAX_RULE_CHARS,
    MAX_RULES_BYTES,
    PLATFORM_DEFAULTS,
    AiSettingsRefused,
    clear_settings,
    envelope_block,
    envelope_block_for_project,
    history,
    platform_defaults,
    read_scope,
    resolve,
    set_settings,
)
from core.model_channel import MODEL_CHANNEL_MAX_BYTES, serialized_bytes
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

ACTOR = "owner@example.com"


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


class Tenant:
    """One organization, one project of it, and a second organization beside."""

    def __init__(self, conn):
        self.conn = conn
        self.org_id = _uid("org")
        self.project_id = _uid("proj")
        self.other_org_id = _uid("org")

    def build(self) -> Tenant:
        with self.conn.cursor() as cur:
            for org in (self.org_id, self.other_org_id):
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                    "VALUES (%s, 'Story 75-4 fixture', %s, 'active', %s)",
                    (org, org.replace("_", "-"), ACTOR),
                )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 75-4 fixture', %s, %s)",
                (self.project_id, self.org_id, self.project_id.replace("_", "-"), ACTOR),
            )
        return self


@pytest.fixture
def tenant(live_postgres):
    return Tenant(live_postgres).build()


# ---------------------------------------------------------------------------
# The cascade.
# ---------------------------------------------------------------------------


def test_nothing_set_anywhere_resolves_to_the_shipped_defaults(live_postgres, tenant) -> None:
    """A project nobody configured obeys the platform, and SAYS so per field."""
    resolved = resolve(live_postgres, org_id=tenant.org_id, project_id=tenant.project_id)

    assert resolved["values"] == PLATFORM_DEFAULTS
    assert set(resolved["sources"].values()) == {"PLATFORM"}


def test_project_beats_org_beats_platform_field_by_field(live_postgres, tenant) -> None:
    """The most specific scope that STATES a field wins THAT field -- and only it."""
    set_settings(
        live_postgres, scope="ORG", scope_id=tenant.org_id, org_id=tenant.org_id,
        payload={
            "narrative_language": "fr-FR",
            "query_scope": "any_published_view",
            "rules_always": ["Name the date range of every figure."],
        },
        actor=ACTOR,
    )
    set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={"narrative_language": "en-GB"},
        actor=ACTOR,
    )

    resolved = resolve(live_postgres, org_id=tenant.org_id, project_id=tenant.project_id)

    # The project stated ONE field, so it won ONE field.
    assert resolved["values"]["narrative_language"] == "en-GB"
    assert resolved["sources"]["narrative_language"] == "PROJECT"
    # The organization's other two survive the project's override.
    assert resolved["values"]["query_scope"] == "any_published_view"
    assert resolved["sources"]["query_scope"] == "ORG"
    assert resolved["values"]["rules_always"] == ["Name the date range of every figure."]
    assert resolved["sources"]["rules_always"] == "ORG"
    # And what neither scope stated is still the platform's.
    assert resolved["values"]["fiscal_calendar"] == PLATFORM_DEFAULTS["fiscal_calendar"]
    assert resolved["sources"]["fiscal_calendar"] == "PLATFORM"


def test_a_platform_row_overrides_the_shipped_default(live_postgres, tenant) -> None:
    """The PLATFORM scope is an override of the code defaults, not a second source."""
    set_settings(
        live_postgres, scope="PLATFORM", scope_id=None, org_id=None,
        payload={"narrative_register": "executive"}, actor=ACTOR,
    )

    resolved = resolve(live_postgres, org_id=tenant.org_id, project_id=tenant.project_id)

    assert resolved["values"]["narrative_register"] == "executive"
    assert resolved["sources"]["narrative_register"] == "PLATFORM"


def test_settings_of_another_organization_are_not_read(live_postgres, tenant) -> None:
    """A neighbour's override is not a value of this tenant's cascade."""
    set_settings(
        live_postgres, scope="ORG", scope_id=tenant.other_org_id, org_id=tenant.other_org_id,
        payload={"narrative_language": "de-DE"}, actor=ACTOR,
    )

    resolved = resolve(live_postgres, org_id=tenant.org_id, project_id=tenant.project_id)

    assert resolved["values"]["narrative_language"] == PLATFORM_DEFAULTS["narrative_language"]
    assert resolved["sources"]["narrative_language"] == "PLATFORM"


# ---------------------------------------------------------------------------
# Clearing: a version, and a return to the parent.
# ---------------------------------------------------------------------------


def test_clearing_the_project_override_returns_the_org_value(live_postgres, tenant) -> None:
    set_settings(
        live_postgres, scope="ORG", scope_id=tenant.org_id, org_id=tenant.org_id,
        payload={"narrative_language": "fr-FR"}, actor=ACTOR,
    )
    set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={"narrative_language": "en-GB"}, actor=ACTOR,
    )

    clear_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        actor=ACTOR, note="Back to the organization's language.",
    )

    resolved = resolve(live_postgres, org_id=tenant.org_id, project_id=tenant.project_id)
    assert resolved["values"]["narrative_language"] == "fr-FR"
    assert resolved["sources"]["narrative_language"] == "ORG"
    # And the scope now states nothing, which is what the panel reads.
    assert read_scope(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id
    ) is None


def test_clearing_the_org_override_returns_the_platform_value(live_postgres, tenant) -> None:
    set_settings(
        live_postgres, scope="ORG", scope_id=tenant.org_id, org_id=tenant.org_id,
        payload={"narrative_language": "fr-FR", "query_scope": "any_field"}, actor=ACTOR,
    )

    clear_settings(
        live_postgres, scope="ORG", scope_id=tenant.org_id, org_id=tenant.org_id, actor=ACTOR,
    )

    resolved = resolve(live_postgres, org_id=tenant.org_id, project_id=tenant.project_id)
    assert resolved["values"]["narrative_language"] == PLATFORM_DEFAULTS["narrative_language"]
    assert resolved["values"]["query_scope"] == PLATFORM_DEFAULTS["query_scope"]
    assert resolved["sources"]["narrative_language"] == "PLATFORM"


def test_clearing_is_a_version_and_deletes_nothing(live_postgres, tenant) -> None:
    """The history keeps BOTH acts: the override, and giving it back."""
    set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={"narrative_language": "en-GB"}, actor=ACTOR,
    )
    clear_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        actor=ACTOR,
    )

    versions = history(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id
    )
    assert [(v["version_number"], v["cleared"]) for v in versions] == [(2, True), (1, False)]
    # The first version still carries what it carried: nothing was rewritten.
    assert versions[1]["narrative_language"] == "en-GB"


def test_clearing_a_scope_that_states_nothing_is_refused_by_name(live_postgres, tenant) -> None:
    with pytest.raises(AiSettingsRefused) as refusal:
        clear_settings(
            live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
            actor=ACTOR,
        )
    assert refusal.value.code == "nothing_to_clear"
    assert "inherits" in (refusal.value.remedy or "")


# ---------------------------------------------------------------------------
# Append-only: the database says so, not a convention.
# ---------------------------------------------------------------------------


def test_a_second_set_appends_a_version_and_moves_the_head(live_postgres, tenant) -> None:
    first = set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={"narrative_language": "en-GB"}, actor=ACTOR,
    )
    second = set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={"narrative_language": "fr-FR"}, actor=ACTOR,
    )

    assert (first["version_number"], second["version_number"]) == (1, 2)
    assert first["id"] != second["id"]
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT current_version_id FROM app.ai_settings WHERE id = %s",
            (second["ai_settings_id"],),
        )
        assert cur.fetchone()[0] == second["id"]
        # The first version is still there, verbatim.
        cur.execute(
            "SELECT narrative_language FROM app.ai_setting_versions WHERE id = %s",
            (first["id"],),
        )
        assert cur.fetchone()[0] == "en-GB"


def test_a_version_cannot_be_rewritten_or_removed(live_postgres, tenant) -> None:
    """The append-only trigger of migration 346, on the real table."""
    import psycopg

    version = set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={"narrative_language": "en-GB"}, actor=ACTOR,
    )

    for statement in (
        "UPDATE app.ai_setting_versions SET narrative_language = 'de-DE' WHERE id = %s",
        "DELETE FROM app.ai_setting_versions WHERE id = %s",
    ):
        with live_postgres.cursor() as cur:
            cur.execute("SAVEPOINT immutability")
            with pytest.raises(psycopg.errors.Error) as refusal:
                cur.execute(statement, (version["id"],))
            assert refusal.value.sqlstate.startswith("23")
            cur.execute("ROLLBACK TO SAVEPOINT immutability")


# ---------------------------------------------------------------------------
# Refusals, and every one of them names its field.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "code", "field"),
    [
        ({"narrative_language": "klingon"}, "unknown_language", "narrative_language"),
        ({"narrative_language": "fr-ZZ"}, "unknown_language", "narrative_language"),
        (
            {"fiscal_calendar": {"year_start_month": 13}},
            "fiscal_month_out_of_range",
            "fiscal_calendar",
        ),
        (
            {"fiscal_calendar": {"year_start_month": 0}},
            "fiscal_month_out_of_range",
            "fiscal_calendar",
        ),
        (
            {"fiscal_calendar": {"week_start_day": "lundi"}},
            "unknown_week_start_day",
            "fiscal_calendar",
        ),
        # A CALENDAR IS STATED WHOLE. A half one used to be stored, and it
        # replaced the parent's calendar ENTIRE at resolution: a project that
        # said only "the week starts on Sunday" silently sent the fiscal year
        # back to January while its panel said "Set here".
        (
            {"fiscal_calendar": {"week_start_day": "sunday"}},
            "partial_fiscal_calendar",
            "fiscal_calendar",
        ),
        (
            {"fiscal_calendar": {"year_start_month": 4}},
            "partial_fiscal_calendar",
            "fiscal_calendar",
        ),
        ({"query_scope": "everything"}, "unknown_query_scope", "query_scope"),
        ({"narrative_register": "shouty"}, "unknown_register", "narrative_register"),
        ({"rules_always": "Name the date range."}, "rules_are_a_list", "rules_always"),
        # The block rides EVERY agent envelope and nothing can move it out of the
        # model channel, so a list past its share of that channel is refused on
        # the way IN -- not discovered on the way out, where the only remedy left
        # is to withhold somebody else's payload.
        (
            {"rules_always": ["Always name the date range and the currency it is in." * 2] * 3},
            "rules_over_envelope_budget",
            "rules_always",
        ),
        (
            {"rules_never": ["Never answer without the governed view." for _ in range(12)]},
            "rules_over_envelope_budget",
            "rules_never",
        ),
        ({"unknown_setting": 1}, "unknown_field", "unknown_setting"),
        ({}, "states_nothing", None),
    ],
)
def test_an_invalid_setting_is_refused_by_name(
    live_postgres, tenant, payload, code, field
) -> None:
    with pytest.raises(AiSettingsRefused) as refusal:
        set_settings(
            live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
            payload=payload, actor=ACTOR,
        )
    assert refusal.value.code == code
    assert refusal.value.field == field
    # Nothing was written: a refused payload leaves no half-object behind.
    assert history(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id
    ) == []


def test_a_project_of_another_organization_is_refused(live_postgres, tenant) -> None:
    with pytest.raises(AiSettingsRefused) as refusal:
        set_settings(
            live_postgres, scope="PROJECT", scope_id=tenant.project_id,
            org_id=tenant.other_org_id, payload={"narrative_language": "en-GB"}, actor=ACTOR,
        )
    assert refusal.value.code == "project_not_in_organization"

    with live_postgres.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.ai_settings WHERE org_id = %s",
                    (tenant.other_org_id,))
        assert cur.fetchone()[0] == 0


def test_the_composite_foreign_key_refuses_the_same_row_written_directly(
    live_postgres, tenant
) -> None:
    """The named refusal is the message; this constraint is the guarantee."""
    import psycopg

    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT foreign_project")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO app.ai_settings (id, scope_level, org_id, project_id, created_by) "
                "VALUES (%s, 'PROJECT', %s, %s, %s)",
                (_uid("aiset"), tenant.other_org_id, tenant.project_id, ACTOR),
            )
        cur.execute("ROLLBACK TO SAVEPOINT foreign_project")


def test_a_cleared_version_may_not_state_a_field(live_postgres, tenant) -> None:
    """`cleared` and "states nothing" are one fact, held by a CHECK."""
    import psycopg

    head = set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={"narrative_language": "en-GB"}, actor=ACTOR,
    )
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT cleared_states_nothing")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO app.ai_setting_versions "
                "(id, ai_settings_id, org_id, project_id, version_number, cleared, "
                " narrative_language, created_by) "
                "VALUES (%s, %s, %s, %s, 99, TRUE, 'de-DE', %s)",
                (_uid("aisetv"), head["ai_settings_id"], tenant.org_id,
                 tenant.project_id, ACTOR),
            )
        cur.execute("ROLLBACK TO SAVEPOINT cleared_states_nothing")


# ---------------------------------------------------------------------------
# The agent envelope.
# ---------------------------------------------------------------------------


def test_the_envelope_block_carries_the_values_and_their_sources(live_postgres, tenant) -> None:
    set_settings(
        live_postgres, scope="ORG", scope_id=tenant.org_id, org_id=tenant.org_id,
        payload={"narrative_language": "fr-FR"}, actor=ACTOR,
    )

    block = envelope_block(live_postgres, tenant.org_id, tenant.project_id)

    assert block is not None
    assert block["values"]["narrative_language"] == "fr-FR"
    assert block["sources"]["narrative_language"] == "ORG"
    assert set(block) == {"values", "sources"}


def test_the_envelope_block_stays_inside_its_share_of_the_model_channel(
    live_postgres, tenant
) -> None:
    """THE BOUND, measured on the worst payload the write path accepts.

    Story 75-4 put this block in `meta.ai_settings` of every agent-surface
    envelope, and `model_channel.partition_envelope` never routes a `meta`
    honesty field to the app channel: it is unmovable weight inside
    `MODEL_CHANNEL_MAX_BYTES` on every call. Nothing bounded it. With the
    count/length caps alone -- 40 rules of 280 characters in each of the two
    lists -- the block serializes to 23029 bytes, five times the whole channel,
    and G16 caught the consequence one door away: `get_exemplars` served
    `{"withheld": "moved_to_app_channel"}` where its exemplars should have been.

    So the write path refuses a rule list past `MAX_RULES_BYTES`, and this is
    the measurement that the refusal is enough.
    """
    # The largest list the write path accepts: every rule inside MAX_RULE_CHARS,
    # and the list itself exactly at MAX_RULES_BYTES.
    biggest = ["r" * MAX_RULE_CHARS, "s" * (MAX_RULES_BYTES - MAX_RULE_CHARS - 7)]
    assert serialized_bytes(biggest) == MAX_RULES_BYTES
    set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={
            "rules_always": biggest,
            "rules_never": biggest,
            "query_scope": "any_field",
            "fiscal_calendar": {"year_start_month": 12, "week_start_day": "wednesday"},
            "narrative_language": "pt-BR",
            "narrative_register": "executive",
        },
        actor=ACTOR,
    )

    block = envelope_block(live_postgres, tenant.org_id, tenant.project_id)
    measured = serialized_bytes(block)

    assert measured <= ENVELOPE_BLOCK_MAX_BYTES, (
        f"the worst accepted payload serializes to {measured} bytes, over the "
        f"{ENVELOPE_BLOCK_MAX_BYTES} the envelope reserves for it"
    )
    # And its share is a share: the block alone must never be able to fill the
    # model channel it rides in.
    assert ENVELOPE_BLOCK_MAX_BYTES < MODEL_CHANNEL_MAX_BYTES // 2


def test_erasing_the_organization_takes_its_ai_settings_with_it(live_postgres, tenant) -> None:
    """The claim migration 346's header makes, MEASURED rather than believed.

    `core.org_purge` names neither table in any of its statements -- every tenant
    edge here is ON DELETE CASCADE, and `plan_purge` walks NO ACTION / RESTRICT
    edges only. So the sentence that must be true is the database's: deleting the
    organization takes both tables' rows with it. The append-only trigger has to
    yield, which is why its WHEN clause carries the RGPD hatch.
    """
    set_settings(
        live_postgres, scope="ORG", scope_id=tenant.org_id, org_id=tenant.org_id,
        payload={"narrative_language": "fr-FR"}, actor=ACTOR,
    )
    set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={"narrative_language": "en-GB"}, actor=ACTOR,
    )

    from core.org_purge import plan_purge

    from tests.conftest import purge_fixture_org

    # FIRST, the claim of the header, read from the PLAN itself -- pure planning,
    # it writes nothing: `plan_purge` walks NO ACTION / RESTRICT edges only, and
    # every tenant edge of these two tables is ON DELETE CASCADE.
    planned = " ".join(op.sql for op in plan_purge(live_postgres, tenant.org_id))
    assert "ai_settings" not in planned
    assert "ai_setting_versions" not in planned

    # THEN the erasure, by the product's own path. `purge_fixture_org` owns the
    # contract (`purge_org_tree` plus the tail it leaves its caller); writing that
    # pair here would be the twenty-first copy of it (AI-291, class II).
    purge_fixture_org(live_postgres, tenant.org_id)

    with live_postgres.cursor() as cur:
        for table in ("ai_settings", "ai_setting_versions"):
            cur.execute(f"SELECT count(*) FROM app.{table} WHERE org_id = %s",
                        (tenant.org_id,))
            assert cur.fetchone()[0] == 0, f"app.{table} survived the erasure"


def test_a_partial_calendar_cannot_silently_replace_the_parents(live_postgres, tenant) -> None:
    """The organization's calendar survives a project that overrides ONE field.

    THE MEASURED DEFECT. `fiscal_calendar` is one field of the cascade, so a
    scope that stated it replaced the parent's whole calendar. Nothing stopped a
    caller from storing `{week_start_day: sunday}` alone, and the fiscal year of
    the organization -- April -- vanished into the shipped January with the panel
    still reading "Set here". Either the half calendar is merged key by key, and
    then one field carries two origins and no `sources` map can say it, or it is
    refused by name. It is refused by name, and the whole calendar still passes.
    """
    set_settings(
        live_postgres, scope="ORG", scope_id=tenant.org_id, org_id=tenant.org_id,
        payload={"fiscal_calendar": {"year_start_month": 4, "week_start_day": "monday"}},
        actor=ACTOR,
    )

    with pytest.raises(AiSettingsRefused) as refusal:
        set_settings(
            live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
            payload={"fiscal_calendar": {"week_start_day": "sunday"}}, actor=ACTOR,
        )
    assert refusal.value.code == "partial_fiscal_calendar"
    assert refusal.value.field == "fiscal_calendar"
    assert "week_start_day" not in str(refusal.value)  # it names what is MISSING
    assert "year_start_month" in str(refusal.value)
    assert refusal.value.remedy

    # THE ORGANIZATION'S APRIL IS STILL THERE, and it is still the organization's.
    resolved = resolve(live_postgres, org_id=tenant.org_id, project_id=tenant.project_id)
    assert resolved["values"]["fiscal_calendar"] == {
        "year_start_month": 4, "week_start_day": "monday",
    }
    assert resolved["sources"]["fiscal_calendar"] == "ORG"

    # And the whole calendar IS accepted at the project, with both keys.
    set_settings(
        live_postgres, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id,
        payload={"fiscal_calendar": {"year_start_month": 4, "week_start_day": "sunday"}},
        actor=ACTOR,
    )
    resolved = resolve(live_postgres, org_id=tenant.org_id, project_id=tenant.project_id)
    assert resolved["values"]["fiscal_calendar"] == {
        "year_start_month": 4, "week_start_day": "sunday",
    }
    assert resolved["sources"]["fiscal_calendar"] == "PROJECT"


def test_a_resolution_hands_back_its_own_copy_of_the_defaults(live_postgres, tenant) -> None:
    """`dict(PLATFORM_DEFAULTS)` shared the list and the calendar underneath.

    A reader that resolved a project and appended one rule to
    `values["rules_always"]` was appending to the constant every later request
    reads, for the lifetime of the process. The copy is deep, and this is what
    says so.
    """
    resolved = resolve(live_postgres, org_id=tenant.org_id, project_id=tenant.project_id)

    resolved["values"]["rules_always"].append("Never do this.")
    resolved["values"]["fiscal_calendar"]["year_start_month"] = 7

    assert PLATFORM_DEFAULTS["rules_always"] == []
    assert PLATFORM_DEFAULTS["fiscal_calendar"] == {
        "year_start_month": 1, "week_start_day": "monday",
    }
    # And the accessor the doors call hands out a copy too, never the constant.
    assert platform_defaults() is not PLATFORM_DEFAULTS
    assert platform_defaults()["fiscal_calendar"] is not PLATFORM_DEFAULTS["fiscal_calendar"]


def test_an_unknown_project_gets_no_envelope_block_at_all(live_postgres) -> None:
    """ABSENT, never the shipped defaults wearing a PLATFORM source.

    The org lookup returned no row and the block was built anyway, with
    `org_id=None`: the caller was told "the platform decided these six values for
    your project" about a project that does not exist. A block that cannot be
    read is honestly absent -- the same contract `envelope_block` already holds.
    """
    assert envelope_block_for_project(live_postgres, _uid("proj")) is None
    assert envelope_block_for_project(live_postgres, None) is None


def test_a_known_project_still_gets_its_block_through_the_project_door(
    live_postgres, tenant
) -> None:
    """The other half of the same rule: the org IS derived, so ORG is visible."""
    set_settings(
        live_postgres, scope="ORG", scope_id=tenant.org_id, org_id=tenant.org_id,
        payload={"narrative_register": "executive"}, actor=ACTOR,
    )

    block = envelope_block_for_project(live_postgres, tenant.project_id)

    assert block is not None
    assert block["values"]["narrative_register"] == "executive"
    assert block["sources"]["narrative_register"] == "ORG"
