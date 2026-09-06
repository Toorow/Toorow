"""Stories 38.16 / 38.17 -- Repair a mapping AT THE MOMENT OF FAILURE.

WHAT THIS MODULE IS, AND THE LARGER PART IT DELIBERATELY IS NOT.

The mapping-candidate engine already exists and is governed: a proposal is
frozen against exact active pointers by ``datastream_change.prepare_change``,
and it commits through ``confirm_change`` inside ``execute_operation`` -- audit
and outbox with the effect, AD-27 confirmation, no pointer moved by autosave.
Rebuilding any of that here would be the fourth engine this epic exists to avoid.

What was missing is the ENTRY. Story 38.16 AC1 asks that, FROM A RAW-IMPORT
ISSUE, the user sees the source columns beside the pinned template fields and
the current mapping version. There was no path from "this file failed" to that
screen: the failed file's columns existed only inside a parser call that had
already raised. So this module answers one question -- "what did the file
actually contain, and what is it being mapped against right now?" -- and hands
back the governed path to change it.

SAMPLES ARE MASKED, ALWAYS, ON BOTH CHANNELS. Story 38.16 AC5 allows raw values
to be "minimally disclosed" to a human and 38.10 AC5 forbids raw rows over MCP.
Two masking policies would mean the safe one is the one somebody forgets, so
there is one: every sample is reduced to a shape -- a leading fragment and a
length -- which is enough to recognise "this column is dates in the wrong format"
and not enough to read a respondent's answer.

THE FILE IS RE-PARSED, NOT RE-INTERPRETED. Reading the columns costs one bounded
parse of retained bytes; nothing is written, no candidate is created, no pointer
moves. A context read that mutated anything would be a trap on a screen an
operator opens precisely because something already went wrong.

The CALLER owns the transaction. ASCII-only source (AI-03).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How many rows of the retained file are looked at for samples. Small on
#: purpose: this is recognition, not analysis.
_SAMPLE_ROWS = 3

#: How much of a value survives masking.
_MASK_KEEP = 2

# Stable reasons a repair context cannot be built.
CONTEXT_NOT_FOUND = "raw_import_not_found"
CONTEXT_NO_BYTES = "retained_bytes_unavailable"
CONTEXT_UNPARSEABLE = "file_not_parseable"
CONTEXT_DATASTREAM_NOT_ACTIVE = "datastream_not_active"


def mask_sample(value: Any) -> str | None:
    """Reduce a cell to a recognisable SHAPE, never a readable value.

    ``"2026-01-15"`` becomes ``"20********(10)"``: enough to see that a column
    holds dates and how long they are, not enough to read anyone's data. None
    stays None, because "this column is empty here" is itself the diagnosis
    half the time.
    """
    if value is None:
        return None
    text = str(value)
    if not text:
        return ""
    keep = text[:_MASK_KEEP]
    hidden = max(0, len(text) - _MASK_KEEP)
    return f"{keep}{'*' * min(hidden, 12)}({len(text)})"


def _governed_path(project_id: str, datastream_id: str) -> dict[str, Any]:
    """Where a repair is actually made -- named, not reimplemented.

    Points at the existing governed change path rather than exposing a write
    here. If those route names move, this reference is wrong in one place and
    obviously so, which is better than a second write path that stays right.
    """
    return {
        "engine": "datastream_change",
        # LES DEUX ADRESSES RENDAIENT 404. Il leur manquait le prefixe
        # `/api/projects/{project_id}` -- que cette fonction avait deja sous la
        # main -- et le confirm ne vit pas sous `mapping/` mais directement sous
        # `workbench/changes/{preparation_id}/confirm`. La garde etait un
        # `in` sur une sous-chaine, donc elle restait verte sur deux URLs mortes.
        # Elle interroge desormais le routeur reel.
        "prepare": (
            f"POST /api/projects/{project_id}/datastreams/{datastream_id}"
            f"/workbench/mapping/changes"
        ),
        "confirm": (
            f"POST /api/projects/{project_id}/datastreams/{datastream_id}"
            f"/workbench/changes/{{preparation_id}}/confirm"
        ),
        # C-1 : DEUX ETAPES, DEUX MOTEURS -- et ce module affirmait que le
        # premier couvrait les deux. `datastream_change.confirm_change` passe
        # `advance_pointer=False` et rend `active_versions_unchanged: True` ; le
        # test de gouvernance du depot ecrit la regle noir sur blanc --
        # << appending a version must not activate it; activation belongs to the
        # publication step >>. Donc l'AC << publier change la version courante >>
        # ne pouvait se produire nulle part dans le moteur nomme ici.
        #
        # Le moteur qui avance le pointeur est `governed_publication`
        # (`_advance_pointer`), monte sous `/api/governance/publication-reviews`.
        # Le nommer n'ajoute pas un quatrieme moteur : il en existait deja deux,
        # et le chemin entrant n'etait relie a aucun.
        "publish": (
            "POST /api/governance/publication-reviews"
            " puis .../{confirmation_id}/confirm"
        ),
        "publish_engine": "governed_publication",
        "kind": "mapping",
        "notes": (
            "The candidate is frozen against the exact active plan and mapping "
            "pointers; confirmation commits state, audit and outbox together. "
            "Nothing in `datastream_change` advances a pointer -- that is what "
            "`publish` is for, and the two steps are ordered: propose, then "
            "publish."
        ),
    }


def get_mapping_repair_context(
    conn,
    *,
    raw_import_id: str,
    datastream_id: str,
    store=None,  # noqa: ANN001
) -> dict[str, Any]:
    """What the file contained, what it is mapped against, and how to change it.

    Never raises for an unavailable answer -- a screen opened because something
    failed must not fail again -- and never writes.
    """
    from core.inbound_raw_imports import get_raw_import  # noqa: PLC0415

    raw = get_raw_import(conn, raw_import_id=raw_import_id, datastream_id=datastream_id)
    if raw is None:
        return {
            "available": False,
            "reason": CONTEXT_NOT_FOUND,
            "raw_import_id": raw_import_id,
            "datastream_id": datastream_id,
        }

    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_plan_version_id, current_mapping_version_id, "
            "       lifecycle_state, project_id "
            "FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    plan_v, mapping_v, lifecycle, project_id = row if row else (None, None, None, None)

    base: dict[str, Any] = {
        "available": True,
        "reason": None,
        "raw_import_id": raw_import_id,
        "datastream_id": datastream_id,
        "project_id": project_id,
        # The failure being repaired, echoed so the screen states its own cause.
        "raw_import": {
            "state": raw.get("state"),
            "error_code": raw.get("error_code"),
            "filename": raw.get("filename"),
            "media_type_detected": raw.get("media_type_detected"),
            "content_hash": raw.get("content_hash"),
            "scan_verdict": raw.get("scan_verdict"),
        },
        # The versions the candidate will be frozen against (38.16 AC2/AC4).
        "pinned_versions": {
            "plan_version_id": plan_v,
            "mapping_version_id": mapping_v,
        },
        "governed_path": _governed_path(project_id, datastream_id),
        "source_columns": [],
    }

    if lifecycle != "active":
        # `prepare_change` requires an Active Datastream with exact pointers.
        # Saying so here beats letting the operator compose a repair the next
        # call will refuse.
        base["available"] = False
        base["reason"] = CONTEXT_DATASTREAM_NOT_ACTIVE
        return base

    uri = raw.get("quarantine_uri")
    if not uri:
        base["available"] = False
        base["reason"] = CONTEXT_NO_BYTES
        return base

    try:
        if store is None:
            from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415

            store = open_quarantine_store()
        data = store.get(uri)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "inbound_mapping_entry: retained bytes unreadable for %s: %s",
            raw_import_id,
            type(exc).__name__,
        )
        base["available"] = False
        base["reason"] = CONTEXT_NO_BYTES
        return base

    columns, parse_error = _read_source_columns(data, raw)
    if columns is None:
        # A file the parser cannot open has no columns to show, and that IS the
        # diagnosis -- it is not an error in this read.
        base["available"] = False
        base["reason"] = parse_error or CONTEXT_UNPARSEABLE
        return base

    base["source_columns"] = columns
    base["preview"] = _preview_on_the_retained_bytes(
        conn,
        data,
        project_id=project_id,
        datastream_id=datastream_id,
        mapping_version_id=mapping_v,
        filename=raw.get("filename"),
    )
    return base


#: The seven readings 38.16 AC3 asks a preview to report. Named here so the
#: shape is a contract and not whatever the composer happened to return.
PREVIEW_READINGS: tuple[str, ...] = (
    "fields",          # required-field coverage, per column
    "gate",            # the landing verdict -- the SAME one the import uses
    "coercions",       # date / timezone / number coercions the file needs
    "units",           # currency and unit declarations
    "placement",       # identity and grain, as the matrix class
    "classification",  # sensitive classification
    "row_validation",  # row-level validation summary
)

#: Why no preview could be produced. Each is a REASON, never an empty preview:
#: an absent template and an unparseable file are different facts, and a screen
#: that shows "0 issues" for both sends an operator to the wrong repair.
PREVIEW_NO_TEMPLATE = "no_file_source_template"
PREVIEW_UNAVAILABLE = "preview_unavailable"


def _preview_on_the_retained_bytes(
    conn,
    data: bytes,
    *,
    project_id: str | None,
    datastream_id: str,
    mapping_version_id: str | None,
    filename: str | None,
) -> dict[str, Any]:
    """The AC3 readings, computed on the file ALREADY HELD in quarantine.

    THE RATIFIED LINE THIS CLOSES. `docs/product-architecture/file-source-ingestion.md:203`
    lists, among the conditions that leave this surface incomplete:

        "the same file yields different results by upload and by email"

    That was the state. The seven readings AC3 asks for -- coverage, coercions,
    date/timezone, currency/unit, identity/grain, sensitive classification and
    row-level validation -- were delivered on `build_file_source_preview`, whose
    only two callers are the UPLOAD path (`admin_api` and
    `file_source_template_api`), and the second of those REQUIRES `file_base64`.
    An operator repairing an emailed file was therefore asked to re-upload the
    very bytes the platform had already retained -- while 38.18 exists precisely
    to "recover without provider resend".

    Measured before this function existed:
        grep -c '"coercions"\\|"row_validation"\\|"units"\\|"classification"\\|"gate"' \\
            server/core/inbound_mapping_entry.py
        -> 0

    THE SAME COMPOSER, NOT A SECOND ONE. This calls
    `build_file_source_preview` -- the function the upload path calls -- so the
    two doors cannot disagree about the same bytes. A second implementation here
    would be the defect the ratified line names, rebuilt in the place that was
    supposed to repair it.

    Never raises: this runs on a file that already failed once, and a screen
    opened because something went wrong must not fail again.
    """
    if not project_id:
        return {"available": False, "reason": PREVIEW_UNAVAILABLE}
    try:
        from core.csv_excel_import import build_file_source_preview  # noqa: PLC0415

        preview = build_file_source_preview(
            conn,
            data,
            project_id=project_id,
            datastream_id=datastream_id,
            mapping_version_id=mapping_version_id,
            filename=filename,
        )
    except Exception as exc:  # noqa: BLE001 -- a preview never fails this read
        logger.warning(
            "inbound_mapping_entry: preview unavailable for %s: %s",
            datastream_id,
            type(exc).__name__,
        )
        return {"available": False, "reason": PREVIEW_UNAVAILABLE}

    if preview is None:
        # NO TEMPLATE IS A FACT, NOT AN EMPTY PREVIEW. `build_file_source_preview`
        # returns None when the Datastream carries no `fst_` binding, and
        # fabricating a green preview there would be inventing a verdict for a
        # contract nobody declared.
        return {"available": False, "reason": PREVIEW_NO_TEMPLATE}

    out: dict[str, Any] = {
        "available": True,
        "reason": None,
        "blocked": bool(preview.get("blocked")),
        "blocked_reason": preview.get("reason"),
    }
    for reading in PREVIEW_READINGS:
        out[reading] = preview.get(reading)
    return out


def _read_source_columns(data: bytes, raw: dict):
    """Parse enough to name columns while retaining a typed refusal code.

    Returns None when nothing can parse the bytes. Every parser failure is
    caught: this read runs on a file that already failed once, so raising here
    would replace one diagnosis with another.
    """
    from core.csv_excel_import import (  # noqa: PLC0415
        FORMAT_CSV,
        FORMAT_SAV,
        CsvExcelImportError,
        detect_format,
        parse_csv,
        parse_excel,
    )

    try:
        fmt = detect_format(raw.get("filename"), data)
        if fmt == FORMAT_CSV:
            result = parse_csv(data, max_rows=50)
        elif fmt == FORMAT_SAV:
            from core.inbound_sav import parse_sav  # noqa: PLC0415

            result = parse_sav(data, max_rows=50)
        else:
            result = parse_excel(data, max_rows=50)
    except CsvExcelImportError as exc:
        logger.debug("inbound_mapping_entry: tabular parse refused: %s", exc.code)
        return None, exc.code
    except Exception as exc:  # noqa: BLE001 -- an unparseable file IS the answer
        logger.debug("inbound_mapping_entry: tabular parse failed: %s", type(exc).__name__)
        return None, CONTEXT_UNPARSEABLE

    rows = getattr(result, "rows", []) or []
    labels = (getattr(result, "metadata", {}) or {}).get("variable_labels", {})

    return [
        {
            "name": column.name,
            "index": column.index,
            "detected_type": column.detected_type,
            "null_count": column.null_count,
            # Evidence from the file, offered to a human -- never auto-bound
            # (Story 38.12 decision 1, and the same rule for any format).
            "source_label": labels.get(column.name),
            "masked_samples": [mask_sample(row.get(column.name)) for row in rows[:_SAMPLE_ROWS]],
        }
        for column in (getattr(result, "columns", []) or [])
    ], None
