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
import re
from datetime import UTC, datetime
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
    gate_result: dict[str, Any],
    sample_content_hash: str,
    datastream_id: str,
    mapping_version_id: str,
    plan_version_id: str,
    sample_filename: str = "",
    accepted_warnings: list[dict[str, Any]] | None = None,
    warning_reason: str | None = None,
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
    from core.file_source_gate import GateNotPassed  # noqa: PLC0415

    if not gate_result.get("passed"):
        raise GateNotPassed("the adaptation self-test gate did not pass")
    warnings = accepted_warnings or []
    if warnings and not str(warning_reason or "").strip():
        raise ValueError("warning_reason is required when adaptation warnings are accepted")
    if re.fullmatch(r"[0-9a-f]{64}", sample_content_hash) is None:
        raise ValueError("sample_content_hash must be a lowercase sha256 digest")

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
    created = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code=template_code,
        contract=contract, created_by=created_by, label=label,
        idempotency_key=idempotency_key, host_context=host_context, trace_id=trace_id,
    )
    from core.file_source_gate import confirm_adaptation_template  # noqa: PLC0415

    evidence = {
        "template_id": created["id"],
        "template_content_hash": created["content_hash"],
        "sample_content_hash": sample_content_hash,
        "sample_filename": sample_filename,
        "mapping_version_id": mapping_version_id,
        "plan_version_id": plan_version_id,
        "actor": created_by,
        "confirmed_at": datetime.now(UTC).isoformat(),
        "resolutions": [],
        "accepted_warnings": warnings,
        "warning_reason": warning_reason,
    }
    confirmation_idempotency_key = hashlib.sha256(
        (
            f"{idempotency_key or created['content_hash']}:{created['id']}:"
            f"{datastream_id}:{mapping_version_id}:{sample_content_hash}:"
            f"{sample_filename}:{created_by}"
        ).encode("utf-8")
    ).hexdigest()
    confirmation = confirm_adaptation_template(
        conn,
        template_id=created["id"],
        project_id=project_id,
        org_id=org_id,
        datastream_id=datastream_id,
        actor=created_by,
        gate_result=gate_result,
        evidence=evidence,
        idempotency_key=confirmation_idempotency_key,
        content_hash=created["content_hash"],
        host_context=host_context,
        trace_id=trace_id,
    )
    return {**created, "confirmation": confirmation}


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
    missing_by_row = [{rid for rid in required if rid not in row} for row in rows] or [
        set(required)
    ]
    missing = [rid for rid in required if any(rid in item for item in missing_by_row)]
    return {
        "status": "needs_revalidation" if missing else "ok",
        "missing_required": missing,
        "invalid_row_indexes": [
            index for index, item in enumerate(missing_by_row) if item
        ],
    }


def produce_adaptation_rows(
    data: bytes,
    template: dict[str, Any],
    *,
    wall_seconds: int = 10,
) -> ParseResult:
    """Run a LOCKED adaptation in the isolated worker and return UNSTAMPED rows.

    This is the adaptation counterpart of ``file_source_producer.produce``: it
    parses and nothing else, so the arrival path can compose the SAME sequence for
    both template kinds -- produce, then check the variant discriminator, then
    stamp the placement. Splitting it out is what let ``run_import`` treat an
    ``adaptation`` template like any other; before, the only entry point also
    stamped, and stamping raises, so an arrival could not distinguish "this
    template declares no class" from any other failure and could not report it.

    Raises when the template is not locked, the worker returns a bounded error, or
    the output drifts (a required canonical field missing from every row).
    """
    if not template.get("content_hash"):
        raise ReshapeProducerError(
            "template_not_locked", "replay requires a locked, content-hashed template."
        )
    py_source = _adaptation_source(template)
    # The authored function is self-tested with the contract itself. Replay must
    # present that exact shape too; the storage wrapper is an internal seal, not
    # part of the adaptation API.
    contract = template.get("contract") if "contract" in template else template
    result = run_adaptation(py_source, data, contract, wall_seconds=wall_seconds)
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
    return _rows_to_parse_result(rows, result.get("rejected", []), _content_hash(data))


def replay_adaptation(
    data: bytes,
    template: dict[str, Any],
    *,
    filename: str | None = None,
    wall_seconds: int = 10,
) -> ParseResult:
    """Replay a LOCKED adaptation on ``data`` in the isolated worker (22.24).

    Runs the locked ``.py`` (never in-process), stamps the matrix placement, and
    returns canonical rows. Raises when the worker returns a bounded error or the
    output drifts (a required field missing).

    NE PAS LA RECABLER DANS LE CHEMIN D'IMPORT (mesure 2026-08-04). Elle compose
    ``produce_adaptation_rows`` PUIS ``stamp_placement``, et cet ordre est
    precisement celui que la production refuse : ``FileSourceProducer.__call__``
    (server/core/file_source_resolution.py#FileSourceProducer) rend des lignes NON
    estampillees, parce que
    ``run_import`` doit verifier le discriminant de variante AVANT de poser le
    placement -- sinon un discriminant indetectable est estampille ``null`` en
    silence au lieu d'etre signale. Sa docstring le dit en toutes lettres.

    Cette fonction est donc une SECONDE composition, ecrite pour 22.24 avant que
    la porte du discriminant n'existe. Le chemin de production la supersede et la
    parite upload/e-mail qu'elle devait temoigner est prouvee ailleurs : les deux
    portes convergent sur ``run_import`` (ledger file-source ``[6]``), et
    ``test_replay_is_byte_identical_upload_equals_email`` la mesure desormais sur
    le seam de production, plus sur cette fonction. Ce qui la garde ici est son
    role de refus (template non verrouille, derive de sortie), teste et utile.
    """
    parse = produce_adaptation_rows(data, template, wall_seconds=wall_seconds)
    return stamp_placement(parse, template, filename=filename)


def adaptation_signature(data: bytes, template: dict[str, Any], *, filename: str | None = None,
                         wall_seconds: int = 10) -> str:
    """Byte-identical witness of a locked adaptation replay (upload==email parity).

    SUPERSEDEE comme temoin de parite, pour la meme raison que
    ``replay_adaptation`` ci-dessus : elle temoigne d'une composition que la
    production n'emprunte pas. Et le test qui s'en servait etait `f(x) == f(x)` --
    deux appels a LA MEME fonction sur LES MEMES octets, ne variant qu'un nom de
    fichier que le template ne lit pas ; il serait reste vert le jour ou les deux
    portes auraient diverge. La parite se mesure desormais sur
    ``FileSourceProducer.__call__``, le seam ou les deux portes convergent
    reellement.
    """
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

    "THE SAME GATE" IS NOW TRUE. This function used to build its verdict by hand:
    a union of the produced rows' keys, ``flagged`` hardcoded to ``[]``, and
    ``passed = not missing``. The sentence above already claimed it re-entered the
    same gate, and it did not — it was a second, weaker scorer, which is exactly
    what 22.15 claimed to have avoided. Consequences, both real: a low-confidence
    binding could never be flagged on this path, and the verdict was missing
    ``ambiguities`` entirely, so a caller reading the Warning level of the
    ratified vocabulary raised here and nowhere else.

    ``mapping=None`` is deliberate and is the gate's documented RESHAPE path: an
    adaptation owns its own projection, so there is no source->canonical mapping
    to invert and the produced columns are the only truthful subject.
    """
    preview = execute_adaptation_preview(
        py_source, data, template, wall_seconds=wall_seconds, preview_limit=preview_limit
    )
    if not preview.get("ok"):
        return {"ok": False, "error": preview.get("error"), "preview": preview, "gate": None}

    from core.file_source_gate import evaluate_landing_gate  # noqa: PLC0415

    rows = list(preview.get("rows") or [])
    columns = sorted({str(key) for row in rows for key in row})
    gate = evaluate_landing_gate(template, None, columns=columns, rows=rows)
    return {"ok": True, "preview": preview, "gate": gate}
