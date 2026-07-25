"""toorow -- LLM-built adaptation lifecycle: lock, replay, re-analyse (22.20/23/24).

Ties the isolated worker (22.21) + executor (22.22) to the versioned template
artifact (22.11) and the producer/gate (22.12/22.14/22.15):

  * lock_adaptation_template  (22.23) -- store a self-tested ``.py`` as an
    ``adaptation``-kind ``file_source_templates`` version (content-hashed,
    immutable) through ``operations.execute_operation``; output is canonical rows
    only (AD-2/AD-4).
  * replay_adaptation         (22.24) -- run the LOCKED ``.py`` in the isolated
    worker on a later file; byte-identical canonical rows by upload and by email;
    a drifted file (a required canonical field missing from the output) re-enters
    the gate (CAP-9/AD-8) instead of mis-mapping.
  * reanalyse_with_adaptation (22.20) -- run a host-LLM-proposed ``.py`` on a
    flagged sample, re-render canonical rows and re-enter the SAME validation gate;
    the caller confirms + locks only on a passing preview (AD-7/AD-8).
"""

from __future__ import annotations

import hashlib
from typing import Any

from core.adaptation_executor import execute_adaptation_preview, run_adaptation
from core.csv_excel_import import ColumnSpec, ParseResult
from core.file_source_producer import (
    ReshapeProducerError,
    canonical_rows_signature,
    stamp_placement,
)
from core.file_source_template import create_file_source_template


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows_to_parse_result(
    rows: list[dict[str, Any]], rejected: list, content_hash: str
) -> ParseResult:
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    columns = [ColumnSpec(name=k, index=i, detected_type="text") for i, k in enumerate(keys)]
    return ParseResult(
        rows=rows, rejected=rejected, columns=columns, encoding="adaptation",
        delimiter=None, sheet_name=None, detected_row_count=len(rows),
        content_hash=content_hash,
    )


# ---------------------------------------------------------------------------
# 22.23 -- lock a confirmed adaptation as a versioned template.
# ---------------------------------------------------------------------------


def lock_adaptation_template(
    conn,
    *,
    project_id: str,
    org_id: str,
    template_code: str,
    py_source: str,
    placement_class: str,
    placement: dict[str, Any],
    required_fields: list[str],
    grain: str,
    created_by: str,
    optional_fields: list[str] | None = None,
    aliases: dict[str, Any] | None = None,
    label: str | None = None,
    idempotency_key: str | None = None,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Lock a self-tested ``.py`` as an ``adaptation`` template version (22.23).

    The caller has already run the adaptation in the worker, the sample output
    passed the gate (22.15), and a human confirmed it (22.19). This persists it
    through ``create_file_source_template`` (execute_operation, immutable,
    content-hashed) with ``kind='adaptation'``, the ``.py`` source and its hash.
    """
    contract: dict[str, Any] = {
        "kind": "adaptation",
        "class": placement_class,
        "grain": grain,
        "placement": placement,
        "required_fields": required_fields,
        "optional_fields": optional_fields or [],
        "py_content_hash": hashlib.sha256(py_source.encode("utf-8")).hexdigest(),
        "py_source": py_source,
    }
    if aliases:
        contract["aliases"] = aliases
    return create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code=template_code,
        contract=contract, created_by=created_by, label=label,
        idempotency_key=idempotency_key, host_context=host_context, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# 22.24 -- replay a locked adaptation, byte-identically, with drift re-gate.
# ---------------------------------------------------------------------------


def _adaptation_source(template: dict[str, Any]) -> str:
    contract = template.get("contract") if "contract" in template else template
    py_source = (contract or {}).get("py_source")
    if not isinstance(py_source, str) or not py_source.strip():
        raise ReshapeProducerError(
            "adaptation_source_missing",
            "the locked adaptation template carries no .py source to replay.",
        )
    return py_source


def check_adaptation_drift(template: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A required canonical field absent from the output = drift -> re-enter the gate.

    An adaptation parses raw bytes freely, so drift is detected on its OUTPUT: if a
    required canonical field is missing from every produced row, the file must
    re-enter the AD-7 gate for re-confirmation rather than land mis-mapped (AD-8).
    """
    contract = template.get("contract") if "contract" in template else template
    required = list((contract or {}).get("required_fields") or [])
    present: set[str] = set()
    for row in rows:
        present.update(row.keys())
    missing = [rid for rid in required if rid not in present]
    return {
        "status": "needs_revalidation" if missing else "ok",
        "missing_required": missing,
    }


def replay_adaptation(
    data: bytes,
    template: dict[str, Any],
    *,
    filename: str | None = None,
    wall_seconds: int = 10,
) -> ParseResult:
    """Replay a LOCKED adaptation on ``data`` in the isolated worker (22.24).

    Runs the locked ``.py`` (never in-process), stamps the matrix placement, and
    returns canonical rows. The same bytes + locked template yield byte-identical
    rows by upload and by email (``canonical_rows_signature``). Raises when the
    worker returns a bounded error or the output drifts (a required field missing).
    """
    if not template.get("content_hash"):
        raise ReshapeProducerError(
            "template_not_locked", "replay requires a locked, content-hashed template."
        )
    py_source = _adaptation_source(template)
    result = run_adaptation(py_source, data, template, wall_seconds=wall_seconds)
    if "error" in result:
        err = result["error"]
        raise ReshapeProducerError(
            f"adaptation_{err.get('code', 'error')}", str(err.get("message", ""))
        )
    rows = result.get("rows", [])
    drift = check_adaptation_drift(template, rows)
    if drift["status"] == "needs_revalidation":
        raise ReshapeProducerError(
            "adaptation_drift",
            "required canonical field(s) missing from the adaptation output: "
            + ", ".join(drift["missing_required"]) + " -- re-enter the gate (AD-8).",
        )
    parse = _rows_to_parse_result(rows, result.get("rejected", []), _content_hash(data))
    return stamp_placement(parse, template, filename=filename)


def adaptation_signature(data: bytes, template: dict[str, Any], *, filename: str | None = None,
                         wall_seconds: int = 10) -> str:
    """Byte-identical witness of a locked adaptation replay (upload==email parity)."""
    return canonical_rows_signature(
        replay_adaptation(data, template, filename=filename, wall_seconds=wall_seconds)
    )


# ---------------------------------------------------------------------------
# 22.20 -- re-analyse a flagged sample with a host-LLM-proposed adaptation.
# ---------------------------------------------------------------------------


def reanalyse_with_adaptation(
    template: dict[str, Any],
    data: bytes,
    py_source: str,
    *,
    wall_seconds: int = 10,
    preview_limit: int = 100,
) -> dict[str, Any]:
    """Run a proposed ``.py`` on a flagged sample and re-enter the validation gate (22.20).

    Returns {ok, preview (rows/rejected/logs), gate}. The host LLM authors the
    adaptation; toorow re-renders it in the isolated worker and re-checks the SAME
    required-field gate over the produced canonical rows. The caller confirms +
    locks (``lock_adaptation_template``) only on a passing gate.
    """
    preview = execute_adaptation_preview(
        py_source, data, template, wall_seconds=wall_seconds, preview_limit=preview_limit
    )
    if not preview.get("ok"):
        return {"ok": False, "error": preview.get("error"), "preview": preview, "gate": None}

    contract = template.get("contract") if "contract" in template else template
    required = list((contract or {}).get("required_fields") or [])
    present: set[str] = set()
    for row in preview.get("rows", []):
        present.update(row.keys())
    missing = [rid for rid in required if rid not in present]
    gate = {"passed": not missing, "missing_required": missing, "flagged": []}
    return {"ok": True, "preview": preview, "gate": gate}
