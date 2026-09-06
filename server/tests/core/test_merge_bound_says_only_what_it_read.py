"""What the merge bound cannot read, it does not assert.

TWO READS FEED A REFUSAL OF THIS BOUND, and until 2026-08-21 only one of them
could say it had failed.

`_governed_vocabulary` reports `readable`, and a catalogue that could not be
read produces `vocabulary_unreadable` -- a sentence that names the sources and
asks the person to try again. That fail-open was closed on purpose: "unavailable
never reads as a pass".

`_reader_identities` had no such report. It fell back to `{}` on any failure,
and `{}` is exactly what a registry that holds no archived field looks like. So
an unreadable registry made every archived measure look UNDECLARED, and the
refusal told the person to

    Declare it in Governance with an aggregation, then compile this cross again.

about a field they had declared and then archived themselves. That is the
impossible gesture the `metric_archived` branch was written to close, reopened
by a read failure -- and reopened silently, which is why nothing caught it.

Both tests below drive `_merge_bound` directly with the two readability flags,
because that is where the sentence is chosen. Neither needs a database: the
defect is in what the function concludes from what it was handed.
"""

from __future__ import annotations

from core import multi_source_plan as plans

from tests.support.minted_identifiers import identifier_rendered_in

FIELD = "mdm_01EXAMPLE000000000000000"
KEY = "mdm_01EXAMPLE000000000000001"

MEMBERS = [
    {
        "datastream_id": "ds_01EXAMPLE0000000000000000",
        "name": "Campaign spend",
        "measures": [{"canonical_field_id": FIELD, "physical_field": "spend_micros"}],
    }
]
EDGES = [{"components": [{"canonical_field_id": KEY, "canonical_name": "Reporting day"}]}]


def _verdict(**overrides):
    arguments = {
        "additivity": {},
        "vocabulary": {},
        "additivity_readable": True,
        "names": {FIELD: "Campaign spend, in euros"},
        "archived": {FIELD},
        "identities_readable": True,
    }
    arguments.update(overrides)
    return plans._merge_bound(
        MEMBERS,
        EDGES,
        [],
        None,
        arguments.pop("additivity"),
        arguments.pop("vocabulary"),
        **arguments,
    )


def test_an_archived_measure_is_told_to_be_restored_not_declared():
    """The gesture that CAN be performed, when the registry answered."""
    verdict = _verdict()

    assert not verdict["within"]
    message = verdict["failures"][0]["message"]
    assert "Restore it in Governance" in message
    assert "Declare it in Governance" not in message


def test_an_unreadable_registry_never_says_a_measure_was_never_declared():
    """THE FAIL-SOFT, CLOSED. The registry answered nothing -- so neither does the sentence.

    With `identities_readable=False` the bound holds no opinion on whether this
    measure is archived or was never declared, and it says so instead of picking
    the branch an empty set happens to fall into. The old behaviour is the
    assertion that fails here: `archived={}` plus a silent failure produced
    "not in the governed vocabulary ... Declare it in Governance".
    """
    verdict = _verdict(identities_readable=False, archived=set(), names={})

    assert not verdict["within"]
    message = verdict["failures"][0]["message"]
    assert "could not be read at all" in message
    assert "Declare it in Governance" not in message
    assert "Restore it in Governance" not in message
    # And the sources are still named -- the person keeps a handle on what refused.
    assert "Campaign spend" in message


def test_the_unreadable_sentence_carries_no_canonical_identifier():
    """A refusal built out of a failed read is still a refusal of this module."""
    verdict = _verdict(identities_readable=False, archived=set(), names={})

    leaked = identifier_rendered_in(verdict["failures"][0]["message"])
    assert not leaked, f"the refusal renders {leaked!r}"
