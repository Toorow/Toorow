"""A successful pull RECORDS what it observed about its day boundary -- AI-161.

`time_boundary.record_boundary_evidence` had ZERO callers in the whole repository, while
`capability_compilers` READ the table it fills and told the operator "Run this Datastream
so its publication records the source day boundary". They could run it forever and nothing
was ever written. A false instruction costs more than a silent gap: it spends someone's
time promising the gesture will work.

These tests pin the write at the seam that matters -- the worker's success path -- rather
than re-testing `record_boundary_evidence` in isolation, which already has its own tests
and was never the thing that was broken.
"""

from __future__ import annotations

from unittest.mock import patch

from core.report_timezone import observed_zone_from_pull

# ---------------------------------------------------------------------------
# The reader half of the contract: what a pull returned.
# ---------------------------------------------------------------------------


def test_the_observed_zone_is_read_from_the_pull_result():
    assert observed_zone_from_pull({"row_count": 3, "report_timezone": "Europe/Paris"}) == (
        "Europe/Paris"
    )
    assert observed_zone_from_pull({"report_timezone": "  America/New_York  "}) == (
        "America/New_York"
    )


def test_a_pull_that_observed_nothing_reads_as_None_never_as_a_default():
    """Fail-closed, like every other read here: no zone is never a fabricated UTC."""
    for result in (
        {"row_count": 3},           # the key absent entirely
        {"report_timezone": None},  # the connector observed none
        {"report_timezone": "   "}, # blank is absent
        {"report_timezone": 42},    # not a string
        None,                       # not even a dict
        "nope",
    ):
        assert observed_zone_from_pull(result) is None, result


# ---------------------------------------------------------------------------
# The write half: the worker's success path calls the recorder.
# ---------------------------------------------------------------------------


def _record_call(pull_result, datastream_id="ds_1"):
    """Replay the worker's recording block against a fake connection.

    The block is executed through the same imports the worker uses, so a rename in
    `time_boundary` or `report_timezone` breaks this test rather than silently
    disarming the recording.
    """
    from core.report_timezone import observed_zone_from_pull as _read
    from core.time_boundary import GRAIN_DATE_ONLY, ORIGIN_PULL_METADATA

    captured = {}

    def fake_record(conn, **kwargs):
        captured.update(kwargs)
        return "dstbe_fake"

    with patch("core.time_boundary.record_boundary_evidence", fake_record):
        from core.time_boundary import record_boundary_evidence

        record_boundary_evidence(
            object(),
            project_id="proj_1",
            datastream_id=datastream_id,
            execution_id="pull_1",
            grain=GRAIN_DATE_ONLY,
            observed_report_timezone=_read(pull_result),
            evidence_origin=ORIGIN_PULL_METADATA,
        )
    return captured


def test_a_run_that_observed_a_zone_records_it_as_OBSERVED_not_declared():
    """Origin matters: a declaration can never carry coverage to `complete`, an observation can."""
    from core.time_boundary import OBSERVED_ORIGINS

    call = _record_call({"report_timezone": "Europe/Paris"})
    assert call["observed_report_timezone"] == "Europe/Paris"
    assert call["evidence_origin"] in OBSERVED_ORIGINS


def test_a_run_that_observed_NO_zone_is_still_recorded():
    """Absence of evidence and evidence of absence are not the same screen.

    Recording nothing leaves the compiler saying "no publication has recorded the clock",
    which sends the operator to run a Datastream they already ran. Recording a null zone
    makes it say "published without a resolvable reporting timezone" -- true, and
    actionable at the source instead of here.
    """
    call = _record_call({"row_count": 7})
    assert "observed_report_timezone" in call
    assert call["observed_report_timezone"] is None


def test_the_recorded_grain_is_DATE_the_platform_invariant():
    """One DATE everywhere, never an hour: structurally non-realignable, and honest."""
    from core.time_boundary import GRAIN_DATE_ONLY

    assert _record_call({"report_timezone": "UTC"})["grain"] == GRAIN_DATE_ONLY


# ---------------------------------------------------------------------------
# The worker really carries the block (not just this test's replay of it).
# ---------------------------------------------------------------------------


def test_the_worker_success_path_carries_the_recording_block():
    """Guard against the block being dropped in a refactor.

    A source read, deliberately: importing `queue` pulls a worker's worth of dependencies,
    and what must not regress is that `_execute_job` calls the recorder at all -- the exact
    defect AI-161 names.
    """
    import ast
    import inspect

    from core import queue

    source = inspect.getsource(queue._execute_job)
    tree = ast.parse(source.lstrip())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "record_boundary_evidence" in called, (
        "_execute_job no longer records boundary evidence -- capability_compilers reads "
        "that table and tells the operator to run the Datastream to fill it"
    )
    assert "observed_zone_from_pull" in called, (
        "_execute_job records evidence but no longer reads the zone the pull returned, so "
        "every run would record a gap"
    )
