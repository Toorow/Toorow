"""Story 65.6 AC7 -- measure the request-scoped live-emission overhead.

The instrument runs the same Result-owned recording workload with and without a
``progressToken``. Ten alternating pairs warm the process; at least 200 further
pairs are timed, reversing arm order on every pair. The gate is the p95 of the
per-pair ``armed - silent`` deltas, not the difference between two independent
percentiles.

The progress sink only counts calls and returns. It deliberately excludes host
transport and rendering. A separate sink never returns, proving that sender and
cleanup together respect the 25 ms request wall bound.

Run it::

    cd server && python -m tests.perf.ai_path_emission_overhead --calls 200

or as a gate::

    cd server && python -m pytest tests/perf/ai_path_emission_overhead.py -q
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import anyio
import core.ai_path_recorder as mod
import core.ai_paths as ai_paths
from core.ai_path_recorder import emit_step_sync

DEFAULT_CALLS = 200
WARMUP_PAIRS = 10
HUNG_SINK_BUDGET_MS = 25.0
NFR1_BUDGET_MS = 50.0

_CROSSINGS = 3
_NOTIFICATIONS_PER_ARMED_CALL = _CROSSINGS + 1  # recording + observed crossings
_TOOL_NAME = mod.RESULT_EXECUTION_TOOL_NAME


class _Sink:
    """A no-op progress channel which counts calls but performs no reader work."""

    def __init__(self, *, progress_token: str | None):
        self.request_context = SimpleNamespace(
            meta=SimpleNamespace(progressToken=progress_token)
        )
        self.received = 0

    async def report_progress(self, progress, total=None, message=None):
        self.received += 1


class _HungSink(_Sink):
    """A transport that accepts the call and then never completes it."""

    def __init__(self, *, progress_token: str | None):
        super().__init__(progress_token=progress_token)
        self.cancelled = 0

    async def report_progress(self, progress, total=None, message=None):
        self.received += 1
        try:
            await asyncio.Future()
        finally:
            self.cancelled += 1


def _context(sink: _Sink) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(
            name=_TOOL_NAME,
            arguments={"project_id": "proj_EXAMPLE", "query_spec_id": "qs_EXAMPLE"},
            meta=None,
        ),
        fastmcp_context=sink,
    )


def _execute(path_id: str) -> dict[str, Any]:
    for index in range(_CROSSINGS):
        emit_step_sync(
            step_kind="knowledge_read",
            outcome="succeeded",
            tool_name="search_context",
            owner_workspace="context-hub",
            owner_object_type="topic",
            owner_object_id=f"ctx_EXAMPLE_{index}",
            detail={"considered": 20, "rank": index},
        )
    return {"result_id": "qr_perf", "outcome": "success", "ai_path": path_id}


async def _tool_body(_context: Any) -> dict[str, Any]:
    return await anyio.to_thread.run_sync(
        lambda: mod.record_result_execution(
            object(),
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            actor="perf-harness",
            tool_name=_TOOL_NAME,
            execute=_execute,
        )
    )


async def _run_once(middleware: Any, context: SimpleNamespace) -> float:
    started = time.perf_counter()
    await middleware.on_call_tool(context, _tool_body)
    return (time.perf_counter() - started) * 1000.0


async def _run_pair(
    middleware: Any,
    *,
    silent_context: SimpleNamespace,
    armed_context: SimpleNamespace,
    armed_first: bool,
) -> tuple[float, float]:
    arms = (
        (("armed", armed_context), ("silent", silent_context))
        if armed_first
        else (("silent", silent_context), ("armed", armed_context))
    )
    durations: dict[str, float] = {}
    for name, context in arms:
        durations[name] = await _run_once(middleware, context)
    return durations["silent"], durations["armed"]


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("a percentile needs at least one value")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _install_persistence_fakes() -> tuple[dict[str, int], Callable[[], None]]:
    counts = {"begun": 0, "steps": 0, "finalized": 0}
    originals = (
        ai_paths.begin_path,
        ai_paths.append_step,
        ai_paths.finalize_path,
        mod._skill_step_of,
    )

    def begin_path(*args, **kwargs):
        counts["begun"] += 1
        return {"id": f"aip_perf_{counts['begun']}"}

    def append_step(*args, **kwargs):
        counts["steps"] += 1
        return {"ordinal": counts["steps"] - 1}

    def finalize_path(*args, **kwargs):
        counts["finalized"] += 1
        return None

    ai_paths.begin_path = begin_path
    ai_paths.append_step = append_step
    ai_paths.finalize_path = finalize_path
    mod._skill_step_of = lambda *args, **kwargs: None

    def restore() -> None:
        ai_paths.begin_path, ai_paths.append_step, ai_paths.finalize_path, mod._skill_step_of = (
            originals
        )

    return counts, restore


async def _measure_hung_sink(middleware: Any) -> tuple[float, int, int]:
    sink = _HungSink(progress_token="tok-hung")
    started = time.perf_counter()
    try:
        await asyncio.wait_for(
            middleware.on_call_tool(_context(sink), _tool_body), timeout=0.5
        )
    except TimeoutError:
        return float("inf"), sink.received, sink.cancelled
    return (time.perf_counter() - started) * 1000.0, sink.received, sink.cancelled


async def measure(calls: int = DEFAULT_CALLS) -> dict[str, int | float]:
    if calls < DEFAULT_CALLS:
        raise ValueError(f"calls must be >= {DEFAULT_CALLS}")

    middleware = mod.build_middleware()
    if middleware is None:  # pragma: no cover -- FastMCP is a hard dependency here
        raise RuntimeError("FastMCP middleware base unavailable")

    silent_sink = _Sink(progress_token=None)
    armed_sink = _Sink(progress_token="tok-perf")
    silent_context = _context(silent_sink)
    armed_context = _context(armed_sink)
    counts, restore = _install_persistence_fakes()
    try:
        for pair_index in range(WARMUP_PAIRS):
            await _run_pair(
                middleware,
                silent_context=silent_context,
                armed_context=armed_context,
                armed_first=bool(pair_index % 2),
            )

        silent_sink.received = 0
        armed_sink.received = 0
        counts.update(begun=0, steps=0, finalized=0)

        silent: list[float] = []
        armed: list[float] = []
        paired_deltas: list[float] = []
        for pair_index in range(calls):
            silent_ms, armed_ms = await _run_pair(
                middleware,
                silent_context=silent_context,
                armed_context=armed_context,
                armed_first=bool(pair_index % 2),
            )
            silent.append(silent_ms)
            armed.append(armed_ms)
            paired_deltas.append(armed_ms - silent_ms)

        measured_counts = counts.copy()
        hung_sink_wall_ms, hung_sink_notifications, hung_sink_cancellations = (
            await _measure_hung_sink(middleware)
        )
    finally:
        restore()

    return {
        "warmup_pairs": WARMUP_PAIRS,
        "alternating_pairs": calls,
        "calls_per_arm": calls,
        "timed_calls": calls * 2,
        "paired_deltas": len(paired_deltas),
        "notifications_armed": armed_sink.received,
        "notifications_silent": silent_sink.received,
        "notifications_per_call_armed": armed_sink.received / calls,
        "recorded_paths": measured_counts["begun"],
        "recorded_steps": measured_counts["steps"],
        "finalized_paths": measured_counts["finalized"],
        "p50_silent_ms": _percentile(silent, 0.50),
        "p50_armed_ms": _percentile(armed, 0.50),
        "p95_silent_ms": _percentile(silent, 0.95),
        "p95_armed_ms": _percentile(armed, 0.95),
        "p50_delta_ms": _percentile(paired_deltas, 0.50),
        "p95_delta_ms": _percentile(paired_deltas, 0.95),
        "mean_delta_ms": statistics.fmean(paired_deltas),
        "budget_ms": NFR1_BUDGET_MS,
        "configured_send_budget_ms": mod.LIVE_SEND_BUDGET_SECONDS * 1000.0,
        "hung_sink_wall_ms": hung_sink_wall_ms,
        "hung_sink_notifications": hung_sink_notifications,
        "hung_sink_cancellations": hung_sink_cancellations,
        "hung_sink_budget_ms": HUNG_SINK_BUDGET_MS,
    }


def test_the_emission_overhead_stays_inside_the_nfr1_envelope() -> None:
    report = asyncio.run(measure(DEFAULT_CALLS))
    assert report["warmup_pairs"] == WARMUP_PAIRS
    assert report["alternating_pairs"] == DEFAULT_CALLS
    assert report["paired_deltas"] == DEFAULT_CALLS
    assert report["timed_calls"] == DEFAULT_CALLS * 2
    assert report["notifications_silent"] == 0
    assert report["notifications_per_call_armed"] == _NOTIFICATIONS_PER_ARMED_CALL
    assert report["recorded_paths"] == DEFAULT_CALLS * 2
    assert report["recorded_steps"] == DEFAULT_CALLS * 2 * (_CROSSINGS + 1)
    assert report["finalized_paths"] == DEFAULT_CALLS * 2
    assert report["p95_delta_ms"] < NFR1_BUDGET_MS, report
    assert report["configured_send_budget_ms"] <= HUNG_SINK_BUDGET_MS
    assert report["hung_sink_notifications"] == 1
    assert report["hung_sink_cancellations"] == 1
    assert report["hung_sink_wall_ms"] <= HUNG_SINK_BUDGET_MS, report


def test_the_harness_refuses_a_statistically_insufficient_run() -> None:
    try:
        asyncio.run(measure(DEFAULT_CALLS - 1))
    except ValueError as exc:
        assert str(DEFAULT_CALLS) in str(exc)
    else:  # pragma: no cover -- documents the refusal if it regresses
        raise AssertionError("the performance harness accepted fewer than 200 pairs")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=DEFAULT_CALLS)
    args = parser.parse_args()
    try:
        report = asyncio.run(measure(args.calls))
    except ValueError as exc:
        parser.error(str(exc))

    width = max(len(key) for key in report)
    for key, value in report.items():
        rendered = f"{value:.4f}" if isinstance(value, float) else value
        print(f"{key.ljust(width)}  {rendered}")
    passed = (
        report["p95_delta_ms"] < NFR1_BUDGET_MS
        and report["hung_sink_wall_ms"] <= HUNG_SINK_BUDGET_MS
    )
    verdict = "PASS" if passed else "FAIL"
    print(
        f"\npaired p95 delta {report['p95_delta_ms']:.4f} ms < {NFR1_BUDGET_MS} ms; "
        f"hung sink {report['hung_sink_wall_ms']:.4f} ms <= "
        f"{HUNG_SINK_BUDGET_MS} ms -> {verdict}"
    )
    return 0 if passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
