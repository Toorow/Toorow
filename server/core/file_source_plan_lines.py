"""toorow -- the LINE grain of the one file-source engine (chantier 67-25b).

``file_source_producer.reshape_workbook_to_daily`` always spreads a line across
its [start, end] range. That is right for a KPI export and wrong for a media
plan: the plan model keeps the LINE (its span and its total) and spreads again
at publish (``mediaplan_store``), so a daily producer output would both do that
work twice and lose the span.

This module is the second grain of the SAME engine, not a second engine. It
consumes the same reshape primitives (``core.reshape``) and the same value
coercions (``file_source_producer._parse_amount`` / ``_parse_date``) as the daily
grain, and emits the same ``ParseResult``. What it adds is the four template
capabilities `file-source-ingestion.md` declared a media plan needs and the
daily grain cannot express:

  * ``grain: "line"``     -- one row per plan line (span + total), no spread;
  * ``sheets: [...]``     -- several sheets of ONE workbook in ONE import, with
                             the row-identity registry SHARED across them;
  * ``line_key``          -- a row identity, composed ``<sheet>/<label>`` or read
                             from a named column; two rows resolving to the same
                             one refuse the import rather than silently keeping
                             the last;
  * ``merged_amount``     -- a merged amount cell spanning N physical rows is ONE
                             amount: it COLLAPSES into one line spanning the
                             group (min start .. max end), unless the template
                             explicitly explodes it with weights that sum
                             EXACTLY to the merged amount (else 422). The explode
                             is keyed by the merge's PHYSICAL A1 range, so a
                             shifted merge stops applying rather than splitting
                             on a stale key.

The money invariant is the daily grain's, unchanged and measured the same way:
``file total`` comes from a pass INDEPENDENT of the walk that produced the rows
(``_source_amount_total``), so a walk that counts a merge twice cannot also move
the number it is compared against.

Landing is NOT this module's concern: it returns canonical rows, and
``import_runner.run_import`` routes them to the target the template declares.
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal
from typing import Any

from core.file_source_producer import (
    MAX_SHEET_COLUMNS,
    MAX_SHEET_ROWS,
    RULE_BAD_AMOUNT,
    RULE_GROUP_OR_NOISE,
    RULE_INVERTED_DATES,
    RULE_MISSING_DATES,
    RULE_SUBTOTAL_OR_TOTAL,
    ReshapeProducerError,
    ReshapeSpec,
    _content_hash,
    _detect_header_row,
    _extract_metadata,
    _load_sheet,
    _parse_amount,
    _parse_date,
    _read_text,
    _row_is_blank,
    _source_amount_total,
    _truncate,
)
from core.reshape import CENT, cell_resolved, resolve_column_index, resolve_merges
from core.tabular_types import ColumnSpec, ParseResult, RejectedRow

#: The canonical field id carrying the row identity on every emitted line.
LINE_KEY_FIELD = "line_key"

#: ``line_key: "auto"`` -- compose the identity from ``<sheet>/<label>`` rather
#: than reading it from a declared column.
LINE_KEY_AUTO = "auto"

#: A merged amount range whose covered rows carry no usable date at all: the
#: money is real and cannot be placed, so it is rejected ONCE (never per row).
RULE_MERGED_GROUP_NO_DATE = "merged_group_no_date"

#: A merged amount range whose top-left cell is not a number (a merged header
#: band). Noise, and its rows are evidence.
RULE_MERGED_GROUP_NOT_NUMERIC = "merged_group_not_numeric"

#: The sub-labels a COLLAPSED merged group came from, carried on the emitted line
#: so a report can show what the one line stands for. Underscore-prefixed, like
#: ``PLACEMENT_CLASS_KEY``, so it can never collide with a canonical field id --
#: it is evidence about the row, not a field OF the row, and no landing target
#: stores it.
GROUP_DETAIL_KEY = "_group_detail"


class DuplicateLineKey(ReshapeProducerError):
    """Two rows resolved to the same ``line_key`` -> 422, never a silent keep-last."""

    def __init__(self, key: str, first_sheet: str, second_sheet: str) -> None:
        super().__init__(
            "duplicate_line_key",
            f"Row key '{key}' appears twice (sheets '{first_sheet}' and "
            f"'{second_sheet}'); a plan line identity must be unique across the "
            "whole workbook.",
            repair={"line_key": key, "sheets": [first_sheet, second_sheet]},
        )


def slugify(value: str) -> str:
    """ASCII-ish slug used to compose a ``line_key`` from sheet + label.

    Lowercase, accents folded to ASCII, non-alphanumerics collapsed to '-'.
    Dependency-free (no external transliteration lib). This is the identity
    composition the plan store has always used; it lives here now because the
    engine composes it, and the legacy adapter re-exports it unchanged so a
    key computed before the convergence equals the key computed after it.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    out: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def _explode_weights(spec: ReshapeSpec) -> dict[str, list[Any]]:
    return dict(spec.merged_amount_explode or {})


def _sheet_names(spec: ReshapeSpec) -> list[str | None]:
    """The sheets ONE import walks, in declared order.

    ``sheets`` is the multi-sheet capability; ``sheet_name`` stays the
    single-sheet form the daily grain uses. Declaring neither walks the active
    sheet, exactly as the daily grain does.
    """
    if spec.sheets:
        return list(spec.sheets)
    return [spec.sheet_name]


def _synthesise_line_columns(spec: ReshapeSpec) -> list[ColumnSpec]:
    """Describe the EMITTED fields at the line grain.

    Unlike the daily grain, the start/end columns are NOT consumed into a single
    date: the line's SPAN is the thing the plan store stores. ``line_key`` leads
    because it is the row's identity.
    """
    specs = [ColumnSpec(name=LINE_KEY_FIELD, index=0, detected_type="text")]
    idx = 0
    for cid in spec.fields:
        idx += 1
        if cid in (spec.start_field, spec.end_field):
            detected = "date"
        elif cid == spec.amount_field:
            detected = "decimal"
        else:
            detected = "text"
        specs.append(ColumnSpec(name=cid, index=idx, detected_type=detected))
    return specs


class LineKeyRegistry:
    """The row-identity registry, SHARED across every sheet of one import."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def register(self, key: str, sheet_name: str) -> None:
        if key in self._seen:
            raise DuplicateLineKey(key, self._seen[key], sheet_name)
        self._seen[key] = sheet_name


def reshape_workbook_to_lines(
    data: bytes,
    spec: ReshapeSpec,
    *,
    sample_only: bool = False,
    sample_line_limit: int = 100,
) -> ParseResult:
    """Reshape a workbook into PLAN-LINE canonical rows -> ``ParseResult``.

    One row per plan line, carrying its span (start/end) and its TOTAL amount --
    never spread to daily. Walks every declared sheet in order with ONE shared
    line-key registry, so a duplicate identity across two sheets is caught.

    Raises ``ReshapeProducerError`` on structural failure (unreadable, no header,
    missing declared column, duplicate line key, bad explode split).
    """
    if not data:
        raise ReshapeProducerError("empty_file", "The uploaded file is empty.")

    content_hash = _content_hash(data)
    registry = LineKeyRegistry()

    rows_out: list[dict[str, Any]] = []
    rejected: list[RejectedRow] = []
    metadata: dict[str, Any] = {}
    source_lines_seen = 0
    file_total = Decimal("0.00")
    rejected_amount = Decimal("0.00")
    measured_file_total = not sample_only
    first_sheet_title: str | None = None

    for sheet in _sheet_names(spec):
        sheet_result = _walk_one_sheet(
            data,
            spec,
            sheet_name=sheet,
            registry=registry,
            sample_only=sample_only,
            sample_line_limit=max(0, sample_line_limit - source_lines_seen),
        )
        if first_sheet_title is None:
            first_sheet_title = sheet_result["title"]
        rows_out.extend(sheet_result["rows"])
        rejected.extend(sheet_result["rejected"])
        source_lines_seen += sheet_result["lines_seen"]
        rejected_amount += sheet_result["rejected_amount"]
        if measured_file_total:
            file_total += sheet_result["file_total"]
        # A top metadata block is read per sheet; the first declaring one wins,
        # and later sheets only add keys they alone carry (never overwrite).
        for key, value in (sheet_result["metadata"] or {}).items():
            metadata.setdefault(key, value)

    if not rows_out and not rejected:
        raise ReshapeProducerError(
            "no_data_rows",
            "No data rows found below the header row on any declared sheet.",
        )

    return ParseResult(
        rows=rows_out,
        rejected=rejected,
        columns=_synthesise_line_columns(spec),
        encoding="xlsx",
        delimiter=None,
        sheet_name=first_sheet_title,
        detected_row_count=source_lines_seen,
        content_hash=content_hash,
        metadata=metadata,
        source_amount_total=format(file_total, "f") if measured_file_total else None,
        rejected_amount_total=(
            format(rejected_amount, "f") if measured_file_total else None
        ),
    )


def walk_sheet_lines(
    data: bytes,
    spec: ReshapeSpec,
    *,
    sheet_name: str | None,
    registry: LineKeyRegistry,
) -> dict[str, Any]:
    """Walk ONE sheet at the line grain against a CALLER-OWNED registry.

    The seam a caller needs when each sheet has its OWN spec -- different column
    names per sheet -- which the ``sheets`` capability (one spec, many sheets)
    does not cover. The registry is passed in precisely so row identities stay
    unique across sheets that share nothing else.

    Returns the per-sheet walk result: rows, rejected, totals and the sheet title.
    """
    return _walk_one_sheet(
        data, spec, sheet_name=sheet_name, registry=registry,
        sample_only=False, sample_line_limit=0,
    )


def _walk_one_sheet(
    data: bytes,
    spec: ReshapeSpec,
    *,
    sheet_name: str | None,
    registry: LineKeyRegistry,
    sample_only: bool,
    sample_line_limit: int,
) -> dict[str, Any]:
    """Walk ONE sheet at the line grain. Returns its rows + evidence + totals."""
    ws = _load_sheet(data, sheet_name)
    title = ws.title

    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    if max_row > MAX_SHEET_ROWS or max_col > MAX_SHEET_COLUMNS:
        raise ReshapeProducerError(
            "sheet_too_large",
            f"Sheet '{title}' is too large ({max_row}x{max_col}); "
            f"maximum {MAX_SHEET_ROWS}x{MAX_SHEET_COLUMNS}.",
        )

    merges = resolve_merges(ws)
    metadata = (
        _extract_metadata(ws, merges, spec.metadata_rows) if spec.metadata_rows else {}
    )
    header_row = _detect_header_row(ws, spec)

    field_col: dict[str, int] = {}
    for canonical_id, key in spec.fields.items():
        idx = resolve_column_index(ws, header_row, key)
        if idx is None:
            raise ReshapeProducerError(
                "column_not_found",
                f"Declared column '{key}' (field '{canonical_id}') not found on "
                f"header row {header_row} of sheet '{title}'.",
                repair={"check_column_name_or_letter": key, "sheet": title},
            )
        field_col[canonical_id] = idx

    for required in (spec.amount_field, spec.start_field, spec.end_field):
        if required not in field_col:
            raise ReshapeProducerError(
                "missing_required_column",
                f"The spec must map the '{required}' column.",
            )

    # A template may name the line_key as a source column instead of composing it.
    lk_mode = (spec.line_key or LINE_KEY_AUTO).strip() or LINE_KEY_AUTO
    lk_col: int | None = None
    if lk_mode != LINE_KEY_AUTO:
        lk_col = resolve_column_index(ws, header_row, lk_mode)
        if lk_col is None:
            raise ReshapeProducerError(
                "column_not_found",
                f"Declared line_key column '{lk_mode}' not found on header row "
                f"{header_row} of sheet '{title}'.",
                repair={"check_column_name_or_letter": lk_mode, "sheet": title},
            )

    descriptive = [cid for cid in spec.fields if cid != spec.amount_field]
    label_id = spec.label_field or (descriptive[0] if descriptive else None)
    markers = tuple(m.strip().casefold() for m in spec.total_markers if m.strip())
    explode = _explode_weights(spec)

    first_data_row = header_row + 1
    last_row = ws.max_row or header_row

    ctx = _SheetContext(
        ws=ws,
        merges=merges,
        spec=spec,
        title=title,
        field_col=field_col,
        label_id=label_id,
        markers=markers,
        lk_col=lk_col,
        registry=registry,
        rows_out=[],
        rejected=[],
        rejected_amount=Decimal("0.00"),
        lines_seen=0,
    )

    amount_col = field_col[spec.amount_field]
    row = first_data_row
    while row <= last_row:
        if _row_is_blank(ws, merges, row, max_col):
            row += 1
            continue
        if sample_only and ctx.lines_seen >= sample_line_limit:
            break

        raw_amount, amount_range = cell_resolved(ws, merges, row, amount_col)
        if amount_range is None:
            ctx.lines_seen += 1
            _emit_single_row(ctx, row=row, raw_amount=raw_amount)
            row += 1
            continue

        # A merged amount: gather the full run of rows sharing this physical range.
        group_rows: list[int] = []
        probe = row
        while probe <= last_row:
            _, rng = cell_resolved(ws, merges, probe, amount_col)
            if rng != amount_range:
                break
            group_rows.append(probe)
            probe += 1

        ctx.lines_seen += 1
        _emit_merged_group(
            ctx,
            group_rows=group_rows,
            amount_range=amount_range,
            raw_amount=raw_amount,
            explode=explode,
        )
        row = probe

    file_total = Decimal("0.00")
    if not sample_only:
        file_total = _source_amount_total(
            ws,
            merges,
            amount_col=amount_col,
            first_data_row=first_data_row,
            last_row=last_row,
        )

    return {
        "title": title,
        "rows": ctx.rows_out,
        "rejected": ctx.rejected,
        "rejected_amount": ctx.rejected_amount,
        "file_total": file_total,
        "lines_seen": ctx.lines_seen,
        "metadata": metadata,
    }


class _SheetContext:
    """Mutable per-sheet walk state (kept out of the argument lists)."""

    __slots__ = (
        "ws", "merges", "spec", "title", "field_col", "label_id", "markers",
        "lk_col", "registry", "rows_out", "rejected", "rejected_amount",
        "lines_seen",
    )

    def __init__(self, **kw: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, kw[name])

    def text(self, cid: str | None, row: int) -> str:
        if not cid:
            return ""
        return _read_text(self.ws, self.merges, self.field_col, cid, row)

    def date_at(self, cid: str, row: int):
        return _parse_date(
            cell_resolved(self.ws, self.merges, row, self.field_col[cid])[0],
            date_format=self.spec.date_format,
        )

    def reject(
        self, row: int, field_name: str, rule: str, reason: str, value: Any,
        *, amount: Decimal | None = None,
    ) -> None:
        """Record one rejected row -- with the money it carried, when readable."""
        self.rejected.append(
            RejectedRow(
                row_number=row,
                field_name=field_name,
                rule=rule,
                reason=reason,
                rejected_value=_truncate(value),
                rejected_amount=None if amount is None else format(amount, "f"),
            )
        )

    def compose_key(self, row: int, label: str) -> str:
        if self.lk_col is not None:
            raw, _ = cell_resolved(self.ws, self.merges, row, self.lk_col)
            key = str(raw or "").strip()
            if key:
                return key
        return f"{slugify(self.title)}/{slugify(label)}"

    def emit(
        self, *, row: int, label: str, amount: Decimal, start, end,
        detail: list[str] | None = None,
    ) -> None:
        """Build and register ONE plan line."""
        if start > end:
            raise ReshapeProducerError(
                "inverted_dates",
                f"Sheet '{self.title}', row {row}: start date {start} is after "
                f"end date {end}.",
            )
        spec = self.spec
        out: dict[str, Any] = {}
        for cid in spec.fields:
            if cid == spec.amount_field:
                out[cid] = format(amount, "f")
            elif cid == spec.start_field:
                out[cid] = start.isoformat()
            elif cid == spec.end_field:
                out[cid] = end.isoformat()
            elif cid == self.label_id:
                out[cid] = label
            else:
                out[cid] = self.text(cid, row) or None
        key = self.compose_key(row, label)
        self.registry.register(key, self.title)
        out[LINE_KEY_FIELD] = key
        if detail:
            out[GROUP_DETAIL_KEY] = detail
        self.rows_out.append(out)


def _emit_single_row(ctx: _SheetContext, *, row: int, raw_amount: Any) -> None:
    """One physical row with an UNMERGED amount cell."""
    spec = ctx.spec
    label = ctx.text(ctx.label_id, row)
    amount = _parse_amount(raw_amount)

    if label and any(m in label.casefold() for m in ctx.markers):
        if amount is not None:
            ctx.rejected_amount += amount
        ctx.reject(
            row, ctx.label_id or "", RULE_SUBTOTAL_OR_TOTAL,
            f"subtotal/total row skipped for landing: '{label}'", label,
            amount=amount,
        )
        return

    start = ctx.date_at(spec.start_field, row)
    end = ctx.date_at(spec.end_field, row)

    if amount is None and start is None and end is None:
        ctx.reject(
            row, ctx.label_id or "", RULE_GROUP_OR_NOISE,
            "group-label or empty row (no amount, no dates)", label,
        )
        return
    if amount is None:
        ctx.reject(
            row, spec.amount_field, RULE_BAD_AMOUNT,
            "line has a date range but no numeric amount", str(raw_amount),
        )
        return
    if start is None or end is None:
        # The money is READABLE, so it is counted as rejected: the invariant
        # reads file == landed + rejected and the amount stays visible.
        ctx.rejected_amount += amount
        ctx.reject(
            row, spec.start_field, RULE_MISSING_DATES,
            "line has an amount but no valid start/end date range (no valid dates)",
            label, amount=amount,
        )
        return
    if start > end:
        ctx.rejected_amount += amount
        ctx.reject(
            row, spec.start_field, RULE_INVERTED_DATES,
            f"start date {start} is after end date {end}", f"{start}..{end}",
            amount=amount,
        )
        return

    ctx.emit(row=row, label=label, amount=amount, start=start, end=end)


def _emit_merged_group(
    ctx: _SheetContext,
    *,
    group_rows: list[int],
    amount_range: str,
    raw_amount: Any,
    explode: dict[str, list[Any]],
) -> None:
    """A merged amount range: ONE amount, landing ONCE.

    Default is COLLAPSE -- one line spanning the whole group (min start .. max
    end). Splitting the group is an EXPLICIT template choice keyed by the merge's
    physical A1 range, with weights that must sum exactly to the merged amount.
    """
    spec = ctx.spec
    group_amount = _parse_amount(raw_amount)
    if group_amount is None:
        for r in group_rows:
            ctx.reject(
                r, spec.amount_field, RULE_MERGED_GROUP_NOT_NUMERIC,
                f"merged group {amount_range} skipped: non-numeric amount",
                ctx.text(ctx.label_id, r),
            )
        return

    sub = [
        {
            "row": r,
            "label": ctx.text(ctx.label_id, r),
            "start": ctx.date_at(spec.start_field, r),
            "end": ctx.date_at(spec.end_field, r),
        }
        for r in group_rows
    ]

    if amount_range in explode:
        _explode_merged_group(
            ctx,
            amount_range=amount_range,
            group_amount=group_amount,
            weights=explode[amount_range],
            sub=sub,
        )
        return

    starts = [s["start"] for s in sub if s["start"] is not None]
    ends = [s["end"] for s in sub if s["end"] is not None]
    if not starts or not ends:
        # The merged amount is READABLE but unplaceable: reject the group ONCE
        # (never once per covered row) so the money is counted a single time.
        for r in group_rows:
            ctx.reject(
                r, spec.start_field, RULE_MERGED_GROUP_NO_DATE,
                f"merged group {amount_range} skipped: no valid date",
                ctx.text(ctx.label_id, r),
                # The money of a merged range is ONE amount: it is attributed to
                # the anchor row alone, never once per covered row.
                amount=group_amount if r == group_rows[0] else None,
            )
        ctx.rejected_amount += group_amount
        return

    # Label: the merged label value when the label cell covers the whole group,
    # else the first sub-label marked as a group.
    label_col = ctx.field_col.get(ctx.label_id) if ctx.label_id else None
    label = ""
    if label_col is not None:
        label_val, label_range = cell_resolved(ctx.ws, ctx.merges, group_rows[0], label_col)
        covers = label_range is not None and all(
            cell_resolved(ctx.ws, ctx.merges, r, label_col)[1] == label_range
            for r in group_rows
        )
        if covers and label_val:
            label = str(label_val).strip()
    if not label:
        first = next((s["label"] for s in sub if s["label"]), "")
        n = len(group_rows)
        label = f"{first} (groupe de {n})" if first else f"Groupe de {n}"

    ctx.emit(
        row=group_rows[0],
        label=label,
        amount=group_amount,
        start=min(starts),
        end=max(ends),
        detail=[s["label"] for s in sub if s["label"]],
    )


def _explode_merged_group(
    ctx: _SheetContext,
    *,
    amount_range: str,
    group_amount: Decimal,
    weights: list[Any],
    sub: list[dict[str, Any]],
) -> None:
    """The EXPLICIT split: N lines whose parts sum EXACTLY to the merged amount."""
    if len(weights) != len(sub):
        raise ReshapeProducerError(
            "explode_arity_mismatch",
            f"Split of {amount_range}: {len(weights)} weights supplied for "
            f"{len(sub)} covered rows.",
            repair={"merged_amount_range": amount_range, "covered_rows": len(sub)},
        )
    parts: list[Decimal] = []
    for w in weights:
        if isinstance(w, bool):
            raise ReshapeProducerError(
                "explode_invalid_weight",
                f"Split of {amount_range}: invalid weight '{w}' (a number is expected).",
                repair={"merged_amount_range": amount_range},
            )
        try:
            weight = Decimal(str(w))
        except (ArithmeticError, ValueError) as exc:
            raise ReshapeProducerError(
                "explode_invalid_weight",
                f"Split of {amount_range}: invalid weight '{w}' (a number is expected).",
                repair={"merged_amount_range": amount_range},
            ) from exc
        if not weight.is_finite() or weight <= 0:
            raise ReshapeProducerError(
                "explode_invalid_weight",
                f"Split of {amount_range}: invalid distribution "
                f"(negative or zero weight '{w}').",
                repair={"merged_amount_range": amount_range},
            )
        parts.append(weight.quantize(CENT))

    total = sum(parts, Decimal("0.00"))
    if total != group_amount:
        raise ReshapeProducerError(
            "explode_sum_mismatch",
            f"Split of {amount_range}: the distribution ({format(total, 'f')}) does "
            f"not sum exactly to the merged amount ({format(group_amount, 'f')}).",
            repair={
                "merged_amount_range": amount_range,
                "expected": format(group_amount, "f"),
                "supplied": format(total, "f"),
            },
        )

    for part, s in zip(parts, sub):
        if s["start"] is None or s["end"] is None:
            raise ReshapeProducerError(
                "explode_missing_dates",
                f"Split of {amount_range}: the covered row {s['row']} "
                f"('{s['label']}') has no valid date range.",
                repair={"merged_amount_range": amount_range, "row": s["row"]},
            )
        ctx.emit(
            row=s["row"],
            label=s["label"] or f"{amount_range} sub-line",
            amount=part,
            start=s["start"],
            end=s["end"],
        )
