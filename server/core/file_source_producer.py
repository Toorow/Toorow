"""toorow -- Canonical-rows producer: reshape a workbook to DAILY rows (Story 22.10).

Phase B-0 of the file-source ingestion framework. Consumes the shared reshape
engine (``core.reshape``, Story 22.9 / AD-5) to turn a real, messy media-plan
workbook -- a top ``label: value`` metadata block, a header that is NOT row 1,
grouped lines with subtotal/total rows, merged cells, and per-line Start/Ende
date ranges -- into standardized DAILY canonical rows, output as the existing
``ParseResult`` (Story 12.9). It is the reshape core of the B-1 producer; the
catalog/template wiring at ``csv_excel_import.run_import`` is Story 22.12.

Architecture boundaries (AD-4 / AD-5):
  * ONE Excel engine: merged-cell resolution, header-row column resolution, and
    the cent-exact daily spread come from ``core.reshape`` -- this module never
    reimplements them, and no second Excel parser is introduced.
  * Ingestion-only: output is canonical RAW rows at the DAILY grain (+ rejected
    rows). No metric, FX, money composition, or analytics -- those stay in dbt.
  * No silent drop: group-label / subtotal / total / dateless rows become honest
    ``RejectedRow`` evidence with a stable rule code, never dropped in silence.

Sum-preserving explode (AD-5): each accepted line's amount is spread across its
[start, end] date range by ``reshape.compute_spread`` (largest-remainder at the
cent), so ``SUM(daily amounts) == line total`` EXACTLY, to the cent. The producer
asserts this per line defensively (a mismatch is a bug, surfaced loudly).

The producer is a PURE parse layer (no I/O, unit-testable on in-memory bytes),
mirroring ``csv_excel_import``'s parse layer. Landing / ledger / publication is
the caller's concern (Story 22.12 reuses ``managed_feed_ledger``).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from core.csv_excel_import import (
    ColumnSpec,
    CsvExcelImportError,
    ParseResult,
    RejectedRow,
)
from core.file_source_template import (
    PLACEMENT_CLASSES,
    FileSourceTemplateError,
    compute_content_hash,
    validate_template_contract,
)
from core.reshape import CENT, cell_resolved, compute_spread, resolve_column_index, resolve_merges

# Reserved per-row key stamping the matrix class (planned/actual/extrapolated).
# Underscore-prefixed so it never collides with a canonical mdm_ field id.
PLACEMENT_CLASS_KEY = "_placement_class"

# The two grains ONE engine emits. 'daily' spreads a line across its date range
# (right for a KPI export); 'line' keeps the line and its span (right for a plan,
# which the plan store spreads again at publish).
GRAIN_DAILY = "daily"
GRAIN_LINE = "line"
GRAINS = frozenset({GRAIN_DAILY, GRAIN_LINE})

# ---------------------------------------------------------------------------
# DoS guards (bound the sheet BEFORE any per-cell loop; same spirit as the
# csv_excel_import / mediaplan_import limits).
# ---------------------------------------------------------------------------

MAX_SHEET_ROWS = 500_000
MAX_SHEET_COLUMNS = 500

# How far down we scan for a header row when it is auto-detected.
HEADER_SCAN_LIMIT = 200

# ---------------------------------------------------------------------------
# Per-row rejection rule codes (stable; carried on RejectedRow).
# ---------------------------------------------------------------------------

RULE_SUBTOTAL_OR_TOTAL = "subtotal_or_total"  # a group subtotal / grand total row
RULE_GROUP_OR_NOISE = "group_or_noise"  # a group-label / blank row (no amount, no dates)
RULE_MISSING_DATES = "missing_dates"  # an amount is present but the date range is absent
RULE_BAD_AMOUNT = "bad_amount"  # the amount cell is non-numeric where a line is expected
RULE_INVERTED_DATES = "inverted_dates"  # start_date > end_date
# A row covered by a merged AMOUNT range that is not its anchor: the money of that
# range landed once, on the anchor row. Never a second time here.
RULE_MERGED_AMOUNT_CARRIED = "merged_amount_carried"


class ReshapeProducerError(CsvExcelImportError):
    """A workbook could not be reshaped (unreadable / no header / no data) -> 422.

    Subclasses the shared import error so the API maps it like every other
    import-layer failure (stable ``code`` + optional ``repair``).
    """

    def __init__(self, code: str, detail: str = "", *, repair: dict | None = None) -> None:
        super().__init__(code, detail, repair=repair)


# ---------------------------------------------------------------------------
# Reshape spec (what the producer needs to reshape one sheet to daily rows).
#
# Story 22.10 keeps the spec explicit/declared -- tolerant recognition (B-2) and
# the versioned template artifact (B-1) resolve this spec for the caller later.
# ---------------------------------------------------------------------------


@dataclass
class ReshapeSpec:
    """Declares how to reshape one sheet's lines into daily canonical rows.

    ``fields`` maps canonical field id -> source column key (a header cell text,
    matched case-insensitively, or an Excel column letter as a fallback). It MUST
    include the amount, start, and end columns (named by ``amount_field`` /
    ``start_field`` / ``end_field``). On output the start/end columns are consumed
    (replaced by the single ``date_field`` grain); every other mapped field is
    carried onto each daily row, and ``amount_field`` carries the DAILY amount.
    """

    fields: dict[str, str]
    amount_field: str
    start_field: str
    end_field: str
    date_field: str = "date"
    label_field: str | None = None  # canonical id used to detect subtotal/total/group rows
    sheet_name: str | None = None  # None -> the active/first sheet
    header_row: int | None = None  # 1-based; None -> auto-detect by declared headers
    metadata_rows: tuple[int, int] | None = None  # 1-based inclusive top-block range
    date_format: str | None = None  # e.g. "%d.%m.%Y" for German plans
    total_markers: tuple[str, ...] = ("total",)  # label substrings -> subtotal/total skip

    # --- The plan-line grain capabilities (chantier 67-25b) -----------------
    # Declared here so ONE spec describes both grains; executed by
    # ``core.file_source_plan_lines``. Additive with defaults, so every daily
    # template built before the convergence keeps its exact behaviour.
    #
    #: 'daily' -- spread each line across its [start, end] range (the default);
    #: 'line'  -- emit one row per plan line (span + total), never spread. The
    #: plan store spreads again at publish, so a daily output would duplicate
    #: that work and lose the line's span.
    grain: str = "daily"
    #: 'auto' -> compose ``<sheet>/<label>``; else the source column carrying it.
    line_key: str = "auto"
    #: Several sheets of ONE workbook in ONE import, walked in declared order
    #: with the row-identity registry SHARED across them. Empty -> ``sheet_name``.
    sheets: tuple[str, ...] = ()
    #: {<physical A1 range>: [weights]} -- the EXPLICIT split of a merged amount.
    #: Keyed by the merge's physical coordinates so a shifted merge stops applying
    #: rather than splitting on a stale key. Weights must sum EXACTLY (else 422).
    merged_amount_explode: dict[str, list[Any]] | None = None


# ---------------------------------------------------------------------------
# Value coercion (dates / amounts).
# ---------------------------------------------------------------------------


def _parse_date(value: Any, *, date_format: str | None) -> date | None:
    """Coerce a cell to a date, or None if empty/unparseable.

    Accepts native datetime/date (openpyxl returns datetime for date cells), an
    optional declared format (e.g. %d.%m.%Y), then a few common fallbacks
    (ISO, German dotted). Returns None on empty so the caller decides noise.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    formats = [date_format] if date_format else []
    formats += ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y%m%d"]
    for fmt in formats:
        if not fmt:
            continue
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_amount(value: Any) -> Decimal | None:
    """Coerce an amount cell to a cent-quantised Decimal, or None if empty.

    Handles native numbers (incl. high-precision floats like a real agency plan's
    ``675622.4950015477``) and FR/EN-formatted strings ("1 234,56" / "1,234.56"
    with regular, NBSP, or thin-NBSP thousands separators). A non-empty but
    non-numeric cell returns None (the caller treats it as "no numeric amount").
    Quantised to the cent (ROUND_HALF_UP) so the sum-preserving invariant holds
    against a cent-exact line total.
    """
    if value is None or isinstance(value, bool):  # bool is an int subclass; never money
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = "".join(ch for ch in text if not ch.isspace())
    for token in ("\N{EURO SIGN}", "EUR", "eur", "$"):
        cleaned = cleaned.replace(token, "")
    if "," in cleaned and "." in cleaned:
        # The LAST separator is the decimal point.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")  # FR 1.234,56
        else:
            cleaned = cleaned.replace(",", "")  # EN 1,234.56
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not parsed.is_finite():
        return None
    return parsed.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Workbook / sheet helpers.
# ---------------------------------------------------------------------------


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_sheet(data: bytes, sheet_name: str | None) -> Any:
    """Load one worksheet from xlsx bytes (read_only=False so merges resolve)."""
    try:
        from openpyxl import load_workbook  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ReshapeProducerError(
            "openpyxl_unavailable", "openpyxl is required for Excel reshape."
        ) from exc
    try:
        # read_only=False so merged_cells ranges are available; data_only=True so
        # formula cells yield their last computed value, not the formula string.
        wb = load_workbook(io.BytesIO(data), read_only=False, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ReshapeProducerError(
            "unreadable_workbook",
            "The bytes are not a valid Excel (.xlsx) workbook.",
            repair={"export_values_only_xlsx": True},
        ) from exc
    if sheet_name is not None:
        if sheet_name not in wb.sheetnames:
            raise ReshapeProducerError(
                "sheet_not_found",
                f"Sheet '{sheet_name}' not found.",
                repair={"pick_available_sheet": list(wb.sheetnames)},
            )
        return wb[sheet_name]
    ws = wb.active
    if ws is None:  # pragma: no cover - a loaded workbook always has an active sheet
        raise ReshapeProducerError("no_active_sheet", "Workbook has no active sheet.")
    return ws


def _detect_header_row(ws: Any, spec: ReshapeSpec) -> int:
    """Return the 1-based header row, explicit or auto-detected by declared headers.

    Auto-detection (spec.header_row is None): the first row (within
    HEADER_SCAN_LIMIT) on which EVERY declared column key resolves via the shared
    header resolver. Raises when no such row exists (the spec cannot bind).
    """
    if spec.header_row is not None:
        return spec.header_row
    keys = list(spec.fields.values())
    max_row = ws.max_row or 0
    scan_to = min(max_row, HEADER_SCAN_LIMIT)
    for candidate in range(1, scan_to + 1):
        if all(resolve_column_index(ws, candidate, key) is not None for key in keys):
            return candidate
    raise ReshapeProducerError(
        "no_header_row",
        "Could not locate a header row where every declared column is present. "
        "Declare an explicit header_row or check the column names.",
        repair={"declare_header_row": True},
    )


def _extract_metadata(
    ws: Any, merges: dict, rows: tuple[int, int]
) -> dict[str, Any]:
    """Extract the top ``label: value`` block over a 1-based inclusive row range.

    Each non-empty row contributes {first non-empty cell as label -> next
    non-empty cell as value}. Cells are merge-resolved. Rows with a single value
    (or none) are skipped. Interpreting the values (dates, budgets) is deferred.
    """
    start, end = rows
    out: dict[str, Any] = {}
    max_col = ws.max_column or 0
    for r in range(start, end + 1):
        values: list[str] = []
        for c in range(1, max_col + 1):
            raw, _ = cell_resolved(ws, merges, r, c)
            if raw is None:
                continue
            text = str(raw).strip()
            if text:
                values.append(text)
            if len(values) >= 2:
                break
        if len(values) >= 2:
            label = values[0].rstrip(":").strip()
            if label and label not in out:
                out[label] = values[1]
    return out


# ---------------------------------------------------------------------------
# Producer entrypoint.
# ---------------------------------------------------------------------------


def _source_amount_total(
    ws: Any,
    merges: dict,
    *,
    amount_col: int,
    first_data_row: int,
    last_row: int,
) -> Decimal:
    """Sum the amount column of the data zone by an INDEPENDENT pass.

    Computed OUTSIDE the reshape walk so it can never tautologically equal the
    landed total: that is the whole point of the invariant. Each distinct merged
    range counts ONCE (its top-left value); each unmerged numeric cell counts
    once. Rows the walk will later reject (subtotals, totals, dateless lines) ARE
    counted -- honesty over the whole file -- and the invariant then reads
    ``file == landed + rejected``.
    """
    total = Decimal("0.00")
    seen: set[str] = set()
    for row in range(first_data_row, last_row + 1):
        raw, amount_range = cell_resolved(ws, merges, row, amount_col)
        if amount_range is not None:
            if amount_range in seen:
                continue  # a merged amount counts ONCE, never per covered row
            seen.add(amount_range)
        amount = _parse_amount(raw)
        if amount is not None:
            total += amount
    return total


def reshape_workbook_to_daily(
    data: bytes,
    spec: ReshapeSpec,
    *,
    sample_only: bool = False,
    sample_line_limit: int = 100,
) -> ParseResult:
    """Reshape one sheet's lines into DAILY canonical rows -> ``ParseResult``.

    Steps (all via the shared ``core.reshape`` engine):
      1. load the sheet, extract the declared top metadata block;
      2. detect the header row (explicit or auto by declared headers);
      3. resolve the declared columns + every merged cell;
      4. walk data rows: skip subtotal/total (label markers) and group/blank rows
         as honest ``RejectedRow`` evidence -- never a silent drop;
      5. explode each valid line's [start, end] range into daily rows whose
         ``amount_field`` values sum back to the line total EXACTLY (cent-exact).

    ``sample_only`` bounds the number of SOURCE lines processed (preview/gate).
    Raises ``ReshapeProducerError`` on structural failure (unreadable, no header,
    no data, missing declared columns, or a broken sum invariant).
    """
    if not data:
        raise ReshapeProducerError("empty_file", "The uploaded file is empty.")

    content_hash = _content_hash(data)
    ws = _load_sheet(data, spec.sheet_name)

    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    if max_row > MAX_SHEET_ROWS or max_col > MAX_SHEET_COLUMNS:
        raise ReshapeProducerError(
            "sheet_too_large",
            f"Sheet is too large ({max_row}x{max_col}); "
            f"maximum {MAX_SHEET_ROWS}x{MAX_SHEET_COLUMNS}.",
        )

    merges = resolve_merges(ws)
    metadata = (
        _extract_metadata(ws, merges, spec.metadata_rows) if spec.metadata_rows else {}
    )

    header_row = _detect_header_row(ws, spec)

    # Resolve every declared field to a 1-based column index (once).
    field_col: dict[str, int] = {}
    for canonical_id, key in spec.fields.items():
        idx = resolve_column_index(ws, header_row, key)
        if idx is None:
            raise ReshapeProducerError(
                "column_not_found",
                f"Declared column '{key}' (field '{canonical_id}') not found on "
                f"header row {header_row}.",
                repair={"check_column_name_or_letter": key},
            )
        field_col[canonical_id] = idx

    for required in (spec.amount_field, spec.start_field, spec.end_field):
        if required not in field_col:
            raise ReshapeProducerError(
                "missing_required_column",
                f"The spec must map the '{required}' column.",
            )

    # Descriptive fields carried verbatim onto each daily row (everything mapped
    # except the two date-range columns, which collapse into the daily grain).
    carried_fields = [
        cid for cid in spec.fields if cid not in (spec.start_field, spec.end_field)
    ]
    # Label default: the first DESCRIPTIVE carried field (never the amount column,
    # so subtotal/total detection reads a name, not a number).
    _descriptive = [cid for cid in carried_fields if cid != spec.amount_field]
    label_id = spec.label_field or (_descriptive[0] if _descriptive else None)
    markers = tuple(m.strip().casefold() for m in spec.total_markers if m.strip())

    rows_out: list[dict[str, Any]] = []
    rejected: list[RejectedRow] = []
    source_lines_seen = 0
    # Money carried by rows the walk rejects, so the invariant can read
    # ``file == landed + rejected`` instead of silently losing the difference.
    rejected_amount = Decimal("0.00")
    # Anchor row of every merged AMOUNT range already counted (range_id -> row).
    amount_anchor: dict[str, int] = {}

    first_data_row = header_row + 1
    last_row = ws.max_row or header_row
    for r in range(first_data_row, last_row + 1):
        # A fully-empty row is skipped silently (spacer), not counted as a line.
        if _row_is_blank(ws, merges, r, max_col):
            continue
        if sample_only and source_lines_seen >= sample_line_limit:
            break
        source_lines_seen += 1

        label = _read_text(ws, merges, field_col, label_id, r) if label_id else ""

        # A merged AMOUNT cell carries ONE amount for the whole range. Money is
        # never forward-filled: the anchor row (the first row of the range we
        # reach) carries it, every other covered row carries none. Forward-fill
        # stays correct for DESCRIPTIVE cells and is a multiplication for money --
        # the distinction is taken on the field, and the amount field is declared.
        raw_amount, amount_range = cell_resolved(ws, merges, r, field_col[spec.amount_field])
        if amount_range is not None:
            anchor = amount_anchor.get(amount_range)
            if anchor is not None:
                rejected.append(
                    RejectedRow(
                        row_number=r, field_name=spec.amount_field,
                        rule=RULE_MERGED_AMOUNT_CARRIED,
                        reason=(
                            f"row covered by merged amount range {amount_range}; its "
                            f"amount landed once on anchor row {anchor}"
                        ),
                        rejected_value=_truncate(label),
                    )
                )
                continue
            amount_anchor[amount_range] = r

        # Subtotal / grand-total row -> skip (kept as evidence, may validate later).
        if label and any(m in label.casefold() for m in markers):
            carried = _parse_amount(raw_amount)
            if carried is not None:
                rejected_amount += carried
            rejected.append(
                RejectedRow(
                    row_number=r, field_name=label_id or "",
                    rule=RULE_SUBTOTAL_OR_TOTAL,
                    reason=f"subtotal/total row skipped for landing: '{label}'",
                    rejected_value=_truncate(label),
                )
            )
            continue

        amount = _parse_amount(raw_amount)
        start = _parse_date(
            cell_resolved(ws, merges, r, field_col[spec.start_field])[0],
            date_format=spec.date_format,
        )
        end = _parse_date(
            cell_resolved(ws, merges, r, field_col[spec.end_field])[0],
            date_format=spec.date_format,
        )

        # Group-label / blank-ish row: no amount AND no dates -> noise.
        if amount is None and start is None and end is None:
            rejected.append(
                RejectedRow(
                    row_number=r, field_name=label_id or "",
                    rule=RULE_GROUP_OR_NOISE,
                    reason="group-label or empty row (no amount, no dates)",
                    rejected_value=_truncate(label),
                )
            )
            continue

        # An amount with no valid date range cannot be placed at the daily grain.
        if amount is not None and (start is None or end is None):
            # A MERGED amount cannot simply be dropped: its range carries the money
            # of several sub-rows, and rejecting it here would lose the difference
            # silently. Refuse, naming the cell that has to be repaired.
            if amount_range is not None:
                raise ReshapeProducerError(
                    "merged_amount_not_placeable",
                    f"Merged amount range {amount_range} (anchor row {r}) carries "
                    f"{format(amount, 'f')} but has no valid start/end date range, so "
                    "it can be neither landed once nor spread; the import is refused "
                    "rather than losing the amount.",
                    repair={"merged_amount_range": amount_range, "anchor_row": r},
                )
            rejected_amount += amount
            rejected.append(
                RejectedRow(
                    row_number=r, field_name=spec.start_field,
                    rule=RULE_MISSING_DATES,
                    reason="line has an amount but no valid start/end date range",
                    rejected_value=_truncate(label),
                )
            )
            continue

        # Dates present but no numeric amount: a line we cannot spend-spread.
        if amount is None:
            rejected.append(
                RejectedRow(
                    row_number=r, field_name=spec.amount_field,
                    rule=RULE_BAD_AMOUNT,
                    reason="line has a date range but no numeric amount",
                    rejected_value=_truncate(str(raw_amount)),
                )
            )
            continue

        if start > end:  # type: ignore[operator]
            if amount_range is not None:
                raise ReshapeProducerError(
                    "merged_amount_not_placeable",
                    f"Merged amount range {amount_range} (anchor row {r}) carries "
                    f"{format(amount, 'f')} but its date range {start}..{end} is "
                    "inverted, so it can be neither landed once nor spread; the "
                    "import is refused rather than losing the amount.",
                    repair={"merged_amount_range": amount_range, "anchor_row": r},
                )
            rejected_amount += amount
            rejected.append(
                RejectedRow(
                    row_number=r, field_name=spec.start_field,
                    rule=RULE_INVERTED_DATES,
                    reason=f"start date {start} is after end date {end}",
                    rejected_value=_truncate(f"{start}..{end}"),
                )
            )
            continue

        # Carry the descriptive fields once (the same for every day of this line).
        base: dict[str, Any] = {}
        for cid in carried_fields:
            if cid == spec.amount_field:
                continue  # amount is per-day, filled below
            base[cid] = _read_text(ws, merges, field_col, cid, r) or None

        allocations = compute_spread(amount, start, end)  # cent-exact, sum-preserving
        total = sum((amt for _d, amt in allocations), Decimal("0"))
        if total != amount:  # defensive: compute_spread guarantees this
            raise ReshapeProducerError(
                "spread_invariant_broken",
                f"Row {r}: daily spread {total} != line total {amount}.",
            )
        for day, amt in allocations:
            rows_out.append(
                {**base, spec.date_field: day.isoformat(), spec.amount_field: format(amt, "f")}
            )

    if not rows_out and not rejected:
        raise ReshapeProducerError(
            "no_data_rows",
            f"No data rows found below header row {header_row}.",
        )

    columns = _synthesise_columns(spec, carried_fields)
    # The file-side total is measured only on a FULL pass: under ``sample_only`` the
    # walk sees a bounded slice of the lines while an independent pass would read
    # the whole column, and comparing the two would refuse every preview.
    file_total: Decimal | None = None
    if not sample_only:
        file_total = _source_amount_total(
            ws, merges,
            amount_col=field_col[spec.amount_field],
            first_data_row=first_data_row,
            last_row=last_row,
        )
    return ParseResult(
        rows=rows_out,
        rejected=rejected,
        columns=columns,
        encoding="xlsx",
        delimiter=None,
        sheet_name=ws.title,
        detected_row_count=source_lines_seen,
        content_hash=content_hash,
        metadata=metadata,
        source_amount_total=None if file_total is None else format(file_total, "f"),
        rejected_amount_total=None if file_total is None else format(rejected_amount, "f"),
    )


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _row_is_blank(ws: Any, merges: dict, row: int, max_col: int) -> bool:
    for c in range(1, max_col + 1):
        raw, _ = cell_resolved(ws, merges, row, c)
        if raw is not None and str(raw).strip() != "":
            return False
    return True


def _read_text(ws: Any, merges: dict, field_col: dict, canonical_id: str | None, row: int) -> str:
    if canonical_id is None or canonical_id not in field_col:
        return ""
    raw, _ = cell_resolved(ws, merges, row, field_col[canonical_id])
    return str(raw).strip() if raw is not None else ""


def _truncate(value: Any, max_len: int = 500) -> str:
    s = str(value) if value is not None else ""
    return s[:max_len] + "…" if len(s) > max_len else s


def _synthesise_columns(spec: ReshapeSpec, carried_fields: list[str]) -> list[ColumnSpec]:
    """Describe the EMITTED canonical fields (date grain + carried + amount)."""
    specs: list[ColumnSpec] = []
    idx = 0
    specs.append(ColumnSpec(name=spec.date_field, index=idx, detected_type="date"))
    for cid in carried_fields:
        idx += 1
        det = "decimal" if cid == spec.amount_field else "text"
        specs.append(ColumnSpec(name=cid, index=idx, detected_type=det))
    return specs


# ---------------------------------------------------------------------------
# Unified producer interface (Story 22.12, AD-1): one signature for both kinds,
# invoked at csv_excel_import.run_import's parse step. Returns the existing
# ParseResult with rows keyed by mdm_canonical_fields ids; everything downstream
# (managed_feed ledger + landing) is reused unchanged.
# ---------------------------------------------------------------------------


def _reshape_spec_from(reshape: dict[str, Any]) -> ReshapeSpec:
    """Build a ReshapeSpec from a template contract's ``reshape`` sub-document."""
    try:
        return ReshapeSpec(
            fields=dict(reshape["fields"]),
            amount_field=reshape["amount_field"],
            start_field=reshape["start_field"],
            end_field=reshape["end_field"],
            date_field=reshape.get("date_field", "date"),
            label_field=reshape.get("label_field"),
            sheet_name=reshape.get("sheet_name"),
            header_row=reshape.get("header_row"),
            metadata_rows=(
                tuple(reshape["metadata_rows"]) if reshape.get("metadata_rows") else None
            ),
            date_format=reshape.get("date_format"),
            total_markers=tuple(reshape.get("total_markers", ("total",))),
            grain=str(reshape.get("grain", GRAIN_DAILY)),
            line_key=str(reshape.get("line_key", "auto")),
            sheets=tuple(reshape.get("sheets") or ()),
            merged_amount_explode=(
                dict((reshape.get("merged_amount") or {}).get("explode") or {})
            ),
        )
    except (KeyError, TypeError) as exc:
        raise ReshapeProducerError(
            "invalid_reshape_spec",
            "the template's 'reshape' contract is missing required keys "
            "(fields/amount_field/start_field/end_field).",
        ) from exc


def _remap_to_canonical(parsed: ParseResult, mapping: dict[str, str]) -> ParseResult:
    """Rename a tabular ParseResult's columns to canonical field ids via ``mapping``.

    ``mapping`` is {source_column -> canonical_field_id}. Only mapped source
    columns are carried (extra columns are ignored -- never a silent drop of a
    REQUIRED field, which the 22.15 gate enforces separately). Rejected rows and
    the content hash pass through unchanged.
    """
    targets = sorted(mapping.items(), key=lambda kv: kv[1])
    new_rows = [{cid: row.get(src) for src, cid in targets} for row in parsed.rows]
    new_cols = [
        ColumnSpec(name=cid, index=i, detected_type="text")
        for i, (_src, cid) in enumerate(targets)
    ]
    return ParseResult(
        rows=new_rows,
        rejected=parsed.rejected,
        columns=new_cols,
        encoding=parsed.encoding,
        delimiter=parsed.delimiter,
        sheet_name=parsed.sheet_name,
        detected_row_count=parsed.detected_row_count,
        content_hash=parsed.content_hash,
        metadata=parsed.metadata,
    )


def produce(
    data: bytes,
    template: dict[str, Any],
    mapping: dict[str, str] | None = None,
    *,
    sample_only: bool = False,
) -> ParseResult:
    """Canonical-rows producer for a catalog template (AD-1 / AD-2).

    ONE signature for both template kinds. ``template`` is a file-source template
    row (or bare contract dict); ``mapping`` is {source_column -> canonical_field
    id} for the simple tabular path. Dispatch:

      * a contract carrying a ``reshape`` sub-document -> the media-plan-shaped
        Excel path (Story 22.10): reshape to daily canonical rows already keyed by
        canonical ids;
      * otherwise -> the simple tabular path: parse the CSV/Excel (Story 12.9) and
        rename columns to canonical ids via ``mapping``.

    Returns the existing ``ParseResult`` so ``run_import`` lands it through the
    unchanged ledger/landing chain -- no second parser, ledger, or landing path.
    """
    contract = template.get("contract") if "contract" in template else template
    if not isinstance(contract, dict):
        raise ReshapeProducerError("invalid_template", "template has no contract object.")

    reshape = contract.get("reshape")
    if reshape:
        spec = _reshape_spec_from(reshape)
        # ONE engine, two grains. The template DECLARES which one it needs; the
        # walk, the coercions, the merged-cell resolution and the money invariant
        # are the same code either way.
        if spec.grain == GRAIN_LINE:
            from core.file_source_plan_lines import (  # noqa: PLC0415
                reshape_workbook_to_lines,
            )

            return reshape_workbook_to_lines(data, spec, sample_only=sample_only)
        return reshape_workbook_to_daily(data, spec, sample_only=sample_only)

    # Simple tabular path: reuse the 12.9 parsers, then remap to canonical ids.
    from core.csv_excel_import import (  # noqa: PLC0415
        FORMAT_CSV,
        FORMAT_SAV,
        SUPPORTED_FORMATS,
        UnsupportedFileType,
        detect_format,
        parse_csv,
        parse_excel,
    )

    if not mapping:
        raise ReshapeProducerError(
            "mapping_required",
            "a tabular catalog template requires a source->canonical field mapping.",
        )
    header_row = int(contract.get("header_row", 1))
    date_format = contract.get("date_format")
    # Prefer the template's declared format; else detect from the bytes, falling
    # back to CSV (a header-less CSV has no magic bytes and no filename hint here).
    fmt = contract.get("format")
    if fmt not in SUPPORTED_FORMATS:
        try:
            fmt = detect_format(None, data)
        except UnsupportedFileType:
            fmt = FORMAT_CSV
    if fmt == FORMAT_CSV:
        parsed = parse_csv(data, header_row=header_row, date_format=date_format)
    elif fmt == FORMAT_SAV:
        from core.inbound_sav import parse_sav
        parsed = parse_sav(data)
    else:
        parsed = parse_excel(
            data, sheet_name=contract.get("sheet_name"), header_row=header_row,
            date_format=date_format,
        )
    return _remap_to_canonical(parsed, mapping)


# ---------------------------------------------------------------------------
# Matrix placement (Story 22.14, AD-6): stamp every landed row with its class
# and any declared-discriminator dimension; refuse a template with no class.
# ---------------------------------------------------------------------------


def _resolve_discriminator(
    discriminator: dict[str, Any], *, filename: str | None, metadata: dict[str, Any]
) -> tuple[str | None, Any]:
    """Resolve a declared discriminator to (dimension_id, value).

    ``source`` = 'cell'     -> value = metadata[<cell label>] (from the top block);
                 'filename'  -> value = a named 'value' group (or group 1 / whole
                                match) of <pattern> applied to the filename.
    An unresolved discriminator yields (dimension_id, None) -- honest, never a
    fabricated value.
    """
    dim = discriminator.get("dimension")
    src = discriminator.get("source")
    value: Any = None
    if src == "cell":
        value = metadata.get(discriminator.get("cell"))
    elif src == "filename" and filename and discriminator.get("pattern"):
        match = re.search(discriminator["pattern"], filename)
        if match:
            named = match.groupdict().get("value")
            value = named if named is not None else (
                match.group(1) if match.groups() else match.group(0)
            )
    return dim, value


def evaluate_variant_discriminator(
    template: dict[str, Any],
    *,
    filename: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a declared variant discriminator and flag it if undetectable (Story 22.18).

    Several country/market variants (FR, DE, ...) land in ONE datastream: a
    declared discriminator (a named metadata cell or a filename pattern) is
    promoted to a market/country dimension. When the discriminator is DECLARED but
    its value cannot be detected on this file, the import is FLAGGED for human
    confirmation rather than landing a mislabeled / null dimension.

    Returns {dimension, value, detected, flagged, reason}. A template that declares
    no discriminator is not a variant datastream -> nothing to flag.
    """
    contract = _template_contract(template)
    discriminator = contract.get("discriminator")
    if not isinstance(discriminator, dict):
        return {"dimension": None, "value": None, "detected": False,
                "flagged": False, "reason": None}

    dim, value = _resolve_discriminator(
        discriminator, filename=filename, metadata=metadata or {}
    )
    detected = value is not None and str(value).strip() != ""
    return {
        "dimension": dim,
        "value": value if detected else None,
        "detected": detected,
        "flagged": not detected,  # declared but undetectable -> confirm, don't mislabel
        "reason": None if detected else "undetectable_discriminator",
    }


def stamp_placement(
    result: ParseResult,
    template: dict[str, Any],
    *,
    filename: str | None = None,
) -> ParseResult:
    """Stamp every row with its matrix class + declared-discriminator dimension.

    AD-6: an import whose template declares NO class is refused BEFORE landing
    (a ReshapeProducerError, not a silent pass). The class (planned/actual/
    extrapolated) is stamped on ``_placement_class``; an optional declared
    discriminator (a named cell value or a filename pattern) is promoted to its
    declared canonical dimension on every row.

    Mutates and returns ``result`` (rows carry the placement so downstream can
    reconcile plan-vs-actual on the same coordinates).

    The stamped keys are also DECLARED on ``result.columns``. Stamping the rows
    alone was enough to reach the warehouse -- the landing composes its column
    list from the row keys -- but not enough to be SEEN: the required-field gate
    reads ``result.columns``, so a discriminator dimension the template declares
    REQUIRED was stamped on every row and simultaneously reported missing, and
    the import was refused for the absence of a field it had just landed.
    """
    contract = template.get("contract") if "contract" in template else template
    if not isinstance(contract, dict):
        raise ReshapeProducerError("invalid_template", "template has no contract object.")

    placement_class = contract.get("class")
    if placement_class not in PLACEMENT_CLASSES:
        raise ReshapeProducerError(
            "no_placement_class",
            "the template declares no matrix class (planned/actual/extrapolated); "
            "the import is refused before landing (AD-6).",
        )

    discriminator = contract.get("discriminator")
    disc_dim, disc_val = (None, None)
    if isinstance(discriminator, dict):
        disc_dim, disc_val = _resolve_discriminator(
            discriminator, filename=filename, metadata=result.metadata or {}
        )

    for row in result.rows:
        row[PLACEMENT_CLASS_KEY] = placement_class
        if disc_dim:
            row[disc_dim] = disc_val

    declared = {spec.name for spec in result.columns}
    for name in (PLACEMENT_CLASS_KEY, disc_dim):
        if name and name not in declared:
            result.columns.append(
                ColumnSpec(name=name, index=len(result.columns), detected_type="text")
            )
            declared.add(name)
    return result


def reconcile_amounts(result: ParseResult, amount_field: str) -> dict[str, Any] | None:
    """Check ``file total == landed + rejected`` on a reshaped result.

    Returns ``None`` when the producer measured no file-side total (a CSV/adaptation
    result, or a bounded preview): an unmeasured invariant is skipped, never
    guessed. Otherwise returns the three amounts and whether they reconcile.

    This is what makes the amount invariant real. ``landed_total`` computed the
    landed half and had no caller outside its tests, so a merged amount cell
    forward-filled onto every covered row multiplied the money and no gate said a
    word. The file-side half is measured by an INDEPENDENT pass, so a walk that
    counts money twice cannot also move the number it is compared against.
    """
    if result.source_amount_total is None:
        return None
    file_amount = Decimal(result.source_amount_total)
    rejected_amount = Decimal(result.rejected_amount_total or "0.00")
    landed = landed_total(result, amount_field)
    return {
        "file": format(file_amount, "f"),
        "landed": format(landed, "f"),
        "rejected": format(rejected_amount, "f"),
        "reconciled": file_amount == landed + rejected_amount,
    }


def landed_total(result: ParseResult, amount_field: str) -> Decimal:
    """Sum a canonical amount field across the landed rows (Decimal, exact).

    Used to prove the F-1 reconciliation invariant (landed total == file total)
    without re-deriving amounts through a different path.
    """
    total = Decimal("0.00")
    for row in result.rows:
        raw = row.get(amount_field)
        if raw is None:
            continue
        total += Decimal(str(raw))
    return total


# ---------------------------------------------------------------------------
# Lock, replay & drift (Story 22.16, AD-8): a locked template replays
# byte-identically by upload and by email; source drift re-enters the gate.
# ---------------------------------------------------------------------------

DRIFT_OK = "ok"  # required source columns all present -> deterministic replay
DRIFT_NEEDS_REVALIDATION = "needs_revalidation"  # a required source disappeared -> gate


class TemplateNotLocked(ReshapeProducerError):
    """Replay was requested on a template that is not a LOCKED, sealed artifact."""

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(
            "template_not_locked",
            detail or "replay requires a LOCKED, content-hashed template (Story 22.11).",
        )


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _assert_template_locked(template: dict[str, Any]) -> None:
    """Refuse replay unless the template is a genuinely SEALED artifact (AD-8).

    The naive guard was ``not template.get("content_hash")``. That can only fire on
    a dict fabricated in a test: migration 097 declares ``content_hash`` NOT NULL
    and ``file_source_template`` computes it unconditionally, so EVERY persisted
    row has one. "Locked" was therefore not a state the schema could distinguish.

    The lock evidence a persisted row does carry is the SEAL: ``content_hash`` is
    the SHA-256 of the normalised contract stored beside it. So the check is that
    the seal still holds --

      * a hash that is not a SHA-256 digest seals nothing;
      * a contract that no longer validates is not a locked contract;
      * a hash that does not re-derive from the contract means the contract was
        edited after the lock, or the row was assembled by hand.

    NOT covered here, and it does not need a schema change any more: the AD-7
    *human gate confirmation*. ``file_source_gate.confirm_mapping_version``
    (renamed from ``record_gate_confirmation``, removed 2026-08-09) writes an
    immutable row into ``app.file_source_template_confirmations`` carrying the
    template id, its content hash, the minted mapping version and the sample hash.
    So "a human confirmed THIS lock" IS provable -- by that table, not by a column
    on the template row, and not by this function, which stays pure (no
    connection). Re-measured 2026-08-17; this note previously said the proof did
    not exist.
    """
    content_hash = template.get("content_hash")
    if not content_hash:
        raise TemplateNotLocked()
    if not isinstance(content_hash, str) or not _SHA256_RE.match(content_hash):
        raise TemplateNotLocked(
            "the template's content_hash is not a SHA-256 digest; it seals nothing."
        )

    contract = _template_contract(template)
    try:
        sealed = compute_content_hash(validate_template_contract(contract))
    except FileSourceTemplateError as exc:
        raise TemplateNotLocked(
            f"the template's contract is not a valid locked contract: {exc}"
        ) from exc
    if sealed != content_hash:
        raise TemplateNotLocked(
            "the template's content_hash does not seal its contract: the contract "
            "changed after the lock, or the row was not produced by "
            "create_file_source_template."
        )


def _template_contract(template: dict[str, Any]) -> dict[str, Any]:
    contract = template.get("contract") if "contract" in template else template
    if not isinstance(contract, dict):
        raise ReshapeProducerError("invalid_template", "template has no contract object.")
    return contract


def canonical_rows_signature(result: ParseResult) -> str:
    """A stable SHA-256 over the landed rows -- the 'byte-identical' witness.

    Two replays of the SAME bytes through the SAME locked template (whatever the
    ingress -- upload or email) yield the SAME signature (AD-8 idempotent replay).
    """
    canonical = json.dumps(result.rows, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_source_columns(data: bytes, template: dict[str, Any]) -> list[str]:
    """Return the source header column names of ``data`` under this template.

    Used to detect source drift (added / reordered / disappeared columns). Works
    for both the reshape (media-plan) path and the simple tabular path.
    """
    contract = _template_contract(template)
    reshape = contract.get("reshape")
    if reshape:
        ws = _load_sheet(data, reshape.get("sheet_name"))
        header_row = reshape.get("header_row")
        if header_row is None:
            header_row = _detect_header_row(ws, _reshape_spec_from(reshape))
        merges = resolve_merges(ws)
        cols: list[str] = []
        for c in range(1, (ws.max_column or 0) + 1):
            raw, _ = cell_resolved(ws, merges, header_row, c)
            if raw is not None and str(raw).strip():
                cols.append(str(raw).strip())
        return cols

    from core.csv_excel_import import (  # noqa: PLC0415
        FORMAT_CSV,
        FORMAT_SAV,
        SUPPORTED_FORMATS,
        UnsupportedFileType,
        detect_format,
        parse_csv,
        parse_excel,
    )

    header_row = int(contract.get("header_row", 1))
    fmt = contract.get("format")
    if fmt not in SUPPORTED_FORMATS:
        try:
            fmt = detect_format(None, data)
        except UnsupportedFileType:
            fmt = FORMAT_CSV
    if fmt == FORMAT_CSV:
        parsed = parse_csv(data, header_row=header_row)
    elif fmt == FORMAT_SAV:
        from core.inbound_sav import parse_sav
        parsed = parse_sav(data)
    else:
        parsed = parse_excel(
            data, sheet_name=contract.get("sheet_name"), header_row=header_row
        )
    return [c.name for c in parsed.columns]


def _fold_column(name: Any) -> str:
    """Fold a column name the way ``reshape.resolve_column_index`` compares it."""
    return str(name).strip().casefold()


def _reshape_declared_sources(reshape: dict[str, Any]) -> list[str]:
    """The SOURCE column keys a reshape contract declares.

    ``reshape["fields"]`` is {canonical id -> source column key} -- the INVERSE of
    the tabular ``mapping``. On this path every declared column is structurally
    required: ``reshape_workbook_to_daily`` raises ``column_not_found`` as soon as
    one of them does not resolve on the header row. So the declared set IS the
    required set; there is no "optional" declared column to soften.
    """
    spec = _reshape_spec_from(reshape)  # raises invalid_reshape_spec on a broken spec
    return list(spec.fields.values())


def detect_source_drift(
    template: dict[str, Any],
    mapping: dict[str, str] | None,
    current_columns: list[str],
) -> dict[str, Any]:
    """Classify a file's source columns against the locked template (AD-8).

    TWO paths, because the two producer paths bind their columns differently:

      * **reshape / media-plan** -- the contract's ``reshape.fields`` declares
        {canonical -> SOURCE column} and ``produce`` IGNORES ``mapping`` entirely.
        Indexing drift on ``mapping`` made this function structurally blind on
        exactly the file class Epic 22 Phase B is about: a locked reshape template
        whose source columns had ALL disappeared reported ``ok``.
      * **simple tabular** -- ``mapping`` is {SOURCE column -> canonical}, and only
        the sources feeding a REQUIRED canonical field are blocking.

    Binding is by column NAME (trimmed, case-insensitive, mirroring
    ``reshape.resolve_column_index``), so an ADDED or REORDERED column is harmless.
    A DISAPPEARED required source column returns ``needs_revalidation``: the file
    re-enters the AD-7 gate for human re-confirmation, never a silent re-map.

    Stated rather than hidden: ``resolve_column_index`` also accepts an Excel
    column LETTER as a fallback key. Such a key binds POSITIONALLY and cannot be
    judged by name, so a template declaring one is reported as drifted -- fail
    closed, toward the gate, rather than silently passed.

    Returns {status, missing_required_sources, added_columns}.
    """
    contract = _template_contract(template)
    reshape = contract.get("reshape")

    if reshape:
        declared_sources = _reshape_declared_sources(reshape)
        required_sources = set(declared_sources)
    else:
        required = set(contract.get("required_fields") or [])
        declared_sources = list(mapping or {})
        required_sources = {
            source_col
            for source_col, canonical_id in (mapping or {}).items()
            if canonical_id in required
        }

    present = {_fold_column(c) for c in current_columns}
    missing_required_sources = sorted(
        source_col for source_col in required_sources
        if _fold_column(source_col) not in present
    )
    declared = {_fold_column(s) for s in declared_sources}
    added_columns = sorted(
        c for c in current_columns if _fold_column(c) not in declared
    )
    status = DRIFT_NEEDS_REVALIDATION if missing_required_sources else DRIFT_OK
    return {
        "status": status,
        "missing_required_sources": missing_required_sources,
        "added_columns": added_columns,
    }


def replay_locked_template(
    data: bytes,
    template: dict[str, Any],
    mapping: dict[str, str] | None = None,
    *,
    filename: str | None = None,
) -> ParseResult:
    """Deterministically replay a LOCKED template over ``data`` (AD-8).

    Ingress parity: upload and email are only transports of the same bytes, so the
    same (locked template + mapping + bytes) yields byte-identical canonical rows
    (see ``canonical_rows_signature``). Requires the template to be locked -- i.e.
    its content_hash must actually SEAL its contract, not merely be present
    (``_assert_template_locked``).
    """
    _assert_template_locked(template)
    return stamp_placement(produce(data, template, mapping), template, filename=filename)
