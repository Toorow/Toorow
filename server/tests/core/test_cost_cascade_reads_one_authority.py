"""The cost cascade screen must name rules from the store its NUMBERS came from.

WHAT WAS MEASURED, 2026-08-17, while closing the capability half of
`governance.md`'s `Incomplete if` clause 3 -- *"reconciliation, DQ or capability
rules create a parallel semantic or evidence store"*.

Story 48.4 moved the authority: migration 146 redefines `app.fee_tax_rules_dim_v`
over the PUBLISHED `tax_fee` Rule Set version instead of the mutable
`app.fee_tax_rules` rows, so the marts -- and therefore
`fee_tax_ladder_daily.applied_rule_ids` -- speak the governed vocabulary. The view
mints `id = rule_set_version_id || ':' || rule_key`.

`datastream_workbench_cost._read_project_rules` was not moved with it. It still
SELECTs `app.fee_tax_rules`, whose ids are minted `ftr_<ULID>`
(`fee_tax_rules.py::_mint_rule_id`). The two namespaces cannot collide, so
`rules_by_id.get(applied_rule_id)` misses every single time and every rule lands
in `unresolved_rule_count`.

The screen then tells the person the rules were *"applied by rules this Project's
rule store no longer carries"* -- a sentence written for a rare edge case, served
as the permanent state since the cutover. It is the same shape as the read
AI-295 removed from `_resolve_members`: a lookup that works only because it
always fails. Here it is worse, because the failure is rendered as a fact about
the Project's data rather than about the cutover.

A second consequence, same cause: `empty_code` picks
`EMPTY_NO_RULE_PUBLISHED if not rules` from the LEGACY list, so a Project whose
governed ladder does produce mart rows can be told no rule is published.

These tests are offline and read the SQL text, because what is wrong is which
relation is named -- not what a row contains.
"""

from __future__ import annotations

from core import datastream_workbench_cost as cost

#: What migration 146 makes the marts speak: `<rule_set_version_id>:<rule_key>`.
_GOVERNED_APPLIED_ID = "grsv_01EXAMPLE:agency_fee_fr"
#: What `app.fee_tax_rules` mints: `fee_tax_rules.py::_mint_rule_id`.
_LEGACY_RULE_ID = "ftr_01EXAMPLE"


def test_the_rule_list_reads_the_governed_view_not_the_mutable_table():
    """One authority, named once: the relation the mart's ids come from."""
    sql = cost._PROJECT_RULES_SQL
    assert "app.fee_tax_rules_dim_v" in sql, (
        "the cascade names its rules from `app.fee_tax_rules` while its numbers "
        "come from `fee_tax_ladder_daily`, whose `applied_rule_ids` are minted by "
        "`app.fee_tax_rules_dim_v` over the published Rule Set version "
        "(migration 146). Two authorities, two id namespaces, one screen."
    )


def test_a_governed_applied_id_resolves_to_a_level():
    """The join that must work: a mart id meets a rule row and names its level."""
    breakdown = cost._levels_of_phase(
        "fee",
        [_GOVERNED_APPLIED_ID],
        {
            _GOVERNED_APPLIED_ID: {
                "id": _GOVERNED_APPLIED_ID,
                "scope_kind": "project",
                "scope_ref": None,
                "category": "fee",
                "status": "confirmed",
            }
        },
    )
    assert breakdown["unresolved_rule_count"] == 0
    assert [level["kind"] for level in breakdown["levels"]] == ["project"]
    assert breakdown["levels"][0]["rule_count"] == 1


def test_a_legacy_keyed_list_resolves_nothing_and_that_is_the_defect():
    """The state before the repair, held so a regression to it is loud.

    Keyed on `ftr_` ids, the lookup cannot match a `grsv_...:` applied id --
    every rule falls to `unresolved` and the screen blames the Project's store.
    """
    breakdown = cost._levels_of_phase(
        "fee",
        [_GOVERNED_APPLIED_ID],
        {
            _LEGACY_RULE_ID: {
                "id": _LEGACY_RULE_ID,
                "scope_kind": "project",
                "scope_ref": None,
                "category": "fee",
                "status": "confirmed",
            }
        },
    )
    assert breakdown["levels"] == []
    assert breakdown["unresolved_rule_count"] == 1
    assert breakdown["unresolved_reason"] == cost.LEVEL_UNRESOLVED_REASON
