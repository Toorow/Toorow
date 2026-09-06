"""The estimate says the same thing in the code, the document and the console.

WHY THIS FILE IS THE GUARD OF THE LOT. Story 63.4 puts a NUMBER in a ratified
document -- how many finished runs an estimate needs before it speaks, and how
many it averages over. A number written in prose ages the day someone tunes the
constant, and it ages SILENTLY: the document goes on claiming a bound that the
code stopped honouring, which is worse than no document at all. So the bounds are
read out of `server/core/datastream_progress_estimate.py` and looked for in the
text, never typed here.

Built on the pattern of `test_execution_state_registry.py`, which already reads
`ui/admin/src` from a Python test for exactly this reason.

AND IT PINS WHERE THE ARITHMETIC LIVES. The estimate is computed on the server
because the MCP reads the same payload as the screen; a duration derived in the
console would be a second answer the tools cannot see. That is a property of the
WHOLE console, not of one file, so the sweep looks at every `.ts`/`.tsx` under
`ui/admin/src`.
"""

from __future__ import annotations

import re
from pathlib import Path

from core import datastream_progress_estimate as estimate
from core import execution_states
from core.datastream_progress_api import PROGRESS_FIELDS
from core.datastream_progress_estimate import PRECISION_POINT

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "server" / "core" / "datastream_progress_estimate.py"
SURFACE = ROOT / "docs" / "product-architecture" / "datastream-workbench-and-wizard.md"
CONSOLE = ROOT / "ui" / "admin" / "src"
HOOK = CONSOLE / "datastreams" / "workbench" / "datastreamProgress.ts"


def _surface() -> str:
    return SURFACE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The bounds the document states are the bounds the code applies.
# ---------------------------------------------------------------------------


def test_the_minimum_the_document_states_is_the_one_the_code_applies() -> None:
    """Read from the module, looked for in the prose -- never typed in this test."""
    text = _surface()
    minimum = estimate.MINIMUM_FINISHED_RUNS
    spelled = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}[minimum]
    assert f"below {spelled} finished runs" in text.lower(), (
        f"the ratified surface does not state the minimum the code applies "
        f"({minimum} finished runs)"
    )
    assert f"fewer than {spelled} finished runs" in text.lower()


def test_the_window_the_median_is_taken_over_is_stated_too() -> None:
    text = _surface()
    assert f"**{estimate.HISTORY_RUNS}** finished runs" in text, (
        f"the surface does not say the median is taken over the last "
        f"{estimate.HISTORY_RUNS} finished runs"
    )


def test_every_silence_the_code_can_answer_is_named_in_the_document() -> None:
    """A fifth reason added tomorrow cannot ship without its sentence."""
    text = _surface()
    for reason in estimate.ESTIMATE_REASONS:
        assert f"`{reason}`" in text, reason
    assert len(set(estimate.ESTIMATE_REASONS)) == 4
    spelled = {3: "three", 4: "four", 5: "five"}[len(estimate.ESTIMATE_REASONS)]
    assert f"declines in {spelled} distinct sentences" in text


def test_the_spread_the_code_degrades_at_is_the_one_the_document_states() -> None:
    """The dispersion rule is a NUMBER in prose: it may not age in silence."""
    text = _surface()
    limit = estimate.MAX_SPREAD_RATIO
    assert limit == int(limit), "a fractional spread limit needs its own wording"
    assert f"a factor of **{int(limit)}**" in text, (
        f"the surface does not state the spread the code degrades at ({limit})"
    )
    assert "`spread_ratio`" in text
    assert "`precision` becomes `range`" in text


def test_the_payload_field_the_document_promises_exists_in_the_contract() -> None:
    assert "estimate" in PROGRESS_FIELDS
    assert "`estimate`" in _surface()


def test_the_document_keeps_the_rules_that_make_the_number_honest() -> None:
    """The unit, the two server instants, and the zero that may never be printed."""
    text = _surface()
    assert "The unit is the day, never the window." in text
    assert "Both ends of every subtraction are server instants." in text
    assert "Both halves of that fraction cover exactly the same windows" in text
    assert "stops being estimated, and says by how" in text
    # And the amendment is written down as an amendment, with its measurement.
    assert "amendment to the epic" in text


def test_the_document_records_what_the_bounded_read_cost_before_and_after() -> None:
    """Migration 219 is a cost claim; a cost claim without its two numbers is prose."""
    text = _surface()
    migration = (
        ROOT / "infra" / "nango" / "migrations"
        / "219_the_rate_of_a_stream_is_read_bounded.sql"
    ).read_text(encoding="utf-8")

    assert "idx_pull_jobs_datastream_completed" in text
    assert "idx_pull_jobs_datastream_completed" in migration
    assert f"{estimate.HISTORY_WINDOWS} rows" in text
    # The bound is derived from a measurement of this repository, not chosen.
    assert str(estimate.MAX_WINDOWS_PER_RUN) in migration
    assert estimate.HISTORY_WINDOWS == estimate.HISTORY_RUNS * estimate.MAX_WINDOWS_PER_RUN


def test_no_zero_countdown_can_reach_a_screen() -> None:
    """The defect a `max(0, ...)` clamp shipped, closed by construction.

    Driven over the whole grid rather than on one example: whatever the sample
    and however long the window has been running, an ARMED point estimate is
    never `0`, and every refusal carries a sentence.
    """
    from datetime import datetime, timezone

    started = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)
    for hours in (0, 1, 2, 8, 24, 48, 240):
        for samples in ([60.0] * 3, [10.0, 120.0, 1200.0], [59.0, 60.0, 61.0]):
            answer = estimate.estimate_time_left(
                days_done=0,
                days_total=31,
                window_days=31,
                window_started_at=started,
                samples=samples,
                measured_at=started.replace(day=5 + hours // 24, hour=hours % 24),
            )
            assert answer["seconds_remaining"] != 0, (hours, samples)
            assert answer["sentence"].strip(), (hours, samples)
            if answer["armed"] and answer["precision"] == PRECISION_POINT:
                assert answer["seconds_remaining"] > 0, (hours, samples)
            else:
                assert answer["seconds_remaining"] is None, (hours, samples)


# ---------------------------------------------------------------------------
# The module itself.
# ---------------------------------------------------------------------------


def test_no_execution_state_is_typed_in_the_estimate_module() -> None:
    """Story 63.1's rule, applied to the newest reader of a run.

    Six copies of the state list existed before `core.execution_states`, and
    adding one state broke four surfaces in four ways.
    """
    source = MODULE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    typed = [
        name
        for name in execution_states.BY_NAME
        if f'"{name}"' in code or f"'{name}'" in code
    ]
    assert not typed, f"these state names are typed instead of generated: {typed}"


def test_the_bounds_are_named_constants_and_not_scattered_literals() -> None:
    """A `3` inlined in a comparison is a bound nothing can read or change.

    Both comparisons that decide whether the estimate speaks must name the
    constant -- which is also what makes the two tests above able to check the
    document against it.
    """
    source = MODULE.read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    gates = re.findall(
        r"(observations|len\(usable\))\s*(?:<|>=)\s*([A-Za-z_0-9]+)", body
    )
    assert len(gates) == 2, gates
    assert {named for _left, named in gates} == {"MINIMUM_FINISHED_RUNS"}, gates
    assert isinstance(estimate.HISTORY_RUNS, int) and estimate.HISTORY_RUNS > 0


def test_the_history_window_reaches_the_statement_that_reads_it() -> None:
    """The constants must be the ones the SQL is generated from, not twins.

    And the ROW bound must be inside the subquery that feeds the aggregate: a
    `LIMIT` above a `GROUP BY` bounds what is returned, never what is read.
    """
    sql = (ROOT / "server" / "core" / "datastream_progress_api.py").read_text(
        encoding="utf-8"
    )
    assert "from core.datastream_progress_estimate import" in sql
    assert "LIMIT {HISTORY_RUNS}" in sql
    assert "LIMIT {HISTORY_WINDOWS}" in sql

    from core.datastream_progress_api import PROGRESS_SQL

    assert PROGRESS_SQL.index(f"LIMIT {estimate.HISTORY_WINDOWS}") < PROGRESS_SQL.index(
        "GROUP BY"
    ), (
        "the row bound sits after the aggregate: every tick still reads the "
        "whole pull-job history of the Datastream\n" + PROGRESS_SQL
    )


# ---------------------------------------------------------------------------
# The console reads the number; it never computes one.
# ---------------------------------------------------------------------------


def _console_sources() -> list[Path]:
    return [
        path
        for path in CONSOLE.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"} and "__tests__" not in path.parts
    ]


def test_no_screen_of_the_console_computes_a_remaining_duration() -> None:
    """The class, not the instance: any file doing this arithmetic is a second answer.

    The MCP reads the same payload as the screen. A console that subtracts
    `days_done` from `days_total` and multiplies by something is answering "how
    much longer" on its own, and the tools cannot see the answer it gives.
    """
    forbidden = re.compile(r"days_total\s*[-*/]|days_done\s*[-*/]|remainingMs")
    offenders = [
        str(path.relative_to(ROOT)).replace("\\", "/")
        for path in _console_sources()
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "console file(s) deriving a duration from the progress numbers instead "
        f"of reading the server's estimate: {offenders}"
    )


def test_the_console_reads_the_estimate_and_keeps_it_out_of_the_poll_signature() -> None:
    """A countdown in the signature holds the poll at five seconds for a whole run."""
    source = HOOK.read_text(encoding="utf-8")
    assert "readEstimate" in source
    assert "progressSignature" in source
    signature = source.split("export function progressSignature", 1)[1].split("}\n", 1)[0]
    assert "estimate: _estimate" in signature, (
        "the quiet-cadence signature no longer excludes the estimate: the poll "
        "will stay at the live cadence for the length of every run"
    )
