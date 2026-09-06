"""One vocabulary for "can this Semantic View version be pinned", two
declarations, pinned equal here.

`core.answerable_topics` declares its own `PINNABLE_VIEW_STATUSES` rather than
importing `core.golden_questions`': the anchoring guard of
`test_metric_semantics_mcp.test_the_anchoring_path_does_not_read_the_corpus_either`
forbids the answer path from naming the evaluation path at all, and that guard is
worth more than one saved import. The cost of a second declaration is drift, and
this file is what makes drift a red run.

IT LIVES IN `conformance/` BECAUSE IT NEEDS NOTHING. It imports two modules and
compares two tuples. It used to sit in `tests/core/test_topic_view_bindings_pg.py`,
whose module-level `pytest.importorskip("psycopg")` SKIPPED it wherever the
driver is absent -- so the one guard against drift was silent exactly on the
machines that had no database, which is most of them.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def test_the_pinnable_set_is_the_models_and_does_not_drift():
    from core.answerable_topics import PINNABLE_VIEW_STATUSES as answer_side
    from core.golden_questions import PINNABLE_VIEW_STATUSES as evaluation_side

    assert answer_side == evaluation_side, (
        "a topic and a Golden Question pin the same object; two rules for one pin "
        "is two answers to `can this version be pinned`"
    )


def test_the_pinnable_set_is_the_two_statuses_the_product_words_name():
    """The set itself, written out -- so widening it is a decision, not a diff.

    A `draft` version has not been ratified and a `superseded` or `archived` one
    has been moved on from; either becoming pinnable would change what a topic is
    allowed to read, and that is an amendment, not an edit.
    """
    from core.answerable_topics import PINNABLE_VIEW_STATUSES

    assert PINNABLE_VIEW_STATUSES == ("published", "candidate")
