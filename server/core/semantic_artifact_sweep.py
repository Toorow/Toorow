"""Re-derive stale compiled Semantic View artifacts from a running process (AI-346).

`semantic_model.recompile_stale_artifacts` is the sweep; this module is where a
PROCESS runs it, best-effort, in the two places the ratified amendment names
(`governance.md`, 2026-09-01):

* at process start, in a daemon thread, so a deploy that bumps
  `COMPILER_VERSION` re-derives every artifact before the first question reaches
  `_load_pinned_view`;
* every night, as the first step of `scheduler.run_nightly_steps`.

Best-effort means: it logs, it never raises out of the thread, and a sweep that
could not run is a WARNING with the reason -- never a crashed process and never
silence. What it could not RECOMPILE is not a failure of the sweep: it is
recorded per version (`app.semantic_recompile_attempts`) and named by the
refusal the person reads.

Environment
-----------
SEMANTIC_RECOMPILE_ON_START   default "true" -- set to "false" to skip the
                              startup thread (tests and CI do, in conftest).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_STARTUP_THREAD_NAME = "semantic-recompile-startup"


def run_sweep(source: str) -> dict[str, Any] | None:
    """One sweep on the platform database, committed. Returns the report, or None.

    `source` names the run in the audit row and the attempt record
    (`system:semantic-recompile:<source>`).
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.semantic_model import RECOMPILE_ACTOR, recompile_stale_artifacts  # noqa: PLC0415

    try:
        with get_connection() as conn:
            report = recompile_stale_artifacts(conn, actor=f"{RECOMPILE_ACTOR}:{source}")
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- best-effort by contract; the reason is logged
        logger.warning("semantic_recompile: sweep_failed source=%s error=%s", source, exc)
        return None
    _log_report(report, source)
    return report


def _log_report(report: dict[str, Any], source: str) -> None:
    recompiled = report.get("recompiled") or []
    refused = report.get("refused") or []
    if not recompiled and not refused:
        logger.info(
            "semantic_recompile: nothing_stale source=%s compiler=%s",
            source,
            report.get("compiler_version"),
        )
        return
    for entry in recompiled:
        logger.info(
            "semantic_recompile: recompiled source=%s project=%s view=%s version=%s "
            "from=%s artifact=%s",
            source,
            entry.get("project_id"),
            entry.get("view_id"),
            entry.get("version_id"),
            entry.get("compiled_by"),
            entry.get("artifact_id"),
        )
    for entry in refused:
        # AT WARNING. This is the line that says a published View stays
        # unqueryable after the product tried -- the silence AI-346 closes.
        logger.warning(
            "semantic_recompile: refused source=%s project=%s view=%s version=%s "
            "from=%s codes=%s",
            source,
            entry.get("project_id"),
            entry.get("view_id"),
            entry.get("version_id"),
            entry.get("compiled_by"),
            ",".join(entry.get("codes") or []),
        )


def start_startup_sweep() -> None:
    """Run the sweep once, in a daemon thread, at process start (call from build_asgi_app)."""
    if os.environ.get("SEMANTIC_RECOMPILE_ON_START", "true").lower() != "true":
        logger.debug("semantic_recompile: startup sweep disabled")
        return
    if any(t.name == _STARTUP_THREAD_NAME for t in threading.enumerate()):
        logger.info("semantic_recompile: startup sweep already running")
        return
    threading.Thread(
        target=run_sweep, args=("startup",), daemon=True, name=_STARTUP_THREAD_NAME
    ).start()
    logger.info("semantic_recompile: startup sweep started")
