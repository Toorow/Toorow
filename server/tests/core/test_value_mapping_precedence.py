"""Story 60.5 -- AI-238 stays OPEN, and this file is why that is a decision.

The story's Arbitrage 5 chose HISTORY ONLY: 60.5 gives the two rule families a
version ledger and does NOT wire either of them into a render path. Applying them
is an engine, it inherits the two-dialect obligation `test_cleanup_rule_dialects`
holds 60.3 to, and it does not fit in a story whose acceptance is about versions.

So no precedence RESOLVER is shipped. Code nothing calls is what this batch has
already been rejected for. What is shipped instead is the MEASUREMENT the next
owner would otherwise have to re-derive, held here so it cannot rot:

  1. the precedence is WRITTEN, in one named constant, and it says which store
     wins where;
  2. the two key spaces really are disjoint -- migration 052 keys on a connector
     and names no field, migration 235 keys on a Datastream AND a field -- so the
     precedence is a precedence and not a merge;
  3. AI-260: three of the four families this batch delivered are read by NOTHING
     on a render path. That is the fact the story writes down, and a grep is the
     only honest way to hold it.

AMENDED 2026-08-17 (AI-260): the value-table half is now APPLIED. The resolver
lives in `core/value_table_resolution.py` -- the connector -> Datastreams bridge
of `fee_tax_country_resolution.sql`, with its ambiguity rule -- and its one
render-time caller is `core/plan_actual_alignment.py`. It reads the store
THROUGH `value_mapping_tables` (the SQL stays in its owner), so the grep below
still holds and still polices that no second module names the relations.
`app.cleanup_rules` remains applied by nothing on a render path: its key
(`project_id`, nullable `datastream_id`, field) crosses the same bridge only for
its datastream-scoped half, but its application is compiled WAREHOUSE SQL under
the 60.3 two-dialect obligation -- a distinct pass, still open under AI-260.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import rule_versions as ledger  # noqa: E402

_CORE = Path(__file__).resolve().parents[2] / "core"

#: The modules allowed to name each store. Everything else naming them would mean
#: a second reader appeared -- which is exactly the day AI-238 stops being
#: theoretical, because two stores would then answer one question.
_ENTRY_OWNERS = {"value_mapping_tables.py"}
#: `rule_versions.py` and `rule_versions_api.py` name `app.cleanup_rules` to READ
#: a rule's identity and scope before recording or previewing its version. That is
#: the history path, not a render path, and it is what story 60.5 delivered.
_RULE_OWNERS = {"cleanup_rules.py", "rule_versions.py", "rule_versions_api.py"}


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


def _modules_naming(relation: str) -> set[str]:
    pattern = re.compile(rf"app\.{re.escape(relation)}\b")
    return {
        path.name
        for path in sorted(_CORE.glob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    }


def test_the_precedence_is_written_where_its_owner_will_find_it():
    """AI-238 is closed by a rule, and the rule is stated once.

    A client's value table is ASSIGNED to a named field; migration 052 matches at
    the scale of a connector and names no field at all. An explicit assignment to
    one column is a more specific statement than a connector-wide match, so the
    client's table wins on that field and 052 wins everywhere else.
    """
    text = ledger.AI_238_PRECEDENCE
    assert "field" in text
    assert "052" in text
    assert "precedence and not a merge" in text


def test_no_resolver_ships_with_this_story():
    """Arbitrage 1, amended by the context control: the rule is WRITTEN here and
    coded by whoever closes AI-238. That owner arrived (AI-260, 2026-08-17): the
    resolver is `core/value_table_resolution.py`, called by
    `plan_actual_alignment`. The LEDGER still ships none -- it keeps history,
    and this assertion keeps the two responsibilities apart."""
    exported = set(ledger.__all__)
    assert not any("resolve" in name or "conform" in name for name in exported), exported


def test_only_their_owners_name_the_two_stores():
    """AI-260, measured rather than asserted.

    `app.value_mapping_entries` and `app.cleanup_rules` are named by their own
    stores and by nothing else in `server/core` -- no report composer, no
    warehouse query, no dbt model. Since 2026-08-17 a render path DOES read the
    value tables (`plan_actual_alignment` through `value_table_resolution`), but
    it reads them through the owner's functions, so the grep still holds: what
    it polices now is that the store's SQL never leaks out of its owner while
    being served. `app.cleanup_rules` is still read by nothing on a render path.
    """
    assert _modules_naming("value_mapping_entries") == _ENTRY_OWNERS
    assert _modules_naming("cleanup_rules") == _RULE_OWNERS


@pg_available
def test_the_two_key_spaces_are_disjoint_in_the_live_schema():
    """The measurement the precedence rests on, read from the schema itself.

    If a future migration ever gave migration 052 a `source_field`, or made its
    `connector` nullable, the two stores would start answering the SAME question
    and a precedence would no longer be enough. That day this test fails, and it
    should.
    """
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'dimension_value_mappings'"
            )
            governed = dict(cur.fetchall())
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'value_mapping_assignments'"
            )
            client = {row[0] for row in cur.fetchall()}

    # 052 matches at the scale of a connector and names no field.
    assert "source_field" not in governed
    assert governed.get("connector") == "NO"
    assert "source_value" in governed
    # 235 names both the Datastream and the exact column it translates.
    assert {"datastream_id", "source_field"} <= client
