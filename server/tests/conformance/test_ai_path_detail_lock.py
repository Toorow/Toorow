"""The recorded-detail lock exists twice. Pin the copies to each other.

`ai_path_recorder.sanitize_detail` refuses prose-shaped keys, oversized values and
nesting; the migration restates the same refusal as a CHECK so a second writer
that forgets the sanitizer is stopped by PostgreSQL rather than by a convention.

MIGRATION points at the NEWEST file that replaces the function -- 228 today, 176
before it. An applied migration is never re-opened, so the pinning has to follow
the supersession rather than the origin.

Two copies of a list drift, and a drifted copy is worse than one copy: it reads
as enforced while the newest banned name is only banned on one side. These tests
parse both and compare them, so the drift fails here instead of in production.

Story 65.7 adds a third lock below: only normalized public branch evidence may
leave the owner, and its recursive JSON Schema refuses every storage-only key.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest
from core import candidate_fate
from core.ai_path_recorder import (
    BANNED_DETAIL_KEYS,
    DETAIL_MAX_KEYS,
    DETAIL_MAX_LIST_ITEMS,
    DETAIL_VALUE_MAX_CHARS,
)

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "228_a_crossing_may_carry_its_reasons.sql"
)
ATTACHING_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "176_a_judged_branch_outlives_the_person_watching.sql"
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "core"
    / "schemas"
    / "retrieval-branch-evidence.schema.json"
)


@pytest.fixture(scope="module")
def sql() -> str:
    assert MIGRATION.is_file(), f"migration not found: {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


def _banned_array(sql: str) -> set[str]:
    """The names inside the `banned CONSTANT TEXT[] := ARRAY[...]` literal."""
    match = re.search(
        r"banned\s+CONSTANT\s+TEXT\[\]\s*:=\s*ARRAY\[(?P<body>.*?)\]",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    assert match is not None, "the migration no longer declares a `banned` array"
    return set(re.findall(r"'([^']+)'", match.group("body")))


def test_the_two_banned_lists_are_the_same_list(sql):
    assert _banned_array(sql) == set(BANNED_DETAIL_KEYS)


def test_the_key_cap_is_the_same_number(sql):
    assert re.search(rf">\s*{DETAIL_MAX_KEYS}\b", sql), (
        f"the CHECK no longer caps the map at DETAIL_MAX_KEYS={DETAIL_MAX_KEYS}"
    )


def test_the_value_length_cap_is_the_same_number(sql):
    assert re.search(rf"length\([^)]*\)\s*>\s*{DETAIL_VALUE_MAX_CHARS}\b", sql), (
        "the CHECK no longer caps a string at "
        f"DETAIL_VALUE_MAX_CHARS={DETAIL_VALUE_MAX_CHARS}"
    )


def test_the_constraint_is_actually_attached():
    """A function nobody calls locks nothing -- and the call is where it was made."""
    attached = ATTACHING_MIGRATION.read_text(encoding="utf-8")
    assert "ck_ai_path_steps_detail_recordable" in attached
    assert "app.ai_path_detail_is_recordable(detail)" in attached


def test_the_list_cap_is_its_own_number(sql):
    arrays = re.search(
        rf"jsonb_array_length\([^)]*\)\s*>\s*{DETAIL_MAX_LIST_ITEMS}\b", sql
    )
    assert arrays, (
        "the CHECK no longer caps a recorded list at "
        f"DETAIL_MAX_LIST_ITEMS={DETAIL_MAX_LIST_ITEMS}"
    )


def test_the_key_cap_and_the_list_cap_are_not_the_same_constant():
    """A regression here is invisible in behaviour until a map sits on the edge."""
    import core.ai_path_recorder as recorder

    source = Path(recorder.__file__).read_text(encoding="utf-8")
    assert "len(value) <= DETAIL_MAX_LIST_ITEMS" in source, (
        "the list check reads the KEY budget again -- the two budgets have re-merged"
    )


@pytest.fixture(scope="module")
def validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _listed() -> dict:
    return {
        "schema_version": "retrieval-branch-evidence.v1",
        "state": "branches_listed",
        "walk": {
            "producer": "context_search",
            "mode": "lexical",
            "graph_hop_depth": 1,
            "semantic_recall": False,
            "selection_limit": 5,
            "judged_count": 2,
            "selected_count": 1,
            "rejected_count": 1,
            "listed_count": 2,
            "listing_truncated": False,
            "not_reached_enumerated": False,
            "tier_scale": {"title": 3.0, "description": 2.0, "neighbor": 1.0},
        },
        "branches": [
            {
                "id": "ctx_1",
                "kind": "topic",
                "title": "Revenue pacing",
                "score": 3.0,
                "tier": "title",
                "matched": True,
                "rank": 1,
                "fate": "selected",
                "reason": None,
            },
            {
                "id": "ctx_2",
                "kind": "schema_doc",
                "title": "Legacy pacing",
                "score": 1.0,
                "tier": "neighbor",
                "matched": False,
                "rank": 2,
                "fate": "rejected",
                "reason": candidate_fate.REASON_BELOW_CUTOFF,
            },
        ],
    }


def test_public_schema_accepts_each_exact_state(validator) -> None:
    validator.validate(_listed())
    zero = _listed()
    zero.update(state="no_branch_judged", branches=[])
    zero["walk"].update(
        judged_count=0,
        selected_count=0,
        rejected_count=0,
        listed_count=0,
    )
    validator.validate(zero)
    validator.validate(
        {
            "schema_version": "retrieval-branch-evidence.v1",
            "state": "branches_not_recorded",
        }
    )
    validator.validate(
        {"schema_version": "retrieval-branch-evidence.v1", "state": "unavailable"}
    )


@pytest.mark.parametrize(
    ("location", "key", "value"),
    [
        ("root", "detail", {"candidate_ids": ["ctx_1"]}),
        ("root", "query", "secret"),
        ("walk", "trace_id", "f" * 32),
        ("walk", "arguments", {"project_id": "proj_foreign"}),
        ("branch", "snippet", "raw source body"),
        ("branch", "reasoning", "the model preferred this"),
    ],
)
def test_schema_recursively_refuses_every_unowned_key(validator, location, key, value) -> None:
    value_under_test = _listed()
    target = (
        value_under_test
        if location == "root"
        else value_under_test["walk"]
        if location == "walk"
        else value_under_test["branches"][0]
    )
    target[key] = value

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(value_under_test)


def test_absence_states_cannot_smuggle_a_count_or_listing(validator) -> None:
    for state in ("branches_not_recorded", "unavailable"):
        for extra in ({"branches": []}, {"walk": _listed()["walk"]}):
            value = {
                "schema_version": "retrieval-branch-evidence.v1",
                "state": state,
                **extra,
            }
            with pytest.raises(jsonschema.ValidationError):
                validator.validate(value)
