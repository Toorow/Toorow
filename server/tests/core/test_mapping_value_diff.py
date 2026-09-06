"""What a person reads before confirming a mapping change — 2026-08-18.

The defect these pin: `DatastreamChangeDialog` rendered `before_hash` /
`after_hash` per top-level contract path, so excluding one column of forty-six
was confirmed against one row of two hex strings. `core.mapping_value_diff` composes
the same difference in values; these tests hold it to being a READING — never a
refusal, never a second authority on the vocabulary, and never silent about a
value it cut.
"""

from __future__ import annotations

import json

from core import mapping_value_diff
from core.mapping_value_diff import MAX_VALUE_CHARS, value_diff


def _field(field_id: str, **binding) -> dict:
    return {"field_id": field_id, "binding": {"status": "suggested", **binding}}


def _readings(result: dict) -> set[tuple[str, str, str, str]]:
    return {
        (entry["subject"], entry["reading"], entry["before"], entry["after"])
        for entry in result["entries"]
    }


def test_excluding_one_column_reads_as_that_column_not_as_a_hash() -> None:
    before = {"fields": [_field("date"), _field("spend"), _field("clicks")]}
    after = {
        "fields": [_field("date"), _field("spend", status="excluded"), _field("clicks")],
    }
    result = value_diff("mapping", before, after)

    assert result["state"] == "composed"
    assert ("spend", "Landing", "Lands", "Does not land (excluded)") in _readings(result)
    # The two columns nobody touched are ABSENT from the reading. A diff that
    # listed every field would be the wall of rows this repair exists to remove.
    assert {entry["subject"] for entry in result["entries"]} == {"spend"}


def test_a_binding_names_the_concept_and_not_only_its_registry_identity() -> None:
    """`datastream-workbench-and-wizard.md:4299` — the server names, the client does not."""
    before = {"fields": [_field("day")]}
    after = {"fields": [_field("day", mdm_target="mdm_6D13WZEXAMPLE")]}

    named = value_diff(
        "mapping", before, after, canonical_names={"mdm_6D13WZEXAMPLE": "event_date"}
    )
    target = next(entry for entry in named["entries"] if entry["reading"] == "Governed target")
    assert target["before"] == "No governed target"
    assert target["after"] == "event_date (mdm_6D13WZEXAMPLE)"

    # An unreadable vocabulary costs the reading its NAME, never its truth: the
    # identity stands alone rather than being invented into a name.
    unnamed = value_diff("mapping", before, after)
    unnamed_target = next(
        entry for entry in unnamed["entries"] if entry["reading"] == "Governed target"
    )
    assert unnamed_target["after"] == "mdm_6D13WZEXAMPLE"


def test_both_target_keys_are_read_because_the_estate_uses_both() -> None:
    """105 of 708 bindings carry the identity under `canonical_target` alone."""
    before = {"fields": [_field("day", canonical_target="mdm_A")]}
    after = {"fields": [_field("day", canonical_target="mdm_A", status="confirmed")]}
    result = value_diff("mapping", before, after, canonical_names={"mdm_A": "event_date"})

    # The target did not change, so it is not reported as having changed.
    assert [entry["reading"] for entry in result["entries"]] == ["Binding state"]


def test_a_column_that_appears_or_disappears_says_which() -> None:
    before = {"fields": [_field("date")]}
    after = {"fields": [_field("date"), _field("impressions", mdm_target="mdm_B")]}
    result = value_diff("mapping", before, after, canonical_names={"mdm_B": "impressions"})

    readings = _readings(result)
    assert ("impressions", "Presence in the contract", "Absent", "Declared") in readings
    assert (
        "impressions",
        "Governed target",
        "No governed target",
        "impressions (mdm_B)",
    ) in readings


def test_a_declared_join_reads_as_a_sentence_not_as_its_json() -> None:
    before: dict = {"fields": [_field("date_part"), _field("hour")]}
    after = {
        "fields": before["fields"],
        "column_treatments": {
            "joins": [
                {"target": "event_date", "sources": ["date_part", "hour"], "separator": "-"}
            ],
            "splits": [],
        },
    }
    result = value_diff("mapping", before, after)
    join = next(entry for entry in result["entries"] if entry["reading"] == "Declared join")
    assert join["before"] == "Not declared"
    assert join["after"] == "date_part + hour → event_date joined by “-”"
    # `column_treatments` is never ALSO reported as a raw contract path: one
    # change, one row, or the reader counts it twice.
    assert not any(entry["subject"] == "$.column_treatments" for entry in result["entries"])


def test_anything_a_raw_edit_reaches_is_still_reported() -> None:
    """The dialog's raw door can change paths no control writes; silence would lie."""
    before = {"fields": [{"field_id": "date", "suggestion": {"semantic_role": "dimension"}}],
              "grain": ["date"]}
    after = {"fields": [{"field_id": "date", "suggestion": {"semantic_role": "metric"}}],
             "grain": ["date", "country"]}
    result = value_diff("mapping", before, after)

    readings = {(entry["subject"], entry["reading"]) for entry in result["entries"]}
    assert ("date", "Other readings of this column") in readings
    assert ("$.grain", "Contract path") in readings


def test_a_cut_value_says_it_was_cut() -> None:
    before = {"grain": ["a"]}
    after = {"grain": ["x" * (MAX_VALUE_CHARS * 2)]}
    entry = value_diff("processing", before, after)["entries"][0]

    assert entry["truncated"] is True
    assert "characters)" in entry["after"]
    assert len(entry["before"]) <= MAX_VALUE_CHARS


def test_a_processing_change_is_read_path_by_path_in_values() -> None:
    before = {"schedule": {"interval_minutes": 1440}, "selection": {"metrics": ["spend"]}}
    after = {"schedule": {"interval_minutes": 60}, "selection": {"metrics": ["spend"]}}
    result = value_diff("processing", before, after)

    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["subject"] == "$.schedule"
    assert json.loads(entry["after"]) == {"interval_minutes": 60}


def test_two_identical_contracts_compose_an_empty_reading_not_a_failure() -> None:
    payload = {"fields": [_field("date")]}
    result = value_diff("mapping", payload, json.loads(json.dumps(payload)))
    assert result == {"state": "composed", "entries": []}


def test_the_module_never_writes() -> None:
    """A reading that could write would be a second door onto an immutable ledger."""
    source = (mapping_value_diff.__file__)
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "cursor("):
        assert forbidden not in text
